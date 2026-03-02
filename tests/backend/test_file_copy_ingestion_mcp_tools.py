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


def test_interpret_file_copy_persists_image_interpretation(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "OCR text from screenshot",
            "content_type": "image/png",
            "original_filename": "screenshot.png",
            "size_bytes": 1280,
            "byte_length": 1280,
            "blob": {"backend": "local", "key": "uploads/user/hash/screenshot.png"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **_kwargs: {"success": True, "data": b"\x89PNG..."},
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_image_interpretation",
        lambda **_kwargs: {
            "kind": "image",
            "description": "University enrolment screenshot with tabular student details.",
            "subject_tags": ["screenshot_or_interface", "text_heavy"],
            "content_text": "OCR text from screenshot",
            "content_length": 24,
        },
    )

    writes: list[dict[str, object]] = []

    def _fake_upsert_singleton_text_relation(**kwargs):
        writes.append(dict(kwargs))
        return {"relation_id": f"rel-{len(writes)}"}

    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        _fake_upsert_singleton_text_relation,
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    result = cat._interpret_file_copy(
        concept_id="#V#file_copy_image_test",
        namespace="#V#user@org",
    )

    assert result["success"] is True
    assert result["file_kind"] == "image"
    assert result["description"].startswith("University enrolment screenshot")
    assert result["persisted"] is True
    assert len(result["persisted_relations"]) == 3
    written_predicates = [row["predicate"] for row in writes]
    assert "hasDescription" in written_predicates
    assert "hasContent" in written_predicates
    assert "#V#has_file_copy_interpretation_json" in written_predicates


def test_interpret_file_copy_asserts_docx_subtype_when_determinable(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "DOCX body text",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "original_filename": "Reading Group(3).docx",
            "size_bytes": 2048,
            "byte_length": 2048,
            "blob": {"backend": "local", "key": "uploads/user/hash/reading-group.docx"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_document_interpretation",
        lambda **_kwargs: {
            "kind": "document",
            "description": "Document text extracted: DOCX body text",
            "subject_tags": ["document"],
            "content_text": "DOCX body text",
            "content_length": 14,
        },
    )

    relation_calls: list[dict[str, object]] = []
    text_writes: list[dict[str, object]] = []

    def _fake_add_relationship(**kwargs):
        relation_calls.append(dict(kwargs))
        return {"success": True, "forward_modified": True}

    def _fake_upsert_singleton_text_relation(**kwargs):
        text_writes.append(dict(kwargs))
        return {"relation_id": f"rel-{len(text_writes)}"}

    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        _fake_add_relationship,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        _fake_upsert_singleton_text_relation,
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    result = cat._interpret_file_copy(
        concept_id="#V#file_copy_docx_test",
        namespace="#V#user@org",
    )

    assert result["success"] is True
    assert result["file_kind"] == "document"
    assert result["subtype_type_concept_id"] == "#V#msword_docx_computer_file_copy"
    assert result["diagnostics"]["subtype_assertion_outcome"] == "subtype_added"
    assert result["persisted_structural_relations"] == [
        {
            "predicate": "is_an_instance_of",
            "target_id": "#V#msword_docx_computer_file_copy",
            "modified": True,
        }
    ]
    assert relation_calls == [
        {
            "source_id": "#V#file_copy_docx_test",
            "predicate": "is_an_instance_of",
            "target": "#V#msword_docx_computer_file_copy",
        }
    ]
    assert "hasDescription" in [row["predicate"] for row in text_writes]


def test_interpret_file_copy_reports_subtype_assertion_failure(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "DOCX body text",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "original_filename": "Reading Group(3).docx",
            "size_bytes": 2048,
            "byte_length": 2048,
            "blob": {"backend": "local", "key": "uploads/user/hash/reading-group.docx"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_document_interpretation",
        lambda **_kwargs: {
            "kind": "document",
            "description": "Document text extracted: DOCX body text",
            "subject_tags": ["document"],
            "content_text": "DOCX body text",
            "content_length": 14,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: {
            "success": False,
            "error": "target_not_found",
            "target_id": "#V#msword_docx_computer_file_copy",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **_kwargs: {"relation_id": "rel-test"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    result = cat._interpret_file_copy(
        concept_id="#V#file_copy_docx_test",
        namespace="#V#user@org",
    )

    assert result["success"] is False
    assert result["subtype_type_concept_id"] == "#V#msword_docx_computer_file_copy"
    assert result["diagnostics"]["subtype_assertion_outcome"] == "subtype_assertion_failed"
    assert result["persist_errors"]
    assert result["persist_errors"][0]["predicate"] == "is_an_instance_of"
    assert result["persist_errors"][0]["error"] == "target_not_found"


def test_interpret_file_copy_supports_non_persist_mode(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "Document body text",
            "content_type": "text/plain",
            "original_filename": "notes.txt",
            "size_bytes": 64,
            "byte_length": 64,
            "blob": {"backend": "local", "key": "uploads/user/hash/notes.txt"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_document_interpretation",
        lambda **_kwargs: {
            "kind": "document",
            "description": "Document text extracted: Document body text",
            "subject_tags": ["document"],
            "content_text": "Document body text",
            "content_length": 18,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("should_not_write")),
    )

    result = cat._interpret_file_copy(
        concept_id="#V#file_copy_doc_test",
        namespace="#V#user@org",
        persist=False,
    )

    assert result["success"] is True
    assert result["file_kind"] == "document"
    assert result["persisted"] is False
    assert result["persisted_relations"] == []
