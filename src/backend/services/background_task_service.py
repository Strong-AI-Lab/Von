"""Background task execution service for JVNAUTOSCI-1038.

Provides a registry for running orchestrator requests in the background,
allowing users to switch conversations without aborting in-progress agent work.

Usage:
    from src.backend.services.background_task_service import (
        background_task_registry,
        TaskStatus,
    )

    # Submit a task
    background_task_registry.submit_task(
        task_id="request-123",
        callable=orchestrator.run,
        kwargs={"prompt": "...", "context": [...], ...},
        progress_callback=lambda info: ...,
    )

    # Check status
    status = background_task_registry.get_task_status("request-123")
    if status and status.status == "completed":
        result = status.result
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from src.backend.integrations.internal_mcp.orchestrator import CancellationRequested

_logger = logging.getLogger(__name__)

# Default task result TTL: 10 minutes
_DEFAULT_RESULT_TTL_SEC = 10 * 60

# Maximum concurrent background tasks
_MAX_WORKERS = 4

_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_PROGRESS_HISTORY_LIMIT = 200


def _is_active_status(status: str) -> bool:
    return status in {"pending", "running"}


def _append_progress_history(
    task_status: "TaskStatus",
    progress: Mapping[str, Any],
) -> None:
    progress_payload = dict(progress) if isinstance(progress, Mapping) else {}
    if not progress_payload:
        return
    entry = dict(progress_payload)
    entry.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    task_status.progress_history = [
        *task_status.progress_history[-(_PROGRESS_HISTORY_LIMIT - 1) :],
        entry,
    ]


@dataclass
class TaskStatus:
    """Status and result of a background task."""

    task_id: str
    status: str  # "pending", "running", "completed", "failed", "cancelled"
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: Any = None
    error: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    progress_history: list[dict[str, Any]] = field(default_factory=list)
    # For cancellation support (Phase 3)
    cancellation_requested: bool = False
    # For result retrieval filtering (Phase 4)
    session_id: str | None = None
    user_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise status to a dict suitable for JSON response."""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "has_result": self.result is not None,
            "error": self.error,
            "progress": self.progress,
            "progress_history": list(self.progress_history),
            "cancellation_requested": self.cancellation_requested,
            "session_id": self.session_id,
            "user_id": self.user_id,
        }


class BackgroundTaskRegistry:
    """Registry for background task execution and result retrieval.

    Thread-safe singleton for managing orchestrator requests that continue
    running even when the frontend disconnects or switches conversations.
    """

    def __init__(
        self,
        *,
        max_workers: int = _MAX_WORKERS,
        result_ttl_sec: float = _DEFAULT_RESULT_TTL_SEC,
    ) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskStatus] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="bg_task_"
        )
        self._result_ttl_sec = result_ttl_sec
        self._cleanup_interval_sec = 60.0
        self._last_cleanup = time.monotonic()

    def submit_task(
        self,
        task_id: str,
        callable: Callable[..., Any],
        *,
        args: tuple[Any, ...] = (),
        kwargs: Mapping[str, Any] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> TaskStatus:
        """Submit a callable to run in the background.

        Args:
            task_id: Unique identifier for this task (typically request_id).
            callable: The function to execute (e.g., orchestrator.run).
            args: Positional arguments for the callable.
            kwargs: Keyword arguments for the callable.
            progress_callback: Optional callback for progress updates.
            session_id: Optional session ID for filtering (Phase 4).
            user_id: Optional user ID for filtering (Phase 4).

        Returns:
            Initial TaskStatus with status="pending".

        Raises:
            ValueError: If task_id already exists and is not yet complete.
        """
        self._maybe_cleanup()
        kwargs = dict(kwargs) if kwargs else {}
        now = datetime.now(timezone.utc)

        with self._lock:
            existing = self._tasks.get(task_id)
            if existing and existing.status in ("pending", "running"):
                raise ValueError(
                    f"Task {task_id} already exists and is {existing.status}"
                )

            status = TaskStatus(
                task_id=task_id,
                status="pending",
                created_at=now,
                session_id=session_id,
                user_id=user_id,
            )
            self._tasks[task_id] = status

            def _run_task() -> Any:
                # Mark as running
                with self._lock:
                    task_status = self._tasks.get(task_id)
                    if task_status and _is_active_status(task_status.status):
                        task_status.status = "running"
                        task_status.started_at = datetime.now(timezone.utc)

                def _update_progress(info: Mapping[str, Any]) -> None:
                    with self._lock:
                        task_status = self._tasks.get(task_id)
                        if task_status and _is_active_status(task_status.status):
                            task_status.progress = dict(info)
                            _append_progress_history(task_status, task_status.progress)
                    if progress_callback:
                        try:
                            progress_callback(info)
                        except Exception:
                            pass

                try:
                    with self._lock:
                        task_status = self._tasks.get(task_id)
                        if (
                            task_status
                            and task_status.status == "cancelled"
                            and task_status.cancellation_requested
                        ):
                            raise CancellationRequested(task_id=task_id)

                    # Inject progress callback if supported
                    if "progress_callback" in kwargs or hasattr(callable, "__code__"):
                        # Try to pass progress via kwargs if the callable accepts it
                        pass  # Progress is handled via ProgressTracker now

                    result = callable(*args, **kwargs)

                    with self._lock:
                        task_status = self._tasks.get(task_id)
                        if task_status and _is_active_status(task_status.status):
                            task_status.status = "completed"
                            task_status.completed_at = datetime.now(timezone.utc)
                            task_status.result = result
                            task_status.progress["status"] = "completed"
                            _append_progress_history(task_status, task_status.progress)

                    return result

                except CancellationRequested as exc:
                    _logger.info(
                        "[background_task] Task %s cancelled: %s", task_id, exc
                    )
                    with self._lock:
                        task_status = self._tasks.get(task_id)
                        if task_status and _is_active_status(task_status.status):
                            task_status.status = "cancelled"
                            task_status.completed_at = datetime.now(timezone.utc)
                            task_status.error = str(exc)
                            task_status.progress["status"] = "cancelled"
                            _append_progress_history(task_status, task_status.progress)
                    raise

                except Exception as exc:
                    _logger.exception(
                        "[background_task] Task %s failed: %s", task_id, exc
                    )
                    with self._lock:
                        task_status = self._tasks.get(task_id)
                        if task_status and _is_active_status(task_status.status):
                            task_status.status = "failed"
                            task_status.completed_at = datetime.now(timezone.utc)
                            task_status.error = str(exc)
                            task_status.progress["status"] = "failed"
                            task_status.progress["error"] = str(exc)
                            _append_progress_history(task_status, task_status.progress)
                    raise

            future = self._executor.submit(_run_task)
            self._futures[task_id] = future

        _logger.info("[background_task] Submitted task %s", task_id)
        return status

    def mark_terminal_external(
        self,
        task_id: str,
        *,
        status: str,
        result: Any = None,
        error: str | None = None,
        progress: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> TaskStatus:
        """Record an externally observed terminal task state.

        Durable workflow execution can finish before the worker that launched it
        returns its final HTTP-shaped payload. This method lets polling routes
        publish that terminal durable result without allowing a late worker
        exception to overwrite it afterwards.
        """
        terminal_status = str(status or "").strip().lower()
        if terminal_status not in _TERMINAL_STATUSES:
            raise ValueError(f"Unsupported terminal task status: {status}")

        now = datetime.now(timezone.utc)
        progress_payload = dict(progress) if isinstance(progress, Mapping) else {}
        progress_payload.setdefault("status", terminal_status)

        with self._lock:
            task_status = self._tasks.get(task_id)
            if task_status is None:
                task_status = TaskStatus(
                    task_id=task_id,
                    status=terminal_status,
                    created_at=now,
                    started_at=now,
                    completed_at=now,
                    result=result,
                    error=error,
                    progress=progress_payload,
                    session_id=session_id,
                    user_id=user_id,
                )
                _append_progress_history(task_status, progress_payload)
                self._tasks[task_id] = task_status
                return task_status

            if task_status.status in _TERMINAL_STATUSES:
                if task_status.status != terminal_status and terminal_status == "completed":
                    task_status.status = terminal_status
                    task_status.completed_at = now
                    task_status.result = result
                    task_status.error = None
                    task_status.progress = progress_payload
                    _append_progress_history(task_status, progress_payload)
                    return task_status
                if task_status.result is None and result is not None:
                    task_status.result = result
                if task_status.error is None and error is not None:
                    task_status.error = error
                if not task_status.progress and progress_payload:
                    task_status.progress = progress_payload
                    _append_progress_history(task_status, progress_payload)
                return task_status

            task_status.status = terminal_status
            if task_status.started_at is None:
                task_status.started_at = now
            task_status.completed_at = now
            task_status.result = result
            task_status.error = error
            task_status.progress = progress_payload
            _append_progress_history(task_status, progress_payload)
            if task_status.session_id is None and session_id is not None:
                task_status.session_id = session_id
            if task_status.user_id is None and user_id is not None:
                task_status.user_id = user_id
            return task_status

    def get_task_status(self, task_id: str) -> TaskStatus | None:
        """Get the current status of a task.

        Args:
            task_id: The task identifier.

        Returns:
            TaskStatus if found, None otherwise.
        """
        with self._lock:
            return self._tasks.get(task_id)

    def update_progress(self, task_id: str, progress: Mapping[str, Any]) -> bool:
        """Update progress for an active task.

        Args:
            task_id: The task identifier.
            progress: Latest progress payload to expose via task status.

        Returns:
            True when an active task was updated.
        """
        progress_payload = dict(progress) if isinstance(progress, Mapping) else {}
        with self._lock:
            status = self._tasks.get(task_id)
            if status is None or not _is_active_status(status.status):
                return False
            status.progress = progress_payload
            _append_progress_history(status, progress_payload)
            return True

    def get_task_result(self, task_id: str) -> Any:
        """Get the result of a completed task.

        Args:
            task_id: The task identifier.

        Returns:
            The result value if completed successfully.

        Raises:
            KeyError: If task not found.
            ValueError: If task not yet completed.
            RuntimeError: If task failed (re-raises the original error message).
        """
        with self._lock:
            status = self._tasks.get(task_id)
            if status is None:
                raise KeyError(f"Task {task_id} not found")
            if status.status == "failed":
                raise RuntimeError(f"Task failed: {status.error}")
            if status.status not in ("completed",):
                raise ValueError(f"Task {task_id} is {status.status}, not completed")
            return status.result

    def request_cancellation(self, task_id: str) -> bool:
        """Request cancellation of a running task.

        This sets a flag that the task implementation can check.
        Actual cancellation depends on the task cooperating.

        Args:
            task_id: The task identifier.

        Returns:
            True if cancellation was requested, False if task not found or already done.
        """
        with self._lock:
            status = self._tasks.get(task_id)
            if status is None:
                return False
            if status.status not in ("pending", "running"):
                return False
            status.cancellation_requested = True
            status.status = "cancelled"
            if status.started_at is None:
                status.started_at = datetime.now(timezone.utc)
            status.completed_at = datetime.now(timezone.utc)
            status.error = f"Cancellation requested for task {task_id}"
            progress_payload = dict(status.progress)
            progress_payload.update(
                {
                    "status": "cancelled",
                    "phase": "cancelled",
                    "phase_label": "Cancelled",
                    "result_summary": (
                        "Cancellation requested for the background generate task."
                    ),
                }
            )
            status.progress = progress_payload
            _append_progress_history(status, progress_payload)
            _logger.info("[background_task] Task %s marked cancelled", task_id)
            return True

    def is_cancellation_requested(self, task_id: str) -> bool:
        """Check if cancellation was requested for a task.

        Args:
            task_id: The task identifier.

        Returns:
            True if cancellation was requested.
        """
        with self._lock:
            status = self._tasks.get(task_id)
            return status.cancellation_requested if status else False

    def remove_task(self, task_id: str) -> bool:
        """Remove a task from the registry.

        Should only be called for completed/failed tasks.

        Args:
            task_id: The task identifier.

        Returns:
            True if removed, False if not found.
        """
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                self._futures.pop(task_id, None)
                return True
            return False

    def list_tasks(
        self,
        *,
        status_filter: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all tasks, optionally filtered by status, session, or user.

        Args:
            status_filter: If provided, only return tasks with this status.
            session_id: If provided, only return tasks for this session (Phase 4).
            user_id: If provided, only return tasks for this user (Phase 4).

        Returns:
            List of task status dicts.
        """
        with self._lock:
            tasks = list(self._tasks.values())

        if status_filter:
            tasks = [t for t in tasks if t.status == status_filter]
        if session_id:
            tasks = [t for t in tasks if t.session_id == session_id]
        if user_id:
            tasks = [t for t in tasks if t.user_id == user_id]

        return [t.to_dict() for t in tasks]

    def _maybe_cleanup(self) -> None:
        """Clean up old completed/failed tasks if cleanup interval has passed."""
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval_sec:
            return

        self._last_cleanup = now
        cutoff = datetime.now(timezone.utc).timestamp() - self._result_ttl_sec

        with self._lock:
            to_remove = []
            for task_id, status in self._tasks.items():
                if status.status in ("completed", "failed", "cancelled"):
                    if status.completed_at and status.completed_at.timestamp() < cutoff:
                        to_remove.append(task_id)

            for task_id in to_remove:
                del self._tasks[task_id]
                self._futures.pop(task_id, None)

            if to_remove:
                _logger.info(
                    "[background_task] Cleaned up %d old tasks", len(to_remove)
                )

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the executor.

        Args:
            wait: If True, wait for pending tasks to complete.
        """
        self._executor.shutdown(wait=wait)


# Module-level singleton instance
background_task_registry = BackgroundTaskRegistry()
