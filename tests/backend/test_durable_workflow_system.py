"""Tests for durable workflow system (JVNAUTOSCI-1075).

Validates the WorkflowInstance, WorkflowSchedule models, WorkflowInstanceManager,
DurableWorkflowExecutor, and scheduling components.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.backend.workflows.durable.models import (
    WorkflowInstance,
    WorkflowInstanceStatus,
    WorkflowSchedule,
    ScheduleType,
)
from src.backend.workflows.durable.instance_manager import WorkflowInstanceManager
from src.backend.workflows.durable.scheduler import (
    _parse_cron_expression,
    _calculate_next_cron_run,
    WorkflowScheduler,
)
from src.backend.workflows.durable.vontology_schedule_repository import (
    CTX_ENABLED_BOOL,
    CTX_NEXT_RUN_EPOCH_MS,
    PRED_ENABLED,
    PRED_NEXT_RUN,
)


# Module-level setup for mock DB
@pytest.fixture(autouse=True)
def reset_mock_db(monkeypatch):
    """Reset mock database before each test by clearing workflow collections."""
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    from src.backend.db.mongo_client import get_db
    from src.backend.workflows.durable import instance_manager
    from src.backend.services import concept_service

    db = get_db()
    if db is not None:
        # Drop collections to ensure test isolation
        try:
            db.drop_collection("workflow_instances")
            db.drop_collection("workflow_schedules")
            db.drop_collection("concepts")
            db.drop_collection("text_relations")
            db.drop_collection("text_values")
        except Exception:
            pass  # Ignore errors on first run when collections don't exist

    # Reset the indexes_ensured flag so indexes are recreated
    instance_manager._indexes_ensured = False

    # Seed Ontology for Schedules
    # We need to establish the hierarchy so list_concepts(include_descendants=True) works.
    try:
        # Root Type
        concept_service.create_concept(
            concept_id="#V#workflow_schedule",
            name="Workflow Schedule",
            instance_of_type="#V#type",
        )

        for subtype in [
            "#V#cron_schedule",
            "#V#interval_schedule",
            "#V#one_time_schedule",
        ]:
            try:
                # Proper hierarchy: subtype IS A TYPE OF workflow_schedule
                concept_service.create_concept(
                    concept_id=subtype,
                    name=subtype,
                    instance_of_type="#V#type",
                    parent_concept_ids=["#V#workflow_schedule"],
                )
            except Exception:
                pass
    except Exception:
        pass  # Best effort

    yield


class TestWorkflowInstanceStatus:
    """Unit tests for WorkflowInstanceStatus enum."""

    def test_is_terminal_returns_true_for_completed(self) -> None:
        """COMPLETED should be a terminal status."""
        assert WorkflowInstanceStatus.COMPLETED.is_terminal() is True

    def test_is_terminal_returns_true_for_failed(self) -> None:
        """FAILED should be a terminal status."""
        assert WorkflowInstanceStatus.FAILED.is_terminal() is True

    def test_is_terminal_returns_true_for_cancelled(self) -> None:
        """CANCELLED should be a terminal status."""
        assert WorkflowInstanceStatus.CANCELLED.is_terminal() is True

    def test_is_terminal_returns_false_for_pending(self) -> None:
        """PENDING should not be a terminal status."""
        assert WorkflowInstanceStatus.PENDING.is_terminal() is False

    def test_is_terminal_returns_false_for_running(self) -> None:
        """RUNNING should not be a terminal status."""
        assert WorkflowInstanceStatus.RUNNING.is_terminal() is False

    def test_is_resumable_returns_true_for_pending(self) -> None:
        """PENDING should be resumable."""
        assert WorkflowInstanceStatus.PENDING.is_resumable() is True

    def test_is_resumable_returns_true_for_paused(self) -> None:
        """PAUSED should be resumable."""
        assert WorkflowInstanceStatus.PAUSED.is_resumable() is True

    def test_is_resumable_returns_false_for_running(self) -> None:
        """RUNNING should not be resumable (already running)."""
        assert WorkflowInstanceStatus.RUNNING.is_resumable() is False


class TestWorkflowInstance:
    """Unit tests for WorkflowInstance model."""

    def test_create_generates_uuid(self) -> None:
        """create() should generate a unique instance_id."""
        instance = WorkflowInstance.create(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        assert instance.instance_id is not None
        assert len(instance.instance_id) == 36  # UUID format
        assert instance.workflow_id == "#V#test_workflow"
        assert instance.status == WorkflowInstanceStatus.PENDING

    def test_create_with_inputs(self) -> None:
        """create() should accept initial inputs."""
        inputs = {"param1": "value1", "param2": 42}
        instance = WorkflowInstance.create(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            inputs=inputs,
        )

        assert instance.inputs == inputs

    def test_to_doc_creates_valid_document(self) -> None:
        """to_doc() should create a MongoDB-compatible document."""
        instance = WorkflowInstance.create(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        doc = instance.to_doc()

        assert doc["instance_id"] == instance.instance_id
        assert doc["workflow_id"] == "#V#test_workflow"
        assert doc["status"] == "pending"
        assert doc["user_id"] == "user-1"
        assert isinstance(doc["created_at"], datetime)

    def test_from_doc_restores_instance(self) -> None:
        """from_doc() should restore an instance from a document."""
        now = datetime.now(timezone.utc)
        doc = {
            "instance_id": "test-id-123",
            "workflow_id": "#V#test_workflow",
            "user_id": "user-1",
            "org_id": "org-1",
            "namespace": "user-1/org-1",
            "status": "running",
            "created_at": now,
            "started_at": now,
            "current_state": "state_2",
            "workflow_data": {"key": "value"},
            "step_index": 5,
            "inputs": {"input1": "val1"},
        }

        instance = WorkflowInstance.from_doc(doc)

        assert instance.instance_id == "test-id-123"
        assert instance.status == WorkflowInstanceStatus.RUNNING
        assert instance.current_state == "state_2"
        assert instance.workflow_data == {"key": "value"}
        assert instance.step_index == 5

    def test_to_status_dict_creates_api_response(self) -> None:
        """to_status_dict() should create an API-friendly dict."""
        instance = WorkflowInstance.create(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        status_dict = instance.to_status_dict()

        assert status_dict["instance_id"] == instance.instance_id
        assert status_dict["status"] == "pending"
        assert "created_at" in status_dict
        assert status_dict["has_outputs"] is False

    def test_event_fields_roundtrip(self) -> None:
        """Event linkage fields should survive model serialisation."""
        instance = WorkflowInstance.create(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            source_event_type="task.created",
            source_event_id="#V#task_abc",
            event_idempotency_key="evt:task.created:abc123",
        )

        doc = instance.to_doc()
        assert doc["source_event_type"] == "task.created"
        assert doc["source_event_id"] == "#V#task_abc"
        assert doc["event_idempotency_key"] == "evt:task.created:abc123"

        restored = WorkflowInstance.from_doc(doc)
        assert restored.source_event_type == "task.created"
        assert restored.source_event_id == "#V#task_abc"
        assert restored.event_idempotency_key == "evt:task.created:abc123"

    def test_to_doc_omits_empty_event_fields(self) -> None:
        """Empty event linkage fields should not be persisted as null placeholders."""
        instance = WorkflowInstance.create(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        doc = instance.to_doc()
        assert "source_event_type" not in doc
        assert "source_event_id" not in doc
        assert "event_idempotency_key" not in doc


class TestWorkflowSchedule:
    """Unit tests for WorkflowSchedule model."""

    def test_create_once_schedule(self) -> None:
        """create_once() should create a one-time schedule."""
        run_at = datetime.now(timezone.utc) + timedelta(hours=1)
        schedule = WorkflowSchedule.create_once(
            "#V#test_workflow",
            run_at=run_at,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        assert schedule.schedule_type == ScheduleType.ONCE
        assert schedule.run_at == run_at
        assert schedule.next_run_at == run_at
        assert schedule.interval_seconds is None
        assert schedule.cron_expression is None

    def test_create_interval_schedule(self) -> None:
        """create_interval() should create an interval-based schedule."""
        schedule = WorkflowSchedule.create_interval(
            "#V#test_workflow",
            interval_seconds=3600,  # 1 hour
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        assert schedule.schedule_type == ScheduleType.INTERVAL
        assert schedule.interval_seconds == 3600
        assert schedule.next_run_at is not None

    def test_create_cron_schedule(self) -> None:
        """create_cron() should create a cron-based schedule."""
        schedule = WorkflowSchedule.create_cron(
            "#V#test_workflow",
            cron_expression="0 9 * * 1-5",  # 9 AM on weekdays
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        assert schedule.schedule_type == ScheduleType.CRON
        assert schedule.cron_expression == "0 9 * * 1-5"

    def test_to_doc_and_from_doc_roundtrip(self) -> None:
        """to_doc() and from_doc() should be symmetric."""
        original = WorkflowSchedule.create_interval(
            "#V#test_workflow",
            interval_seconds=300,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            description="Test schedule",
        )

        doc = original.to_doc()
        restored = WorkflowSchedule.from_doc(doc)

        assert restored.schedule_id == original.schedule_id
        assert restored.schedule_type == original.schedule_type
        assert restored.interval_seconds == original.interval_seconds
        assert restored.description == original.description


class TestCronParsing:
    """Unit tests for cron expression parsing."""

    def test_parse_every_minute(self) -> None:
        """'* * * * *' should match every minute."""
        fields = _parse_cron_expression("* * * * *")

        assert fields is not None
        assert fields["minute"] is None  # Any value
        assert fields["hour"] is None
        assert fields["day"] is None
        assert fields["month"] is None
        assert fields["weekday"] is None

    def test_parse_specific_time(self) -> None:
        """'30 9 * * *' should match 9:30 AM every day."""
        fields = _parse_cron_expression("30 9 * * *")

        assert fields is not None
        assert fields["minute"] == {30}
        assert fields["hour"] == {9}

    def test_parse_step_expression(self) -> None:
        """'*/15 * * * *' should match every 15 minutes."""
        fields = _parse_cron_expression("*/15 * * * *")

        assert fields is not None
        assert fields["minute"] == {0, 15, 30, 45}

    def test_parse_range_expression(self) -> None:
        """'0 9-17 * * *' should match hours 9-17."""
        fields = _parse_cron_expression("0 9-17 * * *")

        assert fields is not None
        assert fields["hour"] == set(range(9, 18))

    def test_parse_list_expression(self) -> None:
        """'0 9,12,18 * * *' should match specific hours."""
        fields = _parse_cron_expression("0 9,12,18 * * *")

        assert fields is not None
        assert fields["hour"] == {9, 12, 18}

    def test_parse_weekday_expression(self) -> None:
        """'0 9 * * 1-5' should match Monday-Friday."""
        fields = _parse_cron_expression("0 9 * * 1-5")

        assert fields is not None
        assert fields["weekday"] == {1, 2, 3, 4, 5}

    def test_parse_invalid_expression_returns_none(self) -> None:
        """Invalid expressions should return None."""
        assert _parse_cron_expression("invalid") is None
        assert _parse_cron_expression("* * *") is None  # Too few fields
        assert _parse_cron_expression("") is None

    def test_calculate_next_cron_run(self) -> None:
        """_calculate_next_cron_run should find next matching time."""
        # Every minute
        fields = _parse_cron_expression("* * * * *")
        assert fields is not None

        now = datetime(2026, 2, 3, 10, 30, 45, tzinfo=timezone.utc)
        next_run = _calculate_next_cron_run(fields, now)

        # Should be next minute (10:31)
        assert next_run.minute == 31
        assert next_run.hour == 10

    def test_calculate_next_cron_run_specific_hour(self) -> None:
        """Should find next occurrence at specific hour."""
        # 9 AM every day
        fields = _parse_cron_expression("0 9 * * *")
        assert fields is not None

        now = datetime(2026, 2, 3, 10, 0, 0, tzinfo=timezone.utc)  # 10 AM
        next_run = _calculate_next_cron_run(fields, now)

        # Should be tomorrow at 9 AM
        assert next_run.day == 4
        assert next_run.hour == 9
        assert next_run.minute == 0


class TestWorkflowInstanceManager:
    """Unit tests for WorkflowInstanceManager."""

    def test_create_instance_returns_id(self) -> None:
        """create_instance() should return the generated instance_id."""
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        assert instance_id is not None
        assert len(instance_id) == 36  # UUID format

    def test_get_instance_returns_created(self) -> None:
        """get_instance() should retrieve a created instance."""
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            inputs={"key": "value"},
        )

        instance = manager.get_instance(instance_id)

        assert instance is not None
        assert instance.instance_id == instance_id
        assert instance.workflow_id == "#V#test_workflow"
        assert instance.inputs == {"key": "value"}

    def test_get_instance_returns_none_for_missing(self) -> None:
        """get_instance() should return None for non-existent ID."""
        manager = WorkflowInstanceManager()

        instance = manager.get_instance("non-existent-id")

        assert instance is None

    def test_checkpoint_persists_state(self) -> None:
        """checkpoint() should persist workflow state."""
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        success = manager.checkpoint(
            instance_id,
            current_state="state_2",
            workflow_data={"accumulated": "data"},
            step_index=3,
        )

        assert success is True

        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.current_state == "state_2"
        assert instance.workflow_data == {"accumulated": "data"}
        assert instance.step_index == 3

    def test_mark_completed_updates_status(self) -> None:
        """mark_completed() should set status to COMPLETED."""
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        success = manager.mark_completed(
            instance_id,
            outputs={"result": "done"},
            final_state="end_state",
        )

        assert success is True

        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == WorkflowInstanceStatus.COMPLETED
        assert instance.outputs == {"result": "done"}
        assert instance.completed_at is not None

    def test_mark_failed_updates_status(self) -> None:
        """mark_failed() should set status to FAILED with error."""
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        success = manager.mark_failed(
            instance_id,
            error="Something went wrong",
            error_step="action_3",
        )

        assert success is True

        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == WorkflowInstanceStatus.FAILED
        assert instance.error == "Something went wrong"
        assert instance.error_step == "action_3"
        assert instance.retry_count == 1

    def test_mark_cancelled_updates_status(self) -> None:
        """mark_cancelled() should set status to CANCELLED."""
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        success = manager.mark_cancelled(instance_id)

        assert success is True

        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == WorkflowInstanceStatus.CANCELLED

    def test_mark_cancelled_fails_for_completed(self) -> None:
        """mark_cancelled() should fail for already completed instances."""
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        manager.mark_completed(instance_id, outputs={})
        success = manager.mark_cancelled(instance_id)

        assert success is False  # Already completed, can't cancel

    def test_list_instances_by_status(self) -> None:
        """list_instances() should filter by status."""
        manager = WorkflowInstanceManager()

        # Create instances with different statuses
        id1 = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        id2 = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        manager.mark_completed(id1, outputs={})
        # id2 stays pending

        completed = manager.list_instances(
            user_id="user-1",
            status=WorkflowInstanceStatus.COMPLETED,
        )
        pending = manager.list_instances(
            user_id="user-1",
            status=WorkflowInstanceStatus.PENDING,
        )

        completed_ids = [i.instance_id for i in completed]
        pending_ids = [i.instance_id for i in pending]

        assert id1 in completed_ids
        assert id2 in pending_ids

    def test_create_instance_for_event_reuses_existing(self) -> None:
        """create_instance_for_event() should deduplicate by idempotency key."""
        manager = WorkflowInstanceManager()

        instance_id_1, created_new_1 = manager.create_instance_for_event(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            event_idempotency_key="evt:test:event-1",
            source_event_type="task.created",
            source_event_id="event-1",
            inputs={"payload": "first"},
        )
        instance_id_2, created_new_2 = manager.create_instance_for_event(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            event_idempotency_key="evt:test:event-1",
            source_event_type="task.created",
            source_event_id="event-1",
            inputs={"payload": "second"},
        )

        assert created_new_1 is True
        assert created_new_2 is False
        assert instance_id_1 == instance_id_2

        stored = manager.get_instance(instance_id_1)
        assert stored is not None
        assert stored.source_event_type == "task.created"
        assert stored.source_event_id == "event-1"
        assert stored.event_idempotency_key == "evt:test:event-1"

    def test_list_instances_by_source_event_filters(self) -> None:
        """list_instances() should support source event filters."""
        manager = WorkflowInstanceManager()

        id1, _ = manager.create_instance_for_event(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            event_idempotency_key="evt:test:task-created-1",
            source_event_type="task.created",
            source_event_id="task-created-1",
        )
        id2, _ = manager.create_instance_for_event(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            event_idempotency_key="evt:test:message-created-1",
            source_event_type="message.direct_created",
            source_event_id="message-created-1",
        )

        task_instances = manager.list_instances(source_event_type="task.created")
        message_instances = manager.list_instances(
            source_event_type="message.direct_created",
        )
        event_id_instances = manager.list_instances(source_event_id="task-created-1")

        task_ids = {instance.instance_id for instance in task_instances}
        message_ids = {instance.instance_id for instance in message_instances}
        event_id_filtered_ids = {instance.instance_id for instance in event_id_instances}

        assert id1 in task_ids
        assert id1 in event_id_filtered_ids
        assert id2 in message_ids
        assert id2 not in task_ids

    def test_find_and_claim_instance(self) -> None:
        """find_and_claim_instance() should atomically claim a pending instance."""
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        claimed = manager.find_and_claim_instance("worker-1")

        assert claimed is not None
        assert claimed.instance_id == instance_id
        assert claimed.status == WorkflowInstanceStatus.RUNNING
        assert claimed.locked_by == "worker-1"
        assert claimed.lock_expires_at is not None

    def test_find_and_claim_returns_none_when_empty(self) -> None:
        """find_and_claim_instance() should return None when no instances available."""
        manager = WorkflowInstanceManager()

        claimed = manager.find_and_claim_instance("worker-1")

        assert claimed is None

    def test_extend_lock(self) -> None:
        """extend_lock() should extend the lock expiration."""
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        manager.find_and_claim_instance("worker-1")

        instance_before = manager.get_instance(instance_id)
        assert instance_before is not None
        original_expiry = instance_before.lock_expires_at

        success = manager.extend_lock(instance_id, "worker-1", extend_seconds=600)

        assert success is True

        instance_after = manager.get_instance(instance_id)
        assert instance_after is not None
        assert instance_after.lock_expires_at is not None
        assert original_expiry is not None
        assert instance_after.lock_expires_at > original_expiry

    def test_release_lock(self) -> None:
        """release_lock() should clear the lock."""
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        manager.find_and_claim_instance("worker-1")

        success = manager.release_lock(instance_id, "worker-1")

        assert success is True

        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.locked_by is None
        assert instance.lock_expires_at is None

    def test_reset_for_retry(self) -> None:
        """reset_for_retry() should reset a failed instance for retry."""
        manager = WorkflowInstanceManager()

        # With max_retries=3, first failure sets retry_count=1
        # Since 1 < 3, reset should succeed
        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            max_retries=3,
        )

        manager.mark_failed(instance_id, error="First failure")

        success = manager.reset_for_retry(instance_id)

        assert success is True

        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == WorkflowInstanceStatus.PENDING
        assert instance.error is None
        assert instance.retry_count == 1  # Incremented from mark_failed

    def test_reset_for_retry_fails_when_max_retries_exceeded(self) -> None:
        """reset_for_retry() should fail when max retries exceeded."""
        manager = WorkflowInstanceManager()

        # With max_retries=1, after the first failure retry_count becomes 1
        # The condition is retry_count < max_retries, so 1 < 1 = False
        # Therefore reset_for_retry should fail immediately after first failure
        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            max_retries=1,
        )

        # First failure - retry_count becomes 1
        manager.mark_failed(instance_id, error="First failure")

        # With max_retries=1 and retry_count=1, retry_count < max_retries is False
        # So reset_for_retry should fail
        success = manager.reset_for_retry(instance_id)
        assert success is False  # 1 < 1 is False, can't retry


class TestScheduleManagement:
    """Unit tests for schedule management in WorkflowInstanceManager."""

    def test_create_and_get_schedule(self) -> None:
        """create_schedule() and get_schedule() should work together."""
        manager = WorkflowInstanceManager()

        schedule = WorkflowSchedule.create_interval(
            "#V#test_workflow",
            interval_seconds=3600,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        schedule_id = manager.create_schedule(schedule)

        retrieved = manager.get_schedule(schedule_id)

        assert retrieved is not None
        assert retrieved.schedule_id == schedule_id
        assert retrieved.interval_seconds == 3600

    @patch(
        "src.backend.workflows.durable.vontology_schedule_repository.concept_service.list_concepts"
    )
    def test_list_schedules(self, mock_list_concepts) -> None:
        """list_schedules() should return schedules."""

        # Mock simple list behavior because mongomock graph lookup is flaky
        def side_effect(*args, **kwargs):
            from src.backend.db.repositories.concepts_repository import (
                ConceptsRepository,
            )

            all_docs = list(ConceptsRepository.find({}))
            return all_docs, len(all_docs)

        mock_list_concepts.side_effect = side_effect

        manager = WorkflowInstanceManager()

        schedule = WorkflowSchedule.create_interval(
            "#V#test_workflow",
            interval_seconds=3600,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        # Note: Repository converts UUID schedule_id to #V#schedule_<uuid> format
        # We need to trust list_schedules returns valid items, or better yet, verify via properties
        # But create_schedule returns the new ID. Let's capture it.
        created_id = manager.create_schedule(schedule)

        schedules = manager.list_schedules(user_id="user-1")

        assert len(schedules) >= 1
        # Check against the created_id, not original schedule.schedule_id
        assert any(s.schedule_id == created_id for s in schedules)

    def test_find_due_schedules(self) -> None:
        """find_due_schedules() should return schedules due to run."""

        manager = WorkflowInstanceManager()

        # Create a schedule that's already due
        past_time = datetime.now(timezone.utc) - timedelta(hours=1)
        schedule = WorkflowSchedule.create_once(
            "#V#test_workflow",
            run_at=past_time,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        schedule.next_run_at = past_time  # Ensure it's due

        created_id = manager.create_schedule(schedule)

        due = manager.find_due_schedules()

        assert len(due) >= 1
        assert any(s.schedule_id == created_id for s in due)

    @patch(
        "src.backend.workflows.durable.vontology_schedule_repository.concept_service.list_concepts"
    )
    def test_find_due_schedules_avoids_concept_list_scan(
        self, mock_list_concepts: MagicMock
    ) -> None:
        """Due lookup should not require broad list_concepts scans on the hot path."""
        mock_list_concepts.side_effect = AssertionError(
            "find_due_schedules should not call list_concepts"
        )

        manager = WorkflowInstanceManager()
        past_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        schedule = WorkflowSchedule.create_once(
            "#V#test_workflow",
            run_at=past_time,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        schedule.next_run_at = past_time
        created_id = manager.create_schedule(schedule)

        due = manager.find_due_schedules(limit=10)

        assert any(s.schedule_id == created_id for s in due)
        mock_list_concepts.assert_not_called()

    def test_find_due_schedules_legacy_relation_fallback(self) -> None:
        """Due lookup should still work for relations without WS5 context metadata."""
        from src.backend.db.repositories.text_value_repository import (
            TextRelationsRepository,
        )

        manager = WorkflowInstanceManager()
        past_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        schedule = WorkflowSchedule.create_once(
            "#V#test_workflow",
            run_at=past_time,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        schedule.next_run_at = past_time
        created_id = manager.create_schedule(schedule)

        # Simulate pre-WS5 records that don't include relation context metadata.
        TextRelationsRepository.update_many(
            {
                "subject_concept_id": created_id,
                "predicate": {"$in": [PRED_ENABLED, PRED_NEXT_RUN]},
            },
            {"$unset": {"context": ""}},
        )

        due = manager.find_due_schedules(limit=10)
        assert any(s.schedule_id == created_id for s in due)

    def test_find_due_schedules_ignores_non_schedule_due_relations(self) -> None:
        """Fast-path lookup should not include non-schedule concepts."""
        from src.backend.services import concept_service, text_value_service

        manager = WorkflowInstanceManager()
        past_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        due_schedule = WorkflowSchedule.create_once(
            "#V#test_workflow",
            run_at=past_time,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        due_schedule.next_run_at = past_time
        created_id = manager.create_schedule(due_schedule)

        marker_id = "#V#non_schedule_due_marker"
        concept_service.create_concept(
            concept_id=marker_id,
            name="Non Schedule Due Marker",
        )
        text_value_service.upsert_singleton_text_relation(
            subject_concept_id=marker_id,
            predicate=PRED_ENABLED,
            text="true",
            policy="replace_others",
            lang="en",
            context={CTX_ENABLED_BOOL: True},
        )
        text_value_service.upsert_singleton_text_relation(
            subject_concept_id=marker_id,
            predicate=PRED_NEXT_RUN,
            text=past_time.isoformat(),
            policy="replace_others",
            lang="en",
            context={CTX_NEXT_RUN_EPOCH_MS: int(past_time.timestamp() * 1000)},
        )

        due = manager.find_due_schedules(limit=10)
        due_ids = {item.schedule_id for item in due}
        assert created_id in due_ids
        assert marker_id not in due_ids

    def test_find_due_schedules_with_load_like_fixture(self) -> None:
        """Due lookup should remain correct with many non-due schedules present."""
        manager = WorkflowInstanceManager()
        now = datetime.now(timezone.utc)

        # Seed a moderate backlog of future schedules to emulate production-like
        # queue shape without making the unit test slow/flaky.
        for i in range(120):
            future_schedule = WorkflowSchedule.create_once(
                "#V#test_workflow",
                run_at=now + timedelta(hours=2 + i),
                user_id="user-1",
                org_id="org-1",
                namespace="user-1/org-1",
            )
            future_schedule.next_run_at = now + timedelta(hours=2 + i)
            manager.create_schedule(future_schedule)

        due_ids: set[str] = set()
        for i in range(3):
            due_schedule = WorkflowSchedule.create_once(
                "#V#test_workflow",
                run_at=now - timedelta(minutes=10 + i),
                user_id="user-1",
                org_id="org-1",
                namespace="user-1/org-1",
            )
            due_schedule.next_run_at = now - timedelta(minutes=10 + i)
            due_ids.add(manager.create_schedule(due_schedule))

        due = manager.find_due_schedules(limit=10)
        returned_ids = {schedule.schedule_id for schedule in due}

        assert due_ids.issubset(returned_ids)

    def test_set_schedule_enabled(self) -> None:
        """set_schedule_enabled() should toggle the enabled flag."""
        manager = WorkflowInstanceManager()

        schedule = WorkflowSchedule.create_interval(
            "#V#test_workflow",
            interval_seconds=3600,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        schedule_id = manager.create_schedule(schedule)

        # Disable
        success = manager.set_schedule_enabled(schedule_id, enabled=False)
        assert success is True

        retrieved = manager.get_schedule(schedule_id)
        assert retrieved is not None
        assert retrieved.enabled is False

        # Re-enable
        success = manager.set_schedule_enabled(schedule_id, enabled=True)
        assert success is True

        retrieved = manager.get_schedule(schedule_id)
        assert retrieved is not None
        assert retrieved.enabled is True

    def test_delete_schedule(self) -> None:
        """delete_schedule() should remove the schedule."""
        manager = WorkflowInstanceManager()

        schedule = WorkflowSchedule.create_interval(
            "#V#test_workflow",
            interval_seconds=3600,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        schedule_id = manager.create_schedule(schedule)

        success = manager.delete_schedule(schedule_id)

        assert success is True
        assert manager.get_schedule(schedule_id) is None


class TestWorkflowScheduler:
    """Unit tests for scheduler polling telemetry."""

    def test_process_due_schedules_updates_poll_metrics(self) -> None:
        manager = MagicMock(spec=WorkflowInstanceManager)
        now = datetime.now(timezone.utc)
        due_schedule = WorkflowSchedule.create_once(
            "#V#generate_considerations_workflow",
            run_at=now,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        due_schedule.schedule_id = "#V#schedule_test_due_1"
        due_schedule.default_inputs = {"foo": "bar"}
        due_schedule.next_run_at = now

        manager.find_due_schedules.return_value = [due_schedule]
        manager.create_instance.return_value = "instance-1"
        manager.update_schedule_after_run.return_value = True

        scheduler = WorkflowScheduler(manager)
        scheduler._process_due_schedules()

        metrics = scheduler.get_poll_metrics()
        assert metrics["poll_count"] == 1
        assert metrics["due_schedules_seen_total"] == 1
        assert metrics["due_count"] == 1
        assert metrics["triggered_count"] == 1
        assert metrics["lookup_ms"] >= 0.0
        assert metrics["total_ms"] >= metrics["lookup_ms"]
        assert isinstance(metrics["polled_at"], str)

        manager.find_due_schedules.assert_called_once_with(limit=50)
        manager.create_instance.assert_called_once()
        manager.update_schedule_after_run.assert_called_once()

    def test_process_due_schedules_records_empty_poll(self) -> None:
        manager = MagicMock(spec=WorkflowInstanceManager)
        manager.find_due_schedules.return_value = []

        scheduler = WorkflowScheduler(manager)
        scheduler._process_due_schedules()

        metrics = scheduler.get_poll_metrics()
        assert metrics["poll_count"] == 1
        assert metrics["due_schedules_seen_total"] == 0
        assert metrics["due_count"] == 0
        assert metrics["triggered_count"] == 0
