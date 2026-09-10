import hashlib
import json

import pytest
from cryptography.fernet import Fernet

from scripts import backup_task_project_files as backup


def test_restore_uses_canonical_file_identity_and_rejects_corrupt_content(
    tmp_path, monkeypatch
):
    from src.backend.services import computer_file_copy_service as files
    from src.backend.services import concept_service

    monkeypatch.setenv("VON_DB_NAME", "test_restored_tasks")
    monkeypatch.setenv("VON_BLOB_STORE_BACKEND", "s3")
    cipher = Fernet(Fernet.generate_key())
    data = b"retained original attachment\x00\xff"
    digest = hashlib.sha256(data).hexdigest()
    entry = {
        "file_copy_concept_id": "#V#file",
        "sha256": digest,
        "size_bytes": len(data),
        "content_type": "application/octet-stream",
        "original_filename": "original.bin",
    }
    manifest = {
        "source_database": "von_db",
        "complete": True,
        "project_concept_ids": ["#V#project"],
        "task_count": 1,
        "files": [entry],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    encrypted = tmp_path / f"{digest}.bin.enc"
    encrypted.write_bytes(cipher.encrypt(data))
    writes = []

    def update(identifier, fields):
        writes.append((identifier, fields))

    monkeypatch.setattr(concept_service, "update_concept", update)

    def fetch(**kwargs):
        from src.backend.services.blob_store import get_blob_store_from_env

        assert kwargs["file_copy_concept_id"] == "#V#file"
        data = get_blob_store_from_env().get_bytes(writes[-1][1]["attributes.blob_key"])
        return {"success": True, "data": data}

    monkeypatch.setattr(files, "fetch_file_copy_bytes", fetch)
    receipt = backup.restore_files(
        backup_dir=tmp_path,
        destination=tmp_path / "restored",
        actor_id="#V#owner",
        cipher=cipher,
    )
    assert receipt["complete"] and receipt["restored_file_copy_ids"] == ["#V#file"]
    assert not any("visibility" in key or "specific_to" in key for key in writes[0][1])
    encrypted.write_bytes(cipher.encrypt(b"corrupt"))
    with pytest.raises(ValueError, match="integrity mismatch"):
        backup.restore_files(
            backup_dir=tmp_path,
            destination=tmp_path / "other",
            actor_id="#V#owner",
            cipher=cipher,
        )
    assert len(writes) == 1
    monkeypatch.setenv("VON_DB_NAME", "von_db")
    with pytest.raises(ValueError, match="separate restored database"):
        backup.restore_files(
            backup_dir=tmp_path,
            destination=tmp_path / "other",
            actor_id="#V#owner",
            cipher=cipher,
        )


def test_file_inventory_includes_archive_history_and_native_attachments():
    value = {
        "source_archive": {"file_copy_concept_id": "#V#current"},
        "source_archive_history": [{"file_copy_concept_id": "#V#old"}],
        "attachments": [
            {"file_copy_concept_id": "#V#attachment"},
            {"uri": "https://example.org"},
        ],
    }
    assert backup.file_copy_ids(value) == {"#V#current", "#V#old", "#V#attachment"}
