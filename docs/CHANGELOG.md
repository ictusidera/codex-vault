# 1.0.5 — 2026-10-29

- Exclude only the known generated `tmp/arg0`, `tmp/arg0-windows`, `tmp/arg0-linux`, and `tmp/arg0-macos` trees from Codex source collection. Apply the same exclusions before Git discovery. Retain other temporary-directory contents and separately registered projects.
- Centralize staging removal for startup, clean-stage, and post-backup cleanup. On a failed removal of a read-only staging file/directory, clear the read-only attribute and retry once. Restrict all attribute changes to `work_dir/payload`; reject path aliases and preserve unrelated ACL/sharing errors.
- Report a committed snapshot with residual staging as a success with a cleanup warning (exit 2), rather than a failed backup. Record stage_cleanup_complete in last-success.json.
- Add regression tests for generated-directory traversal, retained user files, read-only cleanup, protected source data, persistent sharing failures, and post-commit status. Add a Windows-only real read-only copy test.
- No configuration, credentials, or repository migration is needed.

# 1.0.4 — 2026-10-29

- Fix the identified hot-backup failure at the Python `read(16)` of `.sqlite-maintenance.lock`.
- Exclude that exact maintenance-lock name in Codex and external SQLite source trees; upstream maintenance workers create one lock per database directory.
- Exclude only `*.lock` entries under the top-level `thread-writer-locks` and `mcp-oauth-locks` paths of Codex sources. Preserve persistent metadata in those directories and ordinary project lock files elsewhere. Add root-anchored exclusion patterns to express this boundary.
- Avoid raw header reads of recognized SQLite database sidecars, which the Online Backup API handles instead.
- Preserve fatal permission failures for other files; no lock deletion, privilege escalation, or configuration/repository migration is involved.
- Add regression coverage for denied reads, external SQLite homes, nested log locations, retained OAuth metadata and project locks, SQLite WAL contents, and a Windows-only byte-range lock test.

# 1.0.3 — 2026-10-29

Diagnostic release: adds global --debug before the subcommand to print exception stacks and source-copy context. Does not silently skip permission failures or claim to resolve an unidentified lock. No configuration migration required.

# 1.0.2 — 2026-10-29

- Exclude Codex runtime `.codex-provisioning-*.guard` files from collection.
- Apply exclusions before opening files for SQLite header detection. Previously, even excluded files could cause a read-permission failure.
- Record omissions in the manifest; retain fatal errors for unreadable real history or other non-excluded files.
- Added three regression tests, including simulated Windows guard read denial. No configuration changes or repository reinitialization required.

# 1.0.1 — 2026-10-29

- Git common-directory discovery no longer uses `--path-format=absolute`.
  Older Git versions can echo this unrecognized option to standard output
  without a failing exit code, causing 1.0.0 to treat it as a backup path.
- Relative `--git-common-dir` results are resolved against the repository
  passed to `git -C`, rather than the Python process working directory.
- Reject malformed or nonexistent Git common-directory results explicitly.
- Added regression coverage for relative paths, absolute paths, and successful
  commands returning an echoed option instead of a path.

No configuration or repository-format migration is required. Retain existing
vault.json, credentials, and backup repositories. Do not run configure or init
again merely to install this update.
