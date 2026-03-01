from __future__ import annotations

import time
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.workflows.action_registry import WorkflowActionRequest, WorkflowEnvironment


class _DummyGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}

    def invoke(self, _tool_name: str, _payload: dict[str, Any]) -> Any:
        raise RuntimeError("invoke should not be called in this test")


class _GatewayResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class _RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {}

    def invoke(self, tool_name: str, payload: dict[str, Any]) -> _GatewayResult:
        self.calls.append((tool_name, payload))
        if tool_name == "workflow_create_instance":
            return _GatewayResult(
                {"success": True, "instance_id": "wf-maint-1", "status": "pending"}
            )
        return _GatewayResult({"success": True})


def _build_orchestrator() -> InternalMCPChatOrchestrator:
    # Avoid full orchestrator initialisation in unit tests.
    # Constructor bootstrap can require external workflow registry state that is
    # irrelevant for testing isolated turn-execution actions.
    return cast(
        InternalMCPChatOrchestrator,
        object.__new__(InternalMCPChatOrchestrator),
    )


def _build_request(
    *,
    action_id: str,
    data: dict[str, Any],
) -> WorkflowActionRequest:
    env = WorkflowEnvironment(
        llm_client=object(),
        gateway=cast(Any, _DummyGateway()),
        model="test-model",
        user_namespace="#V#test_user",
    )
    return WorkflowActionRequest(
        action_id=action_id,
        inputs={},
        environment=env,
        data=data,
    )


def test_turn_execution_critic_detects_unresolved_kb_mutation() -> None:
    orchestrator = _build_orchestrator()
    aux_llm_calls: list[dict[str, Any]] = []
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Those relations were not added. Proceed with the predicates.",
            "final_response": "Done.",
            "invocations": [],
            "aux_llm_calls": aux_llm_calls,
            "turn_id": "req-turn-critic-1",
            "conversation_session_id": "session-critic-1",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)

    assert result.ok
    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)
    assert record.get("request_id") == "req-turn-critic-1"
    assert record.get("actor_concept_id") == "#V#test_user"

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert len(required_effects) == 1
    assert required_effects[0]["status"] == "not_executed"

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    evidence_payload = completion_gate.get("evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("completion_outcome") == "failure"
    unresolved_preconditions = evidence_payload.get("unresolved_preconditions")
    assert isinstance(unresolved_preconditions, list)
    assert unresolved_preconditions

    assert any(
        entry.get("type") == "turn_execution_critic"
        for entry in aux_llm_calls
        if isinstance(entry, dict)
    )


def test_turn_execution_critic_prefers_explicit_actor_concept_id() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Please proceed",
            "final_response": "Done.",
            "invocations": [],
            "aux_llm_calls": [],
            "turn_id": "req-turn-critic-actor",
            "conversation_session_id": "session-critic-actor",
            "actor_concept_id": "#V#github_copilot_instance",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#chat_assistant_workflow",
                "verdict": "plain_response",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok
    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)
    assert record.get("actor_concept_id") == "#V#github_copilot_instance"


def test_turn_execution_critic_prefers_concrete_verification_reads() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Please add this relation to the knowledge base.",
            "final_response": "Response recorded.",
            "invocations": [
                {"tool": "add_relationship", "payload": {"status": "ok"}},
                {"tool": "fetch_concept", "payload": {"status": "ok"}},
            ],
            "aux_llm_calls": [],
            "turn_id": "req-turn-critic-verified",
            "conversation_session_id": "session-critic-verified",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok
    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)

    postcondition_checks = record.get("postcondition_checks")
    assert isinstance(postcondition_checks, list)
    assert postcondition_checks
    check = postcondition_checks[0]
    assert check.get("status") == "verified"
    assert check.get("verification_mode") == "state_requery_observed"
    observed = check.get("observed")
    assert isinstance(observed, dict)
    assert "fetch_concept" in list(observed.get("successful_verification_tools") or [])

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "completed"
    evidence_payload = completion_gate.get("evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("completion_outcome") == "success"


def test_turn_completion_gate_appends_execution_status_for_unresolved_effect() -> None:
    orchestrator = _build_orchestrator()
    aux_llm_calls: list[dict[str, Any]] = []
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I have completed the update.",
            "aux_llm_calls": aux_llm_calls,
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                    "blocking_failure_codes": ["worker_unavailable_zero_execution"],
                    "evidence_payload": {
                        "unresolved_preconditions": [
                            {
                                "effect_id": "effect_1",
                                "effect_type": "tool_execution",
                                "status": "not_executed",
                                "status_reason": (
                                    "Tool execution was blocked while workers "
                                    "were unavailable."
                                ),
                                "failure_codes": ["worker_unavailable_zero_execution"],
                            }
                        ]
                    },
                }
            },
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)

    assert result.ok
    assert result.outputs.get("completion_gate_decision") == "escalation_required"
    assert result.outputs.get("completion_gate_safe_to_claim_completion") is False
    assert result.outputs.get("completion_gate_requires_follow_up") is True
    assert result.outputs.get("completion_gate_terminal_outcome") == "retrying"
    assert result.outputs.get("completion_gate_blocking_failure_codes") == [
        "worker_unavailable_zero_execution"
    ]
    unresolved_preconditions = result.outputs.get("completion_gate_unresolved_preconditions")
    assert isinstance(unresolved_preconditions, list)
    assert unresolved_preconditions

    final_response = result.outputs.get("final_response")
    assert isinstance(final_response, str)
    assert "Execution status:" in final_response
    assert "Blocking effect IDs: effect_1." in final_response
    assert "Unresolved preconditions:" in final_response
    assert "Failure codes: worker_unavailable_zero_execution." in final_response

    evidence_payload = result.outputs.get("completion_gate_evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("terminal_outcome") == "retrying"

    assert any(
        entry.get("type") == "turn_completion_gate"
        for entry in aux_llm_calls
        if isinstance(entry, dict)
    )


def test_turn_execution_critic_flags_missing_non_kb_mutation_execution() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Please create a Jira task for this regression and assign it to me.",
            "final_response": "Done.",
            "invocations": [],
            "aux_llm_calls": [],
            "turn_id": "req-turn-critic-2",
            "conversation_session_id": "session-critic-2",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert len(required_effects) == 1
    assert required_effects[0]["status"] == "not_executed"

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False


def test_turn_execution_critic_treats_diagnostic_prompt_as_non_mutating() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": (
                "You didn't actually create the task instance this time. "
                "Inspect the telemetry and explain why."
            ),
            "final_response": "No mutation was attempted in this turn.",
            "invocations": [],
            "aux_llm_calls": [],
            "turn_id": "req-turn-critic-3",
            "conversation_session_id": "session-critic-3",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects == []

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True


def test_turn_execution_critic_flags_worker_unavailable_zero_execution() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Yes",
            "final_response": "Completed.",
            "invocations": [],
            "aux_llm_calls": [],
            "turn_id": "req-turn-critic-worker-unavailable",
            "conversation_session_id": "session-critic-worker-unavailable",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
            "turn_execution_diagnostics": {
                "latest_progress": {
                    "counters": {"tools_started": 0, "tools_completed": 0},
                    "diagnostic_events": [
                        {
                            "stage": "tool_plan",
                            "phase": "tool_plan",
                            "event_kind": "heartbeat",
                            "liveness_reason": "worker_unavailable",
                        }
                    ],
                }
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert len(required_effects) == 1
    effect = required_effects[0]
    assert effect.get("effect_type") == "tool_execution"
    assert effect.get("status") == "not_executed"
    assert "worker_unavailable_zero_execution" in list(effect.get("failure_codes") or [])

    execution = record.get("execution")
    assert isinstance(execution, dict)
    execution_summary = execution.get("summary")
    assert isinstance(execution_summary, dict)
    assert execution_summary.get("planned_count") == 1
    assert execution_summary.get("executed_count") == 0

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("decision_reason") == "Required tool execution was not observed."
    assert completion_gate.get("safe_to_claim_completion") is False


def test_turn_completion_gate_requests_repeat_when_budget_available() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I have completed the update.",
            "invocations": [],
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                }
            },
            "completion_gate_loop_attempts": 0,
            "completion_gate_loop_max_attempts": 1,
            "completion_gate_loop_max_elapsed_ms": 60_000,
            "completion_gate_loop_no_progress_streak": 0,
            "completion_gate_loop_no_progress_limit": 1,
            "completion_gate_loop_last_invocation_count": 0,
            "completion_gate_loop_last_blocking_signature": "",
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)
    assert result.ok
    assert result.outputs.get("completion_gate_repeat_iteration") is True
    assert result.outputs.get("completion_gate_loop_attempts") == 1
    assert result.outputs.get("completion_gate_loop_stop_reason") is None
    assert result.outputs.get("completion_gate_requires_follow_up") is True
    assert result.outputs.get("completion_gate_terminal_outcome") == "retrying"
    evidence_payload = result.outputs.get("completion_gate_evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("terminal_outcome") == "retrying"


def test_turn_completion_gate_stops_repeat_when_attempt_budget_exhausted() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I have completed the update.",
            "invocations": [],
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                }
            },
            "completion_gate_loop_attempts": 1,
            "completion_gate_loop_max_attempts": 1,
            "completion_gate_loop_max_elapsed_ms": 60_000,
            "completion_gate_loop_no_progress_streak": 0,
            "completion_gate_loop_no_progress_limit": 1,
            "completion_gate_loop_last_invocation_count": 0,
            "completion_gate_loop_last_blocking_signature": "",
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)
    assert result.ok
    assert result.outputs.get("completion_gate_repeat_iteration") is False
    assert (
        result.outputs.get("completion_gate_loop_stop_reason")
        == "attempt_budget_exhausted"
    )
    assert (
        result.outputs.get("completion_gate_terminal_outcome")
        == "attempt_budget_exhausted"
    )
    evidence_payload = result.outputs.get("completion_gate_evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("terminal_outcome") == "attempt_budget_exhausted"


def test_turn_completion_gate_stops_repeat_when_no_progress_guard_triggers() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I have completed the update.",
            "invocations": [],
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                }
            },
            "completion_gate_loop_attempts": 1,
            "completion_gate_loop_max_attempts": 3,
            "completion_gate_loop_max_elapsed_ms": 60_000,
            "completion_gate_loop_no_progress_streak": 0,
            "completion_gate_loop_no_progress_limit": 1,
            "completion_gate_loop_last_invocation_count": 0,
            "completion_gate_loop_last_blocking_signature": "escalation_required:effect_1",
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)
    assert result.ok
    assert result.outputs.get("completion_gate_repeat_iteration") is False
    assert (
        result.outputs.get("completion_gate_loop_stop_reason")
        == "no_progress_guard_triggered"
    )
    assert (
        result.outputs.get("completion_gate_terminal_outcome")
        == "no_progress_guard_triggered"
    )
    assert result.outputs.get("completion_gate_escalation_signal") is True
    assert (
        result.outputs.get("completion_gate_escalation_reason")
        == "no_progress_guard_triggered"
    )


def test_turn_completion_gate_stops_repeat_when_stall_latency_budget_exhausted() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I have completed the update.",
            "invocations": [],
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                }
            },
            "completion_gate_loop_attempts": 1,
            "completion_gate_loop_max_attempts": 5,
            "completion_gate_loop_max_elapsed_ms": 60_000,
            "completion_gate_loop_no_progress_streak": 0,
            "completion_gate_loop_no_progress_limit": 5,
            "completion_gate_loop_last_invocation_count": 0,
            "completion_gate_loop_last_blocking_signature": "escalation_required:effect_1",
            "completion_gate_loop_stall_started_monotonic": time.monotonic() - 2.0,
            "completion_gate_loop_stall_elapsed_ms": 0,
            "completion_gate_loop_stall_max_elapsed_ms": 500,
            "completion_gate_loop_stall_events": 0,
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)
    assert result.ok
    assert result.outputs.get("completion_gate_repeat_iteration") is False
    assert (
        result.outputs.get("completion_gate_loop_stop_reason")
        == "stall_latency_budget_exhausted"
    )
    assert (
        result.outputs.get("completion_gate_terminal_outcome")
        == "stall_latency_budget_exhausted"
    )
    assert result.outputs.get("completion_gate_escalation_signal") is True
    assert (
        result.outputs.get("completion_gate_escalation_reason")
        == "stall_latency_budget_exhausted"
    )
    assert int(result.outputs.get("completion_gate_loop_stall_events") or 0) >= 1
    assert int(result.outputs.get("completion_gate_loop_stall_elapsed_ms") or 0) >= 500
    final_response = result.outputs.get("final_response")
    assert isinstance(final_response, str)
    assert "Escalation trigger: stall_latency_budget_exhausted." in final_response
    evidence_payload = result.outputs.get("completion_gate_evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("terminal_outcome") == "stall_latency_budget_exhausted"
    assert evidence_payload.get("escalation_signal") is True
    assert evidence_payload.get("escalation_reason") == "stall_latency_budget_exhausted"


def test_turn_completion_gate_autotriggers_workflow_introspection(monkeypatch) -> None:
    monkeypatch.setenv("VON_WORKFLOW_INTROSPECTION_AUTOTRIGGER_ENABLE", "1")
    monkeypatch.setenv("VON_WORKFLOW_INTROSPECTION_AUTO_APPLY", "1")

    orchestrator = _build_orchestrator()
    gateway = _RecordingGateway()
    orchestrator._gateway = cast(Any, gateway)

    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "prompt": "Why did this conflate task tooling and concepts?",
            "turn_id": "req-introspection-1",
            "conversation_session_id": "session-introspection-1",
            "user_concept_id": "#V#test_user",
            "org_concept_id": "#V#test_org",
            "turn_execution_record": {
                "workflow_selection": {
                    "selected_workflow_id": "#V#tool_calling_workflow",
                },
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Conflation suspected.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                },
            },
            "completion_gate_loop_attempts": 1,
            "completion_gate_loop_max_attempts": 1,
            "completion_gate_loop_max_elapsed_ms": 60_000,
            "completion_gate_loop_no_progress_streak": 0,
            "completion_gate_loop_no_progress_limit": 2,
            "completion_gate_loop_last_invocation_count": 0,
            "completion_gate_loop_last_blocking_signature": "",
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)
    assert result.ok

    autotrigger = result.outputs.get("workflow_introspection_autotrigger")
    assert isinstance(autotrigger, dict)
    assert autotrigger.get("success") is True
    assert autotrigger.get("instance_id") == "wf-maint-1"

    assert gateway.calls
    tool_name, payload = gateway.calls[0]
    assert tool_name == "workflow_create_instance"
    assert payload.get("workflow_id") == (
        "#V#workflow_introspection_maintenance_workflow"
    )
    inputs = payload.get("inputs")
    assert isinstance(inputs, dict)
    assert inputs.get("request_id") == "req-introspection-1"
