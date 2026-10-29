# Codex Vault 1.0

Codex Vault backs up local Codex conversations, state databases, settings, and development projects as encrypted, versioned snapshots in a restic repository. It is designed primarily for everyday use on Windows and uses only the Python standard library.

**Start by backing up your current folder layout. You do not need to move existing projects.**

This is the English edition of the README. Save it as `README.en.md` in the tool's root directory if you want its relative links to work. Referenced scripts, example configuration, and validation notes are included in the original tool package. This translation does not change the tool itself; its existing prompts and diagnostic messages may still be in Japanese.

## 1. Architecture and Design Decisions

The chosen architecture is **a Python collection, validation, and recovery tool + restic + your existing Git repositories**. The recommended standard location for new projects is `C:\Dev\Projects`, but using that location is not a requirement for backups. The tool is independent of your Git hosting provider, including GitHub and Azure DevOps.

| Decision | Rationale and trade-offs |
|---|---|
| Use restic as the single storage engine | Delegate encryption, deduplication, compression, snapshot retention, storage backends, and restoration to an existing engine. Kopia is also a capable option, but this implementation prioritizes CLI integration, exit codes, and automated validation. Supporting one engine avoids separate implementation and recovery paths. |
| Collect files in Python | Use SQLite's Online Backup API to capture Codex databases, then pass validated staging copies to restic. Identify SQLite files by their headers rather than relying on fixed database names or version numbers. |
| Make a complete staging copy | Freeze the set of files to be stored in each snapshot so the same set can be validated. This requires temporary space and a full read/write pass on every run. restic deduplicates data in the repository, but it does not reduce the I/O needed to make the staging copy. |
| Separate hot and cold backups | Hot backups reduce potential loss while applications are running. Cold backups provide recovery reference points after all relevant applications have been closed. A valid individual database in a hot backup does not guarantee that multiple databases, JSONL files, and source files represent the same instant. |
| Restore, validate, then activate | Restore into an empty directory before changing the original environment. An explicit `activate` operation preserves the existing directory and rolls back if the switch fails. |
| Keep raw JSONL files | Preserve the originals in case Codex changes in the future. Markdown and the searchable SQLite index are derived views that can be regenerated from those originals. |
| Include untracked and Git-ignored files | Protect `.env` files, work in progress, and documents that are not committed to Git. Do not use `.gitignore` as the backup exclusion list. |
| Separate retention cleanup from routine backups | A successful backup never automatically deletes older snapshots. The `retention` command only previews deletions by default, and `prune` is a separate operation. |
| Exclude authentication data by default | Exclude `auth.json` and sign in again after recovery. Protect the restic password with Windows DPAPI, and also keep it separately in a password manager. |

This implementation does not use VSS. restic receives files only after staging copies have been created, so applying VSS to those copies would not improve consistency across the original Codex environment. If a locked regular file cannot be read, the operation fails; close the relevant application and try again.

## 2. Capabilities and Scope

- Collect the entire `CODEX_HOME` directory as the basic backup scope, including `sessions`, `archived_sessions`, indexes, SQLite databases, settings, and Codex-managed worktrees. Collection does not depend on a fixed list of filenames.
- Register a separate source when `sqlite_home` is outside `CODEX_HOME`. Initial detection uses this order: an explicit `--sqlite-home`, top-level `sqlite_home` in `config.toml`, `CODEX_SQLITE_HOME`, then `CODEX_HOME`.
- Preserve `.git`, uncommitted changes, untracked files, and Git-ignored files under registered project directories.
- Detect external Git common directories and add them as sources for Git metadata. Other worktrees located outside the registered sources generate warnings; their working files are not registered automatically.
- Validate SHA-256 hashes, file sizes, file inventories, empty-directory inventories, and SQLite databases using `quick_check`.
- Prioritize preservation of the original bytes even when JSONL files are damaged or incomplete, and report lines that cannot be parsed.
- Extract recognized user and assistant messages into Markdown and SQLite, with substring search that supports Japanese text.
- Include the recovery tool and README in each snapshot under `recovery/tool`. Configuration JSON and passwords are not included in that recovery-tool copy.

### Exclusions and Limitations

- Default project exclusions are `node_modules`, `__pycache__`, `.venv`, `venv`, and `.pytest_cache`. These rules match path components, not just directory names. Remove an exclusion from the configuration if it would omit an important asset.
- `auth.json` is excluded by default. Other settings or conversations may still contain secrets. Treat the entire backup as confidential.
- Symbolic links, junctions, and reparse points are not followed. Their omission is recorded and reported. Register the required target directories separately, for example in `extra_sources`.
- The tool does not automatically collect the Linux side of WSL, other OS users' data, another `CODEX_HOME`, cloud conversations, external attachments, application-specific UI state outside `CODEX_HOME`, or OS credential stores.
- This is not a system-image backup that faithfully restores Windows ACLs, ownership, alternate data streams, hard-link relationships, file IDs, or extended attributes. Its purpose is to preserve ordinary file contents and directories.
- The tool does not rewrite Codex database schemas or recorded paths. It cannot guarantee that the Codex app will display or resume restored conversations.
- Archive export does not use AI summarization. It also does not completely convert tool output, internal reasoning, or attachment contents into Markdown. Consult the originals for unsupported structures.
- External business databases such as SQL Server are outside the scope of this tool. Use their appropriate database backup procedures.

## 3. Recommended Directory Layout

```text
C:\Dev\Tools\codex-vault\                Tool files extracted from the package
C:\Dev\Projects\                        Standard for new projects; existing projects may stay where they are
%LOCALAPPDATA%\CodexVault\vault.json     Backup sources and destination settings
%LOCALAPPDATA%\CodexVault\repository.dpapi
%LOCALAPPDATA%\CodexVault\work\          Staging copies and validation results
%LOCALAPPDATA%\CodexVault\logs\          Scheduled-run logs
E:\CodexVault\restic\                    Encrypted repository on an external SSD or similar storage
```

Keep backup storage independent of the PC itself. A separate partition on the same PC is insufficient protection against PC failure. In addition to an external SSD or NAS, a second independent restic repository at another location or in cloud storage is recommended. Continue pushing your Git repositories to their remotes as well.

Conversations and source files exist in plaintext in the staging directory. On Windows, the tool configures user-specific access controls and removes staging copies after a successful backup. Copies remain for diagnosis after failures or forced termination. Use BitLocker or equivalent protection for the system drive as well. Ordinary deletion does not guarantee secure erasure on an SSD.

The staging area needs **at least enough free space for all registered backup data**. During activation, the destination also needs room for a new copy. For large collections of build outputs or other files, review exclusions and consider increasing the scheduled interval to 120 minutes or more.

## 4. Initial Setup on Windows

### 4.1 Requirements

- Python **3.11 or later**. An official Python distribution such as 3.12 is recommended. No additional Python packages are required.
- restic **0.17 or later**. This release was tested with actual encrypted backups and restoration using **restic 0.19.1 / Python 3.12**.
- Git, used for repository and worktree diagnostics. The tool does not install Git.
- Windows PowerShell 5.1 or later, used to register scheduled tasks.

Install restic following its [official installation instructions](https://restic.readthedocs.io/en/stable/020_installation.html). One option is to extract the executable from an official Windows release ZIP, name it `restic.exe`, and place it on your PATH. If you install a package found through `winget search restic`, verify the publisher and package details. Codex Vault does not automatically install external programs.

```powershell
python --version
restic version
git --version
Set-Location C:\Dev\Tools\codex-vault
python -m codex_vault --help
```

If `python` is not available, use a command such as `py -3.12`. The following examples use `python`. Scheduled tasks must use the absolute path to the actual `python.exe`, rather than the Python launcher.

### 4.2 Create the Configuration

```powershell
python -m codex_vault configure `
  --repository 'E:\CodexVault\restic' `
  --project 'C:\Dev\Projects' `
  --project 'D:\ExistingProject'
```

Replace these example paths with directories that actually exist. Registering a nonexistent directory causes an error. Do not register both the entire `C:\Dev\Projects` directory and individual projects inside it as overlapping sources.

Running `configure` without arguments prompts for the repository location and project paths. To protect only Codex history initially, use `--history-only`.

```powershell
python -m codex_vault configure --repository 'E:\CodexVault\restic' --history-only
```

If you use different storage locations, specify `--codex-home` and `--sqlite-home` explicitly. Environment variables are resolved to fixed absolute paths during initial setup. This prevents scheduled runs from silently switching sources because of a different environment. If a profile in `config.toml`, a startup `-c` argument, or an administrative policy changes the storage location, specify the actual path explicitly.

Using `--sqlite-home` records `sqlite_home_explicit: true`. The `doctor` command reports differences from the automatically detected value as a note. Set this field appropriately when registering an explicit path by editing the configuration manually as well.

The configuration is created at `%LOCALAPPDATA%\CodexVault\vault.json`. Add sources by editing its `projects` or `extra_sources` arrays. See the included `config.example.json` for an example. Forward slashes, such as `C:/Dev/Projects`, are convenient when writing paths in JSON.

Running `configure` against an existing configuration is rejected. Edit the existing JSON to make changes, and retain its `vault_id` rather than generating a new one.

### 4.3 Register the Repository Password

```powershell
python -m codex_vault credential
```

Enter a strong password of at least 12 characters twice. Input is not displayed. The tool protects the password using DPAPI for the current Windows user.

**Also record the same password in a password manager or another secure location independent of the PC. The DPAPI file alone is not sufficient for recovery on another PC.** Even if the Windows account name is the same, the new PC may not be able to decrypt that file.

### 4.4 Initialize the Repository and Review Sources

```powershell
python -m codex_vault init
python -m codex_vault doctor
python -m codex_vault scan
```

Run `init` only once for a new repository. You do not need to repeat it for an existing repository. `doctor` displays registered sources, the detected SQLite location, repository connectivity, and available staging space. `scan` reads past conversations to identify candidate working directories. It does not register or move those directories automatically.

Add important projects marked `covered: false` to `projects`. Missing directories, old worktrees, and paths from another OS cannot be recovered automatically. If important files are outside a project's root directory, add their directories to `extra_sources`.

To prevent recursive backups, the tool rejects layouts in which configuration, staging space, or the restic repository overlaps the backup sources.

### 4.5 Make the First Backup in Cold Mode

Close the Codex app, Codex CLI, IDE extensions, and any processes editing, building, or synchronizing the target files. Then run the following from an ordinary PowerShell session.

```powershell
Set-Location C:\Dev\Tools\codex-vault
python -m codex_vault backup --mode cold --stopped
python -m codex_vault snapshots
python -m codex_vault check --read-data
```

`--stopped` is your confirmation that the relevant applications have stopped. It does not automatically prove that every process is stopped. A cold backup fails if the source file inventory, file sizes, or modification times change during collection. Unsaved changes that exist only inside an application's memory cannot be backed up.

**After the first backup, complete at least one restore test using the procedure below before starting routine operation.**

## 5. Routine Operation

| When | Action | What to check |
|---|---|---|
| Every 30 minutes while logged on | Run scheduled hot backups | Exit code, warnings, and last successful backup time |
| At the end of the working day and before updates | Close relevant applications and make a cold backup | Exit code 0; review omissions and warnings if the code is 2 |
| Weekly | Run `check` | Repository structure; this does not read all stored data |
| Monthly | Run `check --read-data` | Read and validate all stored data |
| Monthly | Preview `retention`, then apply if appropriate | Ensure important historical snapshots will remain |
| Quarterly and before changing PCs | Restore into an empty directory | Validate hashes and databases, then actually open important conversations and source files |
| As part of normal development | Git commit / push | Preserve development history on the remote as well |

### Manual Commands

```powershell
python -m codex_vault backup --mode hot
python -m codex_vault status
python -m codex_vault check
```

`status` keeps the last success and the last failure separately. The failure record remains after a later success, so compare their timestamps. If `hours_since_success` is 24 or greater, check repository connectivity and scheduled-task execution. This release does not include email or other outbound failure notifications.

### Register Tasks in Windows Task Scheduler

```powershell
$configPath = Join-Path $env:LOCALAPPDATA 'CodexVault\vault.json'
$pythonExe = (& python -c 'import sys; print(sys.executable)').Trim()
.\scripts\Install-ScheduledTasks.ps1 -Config $configPath -PythonExe $pythonExe
```

This registers a hot backup every 30 minutes and a `check` every Sunday at 09:00. Times use the Windows local time zone. For a PC configured for Japan, that means Japan Standard Time. Backups also run on weekends.

**Tasks run only while the current user is logged on.** A locked session is still eligible if the user remains logged on. Backups cannot run while logged off or while the PC is powered off. No Windows password is stored in the tasks. Cold backups are not scheduled automatically because they require relevant applications to be stopped. Overlapping operations, including a check and backup, are rejected by the tool's lock; retry manually or wait for the next scheduled run.

For a large set of projects, you can use a longer interval:

```powershell
.\scripts\Install-ScheduledTasks.ps1 -Config $configPath -PythonExe $pythonExe `
  -NamePrefix 'CodexVaultSlow' -BackupIntervalMinutes 120
```

Existing task names are not overwritten. Remove obsolete tasks in Task Scheduler before registering them again. If organizational policy restricts task registration or execution, consult an administrator or use manual operation. Permanently relaxing PowerShell's execution policy is not required. Follow your organization's rules for unblocking or signing scripts.

Run logs are retained for 30 days in the `logs` directory alongside the configuration file. Also check **Last Run Result** in Task Scheduler.

| Exit code | Meaning |
|---|---|
| `0` | Success. The general point-in-time consistency note for hot backups alone does not cause a warning exit code. |
| `2` | Backup completed, but there are specific warnings such as omitted links, unparseable JSONL lines, or changes during collection. |
| `1` | Failure. restic's partial-success exit code 3 is also treated as a failure by this tool. Earlier successful snapshots are retained. |

Unexpected termination may produce another Python or OS exit code. Check the logs for any nonzero result.

### Retention and Space Reclamation

```powershell
python -m codex_vault retention
```

By default, this only previews deletions. The policy keeps all snapshots from the last two days, plus 30 daily, 8 weekly, and 12 monthly snapshots. Retention is grouped by hot/cold mode, host, and backup path, so frequent hot backups do not displace cold recovery reference points. It does not create snapshots for days when no backup ran.

Review the plan and run the following only if the proposed deletions are acceptable:

```powershell
python -m codex_vault retention --apply
python -m codex_vault prune
```

`retention --apply` removes the selected old snapshots. `prune` reclaims unreferenced data across the repository and then runs `check`. Pruning can take time and may prevent backups while it runs, so perform it manually when sufficient time is available. Retention does not select snapshots belonging to other vaults.

If you need to preserve a milestone for the long term, first protect it in another independent repository and manage that repository's retention policy separately. This release does not provide a permanent-protection tag feature.

## 6. Restore Testing and Recovery

### 6.1 Restore into an Empty Directory

```powershell
python -m codex_vault snapshots
python -m codex_vault restore --snapshot latest --target 'C:\Dev\Recovery\test-20261028'
```

Snapshot selection is restricted to snapshots belonging to the configuration's `vault_id`. To select a specific snapshot, use an ID of at least eight hexadecimal characters from the list.

```powershell
python -m codex_vault restore --snapshot '12345678' --target 'C:\Dev\Recovery\chosen'
```

The tool rejects nonempty restore destinations, as well as locations overlapping backup sources, staging space, or the repository. A directory from a failed restore is left in place. Use another empty directory for the next attempt.

On success, the tool reports the `payload` location, source IDs and original paths, backup mode, and warnings. It validates the restored data automatically. You can also repeat validation explicitly:

```powershell
python -m codex_vault verify --payload 'C:\Dev\Recovery\test-20261028\payload'
```

```text
payload/
  manifest.json            Sources, original paths, hashes, exclusions, warnings, and Git information
  sources/
    codex/                 Saved CODEX_HOME contents
    sqlite/                External SQLite location, if any
    project-001/           Registered project source
    extra-001/             Explicitly registered external directory, if any
    git-.../               External Git metadata, if any
  recovery/tool/           Recovery tool and README
```

Check the source IDs and original paths in the manifest for each snapshot. Changing the registration order can change project numbers. Do not infer a project's identity from its number alone.

Hash validation confirms that restored files match the copies captured for the backup. It does not prove that the original conversations were logically complete, that Codex can resume them, or that the source code builds. Open important files and check the build in the appropriate environment.

### 6.2 Try Restoring the Codex Environment

1. Close Codex, CLI sessions, IDEs, and other processes that might edit the recovery destination.
2. If possible, also preserve the current problematic state in another snapshot or on another disk.
3. Start by trying recovery with the same paths, the same OS user, and a similar Codex version.
4. Match original paths and source IDs against the output of `verify`.
5. Use `activate` to switch one source at a time. Do not start Codex until every required source, including external SQLite storage, has been restored.

```powershell
$payload = 'C:\Dev\Recovery\chosen\payload'
python -m codex_vault activate --payload $payload --source codex `
  --target "$env:USERPROFILE\.codex" --stopped --apply
```

This example assumes the default `CODEX_HOME`. Substitute your original configured location if you use a custom path. For an external SQLite directory:

```powershell
python -m codex_vault activate --payload $payload --source sqlite `
  --target 'D:\CodexState' --stopped --apply
```

If a project source also needs to be restored:

```powershell
python -m codex_vault activate --payload $payload --source project-001 `
  --target 'C:\Dev\Projects' --stopped --apply
```

Here, `project-001` represents the entire projects directory. If you registered an individual project, use that project's original path instead. **This operation switches the entire target directory, so verify the original path and source ID carefully.**

After validating the copied files, `activate` renames the existing directory to a name such as `original-name.before-vault-timestamp...` and puts the restored copy in its place. If the switch fails, it restores the previous directory. Preserved directories are not deleted automatically. Activation operates on one source at a time; restoring multiple sources is not a single transaction. Confirm that all required sources have been restored before starting applications.

If you cannot confirm that applications have stopped, or you only want to inspect the backup, do not activate it; browse the restored files instead. Authentication and UI settings outside `CODEX_HOME` may need to be configured again. If `auth.json` was excluded, sign in again.

### 6.3 Roll Back an Activation

Close Codex and related applications again. Use the exact `previous` path returned by `activate`. Move the attempted recovery directory aside, then move the previous directory back. For example:

```powershell
# Replace the example with the exact path reported by activate before running.
$target = Join-Path $env:USERPROFILE '.codex'
$previous = 'C:\Users\USER\.codex.before-vault-ACTUAL-TIMESTAMP'
$failed = $target + '.recovery-attempt-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
if (-not (Test-Path -LiteralPath $previous -PathType Container)) { throw 'Previous directory not found' }
Move-Item -LiteralPath $target -Destination $failed -ErrorAction Stop
Move-Item -LiteralPath $previous -Destination $target -ErrorAction Stop
```

If you also switched external SQLite storage or other sources, roll them back to a matching state as well. Delete preserved directories only after you have checked the conversations, code, and builds.

### 6.4 Recover Conversations as Readable, Searchable Archives

```powershell
python -m codex_vault archive `
  --payload 'C:\Dev\Recovery\chosen\payload' `
  --output 'C:\Dev\Recovery\archive-20261028'
python -m codex_vault search `
  --archive 'C:\Dev\Recovery\archive-20261028' --query 'RabbitMQ'
```

Use an empty output directory. The command generates `conversations/*.md`, `index.sqlite`, and `archive-report.json`. It retains conversation IDs, original paths, and SHA-256 hashes of the raw files. For recognized formats that record the same message twice, export prefers `response_item` messages over duplicate `event_msg` messages. Search returns up to 50 substring matches.

Archive output is plaintext. Keep it on encrypted storage or use equivalent protection. Review the report for unsupported formats and unparseable lines. The raw JSONL files remain in the restored payload.

### 6.5 Recover After Losing the PC, Configuration, and Tool

1. Install restic on the replacement PC and connect the external backup storage.
2. Retrieve the **original restic password** from its independent secure location. The DPAPI file alone is insufficient.
3. Use restic directly to list and restore snapshots. Enter the password interactively.

```powershell
restic -r 'E:\CodexVault\restic' snapshots
restic -r 'E:\CodexVault\restic' restore 'ACTUAL-SNAPSHOT-ID' `
  --target 'C:\Dev\Recovery\disaster'
```

Rather than using `latest`, select a timestamp and ID you have checked in the list. A snapshot left behind after partial success may fail validation. In that case, try an earlier cold snapshot.

4. The restored Python tool is available under `payload\recovery\tool`.

```powershell
Set-Location 'C:\Dev\Recovery\disaster\payload\recovery\tool'
python -m codex_vault verify --payload 'C:\Dev\Recovery\disaster\payload'
```

5. Check original paths, exclusions, and warnings in `manifest.json`. You can recover files and export a readable archive first.
6. When creating a new configuration, carry over the `vault_id` from the restored manifest to access earlier snapshots through Codex Vault. Adjust source paths for the replacement PC.
7. On Windows, register **the same password used by the existing repository** with `credential`. Do not run `init` again against that existing repository.
8. Once configuration is ready, use the activation procedure above if restoring to the original paths. When changing paths, prefer a new Codex session with context carried over from the archive; do not perform wholesale path substitutions in the existing databases.

### Git Worktrees

A linked worktree whose `.git` entry is a file records information such as the original absolute path. Git metadata is preserved, but recovery on another PC or at a different path may require a command such as `git worktree repair`. Check `git`, `warnings`, and `sources` in `manifest.json`.

Preserving the Git common directory alone cannot recover uncommitted or untracked working files from an unregistered worktree. Add important worktrees reported in warnings to `projects` when setting up routine backups. Also protect dependencies such as submodules, Git LFS content, external object references, and external data, and verify that checkout and build operations actually work during a recovery test.

## 7. A Second Independent Backup Destination

Use this procedure to avoid relying on a single external disk. Instead of copying a live restic repository with an ordinary synchronization tool, use a separate configuration to make independent backups into a second repository. Collecting the sources twice increases I/O.

The following example creates a configuration for another external drive:

```powershell
$primaryPath = Join-Path $env:LOCALAPPDATA 'CodexVault\vault.json'
$secondaryRoot = Join-Path $env:LOCALAPPDATA 'CodexVaultSecondary'
New-Item -ItemType Directory -Path $secondaryRoot -Force | Out-Null
$secondaryPath = Join-Path $secondaryRoot 'vault.json'
if (Test-Path -LiteralPath $secondaryPath) { throw 'Configuration already exists' }
$settings = Get-Content -LiteralPath $primaryPath -Raw | ConvertFrom-Json
$settings.repository = 'F:\CodexVault\restic'
$settings.work_dir = Join-Path $secondaryRoot 'work'
# Retain the original vault_id and credential_file. DPAPI is tied to the current user.
$settings | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $secondaryPath -Encoding UTF8
python -m codex_vault --config $secondaryPath init
python -m codex_vault --config $secondaryPath backup --mode hot
```

Run backups, checks, and restore tests using the secondary configuration as well. If you schedule it, use a different prefix, such as `-NamePrefix CodexVaultSecondary`. The repositories are independent: this procedure does not copy existing snapshots, and snapshots from before the second repository was created will not be present there.

For a NAS, a UNC path is recommended. Confirm that it is accessible from the user's logon session. For cloud storage, change the repository setting to a backend supported by restic, such as SFTP or S3, and configure authentication according to restic's official instructions. Codex Vault does not automatically configure cloud credentials, network settings, or deletion protection.

Encryption alone does not prevent ransomware or mistakes from deleting backups. Maintain independence by disconnecting one destination, separating access through another account, or using destination-side deletion protection.

## 8. Troubleshooting

| Situation | Action |
|---|---|
| SQLite capture or a regular file copy fails | Close Codex, IDEs, and related processes, then retry in cold mode. Do not delete the originals. |
| Warning about unparseable JSONL | Preserve the raw backup and earlier snapshots. Check parsing locations in `archive-report.json`. |
| Links or junctions were omitted | Register their actual target directories as separate sources. Do not automatically move the original folders. |
| Backup drive disconnected | Reconnect it, run `doctor`, and retry. A backup operation does not silently initialize a new repository. |
| Password mismatch or DPAPI failure | Check the configuration and Windows user. On a replacement PC, register the independently stored original password. Do not delete or reinitialize the existing repository. |
| Lock in use | Wait for the other operation to finish. The OS releases the tool's own lock when its process exits. Investigate restic repository locks using its official procedure, and act only after confirming that no relevant operation is running. |
| Old staging copies remain | After investigating the cause, remove them with `clean-stage` below. They are also replaced when the next collection begins. |
| Restored data fails validation | Do not activate it over the original environment. Restore an earlier snapshot into another empty directory and run `check --read-data` against the repository. |
| `doctor` reports a different SQLite location | Verify where the application actually stores data, then update the configuration. Preserve important data at the old location as a separate source if necessary. |

```powershell
python -m codex_vault clean-stage
```

Logs contain paths and error details. Review them for confidential information before sharing. The tool does not intentionally output the restic password to the screen, configuration JSON, or logs.

## 9. Linux, macOS, and WSL

The core Python backup, validation, and archive operations are shared across platforms. DPAPI and PowerShell task registration are Windows-specific. Treat Windows and WSL as **separate configurations and backup sources**. A backup of the Windows side alone does not establish that WSL data is protected.

On non-Windows systems, create a restic password file in a secure location outside the tool directory and restrict it to its owner. Do not type the password directly into a command that will be saved in shell history.

```bash
export RESTIC_PASSWORD_FILE=/absolute/private/restic-password.txt
python3 -m codex_vault --config /absolute/private/vault.json configure \
  --repository /mnt/backup/codex-restic --project /home/user/projects
python3 -m codex_vault --config /absolute/private/vault.json init
```

The tool does not extract Codex credentials from macOS Keychain or Linux secret stores. Sign in again after recovery. Validate Windows-specific behavior, including junctions, ACLs, Task Scheduler, and DPAPI, on an actual Windows machine before relying on it.

## 10. Validation and Maintenance

```powershell
python -m unittest discover -s tests -v
```

If restic is on PATH, the tests also exercise actual encrypted backups and restoration. To specify an executable explicitly:

```powershell
$env:VAULT_TEST_RESTIC = 'C:\Tools\restic.exe'
python -m unittest discover -s tests -v
```

Tests use synthetic data in temporary directories, not your actual `CODEX_HOME` or project directories. Details of validation and environment limitations are recorded in [docs/VALIDATION.md](docs/VALIDATION.md), included in the original package.

Whole-directory collection accommodates changes to internal filenames, while retaining raw files provides a fallback for changes to JSONL formats. Make a cold backup immediately before a Codex update, then check conversation visibility and backups after the update.

Codex Vault is not an official OpenAI backup product. Preserving the original files and having the Codex app recognize restored history are separate concerns. It does not guarantee complete restoration inside the app; it prioritizes preserving originals, development files, and readable conversation history.

## 11. Official References

Design references were reviewed on **2026-10-28**. The implementation does not depend on Codex database table schemas.

- [OpenAI: Codex environment variables and storage locations](https://developers.openai.com/codex/config-file/environment-variables)
- [OpenAI: Configuration reference](https://developers.openai.com/codex/config-reference/)
- [SQLite Online Backup API](https://www.sqlite.org/backup.html)
- [restic: Backups and exit codes](https://restic.readthedocs.io/en/stable/040_backup.html)
- [restic: Restoration](https://restic.readthedocs.io/en/stable/050_restore.html)
- [restic: Snapshot retention and pruning](https://restic.readthedocs.io/en/stable/060_forget.html)
- [Kopia: Features](https://kopia.io/docs/features/)

The code is licensed under MIT and may be modified and used internally. The restic distribution itself is not included.
