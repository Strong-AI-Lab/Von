from __future__ import annotations

from src.backend.services.thinking_semantic_projection_service import (
    SEMANTIC_OPERATION_SCHEMA_VERSION,
    build_semantic_operation_projection,
)


def _relation_projection(**overrides):
    arguments = {
        "source_id": "#V#nathan_young_doctoral_candidature_situation",
        "predicate": "#V#has_doctoral_supervisor",
        "target": "#V#robert_amor",
    }
    arguments.update(overrides.pop("arguments", {}))
    values = {
        "operation_id": "call-relationship",
        "capability_name": "add_relationship",
        "execution_method": "add_relationship",
        "capability_kind": "registered_tool",
        "arguments": arguments,
        "lifecycle_status": "running",
    }
    values.update(overrides)
    return build_semantic_operation_projection(**values)


def test_relation_projection_has_role_labelled_arguments_and_readable_start() -> None:
    projection = _relation_projection()

    assert projection["schema_version"] == SEMANTIC_OPERATION_SCHEMA_VERSION
    assert [item["role"] for item in projection["arguments"]] == [
        "subject",
        "predicate",
        "object",
    ]
    assert projection["relation"]["predicate"]["concept_id"] == (
        "#V#has_doctoral_supervisor"
    )
    assert projection["summary"] == (
        "Add Relationship: Subject: Nathan Young Doctoral Candidature Situation; "
        "Relation: Has Doctoral Supervisor; Object: Robert Amor (in progress)."
    )


def test_nested_predicate_reference_and_unchanged_outcome_are_truthful() -> None:
    projection = _relation_projection(
        arguments={
            "predicate": None,
            "predicate_ref": {"concept_id": "#V#has_doctoral_supervisor"},
        },
        lifecycle_status="succeeded",
        success=True,
        result={"effect_status": "succeeded", "changed": False},
    )

    assert projection["relation"]["predicate"]["concept_id"] == (
        "#V#has_doctoral_supervisor"
    )
    assert projection["outcome"]["changed"] is False
    assert projection["summary"] == (
        "Add Relationship confirmed no change was needed: "
        "Subject: Nathan Young Doctoral Candidature Situation; "
        "Relation: Has Doctoral Supervisor; Object: Robert Amor."
    )


def test_relation_operation_wording_preserves_remove_partial_and_unknown() -> None:
    removed = _relation_projection(
        capability_name="remove_relationship",
        execution_method="remove_relationship",
        lifecycle_status="succeeded",
        success=True,
        result={"effect_status": "succeeded", "changed": True},
    )
    partial = _relation_projection(
        capability_name="preview_remove_relationship",
        execution_method="preview_remove_relationship",
        lifecycle_status="partial",
        success=False,
        result={"effect_status": "partial", "changed": True},
    )
    unknown = _relation_projection(
        lifecycle_status="indeterminate",
        success=False,
        result={
            "effect_status": "indeterminate",
            "mutation_outcome": "unknown",
        },
    )

    assert removed["summary"].startswith("Remove Relationship reported a change:")
    assert partial["summary"].startswith(
        "Preview Remove Relationship completed only partially:"
    )
    assert unknown["summary"].startswith(
        "The outcome of Add Relationship could not be verified:"
    )
    assert "Represented" not in " ".join(
        (removed["summary"], partial["summary"], unknown["summary"])
    )


def test_literal_text_preserves_case_and_punctuation() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-text",
        capability_name="upsert_text_relation",
        execution_method="upsert_text_relation",
        capability_kind="registered_tool",
        arguments={
            "concept_id": "#V#robert_amor",
            "predicate": "#V#has_professional_title",
            "text": "Professor of Computer Science (emeritus).",
        },
        lifecycle_status="succeeded",
        success=True,
        result={"effect_status": "succeeded", "changed": False},
    )

    assert projection["relation"]["object"]["display"] == (
        "Professor of Computer Science (emeritus)."
    )
    assert (
        "Value: “Professor of Computer Science (emeritus).”"
        in projection["summary"]
    )
    assert "Professor Of Computer Science" not in projection["summary"]


def test_projection_bounds_values_and_does_not_invent_a_relation() -> None:
    projection = build_semantic_operation_projection(
        operation_id="call-generic",
        capability_name="create_concepts",
        execution_method="create_concepts",
        capability_kind="registered_tool",
        arguments={
            "name": "x" * 1_000,
            "unused_1": "one",
            "unused_2": "two",
        },
        lifecycle_status="succeeded",
        success=True,
        result={"effect_status": "succeeded", "changed": True},
    )

    assert "relation" not in projection
    assert len(projection["arguments"]) == 1
    assert len(projection["arguments"][0]["value"]) <= 240
    assert projection["summary"] == "Finished Create Concepts."
