from __future__ import annotations

from typing import Any

from bson import ObjectId

from src.backend.services import text_relation_resolution_service as service


def _install_lookup_rows(
    monkeypatch,
    *,
    relation_subjects: list[str],
    concept_rows: list[dict[str, Any]],
    accessible_ids: set[str] | None = None,
) -> tuple[ObjectId, dict[str, Any]]:
    text_value_id = ObjectId()
    captured: dict[str, Any] = {}

    def find_text_values(query, **kwargs):
        captured["text_query"] = query
        captured["text_limit"] = kwargs.get("limit")
        return [{"_id": text_value_id}]

    def find_text_relations(query, **kwargs):
        captured["relation_query"] = query
        captured["relation_limit"] = kwargs.get("limit")
        return [{"subject_concept_id": concept_id} for concept_id in relation_subjects]

    monkeypatch.setattr(service.TextValuesRepository, "find", find_text_values)
    monkeypatch.setattr(
        service.TextRelationsRepository,
        "find",
        find_text_relations,
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda candidate_ids: (
            set(candidate_ids)
            if accessible_ids is None
            else set(candidate_ids) & accessible_ids
        ),
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: list(concept_rows),
    )
    monkeypatch.setattr(service, "is_code_concept_id", lambda _concept_id: False)
    return text_value_id, captured


def test_exact_predicate_and_text_resolve_one_visible_existing_concept(
    monkeypatch,
) -> None:
    concept_id = "#V#person_alice"
    text_value_id, captured = _install_lookup_rows(
        monkeypatch,
        relation_subjects=[concept_id],
        concept_rows=[{"concept_id": concept_id, "relationships": {}}],
    )

    result = service.resolve_concept_by_text_relation(
        predicate="#V#has_email",
        text="alice@example.test",
    )

    assert result["status"] == "resolved"
    assert result["resolved_concept_id"] == concept_id
    assert result["resolution_complete"] is True
    assert result["candidate_count"] == 1
    assert result["candidates"] == []
    assert captured["text_query"] == {"text": "alice@example.test"}
    assert captured["relation_query"] == {
        "object_text_id": {"$in": [text_value_id, str(text_value_id)]},
        "predicate": "#V#has_email",
    }


def test_login_email_predicate_is_not_available_to_generic_text_resolution(
    monkeypatch,
) -> None:
    def unexpected_query(*_args: Any, **_kwargs: Any):
        raise AssertionError("hidden predicates must fail before repository lookup")

    monkeypatch.setattr(service.TextValuesRepository, "find", unexpected_query)
    monkeypatch.setattr(service.TextRelationsRepository, "find", unexpected_query)

    result = service.resolve_concept_by_text_relation(
        predicate="#V#hasVonLoginEmail",
        text="private-login@example.test",
    )

    assert result["success"] is True
    assert result["status"] == "not_found"
    assert result["resolved_concept_id"] is None
    assert result["candidate_count"] == 0
    assert result["candidates"] == []
    assert result["resolution_complete"] is True


def test_multiple_visible_matches_are_ambiguous_and_deterministic(
    monkeypatch,
) -> None:
    concept_ids = ["#V#person_c", "#V#person_a", "#V#person_b"]
    _install_lookup_rows(
        monkeypatch,
        relation_subjects=concept_ids,
        concept_rows=[
            {"concept_id": concept_id, "relationships": {}}
            for concept_id in concept_ids
        ],
    )

    result = service.resolve_concept_by_text_relation(
        predicate="#V#has_email",
        text="shared@example.test",
        max_results=2,
    )

    assert result["status"] == "ambiguous"
    assert result["resolved_concept_id"] is None
    assert result["candidate_count"] == 3
    assert result["candidate_count_is_lower_bound"] is False
    assert result["candidates_truncated"] is True
    assert result["candidates"] == [
        {"concept_id": "#V#person_a"},
        {"concept_id": "#V#person_b"},
    ]


def test_hidden_and_stale_subjects_are_not_returned_or_counted(monkeypatch) -> None:
    visible_id = "#V#person_visible"
    _install_lookup_rows(
        monkeypatch,
        relation_subjects=[
            visible_id,
            "#V#person_hidden",
            "#V#person_stale",
        ],
        accessible_ids={visible_id, "#V#person_stale"},
        concept_rows=[{"concept_id": visible_id, "relationships": {}}],
    )

    result = service.resolve_concept_by_text_relation(
        predicate="#V#has_email",
        text="visible@example.test",
    )

    assert result["status"] == "resolved"
    assert result["resolved_concept_id"] == visible_id
    assert result["candidate_count"] == 1
    assert "hidden" not in repr(result)
    assert "stale" not in repr(result)


def test_instance_of_restriction_filters_after_exact_actor_visible_lookup(
    monkeypatch,
) -> None:
    person_id = "#V#researcher_alice"
    organisation_id = "#V#organisation_alice"
    _install_lookup_rows(
        monkeypatch,
        relation_subjects=[person_id, organisation_id],
        concept_rows=[
            {
                "concept_id": person_id,
                "relationships": {"is_an_instance_of": "#V#researcher"},
            },
            {
                "concept_id": organisation_id,
                "relationships": {"is_an_instance_of": ["#V#organisation"]},
            },
        ],
    )
    monkeypatch.setattr(
        service,
        "get_vontology_node_and_descendant_ids",
        lambda concept_id: [concept_id, "#V#researcher"],
    )

    result = service.resolve_concept_by_text_relation(
        predicate="#V#has_email",
        text="alice@example.test",
        instance_of="#V#person",
    )

    assert result["status"] == "resolved"
    assert result["resolved_concept_id"] == person_id
    assert result["instance_of"] == "#V#person"


def test_saturated_scan_never_resolves_the_single_visible_partial_match(
    monkeypatch,
) -> None:
    visible_id = "#V#person_visible"
    _install_lookup_rows(
        monkeypatch,
        relation_subjects=[visible_id, "#V#hidden_a", "#V#hidden_b"],
        accessible_ids={visible_id},
        concept_rows=[{"concept_id": visible_id, "relationships": {}}],
    )
    monkeypatch.setattr(service, "_MAX_EXACT_TEXT_RELATIONS", 2)

    result = service.resolve_concept_by_text_relation(
        predicate="#V#has_email",
        text="bounded@example.test",
    )

    assert result["status"] == "ambiguous"
    assert result["resolved_concept_id"] is None
    assert result["resolution_complete"] is False
    assert result["candidate_count_is_lower_bound"] is True
    assert result["incomplete_stages"] == ["text_relations"]
    assert result["candidates"] == [{"concept_id": visible_id}]


def test_max_results_must_be_between_one_and_twenty_without_querying(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service.TextValuesRepository,
        "find",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid input must not query")
        ),
    )

    result = service.resolve_concept_by_text_relation(
        predicate="#V#has_email",
        text="alice@example.test",
        max_results=21,
    )

    assert result["success"] is False
    assert result["status"] == "not_found"
    assert result["error_code"] == "invalid_parameter"
    assert result["resolution_complete"] is False
