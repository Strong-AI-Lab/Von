"""Startup integration for durable workflow system.

Provides factory functions and startup hooks for integrating the durable
workflow system with the Von application server.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .instance_manager import WorkflowInstanceManager
from .durable_executor import DurableWorkflowExecutor
from .worker import DurableWorkflowWorker
from .scheduler import WorkflowScheduler

logger = logging.getLogger(__name__)


# Module-level singleton instances (lazily initialised)
_instance_manager: WorkflowInstanceManager | None = None
_worker: DurableWorkflowWorker | None = None
_scheduler: WorkflowScheduler | None = None


def get_instance_manager() -> WorkflowInstanceManager:
    """Get or create the global WorkflowInstanceManager.

    Returns:
        The singleton instance manager.
    """
    global _instance_manager
    if _instance_manager is None:
        _instance_manager = WorkflowInstanceManager()
    return _instance_manager


def create_durable_executor(
    registry: Any,  # ActionRegistry - using Any to avoid circular import
    instance_manager: WorkflowInstanceManager | None = None,
    max_transitions: int = 50,
) -> DurableWorkflowExecutor:
    """Create a DurableWorkflowExecutor.

    Args:
        registry: The action registry for workflow execution.
        instance_manager: Optional instance manager (uses singleton if None).
        max_transitions: Maximum state transitions per workflow.

    Returns:
        Configured executor.
    """
    mgr = instance_manager or get_instance_manager()
    return DurableWorkflowExecutor(
        registry=registry,
        instance_manager=mgr,
        max_transitions=max_transitions,
    )


def create_worker(
    registry: Any,  # ActionRegistry
    definition_loader: Callable[[str], Any],  # WorkflowDefinitionLoader
    *,
    instance_manager: WorkflowInstanceManager | None = None,
    worker_id: str | None = None,
    poll_interval: float = 5.0,
    batch_size: int = 5,
) -> DurableWorkflowWorker:
    """Create a DurableWorkflowWorker.

    Args:
        registry: The action registry for workflow execution.
        definition_loader: Function to load workflow definitions by ID.
        instance_manager: Optional instance manager (uses singleton if None).
        worker_id: Optional worker identifier.
        poll_interval: Seconds between polling cycles.
        batch_size: Maximum concurrent instances.

    Returns:
        Configured worker (not yet started).
    """
    mgr = instance_manager or get_instance_manager()
    return DurableWorkflowWorker(
        worker_id=worker_id,
        instance_manager=mgr,
        registry=registry,
        definition_loader=definition_loader,
        poll_interval_seconds=poll_interval,
        batch_size=batch_size,
    )


def create_scheduler(
    instance_manager: WorkflowInstanceManager | None = None,
    check_interval: float = 60.0,
) -> WorkflowScheduler:
    """Create a WorkflowScheduler.

    Args:
        instance_manager: Optional instance manager (uses singleton if None).
        check_interval: Seconds between schedule checks.

    Returns:
        Configured scheduler (not yet started).
    """
    mgr = instance_manager or get_instance_manager()
    return WorkflowScheduler(mgr, check_interval_seconds=check_interval)


def start_worker_and_scheduler(
    registry: Any,
    definition_loader: Callable[[str], Any],
    *,
    instance_manager: WorkflowInstanceManager | None = None,
    enable_worker: bool = True,
    enable_scheduler: bool = True,
    worker_poll_interval: float = 5.0,
    scheduler_check_interval: float = 60.0,
) -> dict[str, Any]:
    """Start the durable workflow worker and scheduler.

    Convenience function for application startup.

    Args:
        registry: The action registry.
        definition_loader: Function to load workflow definitions.
        instance_manager: Optional instance manager.
        enable_worker: Whether to start the worker.
        enable_scheduler: Whether to start the scheduler.
        worker_poll_interval: Worker poll interval.
        scheduler_check_interval: Scheduler check interval.

    Returns:
        Dict with 'worker' and 'scheduler' keys (values may be None).
    """
    global _worker, _scheduler
    mgr = instance_manager or get_instance_manager()

    result: dict[str, Any] = {"worker": None, "scheduler": None}

    if enable_worker:
        _worker = create_worker(
            registry,
            definition_loader,
            instance_manager=mgr,
            poll_interval=worker_poll_interval,
        )
        _worker.start_background()
        result["worker"] = _worker
        logger.info("[durable_startup] Started workflow worker: %s", _worker.worker_id)

    if enable_scheduler:
        _scheduler = create_scheduler(mgr, check_interval=scheduler_check_interval)
        _scheduler.start_background()
        result["scheduler"] = _scheduler
        logger.info("[durable_startup] Started workflow scheduler")

    return result


def stop_worker_and_scheduler(timeout: float = 30.0) -> None:
    """Stop the global worker and scheduler.

    Args:
        timeout: Maximum time to wait for graceful shutdown.
    """
    global _worker, _scheduler

    if _scheduler is not None:
        logger.info("[durable_startup] Stopping scheduler...")
        _scheduler.stop()
        _scheduler = None

    if _worker is not None:
        logger.info("[durable_startup] Stopping worker...")
        _worker.stop(timeout=timeout)
        _worker = None


def recover_orphaned_instances(
    worker_id: str | None = None,
    *,
    max_stale_hours: int | None = None,
) -> int:
    """Recover instances that were orphaned during previous shutdown.

    This should be called during application startup to handle any
    instances that were left in RUNNING state with expired locks.

    Args:
        worker_id: Worker ID for logging (informational only).
        max_stale_hours: Optional maximum age of instances to recover. When
            omitted, recover all expired running instances regardless of age.

    Returns:
        Number of instances recovered.
    """
    from datetime import datetime, timedelta, timezone
    from ...db.mongo_client import get_db
    from .instance_manager import WORKFLOW_INSTANCES_COLLECTION
    from .models import WorkflowInstanceStatus

    db = get_db()
    if db is None:
        return 0

    coll = db[WORKFLOW_INSTANCES_COLLECTION]
    now = datetime.now(timezone.utc)
    # Find running instances with expired locks
    query: dict[str, Any] = {
        "status": WorkflowInstanceStatus.RUNNING.value,
        "lock_expires_at": {"$lt": now},
    }
    if isinstance(max_stale_hours, int) and max_stale_hours > 0:
        stale_cutoff = now - timedelta(hours=max_stale_hours)
        query["started_at"] = {"$gt": stale_cutoff}

    result = coll.update_many(
        query,
        {
            "$set": {
                "status": WorkflowInstanceStatus.PAUSED.value,
                "locked_by": None,
                "lock_expires_at": None,
            }
        },
    )

    count = result.modified_count
    if count > 0:
        logger.info(
            "[durable_startup] Recovered %d orphaned workflow instances",
            count,
        )

    return count


def get_system_status() -> dict[str, Any]:
    """Get the status of the durable workflow system.

    Returns:
        Dict with status information.
    """
    from datetime import datetime, timezone
    from ...db.mongo_client import get_db
    from .instance_manager import (
        WORKFLOW_INSTANCES_COLLECTION,
        WORKFLOW_SCHEDULES_COLLECTION,
    )

    db = get_db()
    status: dict[str, Any] = {
        "database_connected": db is not None,
        "worker_running": _worker is not None and _worker.is_running,
        "scheduler_running": _scheduler is not None and _scheduler.is_running,
        "worker_id": _worker.worker_id if _worker else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    if db is not None:
        try:
            instances_coll = db[WORKFLOW_INSTANCES_COLLECTION]
            schedules_coll = db[WORKFLOW_SCHEDULES_COLLECTION]

            # Count instances by status
            pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
            status_counts = {
                doc["_id"]: doc["count"] for doc in instances_coll.aggregate(pipeline)
            }
            status["instances"] = {
                "pending": status_counts.get("pending", 0),
                "running": status_counts.get("running", 0),
                "completed": status_counts.get("completed", 0),
                "failed": status_counts.get("failed", 0),
                "cancelled": status_counts.get("cancelled", 0),
                "paused": status_counts.get("paused", 0),
            }

            # Count schedules
            status["schedules"] = {
                "total": schedules_coll.count_documents({}),
                "enabled": schedules_coll.count_documents({"enabled": True}),
            }
        except Exception as e:
            status["error"] = str(e)

    return status
