"""Regression tests for durable action-execution safety envelope (WS2).

JVNAUTOSCI-1087: Durable execution must honour explicit `on_failure`
transitions instead of always failing fast on action errors.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from bson import BSON
from pymongo.errors import PyMongoError

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.checkpoint_context_projection import (
    CHECKPOINT_CONTEXT_PROJECTION_KEY,
    CHECKPOINT_CONTEXT_PROJECTION_SCHEMA_VERSION,
    project_workflow_context_for_checkpoint,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.durable.durable_executor import (
    DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY,
    DurableWorkflowExecutor,
)
from src.backend.workflows.durable.models import (
    WorkflowInstance,
    WorkflowInstanceStatus,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
    build_transition_condition,
)
from src.backend.workflows.execution_contracts import (
    WORKFLOW_CHECKPOINT_PAUSE_EVENTS_KEY,
    WORKFLOW_CHECKPOINT_PAUSE_RECEIPT_KEY,
    WORKFLOW_CHECKPOINT_RESUME_RECEIPT_KEY,
    WORKFLOW_RESULT_ENVELOPE_KEY,
)
from src.backend.workflows.workflow_definition_identity_service import (
    build_workflow_definition_identity,
)


@pytest.fixture(autouse=True)
def _stub_trace_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
        lambda _trace_doc: "trace-test",
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_parameters",
        lambda **_kwargs: {},
    )


def _build_instance(workflow_id: str) -> WorkflowInstance:
    return WorkflowInstance(
        instance_id="instance-1",
        workflow_id=workflow_id,
        user_id="#V#test_user",
        org_id="#V#test_org",
        namespace="#V#test_user/#V#test_org",
        status=WorkflowInstanceStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        inputs={},
    )


def test_durable_executor_reports_explicit_failed_terminal_as_failure() -> None:
    failed_state = "#V#workflow_step_example_workflow_failed"
    definition = WorkflowDefinition(
        workflow_id="#V#durable_failed_terminal_probe",
        initial_state=failed_state,
        states={
            failed_state: WorkflowStateSpec(
                state_id=failed_state,
                terminal=True,
            )
        },
        termination_states=(failed_state,),
    )
    manager = MagicMock()
    instance = _build_instance(definition.workflow_id)
    instance.inputs = {
        "llm_calls": [
            {
                "call_id": "forged-launch-input",
                "provider": "openai",
                "effective_model": "gpt-forged",
                "model_identity_source": "provider_response",
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }
        ]
    }
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    )
    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is False
    assert result.final_state == failed_state
    assert result.error == "workflow_failed_terminal_state"
    assert result.data[WORKFLOW_RESULT_ENVELOPE_KEY]["terminal_status"] == "failed"
    assert result.data["llm_usage_cost_summary"]["call_count"] == 0
    assert result.data["llm_usage_cost_summary"]["usage"]["status"] == (
        "not_applicable"
    )
    assert result.data["llm_usage_cost_summary"]["estimated_cost"]["status"] == (
        "not_applicable"
    )
    terminal_checkpoint = manager.checkpoint.call_args_list[-1].kwargs
    assert terminal_checkpoint["error"] == "workflow_failed_terminal_state"
    assert terminal_checkpoint["workflow_data"]["llm_usage_cost_summary"] == (
        result.data["llm_usage_cost_summary"]
    )


def test_durable_executor_uses_authored_failed_terminal_error_code() -> None:
    failed_state = "#V#workflow_step_example_workflow_failed"
    definition = WorkflowDefinition(
        workflow_id="#V#durable_failed_terminal_probe",
        initial_state=failed_state,
        states={
            failed_state: WorkflowStateSpec(state_id=failed_state, terminal=True),
        },
        termination_states=(failed_state,),
        metadata={
            "terminal_success_contract": {
                "failed_terminal_error_code": "example_workflow_no_items_succeeded"
            }
        },
    )
    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    )
    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is False
    assert result.error == "example_workflow_no_items_succeeded"
    assert result.data[WORKFLOW_RESULT_ENVELOPE_KEY]["diagnostics"]["error"] == (
        "example_workflow_no_items_succeeded"
    )
    assert manager.checkpoint.call_args_list[-1].kwargs["error"] == (
        "example_workflow_no_items_succeeded"
    )


def test_durable_executor_atomically_pauses_at_successor_checkpoint() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)
    condition_spec, condition = build_transition_condition({"kind": "always"})
    definition = WorkflowDefinition(
        workflow_id="#V#durable_checkpoint_pause_probe",
        initial_state="pause_here",
        states={
            "pause_here": WorkflowStateSpec(
                state_id="pause_here",
                actions=(
                    WorkflowActionInvocation(
                        action_id="workflow_control.pause_at_checkpoint",
                        inputs={"reason_code": "certification_interruption"},
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=condition,
                        condition_spec=condition_spec,
                        reason="pause_before_resume",
                    ),
                ),
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )
    instance = _build_instance(definition.workflow_id)
    instance.locked_by = "worker-pause"
    instance.claim_token = "claim-pause"

    pause_receipt = {
        "schema_version": "workflow_checkpoint_pause_receipt.v1",
        "instance_id": instance.instance_id,
        "workflow_id": definition.workflow_id,
        "status": "paused",
        "checkpoint_state": "done",
        "checkpoint_step_index": 1,
        "request_sha256": "a" * 64,
        "manual_resume_required": True,
    }
    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.pause_claim_at_checkpoint.return_value = pause_receipt

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)
    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            instance.instance_id,
            definition,
            worker_id="worker-pause",
            claim_token="claim-pause",
            resume_from_checkpoint=False,
        )

    assert result.completed is False
    assert result.error == "paused_at_checkpoint"
    assert result.final_state == "done"
    assert result.data[WORKFLOW_CHECKPOINT_PAUSE_RECEIPT_KEY] == pause_receipt
    assert result.data[WORKFLOW_CHECKPOINT_PAUSE_EVENTS_KEY][-1]["status"] == (
        "paused"
    )
    manager.checkpoint.assert_not_called()
    pause_call = manager.pause_claim_at_checkpoint.call_args
    assert pause_call.args == (instance.instance_id,)
    assert pause_call.kwargs["prior_state"] == ""
    assert pause_call.kwargs["prior_step_index"] == 0
    assert pause_call.kwargs["request_state"] == "pause_here"
    assert pause_call.kwargs["checkpoint_state"] == "done"
    assert pause_call.kwargs["checkpoint_step_index"] == 1
    assert pause_call.kwargs["workflow_data"][
        WORKFLOW_CHECKPOINT_PAUSE_EVENTS_KEY
    ][-1]["status"] == "requested"
    assert pause_call.kwargs["worker_id"] == "worker-pause"
    assert pause_call.kwargs["claim_token"] == "claim-pause"


def test_durable_executor_injects_manager_receipts_on_same_instance_resume() -> None:
    pause_receipt = {
        "schema_version": "workflow_checkpoint_pause_receipt.v1",
        "instance_id": "instance-1",
        "status": "paused",
        "checkpoint_state": "after_pause",
        "checkpoint_step_index": 1,
        "manual_resume_required": True,
    }
    resume_receipt = {
        "schema_version": "workflow_checkpoint_resume_receipt.v1",
        "instance_id": "instance-1",
        "status": "pending",
        "checkpoint_state": "after_pause",
        "checkpoint_step_index": 1,
        "resume_count": 1,
        "same_instance_resume": True,
    }

    def _observe_receipts(request: WorkflowActionRequest) -> WorkflowActionResult:
        assert request.data[WORKFLOW_CHECKPOINT_PAUSE_RECEIPT_KEY] == pause_receipt
        assert request.data[WORKFLOW_CHECKPOINT_RESUME_RECEIPT_KEY] == resume_receipt
        return WorkflowActionResult(
            outputs={
                "same_instance_resume_seen": request.data[
                    WORKFLOW_CHECKPOINT_RESUME_RECEIPT_KEY
                ]["same_instance_resume"]
            }
        )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(action_id="lifecycle.observe", handler=_observe_receipts)
    )
    condition_spec, condition = build_transition_condition({"kind": "always"})
    definition = WorkflowDefinition(
        workflow_id="#V#durable_checkpoint_resume_probe",
        initial_state="pause_here",
        states={
            "pause_here": WorkflowStateSpec(state_id="pause_here"),
            "after_pause": WorkflowStateSpec(
                state_id="after_pause",
                actions=(WorkflowActionInvocation(action_id="lifecycle.observe"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=condition,
                        condition_spec=condition_spec,
                        reason="resume_completed",
                    ),
                ),
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )
    instance = _build_instance(definition.workflow_id)
    instance.current_state = "after_pause"
    instance.step_index = 1
    instance.workflow_data = {"checkpoint_fact": "preserved"}
    instance.checkpoint_pause_receipt = pause_receipt
    instance.checkpoint_resume_receipt = resume_receipt
    instance.checkpoint_resume_count = 1
    instance.locked_by = "worker-resume"
    instance.claim_token = "claim-resume"

    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True
    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            instance.instance_id,
            definition,
            worker_id="worker-resume",
            claim_token="claim-resume",
            resume_from_checkpoint=True,
        )

    assert result.completed is True
    assert result.final_state == "done"
    assert result.data["checkpoint_fact"] == "preserved"
    assert result.data["same_instance_resume_seen"] is True
    assert result.data[WORKFLOW_CHECKPOINT_PAUSE_RECEIPT_KEY] == pause_receipt
    assert result.data[WORKFLOW_CHECKPOINT_RESUME_RECEIPT_KEY] == resume_receipt


def test_checkpoint_context_projection_bounds_diagnostic_payloads() -> None:
    raw_body = "raw message body " + ("x" * 600_000)
    context = {
        "paper_id": "#V#paper_1",
        "result": {
            "paper_id": "#V#paper_1",
            "raw_body": raw_body,
        },
        "last_action_outputs": {
            "result": {
                "paper_id": "#V#paper_1",
                "raw_body": raw_body,
            },
            "api_token": "secret-token-value",
        },
        "workflow_step_result_envelopes": [
            {
                "state_id": "fetch",
                "output_payload": {
                    "raw_body": raw_body,
                },
            }
        ],
    }

    projected = project_workflow_context_for_checkpoint(context)

    assert projected["paper_id"] == "#V#paper_1"
    metadata = projected[CHECKPOINT_CONTEXT_PROJECTION_KEY]
    assert (
        metadata["schema_version"]
        == CHECKPOINT_CONTEXT_PROJECTION_SCHEMA_VERSION
    )
    assert metadata["projected_key_count"] >= 3
    assert BSON.encode({"workflow_data": projected})
    serialised = json.dumps(projected, sort_keys=True, default=str)
    assert raw_body not in serialised
    assert "secret-token-value" not in serialised
    assert projected["last_action_outputs"]["api_token"] == "[redacted]"
    assert projected["result"]["raw_body"]["truncated"] is True


def test_checkpoint_context_projection_preserves_declared_execution_input() -> None:
    records = [
        {
            "source_item_id": f"student:{index}",
            "evidence": "e" * 40_000,
        }
        for index in range(12)
    ]

    projected = project_workflow_context_for_checkpoint(
        {"spreadsheet_records": records},
        lossless_keys=("spreadsheet_records",),
    )

    assert projected["spreadsheet_records"] == records
    assert not any(
        item.get("truncated") is True
        for item in projected["spreadsheet_records"]
        if isinstance(item, dict)
    )
    projection = projected[CHECKPOINT_CONTEXT_PROJECTION_KEY]
    assert not any(
        item.get("key") == "spreadsheet_records"
        for item in projection["projected_keys"]
    )


def test_durable_executor_checkpoints_bounded_context_and_preserves_mapped_fields() -> None:
    raw_body = "private email plus paper text " + ("z" * 900_000)

    def _large_payload_action(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "paper_id": "#V#paper_1",
                    "raw_body": raw_body,
                },
                "mcp_result": {
                    "paper_id": "#V#paper_1",
                    "raw_body": raw_body,
                },
            },
        )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="large_payload.fetch",
            handler=_large_payload_action,
        )
    )
    condition_spec, condition = build_transition_condition({"kind": "always"})
    definition = WorkflowDefinition(
        workflow_id="#V#bounded_checkpoint_context_workflow",
        initial_state="fetch",
        states={
            "fetch": WorkflowStateSpec(
                state_id="fetch",
                actions=(WorkflowActionInvocation(action_id="large_payload.fetch"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=condition,
                        condition_spec=condition_spec,
                        reason="next_step",
                    ),
                ),
                metadata={
                    "tool_output_context_mappings": [
                        {
                            "tool_output_field": "result.paper_id",
                            "context_key": "paper_id",
                        }
                    ]
                },
            ),
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
            ),
        },
    )

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
        patch(
            "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
            return_value="trace-2314",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.data["paper_id"] == "#V#paper_1"
    assert result.data["result"]["raw_body"] == raw_body
    assert manager.checkpoint.call_args_list
    for checkpoint_call in manager.checkpoint.call_args_list:
        workflow_data = checkpoint_call.kwargs["workflow_data"]
        assert workflow_data["paper_id"] == "#V#paper_1"
        assert CHECKPOINT_CONTEXT_PROJECTION_KEY in workflow_data
        assert len(BSON.encode({"workflow_data": workflow_data})) < 1_500_000
        serialised = json.dumps(workflow_data, sort_keys=True, default=str)
        assert raw_body not in serialised


def test_durable_executor_persists_trace_and_checkpoints_trace_link() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_trace_persistence",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
            ),
        },
    )

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=ActionRegistry(), instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
        patch(
            "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
            return_value="trace-1550",
        ) as insert_trace,
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.execution_trace_id == "trace-1550"
    insert_trace.assert_called_once()
    stored_doc = insert_trace.call_args.args[0]
    assert stored_doc["instance_id"] == "instance-1"
    assert stored_doc["metadata"]["default_model"] == "test-model"
    assert any(
        isinstance(call.kwargs, dict)
        and call.kwargs.get("execution_trace_id") == "trace-1550"
        for call in manager.checkpoint.call_args_list
    )


def test_durable_executor_persists_worker_loaded_definition_identity() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_executed_definition_identity",
        initial_state="done",
        states={"done": WorkflowStateSpec(state_id="done", terminal=True)},
        termination_states=("done",),
    )
    definition_identity = build_workflow_definition_identity(
        workflow_id=definition.workflow_id,
        source="vontology",
        definition=definition,
        authoritative_definition=definition,
    )
    supplied_identity = {
        **definition_identity,
        "unexpected_diagnostic": {"must_not_be_persisted": True},
    }
    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True
    executor = DurableWorkflowExecutor(registry=ActionRegistry(), instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
        patch(
            "src.backend.workflows.durable.registry_factory._get_or_build_durable_mcp_gateway",
            return_value=None,
        ),
        patch(
            "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
            return_value="trace-definition-identity",
        ) as insert_trace,
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
            workflow_definition_identity=supplied_identity,
        )

    assert result.completed is True
    assert (
        result.data[DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY]
        == definition_identity
    )
    terminal_data = manager.checkpoint.call_args_list[-1].kwargs["workflow_data"]
    assert (
        terminal_data[DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY]
        == definition_identity
    )
    stored_trace = insert_trace.call_args.args[0]
    assert (
        stored_trace["metadata"][DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY]
        == definition_identity
    )


def test_durable_executor_reasserts_identity_after_action_overwrite() -> None:
    condition_spec, condition = build_transition_condition({"kind": "always"})
    definition = WorkflowDefinition(
        workflow_id="#V#durable_identity_action_overwrite",
        initial_state="overwrite",
        states={
            "overwrite": WorkflowStateSpec(
                state_id="overwrite",
                actions=(
                    WorkflowActionInvocation(action_id="identity.overwrite"),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=condition,
                        condition_spec=condition_spec,
                        reason="next_step",
                    ),
                ),
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )
    definition_identity = build_workflow_definition_identity(
        workflow_id=definition.workflow_id,
        source="vontology",
        definition=definition,
        authoritative_definition=definition,
    )
    forged_identity = {
        **definition_identity,
        "definition_hash": "f" * 64,
        "runtime_definition_hash": "f" * 64,
        "authoritative_definition_hash": "f" * 64,
    }

    def _overwrite_identity(
        request: WorkflowActionRequest,
    ) -> WorkflowActionResult:
        request.data[DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY] = (
            forged_identity
        )
        return WorkflowActionResult(outputs={})

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="identity.overwrite",
            handler=_overwrite_identity,
        )
    )
    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True
    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
        patch(
            "src.backend.workflows.durable.registry_factory._get_or_build_durable_mcp_gateway",
            return_value=None,
        ),
        patch(
            "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
            return_value="trace-action-overwrite-identity",
        ) as insert_trace,
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
            workflow_definition_identity=definition_identity,
        )

    assert result.completed is True
    assert (
        result.data[DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY]
        == definition_identity
    )
    assert len(manager.checkpoint.call_args_list) >= 2
    assert all(
        checkpoint_call.kwargs["workflow_data"][
            DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY
        ]
        == definition_identity
        for checkpoint_call in manager.checkpoint.call_args_list
    )
    stored_trace = insert_trace.call_args.args[0]
    assert (
        stored_trace["metadata"][DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY]
        == definition_identity
    )


@pytest.mark.parametrize("resume_from_checkpoint", [False, True])
def test_durable_executor_discards_untrusted_definition_identity_from_context(
    resume_from_checkpoint: bool,
) -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_definition_identity_forgery",
        initial_state="done",
        states={"done": WorkflowStateSpec(state_id="done", terminal=True)},
        termination_states=("done",),
    )
    instance = _build_instance(definition.workflow_id)
    instance.inputs = {
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY: {
            "workflow_id": definition.workflow_id,
            "definition_hash": "f" * 64,
            "source": "vontology",
        }
    }
    instance.workflow_data = {
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY: {
            "workflow_id": definition.workflow_id,
            "definition_hash": "e" * 64,
            "source": "vontology",
        }
    }
    instance.current_state = "done"
    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True
    executor = DurableWorkflowExecutor(registry=ActionRegistry(), instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
        patch(
            "src.backend.workflows.durable.registry_factory._get_or_build_durable_mcp_gateway",
            return_value=None,
        ),
        patch(
            "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
            return_value="trace-forged-definition-identity",
        ) as insert_trace,
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=resume_from_checkpoint,
        )

    assert result.completed is True
    assert DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY not in result.data
    terminal_data = manager.checkpoint.call_args_list[-1].kwargs["workflow_data"]
    assert DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY not in terminal_data
    stored_trace = insert_trace.call_args.args[0]
    assert (
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY
        not in stored_trace["metadata"]
    )


def test_durable_executor_retries_transient_cancel_check() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_transient_cancel_retry",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
            ),
        },
    )

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.side_effect = [
        PyMongoError("server selection timeout while reading workflow_instances"),
        False,
    ]
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=ActionRegistry(), instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
        patch(
            "src.backend.db.transient_errors.attempt_reconnect",
            return_value={"reconnected": True},
        ),
        patch(
            "src.backend.db.transient_errors.time.sleep",
            return_value=None,
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert manager.is_cancelled.call_count == 2


def test_durable_executor_uses_scoped_active_model_and_context_defaults() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_scoped_model",
        initial_state="capture",
        states={
            "capture": WorkflowStateSpec(
                state_id="capture",
                actions=(WorkflowActionInvocation(action_id="capture.action"),),
                terminal=True,
            ),
        },
    )

    captured: dict[str, object] = {}
    registry = ActionRegistry()

    def capture_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        captured["model"] = request.environment.model
        captured["user_concept_id"] = request.data.get("user_concept_id")
        captured["org_concept_id"] = request.data.get("org_concept_id")
        captured["namespace"] = request.data.get("namespace")
        captured["user_namespace"] = request.data.get("user_namespace")
        return WorkflowActionResult(outputs={"captured": True})

    registry.register(ActionSpec(action_id="capture.action", handler=capture_handler))

    manager = MagicMock()
    instance = _build_instance(definition.workflow_id)
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="scoped-model",
        ) as get_active_model_name,
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert captured == {
        "model": "scoped-model",
        "user_concept_id": instance.user_id,
        "org_concept_id": instance.org_id,
        "namespace": instance.namespace,
        "user_namespace": instance.namespace,
    }
    get_active_model_name.assert_called_once_with(
        user_concept_id=instance.user_id,
        org_concept_id=instance.org_id,
    )


def test_durable_executor_inherits_scoped_active_model_parameters() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_scoped_model_parameters",
        initial_state="capture",
        states={
            "capture": WorkflowStateSpec(
                state_id="capture",
                actions=(WorkflowActionInvocation(action_id="capture.action"),),
                terminal=True,
            ),
        },
    )
    captured: dict[str, object] = {}
    registry = ActionRegistry()

    def capture_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        captured["model"] = request.environment.model
        captured["model_parameters"] = request.environment.model_parameters
        return WorkflowActionResult(outputs={"captured": True})

    registry.register(ActionSpec(action_id="capture.action", handler=capture_handler))
    manager = MagicMock()
    instance = _build_instance(definition.workflow_id)
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True
    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="gpt-5.6-luna",
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_parameters",
            return_value={"reasoning_effort": "low"},
        ) as get_active_model_parameters,
        patch(
            "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
            return_value="trace-active-parameters",
        ) as insert_trace,
    ):
        result = executor.run_durable(
            instance.instance_id,
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert captured == {
        "model": "gpt-5.6-luna",
        "model_parameters": {"reasoning_effort": "low"},
    }
    get_active_model_parameters.assert_called_once_with(
        user_concept_id=instance.user_id,
        org_concept_id=instance.org_id,
    )
    stored_doc = insert_trace.call_args.args[0]
    assert stored_doc["metadata"]["effective_model_parameters"] == {
        "reasoning_effort": "low"
    }
    assert (
        stored_doc["metadata"]["effective_model_parameters_source"]
        == "active_setting"
    )


def test_durable_executor_overwrites_forged_actor_fields_when_resuming() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_actor_resume_authority",
        initial_state="capture",
        states={
            "capture": WorkflowStateSpec(
                state_id="capture",
                actions=(WorkflowActionInvocation(action_id="capture.actor"),),
                terminal=True,
            ),
        },
    )
    captured: dict[str, object] = {}
    registry = ActionRegistry()

    def capture_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        for field_name in (
            "user_concept_id",
            "org_concept_id",
            "organisation_concept_id",
            "namespace",
            "user_namespace",
        ):
            captured[field_name] = request.data.get(field_name)
        return WorkflowActionResult(outputs={"captured": True})

    registry.register(ActionSpec(action_id="capture.actor", handler=capture_handler))
    manager = MagicMock()
    instance = _build_instance(definition.workflow_id)
    forged_scope = {
        "user_concept_id": "#V#forged_user",
        "org_concept_id": "#V#forged_org",
        "organisation_concept_id": "#V#forged_org",
        "namespace": "#V#forged_user@forged_org",
        "user_namespace": "#V#forged_user@forged_org",
    }
    instance.inputs = dict(forged_scope)
    instance.workflow_data = dict(forged_scope)
    instance.current_state = "capture"
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)
    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="scoped-model",
        ),
    ):
        result = executor.run_durable(
            instance.instance_id,
            definition,
            resume_from_checkpoint=True,
        )

    assert result.completed is True
    assert captured == {
        "user_concept_id": instance.user_id,
        "org_concept_id": instance.org_id,
        "organisation_concept_id": instance.org_id,
        "namespace": instance.namespace,
        "user_namespace": instance.namespace,
    }


def test_durable_executor_prefers_requested_model_override_from_instance_inputs() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_requested_model_override",
        initial_state="capture",
        states={
            "capture": WorkflowStateSpec(
                state_id="capture",
                actions=(WorkflowActionInvocation(action_id="capture.action"),),
                terminal=True,
            ),
        },
    )

    captured: dict[str, object] = {}
    requested_client = MagicMock(name="requested_client")
    registry = ActionRegistry()

    def capture_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        captured["model"] = request.environment.model
        captured["requested_model"] = request.data.get("requested_model")
        captured["requested_client_type"] = request.data.get("requested_client_type")
        return WorkflowActionResult(outputs={"captured": True})

    registry.register(ActionSpec(action_id="capture.action", handler=capture_handler))

    manager = MagicMock()
    instance = _build_instance(definition.workflow_id)
    instance.inputs = {
        "requested_model": "gemma4:26b",
        "requested_client_type": "ollama",
    }
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=requested_client,
        ) as get_llm_client,
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
        ) as get_active_model_name,
        patch(
            "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
            return_value="trace-2364",
        ) as insert_trace,
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert captured == {
        "model": "gemma4:26b",
        "requested_model": "gemma4:26b",
        "requested_client_type": "ollama",
    }
    assert result.execution_trace_id == "trace-2364"
    insert_trace.assert_called_once()
    stored_doc = insert_trace.call_args.args[0]
    assert stored_doc["metadata"]["default_model"] == "gemma4:26b"
    assert stored_doc["metadata"]["requested_model"] == "gemma4:26b"
    assert stored_doc["metadata"]["requested_client_type"] == "ollama"
    assert stored_doc["metadata"]["requested_model_override_applied"] is True
    assert stored_doc["metadata"]["default_provider"] == "ollama"
    get_llm_client.assert_called_once_with(
        client_type="ollama",
        user_concept_id=instance.user_id,
        org_concept_id=instance.org_id,
    )
    get_active_model_name.assert_not_called()


def test_durable_executor_routes_to_on_failure_recovery() -> None:
    """Failed actions should transition via `on_failure` when defined."""
    definition = WorkflowDefinition(
        workflow_id="#V#durable_failure_recovery",
        initial_state="risky",
        states={
            "risky": WorkflowStateSpec(
                state_id="risky",
                actions=(WorkflowActionInvocation(action_id="risky.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="recover",
                        reason="on_failure",
                        condition=lambda ctx: bool(ctx.get("last_action_failed")),
                    ),
                ),
            ),
            "recover": WorkflowStateSpec(
                state_id="recover",
                actions=(WorkflowActionInvocation(action_id="recover.action"),),
                terminal=True,
            ),
        },
    )

    registry = ActionRegistry()

    def fail_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(status="failed", error="boom")

    def recover_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(outputs={"recovered": True})

    registry.register(ActionSpec(action_id="risky.action", handler=fail_handler))
    registry.register(ActionSpec(action_id="recover.action", handler=recover_handler))

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.final_state == "recover"
    assert result.data.get("recovered") is True

    checkpoint_errors = [
        call.kwargs.get("error")
        for call in manager.checkpoint.call_args_list
        if isinstance(call.kwargs, dict)
    ]
    assert "boom" not in checkpoint_errors


def test_durable_executor_still_fails_without_on_failure_route() -> None:
    """Without an explicit failure route, durable execution should fail-fast."""
    definition = WorkflowDefinition(
        workflow_id="#V#durable_fail_fast",
        initial_state="risky",
        states={
            "risky": WorkflowStateSpec(
                state_id="risky",
                actions=(WorkflowActionInvocation(action_id="risky.action"),),
                terminal=True,
            ),
        },
    )

    registry = ActionRegistry()

    def fail_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(status="failed", error="boom")

    registry.register(ActionSpec(action_id="risky.action", handler=fail_handler))

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is False
    assert result.final_state == "risky"
    assert result.error == "boom"
    assert any(
        isinstance(call.kwargs, dict) and call.kwargs.get("error") == "boom"
        for call in manager.checkpoint.call_args_list
    )


def test_durable_executor_preserves_failed_action_outputs_in_step_envelopes() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_failure_snapshot",
        initial_state="risky",
        states={
            "risky": WorkflowStateSpec(
                state_id="risky",
                actions=(WorkflowActionInvocation(action_id="risky.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="recover",
                        reason="on_failure",
                        condition=lambda ctx: bool(ctx.get("last_action_failed")),
                    ),
                ),
            ),
            "recover": WorkflowStateSpec(state_id="recover", terminal=True),
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="risky.action",
            handler=lambda _request: WorkflowActionResult(
                status="failed",
                error="boom",
                outputs={
                    "cache_state": "markdown_only_partial_cache",
                    "partial_cache_without_pdf": True,
                },
            ),
        )
    )

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    envelopes = result.data.get("workflow_step_result_envelopes")
    assert isinstance(envelopes, list)
    assert len(envelopes) == 1
    assert envelopes[0]["action_outcome"] == "failure"
    assert envelopes[0]["output_payload"]["cache_state"] == "markdown_only_partial_cache"
    assert envelopes[0]["output_payload"]["partial_cache_without_pdf"] is True
    assert "cache_state" not in result.data


def test_durable_executor_routes_to_on_unknown_recovery() -> None:
    """Unknown outcomes should route via explicit `on_unknown` transitions."""
    definition = WorkflowDefinition(
        workflow_id="#V#durable_unknown_recovery",
        initial_state="probe",
        states={
            "probe": WorkflowStateSpec(
                state_id="probe",
                actions=(WorkflowActionInvocation(action_id="probe.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="escalate",
                        reason="on_unknown",
                        condition=lambda ctx: bool(ctx.get("last_action_unknown")),
                    ),
                ),
            ),
            "escalate": WorkflowStateSpec(
                state_id="escalate",
                actions=(WorkflowActionInvocation(action_id="recover.action"),),
                terminal=True,
            ),
        },
    )

    registry = ActionRegistry()

    def unknown_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="unknown",
            error="insufficient_confidence",
            outputs={"probe_summary": "need escalation"},
        )

    def recover_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(outputs={"escalated": True})

    registry.register(ActionSpec(action_id="probe.action", handler=unknown_handler))
    registry.register(ActionSpec(action_id="recover.action", handler=recover_handler))

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.final_state == "escalate"
    assert result.data.get("last_action_unknown") is False
    assert result.data.get("probe_summary") == "need escalation"
    assert result.data.get("escalated") is True
    checkpoint_errors = [
        call.kwargs.get("error")
        for call in manager.checkpoint.call_args_list
        if isinstance(call.kwargs, dict)
    ]
    assert "action_unknown" not in checkpoint_errors


def test_durable_executor_fails_unknown_without_on_unknown_route() -> None:
    """Without on_unknown route, unknown outcomes should fail closed."""
    definition = WorkflowDefinition(
        workflow_id="#V#durable_unknown_fail_closed",
        initial_state="probe",
        states={
            "probe": WorkflowStateSpec(
                state_id="probe",
                actions=(WorkflowActionInvocation(action_id="probe.action"),),
                terminal=True,
            ),
        },
    )

    registry = ActionRegistry()

    def unknown_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(status="unknown")

    registry.register(ActionSpec(action_id="probe.action", handler=unknown_handler))

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is False
    assert result.final_state == "probe"
    assert result.error == "action_unknown"
    assert any(
        isinstance(call.kwargs, dict) and call.kwargs.get("error") == "action_unknown"
        for call in manager.checkpoint.call_args_list
    )


def test_durable_executor_blocks_unsatisfied_metadata_precondition() -> None:
    """Metadata precondition failures should block execution with a reason code."""
    definition = WorkflowDefinition(
        workflow_id="#V#durable_metadata_validation",
        initial_state="guarded",
        states={
            "guarded": WorkflowStateSpec(
                state_id="guarded",
                actions=(WorkflowActionInvocation(action_id="guarded.action"),),
                terminal=True,
                metadata={"preconditions": ["#V#user_authenticated"]},
            ),
        },
    )

    registry = ActionRegistry()
    calls = {"count": 0}

    def guarded_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"ok": True})

    registry.register(ActionSpec(action_id="guarded.action", handler=guarded_handler))

    instance = _build_instance(definition.workflow_id)
    instance.inputs = {"user_authenticated": False}

    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is False
    assert result.final_state == "guarded"
    assert result.error is not None
    assert result.error.startswith(
        "metadata_validation_failed:metadata_precondition_unsatisfied:guarded:"
    )
    assert calls["count"] == 0
    assert any(
        isinstance(call.kwargs, dict) and call.kwargs.get("error") == result.error
        for call in manager.checkpoint.call_args_list
    )


def test_durable_executor_warn_mode_records_metadata_failure_without_blocking(
    monkeypatch,
) -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_metadata_warn",
        initial_state="guarded",
        states={
            "guarded": WorkflowStateSpec(
                state_id="guarded",
                actions=(WorkflowActionInvocation(action_id="guarded.action"),),
                terminal=True,
                metadata={"preconditions": ["#V#user_authenticated"]},
            ),
        },
    )

    registry = ActionRegistry()
    calls = {"count": 0}

    def guarded_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"ok": True})

    registry.register(ActionSpec(action_id="guarded.action", handler=guarded_handler))

    instance = _build_instance(definition.workflow_id)
    instance.inputs = {"user_authenticated": False}

    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)
    monkeypatch.setenv("VON_WORKFLOW_METADATA_VALIDATION_MODE", "warn")

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.final_state == "guarded"
    assert result.error is None
    assert calls["count"] == 1
    events = result.data.get("workflow_metadata_validation_events")
    assert isinstance(events, list)
    assert len(events) == 1
    pre_action = events[0]
    assert pre_action.get("phase") == "pre_action"
    assert pre_action.get("ok") is False
    assert pre_action.get("mode") == "warn"
    assert pre_action.get("enforced") is False
    assert pre_action.get("reason_code") == "metadata_precondition_unsatisfied"


def test_durable_executor_off_mode_skips_metadata_checks(monkeypatch) -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_metadata_off",
        initial_state="guarded",
        states={
            "guarded": WorkflowStateSpec(
                state_id="guarded",
                actions=(WorkflowActionInvocation(action_id="guarded.action"),),
                terminal=True,
                metadata={"preconditions": ["#V#user_authenticated"]},
            ),
        },
    )

    registry = ActionRegistry()
    calls = {"count": 0}

    def guarded_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"ok": True})

    registry.register(ActionSpec(action_id="guarded.action", handler=guarded_handler))

    instance = _build_instance(definition.workflow_id)
    instance.inputs = {"user_authenticated": False}

    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)
    monkeypatch.setenv("VON_WORKFLOW_METADATA_VALIDATION_MODE", "off")

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.final_state == "guarded"
    assert result.error is None
    assert calls["count"] == 1
    events = result.data.get("workflow_metadata_validation_events")
    assert isinstance(events, list)
    assert len(events) == 1
    assert all(event.get("mode") == "off" for event in events)
    assert all(event.get("enforced") is False for event in events)
    assert all(event.get("skipped") is True for event in events)
    assert all(
        event.get("skip_reason") == "disabled_by_rollout_mode" for event in events
    )


def test_durable_executor_materialises_terminal_effect_evidence() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_terminal_effect_validation",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
                metadata={
                    "effects": [
                        (
                            "#V#workflow_effect_"
                            "durable_terminal_effect_validation_done_terminal"
                        )
                    ]
                },
            ),
        },
    )

    instance = _build_instance(definition.workflow_id)
    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    )

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    terminal_effect_id = (
        "#V#workflow_effect_durable_terminal_effect_validation_done_terminal"
    )
    assert result.completed is True
    assert result.error is None
    assert result.data.get(terminal_effect_id) is True
    assert (
        result.data.get(
            "workflow_effect_durable_terminal_effect_validation_done_terminal"
        )
        is True
    )
    events = result.data.get("workflow_terminal_effect_events")
    assert isinstance(events, list)
    assert events
    assert events[-1].get("status") == "terminal_effect_materialised"
    assert events[-1].get("symbol") == terminal_effect_id


def test_durable_executor_applies_output_mapping_before_metadata_validation() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_output_mapping_validation",
        initial_state="write",
        states={
            "write": WorkflowStateSpec(
                state_id="write",
                actions=(WorkflowActionInvocation(action_id="tool.write"),),
                terminal=True,
                metadata={
                    "writes_context_keys": [
                        "#V#workflow_context_key_validated_type_id",
                    ],
                    "tool_output_context_mappings": [
                        {
                            "mapping_concept_id": "#V#workflow_mapping_tool_field_concept_id_to_validated_type_id",
                            "tool_output_field": "concept_id",
                            "context_key": "validated_type_id",
                        }
                    ],
                },
            ),
        },
    )

    registry = ActionRegistry()

    def write_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(outputs={"result": {"concept_id": "#V#person"}})

    registry.register(ActionSpec(action_id="tool.write", handler=write_handler))

    instance = _build_instance(definition.workflow_id)
    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.error is None
    assert result.data.get("validated_type_id") == "#V#person"
    events = result.data.get("workflow_tool_output_mapping_events")
    assert isinstance(events, list)
    assert len(events) == 1
    assert events[0].get("tool_output_field") == "concept_id"
    assert events[0].get("context_key") == "validated_type_id"
    assert events[0].get("value_present") is True


def test_durable_executor_populates_result_envelope_for_return_signal() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_return_signal",
        initial_state="compute",
        states={
            "compute": WorkflowStateSpec(
                state_id="compute",
                actions=(WorkflowActionInvocation(action_id="emit.return"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        reason="next_step",
                        condition=lambda _ctx: True,
                    ),
                ),
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="emit.return",
            handler=lambda _request: WorkflowActionResult(
                outputs={
                    "control_signal": "return",
                    "return_payload": {"answer": "done"},
                }
            ),
        )
    )

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)
    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.final_state == "compute"
    assert isinstance(result.result_envelope, dict)
    assert result.result_envelope.get("control_signal") == "return"
    assert result.result_envelope.get("declared_output_payload") == {"answer": "done"}


def test_durable_executor_records_plan_state_checkpoint_progress() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_plan_state_progress",
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
                        "summary_context_keys": ["dispatch.completed"],
                        "cursor_context_keys": ["page_cursor"],
                        "progress_message": "Dispatching",
                        "force_summary": True,
                    }
                },
            ),
        },
        termination_states=("dispatch",),
        metadata={
            "plan_state_policy": {
                "schema_version": "workflow_plan_state_policy.v1",
                "plan_items": ["dispatch"],
                "summary_interval_steps": 1,
            },
            "completion_gate": {
                "schema_version": "workflow_completion_gate.v1",
                "required_done_plan_items": ["dispatch"],
                "required_context_keys": ["dispatch.completed"],
            },
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="dispatch.action",
            handler=lambda _request: WorkflowActionResult(
                outputs={
                    "dispatch": {"completed": True},
                    "page_cursor": "cursor-2",
                }
            ),
        )
    )

    instance = _build_instance(definition.workflow_id)
    instance.inputs = {"page_cursor": "cursor-1"}

    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)
    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    plan_state = result.data.get("workflow_plan_state")
    assert isinstance(plan_state, dict)
    assert plan_state["items"]["dispatch"]["status"] == "done"
    gate = result.data.get("workflow_completion_gate")
    assert isinstance(gate, dict)
    assert gate["safe_to_claim_completion"] is True
    assert manager.checkpoint.call_args_list
    assert any(
        call.kwargs.get("progress_current") == 1
        and call.kwargs.get("progress_total") == 1
        and call.kwargs.get("progress_message") == "Dispatching"
        for call in manager.checkpoint.call_args_list
        if isinstance(call.kwargs, dict)
    )


def test_durable_executor_blocks_false_terminal_success_when_completion_gate_unmet() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_completion_gate_unmet",
        initial_state="dispatch",
        states={
            "dispatch": WorkflowStateSpec(
                state_id="dispatch",
                actions=(WorkflowActionInvocation(action_id="dispatch.action"),),
                terminal=True,
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

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)
    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is False
    assert result.error is not None
    assert result.error.startswith("workflow_completion_gate_unmet")
    gate = result.data.get("workflow_completion_gate")
    assert isinstance(gate, dict)
    assert gate["safe_to_claim_completion"] is False
    assert "required_plan_items_unmet" in gate["blocking_reason_codes"]
    assert "missing_required_context_keys" in gate["blocking_reason_codes"]


def test_durable_executor_matches_base_executor_for_approval_gate_routing() -> None:
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
        workflow_id="#V#durable_approval_gate_parity",
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

    base_result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    durable_executor = DurableWorkflowExecutor(
        registry=registry,
        instance_manager=manager,
    )
    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        durable_result = durable_executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert calls["count"] == 0
    assert durable_result.completed is base_result.completed is True
    assert durable_result.final_state == base_result.final_state == "blocked"
    assert durable_result.data.get("approval_required") == base_result.data.get(
        "approval_required"
    )
    assert durable_result.data.get("approval_state") == base_result.data.get(
        "approval_state"
    )
    durable_events = durable_result.data.get("workflow_approval_gate_events")
    base_events = base_result.data.get("workflow_approval_gate_events")
    assert isinstance(durable_events, list)
    assert isinstance(base_events, list)
    assert durable_events[0]["decision"] == base_events[0]["decision"] == "approval_required"


def test_durable_executor_retries_until_success_with_shared_runtime_policy(
    monkeypatch,
) -> None:
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
        workflow_id="#V#durable_retry_policy_workflow",
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
    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    monkeypatch.setattr("src.backend.workflows.engine._sleep_retry_delay", _sleep)

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)
    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
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


def test_durable_executor_reuses_idempotent_action_result_with_shared_runtime_policy() -> None:
    next_spec, next_condition = build_transition_condition({"kind": "always"})
    calls = {"count": 0}

    def _write_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"write_completed": True})

    definition = WorkflowDefinition(
        workflow_id="#V#durable_idempotency_policy_workflow",
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
    instance = _build_instance(definition.workflow_id)
    instance.inputs = {"request_id": "req-1"}

    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)
    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        first_result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )
        instance.inputs = first_result.data
        second_result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert first_result.completed is True
    assert second_result.completed is True
    assert calls["count"] == 1
    assert second_result.data.get("write_completed") is True
    idempotency_events = second_result.data.get("workflow_idempotency_events")
    assert isinstance(idempotency_events, list)
    assert any(event.get("status") == "idempotent_reuse" for event in idempotency_events)
