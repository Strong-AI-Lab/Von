from __future__ import annotations


def test_index_file_copy_indexes_blob_backed_text(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "Indexed text content",
            "content_type": "text/plain",
            "original_filename": "notes.txt",
            "size_bytes": 21,
            "blob": {"backend": "swift", "key": "uploads/user/hash/notes.txt"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.build_file_copy_artifact_record",
        lambda **_kwargs: {"artifact_id": "#V#file_copy_test"},
    )

    class _StubRAG:
        def __init__(self):
            self.docs = []
            self.namespace = None

        def upsert_documents(
            self, docs, *, namespace=None, allow_partial_failures=True
        ):
            self.docs = list(docs)
            self.namespace = namespace
            return 1, 0

    rag = _StubRAG()
    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        lambda *_args, **_kwargs: rag,
    )

    result = cat._index_file_copy(
        concept_id="#V#file_copy_test",
        namespace="#V#user@org",
    )

    assert result["success"] is True
    assert result["document_id"] == "file_copy:#V#file_copy_test"
    assert result["indexed_count"] == 1
    assert rag.namespace == "#V#user@org"
    assert rag.docs[0]["metadata"]["artifact_record"]["artifact_id"] == "#V#file_copy_test"


def test_index_file_copy_requires_namespace(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.delenv("VON_DEFAULT_NAMESPACE", raising=False)
    result = cat._index_file_copy(concept_id="#V#file_copy_test")
    assert result["success"] is False
    assert result["error"] == "namespace_required"


def test_import_local_file_copy_uses_authenticated_context(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user",
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.import_local_file_copy",
        lambda **_kwargs: {
            "success": True,
            "concept_id": "#V#imported_file",
            "artifact_record": {"artifact_id": "#V#imported_file"},
            "storage": {"backend": "local", "key": "imports/user/hash/file.txt"},
        },
    )

    result = cat._import_local_file_copy(
        local_path="README.md",
        namespace="#V#user@org",
    )

    assert result["success"] is True
    assert result["concept_id"] == "#V#imported_file"
    assert result["namespace"] == "#V#user@org"
