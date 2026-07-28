"""Transport utilities for the internal MCP gateway.

The gateway is synchronous from its callers' perspective, but registered
handlers run on a bounded worker pool.  This keeps a slow Vontology/database
handler from occupying the workflow worker indefinitely while preserving the
caller's context variables (actor scope, AgentTest fault scope, and similar
request-local authority).

Deadlines have two deliberately separate meanings:

* the advisory budget is telemetry only and never changes a successful result;
* the hard deadline is terminal for the current turn and returns a typed MCP
  timeout payload.  A handler that later completes cannot rewrite that outcome.
  Reads and calls without an observer discard the late payload; an explicitly
  observed handler-started write may emit one bounded out-of-band completion
  observation.

The pool and its queue are both bounded.  Python cannot forcibly stop an
arbitrary running thread, so handlers may also use the cooperative cancellation
helpers in this module.  Non-cooperative late handlers remain isolated to the
fixed-size daemon pool instead of leaking an unbounded number of threads or
queued calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections import deque
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping

from pymongo import timeout as pymongo_timeout
from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

_DEFAULT_READ_ADVISORY_TIMEOUT_SEC = 6.0
_DEFAULT_READ_HARD_TIMEOUT_SEC = 20.0
_DEFAULT_WRITE_ADVISORY_TIMEOUT_SEC = 20.0
_DEFAULT_WRITE_HARD_TIMEOUT_SEC = 20.0
_DEFAULT_HANDLER_WORKER_COUNT = 8
_DEFAULT_HANDLER_QUEUE_CAPACITY = 32
# A task admitted with a minimum equal to its hard timeout necessarily loses a
# few microseconds between the submission and worker locks.  This is the only
# scheduling tolerance applied at handler start; materially queued work must
# still retain its configured minimum.
_EFFECT_ADMISSION_START_TOLERANCE_SEC = 0.005
# Leave a small bounded interval for a database deadline to unwind through
# legacy handlers that catch and return exceptions before the transport itself
# reaches its terminal deadline.  This prevents a caught CSOT expiry from
# becoming an apparently completed error payload or a misleading late result.
_MONGO_DEADLINE_COMPLETION_RESERVE_SEC = 0.05
_MONGO_DEADLINE_COMPLETION_RESERVE_FRACTION = 0.1
_MONGO_DEADLINE_CLASSIFICATION_TOLERANCE_SEC = 0.1
_MONGO_CSOT_ADMISSION_REFUSAL_MARKER = (
    "operation would exceed time limit, remaining timeout:"
)
_MONGO_CSOT_CONFIGURED_TIMEOUT_MARKER = "configured timeouts: timeoutms:"
_LATE_COMPLETION_HISTORY_LIMIT = 50
_LATE_COMPLETION_MAX_PAYLOAD_CHARS = 32_000
_LATE_COMPLETION_MAX_RECEIPT_FIELD_CHARS = 4_000
_LATE_COMPLETION_RECEIPT_FIELDS = (
    "success",
    "status",
    "effect_status",
    "changed",
    "error",
    "error_code",
    "mutation_outcome",
    "partial_failures",
)
_INTERMEDIATE_EFFECT_RECEIPT_FIELDS = (
    "schema_version",
    "success",
    "status",
    "effect_status",
    "changed",
    "workflow_id",
    "instance_id",
    "created_new",
    "durable_submission_status",
    "final_status",
    "mutation_outcome",
    "outcome_finality",
)


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_late_completion_value(value: Any) -> tuple[Any, bool]:
    """Return a small JSON-compatible handler-result projection."""

    try:
        serialised = json.dumps(
            value,
            default=str,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except Exception as exc:
        return {
            "_truncated": True,
            "_type": type(value).__name__,
            "_serialisation_error": type(exc).__name__,
        }, True
    if len(serialised) <= _LATE_COMPLETION_MAX_PAYLOAD_CHARS:
        return json.loads(serialised), False

    projection: dict[str, Any] = {
        "_truncated": True,
        "_original_char_count": len(serialised),
        "_sha256": hashlib.sha256(serialised.encode("utf-8")).hexdigest(),
    }
    if isinstance(value, Mapping):
        for key in _LATE_COMPLETION_RECEIPT_FIELDS:
            if key not in value:
                continue
            try:
                field_text = json.dumps(
                    value.get(key),
                    default=str,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except Exception:
                continue
            if len(field_text) > _LATE_COMPLETION_MAX_RECEIPT_FIELD_CHARS:
                projection[key] = (
                    field_text[:_LATE_COMPLETION_MAX_RECEIPT_FIELD_CHARS] + "..."
                )
            else:
                projection[key] = json.loads(field_text)
    return projection, True


@dataclass(frozen=True)
class InternalMCPExecutionScope:
    """Context available to a cooperatively cancellable MCP handler."""

    execution_id: str
    method_name: str
    deadline_monotonic: float
    cancellation_event: threading.Event
    effect_receipt_recorder: Callable[[Mapping[str, Any]], None] | None = None

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


class InternalMCPHandlerDeadlineExceeded(InternalMCPHandlerCancelled):
    """Raised when a handler's database work consumes its transport deadline."""


def _mongo_timeout_consumed_transport_deadline(
    exc: PyMongoError,
    *,
    scope: InternalMCPExecutionScope,
) -> bool:
    if not bool(getattr(exc, "timeout", False)):
        return False
    details = getattr(exc, "details", None)
    detail_message = (
        str(details.get("errmsg") or "")
        if isinstance(details, Mapping)
        else ""
    )
    if _MONGO_CSOT_ADMISSION_REFUSAL_MARKER in (
        f"{exc} {detail_message}".lower()
    ):
        return True
    return (
        scope.remaining_seconds
        <= _MONGO_DEADLINE_CLASSIFICATION_TOLERANCE_SEC
    )


def _handler_payload_reports_mongo_deadline(
    payload: Any,
    *,
    scope: InternalMCPExecutionScope,
) -> bool:
    if not isinstance(payload, Mapping):
        return False
    messages = [
        payload.get("error"),
        payload.get("message"),
        payload.get("detail"),
    ]
    details = payload.get("details")
    if isinstance(details, Mapping):
        messages.extend((details.get("error"), details.get("errmsg")))
    combined_message = " ".join(
        str(message or "") for message in messages
    ).lower()
    if _MONGO_CSOT_ADMISSION_REFUSAL_MARKER in combined_message:
        return True
    return bool(
        _MONGO_CSOT_CONFIGURED_TIMEOUT_MARKER in combined_message
        and scope.remaining_seconds
        <= _MONGO_DEADLINE_CLASSIFICATION_TOLERANCE_SEC
    )


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


def record_internal_mcp_effect_receipt(receipt: Mapping[str, Any]) -> bool:
    """Record bounded durable effect identity before a handler returns.

    This is intentionally an observation channel, not a way to extend the
    handler deadline or rewrite a terminal turn result. Only handlers running
    inside the internal MCP transport can publish a receipt.
    """

    scope = get_internal_mcp_execution_scope()
    if scope is None or scope.effect_receipt_recorder is None:
        return False
    scope.effect_receipt_recorder(receipt)
    return True


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
    late_result_policy: str = "discard_from_turn"
    configured_hard_timeout_sec: float | None = None
    minimum_execution_window_sec: float | None = None

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
            "late_result_policy": self.late_result_policy,
            "configured_hard_timeout_sec": self.configured_hard_timeout_sec,
            "minimum_execution_window_sec": self.minimum_execution_window_sec,
        }


LateCompletionObserver = Callable[[Dict[str, Any]], None]


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
    category: str = "read"
    minimum_execution_window_sec: float | None = None
    late_completion_observer: LateCompletionObserver | None = None
    cancellation_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    started_at: float | None = None
    completed_at: float | None = None
    completed_monotonic: float | None = None
    result: Any = None
    exception: BaseException | None = None
    deadline_exceeded_during_handler: bool = False
    terminal_returned: bool = False
    cancelled_before_start: bool = False
    admission_denied_before_start: bool = False
    admission_remaining_window_sec: float | None = None
    late_completion_recorded: bool = False
    effect_receipt: dict[str, Any] | None = None

    def record_effect_receipt(self, receipt: Mapping[str, Any]) -> None:
        bounded: dict[str, Any] = {}
        for key in _INTERMEDIATE_EFFECT_RECEIPT_FIELDS:
            if key not in receipt:
                continue
            value, truncated = _bounded_late_completion_value(receipt.get(key))
            if truncated:
                continue
            bounded[key] = value
        if not bounded:
            return
        with self.lock:
            self.effect_receipt = bounded

    def _invoke(self) -> Any:
        scope = InternalMCPExecutionScope(
            execution_id=self.execution_id,
            method_name=self.method_name,
            deadline_monotonic=self.deadline_monotonic,
            cancellation_event=self.cancellation_event,
            effect_receipt_recorder=self.record_effect_receipt,
        )
        token = _ACTIVE_EXECUTION_SCOPE.set(scope)
        try:
            remaining_seconds = scope.remaining_seconds
            if remaining_seconds <= 0.0:
                raise InternalMCPHandlerDeadlineExceeded(
                    f"Internal MCP execution {scope.execution_id} reached its "
                    "deadline before the handler started."
                )
            database_completion_reserve_sec = min(
                _MONGO_DEADLINE_COMPLETION_RESERVE_SEC,
                remaining_seconds
                * _MONGO_DEADLINE_COMPLETION_RESERVE_FRACTION,
            )
            database_timeout_sec = (
                remaining_seconds - database_completion_reserve_sec
            )
            try:
                # PyMongo CSOT is context-local, applies the remaining budget
                # across all nested database operations, and leaves only a
                # small bounded interval for caught exceptions to unwind before
                # the transport deadline. Non-Mongo handlers are unaffected
                # and remain bounded by the transport worker pool.
                with pymongo_timeout(database_timeout_sec):
                    result = self.handler(**self.payload)
                # Some legacy support handlers convert every exception to an
                # error mapping.  Preserve their compatibility behaviour for
                # ordinary errors, but do not let an explicit PyMongo CSOT
                # admission refusal masquerade as a completed transport call.
                if _handler_payload_reports_mongo_deadline(
                    result,
                    scope=scope,
                ):
                    raise InternalMCPHandlerDeadlineExceeded(
                        f"Internal MCP execution {scope.execution_id} consumed "
                        "its database operation deadline."
                    )
                return result
            except PyMongoError as exc:
                if _mongo_timeout_consumed_transport_deadline(
                    exc,
                    scope=scope,
                ):
                    raise InternalMCPHandlerDeadlineExceeded(
                        f"Internal MCP execution {scope.execution_id} consumed "
                        "its database operation deadline."
                    ) from exc
                raise
        finally:
            _ACTIVE_EXECUTION_SCOPE.reset(token)

    def run(self) -> None:
        with self.lock:
            if self.cancellation_event.is_set():
                self.cancelled_before_start = True
                self.completed_at = time.perf_counter()
                self.completed_monotonic = time.monotonic()
                self.done_event.set()
                return
            admission_checked_monotonic = time.monotonic()
            if self.minimum_execution_window_sec is not None:
                remaining_window_sec = max(
                    0.0,
                    self.deadline_monotonic - admission_checked_monotonic,
                )
                self.admission_remaining_window_sec = remaining_window_sec
                if (
                    remaining_window_sec
                    + _EFFECT_ADMISSION_START_TOLERANCE_SEC
                    < self.minimum_execution_window_sec
                ):
                    self.admission_denied_before_start = True
                    # No handler starts, so no late receipt can exist.
                    self.late_completion_observer = None
                    self.completed_at = time.perf_counter()
                    self.completed_monotonic = admission_checked_monotonic
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
        completed_monotonic = time.monotonic()
        with self.lock:
            self.result = result
            self.exception = exception
            self.deadline_exceeded_during_handler = isinstance(
                exception,
                InternalMCPHandlerDeadlineExceeded,
            )
            self.completed_at = completed_at
            self.completed_monotonic = completed_monotonic
            was_late = bool(
                not self.deadline_exceeded_during_handler
                and (
                    self.terminal_returned
                    or completed_monotonic > self.deadline_monotonic
                )
            )
            # Wake the caller before any potentially blocking durable observer.
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
        with task.lock:
            if task.late_completion_recorded:
                return
            task.late_completion_recorded = True
        terminal_at = task.completed_at or time.perf_counter()
        queue_ms, handler_ms, _ = self._timing_snapshot(
            task,
            terminal_at=terminal_at,
        )
        is_observable_write = (
            str(task.category or "").strip().lower() == "write"
            and task.late_completion_observer is not None
        )
        observer_notified = False
        observer_error_type: str | None = None
        if is_observable_write:
            bounded_payload = None
            payload_truncated = False
            error_text = None
            if task.exception is None:
                bounded_payload, payload_truncated = (
                    _bounded_late_completion_value(task.result)
                )
            else:
                bounded_error, payload_truncated = (
                    _bounded_late_completion_value(str(task.exception))
                )
                error_text = str(bounded_error)
            observation = {
                "schema_version": "internal_mcp_late_completion.v1",
                "execution_id": task.execution_id,
                "method_name": task.method_name,
                "category": "write",
                "outcome": (
                    "late_error" if task.exception is not None else "late_success"
                ),
                "observed_at_utc": _utc_now_iso(),
                "queue_duration_ms": queue_ms,
                "handler_duration_ms": handler_ms,
                "payload": bounded_payload,
                "payload_truncated": payload_truncated,
                "error_type": (
                    type(task.exception).__name__
                    if task.exception is not None
                    else None
                ),
                "error": error_text,
                "output_schema_validation": "not_checked",
                "output_schema_valid": None,
                "output_schema_error": None,
            }
            try:
                task.late_completion_observer(observation)
                observer_notified = True
            except BaseException as exc:
                observer_error_type = type(exc).__name__
                logger.exception(
                    "[mcp_transport] late-completion observer failed for %s "
                    "(execution_id=%s)",
                    task.method_name,
                    task.execution_id,
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
                    "payload_discarded": not observer_notified,
                    "observer_notified": observer_notified,
                    "observer_error_type": observer_error_type,
                }
            )
        logger.warning(
            "[mcp_transport] %s late %s for %s "
            "(execution_id=%s, handler=%.2fms)",
            "observed" if observer_notified else "discarded",
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
        late_result_policy: str = "discard_from_turn",
        effect_receipt: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        is_write = str(category or "").strip().lower() == "write"
        outcome_unknown = is_write and timeout_phase == "handler"
        payload: Dict[str, Any] = {
            "success": False,
            "status": "timed_out",
            "error": (
                f"Internal MCP tool '{method_name}' exceeded its "
                f"{timeout_sec:.1f}s hard deadline."
            ),
            "error_code": (
                "tool_timeout_outcome_unknown"
                if outcome_unknown
                else "tool_timeout"
            ),
            "error_type": "deadline_exceeded",
            "retryable": not outcome_unknown,
            "timeout_seconds": timeout_sec,
            "advisory_timeout_seconds": advisory_timeout_sec,
            "timeout_phase": timeout_phase,
            "execution_id": execution_id,
            "queue_duration_ms": queue_duration_ms,
            "handler_elapsed_ms": handler_elapsed_ms,
            "cancellation_requested": True,
            "handler_isolation": "bounded_worker_pool",
            "database_deadline_propagation": "pymongo_csot",
            "outcome_finality": "terminal_for_turn",
            "late_result_policy": late_result_policy,
        }
        durable_instance_id = (
            str((effect_receipt or {}).get("instance_id") or "").strip()
            if isinstance(effect_receipt, Mapping)
            else ""
        )
        if outcome_unknown and durable_instance_id:
            bounded_receipt = dict(effect_receipt or {})
            payload.update(
                {
                    "error_code": "tool_timeout_after_durable_submission",
                    "retryable": False,
                    "effect_status": "partial",
                    "mutation_outcome": "partial",
                    "changed": (
                        bounded_receipt.get("created_new")
                        if isinstance(bounded_receipt.get("created_new"), bool)
                        else None
                    ),
                    "workflow_id": bounded_receipt.get("workflow_id"),
                    "instance_id": durable_instance_id,
                    "created_new": bounded_receipt.get("created_new"),
                    "durable_submission_status": bounded_receipt.get(
                        "durable_submission_status"
                    ),
                    "durable_effect_receipt": bounded_receipt,
                }
            )
            payload["recovery_affordances"] = [
                {
                    "action_type": "inspect_workflow_instance",
                    "capability": "workflow_get_instance",
                    "arguments": {"instance_id": durable_instance_id},
                }
            ]
        elif outcome_unknown:
            payload["mutation_outcome"] = "unknown"
            payload["recovery_affordances"] = [
                {"action_type": "inspect_operation_state_before_retry"}
            ]
        else:
            if is_write:
                payload["mutation_outcome"] = "not_started"
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
        payload: Dict[str, Any] = {
            "success": False,
            "status": "failed",
            "error": "The bounded internal MCP handler pool is saturated.",
            "error_code": "internal_mcp_handler_pool_saturated",
            "error_type": "capacity_exhausted",
            "retryable": True,
            "execution_id": execution_id,
            "timeout_seconds": timeout_sec,
            "advisory_timeout_seconds": advisory_timeout_sec,
            "handler_isolation": "bounded_worker_pool",
            "outcome_finality": "terminal_for_turn",
            "recovery_affordances": [
                {"action_type": "bounded_retry"},
                {"action_type": "choose_alternate_represented_path"},
            ],
        }
        if is_write:
            payload["mutation_outcome"] = "not_started"
        return payload

    @staticmethod
    def _admission_denied_payload(
        *,
        method_name: str,
        execution_id: str,
        category: str,
        configured_hard_timeout_sec: float,
        minimum_window_sec: float,
        effective_window_sec: float,
        remaining_window_sec: float,
        timeout_phase: str,
        queue_duration_ms: float,
    ) -> Dict[str, Any]:
        is_write = str(category or "").strip().lower() == "write"
        payload: Dict[str, Any] = {
            "success": False,
            "status": "not_started",
            "error": (
                f"{method_name!r} was not started because the caller's "
                "remaining window is shorter than its minimum admission "
                "window."
            ),
            "error_code": (
                "insufficient_effect_window"
                if is_write
                else "insufficient_execution_window"
            ),
            "error_type": "admission_denied",
            "retryable": True,
            "execution_id": execution_id,
            "configured_hard_timeout_seconds": configured_hard_timeout_sec,
            "minimum_admission_window_seconds": minimum_window_sec,
            "effective_execution_window_seconds": effective_window_sec,
            "remaining_execution_window_seconds": remaining_window_sec,
            "timeout_phase": timeout_phase,
            "queue_duration_ms": queue_duration_ms,
            "outcome_finality": "terminal_for_turn",
            "recovery_affordances": [
                {
                    "action_type": (
                        "return_bounded_failure_or_retry_in_new_turn"
                    )
                }
            ],
        }
        if is_write:
            payload["mutation_outcome"] = "not_started"
        return payload

    def execute(
        self,
        *,
        method_name: str,
        handler: Callable[..., Any],
        payload: Dict[str, Any],
        timeout_sec: float | None,
        category: str = "read",
        advisory_timeout_sec: float | None = None,
        deadline_monotonic: float | None = None,
        minimum_execution_window_sec: float | None = None,
        log_tag: str = "[mcp_gateway]",
        late_completion_observer: LateCompletionObserver | None = None,
    ) -> TransportResult:
        """Execute a handler within a bounded hard deadline.

        The returned result is immutable for the current turn.  If the handler
        ignores cooperative cancellation and completes late, the terminal
        result remains unchanged.  Reads and calls without an observer discard
        the late payload.  A dispatched write with an observer emits exactly
        one bounded observation out of band after the handler starts.  A write
        cancelled while still queued reports ``not_started`` and cannot promise
        an observation.  A caller may provide an absolute monotonic deadline to
        shorten, but never extend, the method's configured hard timeout.
        """

        configured_hard_timeout_sec = float(
            timeout_sec
            if timeout_sec is not None and float(timeout_sec) > 0.0
            else (
                self._write_timeout_sec
                if str(category or "").strip().lower() == "write"
                else self._read_timeout_sec
            )
        )
        minimum_window_sec: float | None = None
        if minimum_execution_window_sec is not None:
            minimum_window_sec = float(minimum_execution_window_sec)
            if minimum_window_sec <= 0.0:
                raise ValueError("minimum_execution_window_sec must be positive")
            if minimum_window_sec > configured_hard_timeout_sec:
                raise ValueError(
                    "minimum_execution_window_sec cannot exceed the configured "
                    "hard timeout"
                )
        submitted_at = time.perf_counter()
        submitted_monotonic = time.monotonic()
        hard_timeout_sec = configured_hard_timeout_sec
        if deadline_monotonic is not None:
            hard_timeout_sec = min(
                configured_hard_timeout_sec,
                max(0.0, float(deadline_monotonic) - submitted_monotonic),
            )
        advisory_sec = float(
            advisory_timeout_sec
            if advisory_timeout_sec is not None and advisory_timeout_sec > 0.0
            else self.advisory_timeout_sec(
                category,
                hard_timeout_sec=configured_hard_timeout_sec,
            )
        )
        advisory_sec = min(advisory_sec, hard_timeout_sec)
        execution_id = f"mcp_{uuid.uuid4().hex}"
        handler_deadline_monotonic = submitted_monotonic + hard_timeout_sec
        observe_late_write = (
            str(category or "").strip().lower() == "write"
            and late_completion_observer is not None
        )
        dispatched_late_result_policy = (
            "observe_out_of_band"
            if observe_late_write
            else "discard_from_turn"
        )

        if (
            minimum_window_sec is not None
            and hard_timeout_sec < minimum_window_sec
        ):
            payload = self._admission_denied_payload(
                method_name=method_name,
                execution_id=execution_id,
                category=category,
                configured_hard_timeout_sec=configured_hard_timeout_sec,
                minimum_window_sec=minimum_window_sec,
                effective_window_sec=hard_timeout_sec,
                remaining_window_sec=hard_timeout_sec,
                timeout_phase="pre_dispatch",
                queue_duration_ms=0.0,
            )
            return TransportResult(
                payload=payload,
                duration_ms=max(0.0, (time.perf_counter() - submitted_at) * 1000.0),
                execution_id=execution_id,
                outcome="not_started",
                timeout_sec=hard_timeout_sec,
                advisory_timeout_sec=advisory_sec,
                queue_duration_ms=0.0,
                handler_duration_ms=None,
                handler_elapsed_ms=None,
                transport_overhead_ms=0.0,
                timeout_phase="pre_dispatch",
                configured_hard_timeout_sec=configured_hard_timeout_sec,
                minimum_execution_window_sec=minimum_window_sec,
            )

        if hard_timeout_sec <= 0.0:
            with self._diagnostics_lock:
                self._timeout_count += 1
            timeout_payload = self._timeout_payload(
                method_name=method_name,
                execution_id=execution_id,
                category=category,
                timeout_sec=hard_timeout_sec,
                advisory_timeout_sec=advisory_sec,
                timeout_phase="pre_dispatch",
                queue_duration_ms=0.0,
                handler_elapsed_ms=None,
            )
            return TransportResult(
                payload=timeout_payload,
                duration_ms=max(0.0, (time.perf_counter() - submitted_at) * 1000.0),
                execution_id=execution_id,
                outcome="timed_out",
                timeout_sec=hard_timeout_sec,
                advisory_timeout_sec=advisory_sec,
                advisory_budget_exceeded=True,
                queue_duration_ms=0.0,
                handler_duration_ms=None,
                handler_elapsed_ms=None,
                transport_overhead_ms=0.0,
                timeout_phase="pre_dispatch",
                configured_hard_timeout_sec=configured_hard_timeout_sec,
                minimum_execution_window_sec=minimum_window_sec,
            )

        task = _HandlerTask(
            execution_id=execution_id,
            method_name=method_name,
            context=copy_context(),
            handler=handler,
            payload=dict(payload),
            deadline_monotonic=handler_deadline_monotonic,
            submitted_at=submitted_at,
            on_late_completion=self._record_late_completion,
            category=category,
            minimum_execution_window_sec=minimum_window_sec,
            late_completion_observer=(
                late_completion_observer if observe_late_write else None
            ),
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
                configured_hard_timeout_sec=configured_hard_timeout_sec,
                minimum_execution_window_sec=minimum_window_sec,
            )

        completed = task.done_event.wait(timeout=hard_timeout_sec)
        terminal_at = time.perf_counter()
        with task.lock:
            completed_within_deadline = bool(
                (completed or task.done_event.is_set())
                and task.completed_monotonic is not None
                and task.completed_monotonic <= handler_deadline_monotonic
                and not task.deadline_exceeded_during_handler
            )
            if completed_within_deadline:
                task_completed = True
            else:
                task.terminal_returned = True
                task.cancellation_event.set()
                if task.started_at is None:
                    # A queued task will observe cancellation before invoking
                    # its handler.  Remove the observer while holding the task
                    # lock so no future worker path can imply otherwise.
                    task.late_completion_observer = None
                task_completed = False
            result = task.result
            exception = task.exception
            started_at = task.started_at
            admission_denied_before_start = (
                task.admission_denied_before_start
            )
            admission_remaining_window_sec = (
                task.admission_remaining_window_sec
            )
            effect_receipt = (
                dict(task.effect_receipt)
                if isinstance(task.effect_receipt, Mapping)
                else None
            )

        queue_ms, handler_ms, handler_elapsed_ms = self._timing_snapshot(
            task,
            terminal_at=terminal_at,
        )
        duration_ms = max(0.0, (terminal_at - submitted_at) * 1000.0)

        if task_completed and admission_denied_before_start:
            remaining_window_sec = max(
                0.0,
                float(admission_remaining_window_sec or 0.0),
            )
            admission_payload = self._admission_denied_payload(
                method_name=method_name,
                execution_id=execution_id,
                category=category,
                configured_hard_timeout_sec=configured_hard_timeout_sec,
                minimum_window_sec=float(minimum_window_sec or 0.0),
                effective_window_sec=hard_timeout_sec,
                remaining_window_sec=remaining_window_sec,
                timeout_phase="queue",
                queue_duration_ms=queue_ms,
            )
            advisory_exceeded = duration_ms > advisory_sec * 1000.0
            logger.info(
                "%s denied queued admission for %s after %.2fms "
                "(remaining=%.3fs, minimum=%.3fs, execution_id=%s)",
                log_tag,
                method_name,
                duration_ms,
                remaining_window_sec,
                minimum_window_sec,
                execution_id,
            )
            return TransportResult(
                payload=admission_payload,
                duration_ms=duration_ms,
                execution_id=execution_id,
                outcome="not_started",
                timeout_sec=hard_timeout_sec,
                advisory_timeout_sec=advisory_sec,
                advisory_budget_exceeded=advisory_exceeded,
                queue_duration_ms=queue_ms,
                handler_duration_ms=None,
                handler_elapsed_ms=None,
                transport_overhead_ms=max(0.0, duration_ms - queue_ms),
                timeout_phase="queue",
                late_result_policy="discard_from_turn",
                configured_hard_timeout_sec=configured_hard_timeout_sec,
                minimum_execution_window_sec=minimum_window_sec,
            )

        if not task_completed:
            with self._diagnostics_lock:
                self._timeout_count += 1
            timeout_phase = "handler" if started_at is not None else "queue"
            late_result_policy = (
                dispatched_late_result_policy
                if timeout_phase == "handler"
                else "discard_from_turn"
            )
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
                late_result_policy=late_result_policy,
                effect_receipt=effect_receipt,
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
                late_result_policy=late_result_policy,
                configured_hard_timeout_sec=configured_hard_timeout_sec,
                minimum_execution_window_sec=minimum_window_sec,
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
            configured_hard_timeout_sec=configured_hard_timeout_sec,
            minimum_execution_window_sec=minimum_window_sec,
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
                "database_deadline_propagation": "pymongo_csot",
            }
        local["handler_pool"] = self._executor.diagnostics()
        return local
