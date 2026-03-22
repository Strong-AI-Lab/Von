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
    approval_events = result.data.get("workflow_approval_gate_events")
    assert isinstance(approval_events, list)
    assert approval_events
    assert approval_events[0]["type"] == "mutation_guardrail"
    assert approval_events[0]["guardrail_surface"] == "workflow_approval_gate"
    assert approval_events[0]["decision"] == "approval_required"


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


def test_completion_gate_blocks_false_terminal_success_and_records_plan_state() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#completion_gate_workflow",
        initial_state="dispatch",
        states={
            "dispatch": WorkflowStateSpec(
                state_id="dispatch",
                actions=(WorkflowActionInvocation(action_id="dispatch.action"),),
                terminal=True,
                metadata={
                    "checkpoint_policy": {
                        "schema_version": "workflow_step_checkpoint_policy.v1",
                        "plan_item_updates": [
                            {"item_id": "dispatch", "status": "done"}
                        ],
                        "progress_message": "Dispatching",
                    }
                },
            ),
        },
        termination_states=("dispatch",),
        metadata={
            "plan_state_policy": {
                "schema_version": "workflow_plan_state_policy.v1",
                "plan_items": ["dispatch", "finalise"],
            },
            "completion_gate": {
                "schema_version": "workflow_completion_gate.v1",
                "required_done_plan_items": ["dispatch", "finalise"],
                "required_context_keys": ["dispatch.completed"],
            },
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="dispatch.action",
            handler=lambda _request: WorkflowActionResult(outputs={}),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is False
    assert result.error is not None
    assert result.error.startswith("workflow_completion_gate_unmet")
    plan_state = result.data.get("workflow_plan_state")
    assert isinstance(plan_state, dict)
    assert plan_state["items"]["dispatch"]["status"] == "done"
    assert plan_state["items"]["finalise"]["status"] == "pending"
    gate = result.data.get("workflow_completion_gate")
    assert isinstance(gate, dict)
    assert gate["safe_to_claim_completion"] is False
    assert "required_plan_items_unmet" in gate["blocking_reason_codes"]
    assert "missing_required_context_keys" in gate["blocking_reason_codes"]
