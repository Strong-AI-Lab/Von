"""Tests for background task service (JVNAUTOSCI-1038).

Validates the BackgroundTaskRegistry for task submission, status tracking,
and result retrieval.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from src.backend.integrations.internal_mcp.orchestrator import CancellationRequested
from src.backend.services.background_task_service import (
    BackgroundTaskRegistry,
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
        assert result["cancellation_requested"] is False

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

    def test_request_cancellation_sets_flag(self) -> None:
        """request_cancellation() should set the cancellation_requested flag."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _slow_task() -> str:
            time.sleep(10)
            return "done"

        registry.submit_task(task_id="task-cancel", callable=_slow_task)

        # Request cancellation
        success = registry.request_cancellation("task-cancel")
        assert success is True

        # Check flag
        assert registry.is_cancellation_requested("task-cancel") is True

        status = registry.get_task_status("task-cancel")
        assert status is not None
        assert status.cancellation_requested is True

        registry.shutdown(wait=False)

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

    def test_submit_task_with_session_and_user_id(self) -> None:
        """submit_task() should store session_id and user_id (Phase 4)."""
        registry = BackgroundTaskRegistry(max_workers=1)

        def _task() -> str:
            return "done"

        registry.submit_task(
            task_id="session-task",
            callable=_task,
            session_id="sess-123",
            user_id="user-456",
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

        # Check to_dict includes the fields
        status_dict = status.to_dict()
        assert status_dict["session_id"] == "sess-123"
        assert status_dict["user_id"] == "user-456"

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
