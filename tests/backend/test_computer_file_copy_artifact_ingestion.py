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
                "user_concept_id": "#V#user_test",
                "organisation_concept_id": "#V#org_test",
                "namespace": "#V#user_test@org_test",
                "namespace_source": "request.namespace",
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
    assert record["provenance"]["user_concept_id"] == "#V#user_test"
    assert record["provenance"]["organisation_concept_id"] == "#V#org_test"
    assert record["provenance"]["namespace"] == "#V#user_test@org_test"
    assert record["provenance"]["namespace_source"] == "request.namespace"
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

    captured_create_kwargs: dict[str, object] = {}

    def _fake_create_computer_file_copy_instance(**kwargs):
        captured_create_kwargs.update(kwargs)
        return _Record()

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        _fake_create_computer_file_copy_instance,
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.build_file_copy_artifact_record",
        lambda **_kwargs: {"artifact_id": "#V#imported_file_copy"},
    )

    result = svc.import_local_file_copy(
        local_path=str(local_file),
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
        namespace_source="request.namespace",
        allowed_root=tmp_path,
    )

    assert result["success"] is True
    assert result["concept_id"] == "#V#imported_file_copy"
    assert result["artifact_record"]["artifact_id"] == "#V#imported_file_copy"
    assert result["storage"]["key"].startswith("imports/user/")
    assert captured_create_kwargs["organisation_concept_id"] == "#V#org"
    assert captured_create_kwargs["namespace"] == "#V#user@org"
    assert captured_create_kwargs["namespace_source"] == "request.namespace"


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


def test_import_bytes_file_copy_persists_typing(monkeypatch):
    from src.backend.services import computer_file_copy_service as svc
    from src.backend.services.blob_store import BlobRef
    from src.backend.services.blob_uploads import StoredBytes

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
            sha256="typed123",
            size_bytes=len(kwargs["data"]),
        ),
    )

    class _Record:
        concept_id = "#V#typed_file_copy"
        type_concept_id = "#V#computer_file_copy"
        uploaded_at = "2026-01-01T00:00:00+00:00"

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        lambda **_kwargs: _Record(),
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.build_file_copy_artifact_record",
        lambda **_kwargs: {"artifact_id": "#V#typed_file_copy", "sha256": "typed123"},
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_typing_service.infer_file_copy_typing",
        lambda **_kwargs: {
            "success": True,
            "detected_type_concept_ids": ["#V#mspowerpoint_pptx_computer_file_copy"],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_typing_service.persist_file_copy_typing",
        lambda **_kwargs: {"success": True, "typing_persisted": True},
    )

    result = svc.import_bytes_file_copy(
        data=b"pptx-bytes",
        user_concept_id="#V#user",
        original_filename="deck.pptx",
        content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        source_system="remote_url_import",
        source_identifier="https://example.com/deck.pptx",
        source_uri="https://cdn.example.com/deck.pptx",
    )

    assert result["success"] is True
    assert result["concept_id"] == "#V#typed_file_copy"
    assert result["storage"]["key"].endswith("/deck.pptx")
    assert result["typing"]["typing_persisted"] is True
    assert result["typing_result"]["detected_type_concept_ids"] == [
        "#V#mspowerpoint_pptx_computer_file_copy"
    ]


def test_import_bytes_file_copy_reuses_existing_concept_without_reupload(monkeypatch):
    from src.backend.services import computer_file_copy_service as svc

    class _Existing:
        concept_id = "#V#existing_file_copy"
        type_concept_id = "#V#computer_file_copy"
        uploaded_at = "2026-01-01T00:00:00+00:00"

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.find_existing_computer_file_copy_instance",
        lambda **_kwargs: _Existing(),
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.resolve_file_copy_blob_info",
        lambda **_kwargs: svc.FileCopyBlobInfo(
            concept_id="#V#existing_file_copy",
            blob_key="imports/user/hash/notes.txt",
            blob_backend="s3",
            blob_uri="s3://bucket/imports/user/hash/notes.txt",
            content_type="text/plain",
            original_filename="notes.txt",
            size_bytes=11,
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.build_file_copy_artifact_record",
        lambda **_kwargs: {"artifact_id": "#V#existing_file_copy"},
    )
    monkeypatch.setattr(
        "src.backend.services.blob_uploads.put_bytes_durable",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("should_not_upload")),
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("should_not_create")),
    )

    result = svc.import_bytes_file_copy(
        data=b"hello world",
        user_concept_id="#V#user",
        original_filename="notes.txt",
        content_type="text/plain",
    )

    assert result["success"] is True
    assert result["concept_id"] == "#V#existing_file_copy"
    assert result["reused_existing"] is True
    assert result["storage"]["backend"] == "s3"
    assert result["storage"]["key"] == "imports/user/hash/notes.txt"
