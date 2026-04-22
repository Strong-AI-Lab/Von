from __future__ import annotations

import pytest
from flask import Flask, session


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


def test_relation_previews_and_predicate_incidence_groundings_include_type_ids() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.concept_relation_service import (
        find_relations_with_argument,
        get_predicate_incidence,
    )

    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#michael_witbrock",
            "relationships": {
                "#V#author_of": ["#V#paper_one", "#V#diary_one"],
            },
        }
    )
    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#paper_one",
            "relationships": {"is_an_instance_of": ["#V#scholarly_article"]},
        }
    )
    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#diary_one",
            "relationships": {
                "is_an_instance_of": ["#V#diary_entry_about_michael_witbrocks_work"]
            },
        }
    )

    relation_payload = find_relations_with_argument(
        "#V#michael_witbrock",
        predicate_filter=["#V#author_of"],
        argument_index="subject",
        relation_kind="binary",
    )
    hits = relation_payload.get("hits") or []
    paper_hit = next(
        hit for hit in hits if (hit.get("target_value") or "").strip() == "#V#paper_one"
    )
    diary_hit = next(
        hit for hit in hits if (hit.get("target_value") or "").strip() == "#V#diary_one"
    )
    assert paper_hit["target_concept_preview"]["type_ids"] == ["#V#scholarly_article"]
    assert diary_hit["target_concept_preview"]["type_ids"] == [
        "#V#diary_entry_about_michael_witbrocks_work"
    ]

    incidence_payload = get_predicate_incidence(
        concept_id="#V#michael_witbrock",
        argument_index="subject",
        relation_kind="binary",
    )
    rows = incidence_payload.get("predicates") or []
    author_row = next(
        row for row in rows if row.get("predicate_concept_id") == "#V#author_of"
    )
    sample_groundings = author_row.get("sample_groundings") or []
    grounded_type_ids = {
        grounding.get("concept_id"): grounding.get("type_ids")
        for grounding in sample_groundings
        if isinstance(grounding, dict)
    }
    assert grounded_type_ids["#V#paper_one"] == ["#V#scholarly_article"]
    assert grounded_type_ids["#V#diary_one"] == [
        "#V#diary_entry_about_michael_witbrocks_work"
    ]


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


def test_subject_relation_retrieval_filters_hidden_targets_under_access_control() -> None:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.concept_relation_service import (
        find_relations_with_argument,
        get_predicate_incidence,
    )

    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#alice",
            "relationships": {
                "#V#author_of": ["#V#paper_public", "#V#paper_hidden"],
            },
        }
    )
    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#paper_public",
            "relationships": {
                "is_an_instance_of": ["#V#scholarly_article", "#V#secret_type"]
            },
        }
    )
    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#paper_hidden",
            "relationships": {"specific_to_user": ["#V#other_user"]},
        }
    )
    ConceptsRepository.insert_one({"concept_id": "#V#scholarly_article", "relationships": {}})
    ConceptsRepository.insert_one(
        {
            "concept_id": "#V#secret_type",
            "relationships": {"specific_to_user": ["#V#other_user"]},
        }
    )

    app = Flask(__name__)
    app.secret_key = "test"

    with app.test_request_context("/"):
        session["user_concept_id"] = "#V#alice"
        session["user_email"] = "alice@example.test"

        relation_payload = find_relations_with_argument(
            "#V#alice",
            predicate_filter=["#V#author_of"],
            argument_index="subject",
            relation_kind="binary",
        )
        hits = relation_payload.get("hits") or []

        assert [hit.get("target_value") for hit in hits] == ["#V#paper_public"]
        assert hits[0]["target_concept_preview"]["type_ids"] == ["#V#scholarly_article"]

        incidence_payload = get_predicate_incidence(
            concept_id="#V#alice",
            argument_index="subject",
            relation_kind="binary",
        )
        predicates = incidence_payload.get("predicates") or []
        author_row = next(
            row for row in predicates if row.get("predicate_concept_id") == "#V#author_of"
        )
        groundings = author_row.get("sample_groundings") or []
        assert [grounding.get("concept_id") for grounding in groundings] == ["#V#paper_public"]

