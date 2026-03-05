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
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "verified": True,
            "paper_concept_id": "#V#paper_from_file_copy",
            "file_copy_concept_id": "#V#file_copy_docx_test",
            "representation_mode": "generic_file_copy",
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
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "verified": True,
            "paper_concept_id": "#V#paper_from_file_copy",
            "file_copy_concept_id": "#V#file_copy_docx_test",
            "representation_mode": "generic_file_copy",
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


def test_interpret_file_copy_fails_closed_when_person_representation_unverified(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "Candidate CV details",
            "content_type": "text/plain",
            "original_filename": "candidate-cv.txt",
            "size_bytes": 128,
            "byte_length": 128,
            "blob": {"backend": "local", "key": "uploads/user/hash/candidate-cv.txt"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_document_interpretation",
        lambda **_kwargs: {
            "kind": "document",
            "description": "Document text extracted: Candidate CV details",
            "subject_tags": ["document"],
            "content_text": "Candidate CV details",
            "content_length": 20,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "verified": True,
            "paper_concept_id": "#V#paper_from_file_copy",
            "file_copy_concept_id": "#V#file_copy_cv_test",
            "representation_mode": "generic_file_copy",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.person_file_representation_service.materialise_person_representation_for_file_copy",
        lambda **_kwargs: {
            "success": False,
            "attempted": True,
            "verified": False,
            "reason": "person_identity_unresolved",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **_kwargs: {"relation_id": "rel-cv"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    result = cat._interpret_file_copy(
        concept_id="#V#file_copy_cv_test",
        namespace="#V#user@org",
    )

    assert result["success"] is False
    person_representation = result.get("person_representation") or {}
    assert person_representation.get("attempted") is True
    assert person_representation.get("verified") is False
    persist_errors = result.get("persist_errors") or []
    assert any(
        row.get("predicate") == "#V#person_representation_verification"
        for row in persist_errors
        if isinstance(row, dict)
    )


def test_interpret_file_copy_fails_closed_when_company_representation_unverified(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "Company profile content for uploaded web page.",
            "content_type": "text/plain",
            "original_filename": "company-webpage.txt",
            "size_bytes": 256,
            "byte_length": 256,
            "blob": {"backend": "local", "key": "uploads/user/hash/company-webpage.txt"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_document_interpretation",
        lambda **_kwargs: {
            "kind": "document",
            "description": "Document text extracted: Company profile content for uploaded web page.",
            "subject_tags": ["document"],
            "content_text": "Company profile content for uploaded web page.",
            "content_length": 47,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "verified": True,
            "paper_concept_id": "#V#paper_from_file_copy",
            "file_copy_concept_id": "#V#file_copy_company_test",
            "representation_mode": "generic_file_copy",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.company_file_representation_service.materialise_company_representation_for_file_copy",
        lambda **_kwargs: {
            "success": False,
            "attempted": True,
            "verified": False,
            "reason": "company_identity_unresolved",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **_kwargs: {"relation_id": "rel-company"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    result = cat._interpret_file_copy(
        concept_id="#V#file_copy_company_test",
        namespace="#V#user@org",
    )

    assert result["success"] is False
    company_representation = result.get("company_representation") or {}
    assert company_representation.get("attempted") is True
    assert company_representation.get("verified") is False
    persist_errors = result.get("persist_errors") or []
    assert any(
        row.get("predicate") == "#V#company_representation_verification"
        for row in persist_errors
        if isinstance(row, dict)
    )


def test_interpret_file_copy_includes_pdf_diagram_analysis(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "This report discusses the Ministry of Health.",
            "content_type": "application/pdf",
            "original_filename": "ecosystem-map.pdf",
            "size_bytes": 4096,
            "byte_length": 4096,
            "blob": {"backend": "local", "key": "uploads/user/hash/ecosystem-map.pdf"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **_kwargs: {"success": True, "data": b"%PDF-1.4..."},
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_document_interpretation",
        lambda **_kwargs: {
            "kind": "document",
            "description": "Document text extracted: This report discusses the Ministry of Health.",
            "subject_tags": ["document"],
            "content_text": "This report discusses the Ministry of Health.",
            "content_length": 44,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.extract_pdf_diagram_organisation_candidates",
        lambda **_kwargs: {
            "available": True,
            "method": "pymupdf_diagram_ocr",
            "requires_human_confirmation": True,
            "prose_organisations": [{"name": "Ministry of Health"}],
            "diagram_organisations": [
                {"name": "Ministry of Health"},
                {"name": "University of Auckland"},
            ],
            "diagram_only_organisations": [{"name": "University of Auckland"}],
            "diagram_relationship_candidates": [
                {
                    "source_name": "Ministry of Health",
                    "target_name": "University of Auckland",
                    "relation_hint": "directed_link",
                }
            ],
            "page_summaries": [
                {"page_number": 2, "diagram_candidate": True, "signals": ["embedded_images"]}
            ],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "verified": True,
            "paper_concept_id": "#V#paper_from_file_copy",
            "file_copy_concept_id": "#V#file_copy_pdf_test",
            "representation_mode": "generic_file_copy",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **_kwargs: {"relation_id": "rel-diagram"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    result = cat._interpret_file_copy(
        concept_id="#V#file_copy_pdf_test",
        namespace="#V#user@org",
        include_pdf_diagram_analysis=True,
    )

    assert result["success"] is True
    assert result["file_kind"] == "document"
    assert result["diagram_analysis"]["available"] is True
    assert (
        result["interpretation"]["candidate_assertions_require_confirmation"] is True
    )
    assert "diagram_organisation_candidates" in result["interpretation"]["subject_tags"]
    assert result["diagnostics"]["diagram_analysis"]["diagram_only_count"] == 1
