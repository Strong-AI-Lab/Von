"""Workflow instance management with MongoDB persistence.

Provides CRUD operations, atomic locking, and checkpoint support for
durable workflow instances.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, OperationFailure

from ...db.mongo_client import get_db
from ...services.workflow_episode_service import (
    build_workflow_episode_stable_key,
    finalise_workflow_use_episode,
    start_workflow_use_episode,
)
from ...services.workflow_payload_store import (
    compact_workflow_payload_for_storage,
    hydrate_workflow_payload_blob_refs,
)
from .models import (
    EventWorkflowBinding,
    WorkflowInstance,
    WorkflowInstanceStatus,
    WorkflowSchedule,
)
from .vontology_schedule_repository import VontologyScheduleRepository

logger = logging.getLogger(__name__)

WORKFLOW_INSTANCES_COLLECTION = "workflow_instances"
WORKFLOW_SCHEDULES_COLLECTION = "workflow_schedules"
WORKFLOW_EVENT_BINDINGS_COLLECTION = "workflow_event_bindings"

# Default lock TTL: 5 minutes
DEFAULT_LOCK_TTL_SECONDS = 300

_WORKFLOW_INSTANCE_STATUS_SUMMARY_PROJECTION: dict[str, Any] = {
    "_id": 0,
    "instance_id": 1,
    "workflow_id": 1,
    "status": 1,
    "current_state": 1,
    "step_index": 1,
    "created_at": 1,
    "started_at": 1,
    "completed_at": 1,
    "progress_current": 1,
    "progress_total": 1,
    "progress_message": 1,
    "progress_updated_at": 1,
    "error": 1,
    "retry_count": 1,
    "max_retries": 1,
    "source_event_type": 1,
    "source_event_id": 1,
    "event_idempotency_key": 1,
    "execution_trace_id": 1,
    "has_outputs": {"$ne": [{"$ifNull": ["$outputs", None]}, None]},
}

# Default completed instance TTL: 30 days
DEFAULT_COMPLETED_TTL_SECONDS = 30 * 24 * 60 * 60

_indexes_ensured = False


def _parse_datetime_filter(value: datetime | str | None) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        normalised = cleaned.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _ensure_indexes() -> None:
    """Create indexes for workflow collections (idempotent, best-effort)."""
    global _indexes_ensured
    if _indexes_ensured:
        return

    db = get_db()
    if db is None:
        return

    try:
        # Workflow instances collection
        instances_coll = db[WORKFLOW_INSTANCES_COLLECTION]
        existing = [idx["name"] for idx in instances_coll.list_indexes()]

        # Primary lookup by instance_id
        if "instance_id_unique" not in existing:
            instances_coll.create_index(
                [("instance_id", ASCENDING)],
                unique=True,
                name="instance_id_unique",
            )

        # Worker polling: find claimable instances
        if "status_lock_expires" not in existing:
            instances_coll.create_index(
                [("status", ASCENDING), ("lock_expires_at", ASCENDING)],
                name="status_lock_expires",
            )
        if "started_status_created_instance_lock" not in existing:
            instances_coll.create_index(
                [
                    ("started_at", ASCENDING),
                    ("status", ASCENDING),
                    ("created_at", ASCENDING),
                    ("instance_id", ASCENDING),
                    ("lock_expires_at", ASCENDING),
                ],
                name="started_status_created_instance_lock",
            )

        # User queries
        if "created_at_desc" not in existing:
            instances_coll.create_index(
                [("created_at", DESCENDING)],
                name="created_at_desc",
            )

        if "user_created" not in existing:
            instances_coll.create_index(
                [("user_id", ASCENDING), ("created_at", DESCENDING)],
                name="user_created",
            )

        if "org_status" not in existing:
            instances_coll.create_index(
                [("org_id", ASCENDING), ("status", ASCENDING)],
                name="org_status",
            )

        if "namespace_status_created" not in existing:
            instances_coll.create_index(
                [
                    ("namespace", ASCENDING),
                    ("status", ASCENDING),
                    ("created_at", DESCENDING),
                ],
                name="namespace_status_created",
            )
        if "conversation_turn_namespace_created" not in existing:
            instances_coll.create_index(
                [
                    ("inputs.conversation_session_id", ASCENDING),
                    ("inputs.turn_id", ASCENDING),
                    ("namespace", ASCENDING),
                    ("created_at", DESCENDING),
                ],
                name="conversation_turn_namespace_created",
            )

        # Schedule association
        if "schedule_created" not in existing:
            instances_coll.create_index(
                [("schedule_id", ASCENDING), ("created_at", DESCENDING)],
                name="schedule_created",
            )

        # Cadence/rate-limit lookups: find newest instance per workflow quickly.
        if "workflow_created" not in existing:
            instances_coll.create_index(
                [("workflow_id", ASCENDING), ("created_at", DESCENDING)],
                name="workflow_created",
            )

        # Event-driven observability: find workflow instances for a source event.
        if "source_event_lookup" not in existing:
            instances_coll.create_index(
                [
                    ("source_event_type", ASCENDING),
                    ("source_event_id", ASCENDING),
                    ("created_at", DESCENDING),
                ],
                name="source_event_lookup",
            )

        # Idempotency for event-triggered launches.
        if "event_idempotency_key_unique" not in existing:
            instances_coll.create_index(
                [("event_idempotency_key", ASCENDING)],
                unique=True,
                name="event_idempotency_key_unique",
                partialFilterExpression={"event_idempotency_key": {"$exists": True}},
            )

        # TTL for completed instances (30 days)
        if "completed_ttl" not in existing:
            instances_coll.create_index(
                [("completed_at", ASCENDING)],
                name="completed_ttl",
                expireAfterSeconds=DEFAULT_COMPLETED_TTL_SECONDS,
                partialFilterExpression={"status": "completed"},
            )

        # Workflow schedules collection
        schedules_coll = db[WORKFLOW_SCHEDULES_COLLECTION]
        sched_existing = [idx["name"] for idx in schedules_coll.list_indexes()]

        if "schedule_id_unique" not in sched_existing:
            schedules_coll.create_index(
                [("schedule_id", ASCENDING)],
                unique=True,
                name="schedule_id_unique",
            )

        if "enabled_next_run" not in sched_existing:
            schedules_coll.create_index(
                [("enabled", ASCENDING), ("next_run_at", ASCENDING)],
                name="enabled_next_run",
            )

        if "user_schedules" not in sched_existing:
            schedules_coll.create_index(
                [("user_id", ASCENDING), ("created_at", DESCENDING)],
                name="user_schedules",
            )

        # Event-workflow bindings collection
        event_bindings_coll = db[WORKFLOW_EVENT_BINDINGS_COLLECTION]
        event_existing = [idx["name"] for idx in event_bindings_coll.list_indexes()]

        # One binding per (event_type, workflow_id). This allows multiple
        # workflows for one event while keeping duplicate registration idempotent.
        if "event_workflow_binding_unique" not in event_existing:
            event_bindings_coll.create_index(
                [("event_type", ASCENDING), ("workflow_id", ASCENDING)],
                unique=True,
                name="event_workflow_binding_unique",
            )

        if "event_enabled_lookup" not in event_existing:
            event_bindings_coll.create_index(
                [("event_type", ASCENDING), ("enabled", ASCENDING)],
                name="event_enabled_lookup",
            )

        if "binding_id_unique" not in event_existing:
            event_bindings_coll.create_index(
                [("binding_id", ASCENDING)],
                unique=True,
                name="binding_id_unique",
            )

    except OperationFailure as e:
        logger.warning("Could not create some workflow indexes: %s", e)
    except Exception as e:
        logger.warning("Index creation skipped for workflow collections: %s", e)
    finally:
        _indexes_ensured = True


class WorkflowInstanceManager:
    """Manages workflow instance lifecycle with MongoDB persistence.

    Provides:
    - Instance creation and retrieval
    - Atomic locking for distributed workers
    - Checkpoint persistence
    - Status transitions
    """

    def __init__(self, lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS) -> None:
        self._lock_ttl = lock_ttl_seconds
        self._schedule_repo = VontologyScheduleRepository()
        _ensure_indexes()

    def _get_instances_collection(self) -> Collection | None:
        """Get the workflow_instances collection."""
        db = get_db()
        return db[WORKFLOW_INSTANCES_COLLECTION] if db is not None else None

    @staticmethod
    def _normalise_status_filter(
        status: WorkflowInstanceStatus
        | str
        | Iterable[WorkflowInstanceStatus | str]
        | None,
    ) -> list[str]:
        if status is None:
            return []
        if isinstance(status, WorkflowInstanceStatus):
            return [status.value]
        if isinstance(status, str):
            value = status.strip().lower()
            return [value] if value else []

        values: list[str] = []
        for raw_status in status:
            if isinstance(raw_status, WorkflowInstanceStatus):
                value = raw_status.value
            else:
                value = str(raw_status or "").strip().lower()
            if value and value not in values:
                values.append(value)
        return values

    @staticmethod
    def _monitor_snapshot_should_reserve_active_workflows(
        status_values: list[str],
    ) -> bool:
        """Reserve running or paused visibility when pending backlog is present."""
        if not status_values:
            return False
        return WorkflowInstanceStatus.PENDING.value in status_values and (
            WorkflowInstanceStatus.RUNNING.value in status_values
            or WorkflowInstanceStatus.PAUSED.value in status_values
        )

    @staticmethod
    def _monitor_snapshot_status_order(status_values: list[str]) -> list[str]:
        """Order statuses for bounded monitor snapshots."""
        ordered: list[str] = []
        for status_value in (
            WorkflowInstanceStatus.RUNNING.value,
            WorkflowInstanceStatus.PAUSED.value,
            WorkflowInstanceStatus.PENDING.value,
            WorkflowInstanceStatus.COMPLETED.value,
            WorkflowInstanceStatus.FAILED.value,
            WorkflowInstanceStatus.CANCELLED.value,
        ):
            if status_value in status_values and status_value not in ordered:
                ordered.append(status_value)
        for status_value in status_values:
            if status_value not in ordered:
                ordered.append(status_value)
        return ordered

    @staticmethod
    def _build_exact_status_query(
        base_query: dict[str, Any], *, status_value: str
    ) -> dict[str, Any]:
        query = dict(base_query)
        query["status"] = status_value
        return query

    @staticmethod
    def _monitor_snapshot_workflow_key(doc: dict[str, Any]) -> str:
        workflow_id = str(doc.get("workflow_id") or "").strip()
        return workflow_id or "__unknown_workflow__"

    def _build_instance_list_query(
        self,
        *,
        user_id: str | None = None,
        org_id: str | None = None,
        namespace: str | None = None,
        status: WorkflowInstanceStatus
        | str
        | Iterable[WorkflowInstanceStatus | str]
        | None = None,
        workflow_id: str | None = None,
        source_event_type: str | None = None,
        source_event_id: str | None = None,
        conversation_session_id: str | None = None,
        request_id: str | None = None,
        from_utc: datetime | str | None = None,
        to_utc: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Build the shared query for workflow-instance list operations."""
        query: dict[str, Any] = {}
        if user_id:
            query["user_id"] = user_id
        if org_id:
            query["org_id"] = org_id
        if namespace:
            query["namespace"] = namespace

        status_values = self._normalise_status_filter(status)
        if status_values:
            query["status"] = (
                status_values[0] if len(status_values) == 1 else {"$in": status_values}
            )

        if workflow_id:
            query["workflow_id"] = workflow_id
        if source_event_type:
            query["source_event_type"] = source_event_type
        if source_event_id:
            query["source_event_id"] = source_event_id
        if conversation_session_id:
            query["inputs.conversation_session_id"] = conversation_session_id
        if request_id:
            query["inputs.turn_id"] = request_id

        created_range: dict[str, Any] = {}
        from_dt = _parse_datetime_filter(from_utc)
        to_dt = _parse_datetime_filter(to_utc)
        if from_dt is not None:
            created_range["$gte"] = from_dt
        if to_dt is not None:
            created_range["$lte"] = to_dt
        if created_range:
            query["created_at"] = created_range

        return query

    def _get_schedules_collection(self) -> Collection | None:
        """Get the workflow_schedules collection."""
        db = get_db()
        return db[WORKFLOW_SCHEDULES_COLLECTION] if db is not None else None

    def _get_event_bindings_collection(self) -> Collection | None:
        """Get the workflow_event_bindings collection."""
        db = get_db()
        return db[WORKFLOW_EVENT_BINDINGS_COLLECTION] if db is not None else None

    def _broadcast_instance(self, instance: WorkflowInstance) -> None:
        try:
            from ...services.durable_workflow_stream_service import (
                broadcast_workflow_instance,
            )

            broadcast_workflow_instance(instance)
        except Exception as exc:
            logger.debug("[durable_workflow] Broadcast skipped: %s", exc)

    def _broadcast_instance_status(self, instance_id: str) -> None:
        instance = self.get_instance(instance_id)
        if instance is not None:
            self._broadcast_instance(instance)

    @staticmethod
    def _compact_instance_payload_field(
        value: Any,
        *,
        field: str,
        instance_id: str | None,
        namespace: str | None = None,
        workflow_id: str | None = None,
    ) -> Any:
        try:
            result = compact_workflow_payload_for_storage(
                {field: value},
                record_family=f"{WORKFLOW_INSTANCES_COLLECTION}.{field}",
                record_id=instance_id,
                namespace=namespace,
                workflow_id=workflow_id,
                fail_soft=False,
            )
        except Exception as exc:
            logger.warning(
                "[durable_workflow] Workflow payload blob compaction skipped for %s.%s: %s",
                instance_id,
                field,
                exc,
            )
            return value
        if result.offloaded_count <= 0 or not isinstance(result.payload, dict):
            return value
        return result.payload.get(field, value)

    @staticmethod
    def _hydrate_instance_payloads(doc: dict[str, Any]) -> dict[str, Any]:
        hydrated_doc = dict(doc)
        for field in ("inputs", "workflow_data", "outputs"):
            if field not in hydrated_doc or hydrated_doc[field] is None:
                continue
            hydrated = hydrate_workflow_payload_blob_refs(
                hydrated_doc[field],
                fail_soft=True,
            )
            hydrated_doc[field] = hydrated.payload
        return hydrated_doc

    @classmethod
    def _instance_from_doc(
        cls,
        doc: dict[str, Any] | None,
        *,
        hydrate_payloads: bool = False,
    ) -> WorkflowInstance | None:
        if doc is None:
            return None
        if hydrate_payloads:
            doc = cls._hydrate_instance_payloads(doc)
        return WorkflowInstance.from_doc(doc)

    def _find_one_and_update_instance(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        *,
        return_document: bool = True,
    ) -> WorkflowInstance | None:
        """Apply an atomic instance update and decode the matched document."""
        coll = self._get_instances_collection()
        if coll is None:
            return None
        doc = coll.find_one_and_update(
            query,
            update,
            return_document=return_document,
        )
        return self._instance_from_doc(doc)

    @staticmethod
    def _build_durable_episode_stable_key(
        *,
        workflow_id: str,
        instance_id: str,
        retry_count: int,
    ) -> str:
        attempt_number = max(1, int(retry_count) + 1)
        return build_workflow_episode_stable_key(
            workflow_id=workflow_id,
            source="durable_instance",
            instance_id=instance_id,
            attempt_number=attempt_number,
        )

    def _record_durable_episode_start(
        self,
        *,
        instance: WorkflowInstance,
        worker_id: str | None,
    ) -> None:
        try:
            stable_key = self._build_durable_episode_stable_key(
                workflow_id=instance.workflow_id,
                instance_id=instance.instance_id,
                retry_count=instance.retry_count,
            )
            start_workflow_use_episode(
                workflow_id=instance.workflow_id,
                source="durable_instance",
                namespace=instance.namespace,
                user_id=instance.user_id,
                org_id=instance.org_id,
                instance_id=instance.instance_id,
                stable_key=stable_key,
                metadata={
                    "status": instance.status.value,
                    "worker_id": worker_id,
                    "retry_count": int(instance.retry_count),
                },
            )
        except Exception as exc:
            logger.debug(
                "[durable_workflow] Episode start skipped for %s: %s",
                instance.instance_id,
                exc,
            )

    def _record_durable_episode_final(
        self,
        *,
        instance: WorkflowInstance,
        completed: bool,
        terminal_stage: str | None,
        termination_code: str | None,
        termination_detail: str | None,
        final_state: str | None = None,
    ) -> None:
        try:
            stable_key = self._build_durable_episode_stable_key(
                workflow_id=instance.workflow_id,
                instance_id=instance.instance_id,
                retry_count=instance.retry_count,
            )
            finalise_workflow_use_episode(
                workflow_id=instance.workflow_id,
                stable_key=stable_key,
                completed=completed,
                terminal_stage=terminal_stage,
                final_state=final_state,
                termination_code=termination_code,
                termination_detail=termination_detail,
                metadata={
                    "instance_id": instance.instance_id,
                    "namespace": instance.namespace,
                    "retry_count": int(instance.retry_count),
                    "status": instance.status.value,
                },
            )
        except Exception as exc:
            logger.debug(
                "[durable_workflow] Episode finalise skipped for %s: %s",
                instance.instance_id,
                exc,
            )

    # -------------------------------------------------------------------------
    # Instance CRUD
    # -------------------------------------------------------------------------

    def create_instance(
        self,
        workflow_id: str,
        *,
        user_id: str,
        org_id: str | None,
        namespace: str,
        inputs: dict[str, Any] | None = None,
        schedule_id: str | None = None,
        max_retries: int = 3,
        source_event_type: str | None = None,
        source_event_id: str | None = None,
        event_idempotency_key: str | None = None,
        auto_claim_enabled: bool = True,
    ) -> str:
        """Create a new workflow instance.

        Args:
            workflow_id: The workflow definition ID.
            user_id: User who initiated the workflow.
            org_id: Organisation context, if the namespace is org-scoped.
            namespace: Full namespace for data access.
            inputs: Initial workflow inputs.
            schedule_id: Optional reference to triggering schedule.
            max_retries: Maximum retry attempts on failure.
            source_event_type: Optional canonical event type that triggered launch.
            source_event_id: Optional source event identifier.
            event_idempotency_key: Optional idempotency key for event replay safety.
            auto_claim_enabled: When False, background workers must never
                claim this instance for execution. Used for mirror/telemetry
                instances whose execution happens elsewhere (e.g. the
                supervised conversation-turn path finalises them itself);
                worker auto-claim of such instances re-executes the same turn
                and produces duplicate user-visible outputs (JVNAUTOSCI-2503).

        Returns:
            The generated instance_id.

        Raises:
            RuntimeError: If database is unavailable.
        """
        coll = self._get_instances_collection()
        if coll is None:
            raise RuntimeError("Database unavailable for workflow instance creation")

        instance = WorkflowInstance.create(
            workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            inputs=inputs,
            schedule_id=schedule_id,
            max_retries=max_retries,
            source_event_type=source_event_type,
            source_event_id=source_event_id,
            event_idempotency_key=event_idempotency_key,
        )

        compacted_inputs = self._compact_instance_payload_field(
            instance.inputs,
            field="inputs",
            instance_id=instance.instance_id,
            namespace=namespace,
            workflow_id=workflow_id,
        )
        if compacted_inputs is not instance.inputs:
            instance = replace(instance, inputs=compacted_inputs)

        instance_doc = instance.to_doc()
        instance_doc["auto_claim_enabled"] = bool(auto_claim_enabled)
        coll.insert_one(instance_doc)
        logger.info(
            "[durable_workflow] Created instance %s for workflow %s "
            "(auto_claim_enabled=%s)",
            instance.instance_id,
            workflow_id,
            bool(auto_claim_enabled),
        )
        self._broadcast_instance(instance)
        return instance.instance_id

    def create_instance_for_event(
        self,
        workflow_id: str,
        *,
        user_id: str,
        org_id: str | None,
        namespace: str,
        event_idempotency_key: str,
        source_event_type: str,
        source_event_id: str,
        inputs: dict[str, Any] | None = None,
        schedule_id: str | None = None,
        max_retries: int = 3,
        auto_claim_enabled: bool = True,
    ) -> tuple[str, bool]:
        """Create an event-triggered instance with idempotency protection.

        Returns:
            (instance_id, created_new)
        """
        coll = self._get_instances_collection()
        if coll is None:
            raise RuntimeError("Database unavailable for workflow instance creation")

        key = event_idempotency_key.strip()
        existing = coll.find_one({"event_idempotency_key": key}, {"instance_id": 1})
        if existing and isinstance(existing.get("instance_id"), str):
            return existing["instance_id"], False

        try:
            instance_id = self.create_instance(
                workflow_id,
                user_id=user_id,
                org_id=org_id,
                namespace=namespace,
                inputs=inputs,
                schedule_id=schedule_id,
                max_retries=max_retries,
                source_event_type=source_event_type,
                source_event_id=source_event_id,
                event_idempotency_key=key,
                auto_claim_enabled=auto_claim_enabled,
            )
            return instance_id, True
        except DuplicateKeyError:
            # Another caller may have inserted concurrently for the same event key.
            existing = coll.find_one({"event_idempotency_key": key}, {"instance_id": 1})
            if existing and isinstance(existing.get("instance_id"), str):
                return existing["instance_id"], False
            raise

    def get_latest_instance_for_workflow(
        self,
        workflow_id: str,
    ) -> WorkflowInstance | None:
        """Return the most recently created instance for ``workflow_id``."""
        workflow_id_clean = str(workflow_id or "").strip()
        if not workflow_id_clean:
            return None

        coll = self._get_instances_collection()
        if coll is None:
            return None

        doc = coll.find_one(
            {"workflow_id": workflow_id_clean},
            sort=[("created_at", DESCENDING)],
        )
        return self._instance_from_doc(doc)

    def get_event_instance(
        self,
        *,
        workflow_id: str,
        source_event_type: str,
        source_event_id: str,
    ) -> WorkflowInstance | None:
        """Return the newest instance for an event/workflow pair, if any."""
        workflow_id_clean = str(workflow_id or "").strip()
        source_event_type_clean = str(source_event_type or "").strip()
        source_event_id_clean = str(source_event_id or "").strip()
        if (
            not workflow_id_clean
            or not source_event_type_clean
            or not source_event_id_clean
        ):
            return None

        coll = self._get_instances_collection()
        if coll is None:
            return None

        doc = coll.find_one(
            {
                "workflow_id": workflow_id_clean,
                "source_event_type": source_event_type_clean,
                "source_event_id": source_event_id_clean,
            },
            sort=[("created_at", DESCENDING)],
        )
        return self._instance_from_doc(doc)

    def get_instance(self, instance_id: str) -> WorkflowInstance | None:
        """Load a workflow instance by ID.

        Args:
            instance_id: The instance identifier.

        Returns:
            WorkflowInstance if found, None otherwise.
        """
        coll = self._get_instances_collection()
        if coll is None:
            return None

        doc = coll.find_one({"instance_id": instance_id})
        return self._instance_from_doc(doc, hydrate_payloads=True)

    def list_instances(
        self,
        *,
        user_id: str | None = None,
        org_id: str | None = None,
        namespace: str | None = None,
        status: WorkflowInstanceStatus
        | str
        | Iterable[WorkflowInstanceStatus | str]
        | None = None,
        workflow_id: str | None = None,
        source_event_type: str | None = None,
        source_event_id: str | None = None,
        conversation_session_id: str | None = None,
        request_id: str | None = None,
        from_utc: datetime | str | None = None,
        to_utc: datetime | str | None = None,
        limit: int = 50,
    ) -> list[WorkflowInstance]:
        """List workflow instances with optional filters.

        Args:
            user_id: Filter by user.
            org_id: Filter by organisation.
            namespace: Filter by namespace.
            status: Filter by status.
            workflow_id: Filter by workflow definition.
            conversation_session_id: Filter by `inputs.conversation_session_id`.
            request_id: Filter by `inputs.turn_id` (request/turn identifier).
            from_utc: Optional lower bound for created_at.
            to_utc: Optional upper bound for created_at.
            limit: Maximum results to return.

        Returns:
            List of matching instances.
        """
        coll = self._get_instances_collection()
        if coll is None:
            return []

        query = self._build_instance_list_query(
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            status=status,
            workflow_id=workflow_id,
            source_event_type=source_event_type,
            source_event_id=source_event_id,
            conversation_session_id=conversation_session_id,
            request_id=request_id,
            from_utc=from_utc,
            to_utc=to_utc,
        )
        cursor = coll.find(query).sort("created_at", -1).limit(limit)
        return [instance for doc in cursor if (instance := self._instance_from_doc(doc))]

    def list_instance_status_dicts(
        self,
        *,
        user_id: str | None = None,
        org_id: str | None = None,
        namespace: str | None = None,
        status: WorkflowInstanceStatus
        | str
        | Iterable[WorkflowInstanceStatus | str]
        | None = None,
        workflow_id: str | None = None,
        source_event_type: str | None = None,
        source_event_id: str | None = None,
        conversation_session_id: str | None = None,
        request_id: str | None = None,
        from_utc: datetime | str | None = None,
        to_utc: datetime | str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List workflow monitor status cards without hydrating full instances."""
        coll = self._get_instances_collection()
        if coll is None:
            return []

        status_values = self._normalise_status_filter(status)
        query = self._build_instance_list_query(
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            status=status,
            workflow_id=workflow_id,
            source_event_type=source_event_type,
            source_event_id=source_event_id,
            conversation_session_id=conversation_session_id,
            request_id=request_id,
            from_utc=from_utc,
            to_utc=to_utc,
        )
        if self._monitor_snapshot_should_reserve_active_workflows(status_values):
            selected_docs: list[dict[str, Any]] = []
            selected_ids: set[str] = set()
            represented_workflows: set[str] = set()

            # Reserve one running or paused row per workflow first so active
            # execution cannot disappear behind a newer pending backlog.
            for status_value in (
                WorkflowInstanceStatus.RUNNING.value,
                WorkflowInstanceStatus.PAUSED.value,
            ):
                if status_value not in status_values:
                    continue
                cursor = coll.find(
                    self._build_exact_status_query(query, status_value=status_value)
                ).sort("created_at", -1)
                for doc in cursor:
                    if len(selected_docs) >= limit:
                        break
                    instance_id = str(doc.get("instance_id") or "").strip()
                    if not instance_id or instance_id in selected_ids:
                        continue
                    workflow_key = self._monitor_snapshot_workflow_key(doc)
                    if workflow_key in represented_workflows:
                        continue
                    selected_docs.append(doc)
                    selected_ids.add(instance_id)
                    represented_workflows.add(workflow_key)
                if len(selected_docs) >= limit:
                    break

            if len(selected_docs) < limit:
                for status_value in self._monitor_snapshot_status_order(status_values):
                    cursor = coll.find(
                        self._build_exact_status_query(query, status_value=status_value)
                    ).sort("created_at", -1)
                    for doc in cursor:
                        if len(selected_docs) >= limit:
                            break
                        instance_id = str(doc.get("instance_id") or "").strip()
                        if not instance_id or instance_id in selected_ids:
                            continue
                        selected_docs.append(doc)
                        selected_ids.add(instance_id)
                    if len(selected_docs) >= limit:
                        break

            return [WorkflowInstance.status_dict_from_doc(doc) for doc in selected_docs]

        pipeline = [
            {"$match": query},
            {"$sort": {"created_at": -1}},
            {"$limit": limit},
            {"$project": dict(_WORKFLOW_INSTANCE_STATUS_SUMMARY_PROJECTION)},
        ]
        return [
            WorkflowInstance.status_dict_from_doc(doc)
            for doc in coll.aggregate(pipeline)
        ]

    # -------------------------------------------------------------------------
    # Locking for distributed workers
    # -------------------------------------------------------------------------

    def find_and_claim_instance(
        self,
        worker_id: str,
        *,
        workflow_ids: list[str] | None = None,
        priority_only: bool = False,
    ) -> WorkflowInstance | None:
        """Atomically find and claim a pending/resumable instance.

        Uses findAndModify to atomically claim an instance, preventing
        race conditions between workers.

        Args:
            worker_id: Identifier for the claiming worker.
            workflow_ids: Optional list of workflow IDs to filter by.
            priority_only: When true, only claim fresh user-turn priority work.

        Returns:
            The claimed instance, or None if none available.
        """
        coll = self._get_instances_collection()
        if coll is None:
            return None

        now = datetime.now(timezone.utc)
        lock_expires = now + timedelta(seconds=self._lock_ttl)

        # Query: pending instances OR running with expired lock.
        # Mirror/telemetry instances (auto_claim_enabled=False) are executed
        # and finalised by their submitting path; claiming them here would
        # re-execute the same turn (JVNAUTOSCI-2503).
        query: dict[str, Any] = {
            "auto_claim_enabled": {"$ne": False},
            "$or": [
                {"status": WorkflowInstanceStatus.PENDING.value},
                {"status": WorkflowInstanceStatus.PAUSED.value},
                {
                    "status": WorkflowInstanceStatus.RUNNING.value,
                    "lock_expires_at": {"$lt": now},
                },
            ]
        }
        if workflow_ids:
            query["workflow_id"] = {"$in": workflow_ids}

        priority_workflow_ids = [
            "#V#conversation_turn_execution_workflow",
            "#V#chat_assistant_workflow",
            "#V#tool_calling_workflow",
        ]
        try:
            conversation_turn_max_age_seconds = max(
                0.0,
                float(
                    os.getenv(
                        "VON_DURABLE_CONVERSATION_TURN_AUTO_CLAIM_MAX_AGE_SECONDS",
                        "86400",
                    )
                ),
            )
        except (TypeError, ValueError):
            conversation_turn_max_age_seconds = 86400.0
        recent_conversation_cutoff = now - timedelta(
            seconds=conversation_turn_max_age_seconds
        )
        conversation_turn_filter: dict[str, Any] = {
            "$or": [
                {"source_event_type": "conversation_turn"},
                {"workflow_id": {"$in": priority_workflow_ids}},
            ]
        }
        stale_conversation_turn_filter: dict[str, Any] = {
            "$and": [
                conversation_turn_filter,
                {"created_at": {"$lt": recent_conversation_cutoff}},
            ]
        }

        def _claim_matching_instance(
            claim_query: dict[str, Any],
            *,
            claim_sort: list[tuple[str, int]],
        ) -> dict[str, Any] | None:
            # Use $setOnInsert for started_at only if not already set
            claimed_doc = coll.find_one_and_update(
                {**claim_query, "started_at": None},
                {
                    "$set": {
                        "status": WorkflowInstanceStatus.RUNNING.value,
                        "locked_by": worker_id,
                        "lock_expires_at": lock_expires,
                        "started_at": now,
                        "progress_message": "running",
                        "progress_updated_at": now,
                    }
                },
                sort=claim_sort,
                return_document=True,
            )

            # If no doc found with started_at=None, try without that constraint
            if claimed_doc is None:
                claimed_doc = coll.find_one_and_update(
                    claim_query,
                    {
                        "$set": {
                            "status": WorkflowInstanceStatus.RUNNING.value,
                            "locked_by": worker_id,
                            "lock_expires_at": lock_expires,
                            "progress_message": "running",
                            "progress_updated_at": now,
                        }
                    },
                    sort=claim_sort,
                    return_document=True,
                )
            return claimed_doc

        doc: dict[str, Any] | None = None
        if not workflow_ids:
            priority_query = {
                "$and": [
                    query,
                    conversation_turn_filter,
                    {"created_at": {"$gte": recent_conversation_cutoff}},
                ]
            }
            doc = _claim_matching_instance(
                priority_query,
                claim_sort=[("created_at", -1), ("instance_id", 1)],
            )

        if priority_only and not workflow_ids and doc is None:
            return None

        if doc is None:
            general_query = query
            if not workflow_ids:
                general_query = {
                    "$and": [query, {"$nor": [stale_conversation_turn_filter]}]
                }
            doc = _claim_matching_instance(
                general_query,
                claim_sort=[("created_at", 1), ("instance_id", 1)],
            )

        if doc:
            instance = self._instance_from_doc(doc)
            if instance is None:
                return None
            logger.info(
                "[durable_workflow] Worker %s claimed instance %s",
                worker_id,
                instance.instance_id,
            )
            self._record_durable_episode_start(instance=instance, worker_id=worker_id)
            self._broadcast_instance(instance)
            return instance
        return None

    def extend_lock(
        self,
        instance_id: str,
        worker_id: str,
        extend_seconds: int | None = None,
    ) -> bool:
        """Extend the lock on an instance.

        Args:
            instance_id: The instance to extend.
            worker_id: The worker that holds the lock.
            extend_seconds: How long to extend (defaults to lock_ttl).

        Returns:
            True if lock was extended, False if not held by this worker.
        """
        coll = self._get_instances_collection()
        if coll is None:
            return False

        extend_by = extend_seconds or self._lock_ttl
        new_expiry = datetime.now(timezone.utc) + timedelta(seconds=extend_by)

        result = coll.update_one(
            {"instance_id": instance_id, "locked_by": worker_id},
            {"$set": {"lock_expires_at": new_expiry}},
        )
        return result.modified_count > 0

    def release_lock(self, instance_id: str, worker_id: str) -> bool:
        """Release the lock on an instance.

        Args:
            instance_id: The instance to release.
            worker_id: The worker releasing the lock.

        Returns:
            True if lock was released.
        """
        coll = self._get_instances_collection()
        if coll is None:
            return False

        result = coll.update_one(
            {"instance_id": instance_id, "locked_by": worker_id},
            {"$set": {"locked_by": None, "lock_expires_at": None}},
        )
        return result.modified_count > 0

    # -------------------------------------------------------------------------
    # Checkpointing
    # -------------------------------------------------------------------------

    def checkpoint(
        self,
        instance_id: str,
        *,
        current_state: str,
        workflow_data: dict[str, Any],
        step_index: int | None = None,
        error: str | None = None,
        error_step: str | None = None,
        progress_current: int | None = None,
        progress_total: int | None = None,
        progress_message: str | None = None,
        execution_trace_id: str | None = None,
    ) -> bool:
        """Persist checkpoint data for an instance.

        Called after each successful step to enable resume.

        Args:
            instance_id: The instance to checkpoint.
            current_state: Current workflow state ID.
            workflow_data: Accumulated context data.
            step_index: Optional sequential step index.
            error: Optional error message (for failed checkpoints).
            error_step: Optional step that caused the error.
            progress_current: Optional progress step index.
            progress_total: Optional total step count.
            progress_message: Optional progress status message.

        Returns:
            True if checkpoint was saved.
        """
        update: dict[str, Any] = {
            "$set": {
                "current_state": current_state,
                "workflow_data": self._compact_instance_payload_field(
                    workflow_data,
                    field="workflow_data",
                    instance_id=instance_id,
                    namespace=(
                        workflow_data.get("namespace")
                        or workflow_data.get("user_namespace")
                    )
                    if isinstance(workflow_data, dict)
                    else None,
                    workflow_id=workflow_data.get("workflow_id")
                    if isinstance(workflow_data, dict)
                    else None,
                ),
            }
        }
        if step_index is not None:
            update["$set"]["step_index"] = step_index
        if error is not None:
            # JVNAUTOSCI-2502: checkpointed errors need the same writer
            # provenance as mark_failed — an untraceable persisted error
            # string cost hours of diagnosis on 2026-06-12.
            import traceback

            checkpoint_caller_frames = [
                f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_stack(limit=8)[:-1]
            ]
            logger.warning(
                "[durable_workflow] checkpoint error instance=%s error=%r "
                "step=%s via %s",
                instance_id,
                str(error)[:200],
                error_step,
                " <- ".join(reversed(checkpoint_caller_frames[-6:])),
            )
            update["$set"]["error"] = error
            update["$set"]["error_written_by"] = checkpoint_caller_frames[-6:]
        if error_step is not None:
            update["$set"]["error_step"] = error_step
        if progress_current is not None:
            update["$set"]["progress_current"] = progress_current
        if progress_total is not None:
            update["$set"]["progress_total"] = progress_total
        if progress_message is not None:
            update["$set"]["progress_message"] = progress_message
        if execution_trace_id is not None:
            update["$set"]["execution_trace_id"] = execution_trace_id
        if any(
            field is not None
            for field in (
                progress_current,
                progress_total,
                progress_message,
                execution_trace_id,
            )
        ):
            update["$set"]["progress_updated_at"] = datetime.now(timezone.utc)

        updated_instance = self._find_one_and_update_instance(
            {"instance_id": instance_id},
            update,
        )
        if updated_instance is not None:
            logger.debug(
                "[durable_workflow] Checkpointed instance %s at state %s",
                instance_id,
                current_state,
            )
            self._broadcast_instance(updated_instance)
            return True
        return False

    # -------------------------------------------------------------------------
    # Status transitions
    # -------------------------------------------------------------------------

    def _emit_episode_evaluation_terminal_event(
        self,
        *,
        instance: WorkflowInstance,
        terminal_status: str,
        final_state: str | None,
        termination_code: str | None,
        termination_detail: str | None,
    ) -> None:
        try:
            from ...services.workflow_event_integration_service import (
                maybe_launch_episode_evaluation_for_workflow_terminal,
            )

            result = maybe_launch_episode_evaluation_for_workflow_terminal(
                instance=instance,
                terminal_status=terminal_status,
                final_state=final_state,
                termination_code=termination_code,
                termination_detail=termination_detail,
            )
            if not bool(result.get("success")) and str(
                result.get("reason") or ""
            ) not in {
                "autotrigger_disabled",
                "max_depth_reached",
                "workflow_not_configured",
            }:
                logger.warning(
                    "[durable_workflow] Episode evaluation autotrigger did not launch for %s: %s",
                    instance.instance_id,
                    result,
                )
        except Exception as exc:
            logger.warning(
                "[durable_workflow] Episode evaluation autotrigger error for %s: %s",
                instance.instance_id,
                exc,
            )

    def mark_completed(
        self,
        instance_id: str,
        *,
        outputs: dict[str, Any] | None = None,
        final_state: str | None = None,
        execution_trace_id: str | None = None,
    ) -> bool:
        """Mark an instance as completed.

        Args:
            instance_id: The instance to complete.
            outputs: Final workflow outputs.
            final_state: Final state ID.

        Returns:
            True if status was updated.
        """
        now = datetime.now(timezone.utc)
        update: dict[str, Any] = {
            "$set": {
                "status": WorkflowInstanceStatus.COMPLETED.value,
                "completed_at": now,
                "locked_by": None,
                "lock_expires_at": None,
                "progress_message": "completed",
                "progress_updated_at": now,
            }
        }
        if outputs is not None:
            update["$set"]["outputs"] = self._compact_instance_payload_field(
                outputs,
                field="outputs",
                instance_id=instance_id,
                workflow_id=outputs.get("workflow_id")
                if isinstance(outputs, dict)
                else None,
            )
        if final_state is not None:
            update["$set"]["current_state"] = final_state
        if execution_trace_id is not None:
            update["$set"]["execution_trace_id"] = execution_trace_id

        instance_before = self._find_one_and_update_instance(
            {"instance_id": instance_id},
            update,
            return_document=False,
        )
        if instance_before is not None:
            logger.info("[durable_workflow] Instance %s completed", instance_id)
            self._record_durable_episode_final(
                instance=instance_before,
                completed=True,
                terminal_stage=final_state or "completed",
                termination_code="completed",
                termination_detail=None,
                final_state=final_state or instance_before.current_state,
            )
            completed_instance = replace(
                instance_before,
                status=WorkflowInstanceStatus.COMPLETED,
                completed_at=now,
                locked_by=None,
                lock_expires_at=None,
                progress_message="completed",
                progress_updated_at=now,
                current_state=final_state or instance_before.current_state,
                outputs=outputs if outputs is not None else instance_before.outputs,
                execution_trace_id=(
                    execution_trace_id
                    if execution_trace_id is not None
                    else instance_before.execution_trace_id
                ),
            )
            self._broadcast_instance(completed_instance)
            self._emit_episode_evaluation_terminal_event(
                instance=completed_instance,
                terminal_status=WorkflowInstanceStatus.COMPLETED.value,
                final_state=final_state or instance_before.current_state,
                termination_code="completed",
                termination_detail=None,
            )
            return True
        return False

    def mark_failed(
        self,
        instance_id: str,
        *,
        error: str,
        error_step: str | None = None,
        increment_retry: bool = True,
        outputs: dict[str, Any] | None = None,
        execution_trace_id: str | None = None,
    ) -> bool:
        """Mark an instance as failed.

        Args:
            instance_id: The instance that failed.
            error: Error message.
            error_step: Optional step where failure occurred.
            increment_retry: Whether to increment retry count.
            outputs: Optional bounded diagnostic outputs for the failed run.

        Returns:
            True if status was updated.
        """
        # JVNAUTOSCI-2502: failures are the records that most need provenance.
        # Name the writer (compact caller stack) in the log and on the doc so
        # an error string can always be traced to the code path that wrote it.
        import traceback

        caller_frames = [
            f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_stack(limit=6)[:-1]
        ]
        logger.warning(
            "[durable_workflow] mark_failed instance=%s error=%r step=%s via %s",
            instance_id,
            str(error)[:200],
            error_step,
            " <- ".join(reversed(caller_frames[-4:])),
        )

        now = datetime.now(timezone.utc)
        update: dict[str, Any] = {
            "$set": {
                "status": WorkflowInstanceStatus.FAILED.value,
                "completed_at": now,
                "error": error,
                "error_written_by": caller_frames[-4:],
                "locked_by": None,
                "lock_expires_at": None,
                "progress_message": "failed",
                "progress_updated_at": now,
            }
        }
        if error_step:
            update["$set"]["error_step"] = error_step
        if outputs is not None:
            update["$set"]["outputs"] = self._compact_instance_payload_field(
                outputs,
                field="outputs",
                instance_id=instance_id,
                workflow_id=outputs.get("workflow_id")
                if isinstance(outputs, dict)
                else None,
            )
        if execution_trace_id is not None:
            update["$set"]["execution_trace_id"] = execution_trace_id
        if increment_retry:
            update["$inc"] = {"retry_count": 1}

        instance_before = self._find_one_and_update_instance(
            {"instance_id": instance_id},
            update,
            return_document=False,
        )
        if instance_before is not None:
            logger.warning(
                "[durable_workflow] Instance %s failed: %s", instance_id, error
            )
            reason_code = (
                error.split(":", 1)[0].strip().lower()
                if isinstance(error, str) and ":" in error
                else "failed"
            )
            self._record_durable_episode_final(
                instance=instance_before,
                completed=False,
                terminal_stage=error_step or instance_before.current_state or "failed",
                termination_code=reason_code or "failed",
                termination_detail=error,
                final_state=instance_before.current_state,
            )
            failed_instance = replace(
                instance_before,
                status=WorkflowInstanceStatus.FAILED,
                completed_at=now,
                error=error,
                error_step=error_step or instance_before.error_step,
                locked_by=None,
                lock_expires_at=None,
                progress_message="failed",
                progress_updated_at=now,
                retry_count=(
                    instance_before.retry_count + 1
                    if increment_retry
                    else instance_before.retry_count
                ),
                outputs=outputs if outputs is not None else instance_before.outputs,
                execution_trace_id=(
                    execution_trace_id
                    if execution_trace_id is not None
                    else instance_before.execution_trace_id
                ),
            )
            self._broadcast_instance(failed_instance)
            self._emit_episode_evaluation_terminal_event(
                instance=failed_instance,
                terminal_status=WorkflowInstanceStatus.FAILED.value,
                final_state=instance_before.current_state,
                termination_code=reason_code or "failed",
                termination_detail=error,
            )
            return True
        return False

    def mark_cancelled(self, instance_id: str) -> bool:
        """Mark an instance as cancelled.

        Args:
            instance_id: The instance to cancel.

        Returns:
            True if status was updated.
        """
        now = datetime.now(timezone.utc)
        instance_before = self._find_one_and_update_instance(
            {
                "instance_id": instance_id,
                "status": {
                    "$in": [
                        WorkflowInstanceStatus.PENDING.value,
                        WorkflowInstanceStatus.RUNNING.value,
                        WorkflowInstanceStatus.PAUSED.value,
                    ]
                },
            },
            {
                "$set": {
                    "status": WorkflowInstanceStatus.CANCELLED.value,
                    "completed_at": now,
                    "locked_by": None,
                    "lock_expires_at": None,
                    "progress_message": "cancelled",
                    "progress_updated_at": now,
                }
            },
            return_document=False,
        )
        if instance_before is not None:
            logger.info("[durable_workflow] Instance %s cancelled", instance_id)
            self._record_durable_episode_final(
                instance=instance_before,
                completed=False,
                terminal_stage=instance_before.current_state or "cancelled",
                termination_code="cancelled",
                termination_detail="Workflow instance cancelled",
                final_state=instance_before.current_state,
            )
            cancelled_instance = replace(
                instance_before,
                status=WorkflowInstanceStatus.CANCELLED,
                completed_at=now,
                locked_by=None,
                lock_expires_at=None,
                progress_message="cancelled",
                progress_updated_at=now,
            )
            self._broadcast_instance(cancelled_instance)
            self._emit_episode_evaluation_terminal_event(
                instance=cancelled_instance,
                terminal_status=WorkflowInstanceStatus.CANCELLED.value,
                final_state=instance_before.current_state,
                termination_code="cancelled",
                termination_detail="Workflow instance cancelled",
            )
            return True
        return False

    def pause_instance(self, instance_id: str) -> bool:
        """Pause a running instance for later resumption.

        Args:
            instance_id: The instance to pause.

        Returns:
            True if status was updated.
        """
        updated_instance = self._find_one_and_update_instance(
            {
                "instance_id": instance_id,
                "status": WorkflowInstanceStatus.RUNNING.value,
            },
            {
                "$set": {
                    "status": WorkflowInstanceStatus.PAUSED.value,
                    "locked_by": None,
                    "lock_expires_at": None,
                    "progress_message": "paused",
                    "progress_updated_at": datetime.now(timezone.utc),
                }
            },
        )
        if updated_instance is not None:
            logger.info("[durable_workflow] Instance %s paused", instance_id)
            self._broadcast_instance(updated_instance)
            return True
        return False

    def is_cancelled(self, instance_id: str) -> bool:
        """Check if an instance has been cancelled.

        Args:
            instance_id: The instance to check.

        Returns:
            True if the instance status is CANCELLED.
        """
        coll = self._get_instances_collection()
        if coll is None:
            return False

        doc = coll.find_one(
            {"instance_id": instance_id},
            {"status": 1},
        )
        return (
            doc is not None
            and doc.get("status") == WorkflowInstanceStatus.CANCELLED.value
        )

    # -------------------------------------------------------------------------
    # Retry support
    # -------------------------------------------------------------------------

    def reset_for_retry(self, instance_id: str) -> bool:
        """Reset a failed instance for retry.

        Only works if retry_count < max_retries.

        Args:
            instance_id: The instance to retry.

        Returns:
            True if instance was reset for retry.
        """
        # Use $expr to compare retry_count < max_retries
        updated_instance = self._find_one_and_update_instance(
            {
                "instance_id": instance_id,
                "status": WorkflowInstanceStatus.FAILED.value,
                "$expr": {"$lt": ["$retry_count", "$max_retries"]},
            },
            {
                "$set": {
                    "status": WorkflowInstanceStatus.PENDING.value,
                    "error": None,
                    "error_step": None,
                    "completed_at": None,
                    "progress_current": 0,
                    "progress_message": "queued",
                    "progress_updated_at": datetime.now(timezone.utc),
                }
            },
        )
        if updated_instance is not None:
            logger.info("[durable_workflow] Instance %s reset for retry", instance_id)
            self._broadcast_instance(updated_instance)
            return True
        return False

    # -------------------------------------------------------------------------
    # Event Binding CRUD
    # -------------------------------------------------------------------------

    def _invalidate_event_binding_verification_cache(
        self,
        binding_workflow_id: str,
    ) -> None:
        """Invalidate runnable verification after event-binding mutations."""

        try:
            from .workflow_instance_submission_service import (
                invalidate_workflow_runnable_verification_cache,
            )

            invalidate_workflow_runnable_verification_cache(
                reason="event_binding_mutated",
                workflow_id=binding_workflow_id,
            )
        except Exception:
            logger.debug(
                "[durable_workflow] Event binding cache invalidation skipped",
                exc_info=True,
            )

    def upsert_event_binding(
        self,
        *,
        event_type: str,
        workflow_id: str,
        input_mapping: dict[str, str] | None = None,
        condition: dict[str, Any] | None = None,
        enabled: bool = True,
        actor: str | None = None,
        replace_existing: bool = False,
    ) -> tuple[EventWorkflowBinding, bool, bool]:
        """Create or update an event->workflow binding.

        Returns:
            (binding, created, updated)

        Conflict behaviour:
        - Same (event_type, workflow_id) + same config => idempotent no-op.
        - Same (event_type, workflow_id) + different config:
          - replace_existing=False => ValueError("binding_conflict")
          - replace_existing=True => update existing binding (revision++).
        """

        coll = self._get_event_bindings_collection()
        if coll is None:
            raise RuntimeError("Database unavailable for event binding persistence")

        event_type_clean = str(event_type or "").strip()
        workflow_id_clean = str(workflow_id or "").strip()
        if not event_type_clean:
            raise ValueError("event_type is required")
        if not workflow_id_clean:
            raise ValueError("workflow_id is required")

        mapping_clean: dict[str, str] = {}
        if isinstance(input_mapping, dict):
            for key, value in input_mapping.items():
                key_clean = str(key or "").strip()
                value_clean = str(value or "").strip()
                if key_clean and value_clean:
                    mapping_clean[key_clean] = value_clean

        condition_clean = dict(condition) if isinstance(condition, dict) else None

        actor_clean = (
            actor.strip() if isinstance(actor, str) and actor.strip() else None
        )
        query = {"event_type": event_type_clean, "workflow_id": workflow_id_clean}
        existing_doc = coll.find_one(query)
        if existing_doc:
            existing = EventWorkflowBinding.from_doc(existing_doc)
            if (
                existing.input_mapping == mapping_clean
                and existing.condition == condition_clean
                and bool(existing.enabled) == bool(enabled)
            ):
                return existing, False, False

            if not replace_existing:
                raise ValueError("binding_conflict")

            now = datetime.now(timezone.utc)
            next_revision = max(1, int(existing.revision)) + 1
            coll.update_one(
                {"binding_id": existing.binding_id},
                {
                    "$set": {
                        "input_mapping": mapping_clean,
                        "condition": condition_clean,
                        "enabled": bool(enabled),
                        "updated_at": now,
                        "updated_by": actor_clean,
                        "revision": next_revision,
                    }
                },
            )
            updated_doc = coll.find_one({"binding_id": existing.binding_id})
            if updated_doc is None:
                raise RuntimeError("binding_update_failed")
            updated_binding = EventWorkflowBinding.from_doc(updated_doc)
            self._invalidate_event_binding_verification_cache(
                updated_binding.workflow_id
            )
            return updated_binding, False, True

        binding = EventWorkflowBinding.create(
            event_type=event_type_clean,
            workflow_id=workflow_id_clean,
            input_mapping=mapping_clean,
            condition=condition_clean,
            enabled=bool(enabled),
            actor=actor_clean,
        )
        try:
            coll.insert_one(binding.to_doc())
            self._invalidate_event_binding_verification_cache(binding.workflow_id)
            return binding, True, False
        except DuplicateKeyError:
            # Concurrent upsert: re-read and apply deterministic conflict rules.
            existing_doc = coll.find_one(query)
            if existing_doc is None:
                raise
            existing = EventWorkflowBinding.from_doc(existing_doc)
            if (
                existing.input_mapping == mapping_clean
                and existing.condition == condition_clean
                and bool(existing.enabled) == bool(enabled)
            ):
                return existing, False, False
            if not replace_existing:
                raise ValueError("binding_conflict")
            now = datetime.now(timezone.utc)
            next_revision = max(1, int(existing.revision)) + 1
            coll.update_one(
                {"binding_id": existing.binding_id},
                {
                    "$set": {
                        "input_mapping": mapping_clean,
                        "condition": condition_clean,
                        "enabled": bool(enabled),
                        "updated_at": now,
                        "updated_by": actor_clean,
                        "revision": next_revision,
                    }
                },
            )
            updated_doc = coll.find_one({"binding_id": existing.binding_id})
            if updated_doc is None:
                raise RuntimeError("binding_update_failed")
            updated_binding = EventWorkflowBinding.from_doc(updated_doc)
            self._invalidate_event_binding_verification_cache(
                updated_binding.workflow_id
            )
            return updated_binding, False, True

    def set_event_binding_enabled(
        self,
        binding_id: str,
        *,
        enabled: bool,
        actor: str | None = None,
    ) -> EventWorkflowBinding | None:
        """Enable or disable an existing event-workflow binding."""

        binding_id_clean = str(binding_id or "").strip()
        if not binding_id_clean:
            raise ValueError("binding_id is required")

        coll = self._get_event_bindings_collection()
        if coll is None:
            raise RuntimeError("Database unavailable for event binding persistence")

        existing_doc = coll.find_one({"binding_id": binding_id_clean})
        if existing_doc is None:
            return None

        existing = EventWorkflowBinding.from_doc(existing_doc)
        if bool(existing.enabled) == bool(enabled):
            return existing

        actor_clean = (
            actor.strip() if isinstance(actor, str) and actor.strip() else None
        )
        now = datetime.now(timezone.utc)
        next_revision = max(1, int(existing.revision)) + 1
        coll.update_one(
            {"binding_id": binding_id_clean},
            {
                "$set": {
                    "enabled": bool(enabled),
                    "updated_at": now,
                    "updated_by": actor_clean,
                    "revision": next_revision,
                }
            },
        )
        updated_doc = coll.find_one({"binding_id": binding_id_clean})
        if updated_doc is None:
            raise RuntimeError("binding_update_failed")
        updated_binding = EventWorkflowBinding.from_doc(updated_doc)
        self._invalidate_event_binding_verification_cache(updated_binding.workflow_id)
        return updated_binding

    def delete_event_binding(self, binding_id: str) -> EventWorkflowBinding | None:
        """Delete an existing event-workflow binding.

        Delete is intentionally explicit and binding-id scoped so callers can
        review current bindings first, then remove one exact obsolete entry
        without guessing at event/workflow pairs.
        """

        binding_id_clean = str(binding_id or "").strip()
        if not binding_id_clean:
            raise ValueError("binding_id is required")

        coll = self._get_event_bindings_collection()
        if coll is None:
            raise RuntimeError("Database unavailable for event binding persistence")

        existing_doc = coll.find_one({"binding_id": binding_id_clean})
        if existing_doc is None:
            return None

        existing = EventWorkflowBinding.from_doc(existing_doc)
        result = coll.delete_one({"binding_id": binding_id_clean})
        if result.deleted_count <= 0:
            raise RuntimeError("binding_delete_failed")
        self._invalidate_event_binding_verification_cache(existing.workflow_id)
        return existing

    def list_event_bindings(
        self,
        *,
        event_type: str | None = None,
        enabled_only: bool = False,
        limit: int = 100,
    ) -> list[EventWorkflowBinding]:
        """List event-workflow bindings with optional filters."""

        coll = self._get_event_bindings_collection()
        if coll is None:
            return []

        query: dict[str, Any] = {}
        if isinstance(event_type, str) and event_type.strip():
            query["event_type"] = event_type.strip()
        if enabled_only:
            query["enabled"] = True

        capped_limit = max(1, min(int(limit), 500))
        cursor = (
            coll.find(query)
            .sort(
                [
                    ("event_type", ASCENDING),
                    ("created_at", ASCENDING),
                    ("workflow_id", ASCENDING),
                ]
            )
            .limit(capped_limit)
        )
        return [EventWorkflowBinding.from_doc(doc) for doc in cursor]

    def get_event_binding(self, binding_id: str) -> EventWorkflowBinding | None:
        """Load a specific event-workflow binding by ID."""

        binding_id_clean = str(binding_id or "").strip()
        if not binding_id_clean:
            return None
        coll = self._get_event_bindings_collection()
        if coll is None:
            return None
        doc = coll.find_one({"binding_id": binding_id_clean})
        if doc is None:
            return None
        return EventWorkflowBinding.from_doc(doc)

    # -------------------------------------------------------------------------
    # Schedule CRUD
    # -------------------------------------------------------------------------

    def create_schedule(self, schedule: WorkflowSchedule) -> str:
        """Create a new workflow schedule.

        Args:
            schedule: The schedule to create.

        Returns:
            The schedule_id.

        Raises:
            RuntimeError: If database is unavailable.
        """
        try:
            return self._schedule_repo.create_schedule(schedule)
        except Exception as e:
            logger.error(
                f"[durable_workflow] Failed to create schedule in Vontology: {e}"
            )
            raise RuntimeError(f"Schedule creation failed: {e}")

    def get_schedule(self, schedule_id: str) -> WorkflowSchedule | None:
        """Load a schedule by ID.

        Args:
            schedule_id: The schedule identifier.

        Returns:
            WorkflowSchedule if found, None otherwise.
        """
        return self._schedule_repo.get_schedule(schedule_id)

    def list_schedules(
        self,
        *,
        user_id: str | None = None,
        workflow_id: str | None = None,
        enabled_only: bool = False,
        limit: int = 50,
    ) -> list[WorkflowSchedule]:
        """List workflow schedules.

        Args:
            user_id: Filter by user.
            enabled_only: Only return enabled schedules.
            limit: Maximum results.

        Returns:
            List of matching schedules.
        """
        return self._schedule_repo.list_schedules(
            user_id=user_id,
            workflow_id=workflow_id,
            enabled_only=enabled_only,
            limit=limit,
        )

    def find_due_schedules(self, limit: int = 100) -> list[WorkflowSchedule]:
        """Find schedules that are due to run.

        Args:
            limit: Maximum schedules to return.

        Returns:
            List of schedules where next_run_at <= now.
        """
        return self._schedule_repo.find_due_schedules(limit=limit)

    def update_schedule_after_run(
        self,
        schedule_id: str,
        *,
        next_run_at: datetime | None,
    ) -> bool:
        """Update a schedule after it has been triggered.

        Args:
            schedule_id: The schedule to update.
            next_run_at: The next run time, or None to disable.

        Returns:
            True if updated.
        """
        return self._schedule_repo.update_schedule_after_run(
            schedule_id=schedule_id,
            next_run_at=next_run_at,
        )

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool:
        """Enable or disable a schedule.

        Args:
            schedule_id: The schedule to update.
            enabled: New enabled state.

        Returns:
            True if updated.
        """
        return self._schedule_repo.set_schedule_enabled(schedule_id, enabled)

    def delete_schedule(self, schedule_id: str) -> bool:
        """Delete a schedule.

        Args:
            schedule_id: The schedule to delete.

        Returns:
            True if deleted.
        """
        result = self._schedule_repo.delete_schedule(schedule_id)
        if result:
            logger.info(
                "[durable_workflow] Deleted schedule %s (Vontology)", schedule_id
            )
        return result
