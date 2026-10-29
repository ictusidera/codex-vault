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
