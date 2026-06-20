"""Tests for durable workflow system (JVNAUTOSCI-1075).

Validates the WorkflowInstance, WorkflowSchedule models, WorkflowInstanceManager,
DurableWorkflowExecutor, and scheduling components.
"""

from __future__ import annotations

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
from src.backend.workflows.durable.startup import recover_orphaned_instances
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
            db.drop_collection("workflow_event_bindings")
            db.drop_collection("workflow_workers")
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
        instance.execution_trace_id = "trace-status-1"

        status_dict = instance.to_status_dict()

        assert status_dict["instance_id"] == instance.instance_id
        assert status_dict["status"] == "pending"
        assert "created_at" in status_dict
        assert status_dict["has_outputs"] is False
        assert status_dict["execution_trace_id"] == "trace-status-1"

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

    def test_get_latest_instance_for_workflow_returns_newest_created(self) -> None:
        """Latest-instance lookup should return newest created workflow instance."""
        manager = WorkflowInstanceManager()

        first_id = manager.create_instance(
            "#V#test_workflow_latest",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        second_id = manager.create_instance(
            "#V#test_workflow_latest",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        coll = manager._get_instances_collection()
        assert coll is not None
        coll.update_one(
            {"instance_id": first_id},
            {"$set": {"created_at": datetime(2026, 2, 1, 10, 0, tzinfo=timezone.utc)}},
        )
        coll.update_one(
            {"instance_id": second_id},
            {"$set": {"created_at": datetime(2026, 2, 1, 10, 1, tzinfo=timezone.utc)}},
        )

        latest = manager.get_latest_instance_for_workflow("#V#test_workflow_latest")
        assert latest is not None
        assert latest.instance_id == second_id
        assert latest.instance_id != first_id

    def test_get_event_instance_returns_matching_event_workflow_instance(self) -> None:
        """Event instance lookup should filter by workflow + source event tuple."""
        manager = WorkflowInstanceManager()

        manager.create_instance_for_event(
            "#V#event_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            event_idempotency_key="evt:task.created:abc",
            source_event_type="task.created",
            source_event_id="#V#task_1",
        )

        matching = manager.get_event_instance(
            workflow_id="#V#event_workflow",
            source_event_type="task.created",
            source_event_id="#V#task_1",
        )
        missing = manager.get_event_instance(
            workflow_id="#V#event_workflow",
            source_event_type="task.created",
            source_event_id="#V#task_2",
        )

        assert matching is not None
        assert matching.workflow_id == "#V#event_workflow"
        assert matching.source_event_type == "task.created"
        assert matching.source_event_id == "#V#task_1"
        assert missing is None

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
            execution_trace_id="trace-1550",
        )

        assert success is True

        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == WorkflowInstanceStatus.COMPLETED
        assert instance.outputs == {"result": "done"}
        assert instance.execution_trace_id == "trace-1550"
        assert instance.completed_at is not None

    def test_mark_completed_emits_episode_evaluation_terminal_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager = WorkflowInstanceManager()
        launches: list[dict[str, Any]] = []

        monkeypatch.setattr(
            "src.backend.services.workflow_event_integration_service.maybe_launch_episode_evaluation_for_workflow_terminal",
            lambda **kwargs: (
                launches.append(dict(kwargs)) or {"success": True, "triggered": True}
            ),
        )

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
        assert len(launches) == 1
        assert launches[0]["terminal_status"] == "completed"
        assert launches[0]["final_state"] == "end_state"
        assert launches[0]["instance"].instance_id == instance_id

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
            outputs={"schema_version": "workflow_failed_outputs.v1"},
            execution_trace_id="trace-failed-1",
        )

        assert success is True

        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == WorkflowInstanceStatus.FAILED
        assert instance.error == "Something went wrong"
        assert instance.error_step == "action_3"
        assert instance.outputs == {"schema_version": "workflow_failed_outputs.v1"}
        assert instance.execution_trace_id == "trace-failed-1"
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

    def test_list_instances_accepts_multiple_status_filters(self) -> None:
        """list_instances() should support multi-status queries."""
        manager = WorkflowInstanceManager()

        pending_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        running_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        paused_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        collection = manager._get_instances_collection()
        assert collection is not None
        collection.update_one(
            {"instance_id": running_id},
            {
                "$set": {
                    "status": WorkflowInstanceStatus.RUNNING.value,
                    "progress_message": "running",
                }
            },
        )
        collection.update_one(
            {"instance_id": paused_id},
            {
                "$set": {
                    "status": WorkflowInstanceStatus.PAUSED.value,
                    "progress_message": "paused",
                }
            },
        )

        active_instances = manager.list_instances(
            user_id="user-1",
            status=[
                WorkflowInstanceStatus.PENDING,
                WorkflowInstanceStatus.RUNNING,
            ],
        )

        active_ids = {instance.instance_id for instance in active_instances}
        assert pending_id in active_ids
        assert running_id in active_ids
        assert paused_id not in active_ids

    def test_list_instance_status_dicts_returns_summary_payload(self) -> None:
        """list_instance_status_dicts() should return projected monitor payloads."""
        manager = WorkflowInstanceManager()

        completed_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        active_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        manager.mark_completed(completed_id, outputs={"result": "done"})
        collection = manager._get_instances_collection()
        assert collection is not None
        collection.update_one(
            {"instance_id": active_id},
            {
                "$set": {
                    "status": WorkflowInstanceStatus.RUNNING.value,
                    "progress_message": "running",
                }
            },
        )

        summaries = manager.list_instance_status_dicts(
            user_id="user-1",
            status=[WorkflowInstanceStatus.COMPLETED],
        )

        assert [summary["instance_id"] for summary in summaries] == [completed_id]
        assert summaries[0]["status"] == WorkflowInstanceStatus.COMPLETED.value
        assert summaries[0]["has_outputs"] is True
        assert "workflow_data" not in summaries[0]

    def test_list_instance_status_dicts_keeps_running_rows_visible_during_pending_backlog(
        self,
    ) -> None:
        """Monitor snapshots should not hide running work behind newer pending rows."""
        manager = WorkflowInstanceManager()

        running_alpha = manager.create_instance(
            "#V#alpha_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        pending_alpha_old = manager.create_instance(
            "#V#alpha_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        pending_alpha_new = manager.create_instance(
            "#V#alpha_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        running_beta = manager.create_instance(
            "#V#beta_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        collection = manager._get_instances_collection()
        assert collection is not None
        now = datetime.now(timezone.utc)

        collection.update_one(
            {"instance_id": running_alpha},
            {
                "$set": {
                    "status": WorkflowInstanceStatus.RUNNING.value,
                    "created_at": now - timedelta(minutes=4),
                    "started_at": now - timedelta(minutes=4),
                    "progress_message": "running alpha",
                }
            },
        )
        collection.update_one(
            {"instance_id": pending_alpha_old},
            {
                "$set": {
                    "created_at": now - timedelta(minutes=2),
                    "progress_message": "queued alpha old",
                }
            },
        )
        collection.update_one(
            {"instance_id": pending_alpha_new},
            {
                "$set": {
                    "created_at": now - timedelta(minutes=1),
                    "progress_message": "queued alpha new",
                }
            },
        )
        collection.update_one(
            {"instance_id": running_beta},
            {
                "$set": {
                    "status": WorkflowInstanceStatus.RUNNING.value,
                    "created_at": now - timedelta(minutes=3),
                    "started_at": now - timedelta(minutes=3),
                    "progress_message": "running beta",
                }
            },
        )

        summaries = manager.list_instance_status_dicts(
            user_id="user-1",
            status=[
                WorkflowInstanceStatus.PENDING,
                WorkflowInstanceStatus.RUNNING,
                WorkflowInstanceStatus.PAUSED,
            ],
            limit=3,
        )

        assert len(summaries) == 3
        alpha_statuses = {
            summary["status"]
            for summary in summaries
            if summary["workflow_id"] == "#V#alpha_workflow"
        }
        assert WorkflowInstanceStatus.RUNNING.value in alpha_statuses
        assert summaries[0]["status"] == WorkflowInstanceStatus.RUNNING.value
        assert summaries[1]["status"] == WorkflowInstanceStatus.RUNNING.value

    def test_workflow_instance_indexes_include_namespace_status_created(self) -> None:
        """Workflow instance indexes should cover the monitor namespace/status query."""
        manager = WorkflowInstanceManager()
        manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        collection = manager._get_instances_collection()
        assert collection is not None

        index_names = {index["name"] for index in collection.list_indexes()}
        assert "namespace_status_created" in index_names

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
        event_id_filtered_ids = {
            instance.instance_id for instance in event_id_instances
        }

        assert id1 in task_ids
        assert id1 in event_id_filtered_ids
        assert id2 in message_ids
        assert id2 not in task_ids

    def test_upsert_event_binding_create_and_list(self) -> None:
        """Event binding upsert should create records and support listing."""
        manager = WorkflowInstanceManager()

        binding, created, updated = manager.upsert_event_binding(
            event_type="type.created",
            workflow_id="#V#salient_predicate_governance_workflow",
            input_mapping={"type_concept_id": "event.concept_id"},
            enabled=True,
            actor="test_user",
        )

        assert created is True
        assert updated is False
        assert binding.event_type == "type.created"
        assert binding.workflow_id == "#V#salient_predicate_governance_workflow"
        assert binding.revision == 1

        listed = manager.list_event_bindings(event_type="type.created")
        assert len(listed) == 1
        assert listed[0].binding_id == binding.binding_id

    def test_upsert_event_binding_condition_without_input_mapping(self) -> None:
        """Condition-only event bindings should persist represented policy."""
        manager = WorkflowInstanceManager()

        condition = {
            "kind": "context_value_equals",
            "key": "event.new_status",
            "value": "completed",
        }
        binding, created, updated = manager.upsert_event_binding(
            event_type="task.status_changed",
            workflow_id="#V#task_status_workflow",
            condition=condition,
            enabled=True,
            actor="test_user",
        )

        assert created is True
        assert updated is False
        assert binding.input_mapping == {}
        assert binding.condition == condition

        listed = manager.list_event_bindings(event_type="task.status_changed")
        assert len(listed) == 1
        assert listed[0].condition == condition

    def test_upsert_event_binding_invalidates_runnable_verification_cache(
        self, monkeypatch
    ) -> None:
        manager = WorkflowInstanceManager()
        invalidations: list[dict[str, Any]] = []

        monkeypatch.setattr(
            "src.backend.workflows.durable.workflow_instance_submission_service.invalidate_workflow_runnable_verification_cache",
            lambda **kwargs: invalidations.append(dict(kwargs)) or {"success": True},
        )

        manager.upsert_event_binding(
            event_type="type.created",
            workflow_id="#V#salient_predicate_governance_workflow",
            input_mapping={"type_concept_id": "event.concept_id"},
            enabled=True,
            actor="test_user",
        )

        assert invalidations
        assert (
            invalidations[-1].get("workflow_id")
            == "#V#salient_predicate_governance_workflow"
        )
        assert invalidations[-1].get("reason") == "event_binding_mutated"

    def test_upsert_event_binding_conflict_requires_replace(self) -> None:
        """Conflicting upsert should require replace_existing=True."""
        manager = WorkflowInstanceManager()

        original, created, updated = manager.upsert_event_binding(
            event_type="concept.updated",
            workflow_id="#V#enrichment_workflow",
            input_mapping={"concept_id": "event.concept_id"},
            enabled=True,
            actor="test_user",
        )
        assert created is True
        assert updated is False

        with pytest.raises(ValueError, match="binding_conflict"):
            manager.upsert_event_binding(
                event_type="concept.updated",
                workflow_id="#V#enrichment_workflow",
                input_mapping={"different_key": "event.concept_id"},
                enabled=True,
                actor="test_user",
                replace_existing=False,
            )

        replaced, created, updated = manager.upsert_event_binding(
            event_type="concept.updated",
            workflow_id="#V#enrichment_workflow",
            input_mapping={"different_key": "event.concept_id"},
            enabled=False,
            actor="test_user",
            replace_existing=True,
        )

        assert created is False
        assert updated is True
        assert replaced.binding_id == original.binding_id
        assert replaced.revision == original.revision + 1
        assert replaced.enabled is False

    def test_set_event_binding_enabled_updates_revision(self) -> None:
        manager = WorkflowInstanceManager()

        binding, created, updated = manager.upsert_event_binding(
            event_type="file_copy.uploaded",
            workflow_id="#V#file_copy_upload_handler_workflow",
            input_mapping={"concept_id": "event.file_copy_concept_id"},
            enabled=True,
            actor="test_user",
        )
        assert created is True
        assert updated is False

        disabled = manager.set_event_binding_enabled(
            binding.binding_id,
            enabled=False,
            actor="test_user",
        )

        assert disabled is not None
        assert disabled.binding_id == binding.binding_id
        assert disabled.enabled is False
        assert disabled.revision == binding.revision + 1

    def test_delete_event_binding_removes_record_and_invalidates_cache(
        self, monkeypatch
    ) -> None:
        manager = WorkflowInstanceManager()
        invalidations: list[dict[str, Any]] = []

        monkeypatch.setattr(
            "src.backend.workflows.durable.workflow_instance_submission_service.invalidate_workflow_runnable_verification_cache",
            lambda **kwargs: invalidations.append(dict(kwargs)) or {"success": True},
        )

        binding, created, updated = manager.upsert_event_binding(
            event_type="file_copy.uploaded",
            workflow_id="#V#file_copy_upload_handler_workflow",
            input_mapping={"concept_id": "event.file_copy_concept_id"},
            enabled=True,
            actor="test_user",
        )
        assert created is True
        assert updated is False

        deleted = manager.delete_event_binding(binding.binding_id)

        assert deleted is not None
        assert deleted.binding_id == binding.binding_id
        assert manager.get_event_binding(binding.binding_id) is None
        assert invalidations
        assert (
            invalidations[-1].get("workflow_id")
            == "#V#file_copy_upload_handler_workflow"
        )
        assert invalidations[-1].get("reason") == "event_binding_mutated"

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

    def test_find_and_claim_stamps_worker_build_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker claims should persist build provenance for diagnostics."""
        monkeypatch.delenv("VON_DURABLE_MIN_WORKER_BUILD", raising=False)
        monkeypatch.delenv("VON_DURABLE_MIN_WORKER_GIT_SHORT_COMMIT", raising=False)
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        claimed = manager.find_and_claim_instance(
            "worker-build-stamp",
            worker_build_identity={
                "version": "v20260620_0100_backend+gabcdef123456",
                "git_short_commit": "abcdef123456",
                "git_commit": "abcdef1234567890abcdef1234567890abcdef12",
                "hostname": "worker-host",
                "pid": 4242,
            },
        )

        assert claimed is not None
        assert claimed.instance_id == instance_id
        assert claimed.claimed_at is not None
        assert claimed.claimed_by_build is not None
        assert claimed.claimed_by_build["worker_id"] == "worker-build-stamp"
        assert claimed.claimed_by_build["git_short_commit"] == "abcdef123456"
        assert claimed.claimed_by_build["hostname"] == "worker-host"
        assert "abcdef1" in claimed.claimed_by_build["match_tokens"]
        assert "gabcdef1" in claimed.claimed_by_build["match_tokens"]
        status_dict = claimed.to_status_dict()
        assert status_dict["locked_by"] == "worker-build-stamp"
        assert status_dict["claimed_by_build"]["git_short_commit"] == "abcdef123456"

    def test_find_and_claim_skips_unmatched_min_worker_build(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Instances with a required build should fail closed for other workers."""
        monkeypatch.delenv("VON_DURABLE_MIN_WORKER_BUILD", raising=False)
        monkeypatch.delenv("VON_DURABLE_MIN_WORKER_GIT_SHORT_COMMIT", raising=False)
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        collection = manager._get_instances_collection()
        assert collection is not None
        collection.update_one(
            {"instance_id": instance_id},
            {"$set": {"min_worker_build": "abcdef1"}},
        )

        claimed = manager.find_and_claim_instance(
            "worker-old-build",
            worker_build_identity={
                "version": "vold_backend+g123456789abc",
                "git_short_commit": "123456789abc",
            },
        )

        assert claimed is None
        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == WorkflowInstanceStatus.PENDING
        assert instance.locked_by is None

    def test_find_and_claim_allows_matching_min_worker_build(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A required build token can be satisfied by a Git-prefix token."""
        monkeypatch.delenv("VON_DURABLE_MIN_WORKER_BUILD", raising=False)
        monkeypatch.delenv("VON_DURABLE_MIN_WORKER_GIT_SHORT_COMMIT", raising=False)
        manager = WorkflowInstanceManager()

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        collection = manager._get_instances_collection()
        assert collection is not None
        collection.update_one(
            {"instance_id": instance_id},
            {"$set": {"min_worker_build": "abcdef1"}},
        )

        claimed = manager.find_and_claim_instance(
            "worker-new-build",
            worker_build_identity={
                "version": "v20260620_0100_backend+gabcdef123456",
                "git_short_commit": "abcdef123456",
            },
        )

        assert claimed is not None
        assert claimed.instance_id == instance_id
        assert claimed.locked_by == "worker-new-build"

    def test_release_ineligible_worker_claims_flags_and_pauses_claim(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The stale-claim reaper should release claims from ineligible builds."""
        monkeypatch.delenv("VON_DURABLE_MIN_WORKER_BUILD", raising=False)
        monkeypatch.delenv("VON_DURABLE_MIN_WORKER_GIT_SHORT_COMMIT", raising=False)
        manager = WorkflowInstanceManager()
        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        collection = manager._get_instances_collection()
        assert collection is not None
        collection.update_one(
            {"instance_id": instance_id},
            {
                "$set": {
                    "status": WorkflowInstanceStatus.RUNNING.value,
                    "locked_by": "worker-old-build",
                    "lock_expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                    "claimed_by_build": {
                        "schema_version": "durable_worker_claim_provenance.v1",
                        "worker_id": "worker-old-build",
                        "git_short_commit": "123456789abc",
                        "match_tokens": ["1234567", "123456789abc"],
                    },
                    "min_worker_build": "abcdef1",
                }
            },
        )

        released = manager.release_ineligible_worker_claims()

        assert released == 1
        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == WorkflowInstanceStatus.PAUSED
        assert instance.locked_by is None
        assert (
            instance.claim_ineligible_reason == "worker_build_requirement_not_satisfied"
        )

    def test_worker_heartbeat_registry_records_build_identity(self) -> None:
        """Worker registry rows should expose worker id, build, and liveness."""
        manager = WorkflowInstanceManager()

        success = manager.upsert_worker_heartbeat(
            worker_id="worker-registry-test",
            worker_build_identity={
                "version": "v20260620_0100_backend+gabcdef123456",
                "git_short_commit": "abcdef123456",
                "hostname": "registry-host",
                "pid": 5150,
            },
            active_instance_ids=["instance-a"],
            state="running",
        )

        assert success is True
        workers = manager.list_worker_heartbeats()
        assert len(workers) == 1
        assert workers[0]["worker_id"] == "worker-registry-test"
        assert workers[0]["hostname"] == "registry-host"
        assert workers[0]["active_instance_ids"] == ["instance-a"]
        assert workers[0]["build"]["git_short_commit"] == "abcdef123456"

    def test_claim_and_terminal_updates_record_workflow_episodes(
        self, monkeypatch
    ) -> None:
        """Durable lifecycle should emit one start + one final episode record."""
        manager = WorkflowInstanceManager()
        started: list[dict[str, Any]] = []
        finalised: list[dict[str, Any]] = []

        monkeypatch.setattr(
            "src.backend.workflows.durable.instance_manager.start_workflow_use_episode",
            lambda **kwargs: started.append(kwargs) or {"episode_id": "wfep_started"},
        )
        monkeypatch.setattr(
            "src.backend.workflows.durable.instance_manager.finalise_workflow_use_episode",
            lambda **kwargs: finalised.append(kwargs) or {"episode_id": "wfep_final"},
        )

        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        claimed = manager.find_and_claim_instance("worker-episode-test")
        assert claimed is not None
        assert len(started) == 1
        assert started[0]["workflow_id"] == "#V#test_workflow"
        assert started[0]["source"] == "durable_instance"
        assert started[0]["instance_id"] == instance_id

        success = manager.mark_failed(
            instance_id,
            error="tool_timeout:Workflow step timed out",
            error_step="tool_execution",
        )
        assert success is True
        assert len(finalised) == 1
        assert finalised[0]["workflow_id"] == "#V#test_workflow"
        assert finalised[0]["completed"] is False
        assert finalised[0]["terminal_stage"] == "tool_execution"
        assert finalised[0]["termination_code"] == "tool_timeout"

    def test_find_and_claim_prioritises_conversation_turn_instances(self) -> None:
        """User-facing conversation turns should not sit behind maintenance backlog."""
        manager = WorkflowInstanceManager()

        stale_conversation_instance_id = manager.create_instance(
            "#V#conversation_turn_execution_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            source_event_type="conversation_turn",
        )
        collection = manager._get_instances_collection()
        assert collection is not None
        collection.update_one(
            {"instance_id": stale_conversation_instance_id},
            {"$set": {"created_at": datetime.now(timezone.utc) - timedelta(days=2)}},
        )
        background_instance_id = manager.create_instance(
            "#V#episode_evaluation_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            source_event_type="episode.created",
        )
        conversation_instance_id = manager.create_instance(
            "#V#conversation_turn_execution_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
            source_event_type="conversation_turn",
        )

        claimed = manager.find_and_claim_instance("worker-priority-test")

        assert claimed is not None
        assert claimed.instance_id == conversation_instance_id
        assert claimed.workflow_id == "#V#conversation_turn_execution_workflow"

        second_claim = manager.find_and_claim_instance("worker-priority-test-2")

        assert second_claim is not None
        assert second_claim.instance_id == background_instance_id
        assert second_claim.workflow_id == "#V#episode_evaluation_workflow"

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

    def test_recover_orphaned_instances_recovers_old_expired_running_rows(self) -> None:
        """Startup recovery should pause expired running rows regardless of age."""
        manager = WorkflowInstanceManager()
        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        claimed = manager.find_and_claim_instance("worker-1")
        assert claimed is not None

        collection = manager._get_instances_collection()
        assert collection is not None
        old_started_at = datetime.now(timezone.utc) - timedelta(days=14)
        expired_lock_at = datetime.now(timezone.utc) - timedelta(hours=2)
        collection.update_one(
            {"instance_id": instance_id},
            {
                "$set": {
                    "started_at": old_started_at,
                    "lock_expires_at": expired_lock_at,
                }
            },
        )

        recovered = recover_orphaned_instances()

        assert recovered == 1
        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == WorkflowInstanceStatus.PAUSED
        assert instance.locked_by is None
        assert instance.lock_expires_at is None

    def test_recover_orphaned_instances_honours_optional_stale_cutoff(self) -> None:
        """Explicit stale-age limits should still support bounded recovery sweeps."""
        manager = WorkflowInstanceManager()
        instance_id = manager.create_instance(
            "#V#test_workflow",
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        claimed = manager.find_and_claim_instance("worker-1")
        assert claimed is not None

        collection = manager._get_instances_collection()
        assert collection is not None
        old_started_at = datetime.now(timezone.utc) - timedelta(days=14)
        expired_lock_at = datetime.now(timezone.utc) - timedelta(hours=2)
        collection.update_one(
            {"instance_id": instance_id},
            {
                "$set": {
                    "started_at": old_started_at,
                    "lock_expires_at": expired_lock_at,
                }
            },
        )

        recovered = recover_orphaned_instances(max_stale_hours=24)

        assert recovered == 0
        instance = manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == WorkflowInstanceStatus.RUNNING


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

    @patch(
        "src.backend.workflows.durable.vontology_schedule_repository.concept_service.list_concepts"
    )
    def test_list_schedules_filters_by_workflow_id(self, mock_list_concepts) -> None:
        """list_schedules(workflow_id=...) should avoid unrelated schedule lookups."""

        def side_effect(*args, **kwargs):
            from src.backend.db.repositories.concepts_repository import (
                ConceptsRepository,
            )

            all_docs = list(ConceptsRepository.find({}))
            return all_docs, len(all_docs)

        mock_list_concepts.side_effect = side_effect

        manager = WorkflowInstanceManager()
        first = WorkflowSchedule.create_interval(
            "#V#alpha_workflow",
            interval_seconds=3600,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        second = WorkflowSchedule.create_interval(
            "#V#beta_workflow",
            interval_seconds=1800,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )

        manager.create_schedule(first)
        manager.create_schedule(second)

        schedules = manager.list_schedules(workflow_id="#V#beta_workflow")

        assert len(schedules) == 1
        assert schedules[0].workflow_id == "#V#beta_workflow"

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

    def test_process_due_schedules_updates_poll_metrics(self, monkeypatch) -> None:
        from src.backend.workflows.durable.workflow_instance_submission_service import (
            WorkflowInstanceSubmissionResult,
        )

        manager = MagicMock(spec=WorkflowInstanceManager)
        now = datetime.now(timezone.utc)
        due_schedule = WorkflowSchedule.create_once(
            "#V#enrichment_workflow",
            run_at=now,
            user_id="user-1",
            org_id="org-1",
            namespace="user-1/org-1",
        )
        due_schedule.schedule_id = "#V#schedule_test_due_1"
        due_schedule.default_inputs = {"predicate": "hasDescription"}
        due_schedule.next_run_at = now

        manager.find_due_schedules.return_value = [due_schedule]
        manager.update_schedule_after_run.return_value = True
        monkeypatch.setattr(
            "src.backend.workflows.durable.scheduler.submit_verified_workflow_instance",
            lambda **_kwargs: WorkflowInstanceSubmissionResult(
                success=True,
                workflow_id="#V#enrichment_workflow",
                status="accepted",
                instance_id="instance-1",
                verification={},
            ),
        )

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
