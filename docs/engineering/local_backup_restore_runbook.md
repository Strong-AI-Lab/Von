# Local Backup and Restore Runbook

This runbook is the canonical local operator procedure for Von database backup
artefacts created by `run.ps1` and `scripts/backup_von_db.py`.

Use it for:

- safe local backup inspection
- non-production restore drills
- recovering a dump into a scratch database before any deliberate production
  repair

This runbook is intentionally PowerShell-first.

## 1. Backup surfaces

The shared local backup path now has three inspectable layers:

- backup artefact
  - directory, `.zip`, or `.zip.enc`
- per-artefact receipt sidecar
  - `<artefact>.backup_receipt.json`
- launcher last-success receipt
  - `.run/last_successful_backup_receipt.json`

The launcher receipt is the authoritative scheduler bookkeeping record.
Artefact sidecars are used for drift repair and inspection.

## 2. Safety posture

- Restore is destructive if pointed at a live target database.
- The `run.ps1 restore-backup` action is disabled unless
  `VON_ENABLE_RESTORE_ACTION=1`.
- Dry-run is the default.
- The safest restore target is a scratch database such as
  `von_db_restore_probe`.
- Restore to the production database should be a deliberate operator action,
  not the first troubleshooting step.

## 3. Inspect the latest backup

Check the launcher receipt:

```powershell
Get-Content .run\last_successful_backup_receipt.json -Raw
```

Typical useful fields:

- `completed_at_utc`
- `db_name`
- `final_artifact_path`
- `artifact_kind`
- `artifact_size_bytes`
- `collection_count`

If the backup root resolves to `W:\von_backups`, the created artefact is
already on the off-machine share. If backups are first created locally, the
launcher migration path verifies the copied/moved offsite artefact and logs its
non-zero size.

## 4. Dry-run a restore drill

Use the launcher wrapper first:

```powershell
$env:VON_ENABLE_RESTORE_ACTION = '1'
.\run.ps1 restore-backup `
  -RestoreBackupPath .run\last_successful_backup_receipt.json `
  -RestoreTargetDbName von_db_restore_probe
```

This does not run `mongorestore`. It reports:

- the selected backup input
- the final backup artefact
- the source DB found in the dump
- the target DB that would be used
- the redacted MongoDB target URI
- a cheap dump summary (`collection_count`)

You can also point `-RestoreBackupPath` directly at:

- a dump directory
- a `.zip`
- a `.zip.enc`
- an artefact-sidecar receipt JSON

Encrypted backups require `VON_BACKUP_ENCRYPTION_KEY` (or the script-level
`--fernet-key` argument).

## 5. Run a non-production restore

Restore into a scratch database and drop any prior copy of that scratch DB:

```powershell
$env:VON_ENABLE_RESTORE_ACTION = '1'
.\run.ps1 restore-backup `
  -RestoreBackupPath .run\last_successful_backup_receipt.json `
  -RestoreTargetDbName von_db_restore_probe `
  -RestoreApply `
  -RestoreDropTarget
```

Equivalent direct script path:

```powershell
python scripts/restore_von_db.py `
  --backup-path .run\last_successful_backup_receipt.json `
  --target-db-name von_db_restore_probe `
  --apply `
  --drop-target
```

## 6. Blob-backed restore drill (Catalyst Cloud / S3)

When a backup receipt contains `blob_key` and `blob_backend`, the artefact
can be restored directly from the blob store.  This path is useful when:

- the local artefact has been deleted or the backup host is unavailable, and
- the receipt JSON is available (e.g. committed to `.run/` or emailed to the operator).

### Prerequisites

- Swift / S3 credentials are set (see `docs/engineering/catalyst_cloud_swift_setup.md`).
- `VON_BACKUP_ENCRYPTION_KEY` is set (needed to decrypt `.zip.enc` after download).

### Automatic fallback

If `--backup-path` points at a receipt that has a `blob_key` and the local
`final_artifact_path` is missing, the restore script automatically downloads
the artefact from the blob store before materialising it:

```powershell
$env:VON_ENABLE_RESTORE_ACTION = '1'
$env:VON_BACKUP_ENCRYPTION_KEY  = '<fernet-key>'
$env:OS_CLOUD                   = 'catalystcloud'
$env:VON_SWIFT_CONTAINER        = 'von-mongo-backups'

python scripts/restore_von_db.py `
  --backup-path .run\last_successful_backup_receipt.json `
  --target-db-name von_db_restore_probe
```

The script prints `[restore] Downloading from blob store: key=...` and then
verifies the SHA-256 of the downloaded bytes against `blob_sha256` in the receipt.

### Explicit blob-first flag

To force a blob download even when the local artefact still exists:

```powershell
python scripts/restore_von_db.py `
  --backup-path .run\last_successful_backup_receipt.json `
  --from-blob `
  --blob-backend swift `
  --target-db-name von_db_restore_probe `
  --apply `
  --drop-target
```

The `--blob-backend` argument overrides the `blob_backend` field in the receipt
and the `VON_BACKUP_BLOB_BACKEND` env var.

### Restore drill checklist

1. Run without `--apply` first (dry-run, default).
2. Confirm `[restore] SHA-256 verified` in the output.
3. Confirm `Source DB` and `Target DB` match expectation.
4. Re-run with `--apply --drop-target` to restore into `von_db_restore_probe`.
5. Inspect the scratch DB; never target `von_db` unless deliberate recovery is required.
6. Drop the scratch DB when finished.

## 7. Production recovery guidance

For a real production recovery:

1. Start with a dry-run using the exact intended backup artefact.
2. Restore into a scratch DB first where practical.
3. Inspect the restored scratch DB before touching `von_db`.
4. Only then consider a deliberate restore into the real target DB.

If the backup incident is tied to MongoDB Atlas availability, credentials, or
control-plane backup settings, treat those as separate operational concerns.
This runbook covers the local dump artefact restore path, not Atlas control
plane automation.

