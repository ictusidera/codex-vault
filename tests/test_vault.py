import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_vault.archive import export_archive, search_archive
from codex_vault.core import VaultError, activate, locked, read_json, remove_staging, verify_payload, write_json
from codex_vault.engine import (backup, change_details, restic, restore, retention,
                               snapshots, source_changes, sqlite_runtime_paths, stage)
from codex_vault.cli import detect_sqlite, doctor, main


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vault-test-")
        self.root = Path(self.tmp.name)
        self.codex = self.root / "codex"
        self.codex.mkdir()
        self.project = self.root / "project 日本語"
        self.project.mkdir()
        (self.codex / "sessions").mkdir()
        events = [
            {"type": "session_meta", "payload": {"id": "session-1", "cwd": str(self.project), "timestamp": "2026-10-28T00:00:00Z"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "重複は除外"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "バックアップの設計"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "復旧手順を記録"}]}},
        ]
        self.raw = self.codex / "sessions" / "rollout-one.jsonl"
        self.raw.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
        (self.codex / "auth.json").write_text('{"token":"synthetic-test-secret"}')
        (self.project / ".env").write_text("SYNTHETIC_IGNORED_BUT_IMPORTANT=yes")
        (self.project / "untracked.txt").write_text("important")
        (self.project / "node_modules").mkdir()
        (self.project / "node_modules" / "cache").write_text("rebuildable")
        self.dbroot = self.root / "external-sqlite"
        self.dbroot.mkdir()
        self.writer = sqlite3.connect(self.dbroot / "state_99.sqlite")
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.writer.execute("CREATE TABLE data (value TEXT)")
        self.writer.execute("INSERT INTO data VALUES ('committed-in-wal')")
        self.writer.commit()
        self.c = {"format": "codex-vault-config/1", "vault_id": "a" * 32,
                  "codex_home": str(self.codex), "sqlite_home": str(self.dbroot),
                  "projects": [str(self.project)], "extra_sources": [], "include_auth": False,
                  "project_excludes": ["node_modules"], "work_dir": str(self.root / "private" / "work"),
                  "repository": str(self.root / "repo"), "restic": os.environ.get("VAULT_TEST_RESTIC", "restic"),
                  "config_file": str(self.root / "private" / "vault.json")}
        self.env = patch.dict(os.environ, {"RESTIC_PASSWORD": "synthetic-integration-password-only"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.writer.close()
        self.tmp.cleanup()


class CoreTests(Fixture):
    def test_second_stage_cleans_copied_readonly_git_object_through_real_rmtree(self):
        source = self.codex / ".tmp/plugins/.git/objects/ab/synthetic-object"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"synthetic object")
        source.chmod(stat.S_IREAD)
        original_unlink = os.unlink
        denied = []
        def windows_unlink(path, *, dir_fd=None):
            try:
                mode = os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode
                if not mode & stat.S_IWRITE:
                    denied.append(str(path))
                    raise PermissionError(13, "simulated Windows read-only deletion", str(path))
            except FileNotFoundError:
                pass
            return original_unlink(path, dir_fd=dir_fd)
        try:
            first, _ = stage(self.c, "hot")
            copied = first / "sources/codex/.tmp/plugins/.git/objects/ab/synthetic-object"
            self.assertFalse(copied.stat().st_mode & stat.S_IWRITE)
            with patch("os.unlink", windows_unlink):
                second, _ = stage(self.c, "hot")
            self.assertTrue(denied)
            self.assertEqual((second / "sources/codex/.tmp/plugins/.git/objects/ab/synthetic-object").read_bytes(),
                             b"synthetic object")
            self.assertFalse(source.stat().st_mode & stat.S_IWRITE)
            self.assertEqual(source.read_bytes(), b"synthetic object")
        finally:
            source.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_generated_arg0_directories_are_not_scanned_or_read(self):
        generated = []
        for name in ("arg0", "arg0-windows", "arg0-linux", "arg0-macos"):
            folder = self.codex / "tmp" / name
            runtime = folder / "codex-arg0Synthetic"
            runtime.mkdir(parents=True)
            (runtime / ".lock").write_bytes(b"\0")
            (runtime / "apply_patch.bat").write_text("synthetic runtime wrapper")
            generated.append(folder)
        saved = [self.codex / "tmp/user-notes.txt", self.codex / ".tmp/plugins/local-source.txt",
                 self.project / "tmp/arg0-windows/important.txt"]
        for path in saved:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep this data")
        original_scandir = os.scandir
        original_open = Path.open
        def guarded_scandir(path):
            if not isinstance(path, int) and any(Path(path).is_relative_to(g) for g in generated):
                raise PermissionError("generated helper directory must not be scanned")
            return original_scandir(path)
        def guarded_open(path, *args, **kwargs):
            if any(path.is_relative_to(g) for g in generated):
                raise PermissionError("generated runtime is locked")
            return original_open(path, *args, **kwargs)
        with patch("os.scandir", guarded_scandir), patch.object(Path, "open", guarded_open):
            payload, manifest = stage(self.c, "hot")
        for folder in generated:
            self.assertFalse((payload / "sources/codex" / folder.relative_to(self.codex)).exists())
            self.assertTrue(any(s["path"] == str(folder) for s in manifest["skipped"]))
        for path in saved:
            source = self.project if path.is_relative_to(self.project) else self.codex
            prefix = "project-001" if source == self.project else "codex"
            self.assertEqual((payload / "sources" / prefix / path.relative_to(source)).read_text(),
                             "keep this data")

    def test_staging_cleanup_retries_readonly_copy_without_changing_source(self):
        work = self.root / "cleanup-work"
        payload = work / "payload"
        copied = payload / "readonly-git-object"
        payload.mkdir(parents=True)
        source = self.project / "readonly-source"
        source.write_text("keep source")
        source.chmod(stat.S_IREAD)
        shutil.copy2(source, copied)
        (work / "last-success.json").write_text("keep status")
        error = PermissionError(13, "read-only staging copy")
        def simulate_windows(path, *, onerror):
            self.assertEqual(Path(path), payload)
            onerror(os.unlink, str(copied), (PermissionError, error, None))
            payload.rmdir()
        try:
            with patch("codex_vault.core.shutil.rmtree", simulate_windows):
                remove_staging(work)
            self.assertFalse(payload.exists())
            self.assertEqual(source.read_text(), "keep source")
            self.assertFalse(source.stat().st_mode & stat.S_IWRITE)
            self.assertEqual((work / "last-success.json").read_text(), "keep status")
        finally:
            source.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_cleanup_preserves_acl_or_sharing_failure_for_writable_file(self):
        work = self.root / "cleanup-work"
        payload = work / "payload"
        payload.mkdir(parents=True)
        file = payload / "locked-file"
        file.write_text("still in use")
        error = PermissionError(13, "sharing violation")
        def simulate_windows(path, *, onerror):
            onerror(os.unlink, str(file), (PermissionError, error, None))
        with patch("codex_vault.core.shutil.rmtree", simulate_windows), patch("os.chmod") as chmod:
            with self.assertRaises(PermissionError):
                remove_staging(work)
            chmod.assert_not_called()
        self.assertEqual(file.read_text(), "still in use")

    def test_cleanup_never_chmods_or_deletes_outside_payload(self):
        work = self.root / "cleanup-work"
        (work / "payload").mkdir(parents=True)
        source = self.project / "untracked.txt"
        error = PermissionError(13, "synthetic failure")
        def bad_path(path, *, onerror):
            onerror(os.unlink, str(source), (PermissionError, error, None))
        with patch("codex_vault.core.shutil.rmtree", bad_path), patch("os.chmod") as chmod:
            with self.assertRaises(PermissionError):
                remove_staging(work)
            chmod.assert_not_called()
        self.assertEqual(source.read_text(), "important")

    @unittest.skipIf(os.name == "nt", "symlink creation may require privileges")
    def test_cleanup_refuses_payload_alias(self):
        work = self.root / "cleanup-work"
        work.mkdir()
        (work / "payload").symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(VaultError):
            remove_staging(work)
        self.assertTrue((self.project / "untracked.txt").exists())

    def test_post_commit_cleanup_failure_is_reported_as_saved_with_warning(self):
        from codex_vault.core import remove_staging as original_remove
        count = 0
        def fail_after_commit(work):
            nonlocal count
            count += 1
            if count > 1:
                raise PermissionError(13, "staging still locked")
            original_remove(work)
        output = json.dumps({"message_type": "summary", "snapshot_id": "1" * 64})
        with patch("codex_vault.engine.restic", return_value=output), patch("codex_vault.engine.remove_staging", fail_after_commit):
            result = backup(self.c, "hot")
        self.assertTrue(result["last_success"])
        self.assertFalse(result["stage_cleanup_complete"])
        self.assertEqual(result["snapshot_id"], "1" * 64)
        self.assertGreater(len(result["warnings"]), 1)
        self.assertFalse(read_json(Path(self.c["work_dir"]) / "last-success.json")["stage_cleanup_complete"])

    @unittest.skipUnless(os.name == "nt", "requires Windows read-only file semantics")
    def test_windows_readonly_git_copy_is_removed_on_retry(self):
        source = self.project / "readonly-git-object"
        source.write_bytes(b"synthetic git object")
        work = self.root / "cleanup-work"
        payload = work / "payload"
        payload.mkdir(parents=True)
        source.chmod(stat.S_IREAD)
        try:
            shutil.copy2(source, payload / "copied-object")
            remove_staging(work)
            self.assertFalse(payload.exists())
            self.assertEqual(source.read_bytes(), b"synthetic git object")
            self.assertFalse(source.stat().st_mode & stat.S_IWRITE)
        finally:
            source.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_sqlite_sidecars_are_not_opened_by_raw_format_detection(self):
        sidecars = {self.dbroot / "state_99.sqlite-wal", self.dbroot / "state_99.sqlite-shm"}
        original_open = Path.open
        def reject_raw_sidecar(p, *args, **kwargs):
            if p in sidecars:
                raise PermissionError("SQLite API must handle this sidecar")
            return original_open(p, *args, **kwargs)
        with patch.object(Path, "open", reject_raw_sidecar):
            payload, manifest = stage(self.c, "hot")
        with sqlite3.connect(payload / "sources/sqlite/state_99.sqlite") as db:
            self.assertEqual(db.execute("SELECT value FROM data").fetchone()[0], "committed-in-wal")
        self.assertTrue(any(s["reason"] == "sqlite_online_backup_sidecar" for s in manifest["skipped"]))

    def test_known_runtime_locks_are_not_read_and_state_files_survive(self):
        # Mimic the user's error: open succeeds, but read(16) is denied.
        relative_locks = [".sqlite-maintenance.lock", "log/.sqlite-maintenance.lock",
                          "thread-writer-locks/.coordination.lock",
                          "thread-writer-locks/synthetic-thread.lock",
                          "mcp-oauth-locks/secrets-store.lock",
                          "mcp-oauth-locks/file-store.lock", "mcp-oauth-locks/synthetic-hash.lock"]
        locks = [self.codex / p for p in relative_locks] + [self.dbroot / ".sqlite-maintenance.lock"]
        for p in locks:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\0")
        persistent = [self.codex / "mcp-oauth-locks/synthetic.enterprise-generation",
                      self.codex / "mcp-oauth-locks/synthetic.enterprise-credential-version",
                      self.codex / "dependencies.lock", self.project / ".sqlite-maintenance.lock",
                      self.codex / "worktrees/project/thread-writer-locks/project.lock"]
        for p in persistent:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("persistent-test-data")
        original_open = Path.open
        denied = unittest.mock.MagicMock()
        denied.__enter__.return_value.read.side_effect = PermissionError(13, "Permission denied")
        def open_with_locked_reads(path, *args, **kwargs):
            if path in locks:
                return denied
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", open_with_locked_reads):
            payload, manifest = stage(self.c, "hot")
        denied.__enter__.assert_not_called()
        skipped_paths = {s["path"] for s in manifest["skipped"]}
        for p in locks:
            self.assertIn(str(p), skipped_paths)
        for p in persistent:
            source = self.project if p.is_relative_to(self.project) else self.codex
            prefix = "project-001" if source == self.project else "codex"
            self.assertEqual((payload / "sources" / prefix / p.relative_to(source)).read_text(),
                             "persistent-test-data")
        with sqlite3.connect(payload / "sources/sqlite/state_99.sqlite") as db:
            self.assertEqual(db.execute("SELECT value FROM data").fetchone()[0], "committed-in-wal")
        self.assertTrue((payload / "sources/codex/sessions/rollout-one.jsonl").is_file())
        verify_payload(payload)

    def test_exclusion_anchors_do_not_match_other_project_paths(self):
        from codex_vault.core import excluded
        rule = ["/mcp-oauth-locks/*.lock"]
        self.assertTrue(excluded(Path("mcp-oauth-locks/secrets-store.lock"), rule))
        self.assertFalse(excluded(Path("worktrees/project/mcp-oauth-locks/secrets-store.lock"), rule))
        self.assertFalse(excluded(Path("mcp-oauth-locks/file.enterprise-generation"), rule))

    def test_unrecognized_locked_file_read_still_fails(self):
        path = self.codex / "important-project.lock"
        path.write_bytes(b"important")
        original_open = Path.open
        denied = unittest.mock.MagicMock()
        denied.__enter__.return_value.read.side_effect = PermissionError(13, "Permission denied")
        def open_with_locked_read(p, *args, **kwargs):
            return denied if p == path else original_open(p, *args, **kwargs)
        with patch.object(Path, "open", open_with_locked_read), self.assertRaises(PermissionError):
            stage(self.c, "hot")

    @unittest.skipUnless(os.name == "nt", "requires Windows byte-range locking")
    def test_windows_maintenance_byte_range_lock_allows_hot_stage(self):
        import msvcrt
        lock = self.codex / ".sqlite-maintenance.lock"
        with lock.open("w+b") as holder:
            holder.write(b"\0")
            holder.flush()
            holder.seek(0)
            msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                with self.assertRaises(PermissionError):
                    with lock.open("rb") as contender:
                        contender.read(16)
                payload, _ = stage(self.c, "hot")
                self.assertFalse((payload / "sources/codex/.sqlite-maintenance.lock").exists())
            finally:
                holder.seek(0)
                msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)

    def test_debug_reports_stack_and_source_for_pathless_permission_error(self):
        import io
        write_json(self.c["config_file"], self.c)
        error = PermissionError(13, "Permission denied")
        stderr = io.StringIO()
        with patch("codex_vault.engine.copy_tree", side_effect=error), patch("sys.stderr", stderr):
            code = main(["--config", self.c["config_file"], "--debug", "backup", "--mode", "hot"])
        output = stderr.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("Traceback", output)
        self.assertIn("source copy", output)
        self.assertIn(str(self.codex), output)
        self.assertNotIn("synthetic-integration-password-only", output)

    def test_locked_provisioning_guard_is_not_opened_and_is_recorded(self):
        guard = self.codex / ".codex-provisioning-0000000000000001.guard"
        guard.write_text("runtime guard")
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path == guard:
                raise PermissionError("simulated Windows guard read denial")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded_open):
            payload, manifest = stage(self.c, "hot")
        self.assertFalse((payload / "sources/codex" / guard.name).exists())
        self.assertTrue(any(s["path"] == str(guard) and s["reason"] == "configured_exclusion"
                            for s in manifest["skipped"]))
        self.assertTrue((payload / "sources/codex/sessions/rollout-one.jsonl").is_file())
        verify_payload(payload)

    def test_unreadable_history_is_still_fatal(self):
        original_open = Path.open
        def denied_open(path, *args, **kwargs):
            if path == self.raw:
                raise PermissionError("unreadable real history")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", denied_open), self.assertRaises(PermissionError):
            stage(self.c, "hot")

    def test_excluded_auth_is_not_opened_for_sqlite_detection(self):
        auth = self.codex / "auth.json"
        original_open = Path.open
        def denied_open(path, *args, **kwargs):
            if path == auth:
                raise PermissionError("excluded auth must not be inspected")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", denied_open):
            payload, _ = stage(self.c, "hot")
        self.assertFalse((payload / "sources/codex/auth.json").exists())

    def test_git_relative_common_dir_resolves_against_repository_not_process_cwd(self):
        from codex_vault.core import git_info
        (self.project / ".git").mkdir()
        result = subprocess.CompletedProcess([], 0, ".git\n", "")
        with patch("codex_vault.core.subprocess.run", return_value=result) as run:
            info = git_info(self.project)
        self.assertEqual(info["common_dir"], str((self.project / ".git").resolve()))
        self.assertEqual(run.call_args_list[0].args[0][-2:], ["rev-parse", "--git-common-dir"])

    def test_git_absolute_common_dir_is_preserved(self):
        from codex_vault.core import git_info
        common = self.root / "external-git"
        common.mkdir()
        result = subprocess.CompletedProcess([], 0, str(common) + "\n", "")
        with patch("codex_vault.core.subprocess.run", return_value=result):
            self.assertEqual(git_info(self.project)["common_dir"], str(common.resolve()))

    def test_git_echoed_option_is_rejected_even_with_success_exit_code(self):
        from codex_vault.core import git_info
        result = subprocess.CompletedProcess([], 0, "--path-format=absolute\n.git\n", "")
        with patch("codex_vault.core.subprocess.run", return_value=result), self.assertRaises(VaultError):
            git_info(self.project)

    @unittest.skipUnless(os.name == "nt", "DPAPI requires a real Windows user profile")
    def test_windows_dpapi_roundtrip(self):
        from codex_vault.secure import save_password, load_password
        path = self.root / "secrets" / "password.dpapi"
        save_password(path, "synthetic-dpapi-password")
        self.assertEqual(load_password(path), "synthetic-dpapi-password")
        self.assertNotIn(b"synthetic-dpapi-password", path.read_bytes())

    def test_live_wal_and_git_ignored_files_are_protected(self):
        p, m = stage(self.c, "hot")
        with sqlite3.connect(p / "sources/sqlite/state_99.sqlite") as db:
            self.assertEqual(db.execute("SELECT value FROM data").fetchone()[0], "committed-in-wal")
        self.assertFalse((p / "sources/sqlite/state_99.sqlite-wal").exists())
        self.assertFalse((p / "sources/codex/auth.json").exists())
        self.assertEqual((p / "sources/project-001/.env").read_text(), "SYNTHETIC_IGNORED_BUT_IMPORTANT=yes")
        self.assertFalse((p / "sources/project-001/node_modules").exists())
        self.assertEqual(verify_payload(p)["format"], "codex-vault/1")

    def test_tampering_is_rejected(self):
        p, _ = stage(self.c, "hot")
        (p / "sources/project-001/untracked.txt").write_text("changed")
        with self.assertRaises(VaultError):
            verify_payload(p)

    def test_missing_files_empty_directories_and_extra_files_rejected(self):
        (self.project / "empty").mkdir()
        p, _ = stage(self.c, "hot")
        (p / "sources/project-001/empty").rmdir()
        with self.assertRaises(VaultError):
            verify_payload(p)
        (p / "sources/project-001/empty").mkdir()
        (p / "unexpected").touch()
        with self.assertRaises(VaultError):
            verify_payload(p)

    def test_traversal_and_link_in_manifest_rejected(self):
        p, m = stage(self.c, "hot")
        m["files"][0]["path"] = "../outside"
        write_json(p / "manifest.json", m)
        with self.assertRaises(VaultError):
            verify_payload(p)

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privilege")
    def test_links_are_omitted_with_warnings(self):
        (self.project / "outside-link").symlink_to(self.dbroot, target_is_directory=True)
        p, m = stage(self.c, "hot")
        self.assertFalse((p / "sources/project-001/outside-link").exists())
        self.assertTrue(any(s["reason"] == "link" for s in m["skipped"]))

    def test_recursive_backup_refused(self):
        self.c["repository"] = str(self.project / "repo")
        with self.assertRaises(VaultError):
            stage(self.c, "hot")

    def test_missing_source_is_fatal(self):
        self.c["projects"].append(str(self.root / "absent"))
        with self.assertRaises(VaultError):
            stage(self.c, "hot")

    def test_cold_requires_explicit_stopped_confirmation(self):
        with self.assertRaises(VaultError):
            stage(self.c, "cold")

    def test_cold_accepts_reader_created_sidecars_of_cleanly_closed_wal_db(self):
        db = self.codex / "stopped.sqlite"
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE data(value)")
            conn.execute("INSERT INTO data VALUES ('saved-before-stop')")
            conn.commit()
        self.assertFalse(Path(str(db) + "-wal").exists())
        original = db.read_bytes()
        payload, manifest = stage(self.c, "cold", True)
        self.assertEqual(db.read_bytes(), original)
        with contextlib.closing(sqlite3.connect(payload / "sources/codex/stopped.sqlite")) as conn:
            self.assertEqual(conn.execute("SELECT value FROM data").fetchall(), [("saved-before-stop",)])
        source = next(s for s in manifest["sources"] if s["kind"] == "codex")
        self.assertIn("stopped.sqlite-shm", source["consistency_ignored"])
        self.assertFalse((payload / "sources/codex/stopped.sqlite-wal").exists())
        verify_payload(payload)

    def test_cold_preserves_stopped_db_committed_wal_and_handles_reader_shm(self):
        db = self.codex / "stopped-with-wal.sqlite"
        code = """import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('CREATE TABLE data(value)')
c.execute("INSERT INTO data VALUES ('committed-only-in-wal')")
c.commit()
os._exit(0)
"""
        subprocess.run([sys.executable, "-c", code, str(db)], check=True)
        # No SQLite process remains. Simulate a missing reconstructible index.
        Path(str(db) + "-shm").unlink()
        wal = Path(str(db) + "-wal")
        original_db, original_wal = db.read_bytes(), wal.read_bytes()
        self.assertGreater(len(original_wal), 0)
        payload, manifest = stage(self.c, "cold", True)
        self.assertEqual(db.read_bytes(), original_db)
        self.assertEqual(wal.read_bytes(), original_wal)
        with contextlib.closing(sqlite3.connect(payload / "sources/codex" / db.name)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM data").fetchall(), [("committed-only-in-wal",)])
        source = next(s for s in manifest["sources"] if s["kind"] == "codex")
        self.assertIn(db.name + "-shm", source["consistency_ignored"])
        self.assertNotIn(db.name + "-wal", source["consistency_ignored"])

    def test_cold_rejects_real_wal_commit_and_reports_changed_path(self):
        from codex_vault.engine import copy_tree as real
        def changing(*args, **kwargs):
            databases = real(*args, **kwargs)
            if Path(args[0]) == self.dbroot:
                self.writer.execute("INSERT INTO data VALUES ('new-commit')")
                self.writer.commit()
            return databases
        with patch("codex_vault.engine.copy_tree", side_effect=changing):
            with self.assertRaisesRegex(VaultError, 'modified: "state_99.sqlite-wal"'):
                stage(self.c, "cold", True)

    def test_cold_reports_add_remove_modify_and_retains_unknown_shm(self):
        changed = self.project / "ordinary-shm"
        removed = self.project / "removed.txt"
        changed.write_text("old")
        removed.write_text("old")
        from codex_vault.engine import copy_tree as real
        def changing(*args, **kwargs):
            databases = real(*args, **kwargs)
            if Path(args[0]) == self.project:
                changed.write_text("new-and-longer")
                removed.unlink()
                (self.project / "added.txt").write_text("new")
            return databases
        with patch("codex_vault.engine.copy_tree", side_effect=changing):
            with self.assertRaises(VaultError) as caught:
                stage(self.c, "cold", True)
        message = str(caught.exception)
        self.assertIn('added: "added.txt"', message)
        self.assertIn('removed: "removed.txt"', message)
        self.assertIn('modified: "ordinary-shm"', message)

    def test_nonempty_wal_growth_truncation_and_deletion_remain_significant(self):
        for old_size, new_size in ((0, 4096), (4096, 8192), (4096, 0), (4096, None)):
            with self.subTest(old=old_size, new=new_size):
                before = [("db-wal", old_size, 1)]
                after = [] if new_size is None else [("db-wal", new_size, 2)]
                ignored = sqlite_runtime_paths(before, after, ["db"])
                self.assertNotIn("db-wal", ignored)
                self.assertTrue(any(source_changes(before, after, ignored).values()))

    def test_change_diagnostics_are_bounded_and_escape_filenames(self):
        changes = {"added": ["a\nunsafe-name"] + [f"file-{i:03}" for i in range(30)],
                   "removed": [], "modified": []}
        detail = change_details(changes)
        self.assertIn('"a\\nunsafe-name"', detail)
        self.assertNotIn("a\nunsafe-name", detail)
        self.assertEqual(sum(line.startswith("  added:") for line in detail.splitlines()), 20)
        self.assertIn("11 件", detail)

    def test_source_change_aborts_cold(self):
        from codex_vault.engine import copy_tree as real
        def changing(*args, **kw):
            real(*args, **kw)
            if Path(args[0]) == self.project:
                (self.project / "new-file").write_text("concurrent change")
        with patch("codex_vault.engine.copy_tree", side_effect=changing):
            with self.assertRaisesRegex(VaultError, 'added: "new-file"'):
                stage(self.c, "cold", True)

    def test_archive_rebuild_japanese_search_no_duplicate_events(self):
        p, _ = stage(self.c, "hot")
        output = self.root / "archive"
        r = export_archive(p, output)
        self.assertEqual(r["messages"], 2)
        result = search_archive(output, "復旧手順")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["conversation_id"], "session-1")
        self.assertEqual(search_archive(output, "重複は除外"), [])

    def test_malformed_jsonl_preserved_and_reported(self):
        with self.raw.open("ab") as f:
            f.write(b'{"partial":')
        p, m = stage(self.c, "hot")
        self.assertEqual((p / "sources/codex/sessions/rollout-one.jsonl").read_bytes(), self.raw.read_bytes())
        self.assertTrue(any("JSONL" in w for w in m["warnings"]))
        report = export_archive(p, self.root / "archive")
        self.assertEqual(len(report["malformed_lines"]), 1)

    def test_activate_retains_existing_target_and_bytes(self):
        p, _ = stage(self.c, "hot")
        target = self.root / "new-pc" / "project"
        target.mkdir(parents=True)
        (target / "old.txt").write_text("preserve")
        result = activate(p, "project-001", target, True, True)
        self.assertEqual((target / "untracked.txt").read_text(), "important")
        self.assertEqual((Path(result["previous"]) / "old.txt").read_text(), "preserve")
        with self.assertRaises(VaultError):
            activate(p, "project-001", target)

    def test_activate_rolls_back_failed_install(self):
        p, _ = stage(self.c, "hot")
        target = self.root / "new-pc/project"
        target.mkdir(parents=True)
        (target / "old.txt").write_text("preserve")
        real = Path.rename
        def rename(path, dest):
            if ".vault-new-" in path.name:
                raise OSError("simulated rename failure")
            return real(path, dest)
        with patch.object(Path, "rename", rename), self.assertRaises(OSError):
            activate(p, "project-001", target, True, True)
        self.assertEqual((target / "old.txt").read_text(), "preserve")

    def test_lock_excludes_concurrent_backup(self):
        with locked(self.root / "lock"), self.assertRaises(VaultError):
            with locked(self.root / "lock"):
                pass

    def test_sqlite_path_config_has_precedence_over_env(self):
        (self.codex / "config.toml").write_text('sqlite_home = "' + self.dbroot.as_posix() + '"')
        with patch.dict(os.environ, {"CODEX_SQLITE_HOME": str(self.root / "ignored")}):
            self.assertEqual(detect_sqlite(self.codex), str(self.dbroot))

    def test_sqlite_relative_path_requires_explicit_override(self):
        (self.codex / "config.toml").write_text('sqlite_home = "relative-state"')
        with self.assertRaises(VaultError):
            detect_sqlite(self.codex)

    def test_explicit_sqlite_override_allows_doctor_with_relative_app_setting(self):
        (self.codex / "config.toml").write_text('sqlite_home = "relative-state"')
        self.c["sqlite_home_explicit"] = True
        Path(self.c["config_file"]).parent.mkdir()
        result = subprocess.CompletedProcess([], 0, "restic test-version", "")
        with patch("codex_vault.cli.subprocess.run", return_value=result), patch("codex_vault.cli.restic", return_value="{}"):
            self.assertTrue(doctor(self.c)["repository_access"])

    def test_cli_failed_backup_has_failure_record_and_nonzero_exit(self):
        write_json(self.c["config_file"], self.c)
        with patch("sys.stderr"), patch("sys.stdout"):
            code = main(["--config", self.c["config_file"], "backup", "--mode", "cold"])
        self.assertEqual(code, 1)
        self.assertTrue((Path(self.c["work_dir"]) / "last-failure.json").is_file())

    def test_partial_restic_exit_is_failure(self):
        r = subprocess.CompletedProcess([], 3, "", "some files unreadable")
        with patch("subprocess.run", return_value=r), self.assertRaises(VaultError):
            restic(self.c, ["backup", "payload"])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_linked_worktree_external_common_dir_is_included(self):
        main = self.root / "main-repository"
        subprocess.run(["git", "init", "-q", str(main)], check=True)
        subprocess.run(["git", "-C", str(main), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "--allow-empty", "-qm", "initial"], check=True)
        wt = self.root / "linked"
        subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "-b", "test", str(wt)], check=True)
        (wt / "local-untracked").write_text("keep")
        self.c["projects"] = [str(wt)]
        p, m = stage(self.c, "hot")
        deps = [s for s in m["sources"] if s["kind"] == "git_metadata"]
        self.assertEqual(len(deps), 1)
        self.assertTrue((p / deps[0]["stored_path"] / "HEAD").is_file())
        self.assertTrue(any("未登録のGit worktree" in w for w in m["warnings"]))


@unittest.skipUnless(shutil.which(os.environ.get("VAULT_TEST_RESTIC", "restic")), "set VAULT_TEST_RESTIC to run real restic integration")
class ResticIntegrationTests(Fixture):
    def setUp(self):
        super().setUp()
        restic(self.c, ["init"])

    def test_real_encrypted_backup_restore_validation_and_archive(self):
        first = backup(self.c, "hot")
        self.assertFalse((Path(self.c["work_dir"]) / "payload").exists())
        (self.project / "untracked.txt").write_text("second-version")
        second = backup(self.c, "cold", True)
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(len(snapshots(self.c)), 2)
        restored = restore(self.c, first["snapshot_id"], self.root / "restored")
        p = Path(restored["payload"])
        self.assertEqual((p / "sources/project-001/untracked.txt").read_text(), "important")
        verify_payload(p)
        r = export_archive(p, self.root / "archive")
        self.assertEqual(r["sessions"], 1)
        with self.assertRaises(VaultError):
            restore(self.c, "latest", self.root / "restored")
        restic(self.c, ["check", "--read-data"])
        self.assertEqual(len(snapshots(self.c)), 2)
        retention(self.c, False)
        self.assertEqual(len(snapshots(self.c)), 2)

    def test_snapshot_filter_isolates_other_vaults(self):
        backup(self.c, "hot")
        other = dict(self.c, vault_id="b" * 32)
        self.assertEqual(snapshots(other), [])
        with self.assertRaises(VaultError):
            restore(other, "latest", self.root / "wrong-vault")

    def test_offline_repository_and_wrong_password_fail(self):
        with patch.dict(os.environ, {"RESTIC_PASSWORD": "incorrect-password"}), self.assertRaises(VaultError):
            snapshots(self.c)
        self.c["repository"] = str(self.root / "disconnected/repo")
        with self.assertRaises(VaultError):
            backup(self.c, "hot")


if __name__ == "__main__":
    unittest.main()
