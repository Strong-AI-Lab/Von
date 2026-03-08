"""Gateway-level coverage for file-copy import/index MCP tools."""

from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_file_copy_ingestion_tools_gateway_invoke(monkeypatch):
    gateway = _build_gateway()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user",
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.import_local_file_copy",
        lambda **_kwargs: {
            "success": True,
            "concept_id": "#V#imported_file_gateway",
            "type_concept_id": "#V#computer_file_copy",
            "artifact_record": {"artifact_id": "#V#imported_file_gateway"},
            "storage": {"backend": "local", "key": "imports/user/hash/file.txt"},
        },
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "gateway indexed text",
            "content_type": "text/plain",
            "original_filename": "gateway.txt",
            "size_bytes": 19,
            "blob": {"backend": "local", "key": "imports/user/hash/file.txt"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.build_file_copy_artifact_record",
        lambda **_kwargs: {"artifact_id": "#V#imported_file_gateway"},
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.fetch_file_copy_bytes",
        lambda **_kwargs: {"success": True, "data": b"\x89PNG..."},
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_image_interpretation",
        lambda **_kwargs: {
            "kind": "image",
            "description": "Campus building exterior photo with visible signage.",
            "subject_tags": ["building_or_structure"],
            "content_text": "",
            "content_length": 0,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: {"success": True, "forward_modified": True},
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **_kwargs: {"relation_id": "rel-test"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "verified": True,
            "paper_concept_id": "#V#paper_from_file_copy_gateway",
            "file_copy_concept_id": "#V#imported_file_gateway",
            "representation_mode": "generic_file_copy",
        },
    )

    class _StubRAG:
        def upsert_documents(
            self, docs, *, namespace=None, allow_partial_failures=True
        ):
            return 1, 0

    monkeypatch.setattr(
        "src.backend.services.rag_service.get_rag_service",
        lambda *_args, **_kwargs: _StubRAG(),
    )

    imported = gateway.invoke(
        "import_local_file_copy",
        {"local_path": "README.md", "namespace": "#V#user@org"},
    ).payload
    assert imported.get("success") is True
    assert imported.get("concept_id") == "#V#imported_file_gateway"

    indexed = gateway.invoke(
        "index_file_copy",
        {"concept_id": "#V#imported_file_gateway", "namespace": "#V#user@org"},
    ).payload
    assert indexed.get("success") is True
    assert indexed.get("indexed_count") == 1

    interpreted = gateway.invoke(
        "interpret_file_copy",
        {"concept_id": "#V#imported_file_gateway", "namespace": "#V#user@org"},
    ).payload
    assert interpreted.get("success") is True
    assert interpreted.get("file_kind") == "document"


def test_interpret_file_copy_gateway_asserts_docx_subtype(monkeypatch):
    gateway = _build_gateway()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "Gateway DOCX text",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "original_filename": "Gateway.docx",
            "size_bytes": 64,
            "byte_length": 64,
            "blob": {"backend": "local", "key": "imports/user/hash/gateway.docx"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_document_interpretation",
        lambda **_kwargs: {
            "kind": "document",
            "description": "Document text extracted: Gateway DOCX text",
            "subject_tags": ["document"],
            "content_text": "Gateway DOCX text",
            "content_length": 17,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "verified": True,
            "paper_concept_id": "#V#paper_from_file_copy_gateway",
            "file_copy_concept_id": "#V#imported_file_gateway",
            "representation_mode": "generic_file_copy",
        },
    )

    relation_calls: list[dict[str, object]] = []

    def _fake_add_relationship(**kwargs):
        relation_calls.append(dict(kwargs))
        return {"success": True, "forward_modified": True}

    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        _fake_add_relationship,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **_kwargs: {"relation_id": "rel-gateway"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    interpreted = gateway.invoke(
        "interpret_file_copy",
        {"concept_id": "#V#imported_file_gateway", "namespace": "#V#user@org"},
    ).payload

    assert interpreted.get("success") is True
    assert interpreted.get("subtype_type_concept_id") == "#V#msword_docx_computer_file_copy"
    assert interpreted.get("diagnostics", {}).get("subtype_assertion_outcome") == "subtype_added"
    assert relation_calls == [
        {
            "source_id": "#V#imported_file_gateway",
            "predicate": "is_an_instance_of",
            "target": "#V#msword_docx_computer_file_copy",
        }
    ]


def test_import_url_file_copy_gateway_invoke(monkeypatch):
    gateway = _build_gateway()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user",
    )
    monkeypatch.setattr(
        "src.backend.services.remote_file_copy_ingestion_service.import_remote_url_file_copy",
        lambda **_kwargs: {
            "success": True,
            "concept_id": "#V#imported_url_file_gateway",
            "type_concept_id": "#V#computer_file_copy",
            "uploaded_at": "2026-03-08T00:00:00+00:00",
            "artifact_record": {
                "artifact_id": "#V#imported_url_file_gateway",
                "sha256": "feedface",
                "size_bytes": 24,
            },
            "storage": {
                "backend": "swift",
                "key": "imports/user/hash/deck.pptx",
                "uri": "swift://bucket/imports/user/hash/deck.pptx",
                "size_bytes": 24,
            },
            "response": {
                "status_code": 200,
                "size_bytes": 24,
                "headers": {"content_type": "application/octet-stream"},
            },
            "filename_resolution": {
                "original_filename": "deck.pptx",
                "source": "response.content_disposition",
            },
            "content_type_resolution": {
                "effective_content_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "source": "filename.extension",
            },
        },
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.maybe_launch_file_copy_uploaded_workflow",
        lambda **_kwargs: {"success": True, "triggered": True},
    )

    payload = gateway.invoke(
        "import_url_file_copy",
        {"url": "https://example.com/deck.pptx", "namespace": "#V#user@org"},
    ).payload

    assert payload.get("success") is True
    assert payload.get("concept_id") == "#V#imported_url_file_gateway"
    assert payload.get("workflow_event_launch", {}).get("triggered") is True


def test_interpret_file_copy_gateway_fails_closed_on_unverified_person_representation(
    monkeypatch,
):
    gateway = _build_gateway()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "Candidate CV details",
            "content_type": "text/plain",
            "original_filename": "candidate-cv.txt",
            "size_bytes": 64,
            "byte_length": 64,
            "blob": {"backend": "local", "key": "imports/user/hash/candidate-cv.txt"},
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
            "paper_concept_id": "#V#paper_from_file_copy_gateway",
            "file_copy_concept_id": "#V#imported_file_gateway",
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
        lambda **_kwargs: {"relation_id": "rel-gateway-cv"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    interpreted = gateway.invoke(
        "interpret_file_copy",
        {"concept_id": "#V#imported_file_gateway", "namespace": "#V#user@org"},
    ).payload

    assert interpreted.get("success") is False
    person_representation = interpreted.get("person_representation") or {}
    assert person_representation.get("attempted") is True
    assert person_representation.get("verified") is False
    persist_errors = interpreted.get("persist_errors") or []
    assert any(
        row.get("predicate") == "#V#person_representation_verification"
        for row in persist_errors
        if isinstance(row, dict)
    )


def test_interpret_file_copy_gateway_fails_closed_on_unverified_company_representation(
    monkeypatch,
):
    gateway = _build_gateway()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "Company profile content for uploaded web page.",
            "content_type": "text/plain",
            "original_filename": "company-webpage.txt",
            "size_bytes": 96,
            "byte_length": 96,
            "blob": {"backend": "local", "key": "imports/user/hash/company-webpage.txt"},
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
            "paper_concept_id": "#V#paper_from_file_copy_gateway",
            "file_copy_concept_id": "#V#imported_file_gateway",
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
        lambda **_kwargs: {"relation_id": "rel-gateway-company"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    interpreted = gateway.invoke(
        "interpret_file_copy",
        {"concept_id": "#V#imported_file_gateway", "namespace": "#V#user@org"},
    ).payload

    assert interpreted.get("success") is False
    company_representation = interpreted.get("company_representation") or {}
    assert company_representation.get("attempted") is True
    assert company_representation.get("verified") is False
    persist_errors = interpreted.get("persist_errors") or []
    assert any(
        row.get("predicate") == "#V#company_representation_verification"
        for row in persist_errors
        if isinstance(row, dict)
    )


def test_interpret_file_copy_gateway_fails_closed_on_unverified_meeting_representation(
    monkeypatch,
):
    gateway = _build_gateway()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "Meeting transcript content.",
            "content_type": "text/plain",
            "original_filename": "meeting-transcript.txt",
            "size_bytes": 96,
            "byte_length": 96,
            "blob": {"backend": "local", "key": "imports/user/hash/meeting-transcript.txt"},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_document_interpretation",
        lambda **_kwargs: {
            "kind": "document",
            "description": "Document text extracted: Meeting transcript content.",
            "subject_tags": ["document"],
            "content_text": "Meeting transcript content.",
            "content_length": 27,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "verified": True,
            "paper_concept_id": "#V#paper_from_file_copy_gateway",
            "file_copy_concept_id": "#V#imported_file_gateway",
            "representation_mode": "generic_file_copy",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.meeting_file_representation_service.materialise_meeting_representation_for_file_copy",
        lambda **_kwargs: {
            "success": False,
            "attempted": True,
            "verified": False,
            "reason": "meeting_identity_unresolved",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **_kwargs: {"relation_id": "rel-gateway-meeting"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    interpreted = gateway.invoke(
        "interpret_file_copy",
        {"concept_id": "#V#imported_file_gateway", "namespace": "#V#user@org"},
    ).payload

    assert interpreted.get("success") is False
    meeting_representation = interpreted.get("meeting_representation") or {}
    assert meeting_representation.get("attempted") is True
    assert meeting_representation.get("verified") is False
    persist_errors = interpreted.get("persist_errors") or []
    assert any(
        row.get("predicate") == "#V#meeting_representation_verification"
        for row in persist_errors
        if isinstance(row, dict)
    )


def test_interpret_file_copy_gateway_returns_pdf_diagram_candidates(monkeypatch):
    gateway = _build_gateway()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": "The prose section references Ministry of Health.",
            "content_type": "application/pdf",
            "original_filename": "ecosystem.pdf",
            "size_bytes": 5120,
            "byte_length": 5120,
            "blob": {"backend": "local", "key": "imports/user/hash/ecosystem.pdf"},
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
            "description": "Document text extracted: The prose section references Ministry of Health.",
            "subject_tags": ["document"],
            "content_text": "The prose section references Ministry of Health.",
            "content_length": 47,
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
            "diagram_relationship_candidates": [],
            "page_summaries": [{"page_number": 1, "diagram_candidate": True}],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: {"success": True, "forward_modified": True},
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.materialise_scholarly_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "verified": True,
            "paper_concept_id": "#V#paper_from_file_copy_gateway",
            "file_copy_concept_id": "#V#imported_file_gateway",
            "representation_mode": "generic_file_copy",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **_kwargs: {"relation_id": "rel-gateway-diagram"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    payload = gateway.invoke(
        "interpret_file_copy",
        {"concept_id": "#V#imported_file_gateway", "namespace": "#V#user@org"},
    ).payload

    assert payload.get("success") is True
    diagram_analysis = payload.get("diagram_analysis") or {}
    assert diagram_analysis.get("available") is True
    assert payload.get("diagnostics", {}).get("diagram_analysis", {}).get("diagram_only_count") == 1
    interpretation = payload.get("interpretation") or {}
    assert interpretation.get("candidate_assertions_require_confirmation") is True
