"""Tests for the blob-upload extension of ``scripts/backup_von_db.py`` and the
blob-download extension of ``scripts/restore_von_db.py``.

These tests use ``LocalBlobStore`` backed by a temporary directory so no real
cloud credentials are required.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.backup_von_db import (
    BackupSuccessReceipt,
    _compute_sha256,
    _upload_backup_to_blob,
)
from scripts.restore_von_db import _download_artifact_from_blob
from src.backend.services.blob_store import LocalBlobStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_zip(tmp_path: Path, name: str = "backup.zip") -> Path:
    """Create a small fake .zip artefact."""
    path = tmp_path / name
    path.write_bytes(b"PK\x03\x04" + b"x" * 128)
    return path


def _make_fake_enc_zip(tmp_path: Path, name: str = "backup.zip.enc") -> Path:
    """Create a small fake .zip.enc artefact."""
    path = tmp_path / name
    path.write_bytes(b"gAAAAA" + b"x" * 128)
    return path


def _local_store(tmp_path: Path, subdir: str = "blob_root") -> LocalBlobStore:
    root = tmp_path / subdir
    root.mkdir(parents=True, exist_ok=True)
    return LocalBlobStore(root)


# ---------------------------------------------------------------------------
# _compute_sha256
# ---------------------------------------------------------------------------


class TestComputeSha256:
    def test_matches_stdlib_digest(self, tmp_path: Path) -> None:
        data = b"hello, Von backup"
        f = tmp_path / "test.bin"
        f.write_bytes(data)
        assert _compute_sha256(f) == hashlib.sha256(data).hexdigest()

    def test_larger_file_chunked_correctly(self, tmp_path: Path) -> None:
        # Larger than the 65 536-byte read chunk so the chunking path executes.
        data = b"a" * (3 * 65536 + 1)
        f = tmp_path / "large.bin"
        f.write_bytes(data)
        assert _compute_sha256(f) == hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# _upload_backup_to_blob
# ---------------------------------------------------------------------------


class TestUploadBackupToBlob:
    def test_refuses_plaintext_zip_when_require_encrypted(self, tmp_path: Path) -> None:
        """A .zip artefact must be rejected when require_encrypted=True."""
        z = _make_fake_zip(tmp_path)
        store = _local_store(tmp_path)

        with pytest.raises(RuntimeError, match="Refusing to upload unencrypted"):
            _upload_backup_to_blob(
                final_artifact=z,
                blob_store=store,
                blob_key="mongo_backups/backup.zip",
                require_encrypted=True,
            )

    def test_allows_plaintext_zip_when_require_encrypted_false(self, tmp_path: Path) -> None:
        """A .zip artefact should be accepted when require_encrypted=False."""
        z = _make_fake_zip(tmp_path)
        store = _local_store(tmp_path)

        blob_uri, blob_sha256, uploaded_at = _upload_backup_to_blob(
            final_artifact=z,
            blob_store=store,
            blob_key="mongo_backups/backup.zip",
            require_encrypted=False,
        )

        expected_sha256 = hashlib.sha256(z.read_bytes()).hexdigest()
        assert blob_sha256 == expected_sha256
        assert uploaded_at  # non-empty ISO timestamp
        assert store.exists("mongo_backups/backup.zip")

    def test_encrypted_zip_enc_is_uploaded_and_sha256_matches(self, tmp_path: Path) -> None:
        """A .zip.enc artefact uploads with require_encrypted=True and verifies correctly."""
        z = _make_fake_enc_zip(tmp_path)
        store = _local_store(tmp_path)

        blob_uri, blob_sha256, uploaded_at = _upload_backup_to_blob(
            final_artifact=z,
            blob_store=store,
            blob_key="mongo_backups/backup.zip.enc",
            require_encrypted=True,
        )

        expected_sha256 = hashlib.sha256(z.read_bytes()).hexdigest()
        assert blob_sha256 == expected_sha256
        assert store.exists("mongo_backups/backup.zip.enc")

    def test_readback_verifies_stored_bytes_integrity(self, tmp_path: Path) -> None:
        """After upload the blob store must contain bytes matching the local SHA-256."""
        z = _make_fake_enc_zip(tmp_path)
        store = _local_store(tmp_path)

        _, blob_sha256, _ = _upload_backup_to_blob(
            final_artifact=z,
            blob_store=store,
            blob_key="mongo_backups/backup.zip.enc",
            require_encrypted=True,
        )

        readback = store.get_bytes("mongo_backups/backup.zip.enc")
        assert hashlib.sha256(readback).hexdigest() == blob_sha256

    def test_receipt_dataclass_accepts_blob_fields(self, tmp_path: Path) -> None:
        """BackupSuccessReceipt can be constructed with and without blob fields."""
        base_kwargs: dict[str, Any] = dict(
            schema_version="backup_success_receipt.v1",
            completed_at_utc="2026-04-23T12:00:00Z",
            db_name="von_db",
            tag="test",
            out_root=str(tmp_path),
            backup_root=str(tmp_path / "backup"),
            final_artifact_path=str(tmp_path / "backup.zip.enc"),
            artifact_kind="zip_encrypted",
            compressed=True,
            encrypted=True,
            artifact_size_bytes=256,
            collection_count=3,
        )

        # No blob fields → all None
        receipt_no_blob = BackupSuccessReceipt(**base_kwargs)
        assert receipt_no_blob.blob_backend is None
        assert receipt_no_blob.blob_key is None
        assert receipt_no_blob.blob_uri is None
        assert receipt_no_blob.blob_uploaded_at_utc is None
        assert receipt_no_blob.blob_sha256 is None

        # With blob fields populated
        receipt_with_blob = BackupSuccessReceipt(
            **base_kwargs,
            blob_backend="swift",
            blob_key="mongo_backups/backup.zip.enc",
            blob_uri="swift://container/mongo_backups/backup.zip.enc",
            blob_uploaded_at_utc="2026-04-23T12:01:00Z",
            blob_sha256="abc123",
        )
        assert receipt_with_blob.blob_backend == "swift"
        assert receipt_with_blob.blob_key == "mongo_backups/backup.zip.enc"
        assert receipt_with_blob.blob_sha256 == "abc123"

    def test_receipt_serialises_blob_fields_to_json(self, tmp_path: Path) -> None:
        """asdict() round-trip preserves blob fields in the JSON sidecar."""
        from dataclasses import asdict

        receipt = BackupSuccessReceipt(
            schema_version="backup_success_receipt.v1",
            completed_at_utc="2026-04-23T12:00:00Z",
            db_name="von_db",
            tag="test",
            out_root=str(tmp_path),
            backup_root=str(tmp_path / "backup"),
            final_artifact_path=str(tmp_path / "backup.zip.enc"),
            artifact_kind="zip_encrypted",
            compressed=True,
            encrypted=True,
            artifact_size_bytes=256,
            collection_count=3,
            blob_backend="local",
            blob_key="mongo_backups/backup.zip.enc",
            blob_uri="file:///tmp/blob/mongo_backups/backup.zip.enc",
            blob_uploaded_at_utc="2026-04-23T12:01:00Z",
            blob_sha256="deadbeef",
        )

        payload = json.dumps(asdict(receipt))
        loaded = json.loads(payload)
        assert loaded["blob_backend"] == "local"
        assert loaded["blob_key"] == "mongo_backups/backup.zip.enc"
        assert loaded["blob_sha256"] == "deadbeef"


# ---------------------------------------------------------------------------
# _download_artifact_from_blob
# ---------------------------------------------------------------------------


class TestDownloadArtifactFromBlob:
    def _seed_store(
        self,
        store: LocalBlobStore,
        blob_key: str,
        data: bytes,
    ) -> str:
        """Put bytes into the store and return the SHA-256 hex digest."""
        store.put_bytes(blob_key, data, content_type="application/octet-stream")
        return hashlib.sha256(data).hexdigest()

    def test_downloads_artifact_and_verifies_sha256(self, tmp_path: Path) -> None:
        data = b"gAAAAA" + b"encrypted_backup_contents" * 10
        blob_key = "mongo_backups/my_backup.zip.enc"
        store = _local_store(tmp_path)
        sha256 = self._seed_store(store, blob_key, data)

        receipt: dict[str, Any] = {
            "blob_key": blob_key,
            "blob_sha256": sha256,
        }

        dest = _download_artifact_from_blob(
            receipt_payload=receipt,
            blob_store=store,
            tmp_dir=tmp_path / "download_dir",
        )

        assert dest.exists()
        assert dest.read_bytes() == data
        assert dest.name == "my_backup.zip.enc"

    def test_raises_on_sha256_mismatch(self, tmp_path: Path) -> None:
        data = b"correct_data" * 20
        blob_key = "mongo_backups/tampered.zip.enc"
        store = _local_store(tmp_path)
        self._seed_store(store, blob_key, data)

        receipt: dict[str, Any] = {
            "blob_key": blob_key,
            "blob_sha256": "0" * 64,  # deliberately wrong
        }

        with pytest.raises(RuntimeError, match="Blob download verification failed"):
            _download_artifact_from_blob(
                receipt_payload=receipt,
                blob_store=store,
                tmp_dir=tmp_path / "download_dir",
            )

    def test_raises_when_blob_key_missing_from_receipt(self, tmp_path: Path) -> None:
        store = _local_store(tmp_path)
        receipt: dict[str, Any] = {"blob_sha256": "abc"}

        with pytest.raises(RuntimeError, match="does not contain a blob_key"):
            _download_artifact_from_blob(
                receipt_payload=receipt,
                blob_store=store,
                tmp_dir=tmp_path / "download_dir",
            )

    def test_skips_hash_check_when_receipt_has_no_sha256(self, tmp_path: Path) -> None:
        """When receipt omits blob_sha256, download proceeds without verification."""
        data = b"unverified_backup"
        blob_key = "mongo_backups/nohash.zip.enc"
        store = _local_store(tmp_path)
        store.put_bytes(blob_key, data, content_type="application/octet-stream")

        receipt: dict[str, Any] = {"blob_key": blob_key}

        dest = _download_artifact_from_blob(
            receipt_payload=receipt,
            blob_store=store,
            tmp_dir=tmp_path / "download_dir",
        )

        assert dest.read_bytes() == data
