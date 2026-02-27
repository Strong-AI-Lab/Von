from __future__ import annotations

from src.backend.workflows.action_registry import ActionRegistry
from src.backend.workflows.durable.file_copy_interpretation_workflow import (
    FILE_COPY_INTERPRETATION_WORKFLOW_ID,
    build_file_copy_interpretation_workflow,
    get_file_copy_interpretation_workflow_registration,
    register_file_copy_interpretation_actions,
)


def test_build_file_copy_interpretation_workflow_definition_shape() -> None:
    workflow = build_file_copy_interpretation_workflow()

    assert workflow.workflow_id == FILE_COPY_INTERPRETATION_WORKFLOW_ID
    assert workflow.initial_state == "interpret"
    assert set(workflow.states.keys()) == {"interpret", "index", "complete", "failed"}
    assert workflow.termination_states == ("complete", "failed")

    interpret_actions = [action.action_id for action in workflow.states["interpret"].actions]
    assert interpret_actions == ["interpret_file_copy"]
    index_actions = [action.action_id for action in workflow.states["index"].actions]
    assert index_actions == ["index_file_copy"]


def test_get_file_copy_interpretation_workflow_registration() -> None:
    registration = get_file_copy_interpretation_workflow_registration()

    assert registration.workflow_id == FILE_COPY_INTERPRETATION_WORKFLOW_ID
    assert registration.source == "built_in"
    assert registration.definition.workflow_id == FILE_COPY_INTERPRETATION_WORKFLOW_ID


def test_register_file_copy_interpretation_actions_is_noop() -> None:
    registry = ActionRegistry()
    register_file_copy_interpretation_actions(registry)
    assert registry.all_action_ids() == []
