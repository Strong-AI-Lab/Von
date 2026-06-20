"""Focused worker regression tests for stale-running recovery hardening."""

from __future__ import annotations

import json
from threading import Thread
from types import SimpleNamespace
from typing import Any, cast

import pytest

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


class _PollManagerStub:
    def __init__(self, general_instances: list[WorkflowInstance]) -> None:
        self.general_instances = general_instances
        self.calls: list[dict[str, Any]] = []
        self.heartbeat_calls: list[dict[str, Any]] = []
        self.stopped_calls: list[dict[str, Any]] = []

    def find_and_claim_instance(
        self,
        worker_id: str,
        *,
        workflow_ids: list[str] | None = None,
        priority_only: bool = False,
        worker_build_identity: dict[str, Any] | None = None,
    ) -> WorkflowInstance | None:
        self.calls.append(
            {
                "worker_id": worker_id,
                "workflow_ids": workflow_ids,
                "priority_only": priority_only,
                "worker_build_identity": worker_build_identity,
            }
        )
        if priority_only:
            return None
        if not self.general_instances:
            return None
        return self.general_instances.pop(0)

    def upsert_worker_heartbeat(self, **kwargs: Any) -> bool:
        self.heartbeat_calls.append(dict(kwargs))
        return True

    def mark_worker_stopped(self, **kwargs: Any) -> bool:
        self.stopped_calls.append(dict(kwargs))
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


def _build_pending_instance(workflow_id: str, instance_id: str) -> WorkflowInstance:
    instance = WorkflowInstance.create(
        workflow_id,
        user_id="#V#user",
        org_id="#V#org",
        namespace="#V#user@org",
    )
    instance.instance_id = instance_id
    instance.status = WorkflowInstanceStatus.RUNNING
    return instance


def test_worker_reserves_capacity_for_priority_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_DURABLE_WORKER_PRIORITY_RESERVED_SLOTS", "1")
    general_instances = [
        _build_pending_instance("#V#episode_evaluation_workflow", f"background-{index}")
        for index in range(5)
    ]
    manager = _PollManagerStub(general_instances)
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
        batch_size=5,
    )
    worker._running = True
    worker._process_instance = cast(Any, lambda _instance: None)

    worker._poll_once()

    assert [call["priority_only"] for call in manager.calls] == [
        True,
        False,
        False,
        False,
        False,
    ]
    assert all(
        call["worker_build_identity"]["worker_id"] == "worker-1548"
        for call in manager.calls
    )
    assert len(worker._current_instances) == 4
    assert len(manager.general_instances) == 1
    assert manager.heartbeat_calls
    assert manager.heartbeat_calls[-1]["worker_id"] == "worker-1548"
    assert len(manager.heartbeat_calls[-1]["active_instance_ids"]) == 4


def test_worker_defers_general_claims_under_live_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a user turn is live, only the priority claim is attempted."""
    monkeypatch.setenv("VON_DURABLE_WORKER_PAUSE_BACKGROUND_UNDER_LIVE_LOAD", "1")
    monkeypatch.setenv("VON_DURABLE_WORKER_LIVE_LOAD_THRESHOLD", "1")
    general_instances = [
        _build_pending_instance("#V#episode_evaluation_workflow", f"background-{index}")
        for index in range(5)
    ]
    manager = _PollManagerStub(general_instances)
    worker = DurableWorkflowWorker(
        worker_id="worker-live-load",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
        batch_size=5,
        live_load_getter=lambda: 1,
    )
    worker._running = True
    worker._process_instance = cast(Any, lambda _instance: None)

    worker._poll_once()

    # Only the priority-only claim is attempted; no general/background claims.
    assert [call["priority_only"] for call in manager.calls] == [True]
    assert len(worker._current_instances) == 0
    assert len(manager.general_instances) == 5


def test_worker_does_not_defer_when_no_live_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no live turn in flight, background claims proceed normally."""
    monkeypatch.setenv("VON_DURABLE_WORKER_PAUSE_BACKGROUND_UNDER_LIVE_LOAD", "1")
    monkeypatch.setenv("VON_DURABLE_WORKER_LIVE_LOAD_THRESHOLD", "1")
    general_instances = [
        _build_pending_instance("#V#episode_evaluation_workflow", f"background-{index}")
        for index in range(3)
    ]
    manager = _PollManagerStub(general_instances)
    worker = DurableWorkflowWorker(
        worker_id="worker-no-live-load",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
        batch_size=5,
        live_load_getter=lambda: 0,
    )
    worker._running = True
    worker._process_instance = cast(Any, lambda _instance: None)

    worker._poll_once()

    assert any(call["priority_only"] is False for call in manager.calls)
    assert len(worker._current_instances) == 3


def test_worker_deferral_disabled_by_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferral can be turned off entirely via env."""
    monkeypatch.setenv("VON_DURABLE_WORKER_PAUSE_BACKGROUND_UNDER_LIVE_LOAD", "0")
    general_instances = [
        _build_pending_instance("#V#episode_evaluation_workflow", "background-0")
    ]
    manager = _PollManagerStub(general_instances)
    worker = DurableWorkflowWorker(
        worker_id="worker-deferral-off",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
        batch_size=5,
        live_load_getter=lambda: 10,
    )
    assert worker._should_defer_background_work() is False


def test_worker_live_load_getter_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throwing live-load getter must not block background work."""
    monkeypatch.setenv("VON_DURABLE_WORKER_PAUSE_BACKGROUND_UNDER_LIVE_LOAD", "1")

    def _boom() -> int:
        raise RuntimeError("signal unavailable")

    manager = _PollManagerStub([])
    worker = DurableWorkflowWorker(
        worker_id="worker-getter-fail",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
        batch_size=5,
        live_load_getter=_boom,
    )
    assert worker._should_defer_background_work() is False


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
