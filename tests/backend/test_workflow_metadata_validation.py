"""Runtime validation tests for workflow metadata (WS7 / JVNAUTOSCI-1092)."""

from __future__ import annotations

from typing import Dict

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from src.backend.workflows.trace_model import WorkflowExecutionTrace


def _build_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="#V#metadata_validation_workflow",
        initial_state="plan",
        states={
            "plan": WorkflowStateSpec(
                state_id="plan",
                actions=(WorkflowActionInvocation(action_id="task.create"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=lambda _ctx: True,
                        reason="next_step",
                    ),
                ),
                metadata={
                    "preconditions": ["#V#user_authenticated"],
                    "effects": ["#V#task_created"],
                    "reads_variables": ["user_id"],
                    "writes_variables": ["task_id"],
                },
            ),
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
            ),
        },
    )


def test_metadata_validation_positive_path_records_trace_events() -> None:
    definition = _build_definition()
    registry = ActionRegistry()

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            outputs={
                "task_id": "task-1",
                "task_created": True,
            }
        )

    registry.register(ActionSpec(action_id="task.create", handler=handler))

    trace = WorkflowExecutionTrace(workflow_id=definition.workflow_id)
    executor = WorkflowExecutor(registry=registry, max_transitions=5)
    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "user_authenticated": True,
            "user_id": "user-1",
        },
        trace=trace,
    )

    assert result.completed is True
    assert result.final_state == "done"
    assert result.error is None
    assert result.data.get("task_id") == "task-1"
    events = result.data.get("workflow_metadata_validation_events")
    assert isinstance(events, list)
    assert len(events) == 2
    assert events[0]["phase"] == "pre_action"
    assert events[1]["phase"] == "post_action"
    assert events[0]["ok"] is True
    assert events[1]["ok"] is True
    trace_verdicts = [
        transition.get("verdict")
        for transition in trace.state_transitions
        if isinstance(transition.get("verdict"), dict)
        and transition["verdict"].get("status") == "metadata_validation"
    ]
    assert len(trace_verdicts) == 2


def test_metadata_validation_blocks_unsatisfied_precondition() -> None:
    definition = _build_definition()
    registry = ActionRegistry()
    calls: Dict[str, int] = {"count": 0}

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"task_id": "task-1", "task_created": True})

    registry.register(ActionSpec(action_id="task.create", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "user_authenticated": False,
            "user_id": "user-1",
        },
    )

    assert result.completed is False
    assert result.final_state == "plan"
    assert result.error is not None
    assert result.error.startswith(
        "metadata_validation_failed:metadata_precondition_unsatisfied:plan:"
    )
    assert calls["count"] == 0


def test_metadata_validation_blocks_missing_write_variable() -> None:
    definition = _build_definition()
    registry = ActionRegistry()

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        # Deliberately omit task_id to trigger writes_variables validation failure.
        return WorkflowActionResult(outputs={"task_created": True})

    registry.register(ActionSpec(action_id="task.create", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "user_authenticated": True,
            "user_id": "user-1",
        },
    )

    assert result.completed is False
    assert result.final_state == "plan"
    assert result.error is not None
    assert result.error.startswith(
        "metadata_validation_failed:metadata_write_variable_missing:plan:"
    )
