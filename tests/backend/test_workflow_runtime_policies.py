from __future__ import annotations

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


def test_approval_gate_routes_to_blocked_state_without_running_action() -> None:
    approval_spec, approval_condition = build_transition_condition(
        {
            "kind": "context_flag",
            "key": "approval_required",
            "expected": True,
        }
    )
    next_spec, next_condition = build_transition_condition({"kind": "always"})

    calls = {"count": 0}

    def _write_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"write_completed": True})

    definition = WorkflowDefinition(
        workflow_id="#V#approval_gate_workflow",
        initial_state="write",
        states={
            "write": WorkflowStateSpec(
                state_id="write",
                actions=(WorkflowActionInvocation(action_id="write.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="blocked",
                        reason="on_approval_required",
                        condition=approval_condition,
                        condition_spec=approval_spec,
                    ),
                    WorkflowTransitionSpec(
                        to_state="done",
                        reason="next_step",
                        condition=next_condition,
                        condition_spec=next_spec,
                    ),
                ),
                metadata={
                    "approval_gate": {
                        "schema_version": "workflow_step_approval_gate.v1",
                        "approval_context_key": "write_approved",
                    }
                },
            ),
            "blocked": WorkflowStateSpec(state_id="blocked", terminal=True),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("blocked", "done"),
    )

    registry = ActionRegistry()
    registry.register(ActionSpec(action_id="write.action", handler=_write_handler))

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.final_state == "blocked"
    assert calls["count"] == 0
    assert result.data.get("approval_required") is True
    assert result.data.get("approval_state") == "blocked"


def test_retry_policy_retries_until_success(monkeypatch) -> None:
    next_spec, next_condition = build_transition_condition({"kind": "always"})
    calls = {"count": 0}
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)

    def _unstable_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        if calls["count"] < 3:
            return WorkflowActionResult(status="failed", error="transient_failure")
        return WorkflowActionResult(outputs={"repair_completed": True})

    definition = WorkflowDefinition(
        workflow_id="#V#retry_policy_workflow",
        initial_state="repair",
        states={
            "repair": WorkflowStateSpec(
                state_id="repair",
                actions=(WorkflowActionInvocation(action_id="repair.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        reason="next_step",
                        condition=next_condition,
                        condition_spec=next_spec,
                    ),
                ),
                metadata={
                    "retry_policy": {
                        "schema_version": "workflow_step_retry_policy.v1",
                        "max_attempts": 3,
                        "backoff_policy": "fixed",
                        "delay_ms": 10,
                        "retry_on_outcomes": ["failure"],
                    }
                },
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )

    registry = ActionRegistry()
    registry.register(ActionSpec(action_id="repair.action", handler=_unstable_handler))
    monkeypatch.setattr("src.backend.workflows.engine.time.sleep", _sleep)

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.final_state == "done"
    assert calls["count"] == 3
    assert slept == [0.01, 0.01]
    retry_events = result.data.get("workflow_retry_events")
    assert isinstance(retry_events, list)
    assert len(retry_events) == 2
    envelopes = result.data.get("workflow_step_result_envelopes")
    assert isinstance(envelopes, list)
    assert [envelope.get("state_attempt") for envelope in envelopes] == [1, 2, 3]


def test_idempotency_policy_reuses_prior_success_outputs() -> None:
    next_spec, next_condition = build_transition_condition({"kind": "always"})
    calls = {"count": 0}

    def _write_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"write_completed": True})

    definition = WorkflowDefinition(
        workflow_id="#V#idempotency_policy_workflow",
        initial_state="write",
        states={
            "write": WorkflowStateSpec(
                state_id="write",
                actions=(WorkflowActionInvocation(action_id="write.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        reason="next_step",
                        condition=next_condition,
                        condition_spec=next_spec,
                    ),
                ),
                metadata={
                    "idempotency_policy": {
                        "schema_version": "workflow_step_idempotency_policy.v1",
                        "key_paths": ["request_id"],
                    }
                },
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )

    registry = ActionRegistry()
    registry.register(ActionSpec(action_id="write.action", handler=_write_handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)

    shared_context = {"request_id": "req-1"}
    first_result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data=shared_context,
    )
    second_result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data=shared_context,
    )

    assert first_result.completed is True
    assert second_result.completed is True
    assert calls["count"] == 1
    assert second_result.data.get("write_completed") is True
    idempotency_events = second_result.data.get("workflow_idempotency_events")
    assert isinstance(idempotency_events, list)
    assert any(event.get("status") == "idempotent_reuse" for event in idempotency_events)
