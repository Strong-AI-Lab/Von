"""Focused worker regression tests for stale-running recovery hardening."""

from __future__ import annotations

from threading import Thread
from types import SimpleNamespace
from typing import Any, cast

from src.backend.workflows.action_registry import ActionRegistry
from src.backend.workflows.durable.models import (
    WorkflowInstance,
    WorkflowInstanceStatus,
)
from src.backend.workflows.durable.worker import DurableWorkflowWorker


class _WorkerManagerStub:
    def __init__(self) -> None:
        self.release_lock_calls: list[tuple[str, str]] = []
        self.mark_failed_calls: list[dict[str, Any]] = []
        self.release_lock_error: Exception | None = None
        self.mark_failed_error: Exception | None = None

    def release_lock(self, instance_id: str, worker_id: str) -> bool:
        self.release_lock_calls.append((instance_id, worker_id))
        if self.release_lock_error is not None:
            raise self.release_lock_error
        return True

    def mark_failed(self, instance_id: str, **kwargs: Any) -> bool:
        call = {"instance_id": instance_id}
        call.update(kwargs)
        self.mark_failed_calls.append(call)
        if self.mark_failed_error is not None:
            raise self.mark_failed_error
        return True


def _build_instance() -> WorkflowInstance:
    instance = WorkflowInstance.create(
        "#V#workflow_introspection_maintenance_workflow",
        user_id="#V#user",
        org_id="#V#org",
        namespace="#V#user@org",
    )
    instance.status = WorkflowInstanceStatus.RUNNING
    return instance


def test_worker_cleanup_removes_tracking_even_if_release_lock_fails() -> None:
    manager = _WorkerManagerStub()
    manager.release_lock_error = RuntimeError("mongo_timeout")
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=lambda _workflow_id: None,
    )
    instance = _build_instance()
    worker._current_instances[instance.instance_id] = Thread()

    worker._process_instance(instance)

    assert instance.instance_id not in worker._current_instances
    assert manager.release_lock_calls == [(instance.instance_id, "worker-1548")]
    assert manager.mark_failed_calls
    assert manager.mark_failed_calls[0]["increment_retry"] is False


def test_worker_swallows_mark_failed_errors_during_exception_path() -> None:
    manager = _WorkerManagerStub()
    manager.mark_failed_error = RuntimeError("mongo_timeout")
    worker = DurableWorkflowWorker(
        worker_id="worker-1548",
        instance_manager=manager,  # type: ignore[arg-type]
        registry=ActionRegistry(),
        definition_loader=cast(
            Any,
            lambda _workflow_id: SimpleNamespace(workflow_id=_workflow_id),
        ),
    )
    instance = _build_instance()
    worker._current_instances[instance.instance_id] = Thread()
    worker._executor = cast(
        Any,
        SimpleNamespace(
            run_durable=lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("executor blew up")
            )
        ),
    )

    worker._process_instance(instance)

    assert instance.instance_id not in worker._current_instances
    assert manager.release_lock_calls == [(instance.instance_id, "worker-1548")]
    assert manager.mark_failed_calls
    assert manager.mark_failed_calls[0]["error"].startswith("worker_exception:")
