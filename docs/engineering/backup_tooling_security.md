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
3. Scheduled daily backups are disabled by default and require `VON_ENABLE_DAILY_BACKUP=1` on the designated backup host.
4. Scheduled daily backups use the same in-repo output safety check and skip if unsafe.
5. Backup artefacts in `backups/` are blocked by pre-commit guardrails.
6. Default backup root resolution prefers non-repository locations:
   - `VON_BACKUP_ROOT`
   - `W:\von_backups`
   - `%LOCALAPPDATA%\Von\backups`
   - repository `backups/` only as a last resort

## Required operator configuration

- Set `VON_BACKUP_ROOT` to a path outside the repository.
- Enable scheduled backups only on the intended backup host: `VON_ENABLE_DAILY_BACKUP=1`.
- Enable manual backup only when needed: `VON_ENABLE_BACKUP_ACTION=1`.
- Leave `VON_ALLOW_BACKUP_IN_REPO` unset (or `0`) unless you intentionally accept repository-path risk.

Example:

```powershell
$env:VON_BACKUP_ROOT = 'C:\von_backups'
$env:VON_ENABLE_DAILY_BACKUP = '1'
$env:VON_ENABLE_BACKUP_ACTION = '1'
.\run.ps1 backup -BackupTag manual
```

## Notes

- `scripts/backup_von_db.py` still supports dry-run and policy controls (retention, compression, encryption).
- For the canonical local operator restore path and restore-drill procedure, see `docs/engineering/local_backup_restore_runbook.md`.
- Encryption at rest remains optional and controlled by:
  - `VON_BACKUP_ENCRYPTION_ENABLED`
  - `VON_BACKUP_ENCRYPTION_KEY`

---

## Catalyst Cloud blob upload (off-machine encrypted archive)

Backups can be uploaded to Catalyst Cloud Object Storage (OpenStack Swift) or any
S3-compatible backend immediately after local creation.

### Mandatory encryption before upload

**Uploading a plaintext artefact to the blob store is refused by default.**
The enforcement logic lives in `scripts/backup_von_db.py:_upload_backup_to_blob()`.

- The upload is accepted only when the artefact ends in `.zip.enc` (Fernet-encrypted).
- To override this safeguard (local testing only), set `VON_BACKUP_BLOB_REQUIRE_ENCRYPTION=0`
  or pass `--no-require-encryption-for-blob` to the script directly.

### Key generation and storage

Generate a fresh Fernet key once and store it securely:

```powershell
pdm run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store the key in your `.env` (never commit it to git):

```
VON_BACKUP_ENCRYPTION_KEY=<key>
VON_BACKUP_ENCRYPTION_ENABLED=1
```

**Key rotation**: Fernet keys are symmetric.  Rotating a key requires re-encrypting
every existing `.zip.enc` artefact.  Keep the old key available until all old
artefacts are superseded or manually re-encrypted.

### Receipt metadata

Every backup receipt (`*.backup_receipt.json`) includes the following fields when blob
upload succeeds:

| Field | Description |
|---|---|
| `blob_backend` | Backend used (`swift`, `s3`, `local`) |
| `blob_key` | Object key in the container |
| `blob_uri` | Full URI (Swift URI or HTTPS if `VON_SWIFT_PUBLIC_BASE_URL` is set) |
| `blob_uploaded_at_utc` | ISO-8601 timestamp of the upload |
| `blob_sha256` | SHA-256 hex digest of the uploaded bytes |

The script performs a post-upload readback and verifies the SHA-256 before writing
the receipt.  If verification fails the script raises an error so the receipt is
never written with incorrect hash metadata.

### Environment variables

| Variable | Description |
|---|---|
| `VON_BACKUP_BLOB_BACKEND` | Blob backend for backup uploads (`swift`, `s3`, `local`). No upload when unset. |
| `VON_BACKUP_BLOB_KEY_PREFIX` | Key prefix for backup objects (default: `mongo_backups`). |
| `VON_BACKUP_BLOB_REQUIRE_ENCRYPTION` | `1` (default) = refuse unencrypted upload; `0` = allow plaintext. |

The standard Swift connection env vars (`VON_SWIFT_CONTAINER`, `OS_CLOUD`, etc.) are
also read — see `docs/engineering/catalyst_cloud_swift_setup.md` for the full list.

### Example configuration

```powershell
# .env additions for off-machine encrypted backups
VON_BACKUP_ENCRYPTION_ENABLED=1
VON_BACKUP_ENCRYPTION_KEY=<fernet-key>
VON_BACKUP_BLOB_BACKEND=swift
VON_BACKUP_BLOB_KEY_PREFIX=mongo_backups
VON_SWIFT_CONTAINER=von-mongo-backups
OS_CLOUD=catalystcloud
```

```powershell
# Run an encrypted backup with blob upload
.\run.ps1 backup -BackupTag auto-daily
```

### Retention policy

Deleting old artefacts from the blob store is an **explicit operator action**, not
an implicit side effect of running the backup script.  The local retention policy
(`VON_BACKUP_RETENTION_DAYS`, `VON_BACKUP_MAX_STORAGE_MB`) applies only to the
local backup root, not to the blob store.

To list remote artefacts:

```powershell
pdm run python -c "
import os; os.environ['VON_BLOB_STORE_BACKEND']='swift'
from src.backend.services.blob_store import get_blob_store_from_env
store = get_blob_store_from_env()
for k in store.list('mongo_backups'): print(k)
"
```
