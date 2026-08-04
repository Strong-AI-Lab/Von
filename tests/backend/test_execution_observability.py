from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pymongo.errors import PyMongoError

from src.backend.workflows.durable.execution_observability import (
    await_workflow_terminal_state,
    build_workflow_execution_trace_summary,
    build_workflow_execution_response,
)
from src.backend.workflows.durable.workflow_instance_submission_service import (
    WorkflowInstanceSubmissionResult,
)


def test_await_workflow_terminal_state_retries_transient_get_instance() -> None:
    manager = SimpleNamespace(
        get_instance=MagicMock(
            side_effect=[
                PyMongoError(
                    "server selection timeout while reading workflow_instances"
                ),
                SimpleNamespace(status="completed"),
            ]
        )
    )

    with (
        patch(
            "src.backend.db.transient_errors.attempt_reconnect",
            return_value={"reconnected": True},
        ),
        patch(
            "src.backend.db.transient_errors.time.sleep",
            return_value=None,
        ),
    ):
        result = await_workflow_terminal_state(
            manager,
            "instance-1",
            timeout_seconds=30,
            poll_interval_seconds=0,
        )

    assert result.timed_out is False
    assert result.final_status == "completed"
    assert manager.get_instance.call_count == 2


def test_await_workflow_terminal_state_does_not_sleep_past_observation_deadline(
) -> None:
    manager = SimpleNamespace(
        get_instance=MagicMock(return_value=SimpleNamespace(status="running"))
    )

    with (
        patch(
            "src.backend.workflows.durable.execution_observability.monotonic",
            side_effect=[0.0, 0.0, 90.0],
        ),
        patch(
            "src.backend.workflows.durable.execution_observability.sleep"
        ) as sleep_mock,
    ):
        result = await_workflow_terminal_state(
            manager,
            "instance-1",
            timeout_seconds=90,
            poll_interval_seconds=1000,
        )

    assert result.timed_out is True
    assert manager.get_instance.call_count == 2
    sleep_mock.assert_called_once_with(90.0)


def test_trace_summary_surfaces_terminal_outcome_receipt_from_action_outputs() -> None:
    receipt = {
        "schema_version": "terminal_outcome_receipt.v1",
        "profile_concept_id": "#V#terminal_outcome_receipt",
        "outcome": "recoverable_failure",
        "cause_code": "verification_mismatch",
        "causal_stage": "verification",
        "summary": "Read-back did not establish the represented postcondition.",
        "evidence_refs": [{"source": "readback", "ref": "check-1"}],
        "committed_effects": [{"effect_id": "effect-write"}],
        "remaining_obligations": [{"effect_id": "effect-readback"}],
        "retryability": "now",
        "recovery_affordances": [{"action_type": "inspect"}],
        "learning_candidate": None,
        "redaction_status": "safe_projection",
        "provenance": {"decision_source": "represented_llm"},
    }
    summary = build_workflow_execution_trace_summary(
        {
            "execution_id": "trace-1",
            "workflow_id": "#V#synthetic_workflow",
            "status": "completed",
            "actions": [
                {
                    "action_id": "turn_execution.critic",
                    "status": "success",
                    "outputs": {"terminal_outcome_receipt": receipt},
                }
            ],
            "steps": [],
        }
    )

    projection = summary["terminal_outcome_receipt_projection"]
    assert projection["available"] is True
    assert projection["outcome"] == "recoverable_failure"
    assert projection["causal_stage"] == "verification"


def test_workflow_execution_response_marks_queued_timeout_as_not_started() -> None:
    submission = WorkflowInstanceSubmissionResult(
        success=True,
        workflow_id="#V#arxiv_paper_representation_workflow",
        status="created",
        instance_id="instance-queued-1",
        verification={"runnable_verification_success": True},
        created_new=True,
    )
    instance = SimpleNamespace(
        to_status_dict=lambda: {
            "instance_id": "instance-queued-1",
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "status": "pending",
            "current_state": "queued",
            "step_index": 0,
            "started_at": None,
            "progress": {"message": "queued"},
            "outputs": {},
            "execution_trace_id": None,
        },
        status="pending",
        workflow_data={},
    )

    payload = build_workflow_execution_response(
        submission,
        workflow_inputs={"arxiv_id": "2406.15341"},
        instance=instance,
        await_terminal=True,
        timeout_seconds=180,
        poll_interval_seconds=1,
        poll_count=181,
        timed_out=True,
        durable_system_status={
            "worker_running": False,
            "instances": {"pending": 1, "running": 0},
        },
    )

    execution = payload.get("workflow_execution") or {}
    assert payload.get("success") is False
    assert payload.get("error_code") == "workflow_worker_unavailable"
    assert payload.get("timed_out") is True
    assert execution.get("execution_state") == "not_started"
    assert execution.get("failure_code") == "workflow_worker_unavailable"
    assert execution.get("failure_family") == "workflow_instance_never_started"
    assert execution.get("durable_system_status", {}).get("worker_running") is False
    assert execution.get("queue_diagnostic", {}).get("worker_running") is False


def test_workflow_execution_response_marks_running_worker_claim_timeout() -> None:
    submission = WorkflowInstanceSubmissionResult(
        success=True,
        workflow_id="#V#arxiv_paper_representation_workflow",
        status="created",
        instance_id="instance-queued-2",
        verification={"runnable_verification_success": True},
        created_new=True,
    )
    instance = SimpleNamespace(
        to_status_dict=lambda: {
            "instance_id": "instance-queued-2",
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "status": "pending",
            "current_state": "queued",
            "step_index": 0,
            "started_at": None,
            "progress": {"message": "queued"},
            "outputs": {},
            "execution_trace_id": None,
        },
        status="pending",
        workflow_data={},
    )

    payload = build_workflow_execution_response(
        submission,
        workflow_inputs={"arxiv_id": "2406.15341"},
        instance=instance,
        await_terminal=True,
        timeout_seconds=180,
        poll_interval_seconds=1,
        poll_count=181,
        timed_out=True,
        durable_system_status={
            "worker_running": True,
            "instances": {"pending": 2, "running": 1},
        },
    )

    execution = payload.get("workflow_execution") or {}
    assert payload.get("success") is False
    assert payload.get("error_code") == "workflow_worker_did_not_claim_instance"
    assert execution.get("failure_family") == "workflow_instance_never_started"
    assert execution.get("queue_diagnostic") == {
        "authority_surface": "durable_system_status",
        "worker_running": True,
        "pending_instance_count": 2,
        "running_instance_count": 1,
    }
