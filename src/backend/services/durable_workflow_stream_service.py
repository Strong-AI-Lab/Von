"""SSE streaming for durable workflow status updates.

Provides a subscriber registry and event broadcaster for workflow instance
status changes and progress updates.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generator, Optional

from src.backend.workflows.durable.models import WorkflowInstance

logger = logging.getLogger(__name__)


@dataclass
class WorkflowStatusEvent:
    """Represents a workflow status update for SSE broadcast."""

    instance_id: str
    workflow_id: str
    status: str
    current_state: str
    step_index: int
    progress_current: int | None = None
    progress_total: int | None = None
    progress_message: str | None = None
    progress_updated_at: str | None = None
    user_id: str | None = None
    org_id: str | None = None
    namespace: str | None = None
    updated_at: str | None = None
    event_type: str = "workflow_status"

    def to_payload(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "current_state": self.current_state,
            "step_index": self.step_index,
            "progress": {
                "current": self.progress_current,
                "total": self.progress_total,
                "message": self.progress_message,
                "updated_at": self.progress_updated_at,
            },
            "user_id": self.user_id,
            "org_id": self.org_id,
            "namespace": self.namespace,
            "updated_at": self.updated_at,
        }


@dataclass
class WorkflowSubscriber:
    """A connected SSE subscriber for workflow status updates."""

    subscriber_id: str
    user_id: str | None = None
    org_id: str | None = None
    namespace: str | None = None
    workflow_id: str | None = None
    instance_id: str | None = None
    statuses: set[str] | None = None
    event_queue: "queue.Queue[Optional[WorkflowStatusEvent]]" = field(
        default_factory=lambda: queue.Queue(maxsize=200)
    )
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def matches(self, event: WorkflowStatusEvent) -> bool:
        if self.instance_id and self.instance_id != event.instance_id:
            return False
        if self.workflow_id and self.workflow_id != event.workflow_id:
            return False
        if self.user_id and self.user_id != event.user_id:
            return False
        if self.org_id and self.org_id != event.org_id:
            return False
        if self.namespace and self.namespace != event.namespace:
            return False
        if self.statuses and event.status not in self.statuses:
            return False
        return True


class DurableWorkflowStreamService:
    """Registry and broadcaster for workflow status SSE streams."""

    _instance: Optional["DurableWorkflowStreamService"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._subscribers: list[WorkflowSubscriber] = []
        self._subscribers_lock = threading.Lock()
        self._keepalive_interval_seconds = 30
        self._keepalive_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()

    @classmethod
    def get_instance(cls) -> "DurableWorkflowStreamService":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    cls._instance._start_keepalive_thread()
        return cls._instance

    def _start_keepalive_thread(self) -> None:
        if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
            return

        def keepalive_loop() -> None:
            while not self._shutdown_event.is_set():
                try:
                    self._send_keepalives()
                except Exception as exc:
                    logger.debug("[workflow_stream] Keepalive error: %s", exc)
                self._shutdown_event.wait(timeout=self._keepalive_interval_seconds)

        self._keepalive_thread = threading.Thread(
            target=keepalive_loop, daemon=True, name="WorkflowSSEKeepalive"
        )
        self._keepalive_thread.start()

    def _send_keepalives(self) -> None:
        with self._subscribers_lock:
            for sub in list(self._subscribers):
                try:
                    sub.event_queue.put_nowait(None)
                except queue.Full:
                    logger.debug(
                        "[workflow_stream] Queue full for subscriber %s",
                        sub.subscriber_id,
                    )

    def subscribe(
        self,
        *,
        user_id: str | None = None,
        org_id: str | None = None,
        namespace: str | None = None,
        workflow_id: str | None = None,
        instance_id: str | None = None,
        statuses: set[str] | None = None,
    ) -> WorkflowSubscriber:
        subscriber = WorkflowSubscriber(
            subscriber_id=str(uuid.uuid4()),
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            workflow_id=workflow_id,
            instance_id=instance_id,
            statuses=statuses,
        )
        with self._subscribers_lock:
            self._subscribers.append(subscriber)
            logger.info(
                "[workflow_stream] Subscriber %s connected",
                subscriber.subscriber_id,
            )
        return subscriber

    def unsubscribe(self, subscriber: WorkflowSubscriber) -> None:
        with self._subscribers_lock:
            self._subscribers = [
                sub
                for sub in self._subscribers
                if sub.subscriber_id != subscriber.subscriber_id
            ]
            logger.info(
                "[workflow_stream] Subscriber %s disconnected",
                subscriber.subscriber_id,
            )

    def broadcast(self, event: WorkflowStatusEvent) -> int:
        notified = 0
        with self._subscribers_lock:
            for sub in list(self._subscribers):
                if not sub.matches(event):
                    continue
                try:
                    sub.event_queue.put_nowait(event)
                    notified += 1
                except queue.Full:
                    logger.warning(
                        "[workflow_stream] Queue full for subscriber %s",
                        sub.subscriber_id,
                    )
        return notified

    def generate_events(
        self, subscriber: WorkflowSubscriber, *, timeout: float = 60.0
    ) -> Generator[str, None, None]:
        while True:
            try:
                event = subscriber.event_queue.get(timeout=timeout)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue

            if event is None:
                yield ": keepalive\n\n"
                continue

            payload = event.to_payload()
            yield f"event: {event.event_type}\ndata: {json.dumps(payload)}\n\n"

    def shutdown(self) -> None:
        self._shutdown_event.set()
        with self._subscribers_lock:
            for sub in self._subscribers:
                try:
                    sub.event_queue.put_nowait(None)
                except queue.Full:
                    pass
            self._subscribers.clear()


def _event_from_instance(instance: WorkflowInstance) -> WorkflowStatusEvent:
    updated_at = datetime.now(timezone.utc).isoformat()
    return WorkflowStatusEvent(
        instance_id=instance.instance_id,
        workflow_id=instance.workflow_id,
        status=instance.status.value,
        current_state=instance.current_state,
        step_index=instance.step_index,
        progress_current=instance.progress_current,
        progress_total=instance.progress_total,
        progress_message=instance.progress_message,
        progress_updated_at=(
            instance.progress_updated_at.isoformat()
            if instance.progress_updated_at
            else None
        ),
        user_id=instance.user_id,
        org_id=instance.org_id,
        namespace=instance.namespace,
        updated_at=updated_at,
    )


def get_workflow_stream_service() -> DurableWorkflowStreamService:
    return DurableWorkflowStreamService.get_instance()


def broadcast_workflow_instance(instance: WorkflowInstance) -> int:
    """Broadcast a workflow instance status update."""
    event = _event_from_instance(instance)
    return get_workflow_stream_service().broadcast(event)
