from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services import tool_evidence_contract_vontology_service as service


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
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


def test_bootstrap_materialises_tool_evidence_contract_vocabulary_as_graph_kr(
    _reset_mock_db: Any,
) -> None:
    report = service.bootstrap_tool_evidence_contract_vocabulary()

    assert report["success"] is True
    assert report["schema_version"] == "tool_evidence_contract_vocabulary.v1"
    assert report["vocabulary_concept_id"] == "#V#tool_evidence_contract_vocabulary_v1"
    assert report["counts"]["predicate_specs"] >= 20
    assert report["counts"]["relationships_written"] >= 1
    assert report["errors"] == []

    vocabulary_relationships = _relationships("#V#tool_evidence_contract_vocabulary_v1")
    included_concepts = set(
        vocabulary_relationships.get("#V#vocabulary_includes_concept") or []
    )
    assert "#V#tool_result_field" in included_concepts
    assert "#V#tool_evidence_view" in included_concepts
    assert "#V#list_tool_has_detail_tool" in included_concepts
    assert "#V#tool_field_role_answer_evidence" in included_concepts

    predicate_relationships = _relationships("#V#evidence_view_requires_field")
    assert "#V#predicate" in predicate_relationships.get("is_an_instance_of", [])
    assert "#V#predicate" not in predicate_relationships.get("is_a_type_of", [])

    evidence_view_relationships = _relationships("#V#tool_evidence_view")
    assert "#V#tool_interface_contract" in evidence_view_relationships.get(
        "is_a_type_of",
        [],
    )

    validation = service.validate_tool_evidence_contract_vocabulary()
    assert validation["success"] is True
    assert validation["missing_concept_ids"] == []
    assert validation["predicate_typing_errors"] == []
    assert validation["missing_vocabulary_members"] == []


def test_vocabulary_contains_generic_list_detail_and_preservation_predicates() -> None:
    predicate_ids = set(service.canonical_tool_evidence_contract_predicate_ids())

    assert {
        "#V#tool_emits_collection_entity_type",
        "#V#entity_type_has_tool_field",
        "#V#field_extracts_from_payload_path",
        "#V#evidence_view_requires_field",
        "#V#list_tool_has_detail_tool",
        "#V#list_detail_affordance_uses_identifier_field",
        "#V#detail_tool_accepts_identifier_field",
        "#V#detail_tool_completes_field",
        "#V#tool_result_preserves_field",
    }.issubset(predicate_ids)


def test_vocabulary_does_not_define_json_text_relation_contract_predicates() -> None:
    concept_ids = service.canonical_tool_evidence_contract_concept_ids(
        include_core=True,
    )

    assert all("json" not in concept_id.lower() for concept_id in concept_ids)