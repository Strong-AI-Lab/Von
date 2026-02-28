from __future__ import annotations

from unittest.mock import patch

import pytest


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


def test_list_uncertain_assertions_includes_legacy_mapping() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.uncertain_relationship_service import (
        list_uncertain_relationship_assertions,
    )

    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#alice",
            "relationships": {},
            "hypothesized_relations": {
                "#V#has_affiliation": [
                    {
                        "id": "legacy-1",
                        "value": "#V#strong_ai_lab",
                        "confidence_score": 0.88,
                        "source": "llm_extraction",
                        "source_interaction_id": "turn-1",
                    }
                ]
            },
        }
    )

    rows = list_uncertain_relationship_assertions(
        source_id="#V#alice",
        predicate="#V#has_affiliation",
        include_legacy=True,
    )
    assert len(rows) == 1
    assert rows[0]["assertion_id"] == "legacy-1"
    assert rows[0]["target"] == "#V#strong_ai_lab"
    assert rows[0]["status"] == "proposed"


def test_upsert_uncertain_assertion_creates_then_updates() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.uncertain_relationship_service import (
        upsert_uncertain_relationship_assertion,
    )

    ConceptsRepository.insert_one({"concept_id": "#V#alice", "relationships": {}})

    created = upsert_uncertain_relationship_assertion(
        source_id="#V#alice",
        predicate="#V#has_affiliation",
        target="#V#strong_ai_lab",
        confidence_score=0.9,
        provenance={"source": "unit_test"},
    )
    assert created["success"] is True
    assert created["created"] is True
    assertion_id = created["assertion"]["assertion_id"]

    updated = upsert_uncertain_relationship_assertion(
        source_id="#V#alice",
        predicate="#V#has_affiliation",
        target="#V#strong_ai_lab",
        confidence_score=0.97,
        assertion_id=assertion_id,
        provenance={"updated_by": "unit_test"},
    )
    assert updated["success"] is True
    assert updated["created"] is False
    assert updated["assertion"]["confidence_score"] == pytest.approx(0.97)
    assert updated["assertion"]["provenance"]["updated_by"] == "unit_test"


def test_promote_uncertain_assertion_writes_relationship_and_marks_promoted() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.uncertain_relationship_service import (
        promote_uncertain_relationship_assertion,
        upsert_uncertain_relationship_assertion,
    )

    ConceptsRepository.insert_one({"concept_id": "#V#alice", "relationships": {}})
    ConceptsRepository.insert_one(
        {"concept_id": "#V#strong_ai_lab", "relationships": {}}
    )

    created = upsert_uncertain_relationship_assertion(
        source_id="#V#alice",
        predicate="#V#related_to",
        target="#V#strong_ai_lab",
        confidence_score=0.96,
        provenance={"source": "unit_test"},
    )
    assertion_id = created["assertion"]["assertion_id"]

    promoted = promote_uncertain_relationship_assertion(
        source_id="#V#alice",
        assertion_id=assertion_id,
        operator="test",
    )
    assert promoted["success"] is True
    assert promoted["assertion"]["status"] == "promoted"
    assert promoted["assertion"]["promoted_relation_kind"] == "concept_relationship"

    source_doc = ConceptsRepository.find_one({"concept_id": "#V#alice"})
    relationships = (source_doc or {}).get("relationships") or {}
    assert "#V#strong_ai_lab" in (relationships.get("related_to") or [])


def test_reject_uncertain_assertion_records_reason_and_blocks_promotion() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.uncertain_relationship_service import (
        promote_uncertain_relationship_assertion,
        reject_uncertain_relationship_assertion,
        upsert_uncertain_relationship_assertion,
    )

    ConceptsRepository.insert_one({"concept_id": "#V#alice", "relationships": {}})

    created = upsert_uncertain_relationship_assertion(
        source_id="#V#alice",
        predicate="#V#has_affiliation",
        target="#V#strong_ai_lab",
        confidence_score=0.45,
        provenance={"source": "unit_test"},
    )
    assertion_id = created["assertion"]["assertion_id"]

    rejected = reject_uncertain_relationship_assertion(
        source_id="#V#alice",
        assertion_id=assertion_id,
        reason="insufficient evidence",
        operator="test",
    )
    assert rejected["success"] is True
    assert rejected["assertion"]["status"] == "rejected"
    assert rejected["assertion"]["rejection_reason"] == "insufficient evidence"

    promotion = promote_uncertain_relationship_assertion(
        source_id="#V#alice",
        assertion_id=assertion_id,
    )
    assert promotion["success"] is False
    assert promotion["error"] == "assertion_not_promotable"


def test_migrate_legacy_hypothesized_relations_persists_canonical_rows() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.uncertain_relationship_service import (
        migrate_legacy_hypothesized_relations,
    )

    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#alice",
            "relationships": {},
            "hypothesized_relations": {
                "#V#has_affiliation": [
                    {"value": "#V#strong_ai_lab", "confidence_score": 0.91}
                ]
            },
        }
    )

    dry = migrate_legacy_hypothesized_relations(
        source_id="#V#alice",
        dry_run=True,
    )
    assert dry["success"] is True
    assert dry["migrated_count"] == 1

    applied = migrate_legacy_hypothesized_relations(
        source_id="#V#alice",
        dry_run=False,
    )
    assert applied["success"] is True
    assert applied["migrated_count"] == 1

    source_doc = ConceptsRepository.find_one({"concept_id": "#V#alice"})
    canonical = (source_doc or {}).get("uncertain_relationship_assertions") or []
    assert len(canonical) == 1
    assert canonical[0]["predicate"] == "#V#has_affiliation"

