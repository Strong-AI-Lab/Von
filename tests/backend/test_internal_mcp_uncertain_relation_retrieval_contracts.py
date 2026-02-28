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
        except Exception:
            pass
    yield


def _gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_fetch_concept_relations_exposes_uncertainty_metadata() -> None:
    gateway = _gateway()
    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#alice",
            "relationships": {"related_to": ["#V#bob"]},
            "uncertain_relationship_assertions": [
                {
                    "assertion_id": "u1",
                    "source_id": "#V#alice",
                    "predicate": "related_to",
                    "target": "#V#charlie",
                    "target_kind": "concept",
                    "confidence_score": 0.66,
                    "status": "proposed",
                    "provenance": {"source": "gateway_test"},
                    "created_at_utc": "2026-02-01T00:00:00Z",
                    "updated_at_utc": "2026-02-02T00:00:00Z",
                }
            ],
        }
    )
    ConceptsRepository.insert_one({"concept_id": "#V#bob", "relationships": {}})
    ConceptsRepository.insert_one({"concept_id": "#V#charlie", "relationships": {}})

    payload = gateway.invoke(
        "fetch_concept",
        {
            "concept_id": "#V#alice",
            "include_relations_arg1": True,
            "include_uncertain": True,
            "uncertainty_mode": "include_uncertain",
            "include_concept_preview": False,
        },
    ).payload

    relations = ((payload or {}).get("relations") or {}).get("relations") or []
    uncertain_rows = [row for row in relations if row.get("relation_state") == "uncertain"]
    assert uncertain_rows
    assert uncertain_rows[0]["uncertainty"]["assertion_id"] == "u1"
    assert uncertain_rows[0]["is_asserted"] is False


def test_find_relations_with_argument_supports_uncertain_only_mode() -> None:
    gateway = _gateway()
    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#alice",
            "relationships": {"related_to": ["#V#bob"]},
            "uncertain_relationship_assertions": [
                {
                    "assertion_id": "u2",
                    "source_id": "#V#alice",
                    "predicate": "related_to",
                    "target": "#V#bob",
                    "target_kind": "concept",
                    "confidence_score": 0.72,
                    "status": "proposed",
                    "provenance": {"source": "gateway_test"},
                    "created_at_utc": "2026-02-01T00:00:00Z",
                    "updated_at_utc": "2026-02-02T00:00:00Z",
                }
            ],
        }
    )
    ConceptsRepository.insert_one({"concept_id": "#V#bob", "relationships": {}})

    payload = gateway.invoke(
        "find_relations_with_argument",
        {
            "concept_id": "#V#bob",
            "uncertainty_mode": "uncertain_only",
            "include_uncertain": True,
            "include_concept_preview": False,
        },
    ).payload
    hits = payload.get("hits") or []
    assert hits
    assert all(hit.get("relation_state") == "uncertain" for hit in hits)
    assert all(hit.get("is_asserted") is False for hit in hits)

