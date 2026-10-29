from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import uuid

from . import __version__
from .core import (VaultError, absolute, copy_tree, discover_git, inside, inventory,
                   no_link_ancestors, now, read_json, verify_payload, write_json)
from .secure import load_password

DEFAULT_EXCLUDES = ["node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"]


def load_config(path):
    c = read_json(path)
    if c.get("format") != "codex-vault-config/1":
        raise VaultError("設定ファイルの形式が未対応です")
    if not re.fullmatch(r"[a-f0-9]{32}", c.get("vault_id", "")):
        raise VaultError("vault_idが不正です")
    for key in ("projects", "extra_sources", "project_excludes"):
        value = c.get(key, [])
        if not isinstance(value, list) or any(not isinstance(p, str) or not p for p in value):
            raise VaultError(f"{key}には空でない文字列の配列を指定してください")
        c[key] = value
    if not isinstance(c.get("include_auth", False), bool):
        raise VaultError("include_authにはJSONのtrue/falseを指定してください（文字列は不可）")
    if not isinstance(c.get("sqlite_home_explicit", False), bool):
        raise VaultError("sqlite_home_explicitにはJSONのtrue/falseを指定してください")
    for key in ("repository", "work_dir", "codex_home"):
        if not isinstance(c.get(key), str) or not c[key].strip():
            raise VaultError(f"{key}には空でない文字列を指定してください")
    for key in ("work_dir", "codex_home"):
        no_link_ancestors(Path(os.path.expandvars(os.path.expanduser(c[key]))))
        c[key] = str(absolute(c[key]))
    c["config_file"] = str(absolute(path))
    return c


def config_sources(c):
    def source_path(value):
        no_link_ancestors(Path(os.path.expandvars(os.path.expanduser(str(value)))))
        return absolute(value)
    items = [("codex", "codex", source_path(c["codex_home"]))]
    if c.get("sqlite_home"):
        p = source_path(c["sqlite_home"])
        if not inside(p, items[0][2]):
            items.append(("sqlite", "sqlite", p))
    for n, p in enumerate(c.get("projects", []), 1):
        items.append((f"project-{n:03}", "project", source_path(p)))
    for n, p in enumerate(c.get("extra_sources", []), 1):
        items.append((f"extra-{n:03}", "extra", source_path(p)))
    # Nested roots are redundant and could be activated in the wrong order.
    for i, (_, _, p) in enumerate(items):
        no_link_ancestors(p)
        for _, _, q in items[:i]:
            if inside(p, q) or inside(q, p):
                raise VaultError(f"保存元の重複を解消してください: {p} / {q}")
    return [{"id": i, "kind": k, "original_path": str(p), "stored_path": f"sources/{i}"}
            for i, k, p in items]


def local_repository(value):
    # Windows drive paths and UNC paths are local filesystem backends.
    if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(("\\\\", "/", ".", "~")) or ":" not in value:
        return absolute(value)
    return None


def validate_paths(c, sources):
    work = absolute(c["work_dir"])
    no_link_ancestors(work)
    repo = local_repository(c["repository"])
    if repo:
        no_link_ancestors(repo)
        if inside(repo, work) or inside(work, repo):
            raise VaultError("保存先リポジトリと作業領域が重複しています")
    for s in sources:
        src = Path(s["original_path"])
        if not src.is_dir():
            raise VaultError(f"保存元フォルダがありません: {src}")
        for p in (work, repo, absolute(c["config_file"])):
            if p and (inside(p, src) or inside(src, p)):
                raise VaultError(f"バックアップ元と保存先／設定領域が重複しています: {src} / {p}")


def restic(c, args, cwd=None):
    env = os.environ.copy()
    # Do not let shell defaults override the configured repository/credentials.
    for key in ("RESTIC_REPOSITORY", "RESTIC_REPOSITORY_FILE", "RESTIC_PASSWORD_COMMAND"):
        env.pop(key, None)
    if c.get("password_file"):
        env.pop("RESTIC_PASSWORD", None)
        env["RESTIC_PASSWORD_FILE"] = str(absolute(c["password_file"]))
    elif c.get("credential_file"):
        env.pop("RESTIC_PASSWORD_FILE", None)
        env["RESTIC_PASSWORD"] = load_password(absolute(c["credential_file"]))
    elif not env.get("RESTIC_PASSWORD") and not env.get("RESTIC_PASSWORD_FILE"):
        raise VaultError("resticパスワードが未設定です。credential または RESTIC_PASSWORD_FILE を指定してください")
    elif env.get("RESTIC_PASSWORD_FILE"):
        env["RESTIC_PASSWORD_FILE"] = str(absolute(env["RESTIC_PASSWORD_FILE"]))
    exe = c.get("restic", "restic")
    cmd = [exe, "-r", c["repository"], "--cache-dir", str(absolute(c["work_dir"]) / "cache"), *args]
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=c.get("restic_timeout_seconds", 21600))
    except FileNotFoundError as e:
        raise VaultError("resticが見つかりません。READMEの導入手順でインストールしてください") from e
    except subprocess.TimeoutExpired as e:
        raise VaultError("restic処理がタイムアウトしました。保存先の接続とロックを確認してください") from e
    if r.returncode:
        # A partial snapshot (exit 3) is a failure, never a successful backup.
        raise VaultError(f"restic終了コード {r.returncode}: {r.stderr[-3000:]}")
    return r.stdout


def filter_args(c):
    # One comma-separated tag expression is AND. Separate --tag flags are OR.
    return ["--tag", f"codex-vault,vault-{c['vault_id']}"]


def snapshots(c):
    return json.loads(restic(c, ["snapshots", "--json", *filter_args(c)])) or []


def select_snapshot(c, wanted):
    items = snapshots(c)
    if wanted == "latest":
        matches = sorted(items, key=lambda s: s["time"])[-1:]
    elif re.fullmatch(r"[0-9a-f]{8,64}", wanted):
        matches = [s for s in items if s["id"].startswith(wanted)]
    else:
        raise VaultError("snapshotには latest または8桁以上の16進IDを指定してください")
    if len(matches) != 1:
        raise VaultError("このvaultのスナップショットを一意に選べません")
    return matches[0]


def tree_stamp(root, patterns):
    from .core import excluded, linked
    result = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        p = Path(folder)
        dirs[:] = sorted(d for d in dirs if not linked(p / d) and not excluded((p / d).relative_to(root), patterns))
        for name in sorted(files):
            f = p / name
            if not linked(f) and not excluded(f.relative_to(root), patterns):
                s = f.stat()
                result.append((str(f.relative_to(root)), s.st_size, s.st_mtime_ns))
    return result


def stage(c, mode, stopped=False):
    if mode == "cold" and not stopped:
        raise VaultError("coldには --stopped が必要です。Codex・CLI・IDE・編集処理を先に終了してください")
    sources = config_sources(c)
    warnings, skipped, git = [], [], []
    # Include external Git common dirs, so linked worktree .git pointers have a recovery source.
    for s in sources:
        if s["kind"] in ("project", "codex"):
            git.extend(discover_git(s["original_path"], c["project_excludes"]))
    for g in git:
        if g.get("warning"):
            warnings.append(g["warning"])
        if g.get("common_dir"):
            p = absolute(g["common_dir"])
            if not any(inside(p, s["original_path"]) for s in sources):
                sources.append({"id": f"git-{len(sources):03}", "kind": "git_metadata",
                                "original_path": str(p), "stored_path": f"sources/git-{len(sources):03}"})
            for line in (g.get("worktrees") or "").splitlines():
                if line.startswith("worktree "):
                    q = line[9:]
                    if not any(inside(q, s["original_path"]) for s in sources):
                        warnings.append(f"未登録のGit worktree作業ファイルがあります。projectsに追加してください: {q}")
    validate_paths(c, sources)
    work = absolute(c["work_dir"])
    payload = work / "payload"
    if payload.exists():
        no_link_ancestors(payload)
        shutil.rmtree(payload)
    payload.mkdir(parents=True)
    (payload / "sources").mkdir()
    from .archive import inspect_jsonl
    for s in sources:
        patterns = [] if s["kind"] in ("codex", "sqlite", "git_metadata") else list(c["project_excludes"])
        if s["kind"] == "codex":
            # Runtime provisioning guards are recreated by Codex, not recovery data.
            patterns += [".codex-provisioning-*.guard"]
        if s["kind"] == "codex" and not c.get("include_auth", False):
            patterns += ["auth.json"]
        before = tree_stamp(s["original_path"], patterns)
        copy_tree(s["original_path"], payload / s["stored_path"], patterns, warnings, skipped)
        after = tree_stamp(s["original_path"], patterns)
        if before != after:
            if mode == "cold":
                raise VaultError(f"cold取得中に保存元が更新されました: {s['original_path']}")
            warnings.append(f"稼働中に保存元の一覧／時刻が変化しました。複数ファイル間の同時点整合性は未保証: {s['original_path']}")
        s["excludes"] = patterns
        if s["kind"] == "codex":
            warnings.extend(inspect_jsonl(payload / s["stored_path"]))
    if mode == "hot":
        warnings.append("hot取得: DB単体はOnline Backup APIで取得。複数DB・会話・作業ファイルの同時点整合性は未保証")
    # A disaster recovery export carries the tool and operating instructions too.
    tool_root = Path(__file__).resolve().parent.parent
    recovery = payload / "recovery/tool"
    recovery.mkdir(parents=True)
    for name in ("codex_vault", "scripts", "docs", "tests"):
        if (tool_root / name).is_dir():
            copy_tree(tool_root / name, recovery / name,
                      ["__pycache__", "*.pyc"], warnings, skipped, sqlite=False)
    for name in ("README.md", "LICENSE", "pyproject.toml", "config.example.json"):
        if (tool_root / name).is_file():
            shutil.copy2(tool_root / name, recovery / name)
    m = {"format": "codex-vault/1", "tool_version": __version__, "created_at": now(),
         "vault_id": c["vault_id"], "host": socket.gethostname(), "mode": mode,
         "stopped_confirmed_by_operator": bool(stopped), "sources": sources,
         "warnings": warnings, "skipped": skipped, "git": git}
    m["files"], m["directories"] = inventory(payload)
    write_json(payload / "manifest.json", m)
    verify_payload(payload)
    return payload, m


def backup(c, mode, stopped=False):
    payload, m = stage(c, mode, stopped)
    output = restic(c, ["backup", "--json", "--host", socket.gethostname(),
                        "--tag", "codex-vault", "--tag", f"vault-{c['vault_id']}",
                        "--tag", mode, "--", "payload"], cwd=payload.parent)
    events = [json.loads(line) for line in output.splitlines() if line.strip()]
    summary = next((e for e in reversed(events) if e.get("message_type") == "summary"), None)
    if not summary or not summary.get("snapshot_id"):
        raise VaultError("resticからスナップショットIDを取得できませんでした")
    result = {"time": now(), "snapshot_id": summary["snapshot_id"], "mode": mode,
              "file_count": len(m["files"]), "warnings": m["warnings"],
              "last_success": True, "repository": c["repository"]}
    write_json(absolute(c["work_dir"]) / "last-success.json", result)
    # Delete plaintext staging after successful encrypted commit. Crash/failure retains it for diagnosis.
    shutil.rmtree(payload)
    return result


def restore(c, wanted, target):
    s = select_snapshot(c, wanted)
    target = Path(target).expanduser().absolute()
    no_link_ancestors(target)
    for p in [absolute(c["work_dir"]), *[Path(x["original_path"]) for x in config_sources(c)]]:
        if inside(target, p) or inside(p, target):
            raise VaultError("復元展開先が保存元／作業領域と重複しています")
    repo = local_repository(c["repository"])
    if repo and (inside(target, repo) or inside(repo, target)):
        raise VaultError("復元展開先とリポジトリが重複しています")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise VaultError("復元展開先には存在しないパスまたは空フォルダを指定してください")
    target.mkdir(parents=True, exist_ok=True)
    restic(c, ["restore", s["id"], "--target", str(target)])
    # Relative backup path `payload` produces target/payload regardless of original drive letter.
    payload = target / "payload"
    m = verify_payload(payload)
    if m["vault_id"] != c["vault_id"]:
        raise VaultError("復元したvault IDが一致しません")
    return {"snapshot_id": s["id"], "payload": str(payload), "verified": True,
            "mode": m["mode"], "warnings": m["warnings"], "sources": m["sources"]}


def retention(c, apply=False):
    # Separate hot/cold groups protect cold reference points from frequent hot runs.
    args = ["forget", *filter_args(c), "--group-by", "host,paths,tags",
            "--keep-within", "2d", "--keep-daily", "30", "--keep-weekly", "8", "--keep-monthly", "12"]
    if not apply:
        args += ["--dry-run"]
    out = restic(c, args)
    # No implicit prune: never run destructive retention as part of a scheduled backup.
    return out
