from __future__ import annotations

from bson import ObjectId

from src.backend.services import text_value_service
from src.backend.services.text_relation_read_policy import (
    GENERIC_TEXT_READ_HIDDEN_PREDICATES,
    filter_generic_text_read_rows,
    generic_text_read_predicate_filter,
    is_hidden_from_generic_text_reads,
    text_contains_hidden_generic_predicate,
)


def test_login_email_predicate_and_storage_alias_are_hidden() -> None:
    assert {"#V#hasVonLoginEmail"} == GENERIC_TEXT_READ_HIDDEN_PREDICATES
    assert is_hidden_from_generic_text_reads("#V#hasVonLoginEmail") is True
    assert is_hidden_from_generic_text_reads("hasVonLoginEmail") is True
    assert (
        text_contains_hidden_generic_predicate(
            "Relations: hasVonLoginEmail: private@example.test"
        )
        is True
    )
    assert is_hidden_from_generic_text_reads("#V#has_email") is False


def test_query_filter_excludes_hidden_predicates_without_counting_them() -> None:
    assert generic_text_read_predicate_filter() == {
        "$nin": ["#V#hasVonLoginEmail", "hasVonLoginEmail"]
    }
    assert generic_text_read_predicate_filter(predicate="#V#hasVonLoginEmail") == {
        "$in": []
    }
    assert generic_text_read_predicate_filter(
        predicates=["hasName", "#V#hasVonLoginEmail"]
    ) == {"$in": ["hasName"]}


def test_materialised_row_filter_is_a_defence_in_depth() -> None:
    rows = filter_generic_text_read_rows(
        [
            {"predicate": "hasName", "text": "Visible name"},
            {
                "predicate": "#V#hasVonLoginEmail",
                "text": "private-login@example.test",
            },
        ]
    )

    assert rows == [{"predicate": "hasName", "text": "Visible name"}]


def test_login_identity_mutations_never_reach_generic_workflow_events(
    monkeypatch,
) -> None:
    from src.backend.services import workflow_event_integration_service

    launched: list[dict[str, object]] = []
    monkeypatch.setattr(
        text_value_service,
        "get_event_workflow_integration_enabled",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        workflow_event_integration_service,
        "resolve_event_actor_context",
        lambda: ("#V#actor", "#V#organisation"),
    )
    monkeypatch.setattr(
        workflow_event_integration_service,
        "maybe_launch_vontology_mutation_workflow",
        lambda **kwargs: launched.append(kwargs),
    )

    for event_type in ("text_relation.upserted", "text_relation.deleted"):
        text_value_service._emit_text_relation_mutation_event(
            event_type=event_type,
            subject_concept_id="#V#target",
            relation_id="relation-1",
            predicate="#V#hasVonLoginEmail",
            text="private@example.test",
            lang="en-NZ",
        )

    assert launched == []

    text_value_service._emit_text_relation_mutation_event(
        event_type="text_relation.upserted",
        subject_concept_id="#V#target",
        relation_id="relation-2",
        predicate="hasName",
        text="Visible name",
        lang="en-NZ",
    )
    assert launched[0]["event_payload"]["text"] == "Visible name"


def test_generic_mutation_read_back_hides_login_identity(monkeypatch) -> None:
    from src.backend.services import ontology_mutation_command_service as command

    captured_query = {}
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(
        command.ConceptsRepository,
        "find_one",
        lambda *_args, **_kwargs: {
            "concept_id": "#V#person",
            "relationships": {},
        },
    )

    def find_relations(query):
        captured_query.update(query)
        return [
            {
                "predicate": "#V#hasVonLoginEmail",
                "object_text_id": "secret",
            },
            {"predicate": "hasName", "object_text_id": "visible"},
        ]

    monkeypatch.setattr(command.TextRelationsRepository, "find", find_relations)
    monkeypatch.setattr(
        command.TextValuesRepository,
        "find_one_by_id",
        lambda text_id: {
            "text": ("private@example.test" if text_id == "secret" else "Visible name"),
            "lang": "en-NZ",
        },
    )
    monkeypatch.setattr(
        command,
        "concept_publication_context",
        lambda _concept_id: type(
            "Context",
            (),
            {"to_mapping": lambda self: {"kind": "global"}},
        )(),
    )

    result = command._concept_read_back("#V#person")

    assert captured_query["predicate"] == {
        "$nin": ["#V#hasVonLoginEmail", "hasVonLoginEmail"]
    }
    assert result["text_relations"] == [
        {
            "predicate": "hasName",
            "text": "Visible name",
            "language": "en-NZ",
            "context": {},
        }
    ]
    assert "private@example.test" not in repr(result)


def test_generic_text_rows_and_summary_apply_hidden_query_constraint(
    monkeypatch,
) -> None:
    concept_id = "#V#generic_read_policy_unit_target"
    visible_text_id = ObjectId()
    hidden_text_id = ObjectId()
    relation_queries: list[dict[str, object]] = []

    monkeypatch.setattr(
        text_value_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        text_value_service,
        "can_access_concept",
        lambda _concept_id: True,
    )

    def find_relations(query, **_kwargs):
        relation_queries.append(query)
        predicate_filter = query.get("predicate")
        if predicate_filter == {"$in": []}:
            return []
        assert predicate_filter == {"$nin": ["#V#hasVonLoginEmail", "hasVonLoginEmail"]}
        return [
            {
                "_id": ObjectId(),
                "subject_concept_id": concept_id,
                "predicate": "hasName",
                "object_text_id": visible_text_id,
            }
        ]

    monkeypatch.setattr(
        text_value_service.TextRelationsRepository,
        "find",
        find_relations,
    )

    def find_text_values(query, **_kwargs):
        requested_ids = query.get("_id", {}).get("$in", [])
        assert hidden_text_id not in requested_ids
        return [
            {
                "_id": visible_text_id,
                "text": "Visible name",
                "lang": "en-NZ",
            }
        ]

    monkeypatch.setattr(
        text_value_service.TextValuesRepository,
        "find",
        find_text_values,
    )

    metadata: dict[str, object] = {}
    rows = text_value_service.get_texts_for_concepts(
        [concept_id],
        query_metadata=metadata,
    )[concept_id]
    explicit_hidden = text_value_service.get_texts_for_concept(
        concept_id,
        predicate="#V#hasVonLoginEmail",
    )
    summary = text_value_service.get_text_relations_summary(concept_id)
    hidden_summary = text_value_service.get_text_relations_summary(
        concept_id,
        predicates=["#V#hasVonLoginEmail"],
    )

    assert [(row["predicate"], row["text"]) for row in rows] == [
        ("hasName", "Visible name")
    ]
    assert metadata["raw_relation_count"] == 1
    assert explicit_hidden == []
    assert summary["groups_found"] == 1
    assert summary["total_relations_scanned"] == 1
    assert hidden_summary["groups"] == []
    assert hidden_summary["total_relations_scanned"] == 0
    assert [query["predicate"] for query in relation_queries] == [
        {"$nin": ["#V#hasVonLoginEmail", "hasVonLoginEmail"]},
        {"$in": []},
        {"$nin": ["#V#hasVonLoginEmail", "hasVonLoginEmail"]},
        {"$in": []},
    ]
