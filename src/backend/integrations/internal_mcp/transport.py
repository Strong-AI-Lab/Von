"""Transport utilities for the internal MCP gateway.

The gateway is synchronous from its callers' perspective, but registered
handlers run on a bounded worker pool.  This keeps a slow Vontology/database
handler from occupying the workflow worker indefinitely while preserving the
caller's context variables (actor scope, AgentTest fault scope, and similar
request-local authority).

Deadlines have two deliberately separate meanings:

* the advisory budget is telemetry only and never changes a successful result;
* the hard deadline is terminal for the current turn and returns a typed MCP
  timeout payload.  A handler that later completes is diagnostics-only and its
  payload is discarded rather than allowed to rewrite the turn outcome.

The pool and its queue are both bounded.  Python cannot forcibly stop an
arbitrary running thread, so handlers may also use the cooperative cancellation
helpers in this module.  Non-cooperative late handlers remain isolated to the
fixed-size daemon pool instead of leaking an unbounded number of threads or
queued calls.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from collections import deque
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass, field
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

_DEFAULT_READ_ADVISORY_TIMEOUT_SEC = 6.0
_DEFAULT_READ_HARD_TIMEOUT_SEC = 20.0
_DEFAULT_WRITE_ADVISORY_TIMEOUT_SEC = 20.0
_DEFAULT_WRITE_HARD_TIMEOUT_SEC = 20.0
_DEFAULT_HANDLER_WORKER_COUNT = 8
_DEFAULT_HANDLER_QUEUE_CAPACITY = 32
_LATE_COMPLETION_HISTORY_LIMIT = 50


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    return value if value > 0.0 else float(default)


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return value if value > 0 else int(default)


@dataclass(frozen=True)
class InternalMCPExecutionScope:
    """Context available to a cooperatively cancellable MCP handler."""

    execution_id: str
    method_name: str
    deadline_monotonic: float
    cancellation_event: threading.Event

    @property
    def cancellation_requested(self) -> bool:
        return self.cancellation_event.is_set()

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())


_ACTIVE_EXECUTION_SCOPE: ContextVar[InternalMCPExecutionScope | None] = ContextVar(
    "internal_mcp_execution_scope",
    default=None,
)


class InternalMCPHandlerCancelled(RuntimeError):
    """Raised by cooperative handlers after the transport requests cancellation."""


def get_internal_mcp_execution_scope() -> InternalMCPExecutionScope | None:
    """Return the active handler deadline/cancellation scope, if any."""

    return _ACTIVE_EXECUTION_SCOPE.get()


def internal_mcp_cancellation_requested() -> bool:
    """Return whether the active handler should stop as soon as safely possible."""

    scope = get_internal_mcp_execution_scope()
    return bool(scope and scope.cancellation_requested)


def raise_if_internal_mcp_cancelled() -> None:
    """Give long-running handlers a reusable cooperative cancellation point."""

    scope = get_internal_mcp_execution_scope()
    if scope is not None and scope.cancellation_requested:
        raise InternalMCPHandlerCancelled(
            f"Internal MCP execution {scope.execution_id} was cancelled."
        )


@dataclass(frozen=True)
class TransportResult:
    """Container for a terminal turn result and truthful transport timing."""

    payload: Any
    duration_ms: float
    execution_id: str | None = None
    outcome: str = "completed"
    timeout_sec: float | None = None
    advisory_timeout_sec: float | None = None
    advisory_budget_exceeded: bool = False
    queue_duration_ms: float | None = None
    handler_duration_ms: float | None = None
    handler_elapsed_ms: float | None = None
    transport_overhead_ms: float | None = None
    timeout_phase: str | None = None

    @property
    def timed_out(self) -> bool:
        return self.outcome == "timed_out"

    def telemetry_metadata(self) -> Dict[str, Any]:
        """Return the bounded metadata persisted with a tool invocation."""

        return {
            "schema_version": "internal_mcp_transport.v1",
            "execution_id": self.execution_id,
            "outcome": self.outcome,
            "duration_ms": self.duration_ms,
            "timeout_sec": self.timeout_sec,
            "advisory_timeout_sec": self.advisory_timeout_sec,
            "advisory_budget_exceeded": self.advisory_budget_exceeded,
            "queue_duration_ms": self.queue_duration_ms,
            "handler_duration_ms": self.handler_duration_ms,
            "handler_elapsed_ms": self.handler_elapsed_ms,
            "transport_overhead_ms": self.transport_overhead_ms,
            "timeout_phase": self.timeout_phase,
            "late_result_policy": "discard_from_turn",
        }


@dataclass
class _HandlerTask:
    execution_id: str
    method_name: str
    context: Context
    handler: Callable[..., Any]
    payload: Dict[str, Any]
    deadline_monotonic: float
    submitted_at: float
    on_late_completion: Callable[["_HandlerTask"], None]
    cancellation_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    started_at: float | None = None
    completed_at: float | None = None
    result: Any = None
    exception: BaseException | None = None
    terminal_returned: bool = False
    cancelled_before_start: bool = False

    def _invoke(self) -> Any:
        scope = InternalMCPExecutionScope(
            execution_id=self.execution_id,
            method_name=self.method_name,
            deadline_monotonic=self.deadline_monotonic,
            cancellation_event=self.cancellation_event,
        )
        token = _ACTIVE_EXECUTION_SCOPE.set(scope)
        try:
            return self.handler(**self.payload)
        finally:
            _ACTIVE_EXECUTION_SCOPE.reset(token)

    def run(self) -> None:
        with self.lock:
            if self.cancellation_event.is_set():
                self.cancelled_before_start = True
                self.completed_at = time.perf_counter()
                self.done_event.set()
                return
            self.started_at = time.perf_counter()

        try:
            result = self.context.run(self._invoke)
        except BaseException as exc:  # preserve the handler's existing semantics
            exception: BaseException | None = exc
            result = None
        else:
            exception = None

        completed_at = time.perf_counter()
        with self.lock:
            self.result = result
            self.exception = exception
            self.completed_at = completed_at
            was_late = self.terminal_returned
            self.done_event.set()

        if was_late:
            self.on_late_completion(self)


class _BoundedHandlerExecutor:
    """Small bounded daemon pool used for internal handler isolation."""

    def __init__(self, *, worker_count: int, queue_capacity: int) -> None:
        self.worker_count = max(1, int(worker_count))
        self.queue_capacity = max(1, int(queue_capacity))
        self._queue: queue.Queue[_HandlerTask] = queue.Queue(
            maxsize=self.queue_capacity
        )
        self._lock = threading.Lock()
        self._submitted = 0
        self._rejected = 0
        self._active = 0
        self._completed = 0
        self._workers = [
            threading.Thread(
                target=self._worker,
                name=f"internal-mcp-handler-{index + 1}",
                daemon=True,
            )
            for index in range(self.worker_count)
        ]
        for worker in self._workers:
            worker.start()

    def submit(self, task: _HandlerTask) -> bool:
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            with self._lock:
                self._rejected += 1
            return False
        with self._lock:
            self._submitted += 1
        return True

    def _worker(self) -> None:
        while True:
            task = self._queue.get()
            with self._lock:
                self._active += 1
            try:
                task.run()
            finally:
                with self._lock:
                    self._active = max(0, self._active - 1)
                    self._completed += 1
                self._queue.task_done()

    def diagnostics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "worker_count": self.worker_count,
                "queue_capacity": self.queue_capacity,
                "queue_depth": self._queue.qsize(),
                "active_worker_count": self._active,
                "submitted_count": self._submitted,
                "completed_count": self._completed,
                "rejected_count": self._rejected,
            }


_SHARED_EXECUTOR: _BoundedHandlerExecutor | None = None
_SHARED_EXECUTOR_LOCK = threading.Lock()


def _shared_handler_executor() -> _BoundedHandlerExecutor:
    global _SHARED_EXECUTOR
    if _SHARED_EXECUTOR is not None:
        return _SHARED_EXECUTOR
    with _SHARED_EXECUTOR_LOCK:
        if _SHARED_EXECUTOR is None:
            _SHARED_EXECUTOR = _BoundedHandlerExecutor(
                worker_count=_positive_int_env(
                    "VON_INTERNAL_MCP_HANDLER_WORKERS",
                    _DEFAULT_HANDLER_WORKER_COUNT,
                ),
                queue_capacity=_positive_int_env(
                    "VON_INTERNAL_MCP_HANDLER_QUEUE_CAPACITY",
                    _DEFAULT_HANDLER_QUEUE_CAPACITY,
                ),
            )
    return _SHARED_EXECUTOR


class InternalMCPTransport:
    """Execute handlers with bounded isolation, deadlines, and timing telemetry."""

    def __init__(
        self,
        *,
        read_timeout_sec: float | None = None,
        write_timeout_sec: float | None = None,
        read_advisory_timeout_sec: float | None = None,
        write_advisory_timeout_sec: float | None = None,
        handler_executor: _BoundedHandlerExecutor | None = None,
    ):
        self._read_timeout_sec = float(
            read_timeout_sec
            if read_timeout_sec is not None
            else _positive_float_env(
                "VON_INTERNAL_MCP_READ_HARD_TIMEOUT_SEC",
                _DEFAULT_READ_HARD_TIMEOUT_SEC,
            )
        )
        self._write_timeout_sec = float(
            write_timeout_sec
            if write_timeout_sec is not None
            else _positive_float_env(
                "VON_INTERNAL_MCP_WRITE_HARD_TIMEOUT_SEC",
                _DEFAULT_WRITE_HARD_TIMEOUT_SEC,
            )
        )
        self._read_advisory_timeout_sec = float(
            read_advisory_timeout_sec
            if read_advisory_timeout_sec is not None
            else _positive_float_env(
                "VON_INTERNAL_MCP_READ_ADVISORY_TIMEOUT_SEC",
                _DEFAULT_READ_ADVISORY_TIMEOUT_SEC,
            )
        )
        self._write_advisory_timeout_sec = float(
            write_advisory_timeout_sec
            if write_advisory_timeout_sec is not None
            else _positive_float_env(
                "VON_INTERNAL_MCP_WRITE_ADVISORY_TIMEOUT_SEC",
                _DEFAULT_WRITE_ADVISORY_TIMEOUT_SEC,
            )
        )
        for value, name in (
            (self._read_timeout_sec, "read_timeout_sec"),
            (self._write_timeout_sec, "write_timeout_sec"),
            (self._read_advisory_timeout_sec, "read_advisory_timeout_sec"),
            (self._write_advisory_timeout_sec, "write_advisory_timeout_sec"),
        ):
            if value <= 0.0:
                raise ValueError(f"{name} must be positive")
        self._executor = handler_executor or _shared_handler_executor()
        self._diagnostics_lock = threading.Lock()
        self._timeout_count = 0
        self._saturation_count = 0
        self._late_completion_count = 0
        self._late_completions: deque[Dict[str, Any]] = deque(
            maxlen=_LATE_COMPLETION_HISTORY_LIMIT
        )

    @property
    def read_timeout_sec(self) -> float:
        """Return the hard deadline for a read handler."""

        return self._read_timeout_sec

    @property
    def write_timeout_sec(self) -> float:
        """Return the hard deadline for a write handler."""

        return self._write_timeout_sec

    def advisory_timeout_sec(
        self,
        category: str,
        *,
        hard_timeout_sec: float,
    ) -> float:
        configured = (
            self._write_advisory_timeout_sec
            if str(category or "").strip().lower() == "write"
            else self._read_advisory_timeout_sec
        )
        return min(float(configured), float(hard_timeout_sec))

    @staticmethod
    def _timing_snapshot(
        task: _HandlerTask,
        *,
        terminal_at: float,
    ) -> tuple[float, float | None, float | None]:
        with task.lock:
            started_at = task.started_at
            completed_at = task.completed_at
        queue_duration_ms = (
            max(0.0, (started_at - task.submitted_at) * 1000.0)
            if started_at is not None
            else max(0.0, (terminal_at - task.submitted_at) * 1000.0)
        )
        handler_duration_ms = (
            max(0.0, (completed_at - started_at) * 1000.0)
            if started_at is not None and completed_at is not None
            else None
        )
        handler_elapsed_ms = (
            max(0.0, ((completed_at or terminal_at) - started_at) * 1000.0)
            if started_at is not None
            else None
        )
        return queue_duration_ms, handler_duration_ms, handler_elapsed_ms

    def _record_late_completion(self, task: _HandlerTask) -> None:
        terminal_at = task.completed_at or time.perf_counter()
        queue_ms, handler_ms, _ = self._timing_snapshot(
            task,
            terminal_at=terminal_at,
        )
        with self._diagnostics_lock:
            self._late_completion_count += 1
            self._late_completions.append(
                {
                    "execution_id": task.execution_id,
                    "method_name": task.method_name,
                    "outcome": (
                        "late_error" if task.exception is not None else "late_success"
                    ),
                    "queue_duration_ms": queue_ms,
                    "handler_duration_ms": handler_ms,
                    "payload_discarded": True,
                }
            )
        logger.warning(
            "[mcp_transport] discarded late %s for %s (execution_id=%s, handler=%.2fms)",
            "error" if task.exception is not None else "result",
            task.method_name,
            task.execution_id,
            handler_ms or 0.0,
        )

    @staticmethod
    def _timeout_payload(
        *,
        method_name: str,
        execution_id: str,
        category: str,
        timeout_sec: float,
        advisory_timeout_sec: float,
        timeout_phase: str,
        queue_duration_ms: float,
        handler_elapsed_ms: float | None,
    ) -> Dict[str, Any]:
        is_write = str(category or "").strip().lower() == "write"
        payload: Dict[str, Any] = {
            "success": False,
            "status": "timed_out",
            "error": (
                f"Internal MCP tool '{method_name}' exceeded its "
                f"{timeout_sec:.1f}s hard deadline."
            ),
            "error_code": (
                "tool_timeout_outcome_unknown" if is_write else "tool_timeout"
            ),
            "error_type": "deadline_exceeded",
            "retryable": not is_write,
            "timeout_seconds": timeout_sec,
            "advisory_timeout_seconds": advisory_timeout_sec,
            "timeout_phase": timeout_phase,
            "execution_id": execution_id,
            "queue_duration_ms": queue_duration_ms,
            "handler_elapsed_ms": handler_elapsed_ms,
            "cancellation_requested": True,
            "handler_isolation": "bounded_worker_pool",
            "outcome_finality": "terminal_for_turn",
            "late_result_policy": "discard_from_turn",
        }
        if is_write:
            payload["mutation_outcome"] = "unknown"
            payload["recovery_affordances"] = [
                {"action_type": "inspect_operation_state_before_retry"}
            ]
        else:
            payload["recovery_affordances"] = [
                {"action_type": "bounded_retry"},
                {"action_type": "choose_alternate_represented_path"},
                {"action_type": "return_bounded_failure"},
            ]
        return payload

    def _saturation_payload(
        self,
        *,
        method_name: str,
        execution_id: str,
        category: str,
        timeout_sec: float,
        advisory_timeout_sec: float,
    ) -> Dict[str, Any]:
        is_write = str(category or "").strip().lower() == "write"
        return {
            "success": False,
            "status": "failed",
            "error": "The bounded internal MCP handler pool is saturated.",
            "error_code": "internal_mcp_handler_pool_saturated",
            "error_type": "capacity_exhausted",
            "retryable": not is_write,
            "execution_id": execution_id,
            "timeout_seconds": timeout_sec,
            "advisory_timeout_seconds": advisory_timeout_sec,
            "handler_isolation": "bounded_worker_pool",
            "outcome_finality": "terminal_for_turn",
            "recovery_affordances": (
                [{"action_type": "inspect_operation_state_before_retry"}]
                if is_write
                else [
                    {"action_type": "bounded_retry"},
                    {"action_type": "choose_alternate_represented_path"},
                ]
            ),
        }

    def execute(
        self,
        *,
        method_name: str,
        handler: Callable[..., Any],
        payload: Dict[str, Any],
        timeout_sec: float | None,
        category: str = "read",
        advisory_timeout_sec: float | None = None,
        log_tag: str = "[mcp_gateway]",
    ) -> TransportResult:
        """Execute a handler within a bounded hard deadline.

        The returned result is immutable for the current turn.  If the handler
        ignores cooperative cancellation and completes late, only bounded
        aggregate diagnostics are updated; the late payload is never surfaced.
        """

        hard_timeout_sec = float(
            timeout_sec
            if timeout_sec is not None and float(timeout_sec) > 0.0
            else (
                self._write_timeout_sec
                if str(category or "").strip().lower() == "write"
                else self._read_timeout_sec
            )
        )
        advisory_sec = float(
            advisory_timeout_sec
            if advisory_timeout_sec is not None and advisory_timeout_sec > 0.0
            else self.advisory_timeout_sec(
                category,
                hard_timeout_sec=hard_timeout_sec,
            )
        )
        advisory_sec = min(advisory_sec, hard_timeout_sec)
        submitted_at = time.perf_counter()
        execution_id = f"mcp_{uuid.uuid4().hex}"
        deadline_monotonic = submitted_at + hard_timeout_sec
        task = _HandlerTask(
            execution_id=execution_id,
            method_name=method_name,
            context=copy_context(),
            handler=handler,
            payload=dict(payload),
            deadline_monotonic=deadline_monotonic,
            submitted_at=submitted_at,
            on_late_completion=self._record_late_completion,
        )
        logger.info(
            "%s invoking %s (advisory=%.1fs, hard_deadline=%.1fs, execution_id=%s)",
            log_tag,
            method_name,
            advisory_sec,
            hard_timeout_sec,
            execution_id,
        )

        if not self._executor.submit(task):
            terminal_at = time.perf_counter()
            duration_ms = (terminal_at - submitted_at) * 1000.0
            with self._diagnostics_lock:
                self._saturation_count += 1
            payload_result = self._saturation_payload(
                method_name=method_name,
                execution_id=execution_id,
                category=category,
                timeout_sec=hard_timeout_sec,
                advisory_timeout_sec=advisory_sec,
            )
            return TransportResult(
                payload=payload_result,
                duration_ms=duration_ms,
                execution_id=execution_id,
                outcome="saturated",
                timeout_sec=hard_timeout_sec,
                advisory_timeout_sec=advisory_sec,
                queue_duration_ms=duration_ms,
                handler_elapsed_ms=None,
                transport_overhead_ms=0.0,
                timeout_phase="queue",
            )

        completed = task.done_event.wait(timeout=hard_timeout_sec)
        terminal_at = time.perf_counter()
        with task.lock:
            if completed or task.done_event.is_set():
                task_completed = True
            else:
                task.terminal_returned = True
                task.cancellation_event.set()
                task_completed = False
            result = task.result
            exception = task.exception
            started_at = task.started_at

        queue_ms, handler_ms, handler_elapsed_ms = self._timing_snapshot(
            task,
            terminal_at=terminal_at,
        )
        duration_ms = max(0.0, (terminal_at - submitted_at) * 1000.0)

        if not task_completed:
            with self._diagnostics_lock:
                self._timeout_count += 1
            timeout_phase = "handler" if started_at is not None else "queue"
            transport_overhead_ms = max(
                0.0,
                duration_ms - queue_ms - (handler_elapsed_ms or 0.0),
            )
            logger.warning(
                "%s timed out %s after %.2fms "
                "(phase=%s, advisory=%.1fs, hard_deadline=%.1fs, execution_id=%s)",
                log_tag,
                method_name,
                duration_ms,
                timeout_phase,
                advisory_sec,
                hard_timeout_sec,
                execution_id,
            )
            timeout_payload = self._timeout_payload(
                method_name=method_name,
                execution_id=execution_id,
                category=category,
                timeout_sec=hard_timeout_sec,
                advisory_timeout_sec=advisory_sec,
                timeout_phase=timeout_phase,
                queue_duration_ms=queue_ms,
                handler_elapsed_ms=handler_elapsed_ms,
            )
            return TransportResult(
                payload=timeout_payload,
                duration_ms=duration_ms,
                execution_id=execution_id,
                outcome="timed_out",
                timeout_sec=hard_timeout_sec,
                advisory_timeout_sec=advisory_sec,
                advisory_budget_exceeded=True,
                queue_duration_ms=queue_ms,
                handler_duration_ms=None,
                handler_elapsed_ms=handler_elapsed_ms,
                transport_overhead_ms=transport_overhead_ms,
                timeout_phase=timeout_phase,
            )

        if exception is not None:
            raise exception

        handler_ms = handler_ms or 0.0
        transport_overhead_ms = max(0.0, duration_ms - queue_ms - handler_ms)
        advisory_exceeded = duration_ms > advisory_sec * 1000.0
        logger.info(
            "%s completed %s in %.2fms "
            "(queue=%.2fms, handler=%.2fms, overhead=%.2fms, advisory_exceeded=%s)",
            log_tag,
            method_name,
            duration_ms,
            queue_ms,
            handler_ms,
            transport_overhead_ms,
            advisory_exceeded,
        )
        return TransportResult(
            payload=result,
            duration_ms=duration_ms,
            execution_id=execution_id,
            outcome="completed",
            timeout_sec=hard_timeout_sec,
            advisory_timeout_sec=advisory_sec,
            advisory_budget_exceeded=advisory_exceeded,
            queue_duration_ms=queue_ms,
            handler_duration_ms=handler_ms,
            handler_elapsed_ms=handler_ms,
            transport_overhead_ms=transport_overhead_ms,
        )

    def get_diagnostics(self) -> Dict[str, Any]:
        with self._diagnostics_lock:
            local = {
                "read_advisory_timeout_sec": self._read_advisory_timeout_sec,
                "read_hard_timeout_sec": self._read_timeout_sec,
                "write_advisory_timeout_sec": self._write_advisory_timeout_sec,
                "write_hard_timeout_sec": self._write_timeout_sec,
                "timeout_count": self._timeout_count,
                "saturation_count": self._saturation_count,
                "late_completion_count": self._late_completion_count,
                "late_completions": list(self._late_completions),
                "late_result_policy": "discard_from_turn",
            }
        local["handler_pool"] = self._executor.diagnostics()
        return local
