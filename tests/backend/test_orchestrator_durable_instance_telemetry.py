from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)


class _DummyGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}

    def invoke(self, _tool_name: str, _payload: dict[str, Any]) -> Any:
        raise RuntimeError("invoke should not be called in this test")


class _FakeWorkflowInstanceManager:
    def __init__(self) -> None:
        self.create_for_event_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.mark_completed_calls: list[dict[str, Any]] = []
        self.mark_failed_calls: list[dict[str, Any]] = []

    def create_instance_for_event(self, **kwargs: Any) -> tuple[str, bool]:
        self.create_for_event_calls.append(dict(kwargs))
        return ("wf-inst-1", True)

    def create_instance(self, *args: Any, **kwargs: Any) -> str:
        self.create_calls.append({"args": list(args), "kwargs": dict(kwargs)})
        return "wf-inst-1"

    def mark_completed(self, instance_id: str, **kwargs: Any) -> bool:
        call = {"instance_id": instance_id}
        call.update(kwargs)
        self.mark_completed_calls.append(call)
        return True

    def mark_failed(self, instance_id: str, **kwargs: Any) -> bool:
        call = {"instance_id": instance_id}
        call.update(kwargs)
        self.mark_failed_calls.append(call)
        return True


def _build_orchestrator() -> InternalMCPChatOrchestrator:
    return InternalMCPChatOrchestrator(gateway=cast(Any, _DummyGateway()))


def test_execute_workflow_persists_completed_durable_instance_with_turn_summary(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    fake_manager = _FakeWorkflowInstanceManager()

    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: fake_manager,
    )

    workflow_result = SimpleNamespace(
        completed=True,
        final_state="completed",
        error=None,
        data={
            "turn_execution_record": {
                "request_id": "req-123",
                "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "requires_follow_up": True,
                    "safe_to_claim_completion": False,
                    "blocking_effect_ids": ["effect_1"],
                },
                "workflow_selection": {
                    "selected_workflow_id": "#V#tool_calling_workflow",
                    "selector_verdict": "tool_seeking",
                },
            },
            "completion_gate_decision": "escalation_required",
            "completion_gate_decision_reason": "Required mutation was not executed.",
            "completion_gate_requires_follow_up": True,
            "completion_gate_safe_to_claim_completion": False,
            "critic_summary": {"not_verified_count": 1},
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
            "workflow_discovery_result": {
                "candidate_ids": ["#V#tool_calling_workflow"]
            },
        },
    )
    monkeypatch.setattr(
        orchestrator._workflow_executor,
        "run",
        lambda *args, **kwargs: workflow_result,
    )

    result = orchestrator.execute_workflow(
        "#V#tool_calling_workflow",
        data={
            "prompt": "Proceed with predicates.",
            "user_concept_id": "#V#user",
            "org_concept_id": "#V#org",
            "conversation_session_id": "chat-1",
            "turn_id": "turn-1",
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
            "workflow_discovery_result": {
                "candidate_ids": ["#V#tool_calling_workflow"]
            },
        },
        llm_client=object(),
        model="test-model",
        user_namespace="#V#user@org",
        conversation_session_id="chat-1",
        turn_id="turn-1",
        episode_source="chat_turn_workflow",
    )

    assert result is workflow_result
    assert len(fake_manager.create_for_event_calls) == 1
    assert len(fake_manager.mark_completed_calls) == 1
    assert len(fake_manager.mark_failed_calls) == 0

    completed_call = fake_manager.mark_completed_calls[0]
    assert completed_call["instance_id"] == "wf-inst-1"
    outputs = completed_call.get("outputs")
    assert isinstance(outputs, dict)
    assert outputs.get("completed") is True
    assert outputs.get("completion_gate_decision") == "escalation_required"
    turn_execution = outputs.get("turn_execution")
    assert isinstance(turn_execution, dict)
    assert turn_execution.get("request_id") == "req-123"
    assert turn_execution.get("decision") == "escalation_required"


def test_execute_workflow_marks_durable_instance_failed_on_exception(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    fake_manager = _FakeWorkflowInstanceManager()

    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: fake_manager,
    )

    def _raise(*_args: Any, **_kwargs: Any):
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _raise)

    with pytest.raises(RuntimeError):
        orchestrator.execute_workflow(
            "#V#tool_calling_workflow",
            data={
                "prompt": "Proceed with predicates.",
                "user_concept_id": "#V#user",
                "org_concept_id": "#V#org",
                "conversation_session_id": "chat-1",
                "turn_id": "turn-2",
            },
            llm_client=object(),
            model="test-model",
            user_namespace="#V#user@org",
            conversation_session_id="chat-1",
            turn_id="turn-2",
            episode_source="chat_turn_workflow",
        )

    assert len(fake_manager.create_for_event_calls) == 1
    assert len(fake_manager.mark_failed_calls) == 1
    failed_call = fake_manager.mark_failed_calls[0]
    assert failed_call["instance_id"] == "wf-inst-1"
    assert failed_call.get("error_step") == "workflow_exception"
    assert "exception:boom" in str(failed_call.get("error"))
