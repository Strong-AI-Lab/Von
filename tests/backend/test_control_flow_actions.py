from __future__ import annotations

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import register_control_flow_actions
from src.backend.workflows.execution_contracts import (
    WORKFLOW_CONTROL_ACTION_BREAK_ID,
    WORKFLOW_CONTROL_ACTION_FORK_ID,
    WORKFLOW_CONTROL_ACTION_JOIN_ID,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)


def _child_definition(workflow_id: str, action_id: str) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id=action_id),),
                terminal=True,
            )
        },
        termination_states=("start",),
    )


def test_break_action_emits_control_signal() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)

    context: dict[str, object] = {}
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_BREAK_ID,
        inputs={"loop_scope_id": "main"},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs.get("control_signal") == "break"
    assert result.outputs.get("control_scope") == "main"


def test_fork_join_actions_execute_and_merge_branch_results() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.emit_one",
            handler=lambda _request: WorkflowActionResult(outputs={"value_one": 1}),
        )
    )
    registry.register(
        ActionSpec(
            action_id="child.emit_two",
            handler=lambda _request: WorkflowActionResult(outputs={"value_two": 2}),
        )
    )
    definitions = {
        "#V#child_one": _child_definition("#V#child_one", "child.emit_one"),
        "#V#child_two": _child_definition("#V#child_two", "child.emit_two"),
    }
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )

    context: dict[str, object] = {}
    fork_result = registry.execute(
        WORKFLOW_CONTROL_ACTION_FORK_ID,
        inputs={
            "fork_id": "main_fork",
            "failure_policy": "collect_errors",
            "branches": [
                {"branch_id": "a", "workflow_id": "#V#child_one"},
                {"branch_id": "b", "workflow_id": "#V#child_two"},
            ],
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert fork_result.status == "success"
    assert fork_result.outputs.get("fork_success_count") == 2
    join_result = registry.execute(
        WORKFLOW_CONTROL_ACTION_JOIN_ID,
        inputs={"fork_id": "main_fork"},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert join_result.status == "success"
    assert join_result.outputs.get("fork_joined") is True
    merged = join_result.outputs.get("result")
    assert merged == {"value_one": 1, "value_two": 2}


def test_join_action_fails_without_matching_fork() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_JOIN_ID,
        inputs={"fork_id": "missing"},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert "join_without_matching_fork" in str(result.error or "")
