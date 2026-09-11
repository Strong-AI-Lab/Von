"""Create a mongodump backup of the configured Von MongoDB database.

This script exists primarily for use by run.ps1 (auto-daily backups), but can
also be used manually.

It writes backups in the same directory structure produced by `mongodump`:
    <out-dir>/<db_name>_<timestamp>Z_<tag>/<db_name>/*.bson + *.metadata.json

Optional features are driven by environment variables:
- VON_BACKUP_RETENTION_DAYS (int; -1 = keep forever)
- VON_BACKUP_MAX_STORAGE_MB (int; -1 = unlimited)
- VON_BACKUP_COMPRESSION_ENABLED (truthy/falsey)
- VON_BACKUP_ENCRYPTION_ENABLED (truthy/falsey)
- VON_BACKUP_ENCRYPTION_KEY (Fernet key; required if encryption enabled)
- VON_BACKUP_MONGODUMP_TIMEOUT_SECONDS (int; default 21600 / 6 hours)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# Repo root — enables lazy import of src.backend when blob upload is requested.
_REPO_ROOT = Path(__file__).resolve().parents[1]


BACKUP_SUCCESS_RECEIPT_SCHEMA_VERSION = "backup_success_receipt.v1"
BACKUP_RECEIPT_SIDECAR_SUFFIX = ".backup_receipt.json"
DEFAULT_MONGODUMP_TIMEOUT_SECONDS = 6 * 60 * 60


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except Exception:
        return default


@dataclass(frozen=True)
class BackupPrelude:
    db_name: str
    tag: str
    timestamp_utc: str
    mongo_uri_redacted: str


@dataclass(frozen=True)
class BackupSuccessReceipt:
    schema_version: str
    completed_at_utc: str
    db_name: str
    tag: str
    out_root: str
    backup_root: str
    final_artifact_path: str
    artifact_kind: str
    compressed: bool
    encrypted: bool
    artifact_size_bytes: int
    collection_count: int
    # Optional blob-upload fields.  None when no blob upload was performed.
    blob_backend: str | None = None
    blob_key: str | None = None
    blob_uri: str | None = None
    blob_uploaded_at_utc: str | None = None
    blob_sha256: str | None = None


def _utc_timestamp_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")


def _utc_timestamp_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get_db_name() -> str:
    name = os.environ.get("VON_DB_NAME")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "von_db"


def _redact_mongo_uri(uri: str) -> str:
    if not uri:
        return uri
    if "@" not in uri:
        return uri
    # mongodb://user:pass@host/db -> mongodb://user:***@host/db
    try:
        prefix, rest = uri.split("://", 1)
        if "@" not in rest:
            return uri
        creds, host_part = rest.split("@", 1)
        user = creds.split(":", 1)[0] if ":" in creds else creds
        return f"{prefix}://{user}:***@{host_part}"
    except Exception:
        return uri


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _iter_backup_artifacts(out_root: Path) -> Iterable[Path]:
    if not out_root.exists():
        return []
    # Backups are either directories (<db>_YYYYMMDD_HHMMSSZ_tag) or compressed files.
    items = []
    for p in out_root.iterdir():
        if p.is_dir():
            items.append(p)
        elif p.is_file() and (p.name.endswith(".zip") or p.name.endswith(".zip.enc")):
            items.append(p)
    return items


def _artifact_mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _artifact_size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except FileNotFoundError:
                continue
    return total


def _backup_receipt_sidecar_path(final_artifact: Path) -> Path:
    return final_artifact.parent / f"{final_artifact.name}{BACKUP_RECEIPT_SIDECAR_SUFFIX}"


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_dir(path.parent)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except FileNotFoundError:
            pass


def _write_text_atomic(path: Path, content: str) -> None:
    _ensure_dir(path.parent)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except FileNotFoundError:
            pass


def _delete_backup_artifact(path: Path) -> None:
    sidecar_path = _backup_receipt_sidecar_path(path)
    if path.is_dir():
        import shutil

        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    try:
        sidecar_path.unlink()
    except FileNotFoundError:
        pass


def _artifact_kind(final_artifact: Path) -> str:
    if final_artifact.is_dir():
        return "directory"
    name = final_artifact.name.lower()
    if name.endswith(".zip.enc"):
        return "zip_encrypted"
    if name.endswith(".zip"):
        return "zip"
    return "file"


def _count_dump_bson_files(db_dir: Path) -> int:
    return sum(1 for item in db_dir.rglob("*.bson") if item.is_file())


def _compress_backup_dir_to_zip(backup_root: Path) -> Path:
    import zipfile

    zip_path = backup_root.with_suffix(backup_root.suffix + ".zip")
    # Ensure deterministic-ish paths: store paths relative to backup_root.
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in backup_root.rglob("*"):
            if not p.is_file():
                continue
            arcname = p.relative_to(backup_root)
            zf.write(p, arcname.as_posix())
    # Propagate timestamp to the zip file (so retention works on either form).
    ts = backup_root.stat().st_mtime
    os.utime(zip_path, (ts, ts))
    return zip_path


def _encrypt_file_fernet(input_path: Path, *, key: str) -> Path:
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from src.backend.utils.fernet_file import encrypt_file

    out_path = input_path.with_suffix(input_path.suffix + ".enc")
    encrypt_file(input_path, out_path, key=key)
    # Carry timestamp forward
    ts = input_path.stat().st_mtime
    os.utime(out_path, (ts, ts))
    return out_path


def _apply_retention_and_storage_limits(
    *,
    out_root: Path,
    retention_days: int,
    max_storage_mb: int,
    protect_paths: set[Path] | None = None,
) -> None:
    protect_paths = protect_paths or set()
    artifacts = list(_iter_backup_artifacts(out_root))
    if not artifacts:
        return

    now = datetime.now(timezone.utc)

    # Retention policy
    if retention_days >= 0:
        cutoff = now.timestamp() - (retention_days * 86400)
        for p in sorted(artifacts, key=lambda x: _artifact_mtime_utc(x)):
            if p in protect_paths:
                continue
            if _artifact_mtime_utc(p).timestamp() < cutoff:
                _delete_backup_artifact(p)

    # Max storage policy (delete oldest first)
    if max_storage_mb >= 0:
        max_bytes = max_storage_mb * 1024 * 1024
        artifacts = [p for p in _iter_backup_artifacts(out_root) if p.exists()]
        sizes = {p: _artifact_size_bytes(p) for p in artifacts}
        total = sum(sizes.values())
        if total <= max_bytes:
            return

        for p in sorted(artifacts, key=lambda x: _artifact_mtime_utc(x)):
            if total <= max_bytes:
                break
            if p in protect_paths:
                continue
            _delete_backup_artifact(p)
            total -= sizes.get(p, 0)


def _compute_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_blob_store_for_backup(backend: str):
    """Lazily import and return a BlobStore for the requested backend.

    Temporarily overrides VON_BLOB_STORE_BACKEND so the existing
    ``get_blob_store_from_env()`` factory picks the right implementation while
    still reading all Swift/S3 connection env vars from the environment.
    """
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from src.backend.services.blob_store import get_blob_store_from_env  # noqa: PLC0415

    orig = os.environ.get("VON_BLOB_STORE_BACKEND")
    try:
        os.environ["VON_BLOB_STORE_BACKEND"] = backend.strip().lower()
        return get_blob_store_from_env()
    finally:
        if orig is None:
            os.environ.pop("VON_BLOB_STORE_BACKEND", None)
        else:
            os.environ["VON_BLOB_STORE_BACKEND"] = orig


def _upload_backup_to_blob(
    *,
    final_artifact: Path,
    blob_store: Any,
    blob_key: str,
    require_encrypted: bool,
) -> tuple[str, str, str]:
    """Upload a backup artefact to the blob store with post-upload SHA-256 verification.

    Returns ``(blob_uri, blob_sha256_hex, uploaded_at_utc_iso)``.

    Raises ``RuntimeError`` when *require_encrypted* is True and the artefact
    is not a ``.zip.enc`` file (i.e. it is plaintext).
    """
    if require_encrypted and not final_artifact.name.lower().endswith(".zip.enc"):
        raise RuntimeError(
            f"Refusing to upload unencrypted backup artefact {final_artifact.name!r} to blob "
            "store.  Enable encryption (VON_BACKUP_ENCRYPTION_ENABLED=1 + "
            "VON_BACKUP_ENCRYPTION_KEY) before uploading, or pass "
            "--no-require-encryption-for-blob / set "
            "VON_BACKUP_BLOB_REQUIRE_ENCRYPTION=0 to allow plaintext upload."
        )

    local_sha256 = _compute_sha256(final_artifact)
    data = final_artifact.read_bytes()

    blob_ref = blob_store.put_bytes(
        blob_key,
        data,
        content_type="application/octet-stream",
        metadata={
            "von-backup-sha256": local_sha256,
            "von-backup-artifact-name": final_artifact.name,
        },
    )

    # Post-upload readback verification.
    readback = blob_store.get_bytes(blob_key)
    readback_sha256 = hashlib.sha256(readback).hexdigest()
    if readback_sha256 != local_sha256:
        raise RuntimeError(
            f"Blob upload verification failed for key {blob_key!r}: "
            f"local sha256={local_sha256!r} but readback sha256={readback_sha256!r}"
        )

    uploaded_at_utc = _utc_timestamp_iso()
    return blob_ref.uri, local_sha256, uploaded_at_utc


def _run_mongodump(*, mongo_uri: str, db_name: str, out_path: Path) -> None:
    timeout_seconds = _env_int(
        os.environ.get("VON_BACKUP_MONGODUMP_TIMEOUT_SECONDS"),
        default=DEFAULT_MONGODUMP_TIMEOUT_SECONDS,
    )
    if timeout_seconds < 1:
        timeout_seconds = DEFAULT_MONGODUMP_TIMEOUT_SECONDS
    with tempfile.TemporaryDirectory(prefix="von-mongodump-") as config_dir:
        config_root = Path(config_dir)
        os.chmod(config_root, 0o700)
        config_path = config_root / "config.yml"
        config_path.write_text(
            f"uri: {json.dumps(mongo_uri)}\n",
            encoding="utf-8",
        )
        os.chmod(config_path, 0o600)
        cmd = [
            "mongodump",
            "--config",
            str(config_path),
            "--db",
            db_name,
            "--out",
            str(out_path),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"mongodump exceeded the configured {timeout_seconds}s timeout"
            ) from None
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"mongodump failed with exit code {exc.returncode}"
            ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backup Von MongoDB database via mongodump"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the backup (default: dry-run).",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=os.environ.get("VON_BACKUP_ROOT") or "backups",
        help="Output directory root for backups (default: VON_BACKUP_ROOT or ./backups).",
    )
    parser.add_argument(
        "--tag",
        type=str,
        default="manual",
        help="Tag suffix for the backup directory name (default: manual).",
    )
    parser.add_argument(
        "--launcher-receipt-path",
        type=str,
        default="",
        help="Optional authoritative launcher receipt path to update on validated success.",
    )
    parser.add_argument(
        "--legacy-sentinel-path",
        type=str,
        default="",
        help="Optional legacy ISO8601 UTC sentinel path to mirror from the success receipt.",
    )
    parser.add_argument(
        "--blob-backend",
        type=str,
        default="",
        help=(
            "Blob backend to upload the backup artefact to after local creation "
            "(swift, s3, local).  Defaults to VON_BACKUP_BLOB_BACKEND env var; "
            "if neither is set, no blob upload is performed."
        ),
    )
    parser.add_argument(
        "--blob-key-prefix",
        type=str,
        default="",
        help=(
            "Key prefix for the blob object (default: mongo_backups).  "
            "Overrides VON_BACKUP_BLOB_KEY_PREFIX env var."
        ),
    )
    parser.add_argument(
        "--no-require-encryption-for-blob",
        action="store_true",
        help=(
            "Allow uploading an unencrypted artefact to the blob store.  "
            "By default (and when VON_BACKUP_BLOB_REQUIRE_ENCRYPTION=1) "
            "the upload is refused unless the artefact ends in .zip.enc."
        ),
    )

    args = parser.parse_args(argv)

    db_name = _get_db_name()
    tag = (args.tag or "manual").strip() or "manual"

    out_root = Path(args.out_dir).expanduser().resolve()
    backup_dir_name = f"{db_name}_{_utc_timestamp_compact()}_{tag}"
    backup_root = out_root / backup_dir_name

    mongo_uri = os.environ.get("MONGO_URI") or "mongodb://localhost:27017/"

    retention_days = _env_int(os.environ.get("VON_BACKUP_RETENTION_DAYS"), default=-1)
    max_storage_mb = _env_int(os.environ.get("VON_BACKUP_MAX_STORAGE_MB"), default=-1)
    compression_enabled = _env_truthy(os.environ.get("VON_BACKUP_COMPRESSION_ENABLED"))
    encryption_enabled = _env_truthy(os.environ.get("VON_BACKUP_ENCRYPTION_ENABLED"))
    encryption_key = (os.environ.get("VON_BACKUP_ENCRYPTION_KEY") or "").strip()

    prelude = BackupPrelude(
        db_name=db_name,
        tag=tag,
        timestamp_utc=_utc_timestamp_iso(),
        mongo_uri_redacted=_redact_mongo_uri(mongo_uri),
    )

    if not args.apply:
        print("[backup] DRY-RUN")
        print(f"[backup] Would create: {backup_root}")
        print(f"[backup] Would dump DB: {db_name}")
        print(f"[backup] Using MONGO_URI: {prelude.mongo_uri_redacted}")
        print(
            "[backup] Policy: "
            f"retention_days={retention_days} max_storage_mb={max_storage_mb} "
            f"compression={compression_enabled} encryption={encryption_enabled}"
        )
        if encryption_enabled:
            if not encryption_key:
                print(
                    "[backup] WARN: encryption enabled but VON_BACKUP_ENCRYPTION_KEY is empty"
                )
            else:
                print(f"[backup] Encryption key looks set (len={len(encryption_key)}).")
        blob_backend_dry = (
            (args.blob_backend or "").strip()
            or os.environ.get("VON_BACKUP_BLOB_BACKEND", "").strip()
        ).lower()
        if blob_backend_dry:
            print(f"[backup] Blob upload: backend={blob_backend_dry!r}")
        else:
            print("[backup] Blob upload: disabled (set --blob-backend or VON_BACKUP_BLOB_BACKEND)")
        return 0

    _ensure_dir(out_root)
    _ensure_dir(backup_root)

    # Write a prelude file at the backup root (helpful provenance when copying around).
    # prelude.json is reserved by MongoDB Database Tools for server metadata.
    (backup_root / "von_backup_prelude.json").write_text(
        json.dumps(asdict(prelude), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"[backup] Running mongodump into: {backup_root}")
    print(
        "[backup] Note: mongodump progress is coarse; counters may stay flat until a collection completes."
    )
    _run_mongodump(mongo_uri=mongo_uri, db_name=db_name, out_path=backup_root)

    # Basic sanity check: ensure the expected database folder exists.
    db_dir = backup_root / db_name
    if not db_dir.exists():
        raise RuntimeError(
            f"Backup completed but expected folder not found: {db_dir} (mongodump output missing?)"
        )
    collection_count = _count_dump_bson_files(db_dir)

    final_artifact: Path = backup_root

    if compression_enabled:
        print(f"[backup] Compressing to zip: {backup_root}.zip")
        zip_path = _compress_backup_dir_to_zip(backup_root)
        final_artifact = zip_path
        # Remove uncompressed directory once zipped (to avoid doubling storage).
        import shutil

        shutil.rmtree(backup_root, ignore_errors=True)

    if encryption_enabled:
        if not encryption_key:
            raise RuntimeError(
                "VON_BACKUP_ENCRYPTION_ENABLED is set but VON_BACKUP_ENCRYPTION_KEY is empty."
            )
        # Fernet keys must be urlsafe-base64 32-byte keys.
        try:
            encrypted_path = _encrypt_file_fernet(final_artifact, key=encryption_key)
        except Exception as e:
            raise RuntimeError(
                "Backup encryption failed. Ensure VON_BACKUP_ENCRYPTION_KEY is a valid Fernet key "
                "(e.g. from cryptography.fernet.Fernet.generate_key())."
            ) from e
        # Remove the plaintext zip once encrypted.
        try:
            final_artifact.unlink()
        except FileNotFoundError:
            pass
        final_artifact = encrypted_path

    # Enforce retention and storage limits across the output root.
    _apply_retention_and_storage_limits(
        out_root=out_root,
        retention_days=retention_days,
        max_storage_mb=max_storage_mb,
        protect_paths={final_artifact} if final_artifact.exists() else set(),
    )

    artifact_size_bytes = _artifact_size_bytes(final_artifact)

    # -----------------------------------------------------------------
    # Optional blob upload (Catalyst Cloud Swift / S3 / local).
    # Must happen before receipt construction so blob metadata ends up
    # in the single sidecar write.
    # -----------------------------------------------------------------
    blob_backend_arg = (
        (args.blob_backend or "").strip()
        or os.environ.get("VON_BACKUP_BLOB_BACKEND", "").strip()
    ).lower()

    effective_blob_backend: str | None = None
    blob_key: str | None = None
    blob_uri: str | None = None
    blob_sha256: str | None = None
    blob_uploaded_at_utc: str | None = None

    if blob_backend_arg:
        blob_key_prefix = (
            (args.blob_key_prefix or "").strip()
            or os.environ.get("VON_BACKUP_BLOB_KEY_PREFIX", "").strip()
            or "mongo_backups"
        ).strip("/")
        require_enc_env = os.environ.get("VON_BACKUP_BLOB_REQUIRE_ENCRYPTION", "1").strip()
        require_encrypted = (
            not args.no_require_encryption_for_blob
            and _env_truthy(require_enc_env if require_enc_env else "1")
        )
        candidate_key = f"{blob_key_prefix}/{final_artifact.name}"
        print(
            f"[backup] Uploading to blob store ({blob_backend_arg}): key={candidate_key!r}"
        )
        blob_store = _resolve_blob_store_for_backup(blob_backend_arg)
        blob_uri_val, blob_sha256_val, blob_uploaded_at_utc_val = _upload_backup_to_blob(
            final_artifact=final_artifact,
            blob_store=blob_store,
            blob_key=candidate_key,
            require_encrypted=require_encrypted,
        )
        effective_blob_backend = blob_backend_arg
        blob_key = candidate_key
        blob_uri = blob_uri_val
        blob_sha256 = blob_sha256_val
        blob_uploaded_at_utc = blob_uploaded_at_utc_val
        print(
            f"[backup] Blob upload verified: uri={blob_uri!r} "
            f"sha256={blob_sha256[:16]}..."
        )

    receipt = BackupSuccessReceipt(
        schema_version=BACKUP_SUCCESS_RECEIPT_SCHEMA_VERSION,
        completed_at_utc=_utc_timestamp_iso(),
        db_name=db_name,
        tag=tag,
        out_root=str(out_root),
        backup_root=str(backup_root),
        final_artifact_path=str(final_artifact),
        artifact_kind=_artifact_kind(final_artifact),
        compressed=bool(compression_enabled),
        encrypted=bool(encryption_enabled),
        artifact_size_bytes=int(artifact_size_bytes),
        collection_count=int(collection_count),
        blob_backend=effective_blob_backend,
        blob_key=blob_key,
        blob_uri=blob_uri,
        blob_uploaded_at_utc=blob_uploaded_at_utc,
        blob_sha256=blob_sha256,
    )
    receipt_payload = asdict(receipt)
    sidecar_path = _backup_receipt_sidecar_path(final_artifact)
    _write_json_atomic(sidecar_path, receipt_payload)
    print(f"[backup] Receipt sidecar: {sidecar_path}")

    launcher_receipt_path = (args.launcher_receipt_path or "").strip()
    if launcher_receipt_path:
        resolved_launcher_receipt = Path(launcher_receipt_path).expanduser().resolve()
        _write_json_atomic(resolved_launcher_receipt, receipt_payload)
        print(f"[backup] Launcher receipt: {resolved_launcher_receipt}")

    legacy_sentinel_path = (args.legacy_sentinel_path or "").strip()
    if legacy_sentinel_path:
        try:
            resolved_legacy_sentinel = Path(legacy_sentinel_path).expanduser().resolve()
            _write_text_atomic(resolved_legacy_sentinel, receipt.completed_at_utc + "\n")
            print(f"[backup] Legacy sentinel: {resolved_legacy_sentinel}")
        except Exception as exc:
            print(
                "[backup] WARN: could not update legacy sentinel "
                f"{legacy_sentinel_path}: {exc}"
            )

    print(
        "[backup] Summary: "
        f"db={db_name} source={prelude.mongo_uri_redacted} "
        f"collections={collection_count} artifact_kind={receipt.artifact_kind} "
        f"artifact_size_bytes={artifact_size_bytes}"
    )
    print(f"[backup] Completed: {final_artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
