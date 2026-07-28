"""Restore a Von MongoDB backup artefact into a target database.

This is the canonical local operator restore surface for dump artefacts created
by `scripts/backup_von_db.py`. It supports:

- raw dump directories
- `.zip` backup artefacts
- `.zip.enc` backup artefacts (when a Fernet key is available)
- launcher or artefact receipt JSON files that point at the final artefact
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Repo root — enables lazy import of src.backend when blob download is requested.
_REPO_ROOT = Path(__file__).resolve().parents[1]

try:
    from scripts.backup_von_db import BACKUP_RECEIPT_SIDECAR_SUFFIX
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    from backup_von_db import BACKUP_RECEIPT_SIDECAR_SUFFIX


DEFAULT_MONGORESTORE_TIMEOUT_SECONDS = 6 * 60 * 60


def _env_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except Exception:
        return default


def _redact_mongo_uri(uri: str) -> str:
    if not uri or "@" not in uri:
        return uri
    try:
        prefix, rest = uri.split("://", 1)
        creds, host_part = rest.split("@", 1)
        user = creds.split(":", 1)[0] if ":" in creds else creds
        return f"{prefix}://{user}:***@{host_part}"
    except Exception:
        return uri


@dataclass(frozen=True)
class RestoreMaterialisedBackup:
    original_path: Path
    artifact_path: Path
    materialised_root: Path
    source_db_name: str
    collection_count: int
    cleanup_dir: Path | None
    receipt_payload: dict[str, Any] | None


def _load_receipt_if_present(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    if path.suffix.lower() != ".json" and not path.name.endswith(
        BACKUP_RECEIPT_SIDECAR_SUFFIX
    ):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict) and payload.get("final_artifact_path"):
        return payload
    return None


def _decrypt_encrypted_zip(
    encrypted_path: Path, *, fernet_key: str, output_zip_path: Path
) -> Path:
    from cryptography.fernet import Fernet

    f = Fernet(fernet_key.encode("utf-8"))
    encrypted_bytes = encrypted_path.read_bytes()
    output_zip_path.write_bytes(f.decrypt(encrypted_bytes))
    return output_zip_path


def _extract_zip_to_temp(zip_path: Path, *, temp_root: Path) -> Path:
    extract_dir = temp_root / "extracted_backup"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, mode="r") as zf:
        zf.extractall(extract_dir)
    return extract_dir


def _discover_source_db_name(materialised_root: Path, expected: str = "") -> str:
    if expected:
        expected_dir = materialised_root / expected
        if expected_dir.is_dir():
            return expected

    candidates: list[str] = []
    for child in materialised_root.iterdir():
        if not child.is_dir():
            continue
        has_dump_files = any(
            item.is_file()
            and (item.name.endswith(".bson") or item.name.endswith(".metadata.json"))
            for item in child.iterdir()
        )
        if has_dump_files:
            candidates.append(child.name)

    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError(
            f"Backup root {materialised_root} does not contain a recognisable dumped database directory."
        )
    raise RuntimeError(
        f"Backup root {materialised_root} contains multiple candidate databases: {', '.join(sorted(candidates))}."
    )


def _count_collection_files(db_dir: Path) -> int:
    return sum(1 for item in db_dir.iterdir() if item.is_file() and item.name.endswith(".bson"))


def _materialise_backup(
    backup_path: Path, *, fernet_key: str
) -> RestoreMaterialisedBackup:
    original_path = backup_path.expanduser().resolve()
    if not original_path.exists():
        raise FileNotFoundError(f"Backup path does not exist: {original_path}")

    receipt_payload = _load_receipt_if_present(original_path)
    artifact_path = original_path
    expected_source_db_name = ""
    if receipt_payload:
        artifact_path = Path(str(receipt_payload["final_artifact_path"])).expanduser().resolve()
        expected_source_db_name = str(receipt_payload.get("db_name") or "").strip()
        if not artifact_path.exists():
            raise FileNotFoundError(
                f"Receipt points at a missing backup artefact: {artifact_path}"
            )

    cleanup_dir: Path | None = None
    materialised_root = artifact_path
    if artifact_path.is_file():
        cleanup_dir = Path(tempfile.mkdtemp(prefix="von_restore_"))
        if artifact_path.name.lower().endswith(".zip.enc"):
            if not fernet_key:
                raise RuntimeError(
                    "Encrypted backup selected but no Fernet key was provided."
                )
            decrypted_zip = cleanup_dir / artifact_path.name.removesuffix(".enc")
            materialised_root = _extract_zip_to_temp(
                _decrypt_encrypted_zip(
                    artifact_path,
                    fernet_key=fernet_key,
                    output_zip_path=decrypted_zip,
                ),
                temp_root=cleanup_dir,
            )
        elif artifact_path.name.lower().endswith(".zip"):
            materialised_root = _extract_zip_to_temp(artifact_path, temp_root=cleanup_dir)
        else:
            raise RuntimeError(
                f"Unsupported backup artefact type: {artifact_path.name}. Expected a directory, .zip, .zip.enc, or a receipt JSON."
            )
    elif not artifact_path.is_dir():
        raise RuntimeError(
            f"Unsupported backup artefact path: {artifact_path}. Expected a file or directory."
        )

    source_db_name = _discover_source_db_name(
        materialised_root, expected=expected_source_db_name
    )
    db_dir = materialised_root / source_db_name
    collection_count = _count_collection_files(db_dir)
    if collection_count < 1:
        raise RuntimeError(
            f"Backup database directory {db_dir} contains no collection .bson files."
        )

    return RestoreMaterialisedBackup(
        original_path=original_path,
        artifact_path=artifact_path,
        materialised_root=materialised_root,
        source_db_name=source_db_name,
        collection_count=collection_count,
        cleanup_dir=cleanup_dir,
        receipt_payload=receipt_payload,
    )


def _download_artifact_from_blob(
    *,
    receipt_payload: dict[str, Any],
    blob_store: Any,
    tmp_dir: Path,
) -> Path:
    """Download the backup artefact referenced in *receipt_payload* from *blob_store*.

    Verifies the downloaded bytes against ``blob_sha256`` from the receipt.
    Returns the path of the downloaded file under *tmp_dir*.
    """
    blob_key: str = str(receipt_payload.get("blob_key") or "").strip()
    if not blob_key:
        raise RuntimeError(
            "Receipt does not contain a blob_key; cannot download from blob store."
        )
    expected_sha256: str = str(receipt_payload.get("blob_sha256") or "").strip()

    artifact_name = blob_key.rsplit("/", 1)[-1] or "blob_artifact"
    dest = tmp_dir / artifact_name

    print(f"[restore] Downloading from blob store: key={blob_key!r}")
    data = blob_store.get_bytes(blob_key)

    if expected_sha256:
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"Blob download verification failed for key {blob_key!r}: "
                f"expected sha256={expected_sha256!r} actual={actual_sha256!r}"
            )
        print(f"[restore] SHA-256 verified: {actual_sha256[:16]}...")
    else:
        print("[restore] WARN: receipt has no blob_sha256; skipping hash verification")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"[restore] Downloaded artefact: {dest}")
    return dest


def _resolve_blob_store_for_restore(backend: str):
    """Lazily import and return a BlobStore for the requested backend."""
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


def _run_mongorestore(
    *,
    mongo_uri: str,
    dump_root: Path,
    source_db_name: str,
    target_db_name: str,
    drop_target: bool,
) -> None:
    timeout_seconds = _env_int(
        os.environ.get("VON_BACKUP_MONGORESTORE_TIMEOUT_SECONDS"),
        default=DEFAULT_MONGORESTORE_TIMEOUT_SECONDS,
    )
    if timeout_seconds < 1:
        timeout_seconds = DEFAULT_MONGORESTORE_TIMEOUT_SECONDS
    with tempfile.TemporaryDirectory(prefix="von-mongorestore-") as config_dir:
        config_root = Path(config_dir)
        os.chmod(config_root, 0o700)
        config_path = config_root / "config.yml"
        config_path.write_text(
            f"uri: {json.dumps(mongo_uri)}\n",
            encoding="utf-8",
        )
        os.chmod(config_path, 0o600)
        cmd = [
            "mongorestore",
            "--config",
            str(config_path),
            "--dir",
            str(dump_root),
            "--nsFrom",
            f"{source_db_name}.*",
            "--nsTo",
            f"{target_db_name}.*",
            "--stopOnError",
        ]
        if drop_target:
            cmd.append("--drop")
        try:
            subprocess.run(cmd, check=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"mongorestore exceeded the configured {timeout_seconds}s timeout"
            ) from None
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"mongorestore failed with exit code {exc.returncode}"
            ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore a Von MongoDB dump artefact into a target database."
    )
    parser.add_argument(
        "--backup-path",
        required=True,
        help="Path to a backup directory, .zip, .zip.enc, or receipt JSON.",
    )
    parser.add_argument(
        "--target-db-name",
        default="",
        help="Target database name. Defaults to <source_db_name>_restore_probe.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the restore. Default is dry-run.",
    )
    parser.add_argument(
        "--drop-target",
        action="store_true",
        help="Pass --drop to mongorestore before restoring collections.",
    )
    parser.add_argument(
        "--fernet-key",
        default="",
        help="Optional Fernet key for encrypted .zip.enc backups. Defaults to VON_BACKUP_ENCRYPTION_KEY.",
    )
    parser.add_argument(
        "--from-blob",
        action="store_true",
        help=(
            "Download the backup artefact from the blob store before restoring.  "
            "The receipt at --backup-path must contain blob_key and blob_backend fields.  "
            "Also activates automatically when the receipt has a blob_key and the local "
            "artefact is missing."
        ),
    )
    parser.add_argument(
        "--blob-backend",
        type=str,
        default="",
        help=(
            "Blob backend to download from (swift, s3, local).  "
            "Defaults to the blob_backend field in the receipt, then "
            "VON_BACKUP_BLOB_BACKEND env var."
        ),
    )

    args = parser.parse_args(argv)

    mongo_uri = os.environ.get("MONGO_URI") or "mongodb://localhost:27017/"
    redacted_uri = _redact_mongo_uri(mongo_uri)
    fernet_key = (args.fernet_key or os.environ.get("VON_BACKUP_ENCRYPTION_KEY") or "").strip()

    # -----------------------------------------------------------------
    # Optional pre-materialise blob download.
    # Activates when --from-blob is set OR when the receipt has a blob_key
    # and the local final_artifact_path is missing.
    # -----------------------------------------------------------------
    backup_path_str = args.backup_path
    blob_download_tmp: str | None = None  # temp dir to clean up

    initial_receipt = _load_receipt_if_present(Path(backup_path_str))
    needs_blob_download = args.from_blob or (
        initial_receipt
        and initial_receipt.get("blob_key")
        and not Path(str(initial_receipt.get("final_artifact_path", ""))).exists()
    )

    if needs_blob_download:
        if initial_receipt is None:
            raise RuntimeError(
                "--from-blob requires --backup-path to be a receipt JSON with blob metadata."
            )
        blob_backend = (
            (args.blob_backend or "").strip()
            or str(initial_receipt.get("blob_backend") or "").strip()
            or os.environ.get("VON_BACKUP_BLOB_BACKEND", "").strip()
        ).lower()
        if not blob_backend:
            raise RuntimeError(
                "Blob backend not specified.  Pass --blob-backend, set blob_backend in the "
                "receipt, or set VON_BACKUP_BLOB_BACKEND."
            )
        blob_download_tmp = tempfile.mkdtemp(prefix="von_restore_blob_")
        blob_store = _resolve_blob_store_for_restore(blob_backend)
        downloaded_artifact = _download_artifact_from_blob(
            receipt_payload=initial_receipt,
            blob_store=blob_store,
            tmp_dir=Path(blob_download_tmp),
        )
        # Restore from the downloaded artefact directly (bypass the receipt path).
        backup_path_str = str(downloaded_artifact)

    materialised: RestoreMaterialisedBackup | None = None
    try:
        materialised = _materialise_backup(
            Path(backup_path_str), fernet_key=fernet_key
        )
        target_db_name = (
            (args.target_db_name or "").strip()
            or f"{materialised.source_db_name}_restore_probe"
        )

        print("[restore] " + ("APPLY" if args.apply else "DRY-RUN"))
        print(f"[restore] Backup input: {materialised.original_path}")
        print(f"[restore] Final artefact: {materialised.artifact_path}")
        print(f"[restore] Materialised dump root: {materialised.materialised_root}")
        print(f"[restore] Source DB: {materialised.source_db_name}")
        print(f"[restore] Target DB: {target_db_name}")
        print(f"[restore] Using MONGO_URI: {redacted_uri}")
        print(
            f"[restore] Dump summary: collections={materialised.collection_count} drop_target={bool(args.drop_target)}"
        )

        if not args.apply:
            return 0

        _run_mongorestore(
            mongo_uri=mongo_uri,
            dump_root=materialised.materialised_root,
            source_db_name=materialised.source_db_name,
            target_db_name=target_db_name,
            drop_target=bool(args.drop_target),
        )
        print(
            f"[restore] Completed: target_db={target_db_name} source_db={materialised.source_db_name}"
        )
        return 0
    finally:
        if materialised and materialised.cleanup_dir:
            shutil.rmtree(materialised.cleanup_dir, ignore_errors=True)
        if blob_download_tmp:
            shutil.rmtree(blob_download_tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
