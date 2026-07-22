from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services import (
    grounded_read_tool_evidence_contract_vontology_service as service,
)
from src.backend.services.tool_evidence_projection_service import (
    project_tool_payload_for_llm,
    resolve_tool_projection_contract,
)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    yield


def _relationships(concept_id: str) -> dict[str, Any]:
    concept_doc = concept_service.get_concept_by_concept_id(concept_id)
    assert concept_doc is not None
    return dict(concept_doc.get("relationships") or {})


def _targets(concept_id: str, predicate: str) -> set[str]:
    values = _relationships(concept_id).get(predicate) or []
    if isinstance(values, str):
        return {values}
    return {str(value) for value in values}


def test_bootstrap_materialises_valid_idempotent_grounded_read_graph(
    _reset_mock_db: Any,
) -> None:
    first = service.bootstrap_grounded_read_tool_evidence_contract()
    second = service.bootstrap_grounded_read_tool_evidence_contract()

    assert first["success"] is True
    assert first["schema_version"] == "grounded_read_tool_evidence_contract.v1"
    assert first["contract_concept_id"] == (
        "#V#grounded_read_tool_evidence_contract_v1"
    )
    assert first["validation"]["success"] is True
    assert first["errors"] == []
    assert second["success"] is True
    assert second["created_concept_ids"] == []
    assert second["repaired_concept_ids"] == []
    assert set(second["existing_concept_ids"]) == set(
        service.canonical_grounded_read_tool_evidence_contract_concept_ids()
    )

    assert _targets(
        service.GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID,
        "#V#tool_contract_applies_to_tool",
    ) == {
        service.ARXIV_SEARCH_TOOL_ID,
        service.RAG_SEARCH_TOOL_ID,
        service.CONCEPT_EXISTS_TOOL_ID,
        service.FETCH_CONCEPT_TOOL_ID,
    }
    assert _targets(
        service.GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID,
        "#V#tool_contract_has_evidence_view",
    ) == {
        service.ARXIV_FINAL_ANSWER_VIEW_ID,
        service.RAG_FINAL_ANSWER_VIEW_ID,
        service.CONCEPT_EXISTS_FINAL_ANSWER_VIEW_ID,
        service.FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID,
    }
    assert _targets(
        service.ARXIV_FINAL_ANSWER_VIEW_ID,
        "#V#evidence_view_requires_field",
    ) == set(service.ARXIV_REQUIRED_FINAL_ANSWER_FIELD_IDS)
    assert _targets(
        service.RAG_FINAL_ANSWER_VIEW_ID,
        "#V#evidence_view_requires_field",
    ) == set(service.RAG_REQUIRED_FINAL_ANSWER_FIELD_IDS)
    assert _targets(
        service.CONCEPT_EXISTS_FINAL_ANSWER_VIEW_ID,
        "#V#evidence_view_requires_field",
    ) == set(service.CONCEPT_EXISTS_REQUIRED_FINAL_ANSWER_FIELD_IDS)
    assert _targets(
        service.FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID,
        "#V#evidence_view_requires_field",
    ) == set(service.FETCH_CONCEPT_REQUIRED_FINAL_ANSWER_FIELD_IDS)

    for tool_name, tool_id, view_id in (
        (
            "search_arxiv",
            service.ARXIV_SEARCH_TOOL_ID,
            service.ARXIV_FINAL_ANSWER_VIEW_ID,
        ),
        (
            "search_knowledge_base",
            service.RAG_SEARCH_TOOL_ID,
            service.RAG_FINAL_ANSWER_VIEW_ID,
        ),
        (
            "concept_exists",
            service.CONCEPT_EXISTS_TOOL_ID,
            service.CONCEPT_EXISTS_FINAL_ANSWER_VIEW_ID,
        ),
        (
            "fetch_concept",
            service.FETCH_CONCEPT_TOOL_ID,
            service.FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID,
        ),
    ):
        contract = resolve_tool_projection_contract(tool_name)
        assert contract is not None
        assert contract.tool_concept_id == tool_id
        assert contract.evidence_view_concept_ids == (view_id,)


def test_conversation_turn_support_bootstrap_materialises_grounded_read_contract(
    _reset_mock_db: Any,
) -> None:
    from src.backend.services.conversation_turn_workflow_vontology_service import (
        _ensure_conversation_turn_prompt_support,
    )

    report = _ensure_conversation_turn_prompt_support(
        ensure_tool_evidence_contracts=True
    )

    grounded_read = report["support_bootstraps"]["grounded_read_tool_evidence_contract"]
    assert grounded_read["success"] is True
    assert grounded_read["validation"]["success"] is True
    assert (
        concept_service.get_concept_by_concept_id(
            service.GROUNDED_READ_TOOL_EVIDENCE_CONTRACT_ID
        )
        is not None
    )
    fetch_contract = resolve_tool_projection_contract("fetch_concept")
    assert fetch_contract is not None
    assert fetch_contract.tool_concept_id == service.FETCH_CONCEPT_TOOL_ID
    assert fetch_contract.evidence_view_concept_ids == (
        service.FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID,
    )
    assert set(fetch_contract.output_field_ids) == set(
        service.FETCH_CONCEPT_OUTPUT_FIELD_IDS
    )
    assert _targets(
        service.FETCH_CONCEPT_FINAL_ANSWER_VIEW_ID,
        "#V#evidence_view_applies_to_tool",
    ) == {service.FETCH_CONCEPT_TOOL_ID}


def test_arxiv_projection_bridges_both_wire_shapes_and_keeps_typed_failure(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_grounded_read_tool_evidence_contract()

    current = project_tool_payload_for_llm(
        "search_arxiv",
        {
            "query": "reliable tool-using agent evaluation",
            "total_results": 1,
            "papers": [
                {
                    "id": "2607.12345",
                    "title": "Reliable Evaluation of Tool-Using Agents",
                    "url": "https://arxiv.org/abs/2607.12345",
                    "resource_uri": "arxiv://2607.12345",
                    "authors": ["Example Author"],
                    "abstract": "Evaluation evidence.",
                    "published": "2026-07-01T00:00:00Z",
                }
            ],
            "raw_provider_payload": "must not survive",
        },
    )
    alternate = project_tool_payload_for_llm(
        "search_arxiv",
        {
            "query": "reliable tool-using agent evaluation",
            "total": 1,
            "results": [
                {
                    "arxiv_id": "2607.12345",
                    "title": "Reliable Evaluation of Tool-Using Agents",
                    "canonical_url": "https://arxiv.org/abs/2607.12345",
                    "summary": "Evaluation evidence.",
                }
            ],
        },
    )
    unavailable = project_tool_payload_for_llm(
        "search_arxiv",
        {
            "success": False,
            "error": "External arXiv provider unavailable",
            "error_code": "external_arxiv_mcp_call_failed",
            "error_details": {
                "external_provider": "arxiv-mcp-server",
                "recovery_hint": "retry_or_use_authoritative_non_mcp_source",
            },
            "suggestions": ["Retry the bounded search"],
        },
    )

    assert current is not None
    assert alternate is not None
    assert current["papers"][0]["id"] == alternate["papers"][0]["id"]
    assert current["papers"][0]["title"] == alternate["papers"][0]["title"]
    assert current["papers"][0]["url"] == alternate["papers"][0]["url"]
    assert current["papers"][0]["abstract"] == alternate["papers"][0]["abstract"]
    assert current["total_results"] == alternate["total_results"] == 1
    assert "raw_provider_payload" not in current

    assert unavailable is not None
    assert unavailable["success"] is False
    assert unavailable["error_code"] == "external_arxiv_mcp_call_failed"
    assert unavailable["error_details"]["recovery_hint"] == (
        "retry_or_use_authoritative_non_mcp_source"
    )
    assert unavailable["suggestions"] == ["Retry the bounded search"]
    missing_ids = {
        item["field_concept_id"]
        for item in unavailable["_tool_evidence_projection"]["missing_required_fields"]
    }
    assert service.ARXIV_PAPERS_COLLECTION_FIELD_ID in missing_ids


def test_rag_projection_keeps_semantic_provenance_and_typed_retrieval_state(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_grounded_read_tool_evidence_contract()

    projected = project_tool_payload_for_llm(
        "search_knowledge_base",
        {
            "success": True,
            "query": "represented workflow purpose",
            "count": 1,
            "results": [
                {
                    "id": "text_relation:1",
                    "score": 0.94,
                    "text": "The workflow retrieves represented concept instances.",
                    "metadata": {
                        "title": "Concept search instance retrieval workflow",
                        "source_system": "rag.llamaindex",
                        "item_kind": "rag_chunk",
                        "type": "text_relation",
                        "predicate": "hasDescription",
                        "concept_id": "#V#concept_search_instance_retrieval_workflow",
                        "private_backend_path": "/must/not/survive",
                    },
                    "embedding": [0.1, 0.2],
                }
            ],
            "retrieval_state": {
                "schema_version": "rag_retrieval_state.v1",
                "status": "results_available",
                "usable": True,
                "authoritative_empty": False,
                "result_count": 1,
                "recovery_affordances": [],
            },
            "effective_namespace": "#V#private_actor@private_org",
        },
    )

    assert projected is not None
    assert projected["query"] == "represented workflow purpose"
    assert projected["count"] == 1
    assert projected["results"] == [
        {
            "id": "text_relation:1",
            "score": 0.94,
            "text": "The workflow retrieves represented concept instances.",
            "title": "Concept search instance retrieval workflow",
            "source_system": "rag.llamaindex",
            "item_kind": "rag_chunk",
            "type": "text_relation",
            "predicate": "hasDescription",
            "concept_id": "#V#concept_search_instance_retrieval_workflow",
        }
    ]
    assert projected["retrieval_state"]["status"] == "results_available"
    assert "effective_namespace" not in projected
    assert "embedding" not in projected["results"][0]
    assert "private_backend_path" not in projected["results"][0]
    assert projected["_tool_evidence_projection"]["missing_required_fields"] == []


def test_concept_lookup_views_keep_safe_identity_and_typed_not_found_evidence(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_grounded_read_tool_evidence_contract()

    existence = project_tool_payload_for_llm(
        "concept_exists",
        {
            "success": True,
            "concept_id": "#V#deliberately_absent",
            "exists": False,
            "accessible": False,
            "access": {
                "authenticated_user_concept_id": "#V#private_actor",
                "organisation_concept_id": "#V#private_org",
                "access_control_enforced": True,
                "restriction_families_present": [],
                "specific_to_user_restricted": False,
                "specific_to_org_restricted": False,
            },
        },
    )
    fetched = project_tool_payload_for_llm(
        "fetch_concept",
        {
            "concept_id": "#V#represented_workflow",
            "direct_concept_name": "Represented workflow",
            "names": [{"name": "Represented workflow", "language": "en-NZ"}],
            "concept_data": {
                "preserved_fields": {
                    "description": "A workflow represented in Vontology."
                }
            },
            "relationships": {"is_an_instance_of": ["#V#ai_workflow"]},
            "relations": {
                "total_hits": 1,
                "hits": [
                    {
                        "predicate_concept_id": "#V#hasStep",
                        "target_value": "#V#represented_workflow_step",
                    }
                ],
            },
            "attributes": {
                "runtime_profile_alias": "represented-profile",
                "oauth_tokens": "must not survive",
            },
            "guid": "private-storage-identifier",
        },
    )
    not_found = project_tool_payload_for_llm(
        "fetch_concept",
        {
            "success": False,
            "error": "Concept '#V#deliberately_absent' not found",
            "error_code": "concept_not_found",
            "suggestions": ["Use search_concepts to find available concepts"],
            "related_concept_ids": ["#V#deliberately_absent"],
        },
    )

    assert existence is not None
    assert existence["concept_id"] == "#V#deliberately_absent"
    assert existence["exists"] is False
    assert existence["accessible"] is False
    assert existence["access_control_enforced"] is True
    assert "authenticated_user_concept_id" not in repr(existence)
    assert "organisation_concept_id" not in repr(existence)

    assert fetched is not None
    assert fetched["concept_id"] == "#V#represented_workflow"
    assert fetched["name"] == "Represented workflow"
    assert fetched["description"] == "A workflow represented in Vontology."
    assert fetched["relationships"] == {"is_an_instance_of": ["#V#ai_workflow"]}
    assert fetched["relations"]["hits"][0]["predicate_concept_id"] == ("#V#hasStep")
    assert fetched["runtime_profile_alias"] == "represented-profile"
    assert "oauth_tokens" not in repr(fetched)
    assert "guid" not in fetched

    assert not_found is not None
    assert not_found["success"] is False
    assert not_found["error_code"] == "concept_not_found"
    assert not_found["related_concept_ids"] == ["#V#deliberately_absent"]
    assert not_found["suggestions"] == [
        "Use search_concepts to find available concepts"
    ]
    assert not_found["_tool_evidence_projection"]["missing_required_fields"] == [
        {
            "field_concept_id": service.CONCEPT_ID_FIELD_ID,
            "output_key": "concept_id",
        }
    ]


def test_grounded_read_contract_uses_graph_kr_instead_of_json_text_contracts() -> None:
    concept_ids = service.canonical_grounded_read_tool_evidence_contract_concept_ids()
    relationship_specs = (
        service.canonical_grounded_read_tool_evidence_contract_relationships()
    )

    assert all("json" not in concept_id.lower() for concept_id in concept_ids)
    assert all("json" not in spec.predicate.lower() for spec in relationship_specs)
    assert all(spec.predicate != "hasText" for spec in relationship_specs)
