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


def test_get_predicate_incidence_entity_mode_groups_distinct_predicates() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.concept_relation_service import get_predicate_incidence

    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#michael_witbrock",
            "relationships": {
                "#V#author_of": ["#V#paper_one", "#V#paper_two"],
                "#V#task_assigned_to": ["#V#task_one", "#V#task_two", "#V#task_three"],
            },
        }
    )
    for concept_id in (
        "#V#paper_one",
        "#V#paper_two",
        "#V#task_one",
        "#V#task_two",
        "#V#task_three",
    ):
        ConceptsRepository.insert_one({"concept_id": concept_id, "relationships": {}})

    payload = get_predicate_incidence(
        concept_id="#V#michael_witbrock",
        include_concept_preview=False,
    )

    assert payload["mode"] == "entity"
    assert payload["concept_id"] == "#V#michael_witbrock"
    assert payload["total_predicates"] == 2
    rows = {
        row["predicate_concept_id"]: row for row in payload.get("predicates") or []
    }
    assert rows["#V#author_of"]["relation_hit_count"] == 2
    assert rows["#V#author_of"]["grounding_count"] == 2
    assert rows["#V#author_of"]["binary_relation_hit_count"] == 2
    assert rows["#V#author_of"]["subject_argument_hit_count"] == 2
    assert rows["#V#author_of"]["object_argument_hit_count"] == 0
    assert rows["#V#author_of"]["argument_indexes"] == [1]
    assert rows["#V#task_assigned_to"]["relation_hit_count"] == 3
    assert rows["#V#task_assigned_to"]["grounding_count"] == 3


def test_get_predicate_incidence_type_mode_counts_instances_and_groundings() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.concept_relation_service import get_predicate_incidence

    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#alice_student",
            "relationships": {
                "is_an_instance_of": ["#V#sail_student"],
                "#V#member_of_organisation": ["#V#sail"],
                "#V#has_phd_supervisor": ["#V#michael_witbrock"],
            },
        }
    )
    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#bob_student",
            "relationships": {
                "is_an_instance_of": ["#V#sail_student"],
                "#V#member_of_organisation": ["#V#sail"],
                "#V#has_phd_supervisor": ["#V#michael_witbrock"],
            },
        }
    )
    ConceptsRepository.insert_one({"concept_id": "#V#sail", "relationships": {}})
    ConceptsRepository.insert_one(
        {"concept_id": "#V#michael_witbrock", "relationships": {}}
    )

    payload = get_predicate_incidence(
        instance_of="#V#sail_student",
        direct_instances_only=True,
        include_concept_preview=False,
    )

    assert payload["mode"] == "type"
    assert payload["instance_of"] == "#V#sail_student"
    assert payload["type_ids_considered"] == ["#V#sail_student"]
    assert payload["instance_count_considered"] == 2
    assert payload["total_predicates"] == 2
    rows = {
        row["predicate_concept_id"]: row for row in payload.get("predicates") or []
    }
    assert rows["#V#member_of_organisation"]["relation_hit_count"] == 2
    assert rows["#V#member_of_organisation"]["grounding_count"] == 1
    assert rows["#V#member_of_organisation"]["grounded_instance_count"] == 2
    assert rows["#V#has_phd_supervisor"]["relation_hit_count"] == 2
    assert rows["#V#has_phd_supervisor"]["grounding_count"] == 1
    assert rows["#V#has_phd_supervisor"]["grounded_instance_count"] == 2

