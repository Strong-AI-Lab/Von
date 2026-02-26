from __future__ import annotations

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
                }
            },
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)

    assert result.ok
    assert result.outputs.get("completion_gate_decision") == "escalation_required"
    assert result.outputs.get("completion_gate_safe_to_claim_completion") is False
    assert result.outputs.get("completion_gate_requires_follow_up") is True

    final_response = result.outputs.get("final_response")
    assert isinstance(final_response, str)
    assert "Execution status:" in final_response
    assert "Blocking effect IDs: effect_1." in final_response

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
