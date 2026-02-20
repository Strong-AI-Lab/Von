"""Declarative workflow transition condition tests."""

from __future__ import annotations

import pytest

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
    build_transition_condition,
)


def test_build_transition_condition_rejects_invalid_context_flag() -> None:
    with pytest.raises(ValueError) as exc_info:
        build_transition_condition({"kind": "context_flag"})
    assert "workflow_condition_invalid:context_flag_key_missing" in str(exc_info.value)


@pytest.mark.parametrize(
    ("status", "reason", "context_key"),
    [
        ("failed", "on_failure", "last_action_failed"),
        ("unknown", "on_unknown", "last_action_unknown"),
    ],
)
def test_executor_routes_failure_and_unknown_via_condition_spec(
    status: str,
    reason: str,
    context_key: str,
) -> None:
    normalised_spec, condition_fn = build_transition_condition(
        {
            "kind": "context_flag",
            "key": context_key,
            "expected": True,
        }
    )
    always_spec, always_fn = build_transition_condition({"kind": "always"})

    definition = WorkflowDefinition(
        workflow_id="#V#condition_spec_recovery_workflow",
        initial_state="probe",
        states={
            "probe": WorkflowStateSpec(
                state_id="probe",
                actions=(WorkflowActionInvocation(action_id="probe.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="recover",
                        reason=reason,
                        condition=condition_fn,
                        condition_spec=normalised_spec,
                    ),
                    WorkflowTransitionSpec(
                        to_state="done",
                        reason="next_step",
                        condition=always_fn,
                        condition_spec=always_spec,
                    ),
                ),
            ),
            "recover": WorkflowStateSpec(state_id="recover", terminal=True),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="probe.action",
            handler=lambda _request: WorkflowActionResult(status=status),
        )
    )

    executor = WorkflowExecutor(registry=registry, max_transitions=5)
    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.final_state == "recover"
    assert result.data.get(context_key) is True


@pytest.mark.parametrize(
    ("transition_result", "expected_state"),
    [(True, "yes"), (False, "no")],
)
def test_executor_evaluates_transition_result_truth_condition_spec(
    transition_result: bool,
    expected_state: str,
) -> None:
    on_true_spec, on_true_fn = build_transition_condition(
        {"kind": "transition_result_truth", "expected": True}
    )
    on_false_spec, on_false_fn = build_transition_condition(
        {"kind": "transition_result_truth", "expected": False}
    )

    definition = WorkflowDefinition(
        workflow_id="#V#condition_spec_truth_workflow",
        initial_state="gate",
        states={
            "gate": WorkflowStateSpec(
                state_id="gate",
                actions=(WorkflowActionInvocation(action_id="gate.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="yes",
                        reason="on_true",
                        condition=on_true_fn,
                        condition_spec=on_true_spec,
                    ),
                    WorkflowTransitionSpec(
                        to_state="no",
                        reason="on_false",
                        condition=on_false_fn,
                        condition_spec=on_false_spec,
                    ),
                ),
            ),
            "yes": WorkflowStateSpec(state_id="yes", terminal=True),
            "no": WorkflowStateSpec(state_id="no", terminal=True),
        },
    )

    registry = ActionRegistry()

    def _gate_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="success",
            outputs={"transition_result": transition_result},
        )

    registry.register(ActionSpec(action_id="gate.action", handler=_gate_handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)
    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.final_state == expected_state
