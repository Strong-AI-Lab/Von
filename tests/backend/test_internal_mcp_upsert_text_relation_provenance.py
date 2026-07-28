"""Regression tests for upsert_text_relation provenance passthrough."""

from __future__ import annotations

from src.backend.integrations.internal_mcp import catalogue


def test_upsert_text_relation_passes_provenance(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_upsert_text_for_concept(**kwargs):
        captured.update(kwargs)
        return {
            "text_value_id": "tv-1",
            "relation_id": "rel-1",
            "relation_created": True,
        }

    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_text_for_concept",
        _fake_upsert_text_for_concept,
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )

    result = catalogue._upsert_text_relation(
        concept_id="#V#example",
        predicate="hasDescription",
        text="Example text",
        provenance={"source": "unit_test", "turn_id": "turn-123"},
    )

    assert result.get("success") is True
    assert captured.get("subject_concept_id") == "#V#example"
    assert captured.get("predicate") == "hasDescription"
    assert captured.get("provenance") == {"source": "unit_test", "turn_id": "turn-123"}


def test_upsert_text_relation_commit_then_raise_is_indeterminate(monkeypatch):
    committed: list[str] = []

    def _commit_then_raise(**_kwargs):
        committed.append("relation-written")
        raise RuntimeError("lost acknowledgement after commit")

    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_text_for_concept",
        _commit_then_raise,
    )

    result = catalogue._upsert_text_relation(
        concept_id="#V#example",
        predicate="hasDescription",
        text="Possibly committed text",
    )

    assert committed == ["relation-written"]
    assert result["success"] is False
    assert result["error_code"] == "effect_outcome_unknown"
    assert result["effect_status"] == "indeterminate"
    assert result["mutation_outcome"] == "unknown"
    assert result["changed"] is None
    assert result["retryable"] is False
    assert result["recovery_affordances"] == [
        {"action_type": "inspect_operation_state_before_retry"}
    ]


def test_upsert_text_relation_typed_predicate_rejection_remains_definite(
    monkeypatch,
):
    from src.backend.services.text_relation_predicate_validation_service import (
        TextRelationPredicateResolutionError,
    )

    def _reject_predicate(_predicate):
        raise TextRelationPredicateResolutionError(
            "predicate_concept_not_found",
            "The predicate is not represented.",
        )

    monkeypatch.setattr(
        "src.backend.services.text_relation_predicate_validation_service."
        "resolve_text_relation_predicate_for_write",
        _reject_predicate,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_text_for_concept",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("typed rejection must happen before the write")
        ),
    )

    result = catalogue._upsert_text_relation(
        concept_id="#V#example",
        predicate="#V#missing_predicate",
        text="Unwritten text",
    )

    assert result["success"] is False
    assert result["error_code"] == "predicate_concept_not_found"
    assert "mutation_outcome" not in result
    assert result.get("effect_status") != "indeterminate"


def test_upsert_text_relation_unexpected_pre_dispatch_failure_remains_definite(
    monkeypatch,
):
    monkeypatch.setattr(
        "src.backend.services.text_relation_predicate_validation_service."
        "resolve_text_relation_predicate_for_write",
        lambda _predicate: (_ for _ in ()).throw(
            RuntimeError("predicate store unavailable")
        ),
    )

    result = catalogue._upsert_text_relation(
        concept_id="#V#example",
        predicate="hasDescription",
        text="Unwritten text",
    )

    assert result["success"] is False
    assert result["error_code"] == "exception"
    assert "mutation_outcome" not in result
    assert result.get("effect_status") != "indeterminate"
