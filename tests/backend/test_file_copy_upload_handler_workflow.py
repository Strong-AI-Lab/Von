from __future__ import annotations

import json

from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.file_copy_upload_handler_workflow import (
    FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
    FILE_COPY_UPLOAD_ROUTE_OUTCOME_PREDICATE,
    build_file_copy_upload_handler_workflow,
    register_file_copy_upload_handler_actions,
)


def test_build_file_copy_upload_handler_workflow_definition_shape() -> None:
    workflow = build_file_copy_upload_handler_workflow()

    assert workflow.workflow_id == FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID
    assert workflow.initial_state == "classify"
    assert set(workflow.states.keys()) == {
        "classify",
        "specialised",
        "specialised_failed",
        "interpret",
        "noop",
        "fail_closed",
        "record_outcome",
        "complete",
        "failed",
    }
    assert workflow.termination_states == ("complete", "failed")
    assert [a.action_id for a in workflow.states["fail_closed"].actions] == [
        "file_copy_upload.mark_fail_closed"
    ]
    assert [a.action_id for a in workflow.states["record_outcome"].actions] == [
        "file_copy_upload.persist_route_outcome"
    ]


def test_register_file_copy_upload_handler_actions_registers_expected_ids() -> None:
    registry = ActionRegistry()
    register_file_copy_upload_handler_actions(registry)
    assert set(registry.all_action_ids()) == {
        "file_copy_upload.mark_noop",
        "file_copy_upload.mark_fail_closed",
        "file_copy_upload.mark_specialised_failure",
        "file_copy_upload.persist_route_outcome",
    }


def test_mark_fail_closed_action_sets_fail_closed_outputs() -> None:
    registry = ActionRegistry()
    register_file_copy_upload_handler_actions(registry)
    context: dict[str, object] = {"upload_route_reasons": ["confidence_below_threshold"]}
    result = registry.execute(
        "file_copy_upload.mark_fail_closed",
        inputs={},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["upload_effective_route_mode"] == "fail_closed"
    assert result.outputs["upload_fail_closed_applied"] is True
    assert result.outputs["upload_route_success"] is False


def test_persist_route_outcome_writes_singleton_text_relation(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def _fake_upsert_singleton_text_relation(**kwargs):
        calls.append(dict(kwargs))
        return {"kept_relation_id": "rel-2", "replaced_count": 1}

    monkeypatch.setattr(
        "src.backend.workflows.durable.file_copy_upload_handler_workflow.upsert_singleton_text_relation",
        _fake_upsert_singleton_text_relation,
    )

    registry = ActionRegistry()
    register_file_copy_upload_handler_actions(registry)
    context: dict[str, object] = {
        "file_copy_concept_id": "#V#file_copy_77",
        "classification_version": "file_copy_upload_classification.v1",
        "upload_route_key": "scholarly",
        "upload_route_mode": "specialised",
        "upload_route_confidence": 0.83,
        "upload_minimum_mutation_confidence": 0.9,
        "upload_mutation_route": True,
        "upload_fail_closed": True,
        "upload_route_reasons": ["mutation_route_confidence_below_threshold"],
        "upload_route_decision_persisted": True,
        "upload_target_workflow_id": "#V#integration_scholarly_paper_representation_workflow",
        "upload_target_workflow_available": True,
        "upload_effective_route_mode": "fail_closed",
    }
    result = registry.execute(
        "file_copy_upload.persist_route_outcome",
        inputs={},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["upload_route_outcome_persisted"] is True
    assert result.outputs["upload_effective_route_mode"] == "fail_closed"
    assert result.outputs["upload_route_success"] is False
    assert len(calls) == 1
    assert calls[0]["subject_concept_id"] == "#V#file_copy_77"
    assert calls[0]["predicate"] == FILE_COPY_UPLOAD_ROUTE_OUTCOME_PREDICATE
    payload = json.loads(str(calls[0]["text"]))
    assert payload["effective_route_mode"] == "fail_closed"
    assert payload["route_success"] is False
