"""Focused worker regression tests for stale-running recovery hardening."""

from __future__ import annotations

import json
from threading import Thread
from types import SimpleNamespace
from typing import Any, cast

from src.backend.workflows.action_registry import ActionRegistry
from src.backend.workflows.durable.durable_executor import DurableWorkflowResult
from src.backend.workflows.durable.models import (
    WorkflowInstance,
    WorkflowInstanceStatus,
)
from src.backend.workflows.durable.worker import DurableWorkflowWorker


class _WorkerManagerStub:
    def __init__(self) -> None:
        self.release_lock_calls: list[tuple[str, str]] = []
        self.mark_completed_calls: list[dict[str, Any]] = []
        self.mark_failed_calls: list[dict[str, Any]] = []
        self.release_lock_error: Exception | None = None
        self.mark_completed_error: Exception | None = None
        self.mark_failed_error: Exception | None = None

    def release_lock(self, instance_id: str, worker_id: str) -> bool:
        self.release_lock_calls.append((instance_id, worker_id))
        if self.release_lock_error is not None:
            raise self.release_lock_error
        return True

    def mark_failed(self, instance_id: str, **kwargs: Any) -> bool:
        call = {"instance_id": instance_id}
        call.update(kwargs)
        self.mark_failed_calls.append(call)
        if self.mark_failed_error is not None:
            raise self.mark_failed_error
        return True

    def mark_completed(self, instance_id: str, **kwargs: Any) -> bool:
        call = {"instance_id": instance_id}
        call.update(kwargs)
        self.mark_completed_calls.append(call)
        if self.mark_completed_error is not None:
            raise self.mark_completed_error
        return True


def _build_instance() -> WorkflowInstance:
    instance = WorkflowInstance.create(
        "#V#workflow_introspection_maintenance_workflow",
        user_id="#V#user",
        org_id="#V#org",
        namespace="#V#user@org",
    )
    instance.status = WorkflowInstanceStatus.RUNNING
    return instance


def test_worker_cleanup_removes_tracking_even_if_release_lock_fails() -> None:
    manager = _WorkerManagerStub()
    manager.release_lock_error = RuntimeError("mongo_timeout")
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
    )
    instance = _build_instance()
    worker._current_instances[instance.instance_id] = Thread()

    worker._process_instance(instance)

    assert instance.instance_id not in worker._current_instances
    assert manager.release_lock_calls == [(instance.instance_id, "worker-1548")]
    assert manager.mark_failed_calls
    assert manager.mark_failed_calls[0]["increment_retry"] is False


def test_worker_swallows_mark_failed_errors_during_exception_path() -> None:
    manager = _WorkerManagerStub()
    manager.mark_failed_error = RuntimeError("mongo_timeout")
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=cast(
            Any,
            lambda _workflow_id: SimpleNamespace(workflow_id=_workflow_id),
        ),
    )
    instance = _build_instance()
    worker._current_instances[instance.instance_id] = Thread()
    worker._executor = cast(
        Any,
        SimpleNamespace(
            run_durable=lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("executor blew up")
            )
        ),
    )

    worker._process_instance(instance)

    assert instance.instance_id not in worker._current_instances
    assert manager.release_lock_calls == [(instance.instance_id, "worker-1548")]
    assert manager.mark_failed_calls
    assert manager.mark_failed_calls[0]["error"].startswith("worker_exception:")


def test_worker_persists_bounded_failed_outputs_from_result_context() -> None:
    manager = _WorkerManagerStub()
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=cast(
            Any,
            lambda _workflow_id: SimpleNamespace(workflow_id=_workflow_id),
        ),
    )
    instance = _build_instance()
    worker._current_instances[instance.instance_id] = Thread()
    raw_response = "{" + ("x" * 3000)
    worker._executor = cast(
        Any,
        SimpleNamespace(
            run_durable=lambda *args, **kwargs: DurableWorkflowResult(
                instance_id=instance.instance_id,
                data={
                    "last_metadata_validation": {
                        "state_id": "infer_expected_outcome",
                        "ok": False,
                        "reason_code": "metadata_write_context_key_missing",
                        "details": {"symbol": "turn_expected_grounding_requirement"},
                    },
                    "workflow_metadata_validation_events": [
                        {"state_id": "infer_expected_outcome", "ok": False}
                    ],
                    "workflow_step_result_envelopes": [
                        {
                            "state_id": "infer_expected_outcome",
                            "action_id": "llm.action",
                            "action_status": "failed",
                            "action_outcome": "failure",
                            "diagnostics": {"error": "json_parse_failed:unparsed"},
                            "output_payload": {
                                "llm_step_response": raw_response,
                                "validated_json_raw_response": raw_response,
                                "prompt": "do not persist the full prompt",
                                "api_token": "secret-token-value",
                            },
                        }
                    ],
                    "last_workflow_step_result_envelope": {
                        "state_id": "infer_expected_outcome",
                        "action_id": "llm.action",
                        "action_status": "failed",
                        "action_outcome": "failure",
                        "diagnostics": {"error": "json_parse_failed:unparsed"},
                        "output_payload": {
                            "llm_step_response": raw_response,
                            "validated_json_raw_response": raw_response,
                            "prompt": "do not persist the full prompt",
                            "api_token": "secret-token-value",
                        },
                    },
                },
                completed=False,
                final_state="infer_expected_outcome",
                error=(
                    "metadata_validation_failed:"
                    "metadata_write_context_key_missing:"
                    "infer_expected_outcome:"
                    "turn_expected_grounding_requirement"
                ),
                execution_trace_id="trace-failed-1",
            )
        ),
    )

    worker._process_instance(instance)

    assert manager.mark_failed_calls
    outputs = manager.mark_failed_calls[0]["outputs"]
    assert outputs["schema_version"] == "workflow_failed_outputs.v1"
    assert outputs["error_step"] == "infer_expected_outcome"
    diagnostics = outputs["failed_action_diagnostics"]
    assert diagnostics["state_id"] == "infer_expected_outcome"
    assert diagnostics["action_id"] == "llm.action"
    assert diagnostics["output_keys"] == [
        "api_token",
        "llm_step_response",
        "prompt",
        "validated_json_raw_response",
    ]
    compact_payload = diagnostics["output_payload"]
    assert compact_payload["llm_step_response"]["truncated"] is True
    assert compact_payload["llm_step_response"]["char_count"] == len(raw_response)
    assert len(compact_payload["llm_step_response"]["text_preview"]) == 2000
    assert compact_payload["api_token"] == "[redacted]"
    assert compact_payload["prompt"]["suppressed"] is True
    assert (
        outputs["metadata_validation"]["last_event"]["reason_code"]
        == "metadata_write_context_key_missing"
    )


def test_worker_persists_bounded_completed_outputs_from_declared_payload() -> None:
    manager = _WorkerManagerStub()
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=cast(
            Any,
            lambda _workflow_id: SimpleNamespace(workflow_id=_workflow_id),
        ),
    )
    instance = _build_instance()
    worker._current_instances[instance.instance_id] = Thread()
    raw_message_body = "private message body " + ("x" * 20_000)
    raw_document_text = "document text " + ("y" * 20_000)
    result_envelope = {
        "schema_version": "workflow_result_envelope.v1",
        "workflow_id": "#V#large_payload_workflow",
        "completed": True,
        "terminal_status": "completed",
        "final_state": "done",
        "declared_output_payload": {
            "artefact_concept_id": "#V#artefact_1",
            "file_copy_concept_id": "#V#file_1",
            "completion_marker": {
                "marker_name": "processed",
                "source_item_id": "message-1",
            },
            "evidence_note": "e" * 3_000,
        },
    }
    worker._executor = cast(
        Any,
        SimpleNamespace(
            run_durable=lambda *args, **kwargs: DurableWorkflowResult(
                instance_id=instance.instance_id,
                data={
                    "raw_source_item": {
                        "body": raw_message_body,
                        "headers": {"authorization": "Bearer secret"},
                    },
                    "raw_document_text": raw_document_text,
                    "workflow_result_envelope": result_envelope,
                    "workflow_step_result_envelopes": [
                        {
                            "state_id": "mark_done",
                            "action_id": "workflow_mcp.invoke_tool",
                            "action_status": "success",
                            "action_outcome": "success",
                        }
                    ],
                },
                completed=True,
                final_state="done",
                result_envelope=result_envelope,
                execution_trace_id="trace-completed-1",
            )
        ),
    )

    worker._process_instance(instance)

    assert manager.mark_completed_calls
    outputs = manager.mark_completed_calls[0]["outputs"]
    assert outputs["schema_version"] == "workflow_completed_outputs.v1"
    assert outputs["terminal_status"] == "completed"
    assert outputs["artefact_concept_id"] == "#V#artefact_1"
    assert outputs["file_copy_concept_id"] == "#V#file_1"
    assert outputs["completion_marker"] == {
        "marker_name": "processed",
        "source_item_id": "message-1",
    }
    assert outputs["evidence_note"]["truncated"] is True
    assert outputs["terminal_output_source"] == "declared_output_payload"
    assert "raw_source_item" not in outputs
    assert "raw_document_text" not in outputs
    serialised = json.dumps(outputs, sort_keys=True)
    assert raw_message_body not in serialised
    assert raw_document_text not in serialised
    assert len(serialised) < 10_000
