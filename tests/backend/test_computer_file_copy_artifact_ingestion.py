from __future__ import annotations


def test_build_file_copy_artifact_record_includes_provenance(monkeypatch):
    from src.backend.services import computer_file_copy_service as svc

    info = svc.FileCopyBlobInfo(
        concept_id="#V#file_copy_test",
        blob_key="uploads/user/hash/notes.txt",
        blob_backend="swift",
        blob_uri="swift://bucket/uploads/user/hash/notes.txt",
        content_type="text/plain",
        original_filename="notes.txt",
        size_bytes=42,
    )

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.resolve_file_copy_blob_info",
        lambda **_kwargs: info,
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service._first_text_value",
        lambda concept_id, predicate: {
            "#V#has_sha256": "deadbeef",
            "#V#has_upload_timestamp": "2026-01-01T00:00:00+00:00",
        }.get(predicate),
    )

    record = svc.build_file_copy_artifact_record(
        concept_doc={
            "concept_id": "#V#file_copy_test",
            "name": "notes.txt",
            "attributes": {
                "source_system": "filesystem_import",
                "source_identifier": "C:/repo/notes.txt",
                "source_uri": "file:///C:/repo/notes.txt",
                "ingested_at": "2026-01-01T00:00:01+00:00",
            },
            "relationships": {"is_an_instance_of": ["#V#computer_file_copy"]},
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:01:00+00:00",
        }
    )

    assert record is not None
    assert record["artifact_id"] == "#V#file_copy_test"
    assert record["blob"]["key"] == "uploads/user/hash/notes.txt"
    assert record["sha256"] == "deadbeef"
    assert record["provenance"]["source"] == "filesystem_import"
    assert record["provenance"]["source_uri"] == "file:///C:/repo/notes.txt"
    assert record["provenance"]["uploaded_at"] == "2026-01-01T00:00:00+00:00"
    assert record["type_concept_ids"] == ["#V#computer_file_copy"]


def test_import_local_file_copy_registers_blob_and_concept(monkeypatch, tmp_path):
    from src.backend.services import computer_file_copy_service as svc
    from src.backend.services.blob_store import BlobRef
    from src.backend.services.blob_uploads import StoredBytes

    local_file = tmp_path / "notes.txt"
    local_file.write_text("hello world", encoding="utf-8")

    monkeypatch.setattr(
        "src.backend.services.blob_uploads.put_bytes_durable",
        lambda **kwargs: StoredBytes(
            ref=BlobRef(
                backend="local",
                key=str(kwargs["key"]),
                uri=f"local://{kwargs['key']}",
                content_type=kwargs.get("content_type"),
                size_bytes=len(kwargs["data"]),
                metadata=dict(kwargs.get("metadata") or {}),
            ),
            sha256="abc123",
            size_bytes=len(kwargs["data"]),
        ),
    )

    class _Record:
        concept_id = "#V#imported_file_copy"
        type_concept_id = "#V#computer_file_copy"
        uploaded_at = "2026-01-01T00:00:00+00:00"

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        lambda **_kwargs: _Record(),
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.build_file_copy_artifact_record",
        lambda **_kwargs: {"artifact_id": "#V#imported_file_copy"},
    )

    result = svc.import_local_file_copy(
        local_path=str(local_file),
        user_concept_id="#V#user",
        allowed_root=tmp_path,
    )

    assert result["success"] is True
    assert result["concept_id"] == "#V#imported_file_copy"
    assert result["artifact_record"]["artifact_id"] == "#V#imported_file_copy"
    assert result["storage"]["key"].startswith("imports/user/")


def test_import_local_file_copy_rejects_path_outside_allowed_root(tmp_path):
    from src.backend.services import computer_file_copy_service as svc

    allowed_root = tmp_path / "allowed"
    outside_root = tmp_path / "outside"
    allowed_root.mkdir()
    outside_root.mkdir()

    local_file = outside_root / "private.txt"
    local_file.write_text("nope", encoding="utf-8")

    result = svc.import_local_file_copy(
        local_path=str(local_file),
        user_concept_id="#V#user",
        allowed_root=allowed_root,
    )

    assert result["success"] is False
    assert result["error"] == "path_outside_allowed_root"
