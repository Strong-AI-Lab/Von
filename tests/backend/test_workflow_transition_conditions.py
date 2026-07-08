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


def test_context_value_in_condition_is_generic_membership_check() -> None:
    normalised_spec, condition_fn = build_transition_condition(
        {
            "kind": "context_value_in",
            "path": "event.predicate",
            "values": ["#V#profile_predicate", "#V#interest_predicate"],
        }
    )

    assert normalised_spec == {
        "kind": "context_value_in",
        "key": "event.predicate",
        "values": ["#V#profile_predicate", "#V#interest_predicate"],
    }
    assert condition_fn({"event": {"predicate": "#V#interest_predicate"}}) is True
    assert condition_fn({"event": {"predicate": "#V#other_predicate"}}) is False
    assert condition_fn({"event": {}}) is False


def test_context_value_in_condition_rejects_missing_values() -> None:
    with pytest.raises(ValueError) as exc_info:
        build_transition_condition(
            {"kind": "context_value_in", "key": "event.predicate", "values": []}
        )
    assert "workflow_condition_invalid:context_value_in_values_missing" in str(
        exc_info.value
    )


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


def test_executor_can_route_from_structured_failed_action_outputs() -> None:
    condition_spec, condition_fn = build_transition_condition(
        {
            "kind": "context_value_equals",
            "path": "last_action_outputs.result.error_details.provider",
            "value": "external_third_party",
        }
    )
    always_spec, always_fn = build_transition_condition({"kind": "always"})

    definition = WorkflowDefinition(
        workflow_id="#V#failed_output_routing_workflow",
        initial_state="download",
        states={
            "download": WorkflowStateSpec(
                state_id="download",
                actions=(WorkflowActionInvocation(action_id="download.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="fallback",
                        reason="on_failure",
                        condition=condition_fn,
                        condition_spec=condition_spec,
                    ),
                    WorkflowTransitionSpec(
                        to_state="failed",
                        reason="unrecoverable",
                        condition=always_fn,
                        condition_spec=always_spec,
                    ),
                ),
            ),
            "fallback": WorkflowStateSpec(state_id="fallback", terminal=True),
            "failed": WorkflowStateSpec(state_id="failed", terminal=True),
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="download.action",
            handler=lambda _request: WorkflowActionResult(
                status="failed",
                error="external_arxiv_mcp_download_failed",
                outputs={
                    "result": {
                        "success": False,
                        "error_details": {"provider": "external_third_party"},
                    }
                },
            ),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.final_state == "fallback"


def test_executor_prefers_condition_spec_over_stale_compiled_condition() -> None:
    on_failure_spec, _on_failure_fn = build_transition_condition(
        {
            "kind": "context_flag",
            "key": "last_action_failed",
            "expected": True,
        }
    )
    always_spec, always_fn = build_transition_condition({"kind": "always"})

    definition = WorkflowDefinition(
        workflow_id="#V#condition_spec_recovery_next_step_workflow",
        initial_state="recover",
        states={
            "recover": WorkflowStateSpec(
                state_id="recover",
                actions=(WorkflowActionInvocation(action_id="recover.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="failed",
                        reason="on_failure",
                        condition=lambda _context: True,
                        condition_spec=on_failure_spec,
                    ),
                    WorkflowTransitionSpec(
                        to_state="delegate",
                        reason="next_step",
                        condition=always_fn,
                        condition_spec=always_spec,
                    ),
                ),
            ),
            "delegate": WorkflowStateSpec(state_id="delegate", terminal=True),
            "failed": WorkflowStateSpec(state_id="failed", terminal=True),
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="recover.action",
            handler=lambda _request: WorkflowActionResult(status="success"),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.final_state == "delegate"
    assert result.data["last_action_failed"] is False


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


def test_executor_routes_on_break_control_signal_condition() -> None:
    on_break_spec, on_break_fn = build_transition_condition(
        {"kind": "control_signal", "signal": "break", "scope": "main_loop"}
    )
    next_spec, next_fn = build_transition_condition({"kind": "always"})

    definition = WorkflowDefinition(
        workflow_id="#V#condition_spec_break_workflow",
        initial_state="loop_step",
        states={
            "loop_step": WorkflowStateSpec(
                state_id="loop_step",
                actions=(WorkflowActionInvocation(action_id="emit.break"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="loop_exit",
                        reason="on_break",
                        condition=on_break_fn,
                        condition_spec=on_break_spec,
                    ),
                    WorkflowTransitionSpec(
                        to_state="loop_continue",
                        reason="next_step",
                        condition=next_fn,
                        condition_spec=next_spec,
                    ),
                ),
            ),
            "loop_exit": WorkflowStateSpec(state_id="loop_exit", terminal=True),
            "loop_continue": WorkflowStateSpec(state_id="loop_continue", terminal=True),
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="emit.break",
            handler=lambda _request: WorkflowActionResult(
                status="success",
                outputs={"control_signal": "break", "control_scope": "main_loop"},
            ),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.final_state == "loop_exit"


def test_build_transition_condition_rejects_invalid_control_signal() -> None:
    with pytest.raises(ValueError) as exc_info:
        build_transition_condition({"kind": "control_signal", "signal": "pause"})
    assert "workflow_condition_invalid:control_signal_invalid" in str(exc_info.value)


@pytest.mark.parametrize(
    ("spec", "context", "expected"),
    [
        (
            {"kind": "context_exists", "path": "current_item.key", "expected": True},
            {"current_item": {"key": "JVNAUTOSCI-1"}},
            True,
        ),
        (
            {"kind": "context_exists", "path": "current_item.key", "expected": False},
            {"current_item": {}},
            True,
        ),
        (
            {"kind": "context_is_null", "path": "current_item.pull_request_url"},
            {"current_item": {"pull_request_url": None}},
            True,
        ),
        (
            {
                "kind": "context_compare",
                "path": "candidate_issue_count",
                "operator": "gt",
                "value": 0,
            },
            {"candidate_issue_count": 3},
            True,
        ),
        (
            {
                "kind": "context_cardinality",
                "path": "candidate_issues",
                "operator": "gte",
                "value": 2,
            },
            {"candidate_issues": ["A", "B"]},
            True,
        ),
    ],
)
def test_transition_conditions_support_nested_paths_and_comparators(
    spec, context, expected
) -> None:
    _normalised, condition = build_transition_condition(spec)
    assert condition(context) is expected


def test_build_transition_condition_rejects_invalid_compare_operator() -> None:
    with pytest.raises(ValueError) as exc_info:
        build_transition_condition(
            {
                "kind": "context_compare",
                "path": "candidate_issue_count",
                "operator": "approx",
                "value": 1,
            }
        )
    assert "workflow_condition_invalid:compare_operator_invalid" in str(exc_info.value)
