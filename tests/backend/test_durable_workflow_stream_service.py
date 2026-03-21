"""Tests for durable_workflow_stream_service."""

from __future__ import annotations

from datetime import datetime, timezone

from src.backend.services.durable_workflow_stream_service import (
    DurableWorkflowStreamService,
    _event_from_instance,
)
from src.backend.workflows.durable.models import (
    WorkflowInstance,
    WorkflowInstanceStatus,
)


def _build_instance() -> WorkflowInstance:
    instance = WorkflowInstance.create(
        "#V#jira_task_full_reconciliation_workflow",
        user_id="#V#michael_witbrock",
        org_id="#V#university_of_auckland_strong_ai_lab",
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        max_retries=5,
    )
    instance.status = WorkflowInstanceStatus.RUNNING
    instance.current_state = "#V#jira_task_full_reconciliation_refresh_recent_updates_step"
    instance.step_index = 1
    instance.started_at = datetime(2026, 3, 22, 9, 10, 0, tzinfo=timezone.utc)
    instance.progress_current = 1
    instance.progress_total = 5
    instance.progress_message = "refreshing"
    instance.progress_updated_at = datetime(2026, 3, 22, 9, 12, 0, tzinfo=timezone.utc)
    instance.retry_count = 1
    instance.outputs = {"summary": "ready"}
    instance.execution_trace_id = "trace-123"
    return instance


def test_event_payload_includes_full_status_dict_fields() -> None:
    instance = _build_instance()

    payload = _event_from_instance(instance).to_payload()

    assert payload["instance_id"] == instance.instance_id
    assert payload["workflow_id"] == instance.workflow_id
    assert payload["status"] == "running"
    assert payload["current_state"] == instance.current_state
    assert payload["created_at"] == instance.created_at.isoformat()
    assert payload["started_at"] == "2026-03-22T09:10:00+00:00"
    assert payload["progress"]["updated_at"] == "2026-03-22T09:12:00+00:00"
    assert payload["retry_count"] == 1
    assert payload["max_retries"] == 5
    assert payload["has_outputs"] is True
    assert payload["execution_trace_id"] == "trace-123"
    assert payload["namespace"] == instance.namespace
    assert isinstance(payload["updated_at"], str) and payload["updated_at"]


def test_generate_events_emits_workflow_status_sse_payload() -> None:
    service = DurableWorkflowStreamService()
    try:
        instance = _build_instance()
        subscriber = service.subscribe(namespace=instance.namespace)

        notified = service.broadcast(_event_from_instance(instance))

        assert notified == 1
        event_str = next(service.generate_events(subscriber, timeout=0.1))
        assert event_str.startswith("event: workflow_status\n")
        assert '"instance_id":' in event_str
        assert '"created_at":' in event_str
        assert '"max_retries": 5' in event_str
    finally:
        service.shutdown()
