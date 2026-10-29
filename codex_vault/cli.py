from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import uuid

from . import __version__
from .archive import export_archive, records, search_archive
from .core import (VaultError, absolute, activate, inside, locked, no_link_ancestors,
                   now, read_json, verify_payload, write_json)
from .engine import (DEFAULT_EXCLUDES, backup, config_sources, load_config, restic,
                     restore, retention, snapshots, validate_paths)
from .secure import private_dir, save_password


def default_config():
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return root / "CodexVault" / "vault.json"


def detect_sqlite(home):
    config = home / "config.toml"
    if config.exists():
        parsed = tomllib.loads(config.read_text(encoding="utf-8-sig"))
        if parsed.get("sqlite_home"):
            p = Path(os.path.expanduser(str(parsed["sqlite_home"])))
            if not p.is_absolute():
                raise VaultError("config.tomlのsqlite_homeが相対パスです。configure --sqlite-home に絶対パスを指定してください")
            return str(absolute(p))
    env = os.environ.get("CODEX_SQLITE_HOME")
    if env:
        if not Path(env).is_absolute():
            raise VaultError("CODEX_SQLITE_HOMEが相対パスです。--sqlite-home に絶対パスを指定してください")
        return str(absolute(env))
    return str(home)


def configure(args):
    path = Path(args.config).absolute()
    no_link_ancestors(path)
    if path.exists():
        raise VaultError("設定ファイルが既にあります。既存設定を編集するか別の --config を指定してください")
    home_input = args.codex_home or os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    no_link_ancestors(Path(os.path.expandvars(os.path.expanduser(home_input))))
    home = absolute(home_input)
    repository = args.repository or input("バックアップ先（例 E:\\CodexVault\\restic）: ").strip()
    if not repository:
        raise VaultError("保存先を入力してください")
    from .engine import local_repository
    local = local_repository(repository)
    if local:
        repository = str(local)
    project_inputs = args.project
    if not project_inputs and not args.history_only:
        answer = input("追加する作業フォルダ（複数は ; 区切り、空欄で履歴のみ）: ").strip()
        project_inputs = [p.strip().strip('"') for p in answer.split(";") if p.strip()]
    for p in project_inputs:
        no_link_ancestors(Path(os.path.expandvars(os.path.expanduser(p))))
    projects = [str(absolute(p)) for p in project_inputs]
    c = {"format": "codex-vault-config/1", "vault_id": uuid.uuid4().hex,
         "repository": repository, "restic": str(absolute(args.restic)) if Path(args.restic).is_file() else args.restic,
         "codex_home": str(home), "sqlite_home": str(absolute(args.sqlite_home)) if args.sqlite_home else detect_sqlite(home),
         "sqlite_home_explicit": bool(args.sqlite_home),
         "projects": projects, "extra_sources": [], "include_auth": False,
         "project_excludes": DEFAULT_EXCLUDES, "work_dir": str(path.parent / "work"),
         "credential_file": str(path.parent / "repository.dpapi") if os.name == "nt" else None,
         "password_file": None, "restic_timeout_seconds": 21600}
    # Validate before persisting configuration. No folder is moved.
    c["config_file"] = str(path)
    validate_paths(c, config_sources(c))
    c.pop("config_file")
    private_dir(path.parent)
    write_json(path, c)
    return {"config": str(path), "sources": config_sources(c), "next": "credential → init → doctor → backup"}


def credential(c):
    if os.name != "nt":
        raise VaultError("Windows以外ではRESTIC_PASSWORD_FILEを設定してください（README参照）")
    p1 = getpass.getpass("resticパスワード（別途パスワード管理ソフトにも保管）: ")
    p2 = getpass.getpass("もう一度: ")
    if p1 != p2 or len(p1) < 12 or "\n" in p1 or "\r" in p1:
        raise VaultError("パスワード不一致、12文字未満、または改行を含みます")
    if not c.get("credential_file"):
        raise VaultError("設定のcredential_fileにDPAPIファイルの絶対パスを指定してください")
    save_password(absolute(c["credential_file"]), p1)
    return {"saved": True, "note": "DPAPIはこのWindowsユーザー用です。PC故障時は別途保管した元パスワードが必要です"}


def scan(c):
    home = Path(c["codex_home"])
    found, malformed = {}, 0
    for name in ("sessions", "archived_sessions"):
        root = home / name
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            for _, item, error in records(path):
                if error:
                    malformed += 1
                    continue
                if item.get("type") == "session_meta":
                    p = item.get("payload", {})
                    cwd = p.get("cwd") if isinstance(p, dict) else None
                    if cwd:
                        found[str(cwd)] = found.get(str(cwd), 0) + 1
                    break
    roots = [s["original_path"] for s in config_sources(c)]
    return {"cwd_candidates": [{"path": p, "session_count": n, "exists_on_this_os": Path(p).is_dir(),
                                "covered": any(inside(p, r) for r in roots)} for p, n in sorted(found.items())],
            "malformed_lines": malformed,
            "note": "候補は自動登録しません。WindowsとWSLは別々に設定してください"}


def doctor(c):
    sources = config_sources(c)
    validate_paths(c, sources)
    version = subprocess.run([c.get("restic", "restic"), "version"], capture_output=True, text=True, check=True).stdout.strip()
    r = {"restic": version, "python": sys.version.split()[0], "sources": sources,
         "staging_free_bytes": shutil.disk_usage(Path(c["work_dir"]).parent).free,
         "notes": ["project_excludesを確認してください: " + ", ".join(c["project_excludes"]),
                   "アプリ固有のCODEX_HOME外データ、添付、WSL、他ユーザー、他アカウントのデータは別途登録が必要です"]}
    try:
        detected = detect_sqlite(Path(c["codex_home"]))
    except VaultError:
        if not c.get("sqlite_home_explicit", False):
            raise
        detected = None
        r["notes"].append("SQLite保存先は明示登録値を使用しています。相対パス等のため自動検出はできません")
    if detected and absolute(detected) != absolute(c.get("sqlite_home") or c["codex_home"]):
        if not c.get("sqlite_home_explicit", False):
            raise VaultError("現在検出したSQLite保存先と登録値が異なります。設定を確認してください: " + detected)
        r["notes"].append("SQLite保存先は明示登録値を使用しています。自動検出値との差を確認してください: " + detected)
    restic(c, ["cat", "config"])
    r["repository_access"] = True
    r["discovery"] = scan(c)
    return r


def parser():
    p = argparse.ArgumentParser(description="Codex Vault: restic暗号化バックアップと検証復旧")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", default=str(default_config()), help="設定JSON（サブコマンドより前に指定）")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("configure", help="初期設定を作成")
    q.add_argument("--repository")
    q.add_argument("--project", action="append", default=[])
    q.add_argument("--history-only", action="store_true", help="プロジェクトの対話入力を省略")
    q.add_argument("--codex-home")
    q.add_argument("--sqlite-home")
    q.add_argument("--restic", default="restic")
    for name in ("credential", "init", "doctor", "scan", "snapshots", "status", "clean-stage", "prune"):
        sub.add_parser(name)
    q = sub.add_parser("backup")
    q.add_argument("--mode", choices=("hot", "cold"), default="hot")
    q.add_argument("--stopped", action="store_true")
    q = sub.add_parser("restore")
    q.add_argument("--snapshot", default="latest")
    q.add_argument("--target", required=True)
    q = sub.add_parser("verify")
    q.add_argument("--payload", required=True)
    q = sub.add_parser("activate")
    q.add_argument("--payload", required=True)
    q.add_argument("--source", required=True)
    q.add_argument("--target", required=True)
    q.add_argument("--stopped", action="store_true")
    q.add_argument("--apply", action="store_true")
    q = sub.add_parser("archive")
    q.add_argument("--payload", required=True)
    q.add_argument("--output", required=True)
    q = sub.add_parser("search")
    q.add_argument("--archive", required=True)
    q.add_argument("--query", required=True)
    q = sub.add_parser("check")
    q.add_argument("--read-data", action="store_true")
    q = sub.add_parser("retention")
    q.add_argument("--apply", action="store_true")
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.umask(0o077)
    args = parser().parse_args(argv)
    c = None
    try:
        if args.command == "configure":
            result = configure(args)
        elif args.command == "verify":
            m = verify_payload(args.payload)
            result = {"verified": True, "files": len(m["files"]), "sources": m["sources"], "warnings": m["warnings"]}
        elif args.command == "archive":
            if inside(args.output, args.payload) or inside(args.payload, args.output):
                raise VaultError("アーカイブ出力先とpayloadを分離してください")
            result = export_archive(args.payload, args.output)
        elif args.command == "search":
            result = search_archive(args.archive, args.query)
        else:
            c = load_config(args.config)
            private_dir(c["work_dir"])
            with locked(c["work_dir"]):
                if args.command == "credential":
                    result = credential(c)
                elif args.command == "init":
                    validate_paths(c, config_sources(c))
                    result = restic(c, ["init"])
                elif args.command == "doctor":
                    result = doctor(c)
                elif args.command == "scan":
                    result = scan(c)
                elif args.command == "snapshots":
                    result = snapshots(c)
                elif args.command == "backup":
                    result = backup(c, args.mode, args.stopped)
                elif args.command == "restore":
                    result = restore(c, args.snapshot, args.target)
                elif args.command == "activate":
                    # Never activate over our own tool/runtime/repository.
                    from .engine import local_repository
                    protected = [Path(__file__).resolve().parent.parent, Path(c["config_file"]).parent,
                                 Path(c["work_dir"]), local_repository(c["repository"])]
                    if any(p and (inside(args.target, p) or inside(p, args.target)) for p in protected):
                        raise VaultError("ツール／保存領域を復元先には指定できません")
                    result = activate(args.payload, args.source, args.target, args.stopped, args.apply)
                elif args.command == "check":
                    result = restic(c, ["check", *(["--read-data"] if args.read_data else [])])
                elif args.command == "retention":
                    result = retention(c, args.apply)
                elif args.command == "prune":
                    result = restic(c, ["prune"])
                    restic(c, ["check"])
                elif args.command == "clean-stage":
                    p = Path(c["work_dir"]) / "payload"
                    no_link_ancestors(p)
                    if p.exists():
                        shutil.rmtree(p)
                    result = {"removed": str(p), "note": "削除は通常削除です。SSD上の安全消去を保証しません"}
                else:
                    work = Path(c["work_dir"])
                    result = {"last_success": read_json(work / "last-success.json") if (work / "last-success.json").exists() else None,
                              "last_failure": read_json(work / "last-failure.json") if (work / "last-failure.json").exists() else None,
                              "stage_exists": (work / "payload").exists()}
                    if result["last_success"]:
                        dt = datetime.fromisoformat(result["last_success"]["time"])
                        result["hours_since_success"] = round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 2)
        print(json.dumps(result, ensure_ascii=False, indent=2) if not isinstance(result, str) else result)
        # Successful backup with explicit omissions is actionable (task scheduler sees 2).
        return 2 if args.command == "backup" and result.get("warnings") and (
            len(result["warnings"]) > (1 if args.mode == "hot" else 0)) else 0
    except (VaultError, OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as e:
        message = f"{type(e).__name__}: {e}"
        if c and args.command == "backup":
            try:
                write_json(Path(c["work_dir"]) / "last-failure.json", {"time": now(), "error": message})
            except OSError:
                pass
        print(message, file=sys.stderr)
        return 1
