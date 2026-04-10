from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.workflows.durable.workflow_instance_submission_service import (
    WorkflowInstanceSubmissionResult,
)
from src.backend.workflows import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)
from src.backend.workflows.workflow_registry import WorkflowRegistration


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


def _register_test_workflow(
    orchestrator: InternalMCPChatOrchestrator,
    *,
    workflow_id: str,
    initial_state: str = "ready",
    terminal: bool = False,
    metadata: dict[str, Any] | None = None,
    actions: tuple[WorkflowActionInvocation, ...] = (),
) -> WorkflowDefinition:
    workflow_metadata = dict(metadata or {})
    definition = WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state=initial_state,
        states={
            initial_state: WorkflowStateSpec(
                state_id=initial_state,
                actions=actions,
                terminal=terminal,
                metadata=workflow_metadata,
            )
        },
        metadata=workflow_metadata,
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=definition,
            source="test",
        )
    )
    return definition


def _patch_submit_verified_instance(monkeypatch) -> None:
    def _fake_submit_verified_workflow_instance(**kwargs: Any):
        manager = kwargs["manager"]
        workflow_id = str(kwargs.get("workflow_id") or "").strip()
        inputs = kwargs.get("inputs")
        source_event_type = kwargs.get("source_event_type")
        source_event_id = kwargs.get("source_event_id")
        event_idempotency_key = kwargs.get("event_idempotency_key")
        if (
            isinstance(source_event_type, str)
            and source_event_type.strip()
            and isinstance(source_event_id, str)
            and source_event_id.strip()
            and isinstance(event_idempotency_key, str)
            and event_idempotency_key.strip()
        ):
            instance_id, created_new = manager.create_instance_for_event(
                workflow_id=workflow_id,
                user_id=str(kwargs.get("user_id") or "anonymous"),
                org_id=(
                    str(kwargs.get("org_id")).strip()
                    if isinstance(kwargs.get("org_id"), str)
                    and str(kwargs.get("org_id")).strip()
                    else None
                ),
                namespace=str(kwargs.get("namespace") or "anonymous/default"),
                event_idempotency_key=event_idempotency_key,
                source_event_type=source_event_type,
                source_event_id=source_event_id,
                inputs=dict(inputs or {}),
                max_retries=int(kwargs.get("max_retries", 3) or 0),
            )
            status = "pending" if created_new else "reused"
        else:
            instance_id = manager.create_instance(
                workflow_id,
                user_id=str(kwargs.get("user_id") or "anonymous"),
                org_id=(
                    str(kwargs.get("org_id")).strip()
                    if isinstance(kwargs.get("org_id"), str)
                    and str(kwargs.get("org_id")).strip()
                    else None
                ),
                namespace=str(kwargs.get("namespace") or "anonymous/default"),
                inputs=dict(inputs or {}),
                max_retries=int(kwargs.get("max_retries", 3) or 0),
            )
            created_new = True
            status = "pending"

        return WorkflowInstanceSubmissionResult(
            success=True,
            workflow_id=workflow_id,
            status=status,
            instance_id=instance_id,
            verification={
                "preflight_passed": True,
                "postflight_passed": True,
                "runnable_verification_success": True,
            },
            created_new=created_new,
        )

    monkeypatch.setattr(
        "src.backend.workflows.durable.workflow_instance_submission_service.submit_verified_workflow_instance",
        _fake_submit_verified_workflow_instance,
    )


def test_execute_workflow_persists_completed_durable_instance_with_turn_summary(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    _register_test_workflow(orchestrator, workflow_id="#V#tool_calling_workflow")
    fake_manager = _FakeWorkflowInstanceManager()
    _patch_submit_verified_instance(monkeypatch)

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
                "workflow_routing_diagnostics": {
                    "schema_version": "workflow_routing_diagnostics.v1",
                    "selected_workflow_id": "#V#tool_calling_workflow",
                    "dispatch": {
                        "selected_execution_mode": "tool_pipeline",
                        "last_successful_boundary": "workflow_terminal",
                    },
                },
            },
            "completion_gate_decision": "escalation_required",
            "completion_gate_decision_reason": "Required mutation was not executed.",
            "completion_gate_requires_follow_up": True,
            "completion_gate_safe_to_claim_completion": False,
            "completion_gate_escalation_signal": True,
            "completion_gate_escalation_reason": "no_progress_guard_triggered",
            "critic_summary": {"not_verified_count": 1},
            "completion_gate_loop_stall_events": 3,
            "completion_gate_loop_stall_elapsed_ms": 1_250,
            "completion_gate_loop_stall_max_elapsed_ms": 1_000,
            "completion_gate_loop_no_progress_streak": 2,
            "completion_gate_loop_no_progress_limit": 2,
            "completion_gate_loop_stop_reason": "no_progress_guard_triggered",
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
            "turn_execution_record": {
                "workflow_selection": {
                    "selected_workflow_id": "#V#tool_calling_workflow",
                    "selector_verdict": "tool_seeking",
                },
                "workflow_routing_diagnostics": {
                    "schema_version": "workflow_routing_diagnostics.v1",
                    "selected_workflow_id": "#V#tool_calling_workflow",
                    "dispatch": {
                        "selected_execution_mode": "tool_pipeline",
                        "last_successful_boundary": "workflow_terminal",
                    },
                },
            },
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
    routing_diagnostics = turn_contract.get("workflow_routing_diagnostics")
    assert isinstance(routing_diagnostics, dict)
    assert routing_diagnostics.get("schema_version") == "workflow_routing_diagnostics.v1"
    assert routing_diagnostics.get("dispatch", {}).get("selected_execution_mode") == (
        "tool_pipeline"
    )
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
    assert outputs.get("completion_gate_escalation_signal") is True
    assert outputs.get("completion_gate_escalation_reason") == "no_progress_guard_triggered"
    assert outputs.get("completion_gate_loop_stall_events") == 3
    assert outputs.get("completion_gate_loop_stall_elapsed_ms") == 1_250
    assert outputs.get("completion_gate_loop_stall_max_elapsed_ms") == 1_000
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
    assert completion_state.get("escalation_signal") is True
    assert completion_state.get("escalation_reason") == "no_progress_guard_triggered"
    assert completion_state.get("loop_stall_events") == 3
    assert completion_state.get("loop_stall_elapsed_ms") == 1_250
    assert completion_state.get("loop_stall_max_elapsed_ms") == 1_000


def test_execute_workflow_marks_failure_like_terminal_state_as_failed(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    _register_test_workflow(
        orchestrator,
        workflow_id="#V#misaligned_specialised_workflow",
    )
    fake_manager = _FakeWorkflowInstanceManager()
    _patch_submit_verified_instance(monkeypatch)

    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: fake_manager,
    )

    workflow_result = SimpleNamespace(
        completed=True,
        final_state="#V#workflow_step_misaligned_specialised_workflow_failed",
        error=None,
        data={},
    )
    monkeypatch.setattr(
        orchestrator._workflow_executor,
        "run",
        lambda *args, **kwargs: workflow_result,
    )

    result = orchestrator.execute_workflow(
        "#V#misaligned_specialised_workflow",
        data={
            "prompt": "Represent those students.",
            "user_concept_id": "#V#user",
            "org_concept_id": "#V#org",
            "conversation_session_id": "chat-1708",
            "turn_id": "turn-1708",
        },
        llm_client=object(),
        model="test-model",
        user_namespace="#V#user@org",
        conversation_session_id="chat-1708",
        turn_id="turn-1708",
        episode_source="chat_turn_workflow",
    )

    assert result is workflow_result
    resolved_result = cast(Any, result)
    assert isinstance(resolved_result.data, dict)
    result_data = cast(dict[str, Any], resolved_result.data)
    summary = cast(dict[str, Any], result_data["workflow_execution_summary"])
    assert summary["completed"] is False
    assert summary["reported_completed"] is True
    assert summary["effective_completed"] is False
    assert len(fake_manager.mark_completed_calls) == 0
    assert len(fake_manager.mark_failed_calls) == 1
    failed_call = fake_manager.mark_failed_calls[0]
    assert failed_call["instance_id"] == "wf-inst-1"
    assert failed_call["error"] == (
        "failed_terminal_state:#V#workflow_step_misaligned_specialised_workflow_failed"
    )


def test_execute_workflow_uses_explicit_failure_detail_for_failure_like_terminal_state(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    _register_test_workflow(
        orchestrator,
        workflow_id="#V#arxiv_paper_representation_workflow",
    )
    fake_manager = _FakeWorkflowInstanceManager()
    _patch_submit_verified_instance(monkeypatch)

    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: fake_manager,
    )

    actionable_error = (
        "Unexpected error: Failed to store PDF in blob store: Blob store "
        "initialisation failed: OpenStack Swift backend requires 'openstacksdk' "
        "in the active runtime environment."
    )
    workflow_result = SimpleNamespace(
        completed=True,
        final_state="#V#workflow_step_arxiv_paper_representation_workflow_failed",
        error=None,
        data={
            "last_action_error": actionable_error,
            "workflow_step_result_envelopes": [
                {
                    "schema_version": "workflow_step_result_envelope.v1",
                    "state_id": (
                        "#V#workflow_step_arxiv_paper_representation_workflow_"
                        "download_or_finalise"
                    ),
                    "action_id": "download_paper",
                    "action_status": "failed",
                    "action_outcome": "failure",
                    "diagnostics": {"error": actionable_error},
                }
            ],
        },
    )
    monkeypatch.setattr(
        orchestrator._workflow_executor,
        "run",
        lambda *args, **kwargs: workflow_result,
    )

    result = orchestrator.execute_workflow(
        "#V#arxiv_paper_representation_workflow",
        data={
            "prompt": "Represent this paper https://arxiv.org/abs/2411.04983",
            "user_concept_id": "#V#user",
            "org_concept_id": "#V#org",
            "conversation_session_id": "chat-1740",
            "turn_id": "turn-1740",
        },
        llm_client=object(),
        model="test-model",
        user_namespace="#V#user@org",
        conversation_session_id="chat-1740",
        turn_id="turn-1740",
        episode_source="chat_turn_workflow",
    )

    assert result is workflow_result
    assert len(fake_manager.mark_failed_calls) == 1
    failed_call = fake_manager.mark_failed_calls[0]
    assert failed_call["instance_id"] == "wf-inst-1"
    assert failed_call["error"] == f"failed_terminal_state:{actionable_error}"


def test_execute_workflow_marks_durable_instance_failed_on_exception(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    _register_test_workflow(orchestrator, workflow_id="#V#tool_calling_workflow")
    fake_manager = _FakeWorkflowInstanceManager()
    _patch_submit_verified_instance(monkeypatch)

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


def test_execute_workflow_materialises_terminal_effect_evidence_on_gateway_path(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    fake_manager = _FakeWorkflowInstanceManager()
    _patch_submit_verified_instance(monkeypatch)

    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: fake_manager,
    )

    workflow_id = "#V#terminal_effect_gateway_workflow"
    terminal_effect_id = (
        "#V#workflow_effect_terminal_effect_gateway_workflow_done_terminal"
    )
    _register_test_workflow(
        orchestrator,
        workflow_id=workflow_id,
        initial_state="done",
        terminal=True,
        metadata={"effects": [terminal_effect_id]},
    )

    result = orchestrator.execute_workflow(
        workflow_id,
        data={
            "prompt": "Run terminal metadata workflow.",
            "user_concept_id": "#V#user",
            "org_concept_id": "#V#org",
            "conversation_session_id": "chat-terminal",
            "turn_id": "turn-terminal",
            "workflow_episode_source": "chat_turn_workflow",
            "workflow_episode_stage": "tool_calling",
        },
        llm_client=object(),
        model="test-model",
        user_namespace="#V#user@org",
        conversation_session_id="chat-terminal",
        turn_id="turn-terminal",
        episode_source="chat_turn_workflow",
    )

    assert result is not None
    assert result.completed is True
    assert result.error is None
    assert result.data.get(terminal_effect_id) is True
    assert fake_manager.mark_completed_calls
    outputs = fake_manager.mark_completed_calls[0].get("outputs")
    assert isinstance(outputs, dict)
    assert outputs.get("completed") is True


def test_execute_workflow_stamps_execution_summary_into_result_and_durable_outputs(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    fake_manager = _FakeWorkflowInstanceManager()
    _patch_submit_verified_instance(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: fake_manager,
    )

    workflow_id = "#V#summary_gateway_workflow"
    _register_test_workflow(
        orchestrator,
        workflow_id=workflow_id,
        initial_state="done",
        terminal=True,
    )

    monkeypatch.setattr(
        orchestrator._workflow_executor,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            completed=True,
            final_state="done",
            error=None,
            data={
                "response_text": "Workflow created.",
                "created_workflow_ids": ["#V#wf_new"],
                "alignment": {"updated_type_ids": ["#V#durable_workflow"]},
                "workflow_step_result_envelopes": [
                    {
                        "state_id": "prepare",
                        "action_id": "tool.prepare",
                        "action_outcome": "success",
                    },
                    {
                        "state_id": "persist",
                        "action_id": "tool.persist",
                        "action_outcome": "success",
                    },
                ],
                "workflow_terminal_effect_events": [
                    {
                        "state_id": "done",
                        "symbol": "#V#workflow_effect_summary_gateway_done_terminal",
                        "alias": "workflow_effect_summary_gateway_done_terminal",
                        "applied": True,
                    }
                ],
                "workflow_control_flow_events": [
                    {"status": "entered_state", "state_id": "prepare"},
                    {"status": "entered_state", "state_id": "persist"},
                ],
            },
        ),
    )

    result = orchestrator.execute_workflow(
        workflow_id,
        data={
            "prompt": "Create the workflow definition.",
            "user_concept_id": "#V#user",
            "org_concept_id": "#V#org",
            "conversation_session_id": "chat-summary",
            "turn_id": "turn-summary",
        },
        llm_client=object(),
        model="test-model",
        user_namespace="#V#user@org",
        conversation_session_id="chat-summary",
        turn_id="turn-summary",
        episode_source="chat_turn_workflow",
    )

    assert result is not None
    execution_summary = result.data.get("workflow_execution_summary")
    assert isinstance(execution_summary, dict)
    assert execution_summary["workflow_id"] == workflow_id
    assert execution_summary["step_result_envelope_count"] == 2
    assert execution_summary["action_success_count"] == 2
    assert execution_summary["terminal_effect_count"] == 1
    assert execution_summary["durable_side_effect_count"] == 2
    assert execution_summary["durable_side_effects"] == [
        {
            "mutation_kind": "created",
            "artefact_type": "workflow",
            "source_key": "created_workflow_ids",
            "source_path": "created_workflow_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#wf_new"],
        },
        {
            "mutation_kind": "updated",
            "artefact_type": "type",
            "source_key": "updated_type_ids",
            "source_path": "alignment.updated_type_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#durable_workflow"],
        },
    ]

    outputs = fake_manager.mark_completed_calls[0].get("outputs")
    assert isinstance(outputs, dict)
    persisted_execution_summary = outputs.get("workflow_execution_summary")
    assert isinstance(persisted_execution_summary, dict)
    assert persisted_execution_summary["workflow_id"] == workflow_id
    assert persisted_execution_summary["durable_side_effect_count"] == 2


def test_execute_workflow_applies_launch_input_contract_before_run(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    fake_manager = _FakeWorkflowInstanceManager()
    _patch_submit_verified_instance(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: fake_manager,
    )

    workflow_id = "#V#launch_input_contract_workflow"
    _register_test_workflow(
        orchestrator,
        workflow_id=workflow_id,
        initial_state="prepare",
        terminal=True,
        actions=(WorkflowActionInvocation(action_id="tool.prepare"),),
        metadata={
            "launch_input_contract": {
                "schema_version": "workflow_launch_input_contract.v1",
                "required_inputs": ["invitation_text"],
                "input_mappings": [
                    {
                        "target_context_key": "invitation_text",
                        "source_expression": "inputs.prompt",
                        "extractor": "first_quoted_text",
                        "required": True,
                    },
                    {
                        "target_context_key": "candidate_workflow_ids",
                        "source_expression": "inputs.workflow_discovery_result.matches",
                        "extractor": "workflow_id_list",
                    },
                ],
            },
            "launch_input_contract_source": "test_contract",
        },
    )

    captured_data: dict[str, Any] = {}

    def _run(*_args: Any, **kwargs: Any):
        captured_data.update(dict(kwargs.get("data") or {}))
        return SimpleNamespace(
            completed=True,
            final_state="prepare",
            error=None,
            data={"response_text": "Prepared."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run)

    result = orchestrator.execute_workflow(
        workflow_id,
        data={
            "prompt": (
                'Run a meeting-invitation test on this invitation text:\n\n'
                '"Kia ora team, please join us on Tuesday at 2:00pm in Room 4 '
                'for a project planning meeting about the Q2 roadmap."'
            ),
            "workflow_discovery_result": {
                "matches": [
                    {"concept_id": "#V#meeting_invitation_testing_workflow"},
                    {"concept_id": "#V#synthetic_workflow_regression_suite_workflow"},
                ]
            },
            "user_concept_id": "#V#user",
            "org_concept_id": "#V#org",
            "conversation_session_id": "chat-launch",
            "turn_id": "turn-launch",
        },
        llm_client=object(),
        model="test-model",
        user_namespace="#V#user@org",
        conversation_session_id="chat-launch",
        turn_id="turn-launch",
        episode_source="chat_turn_workflow",
    )

    assert result is not None
    assert result.completed is True
    assert captured_data["invitation_text"] == (
        "Kia ora team, please join us on Tuesday at 2:00pm in Room 4 for a "
        "project planning meeting about the Q2 roadmap."
    )
    assert captured_data["candidate_workflow_ids"] == [
        "#V#meeting_invitation_testing_workflow",
        "#V#synthetic_workflow_regression_suite_workflow",
    ]
    launch_resolution = captured_data.get("workflow_launch_input_resolution")
    assert isinstance(launch_resolution, dict)
    assert launch_resolution.get("status") == "resolved"
    assert launch_resolution.get("resolved_inputs") == [
        "candidate_workflow_ids",
        "invitation_text",
    ]
    assert len(fake_manager.create_for_event_calls) == 1
    create_inputs = fake_manager.create_for_event_calls[0].get("inputs")
    assert isinstance(create_inputs, dict)
    persisted_resolution = create_inputs.get("workflow_launch_input_resolution")
    assert isinstance(persisted_resolution, dict)
    assert persisted_resolution.get("status") == "resolved"


def test_execute_workflow_fails_closed_when_required_launch_input_unresolved(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    fake_manager = _FakeWorkflowInstanceManager()
    _patch_submit_verified_instance(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: fake_manager,
    )

    workflow_id = "#V#launch_input_failure_workflow"
    _register_test_workflow(
        orchestrator,
        workflow_id=workflow_id,
        initial_state="prepare",
        terminal=True,
        actions=(WorkflowActionInvocation(action_id="tool.prepare"),),
        metadata={
            "launch_input_contract": {
                "schema_version": "workflow_launch_input_contract.v1",
                "required_inputs": ["invitation_text"],
                "input_mappings": [
                    {
                        "target_context_key": "invitation_text",
                        "source_expression": "inputs.prompt",
                        "extractor": "first_quoted_text",
                        "required": True,
                    }
                ],
            },
            "launch_input_contract_source": "test_contract",
        },
    )

    def _unexpected_run(*_args: Any, **_kwargs: Any):
        raise AssertionError(
            "workflow executor should not run when launch inputs are unresolved"
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _unexpected_run)

    result = orchestrator.execute_workflow(
        workflow_id,
        data={
            "prompt": "Run the meeting invitation test without a quoted specimen.",
            "user_concept_id": "#V#user",
            "org_concept_id": "#V#org",
            "conversation_session_id": "chat-fail",
            "turn_id": "turn-fail",
        },
        llm_client=object(),
        model="test-model",
        user_namespace="#V#user@org",
        conversation_session_id="chat-fail",
        turn_id="turn-fail",
        episode_source="chat_turn_workflow",
    )

    assert result is not None
    assert result.completed is False
    assert result.final_state == "prepare"
    assert "workflow_launch_input_resolution_failed" in str(result.error)
    resolution = result.data.get("workflow_launch_input_resolution")
    assert isinstance(resolution, dict)
    assert resolution.get("status") == "failed"
    assert resolution.get("unresolved_required_inputs") == ["invitation_text"]
    assert resolution.get("failing_state_id") == "prepare"
    assert resolution.get("failing_action_id") == "tool.prepare"
    assert "required launch inputs were unresolved" in str(
        result.data.get("response_text")
    )
    assert fake_manager.create_for_event_calls == []


def test_execute_workflow_creates_durable_instance_for_user_only_namespace(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    _register_test_workflow(orchestrator, workflow_id="#V#user_only_namespace_workflow")
    fake_manager = _FakeWorkflowInstanceManager()
    _patch_submit_verified_instance(monkeypatch)

    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: fake_manager,
    )
    monkeypatch.setattr(
        orchestrator._workflow_executor,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={},
        ),
    )

    result = orchestrator.execute_workflow(
        "#V#user_only_namespace_workflow",
        data={
            "prompt": "Run in a user-only namespace.",
            "user_concept_id": "#V#user_only",
            "conversation_session_id": "chat-user-only",
            "turn_id": "turn-user-only",
            "aux_llm_calls": [],
        },
        llm_client=object(),
        model="test-model",
        user_namespace="#V#user_only",
        conversation_session_id="chat-user-only",
        turn_id="turn-user-only",
        episode_source="chat_turn_workflow",
    )

    assert result is not None
    assert len(fake_manager.create_for_event_calls) == 1
    create_call = fake_manager.create_for_event_calls[0]
    assert create_call["user_id"] == "#V#user_only"
    assert create_call["org_id"] is None
    assert create_call["namespace"] == "#V#user_only"


def test_execute_workflow_records_structured_durable_submission_failures(
    monkeypatch,
) -> None:
    orchestrator = _build_orchestrator()
    _register_test_workflow(orchestrator, workflow_id="#V#submission_failure_workflow")
    fake_manager = _FakeWorkflowInstanceManager()
    input_data = {
        "prompt": "Run despite durable submission failure.",
        "user_concept_id": "#V#user",
        "org_concept_id": "#V#org",
        "conversation_session_id": "chat-fail-telemetry",
        "turn_id": "turn-fail-telemetry",
        "aux_llm_calls": [],
    }

    def _failing_submit_verified_workflow_instance(**_kwargs: Any):
        raise RuntimeError("durable store unavailable")

    monkeypatch.setattr(
        "src.backend.workflows.durable.workflow_instance_submission_service.submit_verified_workflow_instance",
        _failing_submit_verified_workflow_instance,
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: fake_manager,
    )
    monkeypatch.setattr(
        orchestrator._workflow_executor,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={"aux_llm_calls": []},
        ),
    )

    result = orchestrator.execute_workflow(
        "#V#submission_failure_workflow",
        data=input_data,
        llm_client=object(),
        model="test-model",
        user_namespace="#V#user@org",
        conversation_session_id="chat-fail-telemetry",
        turn_id="turn-fail-telemetry",
        episode_source="chat_turn_workflow",
    )

    assert result is not None
    aux_calls = input_data.get("aux_llm_calls")
    assert isinstance(aux_calls, list)
    assert any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_instance_submission"
        and entry.get("status") == "submission_failed"
        and entry.get("reason_code") == "RuntimeError"
        and entry.get("workflow_id") == "#V#submission_failure_workflow"
        for entry in aux_calls
    )
