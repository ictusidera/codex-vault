import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_vault.archive import export_archive, search_archive
from codex_vault.core import VaultError, activate, locked, read_json, verify_payload, write_json
from codex_vault.engine import backup, restic, restore, retention, snapshots, stage
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

    def test_source_change_aborts_cold(self):
        from codex_vault.engine import copy_tree as real
        def changing(*args, **kw):
            real(*args, **kw)
            if Path(args[0]) == self.project:
                (self.project / "new-file").write_text("concurrent change")
        with patch("codex_vault.engine.copy_tree", side_effect=changing):
            with self.assertRaises(VaultError):
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
