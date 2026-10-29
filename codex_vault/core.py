from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sqlite3
import stat
import subprocess
import time
from datetime import datetime, timezone


class VaultError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def absolute(value):
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def inside(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def linked(path):
    s = Path(path).lstat()
    return stat.S_ISLNK(s.st_mode) or bool(getattr(s, "st_file_attributes", 0) & 0x400)


def no_link_ancestors(path):
    p = Path(path).absolute()
    for part in (p, *p.parents):
        if (part.exists() or part.is_symlink()) and linked(part):
            raise VaultError(f"リンク／再解析ポイントを含むパスは使えません: {part}")


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@contextlib.contextmanager
def locked(work):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    lock = work / "vault.lock"
    # OS locks release automatically after a crash. The lock file may remain.
    with lock.open("a+b") as f:
        f.seek(0, 2)
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise VaultError("別のCodex Vault処理が実行中です") from e
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def sqlite_file(path):
    with Path(path).open("rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


def sqlite_check(path):
    with contextlib.closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        result = db.execute("PRAGMA quick_check").fetchall()
        if result != [("ok",)]:
            raise VaultError(f"SQLite検査失敗: {path}: {result[:3]}")


def sqlite_copy(source, target, timeout=120):
    deadline = time.monotonic() + timeout

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise VaultError(f"SQLiteバックアップがタイムアウトしました: {source}")

    with contextlib.closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as src:
        with contextlib.closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.05)
            dst.execute("PRAGMA journal_mode=DELETE")
    shutil.copystat(source, target)
    sqlite_check(target)


def stable_copy(source, target, retries=3):
    """Copy a file only if its identity, length and mtime remain stable."""
    for _ in range(retries):
        before = source.stat()
        shutil.copy2(source, target)
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ) and target.stat().st_size == after.st_size:
            return
    raise VaultError(f"書き込みが継続しているファイルです。アプリを停止して再実行してください: {source}")


def excluded(rel, patterns):
    # A pattern without '/' matches any component, otherwise root-relative path.
    return any((any(fnmatch.fnmatchcase(p, pattern) for p in rel.parts)
                if "/" not in pattern else fnmatch.fnmatchcase(rel.as_posix(), pattern))
               for pattern in patterns)


def copy_tree(source, target, patterns, warnings, skipped, sqlite=True):
    """Never follow links. Fail on unreadable regular files; log explicit omissions."""
    source, target = Path(source), Path(target)
    no_link_ancestors(source)
    if not source.is_dir():
        raise VaultError(f"必須の保存元フォルダがありません: {source}")
    target.mkdir(parents=True, exist_ok=False)
    def walk(folder):
        with os.scandir(folder) as scan:
            entries = sorted(scan, key=lambda e: e.name)
        dbs = set()
        if sqlite:
            for e in entries:
                p = Path(e.path)
                # Excluded files must not be opened even for format detection.
                # Some Codex coordination guards deliberately deny reads.
                if excluded(p.relative_to(source), patterns):
                    continue
                if not linked(p) and e.is_file(follow_symlinks=False) and sqlite_file(p):
                    dbs.add(e.name)
        for e in entries:
            src = Path(e.path)
            rel = src.relative_to(source)
            dst = target / rel
            if excluded(rel, patterns):
                skipped.append({"path": str(src), "reason": "configured_exclusion"})
                continue
            if linked(src):
                warnings.append(f"リンク／再解析ポイントを省略: {src}")
                skipped.append({"path": str(src), "reason": "link", "target": os.readlink(src) if src.is_symlink() else "reparse-point"})
                continue
            if sqlite and any(e.name == name + suffix for name in dbs for suffix in ("-wal", "-shm", "-journal")):
                skipped.append({"path": str(src), "reason": "sqlite_online_backup_sidecar"})
                continue
            if e.is_dir(follow_symlinks=False):
                dst.mkdir()
                walk(src)
                shutil.copystat(src, dst)
            elif e.is_file(follow_symlinks=False):
                if sqlite and e.name in dbs:
                    sqlite_copy(src, dst)
                else:
                    if sqlite and e.name.endswith(("-wal", "-shm", "-journal")):
                        warnings.append(f"対応DBを識別できないSQLite補助ファイルを原本保存: {src}")
                    stable_copy(src, dst)
            else:
                warnings.append(f"特殊ファイルを省略: {src}")
                skipped.append({"path": str(src), "reason": "special_file"})
    walk(source)
    shutil.copystat(source, target)


def safe_relative(value):
    p = PurePosixPath(value)
    w = PureWindowsPath(value)
    if (not value or p.is_absolute() or w.drive or w.is_absolute() or ".." in p.parts
            or "\\" in value or ":" in value or value != p.as_posix()):
        raise VaultError(f"不正なマニフェストパス: {value!r}")
    return p


def inventory(payload):
    files, dirs = [], []
    for root, ds, fs in os.walk(payload, followlinks=False):
        for name in ds:
            p = Path(root) / name
            if linked(p):
                raise VaultError(f"保存領域にリンクがあります: {p}")
            dirs.append(p.relative_to(payload).as_posix())
        for name in fs:
            p = Path(root) / name
            if linked(p):
                raise VaultError(f"保存領域にリンクがあります: {p}")
            rel = p.relative_to(payload).as_posix()
            if rel == "manifest.json":
                continue
            files.append({"path": rel, "size": p.stat().st_size, "sha256": sha256(p)})
    return sorted(files, key=lambda x: x["path"]), sorted(dirs)


def verify_payload(payload):
    payload = Path(payload).absolute()
    no_link_ancestors(payload)
    m = read_json(payload / "manifest.json")
    if m.get("format") != "codex-vault/1":
        raise VaultError("未対応のバックアップ形式です")
    expected = m["files"]
    names = [f["path"] for f in expected]
    if len(set(names)) != len(names):
        raise VaultError("マニフェストに重複パスがあります")
    for value in names + m["directories"]:
        safe_relative(value)
    for s in m["sources"]:
        safe_relative(s["stored_path"])
        if not s["stored_path"].startswith("sources/"):
            raise VaultError("不正な保存元マッピングです")
    actual, dirs = inventory(payload)
    if actual != expected or dirs != m["directories"]:
        raise VaultError("ハッシュ・サイズ・ファイル一覧またはディレクトリ一覧が一致しません")
    for f in expected:
        p = payload / f["path"]
        if sqlite_file(p):
            sqlite_check(p)
    return m


def git_info(root):
    def run(*args):
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30, env=env)
        return r.stdout.strip() if r.returncode == 0 else None
    try:
        # Older Git versions may echo an unknown rev-parse option to stdout
        # while still returning success. Avoid --path-format and resolve the
        # documented relative output against the directory passed to git -C.
        common = run("rev-parse", "--git-common-dir")
        if not common:
            return None
        if common.startswith("--") or "\n" in common or "\r" in common:
            raise VaultError("Gitの共通ディレクトリ取得結果が不正です。git --version とGit実行環境を確認してください")
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = Path(root).resolve() / common_path
        common_path = common_path.resolve()
        if not common_path.is_dir():
            raise VaultError(f"Gitの共通ディレクトリが存在しません: {common_path}")
        # No remote URL stored: URLs sometimes contain credentials.
        return {"root": str(root), "common_dir": str(common_path),
                "head": run("rev-parse", "HEAD"), "status": run("status", "--porcelain=v1"),
                "worktrees": run("worktree", "list", "--porcelain")}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"root": str(root), "warning": "gitが利用できないかタイムアウトしました"}


def discover_git(root, excludes):
    results = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        here = Path(folder)
        dirs[:] = [d for d in dirs if d != ".git" and not excluded((here / d).relative_to(root), excludes)
                   and not linked(here / d)]
        if ".git" in files or (here / ".git").is_dir():
            info = git_info(here)
            if info:
                results.append(info)
    return results


def activate(payload, source_id, target, stopped=False, apply=False):
    """Install exactly one source; retain the previous directory for rollback."""
    if not stopped or not apply:
        raise VaultError("切替には --stopped と --apply が必要です。Codex／IDE等を先に終了してください")
    m = verify_payload(payload)
    matches = [s for s in m["sources"] if s["id"] == source_id]
    if len(matches) != 1:
        raise VaultError("保存元IDが見つかりません")
    src = Path(payload).absolute() / matches[0]["stored_path"]
    target = Path(target).expanduser().absolute()
    no_link_ancestors(target)
    if target == Path(target.anchor) or len(target.parts) < 3:
        raise VaultError("ドライブ直下等への復元は拒否しました")
    if inside(target, payload) or inside(payload, target):
        raise VaultError("展開先と復元先が重複しています")
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns()}"
    temp = target.with_name(target.name + ".vault-new-" + suffix)
    previous = target.with_name(target.name + ".before-vault-" + suffix)
    if target.exists() and not target.is_dir():
        raise VaultError("復元先がフォルダではありません")
    shutil.copytree(src, temp)
    # Verify copied bytes before renaming any existing directory.
    for f in m["files"]:
        prefix = matches[0]["stored_path"] + "/"
        if f["path"].startswith(prefix):
            p = temp / f["path"][len(prefix):]
            if sha256(p) != f["sha256"]:
                raise VaultError(f"切替前のコピー検査に失敗: {p}")
    existed = target.exists()
    if existed:
        target.rename(previous)
    try:
        temp.rename(target)
    except BaseException:
        if existed:
            previous.rename(target)
        raise
    return {"target": str(target), "previous": str(previous) if existed else None,
            "note": "退避先は自動削除しません。必要な全保存元を戻してからCodexを起動してください"}
