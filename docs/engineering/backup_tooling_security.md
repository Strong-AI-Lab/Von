# Backup Tooling Security Hardening

This document defines the threat model and operating policy for Von backup tooling driven via `run.ps1` and `scripts/backup_von_db.py`.

## Intended use

- Backups are an admin-level operational action.
- Backups dump the configured MongoDB database (`VON_DB_NAME`) and therefore include user and organisation content.
- Backups are intended for trusted operators running in controlled environments.

## Threat model

- If an actor has local shell access plus database credentials, they can exfiltrate data regardless of this tooling.
- The backup command lowers effort for exfiltration in shared-machine scenarios.
- Writing backup artefacts under the repository root increases accidental sharing/commit risk.

## Decision

We keep backup execution as local-operator tooling (not a remote server endpoint), with explicit safety guardrails:

1. Manual backup action (`.\run.ps1 backup`) requires explicit opt-in via `VON_ENABLE_BACKUP_ACTION=1`.
2. Backup apply mode is blocked when output resolves under the repository root, unless explicitly overridden with `VON_ALLOW_BACKUP_IN_REPO=1`.
3. Scheduled daily backups use the same in-repo output safety check and skip if unsafe.
4. Backup artefacts in `backups/` are blocked by pre-commit guardrails.
5. Default backup root resolution prefers non-repository locations:
   - `VON_BACKUP_ROOT`
   - `W:\von_backups`
   - `%LOCALAPPDATA%\Von\backups`
   - repository `backups/` only as a last resort

## Required operator configuration

- Set `VON_BACKUP_ROOT` to a path outside the repository.
- Enable manual backup only when needed: `VON_ENABLE_BACKUP_ACTION=1`.
- Leave `VON_ALLOW_BACKUP_IN_REPO` unset (or `0`) unless you intentionally accept repository-path risk.

Example:

```powershell
$env:VON_BACKUP_ROOT = 'C:\von_backups'
$env:VON_ENABLE_BACKUP_ACTION = '1'
.\run.ps1 backup -BackupTag manual
```

## Notes

- `scripts/backup_von_db.py` still supports dry-run and policy controls (retention, compression, encryption).
- For the canonical local operator restore path and restore-drill procedure, see `docs/engineering/local_backup_restore_runbook.md`.
- Encryption at rest remains optional and controlled by:
  - `VON_BACKUP_ENCRYPTION_ENABLED`
  - `VON_BACKUP_ENCRYPTION_KEY`
