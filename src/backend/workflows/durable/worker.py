"""Background worker for processing durable workflow instances.

Polls MongoDB for claimable instances and executes them with checkpointing.
Supports graceful shutdown and automatic lock heartbeat.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import threading
import time
from typing import Any, Callable, Mapping

from ...db.transient_errors import run_with_transient_mongo_retry
from ...security.access_control import override_current_actor
from ..action_registry import ActionRegistry
from .instance_manager import WorkflowInstanceManager
from .durable_executor import (
    DurableWorkflowExecutor,
    DurableWorkflowResult,
    _project_executed_workflow_definition_identity,
)
from .failed_output_diagnostics import (
    build_completed_workflow_outputs,
    build_failed_workflow_outputs,
)
from .models import WorkflowInstance
from .worker_identity import build_worker_build_identity

logger = logging.getLogger(__name__)


# Type for workflow definition loaders. Durable execution must resolve the
# definition under the actor persisted on the instance, rather than trusting a
# process-global registry entry that may have been warmed by another actor.
WorkflowDefinitionLoader = Callable[..., Any]


def _loader_explicitly_supports_authority_resolution(loader: Any) -> bool:
    """Return whether a loader explicitly opts into the authority-result protocol.

    Legacy loaders often accept ``**kwargs`` only to tolerate actor fields. Do
    not send the new flag through that catch-all: an explicit parameter keeps
    existing custom loaders unchanged and fail-closed for identity stamping.
    """

    try:
        signature = inspect.signature(loader)
    except (TypeError, ValueError):
        return False
    parameter = signature.parameters.get("include_authority_resolution")
    return parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


def _normalise_loaded_definition_and_identity(
    loaded: Any,
) -> tuple[Any, dict[str, Any] | None]:
    """Separate a legacy definition from a definition-authority resolution.

    The canonical resolution already computed an identity from the exact
    definition it returns. Recompute only the runtime definition hash locally
    to reject a stale or spliced resolution; never perform another Vontology
    lookup from the worker.
    """

    missing = object()
    resolved_definition = getattr(loaded, "definition", missing)
    if resolved_definition is missing:
        return loaded, None
    if resolved_definition is None:
        return None, None

    raw_identity = getattr(loaded, "definition_identity", None)
    if not isinstance(raw_identity, Mapping):
        return resolved_definition, None
    workflow_id = getattr(resolved_definition, "workflow_id", None)
    if (
        not isinstance(workflow_id, str)
        or not workflow_id.strip()
        or raw_identity.get("workflow_id") != workflow_id
    ):
        return resolved_definition, None

    try:
        from ..workflow_definition_identity_service import (
            build_workflow_definition_identity,
        )

        runtime_identity = build_workflow_definition_identity(
            workflow_id=workflow_id,
            source=str(raw_identity.get("source") or "unknown"),
            definition=resolved_definition,
            authoritative_definition=None,
        )
    except Exception:
        logger.warning(
            "[durable_worker] Could not validate loaded definition identity for %s",
            workflow_id,
            exc_info=True,
        )
        return resolved_definition, None
    runtime_hash = runtime_identity.get("runtime_definition_hash")
    source = str(raw_identity.get("source") or "").strip().lower()
    authoritative_hash = raw_identity.get("authoritative_definition_hash")
    if (
        raw_identity.get("runtime_definition_hash") != runtime_hash
        or raw_identity.get("definition_hash") != runtime_hash
        or raw_identity.get("state_count") != runtime_identity.get("state_count")
        or raw_identity.get("action_count") != runtime_identity.get("action_count")
        or (source == "vontology" and authoritative_hash != runtime_hash)
    ):
        logger.warning(
            "[durable_worker] Refusing stale or spliced definition identity for %s",
            workflow_id,
        )
        return resolved_definition, None
    projected_identity = _project_executed_workflow_definition_identity(
        raw_identity,
        workflow_id=workflow_id,
    )
    if projected_identity is None:
        logger.warning(
            "[durable_worker] Refusing invalid definition identity for %s",
            workflow_id,
        )
        return resolved_definition, None
    return resolved_definition, projected_identity


def _default_live_load_getter() -> int:
    """Return the process-global in-flight live foreground turn count.

    Imported lazily so the durable worker has no import-time dependency on the
    Flask request layer, and so a failure to read the signal never wedges the
    worker (it simply reports "no live load").
    """
    try:
        from ...services.live_request_load import get_live_turn_count

        return int(get_live_turn_count())
    except Exception:
        return 0


class DurableWorkflowWorker:
    """Background worker that processes durable workflow instances.

    Features:
    - Polling loop to claim and execute instances
    - Concurrent execution with configurable batch size
    - Lock heartbeat to prevent stale claims
    - Graceful shutdown with drain
    - Callback hooks for observability
    """

    def __init__(
        self,
        worker_id: str | None = None,
        *,
        instance_manager: WorkflowInstanceManager,
        registry: ActionRegistry,
        definition_loader: WorkflowDefinitionLoader,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 5,
        heartbeat_interval_seconds: float = 60.0,
        max_transitions: int = 50,
        live_load_getter: Callable[[], int] | None = None,
    ) -> None:
        """Initialise the worker.

        Args:
            worker_id: Unique identifier for this worker (auto-generated if None).
            instance_manager: Manager for instance persistence.
            registry: Action registry for workflow execution.
            definition_loader: Function to load workflow definitions by ID and
                persisted actor scope.
            poll_interval_seconds: Time between polling cycles.
            batch_size: Maximum concurrent instances.
            heartbeat_interval_seconds: Interval for lock heartbeat.
            max_transitions: Maximum state transitions per instance.
            live_load_getter: Optional callable returning the number of in-flight
                live foreground turns. Injected for testing; defaults to the
                process-global live-request-load signal.
        """
        self._worker_id = worker_id or self._generate_worker_id()
        self._worker_build_identity = build_worker_build_identity(self._worker_id)
        self._instance_manager = instance_manager
        self._registry = registry
        self._definition_loader = definition_loader
        self._definition_loader_supports_authority_resolution = (
            _loader_explicitly_supports_authority_resolution(definition_loader)
        )
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._heartbeat_interval = heartbeat_interval_seconds
        self._priority_reserved_slots = self._load_priority_reserved_slots(
            batch_size=batch_size
        )
        self._live_load_getter = live_load_getter or _default_live_load_getter
        self._pause_background_under_live_load = (
            self._load_pause_background_under_live_load()
        )
        self._live_load_pause_threshold = self._load_live_load_pause_threshold()

        self._executor = DurableWorkflowExecutor(
            registry=registry,
            instance_manager=instance_manager,
            max_transitions=max_transitions,
        )

        self._running = False
        self._shutdown_event = threading.Event()
        self._current_instances: dict[str, threading.Thread] = {}
        self._current_claim_tokens: dict[str, str] = {}
        self._lock = threading.Lock()

        # Callbacks for observability
        self._on_instance_started: Callable[[WorkflowInstance], None] | None = None
        self._on_instance_completed: (
            Callable[[str, DurableWorkflowResult], None] | None
        ) = None
        self._on_instance_failed: Callable[[str, str], None] | None = None

    @staticmethod
    def _generate_worker_id() -> str:
        """Generate a unique worker ID."""
        hostname = socket.gethostname()[:16]
        pid = os.getpid()
        return f"worker_{hostname}_{pid}"

    @staticmethod
    def _load_priority_reserved_slots(*, batch_size: int) -> int:
        """Return worker slots reserved for fresh user-turn workflows."""
        try:
            configured = int(os.getenv("VON_DURABLE_WORKER_PRIORITY_RESERVED_SLOTS", "1"))
        except (TypeError, ValueError):
            configured = 1
        if batch_size <= 1:
            return 0
        return min(max(configured, 0), batch_size - 1)

    @staticmethod
    def _load_pause_background_under_live_load() -> bool:
        """Whether to defer background workflow claims while a user turn is live.

        Enabled by default (JVNAUTOSCI-2383): heavy background evaluation
        workflows (episode/paper-recommendation evaluation) lazy-load large
        definitions and contend for the shared Mongo pool, which can starve the
        live chat/progress path. Deferring — not dropping — that work while a
        user is actively waiting keeps the live path responsive. Disable with
        VON_DURABLE_WORKER_PAUSE_BACKGROUND_UNDER_LIVE_LOAD=0.
        """
        raw = os.getenv("VON_DURABLE_WORKER_PAUSE_BACKGROUND_UNDER_LIVE_LOAD", "1")
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _load_live_load_pause_threshold() -> int:
        """Number of in-flight live turns at/above which background work pauses."""
        try:
            configured = int(
                os.getenv("VON_DURABLE_WORKER_LIVE_LOAD_THRESHOLD", "1")
            )
        except (TypeError, ValueError):
            configured = 1
        return max(configured, 1)

    def _should_defer_background_work(self) -> bool:
        """Return True when background claims should be skipped this poll cycle.

        Fresh user-turn (priority) instances are always claimed; only general
        background instances are deferred. The deferral is transient — the next
        poll cycle re-evaluates once the live turn(s) complete.
        """
        if not self._pause_background_under_live_load:
            return False
        try:
            live = int(self._live_load_getter())
        except Exception:
            return False
        return live >= self._live_load_pause_threshold

    @property
    def worker_id(self) -> str:
        """Return the worker identifier."""
        return self._worker_id

    @property
    def worker_build_identity(self) -> dict[str, Any]:
        """Return the worker build identity stamped on claims."""
        return dict(self._worker_build_identity)

    @property
    def is_running(self) -> bool:
        """Return True if the worker is running."""
        return self._running

    def _active_instance_ids(self) -> list[str]:
        with self._lock:
            return list(self._current_instances.keys())

    def _record_worker_heartbeat(self, *, state: str = "running") -> None:
        upsert = getattr(self._instance_manager, "upsert_worker_heartbeat", None)
        if not callable(upsert):
            return
        try:
            upsert(
                worker_id=self._worker_id,
                worker_build_identity=self._worker_build_identity,
                active_instance_ids=self._active_instance_ids(),
                state=state,
            )
        except Exception as exc:
            logger.debug(
                "[durable_worker] Worker heartbeat skipped for %s: %s",
                self._worker_id,
                exc,
            )

    def _record_worker_stopped(self) -> None:
        mark_stopped = getattr(self._instance_manager, "mark_worker_stopped", None)
        if callable(mark_stopped):
            try:
                mark_stopped(
                    worker_id=self._worker_id,
                    worker_build_identity=self._worker_build_identity,
                    active_instance_ids=self._active_instance_ids(),
                )
                return
            except Exception as exc:
                logger.debug(
                    "[durable_worker] Worker stopped marker skipped for %s: %s",
                    self._worker_id,
                    exc,
                )
        self._record_worker_heartbeat(state="stopped")

    def set_callbacks(
        self,
        *,
        on_started: Callable[[WorkflowInstance], None] | None = None,
        on_completed: Callable[[str, DurableWorkflowResult], None] | None = None,
        on_failed: Callable[[str, str], None] | None = None,
    ) -> None:
        """Set observability callbacks.

        Args:
            on_started: Called when an instance starts processing.
            on_completed: Called when an instance completes.
            on_failed: Called when an instance fails.
        """
        self._on_instance_started = on_started
        self._on_instance_completed = on_completed
        self._on_instance_failed = on_failed

    def start(self) -> None:
        """Start the worker in the current thread (blocking).

        Call this from a dedicated thread or use start_background().
        """
        self._running = True
        self._shutdown_event.clear()
        logger.info("[durable_worker] Worker %s starting", self._worker_id)
        self._record_worker_heartbeat(state="starting")

        # Start heartbeat thread
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"{self._worker_id}_heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()

        try:
            self._poll_loop()
        finally:
            self._running = False
            self._shutdown_event.set()
            self._wait_for_current_instances()
            self._record_worker_stopped()
            logger.info("[durable_worker] Worker %s stopped", self._worker_id)

    def start_background(self) -> threading.Thread:
        """Start the worker in a background thread.

        Returns:
            The worker thread.
        """
        thread = threading.Thread(
            target=self.start,
            name=f"{self._worker_id}_main",
            daemon=True,
        )
        thread.start()
        return thread

    def stop(self, timeout: float = 30.0) -> None:
        """Request graceful shutdown.

        Args:
            timeout: Maximum time to wait for current instances.
        """
        logger.info("[durable_worker] Shutdown requested for %s", self._worker_id)
        self._running = False
        self._shutdown_event.set()

        # Wait for current instances to complete
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._current_instances:
                    break
            time.sleep(0.5)

        with self._lock:
            if self._current_instances:
                logger.warning(
                    "[durable_worker] Shutdown timeout, %d instances still running",
                    len(self._current_instances),
                )

    def _poll_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                logger.exception("[durable_worker] Poll error: %s", e)

            # Wait for next poll or shutdown
            self._shutdown_event.wait(timeout=self._poll_interval)

    def _poll_once(self) -> None:
        """Single poll iteration."""
        # Check capacity
        with self._lock:
            active_count = len(self._current_instances)
            available_slots = self._batch_size - active_count

        if available_slots <= 0:
            return

        def _start_instance(instance: WorkflowInstance) -> bool:
            with self._lock:
                if instance.instance_id in self._current_instances:
                    return False

                thread = threading.Thread(
                    target=self._process_instance,
                    args=(instance,),
                    name=f"{self._worker_id}_{instance.instance_id[:8]}",
                    daemon=True,
                )
                self._current_instances[instance.instance_id] = thread
                if instance.claim_token:
                    self._current_claim_tokens[instance.instance_id] = (
                        instance.claim_token
                    )
                thread.start()
                return True

        started_count = 0
        for _ in range(available_slots):
            if not self._running:
                break

            instance = self._instance_manager.find_and_claim_instance(
                self._worker_id,
                priority_only=True,
                worker_build_identity=self._worker_build_identity,
            )
            if instance is None:
                break
            if _start_instance(instance):
                started_count += 1

        available_slots -= started_count
        background_capacity = max(
            0,
            self._batch_size - self._priority_reserved_slots - active_count - started_count,
        )
        # JVNAUTOSCI-2383: yield background evaluation capacity to the live chat
        # path while a user turn is in flight. Priority (fresh user-turn)
        # instances above were already claimed; only defer general background
        # work, and only for this cycle.
        if self._should_defer_background_work():
            background_capacity = 0
        general_slots = min(available_slots, background_capacity)
        for _ in range(general_slots):
            if not self._running:
                break

            instance = self._instance_manager.find_and_claim_instance(
                self._worker_id,
                worker_build_identity=self._worker_build_identity,
            )
            if instance is None:
                break
            _start_instance(instance)

        if started_count or general_slots:
            self._record_worker_heartbeat()

    def _process_instance(self, instance: WorkflowInstance) -> None:
        """Process a single workflow instance.

        Args:
            instance: The instance to process.
        """
        instance_id = instance.instance_id
        claim_token = instance.claim_token
        logger.info(
            "[durable_worker] Processing instance %s (workflow=%s)",
            instance_id,
            instance.workflow_id,
        )

        def _retry_store_call(operation_name: str, operation):
            return run_with_transient_mongo_retry(
                operation,
                operation_name=f"durable_worker.{operation_name}:{instance_id}",
                logger_obj=logger,
            )

        def _best_effort_mark_failed(
            *,
            error: str,
            error_step: str | None = None,
            increment_retry: bool = True,
            outputs: dict[str, Any] | None = None,
            execution_trace_id: str | None = None,
        ) -> bool:
            try:
                return bool(
                    self._instance_manager.mark_failed(
                        instance_id,
                        error=error,
                        error_step=error_step,
                        increment_retry=increment_retry,
                        outputs=outputs,
                        execution_trace_id=execution_trace_id,
                        worker_id=self._worker_id,
                        claim_token=claim_token,
                    )
                )
            except Exception:
                logger.exception(
                    "[durable_worker] Failed to persist FAILED status for %s",
                    instance_id,
                )
                return False

        def _best_effort_release_lock() -> None:
            try:
                self._instance_manager.release_lock(
                    instance_id,
                    self._worker_id,
                    **(
                        {"claim_token": claim_token}
                        if claim_token is not None
                        else {}
                    ),
                )
            except Exception:
                logger.exception(
                    "[durable_worker] Failed to release lock for %s",
                    instance_id,
                )

        try:
            # Notify started
            if self._on_instance_started:
                try:
                    self._on_instance_started(instance)
                except Exception:
                    pass

            # A durable worker has no Flask request context. Keep the persisted
            # actor bound across definition loading *and the complete workflow
            # run* so LLM prompt resolution, nested workflow loading, explicit
            # actions, and fallback tools all enforce the same authority.
            with override_current_actor(instance.user_id, instance.org_id):
                def _load_definition_with_authority() -> tuple[
                    Any,
                    dict[str, Any] | None,
                ]:
                    loader_kwargs: dict[str, Any] = {
                        "actor_user_id": instance.user_id,
                        "actor_org_id": instance.org_id,
                        "actor_namespace": instance.namespace,
                    }
                    if self._definition_loader_supports_authority_resolution:
                        loader_kwargs["include_authority_resolution"] = True
                    loaded = self._definition_loader(
                        instance.workflow_id,
                        **loader_kwargs,
                    )
                    return _normalise_loaded_definition_and_identity(loaded)

                definition, workflow_definition_identity = _retry_store_call(
                    "load_definition",
                    _load_definition_with_authority,
                )

                result = (
                    self._executor.run_durable(
                        instance_id,
                        definition,
                        worker_id=self._worker_id,
                        claim_token=claim_token,
                        resume_from_checkpoint=True,
                        workflow_definition_identity=workflow_definition_identity,
                    )
                    if definition is not None
                    else None
                )

            if definition is None:
                error = f"workflow_definition_not_found:{instance.workflow_id}"
                failure_saved = _best_effort_mark_failed(
                    error=error,
                    increment_retry=False,
                )
                if failure_saved and self._on_instance_failed:
                    try:
                        self._on_instance_failed(instance_id, error)
                    except Exception:
                        pass
                return

            # The definition guard above establishes that execution produced a
            # result; keep this assertion local so the optional is not allowed
            # to leak into status handling below.
            assert result is not None

            # Update status based on result
            if result.completed:
                completed_outputs = build_completed_workflow_outputs(
                    result.data,
                    result_envelope=result.result_envelope,
                    final_state=result.final_state,
                    execution_trace_id=result.execution_trace_id,
                )
                completion_saved = _retry_store_call(
                    "mark_completed",
                    lambda: self._instance_manager.mark_completed(
                        instance_id,
                        outputs=completed_outputs,
                        final_state=result.final_state,
                        execution_trace_id=result.execution_trace_id,
                        worker_id=self._worker_id,
                        claim_token=claim_token,
                    ),
                )
                if completion_saved is not True:
                    logger.warning(
                        "[durable_worker] Completion fenced after lease loss for %s",
                        instance_id,
                    )
                    return
                if self._on_instance_completed:
                    try:
                        self._on_instance_completed(instance_id, result)
                    except Exception:
                        pass
                logger.info("[durable_worker] Instance %s completed", instance_id)

            elif result.error == "cancelled":
                # Already marked as cancelled
                logger.info("[durable_worker] Instance %s was cancelled", instance_id)

            elif result.error == "paused_at_checkpoint":
                # The executor atomically persisted the successor checkpoint,
                # manual hold, and typed receipt under this worker's claim.
                # Explicit resume is now the only path back to PENDING.
                logger.info(
                    "[durable_worker] Instance %s paused at a represented checkpoint",
                    instance_id,
                )

            elif result.error == "durable_lock_lost":
                logger.warning(
                    "[durable_worker] Lease lost while processing %s; "
                    "leaving persistence to the current claim holder",
                    instance_id,
                )

            else:
                failed_outputs = build_failed_workflow_outputs(
                    result.data,
                    error=result.error or "unknown_error",
                    error_step=result.final_state,
                )
                failure_saved = _best_effort_mark_failed(
                    error=result.error or "unknown_error",
                    error_step=result.final_state,
                    outputs=failed_outputs,
                    execution_trace_id=result.execution_trace_id,
                )
                if failure_saved and self._on_instance_failed:
                    try:
                        self._on_instance_failed(instance_id, result.error or "unknown")
                    except Exception:
                        pass
                if failure_saved:
                    logger.warning(
                        "[durable_worker] Instance %s failed: %s",
                        instance_id,
                        result.error,
                    )
                else:
                    logger.warning(
                        "[durable_worker] Failure fenced after lease loss for %s",
                        instance_id,
                    )

        except Exception as e:
            logger.exception(
                "[durable_worker] Unexpected error processing %s", instance_id
            )
            failure_saved = _best_effort_mark_failed(
                error=f"worker_exception:{e}",
            )
            if failure_saved and self._on_instance_failed:
                try:
                    self._on_instance_failed(instance_id, str(e))
                except Exception:
                    pass

        finally:
            with self._lock:
                self._current_instances.pop(instance_id, None)
                self._current_claim_tokens.pop(instance_id, None)
            # Stop heartbeat extension before best-effort unlock so a dead worker
            # thread cannot keep a stale RUNNING row alive after teardown starts.
            _best_effort_release_lock()

    def _heartbeat_loop(self) -> None:
        """Periodically extend locks on active instances."""
        while self._running:
            self._shutdown_event.wait(timeout=self._heartbeat_interval)
            if not self._running:
                break

            instance_ids = self._active_instance_ids()
            self._record_worker_heartbeat()

            for instance_id in instance_ids:
                with self._lock:
                    claim_token = self._current_claim_tokens.get(instance_id)
                try:
                    lock_extended = self._instance_manager.extend_lock(
                        instance_id,
                        self._worker_id,
                        extend_seconds=int(self._heartbeat_interval * 2),
                        **(
                            {"claim_token": claim_token}
                            if claim_token is not None
                            else {}
                        ),
                    )
                    if lock_extended is not True:
                        logger.warning(
                            "[durable_worker] Heartbeat lease lost for %s",
                            instance_id,
                        )
                except Exception as e:
                    logger.warning(
                        "[durable_worker] Failed to extend lock for %s: %s",
                        instance_id,
                        e,
                    )

    def _wait_for_current_instances(self, timeout: float = 10.0) -> None:
        """Wait for current instances to finish."""
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._current_instances.values())

        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                thread.join(timeout=remaining)


class AsyncDurableWorkflowWorker:
    """Async wrapper for DurableWorkflowWorker.

    Provides async start/stop methods for integration with async frameworks.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialise with same args as DurableWorkflowWorker."""
        self._worker = DurableWorkflowWorker(**kwargs)
        self._thread: threading.Thread | None = None

    @property
    def worker_id(self) -> str:
        """Return the worker identifier."""
        return self._worker.worker_id

    @property
    def is_running(self) -> bool:
        """Return True if the worker is running."""
        return self._worker.is_running

    async def start(self) -> None:
        """Start the worker in a background thread."""
        self._thread = self._worker.start_background()
        # Give it a moment to start
        await asyncio.sleep(0.1)

    async def stop(self, timeout: float = 30.0) -> None:
        """Stop the worker gracefully."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._worker.stop, timeout)
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
