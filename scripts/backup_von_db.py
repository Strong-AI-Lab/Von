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
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


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
    from cryptography.fernet import Fernet

    f = Fernet(key.encode("utf-8"))
    data = input_path.read_bytes()
    encrypted = f.encrypt(data)
    out_path = input_path.with_suffix(input_path.suffix + ".enc")
    out_path.write_bytes(encrypted)
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
                if p.is_dir():
                    import shutil

                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        p.unlink()
                    except FileNotFoundError:
                        pass

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
            if p.is_dir():
                import shutil

                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            total -= sizes.get(p, 0)


def _run_mongodump(*, mongo_uri: str, db_name: str, out_path: Path) -> None:
    cmd = [
        "mongodump",
        "--uri",
        mongo_uri,
        "--db",
        db_name,
        "--out",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


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
        return 0

    _ensure_dir(out_root)
    _ensure_dir(backup_root)

    # Write a prelude file at the backup root (helpful provenance when copying around).
    (backup_root / "prelude.json").write_text(
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

    print(f"[backup] Completed: {final_artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
