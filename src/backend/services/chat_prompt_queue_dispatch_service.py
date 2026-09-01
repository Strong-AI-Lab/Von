"""Server-owned dispatch for durable queued conversation turns.

The Mongo queue owns FIFO, idempotency and the durable conversation fence.
This module deliberately owns only the small, one-process scheduling loop that
turns an eligible queue reservation into a background ``/von/generate``
submission.  Queue rows remain the source of truth across browser and process
lifetime; the in-process worker is replaceable and may safely restart.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..security.access_control import override_current_actor
from . import chat_prompt_queue_service, task_execution_service
from .conversation_turn_admission_service import SERVER_INSTANCE_ID

_logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 1.0
_DEFAULT_RETRY_AFTER_SECONDS = 1.0
_TERMINAL_RETENTION_BACKFILL_BATCH_SIZE = 500


def _positive_float_env(name: str, default: float) -> float:
    try:
        return max(0.05, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ChatPromptDispatchOutcome:
    """Result of submitting one reserved queue row to the execution surface."""

    accepted: bool
    retryable: bool = False
    retry_after_seconds: float = _DEFAULT_RETRY_AFTER_SECONDS
    error: str | None = None


def reconcile_linked_task_execution_terminal(
    record: Mapping[str, Any],
    *,
    status: str,
    source: str,
    error: str | None = None,
) -> dict[str, Any] | None:
    """Apply one exact queue terminal outcome to its internal TaskExecution.

    The queue transition must already have succeeded.  Rows without task links
    remain ordinary conversation work; partially linked rows are rejected
    rather than guessed from an execution envelope.
    """

    queue_id = str(record.get("queue_id") or "").strip()
    if not queue_id:
        return None
    claimed = chat_prompt_queue_service.claim_task_execution_terminal_reconciliation(
        queue_id=queue_id,
        server_instance_id=SERVER_INSTANCE_ID,
    )
    if claimed is None:
        return None
    persisted_status = str(
        claimed.get("task_execution_reconciliation_queue_status") or ""
    ).strip()
    if persisted_status != status:
        chat_prompt_queue_service.release_task_execution_terminal_reconciliation(
            queue_id=queue_id,
            reconciliation_token=str(
                claimed.get("task_execution_reconciliation_token") or ""
            ),
            server_instance_id=SERVER_INSTANCE_ID,
            error=(
                "terminal reconciliation status changed before projection: "
                f"expected {status}, found {persisted_status or 'missing'}"
            ),
        )
        raise RuntimeError("terminal queue status changed before reconciliation")
    try:
        return _reconcile_claimed_task_execution_terminal(
            claimed,
            source=source,
            error=error,
        )
    except Exception as exc:
        chat_prompt_queue_service.release_task_execution_terminal_reconciliation(
            queue_id=queue_id,
            reconciliation_token=str(
                claimed.get("task_execution_reconciliation_token") or ""
            ),
            server_instance_id=SERVER_INSTANCE_ID,
            error=f"{type(exc).__name__}: {exc}"[:4_000],
        )
        raise


def _reconcile_claimed_task_execution_terminal(
    record: Mapping[str, Any],
    *,
    source: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Project and acknowledge one already-claimed durable terminal marker."""

    task_id = str(record.get("task_concept_id") or "").strip()
    execution_id = str(record.get("task_execution_concept_id") or "").strip()
    if not task_id and not execution_id:
        raise RuntimeError("claimed terminal reconciliation has no task link")
    link = {
        "task_concept_id": task_id,
        "task_execution_concept_id": execution_id,
        "enqueue_submission_id": str(record.get("enqueue_submission_id") or "").strip(),
        "queue_id": str(record.get("queue_id") or "").strip(),
        "actor_concept_id": str(record.get("user_concept_id") or "").strip(),
        "organisation_concept_id": str(
            record.get("organisation_concept_id") or ""
        ).strip(),
    }
    missing = [key for key, value in link.items() if not value]
    if missing:
        raise RuntimeError(
            "linked queue terminal record is incomplete: " + ", ".join(missing)
        )
    with override_current_actor(
        link["actor_concept_id"], link["organisation_concept_id"]
    ):
        execution = task_execution_service.get_task_execution(
            execution_id,
            actor_concept_id=link["actor_concept_id"],
            organisation_concept_id=link["organisation_concept_id"],
        )
    for field in (
        "task_concept_id",
        "enqueue_submission_id",
        "queue_id",
    ):
        if execution.get(field) != link[field]:
            raise task_execution_service.TaskExecutionAccessError(
                "task_execution_queue_link_mismatch",
                f"The terminal queue record {field} does not match the task execution",
            )
    queue_status = str(
        record.get("task_execution_reconciliation_queue_status")
        or record.get("status")
        or ""
    ).strip()
    target_status = {
        chat_prompt_queue_service.STATUS_COMPLETED: (
            task_execution_service.TASK_EXECUTION_STATUS_COMPLETED
        ),
        chat_prompt_queue_service.STATUS_FAILED: (
            task_execution_service.TASK_EXECUTION_STATUS_FAILED
        ),
        chat_prompt_queue_service.STATUS_CANCELLED: (
            task_execution_service.TASK_EXECUTION_STATUS_CANCELLED
        ),
    }.get(queue_status)
    if target_status is None:
        raise ValueError("linked TaskExecution requires a terminal queue status")
    result_error = (
        error
        if error is not None
        else record.get("task_execution_reconciliation_error")
    )
    source_clean = str(
        source
        or record.get("task_execution_reconciliation_source")
        or "queue_terminal_reconciliation"
    ).strip()
    try:
        with override_current_actor(
            link["actor_concept_id"], link["organisation_concept_id"]
        ):
            transitioned = task_execution_service.transition_task_execution(
                execution_id,
                actor_concept_id=link["actor_concept_id"],
                organisation_concept_id=link["organisation_concept_id"],
                enqueue_submission_id=link["enqueue_submission_id"],
                queue_id=link["queue_id"],
                status=target_status,
                result=(
                    None
                    if target_status
                    == task_execution_service.TASK_EXECUTION_STATUS_COMPLETED
                    else (str(result_error) if result_error is not None else None)
                ),
                source=source_clean,
            )
    except task_execution_service.TaskExecutionTransitionError:
        # Queue dismissal may turn an already-failed row into cancelled.  The
        # execution's first terminal fact remains the honest attempt outcome.
        with override_current_actor(
            link["actor_concept_id"], link["organisation_concept_id"]
        ):
            refreshed = task_execution_service.get_task_execution(
                execution_id,
                actor_concept_id=link["actor_concept_id"],
                organisation_concept_id=link["organisation_concept_id"],
            )
        if (
            refreshed.get("status")
            in task_execution_service.TASK_EXECUTION_TERMINAL_STATUSES
        ):
            transitioned = refreshed
        else:
            raise
    if transitioned.get("status") not in (
        target_status,
        task_execution_service.TASK_EXECUTION_STATUS_FAILED
        if queue_status == chat_prompt_queue_service.STATUS_CANCELLED
        else target_status,
    ):
        raise task_execution_service.TaskExecutionTransitionError(
            "TaskExecution terminal read-back does not match the queue outcome"
        )
    acknowledged = (
        chat_prompt_queue_service.acknowledge_task_execution_terminal_reconciliation(
            queue_id=link["queue_id"],
            reconciliation_token=str(
                record.get("task_execution_reconciliation_token") or ""
            ),
            server_instance_id=SERVER_INSTANCE_ID,
            task_execution_concept_id=execution_id,
            enqueue_submission_id=link["enqueue_submission_id"],
            queue_status=queue_status,
        )
    )
    if not acknowledged:
        raise RuntimeError(
            "TaskExecution terminal projection succeeded but queue acknowledgement "
            "did not match its exact reconciliation claim"
        )
    return transitioned


class ChatPromptQueueDispatcher:
    """Bounded server worker for safe, unclaimed queue entries.

    The callback must merely submit the turn to the existing background
    generate path.  Admission then upgrades the exact reservation and owns the
    lease/terminal lifecycle.  An accepted submission is therefore not a claim
    that the user task or even the turn has completed.
    """

    def __init__(
        self,
        *,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self._poll_interval_seconds = (
            float(poll_interval_seconds)
            if poll_interval_seconds is not None
            else _positive_float_env(
                "VON_CHAT_PROMPT_DISPATCH_POLL_SECONDS",
                _DEFAULT_POLL_INTERVAL_SECONDS,
            )
        )
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._app: Any | None = None
        self._submit: (
            Callable[
                [Any, Mapping[str, Any]], ChatPromptDispatchOutcome | Mapping[str, Any]
            ]
            | None
        ) = None
        self._last_error: str | None = None
        self._accepted_count = 0
        self._retry_count = 0
        self._failed_count = 0
        self._handoff_reconciled_count = 0
        self._handoff_reconciliation_failed_count = 0
        self._task_launch_reconciled_count = 0
        self._task_launch_reconciliation_retry_count = 0
        self._task_launch_reconciliation_failed_count = 0
        self._task_execution_reconciled_count = 0
        self._task_execution_reconciliation_retry_count = 0
        self._linked_terminal_backfilled_count = 0
        self._linked_terminal_backfill_complete = False
        self._terminal_retention_backfilled_count = 0
        self._terminal_retention_backfill_complete = False

    def configure(
        self,
        *,
        app: Any,
        submit: Callable[
            [Any, Mapping[str, Any]], ChatPromptDispatchOutcome | Mapping[str, Any]
        ],
    ) -> None:
        if app is None or not callable(submit):
            raise ValueError("dispatcher app and submit callback are required")
        with self._lock:
            self._app = app
            self._submit = submit

    @staticmethod
    def _normalise_outcome(
        value: ChatPromptDispatchOutcome | Mapping[str, Any],
    ) -> ChatPromptDispatchOutcome:
        if isinstance(value, ChatPromptDispatchOutcome):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("dispatch callback returned an invalid outcome")
        return ChatPromptDispatchOutcome(
            accepted=bool(value.get("accepted")),
            retryable=bool(value.get("retryable")),
            retry_after_seconds=max(
                0.0,
                float(value.get("retry_after_seconds") or _DEFAULT_RETRY_AFTER_SECONDS),
            ),
            error=(
                str(value.get("error"))[:4_000]
                if value.get("error") is not None
                else None
            ),
        )

    def run_once(self) -> bool:
        """Reserve and submit at most one row; return whether work was found."""

        with self._lock:
            app = self._app
            submit = self._submit
        if app is None or submit is None:
            raise RuntimeError("chat prompt dispatcher is not configured")

        if self._reconcile_one_linked_terminal():
            return True
        if self._reconcile_one_task_launch():
            return True
        if self._reconcile_one_handoff():
            return True

        record = chat_prompt_queue_service.reserve_next_server_dispatch(
            server_instance_id=SERVER_INSTANCE_ID,
        )
        if record is None:
            return False

        queue_id = str(record.get("queue_id") or "").strip()
        token = str(record.get("dispatch_reservation_token") or "").strip()
        if not queue_id or not token:
            self._last_error = "reserved row omitted its exact dispatch identity"
            _logger.error("[chat_prompt_dispatch] %s", self._last_error)
            return True

        try:
            if record.get("task_execution_concept_id"):
                # Re-read task authority immediately before model submission;
                # assignment or cancellation may have changed while queued.
                task_execution_service.reconcile_task_execution_queue_record(record)
            outcome = self._normalise_outcome(submit(app, record))
        except (
            task_execution_service.InvalidTaskExecutionData,
            task_execution_service.TaskExecutionAccessError,
            task_execution_service.TaskExecutionTransitionError,
        ) as exc:
            outcome = ChatPromptDispatchOutcome(
                accepted=False,
                retryable=False,
                error=f"task launch is no longer executable: {exc}",
            )
        except Exception as exc:  # worker must survive one bad submission
            outcome = ChatPromptDispatchOutcome(
                accepted=False,
                retryable=True,
                retry_after_seconds=_DEFAULT_RETRY_AFTER_SECONDS,
                error=f"dispatch submission raised {type(exc).__name__}: {exc}",
            )
            _logger.exception(
                "[chat_prompt_dispatch] Submission failed for queue_id=%s",
                queue_id,
            )

        if outcome.accepted:
            with self._lock:
                self._accepted_count += 1
                self._last_error = None
            return True

        error = outcome.error or "server dispatch was not accepted"
        if outcome.retryable:
            released = chat_prompt_queue_service.release_server_dispatch_reservation(
                queue_id=queue_id,
                dispatch_reservation_token=token,
                server_instance_id=SERVER_INSTANCE_ID,
                retry_after_seconds=outcome.retry_after_seconds,
                error=error,
            )
            with self._lock:
                self._retry_count += 1
                self._last_error = error
            if not released:
                _logger.info(
                    "[chat_prompt_dispatch] Reservation changed before retry "
                    "queue_id=%s",
                    queue_id,
                )
            return True

        failed = chat_prompt_queue_service.fail_server_dispatch_reservation(
            queue_id=queue_id,
            dispatch_reservation_token=token,
            server_instance_id=SERVER_INSTANCE_ID,
            error=error,
        )
        with self._lock:
            self._failed_count += 1
            self._last_error = error
        if not failed:
            _logger.info(
                "[chat_prompt_dispatch] Reservation changed before failure queue_id=%s",
                queue_id,
            )
        else:
            reconcile_linked_task_execution_terminal(
                record,
                status=chat_prompt_queue_service.STATUS_FAILED,
                source="queue_dispatcher",
                error=error,
            )
        return True

    def _reconcile_one_handoff(self) -> bool:
        """Retire and activate at most one durable legacy queue handoff."""

        try:
            record = chat_prompt_queue_service.reconcile_next_server_dispatch_handoff()
        except Exception as exc:  # noqa: BLE001 - the durable row remains retryable
            with self._lock:
                self._handoff_reconciliation_failed_count += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
            _logger.warning(
                "[chat_prompt_dispatch] Queue handoff reconciliation deferred: %s",
                exc,
            )
            # The service has durably scheduled the exact handoff retry. Let
            # this pass continue to unrelated dispatch-ready rows instead of
            # reporting the blocked handoff as completed work.
            return False
        if record is None:
            return False
        with self._lock:
            self._handoff_reconciled_count += 1
            self._last_error = None
        return True

    def _reconcile_one_task_launch(self) -> bool:
        """Project and activate at most one durable task launch outbox."""

        record = chat_prompt_queue_service.claim_task_execution_launch_reconciliation(
            server_instance_id=SERVER_INSTANCE_ID,
        )
        if record is None:
            return False
        queue_id = str(record.get("queue_id") or "").strip()
        token = str(record.get("task_launch_reconciliation_token") or "").strip()
        try:
            execution = task_execution_service.reconcile_task_execution_queue_record(
                record
            )
            expected_execution_id = str(
                record.get("task_execution_concept_id") or ""
            ).strip()
            if (
                execution.get("task_execution_concept_id") != expected_execution_id
                or execution.get("queue_id") != queue_id
                or execution.get("enqueue_submission_id")
                != record.get("enqueue_submission_id")
            ):
                raise task_execution_service.TaskExecutionTransitionError(
                    "TaskExecution launch read-back does not match its queue outbox"
                )
            acknowledged = chat_prompt_queue_service.acknowledge_task_execution_launch_reconciliation(
                queue_id=queue_id,
                reconciliation_token=token,
                server_instance_id=SERVER_INSTANCE_ID,
                task_execution_concept_id=expected_execution_id,
                enqueue_submission_id=str(record.get("enqueue_submission_id") or ""),
            )
            if not acknowledged:
                raise RuntimeError(
                    "TaskExecution launch projection succeeded but queue activation "
                    "did not match its exact reconciliation claim"
                )
        except (
            task_execution_service.InvalidTaskExecutionData,
            task_execution_service.TaskExecutionAccessError,
            task_execution_service.TaskExecutionTransitionError,
        ) as exc:
            failed = (
                chat_prompt_queue_service.fail_task_execution_launch_reconciliation(
                    queue_id=queue_id,
                    reconciliation_token=token,
                    server_instance_id=SERVER_INSTANCE_ID,
                    error=f"{type(exc).__name__}: {exc}"[:4_000],
                )
            )
            with self._lock:
                self._task_launch_reconciliation_failed_count += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
            if not failed:
                _logger.info(
                    "[chat_prompt_dispatch] Task launch reconciliation claim "
                    "changed before permanent failure queue_id=%s",
                    queue_id,
                )
            return True
        except Exception as exc:  # noqa: BLE001 - durable launch remains retryable
            released = (
                chat_prompt_queue_service.release_task_execution_launch_reconciliation(
                    queue_id=queue_id,
                    reconciliation_token=token,
                    server_instance_id=SERVER_INSTANCE_ID,
                    error=f"{type(exc).__name__}: {exc}"[:4_000],
                )
            )
            with self._lock:
                self._task_launch_reconciliation_retry_count += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
            if not released:
                _logger.info(
                    "[chat_prompt_dispatch] Task launch reconciliation claim "
                    "changed before retry queue_id=%s",
                    queue_id,
                )
            return True
        with self._lock:
            self._task_launch_reconciled_count += 1
            self._last_error = None
        return True

    def _reconcile_one_linked_terminal(self) -> bool:
        """Retry at most one durable cross-store terminal projection."""

        record = chat_prompt_queue_service.claim_task_execution_terminal_reconciliation(
            server_instance_id=SERVER_INSTANCE_ID,
        )
        if record is None:
            return False
        queue_id = str(record.get("queue_id") or "").strip()
        token = str(record.get("task_execution_reconciliation_token") or "").strip()
        try:
            _reconcile_claimed_task_execution_terminal(record)
        except Exception as exc:  # noqa: BLE001 - durable marker remains retryable
            released = chat_prompt_queue_service.release_task_execution_terminal_reconciliation(
                queue_id=queue_id,
                reconciliation_token=token,
                server_instance_id=SERVER_INSTANCE_ID,
                error=f"{type(exc).__name__}: {exc}"[:4_000],
            )
            with self._lock:
                self._task_execution_reconciliation_retry_count += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
            if not released:
                _logger.info(
                    "[chat_prompt_dispatch] Terminal reconciliation claim changed "
                    "before retry queue_id=%s",
                    queue_id,
                )
            return True
        with self._lock:
            self._task_execution_reconciled_count += 1
            self._last_error = None
        return True

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._linked_terminal_backfill_complete:
                    backfilled = chat_prompt_queue_service.backfill_linked_terminal_reconciliation(
                        batch_size=_TERMINAL_RETENTION_BACKFILL_BATCH_SIZE,
                    )
                    with self._lock:
                        self._linked_terminal_backfilled_count += backfilled
                        self._linked_terminal_backfill_complete = (
                            backfilled < _TERMINAL_RETENTION_BACKFILL_BATCH_SIZE
                        )
                if not self._terminal_retention_backfill_complete:
                    backfilled = (
                        chat_prompt_queue_service.backfill_legacy_terminal_purge_after(
                            batch_size=_TERMINAL_RETENTION_BACKFILL_BATCH_SIZE,
                        )
                    )
                    with self._lock:
                        self._terminal_retention_backfilled_count += backfilled
                        self._terminal_retention_backfill_complete = (
                            backfilled < _TERMINAL_RETENTION_BACKFILL_BATCH_SIZE
                        )
                found = self.run_once()
                if found:
                    # Continue immediately so free conversations may dispatch
                    # concurrently through the bounded background registry.
                    continue
            except Exception as exc:  # noqa: BLE001 - worker survives store outages
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                _logger.warning(
                    "[chat_prompt_dispatch] Worker iteration deferred: %s",
                    exc,
                )
            self._wake.wait(timeout=self._poll_interval_seconds)
            self._wake.clear()

    def start(self) -> threading.Thread:
        with self._lock:
            if self._app is None or self._submit is None:
                raise RuntimeError("chat prompt dispatcher is not configured")
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="chat-prompt-queue-dispatcher",
                daemon=True,
            )
            self._thread.start()
            return self._thread

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def wake(self) -> None:
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            last_error_sha256 = (
                hashlib.sha256(self._last_error.encode("utf-8")).hexdigest()
                if self._last_error
                else None
            )
            return {
                "schema_version": "chat_prompt_queue_dispatcher.v1",
                "server_instance_id": SERVER_INSTANCE_ID,
                "configured": self._app is not None and self._submit is not None,
                "running": bool(self._thread and self._thread.is_alive()),
                "accepted_count": self._accepted_count,
                "retry_count": self._retry_count,
                "failed_count": self._failed_count,
                "handoff_reconciled_count": self._handoff_reconciled_count,
                "handoff_reconciliation_failed_count": (
                    self._handoff_reconciliation_failed_count
                ),
                "task_launch_reconciled_count": self._task_launch_reconciled_count,
                "task_launch_reconciliation_retry_count": (
                    self._task_launch_reconciliation_retry_count
                ),
                "task_launch_reconciliation_failed_count": (
                    self._task_launch_reconciliation_failed_count
                ),
                "task_execution_reconciled_count": (
                    self._task_execution_reconciled_count
                ),
                "task_execution_reconciliation_retry_count": (
                    self._task_execution_reconciliation_retry_count
                ),
                "linked_terminal_backfilled_count": (
                    self._linked_terminal_backfilled_count
                ),
                "linked_terminal_backfill_complete": (
                    self._linked_terminal_backfill_complete
                ),
                "terminal_retention_backfilled_count": (
                    self._terminal_retention_backfilled_count
                ),
                "terminal_retention_backfill_complete": (
                    self._terminal_retention_backfill_complete
                ),
                "last_error_present": self._last_error is not None,
                "last_error_sha256": last_error_sha256,
            }


chat_prompt_queue_dispatcher = ChatPromptQueueDispatcher()


def configure_chat_prompt_queue_dispatcher(
    *,
    app: Any,
    submit: Callable[
        [Any, Mapping[str, Any]], ChatPromptDispatchOutcome | Mapping[str, Any]
    ],
) -> None:
    chat_prompt_queue_dispatcher.configure(app=app, submit=submit)


def start_chat_prompt_queue_dispatcher() -> threading.Thread:
    return chat_prompt_queue_dispatcher.start()


def stop_chat_prompt_queue_dispatcher(*, timeout: float = 5.0) -> None:
    chat_prompt_queue_dispatcher.stop(timeout=timeout)


def wake_chat_prompt_queue_dispatcher() -> None:
    chat_prompt_queue_dispatcher.wake()


def get_chat_prompt_queue_dispatcher_snapshot() -> dict[str, Any]:
    return chat_prompt_queue_dispatcher.snapshot()


__all__ = [
    "ChatPromptDispatchOutcome",
    "ChatPromptQueueDispatcher",
    "chat_prompt_queue_dispatcher",
    "configure_chat_prompt_queue_dispatcher",
    "get_chat_prompt_queue_dispatcher_snapshot",
    "reconcile_linked_task_execution_terminal",
    "start_chat_prompt_queue_dispatcher",
    "stop_chat_prompt_queue_dispatcher",
    "wake_chat_prompt_queue_dispatcher",
]
