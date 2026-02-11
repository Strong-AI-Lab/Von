"""Tests for workflow event integration service (JVNAUTOSCI-1090 / WS6)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.backend.services.workflow_event_integration_service import (
    EVENT_TYPE_TASK_CREATED,
    launch_event_workflow,
    maybe_launch_task_status_workflow,
)


def test_launch_event_workflow_integration_flag_disables_trigger(monkeypatch) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "0")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    result = launch_event_workflow(
        event_type=EVENT_TYPE_TASK_CREATED,
        event_id="task-1",
        user_id="#V#user_alice",
        org_id="#V#org_nao",
    )

    assert result["success"] is False
    assert result["triggered"] is False
    assert result["event_type"] == EVENT_TYPE_TASK_CREATED
    assert result["reason"] == "integration_disabled"


def test_launch_event_workflow_durable_gate_disables_trigger(monkeypatch) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "0")

    result = launch_event_workflow(
        event_type=EVENT_TYPE_TASK_CREATED,
        event_id="task-1",
        user_id="#V#user_alice",
        org_id="#V#org_nao",
    )

    assert result["success"] is False
    assert result["triggered"] is False
    assert result["event_type"] == EVENT_TYPE_TASK_CREATED
    assert result["reason"] == "durable_disabled"
    assert "hint" in result


def test_launch_event_workflow_requires_configured_workflow(monkeypatch) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")
    monkeypatch.delenv("VON_EVENT_TASK_CREATED_WORKFLOW_ID", raising=False)

    result = launch_event_workflow(
        event_type=EVENT_TYPE_TASK_CREATED,
        event_id="task-1",
        user_id="#V#user_alice",
        org_id="#V#org_nao",
    )

    assert result["success"] is False
    assert result["triggered"] is False
    assert result["event_type"] == EVENT_TYPE_TASK_CREATED
    assert result["reason"] == "workflow_not_configured"
    assert result["workflow_id_env"] == "VON_EVENT_TASK_CREATED_WORKFLOW_ID"


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_launch_event_workflow_creates_instance_when_configured(
    mock_get_instance_manager: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")
    monkeypatch.setenv("VON_EVENT_TASK_CREATED_WORKFLOW_ID", "#V#task_event_workflow")

    mock_manager = MagicMock()
    mock_manager.create_instance_for_event.return_value = ("instance-123", True)
    mock_get_instance_manager.return_value = mock_manager

    result = launch_event_workflow(
        event_type=EVENT_TYPE_TASK_CREATED,
        event_id="task-1",
        user_id="#V#user_alice",
        org_id="#V#org_nao",
        inputs={"task_concept_id": "#V#task_1"},
    )

    assert result["success"] is True
    assert result["triggered"] is True
    assert result["workflow_id"] == "#V#task_event_workflow"
    assert result["instance_id"] == "instance-123"

    called_args = mock_manager.create_instance_for_event.call_args
    assert called_args is not None
    assert called_args.args[0] == "#V#task_event_workflow"
    assert called_args.kwargs["source_event_type"] == EVENT_TYPE_TASK_CREATED
    assert called_args.kwargs["source_event_id"] == "task-1"
    assert called_args.kwargs["namespace"] == "#V#user_alice/#V#org_nao"
    assert called_args.kwargs["event_idempotency_key"].startswith(
        "evt:task.created:",
    )


def test_maybe_launch_task_status_workflow_skips_unconfigured_status(monkeypatch) -> None:
    monkeypatch.setenv("VON_EVENT_TASK_STATUS_TRIGGER_VALUES", "completed")

    result = maybe_launch_task_status_workflow(
        task_concept_id="#V#task_1",
        previous_status="pending",
        new_status="in_progress",
        updated_at_iso="2026-02-08T10:00:00+00:00",
        created_by_concept_id="#V#user_alice",
        organisation_concept_id="#V#org_nao",
    )

    assert result["success"] is False
    assert result["triggered"] is False
    assert result["event_type"] == "task.status_changed"
    assert result["reason"] == "status_not_configured_for_trigger"


@patch("src.backend.services.workflow_event_integration_service.launch_event_workflow")
def test_maybe_launch_task_status_workflow_triggers_configured_status(
    mock_launch_event_workflow: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_TASK_STATUS_TRIGGER_VALUES", "completed")
    mock_launch_event_workflow.return_value = {"success": True, "triggered": True}

    result = maybe_launch_task_status_workflow(
        task_concept_id="#V#task_1",
        previous_status="in_progress",
        new_status="completed",
        updated_at_iso="2026-02-08T10:00:00+00:00",
        created_by_concept_id="#V#user_alice",
        organisation_concept_id="#V#org_nao",
    )

    assert result == {"success": True, "triggered": True}

    called_args = mock_launch_event_workflow.call_args
    assert called_args is not None
    assert called_args.kwargs["event_type"] == "task.status_changed"
    assert (
        called_args.kwargs["event_id"]
        == "#V#task_1:in_progress->completed:2026-02-08T10:00:00+00:00"
    )
