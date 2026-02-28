from __future__ import annotations

import pytest


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


def test_build_concept_relations_payload_defaults_to_asserted_only() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.concept_relation_service import (
        build_concept_relations_payload,
    )

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
                    "confidence_score": 0.74,
                    "status": "proposed",
                    "provenance": {"source": "unit_test"},
                    "created_at_utc": "2026-02-01T00:00:00Z",
                    "updated_at_utc": "2026-02-02T00:00:00Z",
                }
            ],
        }
    )
    ConceptsRepository.insert_one({"concept_id": "#V#bob", "relationships": {}})
    ConceptsRepository.insert_one({"concept_id": "#V#charlie", "relationships": {}})

    concept_doc = ConceptsRepository.find_one({"concept_id": "#V#alice"})
    payload = build_concept_relations_payload(
        concept_doc,
        include_relations_arg1=True,
        include_text_relations_arg1=False,
        include_concept_preview=False,
    )
    relations = payload.get("relations") or []
    assert any(r.get("relation_state") == "asserted" for r in relations)
    assert not any(r.get("relation_state") == "uncertain" for r in relations)
    assert payload["uncertainty_diagnostics"]["mode"] == "asserted_only"


def test_build_concept_relations_payload_includes_uncertain_rows_when_requested() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.concept_relation_service import (
        build_concept_relations_payload,
    )

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
                    "confidence_score": 0.74,
                    "status": "proposed",
                    "provenance": {"source": "unit_test"},
                    "created_at_utc": "2026-02-01T00:00:00Z",
                    "updated_at_utc": "2026-02-02T00:00:00Z",
                }
            ],
        }
    )
    ConceptsRepository.insert_one({"concept_id": "#V#bob", "relationships": {}})
    ConceptsRepository.insert_one({"concept_id": "#V#charlie", "relationships": {}})

    concept_doc = ConceptsRepository.find_one({"concept_id": "#V#alice"})
    payload = build_concept_relations_payload(
        concept_doc,
        include_relations_arg1=True,
        include_text_relations_arg1=False,
        include_concept_preview=False,
        include_uncertain=True,
        uncertainty_mode="include_uncertain",
    )
    relations = payload.get("relations") or []
    uncertain_rows = [r for r in relations if r.get("relation_state") == "uncertain"]
    assert uncertain_rows
    assert uncertain_rows[0]["is_asserted"] is False
    assert uncertain_rows[0]["uncertainty"]["assertion_id"] == "u1"
    assert payload["uncertainty_diagnostics"]["mode"] == "include_uncertain"


def test_find_relations_with_argument_uncertain_only_returns_uncertain_hits() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.concept_relation_service import (
        find_relations_with_argument,
    )

    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#alice",
            "relationships": {"related_to": ["#V#bob"]},
            "uncertain_relationship_assertions": [
                {
                    "assertion_id": "u1",
                    "source_id": "#V#alice",
                    "predicate": "related_to",
                    "target": "#V#bob",
                    "target_kind": "concept",
                    "confidence_score": 0.81,
                    "status": "proposed",
                    "provenance": {"source": "unit_test"},
                    "created_at_utc": "2026-02-01T00:00:00Z",
                    "updated_at_utc": "2026-02-02T00:00:00Z",
                }
            ],
        }
    )
    ConceptsRepository.insert_one({"concept_id": "#V#bob", "relationships": {}})

    payload = find_relations_with_argument(
        "#V#bob",
        include_uncertain=True,
        uncertainty_mode="uncertain_only",
        include_concept_preview=False,
    )
    hits = payload.get("hits") or []
    assert hits
    assert all(hit.get("relation_state") == "uncertain" for hit in hits)
    assert all(hit.get("is_asserted") is False for hit in hits)
    assert payload["uncertainty_diagnostics"]["mode"] == "uncertain_only"

