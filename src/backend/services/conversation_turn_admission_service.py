"""Bounded admission and durable single-flight for conversation turns.

The persisted chat prompt queue owns correlation and the cross-process
conversation fence. Small in-process counters bound the current server's
global and per-actor live work without turning unrelated conversations into a
single FIFO. They are deliberately advisory to a future multi-process
dispatcher; the durable unique conversation key remains the correctness fence.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import chat_prompt_queue_service, task_execution_service

_logger = logging.getLogger(__name__)

_TERMINALISATION_RETRY_INITIAL_DELAY_SEC = 0.5
_TERMINALISATION_RETRY_MAX_DELAY_SEC = 30.0
_DEFAULT_LEASE_HEARTBEAT_INTERVAL_SEC = 30.0
_DEFAULT_WAITRESS_THREADS = 32
_DEFAULT_HTTP_HEADROOM_THREADS = 8


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _configured_waitress_threads() -> int:
    return _positive_int_env("VON_WAITRESS_THREADS", _DEFAULT_WAITRESS_THREADS)


def _default_global_turn_limit() -> int:
    """Leave request threads available for health, progress, and cancellation."""

    waitress_threads = _configured_waitress_threads()
    requested_headroom = _positive_int_env(
        "VON_TURN_HTTP_HEADROOM_THREADS",
        _DEFAULT_HTTP_HEADROOM_THREADS,
    )
    effective_headroom = min(requested_headroom, max(0, waitress_threads - 1))
    return max(1, waitress_threads - effective_headroom)


SERVER_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


class ConversationTurnAdmissionError(RuntimeError):
    """Base class for typed, retryable turn-admission failures."""

    error_code = "conversation_turn_admission_failed"
    status_code = 503

    def __init__(self, message: str, *, queue_id: str | None = None) -> None:
        super().__init__(message)
        self.queue_id = queue_id


class ConversationTurnActive(ConversationTurnAdmissionError):
    """Another request is already executing against this conversation."""

    error_code = "conversation_turn_active"
    status_code = 409


class ConversationTurnCapacityReached(ConversationTurnAdmissionError):
    """The bounded server or actor capacity is currently exhausted."""

    error_code = "turn_capacity_reached"
    status_code = 429


@dataclass
class ConversationTurnAdmissionToken:
    """Exact admission handle; release is idempotent through the service."""

    token_id: str
    queue_id: str | None
    attempt_id: str | None
    user_concept_id: str
    organisation_concept_id: str | None
    namespace: str | None
    conversation_key: str
    client_request_id: str
    session_id: str | None = None
    enqueue_submission_id: str | None = None
    task_concept_id: str | None = None
    task_execution_concept_id: str | None = None
    released: bool = False
    pending_terminal_status: str | None = None
    pending_terminal_error: str | None = None

    @property
    def scope(self) -> dict[str, str | None]:
        return {
            "user_concept_id": self.user_concept_id,
            "organisation_concept_id": self.organisation_concept_id,
            "namespace": self.namespace,
        }


@dataclass
class _TerminalisationRetry:
    """One exact terminal disposition awaiting durable reconciliation."""

    token: ConversationTurnAdmissionToken
    status: str
    error: str | None
    failure_count: int
    retry_at_monotonic: float


def build_conversation_key(
    *,
    owner_user_id: str,
    history_namespace: str,
    conversation_session_id: str,
) -> str:
    """Return a content-free stable key for one canonical conversation carrier."""

    material = "\x1f".join(
        (
            str(owner_user_id or "").strip(),
            str(history_namespace or "").strip(),
            str(conversation_session_id or "").strip(),
        )
    )
    if not all(material_part for material_part in material.split("\x1f")):
        raise ValueError("complete canonical conversation identity is required")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ConversationTurnAdmissionService:
    """Coordinate bounded live turns while preserving cross-conversation parallelism."""

    def __init__(
        self,
        *,
        per_user_limit: int | None = None,
        global_limit: int | None = None,
        terminalisation_retry_initial_delay_sec: float = (
            _TERMINALISATION_RETRY_INITIAL_DELAY_SEC
        ),
        terminalisation_retry_max_delay_sec: float = (
            _TERMINALISATION_RETRY_MAX_DELAY_SEC
        ),
    ) -> None:
        self._per_user_limit = per_user_limit or _positive_int_env(
            "VON_MAX_ACTIVE_TURNS_PER_USER", 4
        )
        self._configured_http_threads = _configured_waitress_threads()
        self._global_limit = global_limit or _positive_int_env(
            "VON_MAX_ACTIVE_TURNS_GLOBAL", _default_global_turn_limit()
        )
        self._lock = threading.RLock()
        self._active_tokens: dict[str, ConversationTurnAdmissionToken] = {}
        self._active_by_user: dict[str, int] = {}
        self._ephemeral_conversation_tokens: dict[str, str] = {}
        self._reserved_global = 0
        self._terminalisation_retry_initial_delay_sec = max(
            0.01,
            float(terminalisation_retry_initial_delay_sec),
        )
        self._terminalisation_retry_max_delay_sec = max(
            self._terminalisation_retry_initial_delay_sec,
            float(terminalisation_retry_max_delay_sec),
        )
        self._terminalisation_retries: dict[str, _TerminalisationRetry] = {}
        self._terminalisation_retry_failure_counts: dict[str, int] = {}
        self._terminalisation_retry_condition = threading.Condition(self._lock)
        self._terminalisation_retry_thread: threading.Thread | None = None
        self._terminalisation_retry_shutdown = False
        try:
            heartbeat_interval = float(
                os.getenv(
                    "VON_TURN_LEASE_HEARTBEAT_SECONDS",
                    str(_DEFAULT_LEASE_HEARTBEAT_INTERVAL_SEC),
                )
            )
        except (TypeError, ValueError):
            heartbeat_interval = _DEFAULT_LEASE_HEARTBEAT_INTERVAL_SEC
        self._lease_heartbeat_interval_sec = max(0.1, heartbeat_interval)
        self._lease_heartbeat_stop = threading.Event()
        self._lease_heartbeat_thread: threading.Thread | None = None

    def _ensure_lease_heartbeat_thread_locked(self) -> None:
        thread = self._lease_heartbeat_thread
        if thread is not None and thread.is_alive():
            return
        self._lease_heartbeat_stop.clear()
        thread = threading.Thread(
            target=self._lease_heartbeat_loop,
            name="conversation_turn_lease_heartbeat",
            daemon=True,
        )
        self._lease_heartbeat_thread = thread
        thread.start()

    def _lease_heartbeat_loop(self) -> None:
        while not self._lease_heartbeat_stop.wait(
            timeout=self._lease_heartbeat_interval_sec
        ):
            with self._lock:
                tokens = [
                    token
                    for token in self._active_tokens.values()
                    if token.queue_id and token.attempt_id and not token.released
                ]
            for token in tokens:
                try:
                    refreshed = chat_prompt_queue_service.heartbeat_bound_turn(
                        scope=token.scope,
                        queue_id=str(token.queue_id),
                        attempt_id=str(token.attempt_id),
                        server_instance_id=SERVER_INSTANCE_ID,
                    )
                    if not refreshed:
                        _logger.warning(
                            "Turn lease heartbeat no longer matched queue_id=%s "
                            "attempt_id=%s",
                            token.queue_id,
                            token.attempt_id,
                        )
                except Exception as exc:  # lease outage must not kill execution
                    _logger.warning(
                        "Turn lease heartbeat deferred for queue_id=%s: %s",
                        token.queue_id,
                        exc,
                    )

    def _ensure_terminalisation_retry_thread_locked(self) -> None:
        thread = self._terminalisation_retry_thread
        if thread is not None and thread.is_alive():
            return
        self._terminalisation_retry_shutdown = False
        thread = threading.Thread(
            target=self._terminalisation_retry_loop,
            name="conversation_turn_terminalisation_retry",
            daemon=True,
        )
        self._terminalisation_retry_thread = thread
        thread.start()

    def _schedule_terminalisation_retry_locked(
        self,
        token: ConversationTurnAdmissionToken,
        *,
        status: str,
        error: str | None,
    ) -> None:
        failure_count = (
            self._terminalisation_retry_failure_counts.get(token.token_id, 0) + 1
        )
        self._terminalisation_retry_failure_counts[token.token_id] = failure_count
        delay = min(
            self._terminalisation_retry_max_delay_sec,
            self._terminalisation_retry_initial_delay_sec
            * (2 ** min(failure_count - 1, 10)),
        )
        self._terminalisation_retries[token.token_id] = _TerminalisationRetry(
            token=token,
            status=status,
            error=error,
            failure_count=failure_count,
            retry_at_monotonic=time.monotonic() + delay,
        )
        self._ensure_terminalisation_retry_thread_locked()
        self._terminalisation_retry_condition.notify_all()

    def _terminalisation_retry_loop(self) -> None:
        while True:
            with self._terminalisation_retry_condition:
                while not self._terminalisation_retry_shutdown:
                    if not self._terminalisation_retries:
                        self._terminalisation_retry_condition.wait()
                        continue
                    retry = min(
                        self._terminalisation_retries.values(),
                        key=lambda item: item.retry_at_monotonic,
                    )
                    wait_seconds = retry.retry_at_monotonic - time.monotonic()
                    if wait_seconds > 0:
                        self._terminalisation_retry_condition.wait(timeout=wait_seconds)
                        continue
                    # Remove the due item before retrying. A failed release puts
                    # it back with backoff; a successful or concurrent release
                    # leaves no stale item that could spin at a past deadline.
                    self._terminalisation_retries.pop(retry.token.token_id, None)
                    break
                else:
                    return

            try:
                self.release(
                    retry.token,
                    status=retry.status,
                    error=retry.error,
                )
            except Exception as exc:  # noqa: BLE001 - retry loop must survive store errors
                _logger.warning(
                    "Turn terminalisation retry %s failed for queue_id=%s: %s",
                    retry.failure_count,
                    retry.token.queue_id,
                    exc,
                )

    def shutdown(self, *, wait: bool = True, timeout: float = 2.0) -> None:
        """Stop this instance's retry and lease-heartbeat workers."""

        self._lease_heartbeat_stop.set()
        with self._terminalisation_retry_condition:
            self._terminalisation_retry_shutdown = True
            self._terminalisation_retry_condition.notify_all()
            retry_thread = self._terminalisation_retry_thread
        heartbeat_thread = self._lease_heartbeat_thread
        if wait:
            for thread in (retry_thread, heartbeat_thread):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=max(0.0, float(timeout)))

    def _reserve_capacity(self, user_concept_id: str) -> None:
        with self._lock:
            global_active = len(self._active_tokens) + self._reserved_global
            user_active = self._active_by_user.get(user_concept_id, 0)
            if global_active >= self._global_limit:
                raise ConversationTurnCapacityReached(
                    "Von is at its current active-turn capacity"
                )
            if user_active >= self._per_user_limit:
                raise ConversationTurnCapacityReached(
                    "This user is at the current active-turn capacity"
                )
            # Reserve before the durable bind so racing requests in this
            # process cannot all pass a count-then-act check.
            self._active_by_user[user_concept_id] = user_active + 1
            self._reserved_global += 1

    def _release_user_capacity(
        self,
        user_concept_id: str,
        *,
        reserved: bool,
    ) -> None:
        with self._lock:
            if reserved:
                self._reserved_global = max(0, self._reserved_global - 1)
            remaining = self._active_by_user.get(user_concept_id, 0) - 1
            if remaining > 0:
                self._active_by_user[user_concept_id] = remaining
            else:
                self._active_by_user.pop(user_concept_id, None)

    @staticmethod
    def _task_execution_link(
        token: ConversationTurnAdmissionToken,
    ) -> dict[str, str] | None:
        """Return one complete internal task-execution link or reject corruption."""

        raw_link = {
            "task_concept_id": token.task_concept_id,
            "task_execution_concept_id": token.task_execution_concept_id,
            "enqueue_submission_id": token.enqueue_submission_id,
            "queue_id": token.queue_id,
            "actor_concept_id": token.user_concept_id,
            "organisation_concept_id": token.organisation_concept_id,
        }
        present = {
            key: str(value).strip()
            for key, value in raw_link.items()
            if isinstance(value, str) and value.strip()
        }
        if not any(
            key in present
            for key in (
                "task_concept_id",
                "task_execution_concept_id",
            )
        ):
            return None
        missing = [key for key in raw_link if key not in present]
        if missing:
            raise RuntimeError(
                "bound task execution link is incomplete: " + ", ".join(missing)
            )
        return present

    @classmethod
    def _transition_linked_task_execution(
        cls,
        token: ConversationTurnAdmissionToken,
        *,
        status: str,
        source: str,
        result: str | None = None,
        queue_record: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        link = cls._task_execution_link(token)
        if link is None:
            return None
        if queue_record is not None:
            for field in (
                "queue_id",
                "enqueue_submission_id",
                "task_concept_id",
                "task_execution_concept_id",
            ):
                if queue_record.get(field) != link[field]:
                    raise RuntimeError(
                        f"terminal queue record {field} does not match admission"
                    )
        return task_execution_service.transition_task_execution(
            link["task_execution_concept_id"],
            actor_concept_id=link["actor_concept_id"],
            organisation_concept_id=link["organisation_concept_id"],
            enqueue_submission_id=link["enqueue_submission_id"],
            queue_id=link["queue_id"],
            status=status,
            result=result,
            source=source,
        )

    def acquire(
        self,
        *,
        scope: Mapping[str, Any],
        prompt_raw: str,
        session_id: str,
        session_name: str | None,
        client_request_id: str,
        conversation_key: str,
        queue_id: str | None = None,
        attempt_id: str | None = None,
        dispatch_reservation_token: str | None = None,
        window_session_id: str | None = None,
    ) -> ConversationTurnAdmissionToken:
        """Acquire capacity and the durable conversation fence without waiting."""

        canonical_scope = chat_prompt_queue_service.build_queue_scope(
            user_concept_id=str(scope.get("user_concept_id") or ""),
            organisation_concept_id=scope.get("organisation_concept_id"),
            namespace=scope.get("namespace"),
        )
        user_id = str(canonical_scope["user_concept_id"] or "")
        self._reserve_capacity(user_id)
        registered_token: ConversationTurnAdmissionToken | None = None
        try:
            bound_queue_id = str(queue_id or "").strip()
            if not bound_queue_id:
                try:
                    record = chat_prompt_queue_service.create_queue_record(
                        scope=canonical_scope,
                        prompt_raw=prompt_raw,
                        session_id=session_id,
                        session_name=session_name,
                        status=chat_prompt_queue_service.STATUS_QUEUED,
                        source="server_direct",
                        client_request_id=client_request_id,
                        attempt_id=attempt_id,
                        conversation_key=conversation_key,
                        legacy_submission_role=(
                            chat_prompt_queue_service.LEGACY_SUBMISSION_ROLE_GENERATE
                            if not isinstance(attempt_id, str) or not attempt_id.strip()
                            else None
                        ),
                        window_session_id=window_session_id,
                    )
                except chat_prompt_queue_service.ChatPromptQueueCapacityReached as exc:
                    raise ConversationTurnCapacityReached(str(exc)) from exc
                except chat_prompt_queue_service.ConversationTurnAlreadyActive as exc:
                    raise ConversationTurnActive(
                        str(exc),
                        queue_id=exc.queue_id,
                    ) from exc
                bound_queue_id = str(record["queue_id"])
            try:
                bound = chat_prompt_queue_service.bind_queue_record_to_turn(
                    scope=canonical_scope,
                    queue_id=bound_queue_id,
                    client_request_id=client_request_id,
                    conversation_key=conversation_key,
                    server_instance_id=SERVER_INSTANCE_ID,
                    attempt_id=attempt_id,
                    dispatch_reservation_token=dispatch_reservation_token,
                )
            except chat_prompt_queue_service.ConversationTurnAlreadyActive as exc:
                raise ConversationTurnActive(
                    str(exc),
                    queue_id=bound_queue_id,
                ) from exc

            token = ConversationTurnAdmissionToken(
                token_id=str(uuid.uuid4()),
                queue_id=bound_queue_id,
                attempt_id=str(bound.get("attempt_id") or attempt_id or ""),
                user_concept_id=user_id,
                organisation_concept_id=canonical_scope.get("organisation_concept_id"),
                namespace=canonical_scope.get("namespace"),
                conversation_key=conversation_key,
                client_request_id=client_request_id,
                session_id=session_id,
                enqueue_submission_id=(
                    str(bound.get("enqueue_submission_id") or "").strip() or None
                ),
                task_concept_id=(
                    str(bound.get("task_concept_id") or "").strip() or None
                ),
                task_execution_concept_id=(
                    str(bound.get("task_execution_concept_id") or "").strip() or None
                ),
            )
            with self._lock:
                self._active_tokens[token.token_id] = token
                self._reserved_global = max(0, self._reserved_global - 1)
                self._ensure_lease_heartbeat_thread_locked()
            registered_token = token
            try:
                self._transition_linked_task_execution(
                    token,
                    status=task_execution_service.TASK_EXECUTION_STATUS_IN_PROGRESS,
                    source="queue_admission",
                    queue_record=bound,
                )
            except Exception as exc:
                # The registered token keeps capacity and the exact queue fence
                # owned until queue and TaskExecution failure terminalisation
                # both succeed through the ordinary durable retry path.
                try:
                    self.release(
                        token,
                        status=chat_prompt_queue_service.STATUS_FAILED,
                        error=(
                            "TaskExecution admission transition failed: "
                            f"{type(exc).__name__}: {exc}"
                        )[:4_000],
                    )
                except Exception:
                    _logger.exception(
                        "Failed to terminalise bound task execution after "
                        "admission transition error queue_id=%s",
                        token.queue_id,
                    )
                raise
            return token
        except Exception:
            if registered_token is None:
                self._release_user_capacity(user_id, reserved=True)
            raise

    def acquire_ephemeral(
        self,
        *,
        actor_capacity_key: str,
        conversation_key: str,
        client_request_id: str,
    ) -> ConversationTurnAdmissionToken:
        """Bound an unpersisted compatibility turn without bypassing capacity.

        Anonymous or otherwise non-durable turns do not gain a prompt queue
        authority record, but they still count towards global/per-browser
        capacity and retain in-process single-flight for their conversation.
        """

        actor_key = str(actor_capacity_key or "").strip()
        if not actor_key:
            raise ValueError("actor capacity key is required")
        self._reserve_capacity(actor_key)
        try:
            with self._lock:
                if conversation_key in self._ephemeral_conversation_tokens:
                    raise ConversationTurnActive(
                        "another turn is active for this conversation"
                    )
                token = ConversationTurnAdmissionToken(
                    token_id=str(uuid.uuid4()),
                    queue_id=None,
                    attempt_id=None,
                    user_concept_id=actor_key,
                    organisation_concept_id=None,
                    namespace=None,
                    conversation_key=conversation_key,
                    client_request_id=client_request_id,
                )
                self._active_tokens[token.token_id] = token
                self._ephemeral_conversation_tokens[conversation_key] = token.token_id
                self._reserved_global = max(0, self._reserved_global - 1)
            return token
        except Exception:
            self._release_user_capacity(actor_key, reserved=True)
            raise

    def release(
        self,
        token: ConversationTurnAdmissionToken,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        """Terminalise the durable record and release capacity exactly once."""

        with self._lock:
            live_token = self._active_tokens.get(token.token_id)
            if live_token is None or token.released:
                return
            # The first terminal observation is the exact disposition retried
            # after an unknown or unavailable persistence acknowledgement.
            if token.pending_terminal_status is None:
                token.pending_terminal_status = status
                token.pending_terminal_error = error
            status = token.pending_terminal_status
            error = token.pending_terminal_error
            token.released = True
        try:
            terminal_record = None
            if token.queue_id:
                terminal_record = chat_prompt_queue_service.finish_prompt_record(
                    scope=token.scope,
                    queue_id=token.queue_id,
                    status=status,
                    error=error,
                    attempt_id=token.attempt_id,
                )
            execution_status = {
                chat_prompt_queue_service.STATUS_COMPLETED: (
                    task_execution_service.TASK_EXECUTION_STATUS_COMPLETED
                ),
                chat_prompt_queue_service.STATUS_FAILED: (
                    task_execution_service.TASK_EXECUTION_STATUS_FAILED
                ),
                chat_prompt_queue_service.STATUS_CANCELLED: (
                    task_execution_service.TASK_EXECUTION_STATUS_CANCELLED
                ),
            }.get(status)
            if execution_status is not None:
                if not (
                    isinstance(terminal_record, Mapping)
                    and terminal_record.get("task_execution_reconciliation_status")
                ):
                    # Compatibility for legacy terminal writers and isolated
                    # callers that predate the durable queue marker.
                    self._transition_linked_task_execution(
                        token,
                        status=execution_status,
                        source="queue_terminalisation",
                        result=(
                            error
                            if execution_status
                            != task_execution_service.TASK_EXECUTION_STATUS_COMPLETED
                            else None
                        ),
                        queue_record=terminal_record,
                    )
                    terminal_record = None
                # The queue terminal CAS has already persisted a linked
                # reconciliation marker.  Attempt the projection now for low
                # latency, but do not keep process capacity or the cleared
                # conversation fence hostage if the separate TaskExecution
                # store is unavailable: the server dispatcher owns the durable
                # retry from this point.
                try:
                    from .chat_prompt_queue_dispatch_service import (
                        reconcile_linked_task_execution_terminal,
                        wake_chat_prompt_queue_dispatcher,
                    )

                    if terminal_record is not None:
                        reconcile_linked_task_execution_terminal(
                            terminal_record,
                            status=status,
                            source="queue_terminalisation",
                            error=error,
                        )
                except Exception as exc:  # noqa: BLE001 - marker is retryable
                    _logger.warning(
                        "TaskExecution terminal reconciliation deferred for "
                        "queue_id=%s: %s",
                        token.queue_id,
                        exc,
                    )
                finally:
                    wake_chat_prompt_queue_dispatcher()
        except Exception:
            # Preserve the live token so teardown or an explicit recovery path
            # can retry durable terminalisation. Releasing process capacity
            # before the persisted conversation fence clears would make the
            # server appear healthier than its actual admission state.
            with self._lock:
                token.released = False
                self._schedule_terminalisation_retry_locked(
                    token,
                    status=status,
                    error=error,
                )
            raise

        with self._lock:
            self._active_tokens.pop(token.token_id, None)
            self._terminalisation_retries.pop(token.token_id, None)
            self._terminalisation_retry_failure_counts.pop(token.token_id, None)
            self._terminalisation_retry_condition.notify_all()
            if (
                self._ephemeral_conversation_tokens.get(token.conversation_key)
                == token.token_id
            ):
                self._ephemeral_conversation_tokens.pop(token.conversation_key, None)
        self._release_user_capacity(token.user_concept_id, reserved=False)

    def snapshot(self) -> dict[str, Any]:
        """Return content-free live capacity telemetry."""

        with self._lock:
            return {
                "schema_version": "conversation_turn_admission.v1",
                "server_instance_id": SERVER_INSTANCE_ID,
                "active_global": len(self._active_tokens),
                "pending_admission": self._reserved_global,
                "global_limit": self._global_limit,
                "per_user_limit": self._per_user_limit,
                "configured_http_threads": self._configured_http_threads,
                "available_http_thread_headroom": max(
                    0,
                    self._configured_http_threads - self._global_limit,
                ),
                "active_user_count": len(self._active_by_user),
                "lease_heartbeat_running": bool(
                    self._lease_heartbeat_thread
                    and self._lease_heartbeat_thread.is_alive()
                ),
                "lease_heartbeat_interval_seconds": (
                    self._lease_heartbeat_interval_sec
                ),
            }


conversation_turn_admission_service = ConversationTurnAdmissionService()
