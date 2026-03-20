"""Background worker for processing durable workflow instances.

Polls MongoDB for claimable instances and executes them with checkpointing.
Supports graceful shutdown and automatic lock heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
from typing import Any, Callable

from ..engine import WorkflowDefinition
from ..action_registry import ActionRegistry
from .instance_manager import WorkflowInstanceManager
from .durable_executor import DurableWorkflowExecutor, DurableWorkflowResult
from .models import WorkflowInstance

logger = logging.getLogger(__name__)


# Type for workflow definition loader
WorkflowDefinitionLoader = Callable[[str], WorkflowDefinition | None]


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
    ) -> None:
        """Initialise the worker.

        Args:
            worker_id: Unique identifier for this worker (auto-generated if None).
            instance_manager: Manager for instance persistence.
            registry: Action registry for workflow execution.
            definition_loader: Function to load workflow definitions by ID.
            poll_interval_seconds: Time between polling cycles.
            batch_size: Maximum concurrent instances.
            heartbeat_interval_seconds: Interval for lock heartbeat.
            max_transitions: Maximum state transitions per instance.
        """
        self._worker_id = worker_id or self._generate_worker_id()
        self._instance_manager = instance_manager
        self._registry = registry
        self._definition_loader = definition_loader
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._heartbeat_interval = heartbeat_interval_seconds

        self._executor = DurableWorkflowExecutor(
            registry=registry,
            instance_manager=instance_manager,
            max_transitions=max_transitions,
        )

        self._running = False
        self._shutdown_event = threading.Event()
        self._current_instances: dict[str, threading.Thread] = {}
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

    @property
    def worker_id(self) -> str:
        """Return the worker identifier."""
        return self._worker_id

    @property
    def is_running(self) -> bool:
        """Return True if the worker is running."""
        return self._running

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
            available_slots = self._batch_size - len(self._current_instances)

        if available_slots <= 0:
            return

        # Claim instances
        for _ in range(available_slots):
            if not self._running:
                break

            instance = self._instance_manager.find_and_claim_instance(self._worker_id)
            if instance is None:
                break

            # Start processing thread
            with self._lock:
                if instance.instance_id in self._current_instances:
                    continue

                thread = threading.Thread(
                    target=self._process_instance,
                    args=(instance,),
                    name=f"{self._worker_id}_{instance.instance_id[:8]}",
                    daemon=True,
                )
                self._current_instances[instance.instance_id] = thread
                thread.start()

    def _process_instance(self, instance: WorkflowInstance) -> None:
        """Process a single workflow instance.

        Args:
            instance: The instance to process.
        """
        instance_id = instance.instance_id
        logger.info(
            "[durable_worker] Processing instance %s (workflow=%s)",
            instance_id,
            instance.workflow_id,
        )

        def _best_effort_mark_failed(
            *,
            error: str,
            error_step: str | None = None,
            increment_retry: bool = True,
        ) -> None:
            try:
                self._instance_manager.mark_failed(
                    instance_id,
                    error=error,
                    error_step=error_step,
                    increment_retry=increment_retry,
                )
            except Exception:
                logger.exception(
                    "[durable_worker] Failed to persist FAILED status for %s",
                    instance_id,
                )

        def _best_effort_release_lock() -> None:
            try:
                self._instance_manager.release_lock(instance_id, self._worker_id)
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

            # Load workflow definition
            definition = self._definition_loader(instance.workflow_id)
            if definition is None:
                error = f"workflow_definition_not_found:{instance.workflow_id}"
                _best_effort_mark_failed(
                    error=error,
                    increment_retry=False,
                )
                if self._on_instance_failed:
                    try:
                        self._on_instance_failed(instance_id, error)
                    except Exception:
                        pass
                return

            # Execute with checkpointing
            result = self._executor.run_durable(
                instance_id,
                definition,
                worker_id=self._worker_id,
                resume_from_checkpoint=True,
            )

            # Update status based on result
            if result.completed:
                self._instance_manager.mark_completed(
                    instance_id,
                    outputs=result.data,
                    final_state=result.final_state,
                )
                if self._on_instance_completed:
                    try:
                        self._on_instance_completed(instance_id, result)
                    except Exception:
                        pass
                logger.info("[durable_worker] Instance %s completed", instance_id)

            elif result.error == "cancelled":
                # Already marked as cancelled
                logger.info("[durable_worker] Instance %s was cancelled", instance_id)

            else:
                _best_effort_mark_failed(
                    error=result.error or "unknown_error",
                    error_step=result.final_state,
                )
                if self._on_instance_failed:
                    try:
                        self._on_instance_failed(instance_id, result.error or "unknown")
                    except Exception:
                        pass
                logger.warning(
                    "[durable_worker] Instance %s failed: %s",
                    instance_id,
                    result.error,
                )

        except Exception as e:
            logger.exception(
                "[durable_worker] Unexpected error processing %s", instance_id
            )
            _best_effort_mark_failed(
                error=f"worker_exception:{e}",
            )
            if self._on_instance_failed:
                try:
                    self._on_instance_failed(instance_id, str(e))
                except Exception:
                    pass

        finally:
            with self._lock:
                self._current_instances.pop(instance_id, None)
            # Stop heartbeat extension before best-effort unlock so a dead worker
            # thread cannot keep a stale RUNNING row alive after teardown starts.
            _best_effort_release_lock()

    def _heartbeat_loop(self) -> None:
        """Periodically extend locks on active instances."""
        while self._running:
            self._shutdown_event.wait(timeout=self._heartbeat_interval)
            if not self._running:
                break

            with self._lock:
                instance_ids = list(self._current_instances.keys())

            for instance_id in instance_ids:
                try:
                    self._instance_manager.extend_lock(
                        instance_id,
                        self._worker_id,
                        extend_seconds=int(self._heartbeat_interval * 2),
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
