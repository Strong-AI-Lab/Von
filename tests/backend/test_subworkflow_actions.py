from __future__ import annotations

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.subworkflow_actions import register_subworkflow_actions
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from src.backend.workflows.subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
)
from src.backend.workflows.trace_model import WorkflowExecutionTrace


def _always_true(_context):
    return True


def _child_success_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="#V#child_success",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="child.emit"),),
                terminal=True,
            ),
        },
        termination_states=("start",),
    )


def _child_failure_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="#V#child_failure",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="child.fail"),),
                terminal=True,
            ),
        },
        termination_states=("start",),
    )


def test_subworkflow_action_executes_child_and_emits_trace_chain() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.emit",
            handler=lambda request: WorkflowActionResult(
                status="success",
                outputs={"answer": request.data.get("value")},
            ),
        )
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            _child_success_definition() if workflow_id == "#V#child_success" else None
        ),
    )

    parent = WorkflowDefinition(
        workflow_id="#V#parent_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                        inputs={
                            "workflow_id": "#V#child_success",
                            "value": 41,
                            "__parent_workflow_id": "#V#parent_workflow",
                            "__parent_state_id": "start",
                        },
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=_always_true,
                        reason="next_step",
                        condition_spec={"kind": "always"},
                    ),
                ),
                metadata={
                    "tool_output_context_mappings": [
                        {
                            "tool_output_field": "answer",
                            "context_key": "child_answer",
                        }
                    ]
                },
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )
    trace = WorkflowExecutionTrace(workflow_id="#V#parent_workflow")
    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
        trace=trace,
    )

    assert result.completed is True
    assert result.data["child_answer"] == 41
    subworkflow_events = trace.metadata.get("subworkflow_invocations")
    assert isinstance(subworkflow_events, list)
    assert len(subworkflow_events) == 1
    event = subworkflow_events[0]
    assert event["parent_workflow_id"] == "#V#parent_workflow"
    assert event["child_workflow_id"] == "#V#child_success"
    assert event["child_completed"] is True
    assert event["invocation_chain"] == ["#V#parent_workflow", "#V#child_success"]


def test_subworkflow_action_propagates_child_failure_by_default() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.fail",
            handler=lambda _request: WorkflowActionResult(
                status="failed",
                error="child_boom",
            ),
        )
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            _child_failure_definition() if workflow_id == "#V#child_failure" else None
        ),
    )

    parent = WorkflowDefinition(
        workflow_id="#V#parent_failure",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                        inputs={
                            "workflow_id": "#V#child_failure",
                            "__parent_workflow_id": "#V#parent_failure",
                            "__parent_state_id": "start",
                        },
                    ),
                ),
            )
        },
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is False
    assert result.error == "subworkflow_failed:#V#child_failure:child_boom"


def test_subworkflow_action_can_capture_child_failure() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.fail",
            handler=lambda _request: WorkflowActionResult(
                status="failed",
                error="child_boom",
            ),
        )
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            _child_failure_definition() if workflow_id == "#V#child_failure" else None
        ),
    )

    parent = WorkflowDefinition(
        workflow_id="#V#parent_capture",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                        inputs={
                            "workflow_id": "#V#child_failure",
                            "failure_mode": WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
                            "__parent_workflow_id": "#V#parent_capture",
                            "__parent_state_id": "start",
                        },
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=_always_true,
                        reason="next_step",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )
    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.data["child_workflow_failed"] is True
    assert result.data["subworkflow_error"] == "subworkflow_failed:#V#child_failure:child_boom"


def test_subworkflow_action_rejects_recursive_self_invocation() -> None:
    registry = ActionRegistry()
    register_subworkflow_actions(
        registry,
        definition_loader=lambda _workflow_id: _child_success_definition(),
    )

    context: dict[str, object] = {}
    execution = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#loop",
            "__parent_workflow_id": "#V#loop",
            "__parent_state_id": "start",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )

    assert execution.status == "failed"
    assert "subworkflow_recursive_invocation" in str(execution.error or "")


def test_subworkflow_action_is_idempotent_for_same_inputs() -> None:
    registry = ActionRegistry()

    def _emit_value(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="success",
            outputs={"answer": request.data.get("value")},
        )

    registry.register(ActionSpec(action_id="child.emit", handler=_emit_value))
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            _child_success_definition() if workflow_id == "#V#child_success" else None
        ),
    )

    context_a: dict[str, object] = {}
    context_b: dict[str, object] = {}
    first = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_success",
            "value": "stable",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context=context_a,
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )
    second = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_success",
            "value": "stable",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context=context_b,
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )

    assert first.status == "success"
    assert second.status == "success"
    assert first.outputs["result"]["answer"] == "stable"
    assert second.outputs["result"]["answer"] == "stable"
    assert first.outputs["result"]["answer"] == second.outputs["result"]["answer"]
