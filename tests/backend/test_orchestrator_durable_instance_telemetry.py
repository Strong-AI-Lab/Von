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
        self.checkpoint_calls: list[dict[str, Any]] = []
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

    def checkpoint(self, instance_id: str, **kwargs: Any) -> bool:
        call = {"instance_id": instance_id}
        call.update(kwargs)
        self.checkpoint_calls.append(call)
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
    assert len(fake_manager.checkpoint_calls) == 1
    assert len(fake_manager.mark_completed_calls) == 1
    assert len(fake_manager.mark_failed_calls) == 0

    create_call = fake_manager.create_for_event_calls[0]
    create_inputs = create_call.get("inputs")
    assert isinstance(create_inputs, dict)
    turn_contract = create_inputs.get("turn_execution_contract")
    assert isinstance(turn_contract, dict)
    assert turn_contract.get("schema_version") == "turn_execution_contract.v1"
    selection_contract = turn_contract.get("selection")
    assert isinstance(selection_contract, dict)
    assert selection_contract.get("selected_workflow_id") == "#V#tool_calling_workflow"
    assert selection_contract.get("selector_verdict") == "tool_seeking"
    assert isinstance(selection_contract.get("selection_rationale"), str)
    contract_stage_model = turn_contract.get("workflow_stage_model")
    assert isinstance(contract_stage_model, dict)
    assert contract_stage_model.get("schema_version") == "conversation_turn_stage_model.v1"
    contract_stage_path = turn_contract.get("workflow_stage_path")
    assert isinstance(contract_stage_path, dict)
    assert contract_stage_path.get("schema_version") == "conversation_turn_stage_path.v1"

    checkpoint_call = fake_manager.checkpoint_calls[0]
    checkpoint_data = checkpoint_call.get("workflow_data")
    assert isinstance(checkpoint_data, dict)
    runtime_snapshot = checkpoint_data.get("turn_execution_runtime")
    assert isinstance(runtime_snapshot, dict)
    assert runtime_snapshot.get("schema_version") == "turn_execution_runtime.v1"
    assert isinstance(runtime_snapshot.get("step_instances"), list)
    assert isinstance(runtime_snapshot.get("check_instances"), list)
    runtime_stage_path = runtime_snapshot.get("workflow_stage_path")
    assert isinstance(runtime_stage_path, dict)
    assert runtime_stage_path.get("schema_version") == "conversation_turn_stage_path.v1"
    runtime_path_entries = runtime_stage_path.get("path")
    assert isinstance(runtime_path_entries, list)
    assert runtime_path_entries
    assert runtime_path_entries[-1].get("stage_id") == "completed"

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
    outcome = outputs.get("turn_execution_outcome")
    assert isinstance(outcome, dict)
    assert outcome.get("schema_version") == "turn_execution_outcome.v1"
    selection = outcome.get("selection")
    assert isinstance(selection, dict)
    assert selection.get("selected_workflow_id") == "#V#tool_calling_workflow"
    outcome_stage_path = outcome.get("workflow_stage_path")
    assert isinstance(outcome_stage_path, dict)
    assert outcome_stage_path.get("schema_version") == "conversation_turn_stage_path.v1"
    outcome_path_entries = outcome_stage_path.get("path")
    assert isinstance(outcome_path_entries, list)
    assert outcome_path_entries
    assert outcome_path_entries[-1].get("stage_id") == "completed"
    assert isinstance(outcome.get("step_instances"), list)
    assert len(outcome["step_instances"]) == 2
    assert isinstance(outcome.get("check_instances"), list)
    completion_state = outcome.get("completion_state")
    assert isinstance(completion_state, dict)
    assert completion_state.get("decision") == "escalation_required"
    assert completion_state.get("requires_follow_up") is True


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
    assert len(fake_manager.checkpoint_calls) == 1
    assert len(fake_manager.mark_failed_calls) == 1
    checkpoint_call = fake_manager.checkpoint_calls[0]
    checkpoint_data = checkpoint_call.get("workflow_data")
    assert isinstance(checkpoint_data, dict)
    runtime_snapshot = checkpoint_data.get("turn_execution_runtime")
    assert isinstance(runtime_snapshot, dict)
    assert runtime_snapshot.get("schema_version") == "turn_execution_runtime.v1"
    stage_path = runtime_snapshot.get("workflow_stage_path")
    assert isinstance(stage_path, dict)
    assert stage_path.get("schema_version") == "conversation_turn_stage_path.v1"
    path_entries = stage_path.get("path")
    assert isinstance(path_entries, list)
    assert path_entries
    assert path_entries[-1].get("stage_id") == "failed"
    completion_state = runtime_snapshot.get("completion_state")
    assert isinstance(completion_state, dict)
    assert completion_state.get("decision") == "failed"
    assert completion_state.get("requires_follow_up") is True
    failed_call = fake_manager.mark_failed_calls[0]
    assert failed_call["instance_id"] == "wf-inst-1"
    assert failed_call.get("error_step") == "workflow_exception"
    assert "exception:boom" in str(failed_call.get("error"))
