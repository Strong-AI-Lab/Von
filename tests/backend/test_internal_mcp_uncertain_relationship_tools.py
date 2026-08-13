from __future__ import annotations

import pytest

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


@pytest.fixture(autouse=True)
def reset_mock_db(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        try:
            db.drop_collection("concepts")
            db.drop_collection("text_values")
            db.drop_collection("text_relations")
        except Exception:
            pass
    yield


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_uncertain_relationship_tools_gateway_denies_sessionless_promotion() -> None:
    gateway = _build_gateway()

    ConceptsRepository.insert_one({"concept_id": "#V#alice", "relationships": {}})
    ConceptsRepository.insert_one({"concept_id": "#V#strong_ai_lab", "relationships": {}})

    upsert = gateway.invoke(
        "upsert_uncertain_relationship_assertion",
        {
            "source_id": "#V#alice",
            "predicate": "#V#related_to",
            "target": "#V#strong_ai_lab",
            "confidence_score": 0.96,
            "provenance": {"source": "gateway_test"},
            "evidence_count": 2,
        },
    ).payload
    assert upsert.get("success") is True
    assertion_id = (upsert.get("assertion") or {}).get("assertion_id")
    assert isinstance(assertion_id, str) and assertion_id

    listed = gateway.invoke(
        "list_uncertain_relationship_assertions",
        {"source_id": "#V#alice", "predicate": "#V#related_to"},
    ).payload
    assert listed.get("success") is True
    assert listed.get("count") == 1

    promoted = gateway.invoke(
        "promote_uncertain_relationship_assertion",
        {"source_id": "#V#alice", "assertion_id": assertion_id},
    ).payload
    assert promoted.get("success") is False
    assert promoted.get("error_code") == "ontology_agent_delegation_required"
    assert promoted.get("effect_status") == "not_started"
    assert promoted.get("changed") is False

    source_doc = ConceptsRepository.find_one({"concept_id": "#V#alice"})
    relationships = (source_doc or {}).get("relationships") or {}
    assert "#V#strong_ai_lab" not in (relationships.get("related_to") or [])

    unchanged = gateway.invoke(
        "list_uncertain_relationship_assertions",
        {"source_id": "#V#alice", "predicate": "#V#related_to"},
    ).payload
    assert (unchanged.get("assertions") or [{}])[0].get("status") == "proposed"


def test_uncertain_relationship_tools_gateway_reject_and_migrate_legacy() -> None:
    gateway = _build_gateway()

    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#alice",
            "relationships": {},
            "hypothesized_relations": {
                "#V#has_affiliation": [
                    {"id": "legacy-1", "value": "#V#strong_ai_lab", "confidence_score": 0.8}
                ]
            },
        }
    )

    migrated = gateway.invoke(
        "migrate_legacy_hypothesized_relations",
        {"source_id": "#V#alice", "dry_run": False},
    ).payload
    assert migrated.get("success") is True
    assert migrated.get("migrated_count") == 1

    rejected = gateway.invoke(
        "reject_uncertain_relationship_assertion",
        {
            "source_id": "#V#alice",
            "assertion_id": "legacy-1",
            "reason": "not enough evidence",
        },
    ).payload
    assert rejected.get("success") is True
    assert (rejected.get("assertion") or {}).get("status") == "rejected"
