"""Tests for background task service (JVNAUTOSCI-1038).

Validates the BackgroundTaskRegistry for task submission, status tracking,
and result retrieval.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from src.backend.integrations.internal_mcp.orchestrator import CancellationRequested
from src.backend.services.background_task_service import (
    BackgroundTaskCapacityReached,
    BackgroundTaskRegistry,
    BackgroundTaskScopeMismatch,
    TaskStatus,
)


class TestTaskStatus:
    """Unit tests for TaskStatus dataclass."""

    def test_to_dict_includes_all_fields(self) -> None:
        """to_dict() should include all relevant fields."""
        now = datetime.now(timezone.utc)
        status = TaskStatus(
            task_id="test-123",
            status="running",
            created_at=now,
            started_at=now,
            progress={"phase": "tool_execute"},
            progress_history=[{"phase": "tool_execute"}],
            session_id="session-123",
            user_id="user-123",
            organisation_id="organisation-123",
            namespace="#V#user-123@organisation-123",
            queue_id="queue-123",
            client_request_id="request-123",
            attempt_id="attempt-123",
        )

        result = status.to_dict()

        assert result["task_id"] == "test-123"
        assert result["status"] == "running"
        assert result["created_at"] == now.isoformat()
        assert result["started_at"] == now.isoformat()
        assert result["completed_at"] is None
        assert result["has_result"] is False
        assert result["error"] is None
        assert result["progress"] == {"phase": "tool_execute"}
        assert result["progress_history"] == [{"phase": "tool_execute"}]
        assert result["cancellation_requested"] is False
        assert result["session_id"] == "session-123"
        assert result["user_id"] == "user-123"
        assert result["organisation_id"] == "organisation-123"
        assert result["namespace"] == "#V#user-123@organisation-123"
        assert result["queue_id"] == "queue-123"
        assert result["client_request_id"] == "request-123"
        assert result["attempt_id"] == "attempt-123"

    def test_to_dict_with_completed_task(self) -> None:
        """to_dict() should handle completed tasks with results."""
        now = datetime.now(timezone.utc)
        status = TaskStatus(
            task_id="test-456",
            status="completed",
            created_at=now,
            started_at=now,
            completed_at=now,
            result={"response_text": "Hello"},
        )

        result = status.to_dict()

        assert result["status"] == "completed"
        assert result["has_result"] is True
        assert result["completed_at"] == now.isoformat()


class TestBackgroundTaskRegistry:
    """Unit tests for BackgroundTaskRegistry."""

    def test_submit_task_returns_pending_status(self) -> None:
        """submit_task() should return a TaskStatus with status='pending'."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _dummy() -> str:
            return "done"

        status = registry.submit_task(task_id="task-1", callable=_dummy)

        assert status.task_id == "task-1"
        assert status.status == "pending"
        assert status.created_at is not None

        registry.shutdown(wait=True)

    def test_task_transitions_to_running_and_completed(self) -> None:
        """Task should transition pending -> running -> completed."""
        registry = BackgroundTaskRegistry(max_workers=1)
        execution_order: list[str] = []

        def _task() -> str:
            execution_order.append("started")
            time.sleep(0.05)
            execution_order.append("finished")
            return "result-value"

        registry.submit_task(task_id="task-2", callable=_task)

        # Wait for completion
        for _ in range(50):
            status = registry.get_task_status("task-2")
            if status and status.status == "completed":
                break
            time.sleep(0.05)

        status = registry.get_task_status("task-2")
        assert status is not None
        assert status.status == "completed"
        assert status.result == "result-value"
        assert status.started_at is not None
        assert status.completed_at is not None
        assert "started" in execution_order
        assert "finished" in execution_order

        registry.shutdown(wait=True)

    def test_task_failure_records_error(self) -> None:
        """Failed tasks should have status='failed' and error message."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _failing_task() -> None:
            raise ValueError("Something went wrong")

        registry.submit_task(task_id="task-fail", callable=_failing_task)

        # Wait for completion
        for _ in range(50):
            status = registry.get_task_status("task-fail")
            if status and status.status == "failed":
                break
            time.sleep(0.05)

        status = registry.get_task_status("task-fail")
        assert status is not None
        assert status.status == "failed"
        assert "Something went wrong" in (status.error or "")

        registry.shutdown(wait=True)

    def test_get_task_result_returns_value(self) -> None:
        """get_task_result() should return the result for completed tasks."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _task() -> dict[str, str]:
            return {"key": "value"}

        registry.submit_task(task_id="task-result", callable=_task)

        # Wait for completion
        for _ in range(50):
            status = registry.get_task_status("task-result")
            if status and status.status == "completed":
                break
            time.sleep(0.05)

        result = registry.get_task_result("task-result")
        assert result == {"key": "value"}

        registry.shutdown(wait=True)

    def test_get_task_result_raises_for_incomplete(self) -> None:
        """get_task_result() should raise ValueError for incomplete tasks."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _slow_task() -> str:
            time.sleep(10)
            return "done"

        registry.submit_task(task_id="task-slow", callable=_slow_task)

        # Don't wait - task should still be running
        time.sleep(0.01)

        with pytest.raises(ValueError, match="not completed"):
            registry.get_task_result("task-slow")

        registry.shutdown(wait=False)

    def test_get_task_result_raises_for_failed(self) -> None:
        """get_task_result() should raise RuntimeError for failed tasks."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _failing_task() -> None:
            raise ValueError("Task error")

        registry.submit_task(task_id="task-err", callable=_failing_task)

        # Wait for failure
        for _ in range(50):
            status = registry.get_task_status("task-err")
            if status and status.status == "failed":
                break
            time.sleep(0.05)

        with pytest.raises(RuntimeError, match="Task failed"):
            registry.get_task_result("task-err")

        registry.shutdown(wait=True)

    def test_request_cancellation_stays_nonterminal_until_worker_acknowledges(
        self,
    ) -> None:
        """Running work is cancelling, not cancelled, until it actually stops."""
        registry = BackgroundTaskRegistry(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def _slow_task() -> str:
            started.set()
            release.wait(timeout=2)
            return "done"

        registry.submit_task(task_id="task-cancel", callable=_slow_task)
        assert started.wait(timeout=2)

        # Request cancellation
        success = registry.request_cancellation("task-cancel")
        assert success is True

        # Check flag
        assert registry.is_cancellation_requested("task-cancel") is True

        status = registry.get_task_status("task-cancel")
        assert status is not None
        assert status.cancellation_requested is True
        assert status.status == "running"
        assert status.completed_at is None
        assert status.progress.get("status") == "cancelling"

        release.set()
        registry.shutdown(wait=True)

    def test_noncooperative_worker_reports_its_actual_late_completion(self) -> None:
        """A cancellation request is not a false terminal cancellation receipt."""
        registry = BackgroundTaskRegistry(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def _late_completing_task() -> str:
            started.set()
            release.wait(timeout=2)
            return "done"

        registry.submit_task(task_id="late-cancel", callable=_late_completing_task)
        assert started.wait(timeout=2)

        success = registry.request_cancellation("late-cancel")
        assert success is True
        release.set()

        for _ in range(50):
            status = registry.get_task_status("late-cancel")
            if status and status.completed_at is not None:
                break
            time.sleep(0.05)

        status = registry.get_task_status("late-cancel")
        assert status is not None
        assert status.status == "completed"
        assert status.result == "done"
        assert status.error is None
        assert status.cancellation_requested is True

        registry.shutdown(wait=True)

    def test_exception_after_cancellation_request_acknowledges_cancellation(
        self,
    ) -> None:
        """A cancel-before-bind race is cancelled rather than failed."""

        registry = BackgroundTaskRegistry(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def _admission_race() -> None:
            started.set()
            release.wait(timeout=2)
            raise RuntimeError("queue row was cancelled before turn admission")

        registry.submit_task(task_id="cancel-before-bind", callable=_admission_race)
        assert started.wait(timeout=2)
        assert registry.request_cancellation("cancel-before-bind") is True

        release.set()
        for _ in range(50):
            status = registry.get_task_status("cancel-before-bind")
            if status and status.completed_at is not None:
                break
            time.sleep(0.05)

        status = registry.get_task_status("cancel-before-bind")
        assert status is not None
        assert status.status == "cancelled"
        assert status.cancellation_requested is True
        assert status.completed_at is not None
        assert status.progress.get("status") == "cancelled"
        assert status.error == "queue row was cancelled before turn admission"

        registry.shutdown(wait=True)

    def test_pending_future_can_acknowledge_cancellation_immediately(self) -> None:
        registry = BackgroundTaskRegistry(max_workers=1, max_pending_tasks=2)
        blocker_started = threading.Event()
        release_blocker = threading.Event()

        def _blocker() -> str:
            blocker_started.set()
            release_blocker.wait(timeout=2)
            return "blocker done"

        registry.submit_task(task_id="blocker", callable=_blocker)
        assert blocker_started.wait(timeout=2)
        registry.submit_task(task_id="pending-cancel", callable=lambda: "must not run")

        assert registry.request_cancellation("pending-cancel") is True
        status = registry.get_task_status("pending-cancel")
        assert status is not None
        assert status.status == "cancelled"
        assert status.completed_at is not None
        assert status.progress.get("status") == "cancelled"

        release_blocker.set()
        registry.shutdown(wait=True)

    def test_request_cancellation_returns_false_for_completed(self) -> None:
        """request_cancellation() should return False for completed tasks."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _quick_task() -> str:
            return "done"

        registry.submit_task(task_id="task-done", callable=_quick_task)

        # Wait for completion
        for _ in range(50):
            status = registry.get_task_status("task-done")
            if status and status.status == "completed":
                break
            time.sleep(0.05)

        success = registry.request_cancellation("task-done")
        assert success is False

        registry.shutdown(wait=True)

    def test_list_tasks_returns_all(self) -> None:
        """list_tasks() should return all tasks."""
        registry = BackgroundTaskRegistry(max_workers=2)

        def _task1() -> str:
            return "done1"

        def _task2() -> str:
            time.sleep(0.1)
            return "done2"

        registry.submit_task(task_id="list-task-1", callable=_task1)
        registry.submit_task(task_id="list-task-2", callable=_task2)

        # Wait a bit for task1 to complete
        time.sleep(0.05)

        tasks = registry.list_tasks()
        task_ids = [t["task_id"] for t in tasks]

        assert "list-task-1" in task_ids
        assert "list-task-2" in task_ids

        registry.shutdown(wait=True)

    def test_list_tasks_with_status_filter(self) -> None:
        """list_tasks(status_filter=...) should filter by status."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _task() -> str:
            return "done"

        registry.submit_task(task_id="filter-task", callable=_task)

        # Wait for completion
        for _ in range(50):
            status = registry.get_task_status("filter-task")
            if status and status.status == "completed":
                break
            time.sleep(0.05)

        completed = registry.list_tasks(status_filter="completed")
        running = registry.list_tasks(status_filter="running")

        assert any(t["task_id"] == "filter-task" for t in completed)
        assert not any(t["task_id"] == "filter-task" for t in running)

        registry.shutdown(wait=True)

    def test_submit_duplicate_task_raises(self) -> None:
        """submit_task() should raise ValueError for duplicate task_id."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _slow_task() -> str:
            time.sleep(10)
            return "done"

        registry.submit_task(task_id="dup-task", callable=_slow_task)

        with pytest.raises(ValueError, match="already exists"):
            registry.submit_task(task_id="dup-task", callable=_slow_task)

        registry.shutdown(wait=False)

    def test_submit_cannot_overwrite_terminal_task_scope(self) -> None:
        """A replayed client request ID cannot replace a terminal task record."""
        registry = BackgroundTaskRegistry(max_workers=1)
        registry.mark_terminal_external(
            "terminal-id",
            status="completed",
            result={"answer": "first"},
            user_id="#V#first_user",
            organisation_id="#V#first_org",
            namespace="#V#first_user@first_org",
        )

        with pytest.raises(ValueError, match="already exists"):
            registry.submit_task(
                task_id="terminal-id",
                callable=lambda: "second",
                user_id="#V#second_user",
                organisation_id="#V#second_org",
                namespace="#V#second_user@second_org",
            )

        retained = registry.get_task_status("terminal-id")
        assert retained is not None
        assert retained.user_id == "#V#first_user"
        assert retained.result == {"answer": "first"}
        registry.shutdown(wait=True)

    def test_background_capacity_is_bounded_and_fair_across_users(self) -> None:
        registry = BackgroundTaskRegistry(
            max_workers=4,
            max_pending_tasks=2,
            max_active_tasks_per_user=2,
        )
        release = threading.Event()

        def _blocking_task() -> str:
            release.wait(timeout=2)
            return "done"

        registry.submit_task(
            task_id="user-a-org-1",
            callable=_blocking_task,
            user_id="#V#user_a",
            organisation_id="#V#org_1",
            namespace="#V#user_a@org_1",
        )
        registry.submit_task(
            task_id="user-a-org-2",
            callable=_blocking_task,
            user_id="#V#user_a",
            organisation_id="#V#org_2",
            namespace="#V#user_a@org_2",
        )

        with pytest.raises(BackgroundTaskCapacityReached):
            registry.submit_task(
                task_id="user-a-third",
                callable=_blocking_task,
                user_id="#V#user_a",
                organisation_id="#V#org_3",
                namespace="#V#user_a@org_3",
            )

        other = registry.submit_task(
            task_id="user-b-first",
            callable=_blocking_task,
            user_id="#V#user_b",
            organisation_id="#V#org_1",
            namespace="#V#user_b@org_1",
        )
        assert other.user_id == "#V#user_b"

        release.set()
        registry.shutdown(wait=True)

    def test_background_pending_queue_has_a_global_bound(self) -> None:
        registry = BackgroundTaskRegistry(
            max_workers=1,
            max_pending_tasks=1,
            max_active_tasks_per_user=1,
        )
        release = threading.Event()

        registry.submit_task(
            task_id="global-running",
            callable=lambda: release.wait(timeout=2),
        )
        registry.submit_task(
            task_id="global-pending",
            callable=lambda: release.wait(timeout=2),
        )

        with pytest.raises(BackgroundTaskCapacityReached):
            registry.submit_task(
                task_id="global-overflow",
                callable=lambda: None,
            )

        release.set()
        registry.shutdown(wait=True)

    def test_remove_task(self) -> None:
        """remove_task() should remove a task from the registry."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _task() -> str:
            return "done"

        registry.submit_task(task_id="remove-task", callable=_task)

        # Wait for completion
        for _ in range(50):
            status = registry.get_task_status("remove-task")
            if status and status.status == "completed":
                break
            time.sleep(0.05)

        removed = registry.remove_task("remove-task")
        assert removed is True

        assert registry.get_task_status("remove-task") is None

        registry.shutdown(wait=True)

    def test_progress_callback_receives_updates(self) -> None:
        """Progress callback should receive progress updates from task."""
        registry = BackgroundTaskRegistry(max_workers=1)
        captured_progress: list[dict[str, Any]] = []

        def _capture(info: Mapping[str, Any]) -> None:
            captured_progress.append(dict(info))

        def _task() -> str:
            return "done"

        registry.submit_task(
            task_id="progress-task",
            callable=_task,
            progress_callback=_capture,
        )

        # Wait for completion
        for _ in range(50):
            status = registry.get_task_status("progress-task")
            if status and status.status == "completed":
                break
            time.sleep(0.05)

        # Progress should have final "completed" status
        status = registry.get_task_status("progress-task")
        assert status is not None
        assert status.progress.get("status") == "completed"

        registry.shutdown(wait=True)

    def test_update_progress_records_bounded_history(self) -> None:
        """update_progress() should preserve recent progress snapshots."""
        registry = BackgroundTaskRegistry(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def _slow_task() -> str:
            started.set()
            release.wait(timeout=2)
            return "done"

        registry.submit_task(task_id="history-task", callable=_slow_task)
        assert started.wait(timeout=2)

        for index in range(205):
            updated = registry.update_progress(
                "history-task",
                {
                    "status": "workflow_step_complete",
                    "sequence": index,
                    "progress_facts": [
                        {
                            "fact_id": f"fact_{index}",
                            "label": "Fact",
                            "source_path": "context.value",
                            "status": "available",
                        }
                    ],
                },
            )
            assert updated is True

        status = registry.get_task_status("history-task")
        assert status is not None
        assert len(status.progress_history) == 200
        assert status.progress_history[0]["sequence"] == 5
        assert status.progress_history[-1]["sequence"] == 204
        assert (
            status.to_dict()["progress_history"][-1]["progress_facts"][0]["fact_id"]
            == "fact_204"
        )

        release.set()
        registry.shutdown(wait=True)

    def test_cancellation_exception_sets_cancelled_status(self) -> None:
        """Task raising CancellationRequested should have status='cancelled'."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _cancelling_task() -> None:
            raise CancellationRequested(task_id="task-cancelled")

        registry.submit_task(task_id="task-cancelled", callable=_cancelling_task)

        # Wait for task to complete (via cancellation)
        for _ in range(50):
            status = registry.get_task_status("task-cancelled")
            if status and status.status == "cancelled":
                break
            time.sleep(0.05)

        status = registry.get_task_status("task-cancelled")
        assert status is not None
        assert status.status == "cancelled"
        assert "Cancellation requested" in (status.error or "")
        assert status.progress.get("status") == "cancelled"

        registry.shutdown(wait=True)

    def test_cooperative_cancellation_via_checker(self) -> None:
        """Task should cancel when checking is_cancellation_requested()."""
        registry = BackgroundTaskRegistry(max_workers=1)
        iterations_completed = [0]

        def _cooperative_task() -> str:
            for i in range(100):
                if registry.is_cancellation_requested("coop-task"):
                    raise CancellationRequested(task_id="coop-task")
                iterations_completed[0] = i + 1
                time.sleep(0.01)
            return "done"

        registry.submit_task(task_id="coop-task", callable=_cooperative_task)

        # Wait a bit then request cancellation
        time.sleep(0.05)
        registry.request_cancellation("coop-task")

        # Wait for task to handle cancellation
        for _ in range(50):
            status = registry.get_task_status("coop-task")
            if status and status.status == "cancelled":
                break
            time.sleep(0.05)

        status = registry.get_task_status("coop-task")
        assert status is not None
        assert status.status == "cancelled"
        # Should have done some iterations but not all 100
        assert 0 < iterations_completed[0] < 100

        registry.shutdown(wait=True)

    def test_submit_task_with_actor_scope(self) -> None:
        """submit_task() should retain conversation and actor scope metadata."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _task() -> str:
            return "done"

        registry.submit_task(
            task_id="session-task",
            callable=_task,
            session_id="sess-123",
            user_id="user-456",
            organisation_id="organisation-789",
            namespace="#V#user-456@organisation-789",
            queue_id="queue-456",
            client_request_id="request-456",
            attempt_id="attempt-456",
        )

        # Wait for completion
        for _ in range(50):
            status = registry.get_task_status("session-task")
            if status and status.status == "completed":
                break
            time.sleep(0.05)

        status = registry.get_task_status("session-task")
        assert status is not None
        assert status.session_id == "sess-123"
        assert status.user_id == "user-456"
        assert status.organisation_id == "organisation-789"
        assert status.namespace == "#V#user-456@organisation-789"
        assert status.queue_id == "queue-456"
        assert status.client_request_id == "request-456"
        assert status.attempt_id == "attempt-456"

        # Check to_dict includes the fields
        status_dict = status.to_dict()
        assert status_dict["session_id"] == "sess-123"
        assert status_dict["user_id"] == "user-456"
        assert status_dict["organisation_id"] == "organisation-789"
        assert status_dict["namespace"] == "#V#user-456@organisation-789"
        assert status_dict["queue_id"] == "queue-456"
        assert status_dict["client_request_id"] == "request-456"
        assert status_dict["attempt_id"] == "attempt-456"

        registry.shutdown(wait=True)

    def test_mark_terminal_external_creates_completed_status(self) -> None:
        """mark_terminal_external() should restore a completed task result."""
        registry = BackgroundTaskRegistry(max_workers=1)

        status = registry.mark_terminal_external(
            "external-complete",
            status="completed",
            result={"source": "durable"},
            progress={"source": "durable_conversation_turn_instance"},
            session_id="session-123",
            user_id="user-456",
            organisation_id="organisation-789",
            namespace="#V#user-456@organisation-789",
        )

        assert status.status == "completed"
        assert status.result == {"source": "durable"}
        assert status.progress["status"] == "completed"
        assert status.progress["source"] == "durable_conversation_turn_instance"
        assert registry.get_task_result("external-complete") == {"source": "durable"}

        restored = registry.get_task_status("external-complete")
        assert restored is not None
        assert restored.session_id == "session-123"
        assert restored.user_id == "user-456"
        assert restored.organisation_id == "organisation-789"
        assert restored.namespace == "#V#user-456@organisation-789"

        registry.shutdown(wait=True)

    def test_mark_terminal_external_resists_late_worker_failure(self) -> None:
        """A durable completion should not be overwritten by a late worker error."""
        registry = BackgroundTaskRegistry(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def _late_failing_task() -> str:
            started.set()
            release.wait(timeout=2)
            raise RuntimeError("late post-processing failure")

        registry.submit_task(task_id="late-worker", callable=_late_failing_task)
        assert started.wait(timeout=2)

        registry.mark_terminal_external(
            "late-worker",
            status="completed",
            result={"source": "durable"},
            progress={"source": "durable_conversation_turn_instance"},
        )
        release.set()

        for _ in range(50):
            status = registry.get_task_status("late-worker")
            if status and status.completed_at is not None:
                break
            time.sleep(0.05)

        status = registry.get_task_status("late-worker")
        assert status is not None
        assert status.status == "completed"
        assert status.result == {"source": "durable"}
        assert status.error is None
        assert registry.get_task_result("late-worker") == {"source": "durable"}

        registry.shutdown(wait=True)

    def test_mark_terminal_external_rejects_conflicting_actor_scope(self) -> None:
        """Terminal reconciliation cannot cross scope bound at submission."""
        registry = BackgroundTaskRegistry(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def _task() -> str:
            started.set()
            release.wait(timeout=2)
            return "done"

        registry.submit_task(
            task_id="scope-bound",
            callable=_task,
            session_id="session-original",
            user_id="user-original",
            organisation_id="organisation-original",
            namespace="#V#user-original@organisation-original",
        )
        assert started.wait(timeout=2)

        with pytest.raises(BackgroundTaskScopeMismatch):
            registry.mark_terminal_external(
                "scope-bound",
                status="completed",
                result={"source": "durable"},
                session_id="session-late",
                user_id="user-late",
                organisation_id="organisation-late",
                namespace="#V#user-late@organisation-late",
            )

        status = registry.get_task_status("scope-bound")
        assert status is not None
        assert status.status == "running"
        assert status.session_id == "session-original"
        assert status.user_id == "user-original"
        assert status.organisation_id == "organisation-original"
        assert status.namespace == "#V#user-original@organisation-original"

        release.set()
        registry.shutdown(wait=True)

    def test_mark_terminal_external_completed_cannot_override_failed_status(
        self,
    ) -> None:
        """The first failed terminal result remains final."""
        registry = BackgroundTaskRegistry(max_workers=1)
        registry.mark_terminal_external(
            "restore-failed",
            status="failed",
            error="post-processing failed",
            progress={"source": "worker"},
        )

        unchanged = registry.mark_terminal_external(
            "restore-failed",
            status="completed",
            result={"source": "durable"},
            progress={"source": "durable_conversation_turn_instance"},
        )

        assert unchanged.status == "failed"
        assert unchanged.error == "post-processing failed"
        assert unchanged.result is None
        assert unchanged.progress["status"] == "failed"
        assert unchanged.progress["source"] == "worker"

        registry.shutdown(wait=True)

    def test_mark_terminal_external_completed_cannot_replace_completed_result(
        self,
    ) -> None:
        """A duplicate completion cannot replace the published result."""
        registry = BackgroundTaskRegistry(max_workers=1)
        registry.mark_terminal_external(
            "enrich-completed",
            status="completed",
            result={"source": "response_ready"},
            progress={"source": "background_generate_success_body"},
            session_id="session-early",
            user_id="user-early",
        )

        unchanged = registry.mark_terminal_external(
            "enrich-completed",
            status="completed",
            result={"source": "final_payload", "llm_debug": {"request_id": "req-1"}},
            progress={"source": "final_generate_payload"},
            session_id="session-final",
            user_id="user-early",
        )

        assert unchanged.status == "completed"
        assert unchanged.error is None
        assert unchanged.result == {"source": "response_ready"}
        assert unchanged.progress["source"] == "background_generate_success_body"
        assert unchanged.session_id == "session-early"
        assert unchanged.user_id == "user-early"
        assert registry.get_task_result("enrich-completed") == unchanged.result

        registry.shutdown(wait=True)

    def test_mark_terminal_external_completed_cannot_override_cancelled_status(
        self,
    ) -> None:
        """Late work cannot publish success after cancellation became terminal."""
        registry = BackgroundTaskRegistry(max_workers=1)
        registry.mark_terminal_external(
            "cancelled-first",
            status="cancelled",
            error="cancelled by user",
            progress={"source": "cancellation"},
        )

        unchanged = registry.mark_terminal_external(
            "cancelled-first",
            status="completed",
            result={"source": "late-worker"},
            progress={"source": "late-worker"},
        )

        assert unchanged.status == "cancelled"
        assert unchanged.error == "cancelled by user"
        assert unchanged.result is None
        assert unchanged.progress["status"] == "cancelled"
        assert unchanged.progress["source"] == "cancellation"

        registry.shutdown(wait=True)

    def test_list_tasks_filter_by_session_id(self) -> None:
        """list_tasks(session_id=...) should filter by session (Phase 4)."""
        registry = BackgroundTaskRegistry(max_workers=2)

        def _task() -> str:
            return "done"

        registry.submit_task(
            task_id="sess-a-task",
            callable=_task,
            session_id="session-A",
        )
        registry.submit_task(
            task_id="sess-b-task",
            callable=_task,
            session_id="session-B",
        )

        # Wait for completion
        time.sleep(0.1)

        session_a_tasks = registry.list_tasks(session_id="session-A")
        session_b_tasks = registry.list_tasks(session_id="session-B")
        all_tasks = registry.list_tasks()

        assert len(session_a_tasks) == 1
        assert session_a_tasks[0]["task_id"] == "sess-a-task"

        assert len(session_b_tasks) == 1
        assert session_b_tasks[0]["task_id"] == "sess-b-task"

        assert len(all_tasks) >= 2

        registry.shutdown(wait=True)

    def test_list_tasks_filter_by_user_id(self) -> None:
        """list_tasks(user_id=...) should filter by user (Phase 4)."""
        registry = BackgroundTaskRegistry(max_workers=2)

        def _task() -> str:
            return "done"

        registry.submit_task(
            task_id="user-1-task",
            callable=_task,
            user_id="user-1",
        )
        registry.submit_task(
            task_id="user-2-task",
            callable=_task,
            user_id="user-2",
        )

        # Wait for completion
        time.sleep(0.1)

        user_1_tasks = registry.list_tasks(user_id="user-1")
        user_2_tasks = registry.list_tasks(user_id="user-2")

        assert len(user_1_tasks) == 1
        assert user_1_tasks[0]["task_id"] == "user-1-task"

        assert len(user_2_tasks) == 1
        assert user_2_tasks[0]["task_id"] == "user-2-task"

        registry.shutdown(wait=True)

    def test_list_tasks_filters_by_organisation_and_namespace(self) -> None:
        """Organisation and namespace filters should isolate actor scopes."""
        registry = BackgroundTaskRegistry(max_workers=2)

        def _task() -> str:
            return "done"

        registry.submit_task(
            task_id="org-a-task",
            callable=_task,
            user_id="same-user",
            organisation_id="organisation-A",
            namespace="#V#same-user@organisation-A",
        )
        registry.submit_task(
            task_id="org-b-task",
            callable=_task,
            user_id="same-user",
            organisation_id="organisation-B",
            namespace="#V#same-user@organisation-B",
        )

        time.sleep(0.1)

        organisation_a_tasks = registry.list_tasks(
            user_id="same-user",
            organisation_id="organisation-A",
        )
        namespace_b_tasks = registry.list_tasks(
            user_id="same-user",
            namespace="#V#same-user@organisation-B",
        )

        assert [task["task_id"] for task in organisation_a_tasks] == ["org-a-task"]
        assert [task["task_id"] for task in namespace_b_tasks] == ["org-b-task"]

        registry.shutdown(wait=True)

    def test_exact_scope_controls_status_and_cancellation(self) -> None:
        """A guessed task ID must not cross user or organisation scope."""
        registry = BackgroundTaskRegistry(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def _task() -> str:
            started.set()
            release.wait(timeout=2)
            return "done"

        registry.submit_task(
            task_id="scoped-task",
            callable=_task,
            user_id="#V#same_user",
            organisation_id="#V#org_a",
            namespace="#V#same_user@org_a",
        )
        assert started.wait(timeout=2)

        assert (
            registry.get_task_status_for_scope(
                "scoped-task",
                user_id="#V#same_user",
                organisation_id="#V#org_b",
                namespace="#V#same_user@org_b",
            )
            is None
        )
        assert (
            registry.list_tasks_for_scope(
                user_id="#V#same_user",
                organisation_id="#V#org_b",
                namespace="#V#same_user@org_b",
            )
            == []
        )
        assert [
            task["task_id"]
            for task in registry.list_tasks_for_scope(
                user_id="#V#same_user",
                organisation_id="#V#org_a",
                namespace="#V#same_user@org_a",
            )
        ] == ["scoped-task"]
        assert not registry.request_cancellation_for_scope(
            "scoped-task",
            user_id="#V#other_user",
            organisation_id="#V#org_a",
            namespace="#V#other_user@org_a",
        )
        assert registry.get_task_status_for_scope(
            "scoped-task",
            user_id="#V#same_user",
            organisation_id="#V#org_a",
            namespace="#V#same_user@org_a",
        )
        assert registry.request_cancellation_for_scope(
            "scoped-task",
            user_id="#V#same_user",
            organisation_id="#V#org_a",
            namespace="#V#same_user@org_a",
        )

        release.set()
        registry.shutdown(wait=True)
