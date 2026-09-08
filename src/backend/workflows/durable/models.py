"""Data models for durable workflow execution.

Defines durable workflow dataclasses that map to persistence records for:
- workflow instances
- workflow schedules
- event-to-workflow bindings
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class WorkflowInstanceStatus(str, Enum):
    """Status of a workflow instance."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"

    def is_terminal(self) -> bool:
        """Return True if this is a terminal status."""
        return self in (
            WorkflowInstanceStatus.COMPLETED,
            WorkflowInstanceStatus.FAILED,
            WorkflowInstanceStatus.CANCELLED,
        )

    def is_resumable(self) -> bool:
        """Return True if a workflow in this status can be resumed."""
        return self in (
            WorkflowInstanceStatus.PENDING,
            WorkflowInstanceStatus.PAUSED,
        )

    @classmethod
    def active_values(cls) -> tuple[str, ...]:
        """Return the active workflow statuses surfaced in the monitor."""
        return (
            cls.PENDING.value,
            cls.RUNNING.value,
            cls.PAUSED.value,
        )


class ScheduleType(str, Enum):
    """Type of workflow schedule."""

    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"


@dataclass
class EventWorkflowBinding:
    """Persistent event -> workflow binding definition.

    Supports reusable event-driven workflow triggering for arbitrary event
    names, with optional input mapping from event payload paths.
    """

    binding_id: str
    event_type: str
    workflow_id: str
    input_mapping: dict[str, str] = field(default_factory=dict)
    condition: dict[str, Any] | None = None
    enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str | None = None
    updated_by: str | None = None
    revision: int = 1

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        workflow_id: str,
        input_mapping: dict[str, str] | None = None,
        condition: dict[str, Any] | None = None,
        enabled: bool = True,
        actor: str | None = None,
    ) -> "EventWorkflowBinding":
        now = datetime.now(timezone.utc)
        return cls(
            binding_id=str(uuid.uuid4()),
            event_type=str(event_type or "").strip(),
            workflow_id=str(workflow_id or "").strip(),
            input_mapping=dict(input_mapping or {}),
            condition=dict(condition) if isinstance(condition, dict) else None,
            enabled=bool(enabled),
            created_at=now,
            updated_at=now,
            created_by=actor.strip()
            if isinstance(actor, str) and actor.strip()
            else None,
            updated_by=actor.strip()
            if isinstance(actor, str) and actor.strip()
            else None,
            revision=1,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "event_type": self.event_type,
            "workflow_id": self.workflow_id,
            "input_mapping": dict(self.input_mapping),
            "condition": dict(self.condition)
            if isinstance(self.condition, dict)
            else None,
            "enabled": bool(self.enabled),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "revision": int(self.revision),
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "EventWorkflowBinding":
        raw_input_mapping = doc.get("input_mapping")
        input_mapping: dict[str, str] = {}
        if isinstance(raw_input_mapping, dict):
            for raw_key, raw_value in raw_input_mapping.items():
                if isinstance(raw_key, bytes):
                    key = raw_key.decode("utf-8", errors="ignore").strip()
                else:
                    key = str(raw_key or "").strip()
                if isinstance(raw_value, bytes):
                    value = raw_value.decode("utf-8", errors="ignore").strip()
                else:
                    value = str(raw_value or "").strip()
                if key and value:
                    input_mapping[key] = value

        raw_condition = doc.get("condition")
        condition = dict(raw_condition) if isinstance(raw_condition, dict) else None

        return cls(
            binding_id=str(doc.get("binding_id") or ""),
            event_type=str(doc.get("event_type") or ""),
            workflow_id=str(doc.get("workflow_id") or ""),
            input_mapping=input_mapping,
            condition=condition,
            enabled=bool(doc.get("enabled", True)),
            created_at=doc.get("created_at") or datetime.now(timezone.utc),
            updated_at=doc.get("updated_at") or datetime.now(timezone.utc),
            created_by=doc.get("created_by"),
            updated_by=doc.get("updated_by"),
            revision=int(doc.get("revision", 1) or 1),
        )

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "event_type": self.event_type,
            "workflow_id": self.workflow_id,
            "input_mapping": dict(self.input_mapping),
            "condition": dict(self.condition)
            if isinstance(self.condition, dict)
            else None,
            "enabled": bool(self.enabled),
            "created_at": self.created_at.isoformat()
            if isinstance(self.created_at, datetime)
            else None,
            "updated_at": self.updated_at.isoformat()
            if isinstance(self.updated_at, datetime)
            else None,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "revision": int(self.revision),
        }


@dataclass
class WorkflowInstance:
    """A persistent workflow execution instance.

    Maps to a document in the workflow_instances MongoDB collection.
    Supports checkpoint/resume for long-running workflows.
    """

    # Identity
    instance_id: str
    workflow_id: str  # Definition ID (e.g., "#V#rag_sync_workflow")

    # Ownership
    user_id: str
    org_id: str | None
    namespace: str

    # Status
    status: WorkflowInstanceStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # Execution state (for checkpoint/resume)
    current_state: str = ""
    workflow_data: dict[str, Any] = field(default_factory=dict)
    step_index: int = 0

    # Progress tracking
    progress_current: int | None = None
    progress_total: int | None = None
    progress_message: str | None = None
    progress_updated_at: datetime | None = None

    # Locking (for distributed workers)
    locked_by: str | None = None
    lock_expires_at: datetime | None = None
    claimed_at: datetime | None = None
    claimed_by_build: dict[str, Any] | None = None
    claim_token: str | None = None
    task_ownership_key: str | None = None
    task_ownership_active: bool = False
    uncheckpointed_effects: list[dict[str, Any]] = field(default_factory=list)
    authority_checkpoint_attestation: dict[str, Any] | None = None
    min_worker_build: str | None = None
    claim_ineligible_reason: str | None = None
    claim_ineligible_detected_at: datetime | None = None
    manual_resume_required: bool = False
    checkpoint_pause_receipt: dict[str, Any] | None = None
    checkpoint_resume_receipt: dict[str, Any] | None = None
    checkpoint_resume_count: int = 0

    # Inputs/outputs
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] | None = None

    # Error info
    error: str | None = None
    error_step: str | None = None
    retry_count: int = 0
    max_retries: int = 3

    # Scheduling reference
    schedule_id: str | None = None

    # Event-driven trigger linkage (WS6 / JVNAUTOSCI-1090).
    source_event_type: str | None = None
    source_event_id: str | None = None
    event_idempotency_key: str | None = None

    # Tracing
    execution_trace_id: str | None = None

    # Content-free provider usage and registry-priced cost estimate. The
    # schedule/occurrence attribution remains on this instance record.
    llm_usage_cost_summary: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
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
    ) -> WorkflowInstance:
        """Create a new workflow instance with generated ID."""
        return cls(
            instance_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            status=WorkflowInstanceStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            inputs=inputs or {},
            schedule_id=schedule_id,
            max_retries=max_retries,
            source_event_type=source_event_type,
            source_event_id=source_event_id,
            event_idempotency_key=event_idempotency_key,
            progress_current=0,
            progress_message="queued",
            progress_updated_at=datetime.now(timezone.utc),
        )

    def to_doc(self) -> dict[str, Any]:
        """Convert to MongoDB document format."""
        doc: dict[str, Any] = {
            "instance_id": self.instance_id,
            "workflow_id": self.workflow_id,
            "user_id": self.user_id,
            "org_id": self.org_id,
            "namespace": self.namespace,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "current_state": self.current_state,
            "workflow_data": self.workflow_data,
            "step_index": self.step_index,
            "progress_current": self.progress_current,
            "progress_total": self.progress_total,
            "progress_message": self.progress_message,
            "progress_updated_at": self.progress_updated_at,
            "locked_by": self.locked_by,
            "lock_expires_at": self.lock_expires_at,
            "claimed_at": self.claimed_at,
            "claimed_by_build": self.claimed_by_build,
            "claim_token": self.claim_token,
            "task_ownership_key": self.task_ownership_key,
            "task_ownership_active": self.task_ownership_active,
            "uncheckpointed_effects": self.uncheckpointed_effects,
            "authority_checkpoint_attestation": (self.authority_checkpoint_attestation),
            "min_worker_build": self.min_worker_build,
            "claim_ineligible_reason": self.claim_ineligible_reason,
            "claim_ineligible_detected_at": self.claim_ineligible_detected_at,
            "manual_resume_required": self.manual_resume_required,
            "checkpoint_pause_receipt": self.checkpoint_pause_receipt,
            "checkpoint_resume_receipt": self.checkpoint_resume_receipt,
            "checkpoint_resume_count": self.checkpoint_resume_count,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "error": self.error,
            "error_step": self.error_step,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "schedule_id": self.schedule_id,
            "execution_trace_id": self.execution_trace_id,
            "llm_usage_cost_summary": self.llm_usage_cost_summary,
        }

        # Persist event linkage fields only when present so sparse/partial indexes
        # can enforce idempotency without collisions on null placeholder values.
        if self.source_event_type is not None:
            doc["source_event_type"] = self.source_event_type
        if self.source_event_id is not None:
            doc["source_event_id"] = self.source_event_id
        if self.event_idempotency_key is not None:
            doc["event_idempotency_key"] = self.event_idempotency_key
        return doc

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> WorkflowInstance:
        """Create instance from MongoDB document."""
        return cls(
            instance_id=doc["instance_id"],
            workflow_id=doc["workflow_id"],
            user_id=doc["user_id"],
            org_id=doc.get("org_id"),
            namespace=doc["namespace"],
            status=WorkflowInstanceStatus(doc["status"]),
            created_at=doc["created_at"],
            started_at=doc.get("started_at"),
            completed_at=doc.get("completed_at"),
            current_state=doc.get("current_state", ""),
            workflow_data=doc.get("workflow_data", {}),
            step_index=doc.get("step_index", 0),
            progress_current=doc.get("progress_current"),
            progress_total=doc.get("progress_total"),
            progress_message=doc.get("progress_message"),
            progress_updated_at=doc.get("progress_updated_at"),
            locked_by=doc.get("locked_by"),
            lock_expires_at=doc.get("lock_expires_at"),
            claimed_at=doc.get("claimed_at"),
            claimed_by_build=(
                doc.get("claimed_by_build")
                if isinstance(doc.get("claimed_by_build"), dict)
                else None
            ),
            claim_token=(
                doc.get("claim_token")
                if isinstance(doc.get("claim_token"), str)
                else None
            ),
            authority_checkpoint_attestation=(
                doc.get("authority_checkpoint_attestation")
                if isinstance(doc.get("authority_checkpoint_attestation"), dict)
                else None
            ),
            task_ownership_key=doc.get("task_ownership_key"),
            task_ownership_active=bool(doc.get("task_ownership_active")),
            uncheckpointed_effects=list(doc.get("uncheckpointed_effects") or []),
            min_worker_build=doc.get("min_worker_build"),
            claim_ineligible_reason=doc.get("claim_ineligible_reason"),
            claim_ineligible_detected_at=doc.get("claim_ineligible_detected_at"),
            manual_resume_required=bool(doc.get("manual_resume_required", False)),
            checkpoint_pause_receipt=(
                doc.get("checkpoint_pause_receipt")
                if isinstance(doc.get("checkpoint_pause_receipt"), dict)
                else None
            ),
            checkpoint_resume_receipt=(
                doc.get("checkpoint_resume_receipt")
                if isinstance(doc.get("checkpoint_resume_receipt"), dict)
                else None
            ),
            checkpoint_resume_count=int(doc.get("checkpoint_resume_count", 0) or 0),
            inputs=doc.get("inputs", {}),
            outputs=doc.get("outputs"),
            error=doc.get("error"),
            error_step=doc.get("error_step"),
            retry_count=doc.get("retry_count", 0),
            max_retries=doc.get("max_retries", 3),
            schedule_id=doc.get("schedule_id"),
            source_event_type=doc.get("source_event_type"),
            source_event_id=doc.get("source_event_id"),
            event_idempotency_key=doc.get("event_idempotency_key"),
            execution_trace_id=doc.get("execution_trace_id"),
            llm_usage_cost_summary=(
                dict(doc["llm_usage_cost_summary"])
                if isinstance(doc.get("llm_usage_cost_summary"), dict)
                else None
            ),
        )

    @staticmethod
    def _status_datetime_to_iso(value: Any) -> str | None:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return None

    @classmethod
    def status_dict_from_doc(cls, doc: dict[str, Any]) -> dict[str, Any]:
        """Convert a persisted document or projection into monitor payload shape."""
        status_value = doc.get("status")
        if isinstance(status_value, WorkflowInstanceStatus):
            status = status_value.value
        else:
            status = str(status_value or "")

        has_outputs = doc.get("has_outputs")
        if not isinstance(has_outputs, bool):
            has_outputs = doc.get("outputs") is not None

        return {
            "instance_id": doc.get("instance_id"),
            "workflow_id": doc.get("workflow_id"),
            "status": status,
            "current_state": doc.get("current_state", ""),
            "step_index": doc.get("step_index", 0),
            "created_at": cls._status_datetime_to_iso(doc.get("created_at")),
            "started_at": cls._status_datetime_to_iso(doc.get("started_at")),
            "completed_at": cls._status_datetime_to_iso(doc.get("completed_at")),
            "locked_by": doc.get("locked_by"),
            "lock_expires_at": cls._status_datetime_to_iso(doc.get("lock_expires_at")),
            "claimed_at": cls._status_datetime_to_iso(doc.get("claimed_at")),
            "claimed_by_build": (
                doc.get("claimed_by_build")
                if isinstance(doc.get("claimed_by_build"), dict)
                else None
            ),
            "min_worker_build": doc.get("min_worker_build"),
            "claim_ineligible_reason": doc.get("claim_ineligible_reason"),
            "claim_ineligible_detected_at": cls._status_datetime_to_iso(
                doc.get("claim_ineligible_detected_at")
            ),
            "manual_resume_required": bool(doc.get("manual_resume_required", False)),
            "checkpoint_pause_receipt": (
                doc.get("checkpoint_pause_receipt")
                if isinstance(doc.get("checkpoint_pause_receipt"), dict)
                else None
            ),
            "checkpoint_resume_receipt": (
                doc.get("checkpoint_resume_receipt")
                if isinstance(doc.get("checkpoint_resume_receipt"), dict)
                else None
            ),
            "checkpoint_resume_count": int(doc.get("checkpoint_resume_count", 0) or 0),
            "progress": {
                "current": doc.get("progress_current"),
                "total": doc.get("progress_total"),
                "message": doc.get("progress_message"),
                "updated_at": cls._status_datetime_to_iso(
                    doc.get("progress_updated_at")
                ),
            },
            "error": doc.get("error"),
            "has_outputs": has_outputs,
            "retry_count": doc.get("retry_count", 0),
            "max_retries": doc.get("max_retries", 3),
            "source_event_type": doc.get("source_event_type"),
            "source_event_id": doc.get("source_event_id"),
            "event_idempotency_key": doc.get("event_idempotency_key"),
            "execution_trace_id": doc.get("execution_trace_id"),
            "llm_usage_cost_summary": (
                dict(doc["llm_usage_cost_summary"])
                if isinstance(doc.get("llm_usage_cost_summary"), dict)
                else None
            ),
        }

    def to_status_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serialisable status dict for API responses."""
        return self.status_dict_from_doc(
            {
                "instance_id": self.instance_id,
                "workflow_id": self.workflow_id,
                "status": self.status,
                "current_state": self.current_state,
                "step_index": self.step_index,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "locked_by": self.locked_by,
                "lock_expires_at": self.lock_expires_at,
                "claimed_at": self.claimed_at,
                "claimed_by_build": self.claimed_by_build,
                "min_worker_build": self.min_worker_build,
                "claim_ineligible_reason": self.claim_ineligible_reason,
                "claim_ineligible_detected_at": self.claim_ineligible_detected_at,
                "manual_resume_required": self.manual_resume_required,
                "checkpoint_pause_receipt": self.checkpoint_pause_receipt,
                "checkpoint_resume_receipt": self.checkpoint_resume_receipt,
                "checkpoint_resume_count": self.checkpoint_resume_count,
                "progress_current": self.progress_current,
                "progress_total": self.progress_total,
                "progress_message": self.progress_message,
                "progress_updated_at": self.progress_updated_at,
                "error": self.error,
                "has_outputs": self.outputs is not None,
                "retry_count": self.retry_count,
                "max_retries": self.max_retries,
                "source_event_type": self.source_event_type,
                "source_event_id": self.source_event_id,
                "event_idempotency_key": self.event_idempotency_key,
                "execution_trace_id": self.execution_trace_id,
                "llm_usage_cost_summary": self.llm_usage_cost_summary,
            }
        )


@dataclass
class WorkflowSchedule:
    """A scheduled workflow trigger.

    Maps to a document in the workflow_schedules MongoDB collection.
    Supports one-time, interval, and cron-based scheduling.
    """

    # Identity
    schedule_id: str
    workflow_id: str  # Definition ID to run

    # Ownership
    user_id: str
    org_id: str
    namespace: str

    # Schedule config
    schedule_type: ScheduleType
    run_at: datetime | None = None  # For "once"
    interval_seconds: int | None = None  # For "interval"
    cron_expression: str | None = None  # For "cron"

    # State
    enabled: bool = True
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None

    # Inputs
    default_inputs: dict[str, Any] = field(default_factory=dict)

    # Metadata
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    description: str | None = None
    # Legacy schedules are deliberately not adopted by release reconciliation.
    origin: str = "legacy_unmanaged"
    definition_identity: dict[str, Any] = field(default_factory=dict)
    creation_context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create_once(
        cls,
        workflow_id: str,
        run_at: datetime,
        *,
        user_id: str,
        org_id: str,
        namespace: str,
        default_inputs: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> WorkflowSchedule:
        """Create a one-time schedule."""
        return cls(
            schedule_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            schedule_type=ScheduleType.ONCE,
            run_at=run_at,
            next_run_at=run_at,
            default_inputs=default_inputs or {},
            description=description,
        )

    @classmethod
    def create_interval(
        cls,
        workflow_id: str,
        interval_seconds: int,
        *,
        user_id: str,
        org_id: str,
        namespace: str,
        default_inputs: dict[str, Any] | None = None,
        description: str | None = None,
        start_at: datetime | None = None,
    ) -> WorkflowSchedule:
        """Create an interval-based schedule."""
        now = datetime.now(timezone.utc)
        next_run = start_at if start_at and start_at > now else now
        return cls(
            schedule_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            schedule_type=ScheduleType.INTERVAL,
            interval_seconds=interval_seconds,
            next_run_at=next_run,
            default_inputs=default_inputs or {},
            description=description,
        )

    @classmethod
    def create_cron(
        cls,
        workflow_id: str,
        cron_expression: str,
        *,
        user_id: str,
        org_id: str,
        namespace: str,
        default_inputs: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> WorkflowSchedule:
        """Create a cron-based schedule.

        Args:
            cron_expression: Standard 5-field cron expression (minute hour day month weekday).
        """
        return cls(
            schedule_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            schedule_type=ScheduleType.CRON,
            cron_expression=cron_expression,
            default_inputs=default_inputs or {},
            description=description,
            # next_run_at will be computed by the scheduler
        )

    def to_doc(self) -> dict[str, Any]:
        """Convert to MongoDB document format."""
        return {
            "schedule_id": self.schedule_id,
            "workflow_id": self.workflow_id,
            "user_id": self.user_id,
            "org_id": self.org_id,
            "namespace": self.namespace,
            "schedule_type": self.schedule_type.value,
            "run_at": self.run_at,
            "interval_seconds": self.interval_seconds,
            "cron_expression": self.cron_expression,
            "enabled": self.enabled,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "default_inputs": self.default_inputs,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "description": self.description,
            "origin": self.origin,
            "definition_identity": self.definition_identity,
            "creation_context": self.creation_context,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> WorkflowSchedule:
        """Create schedule from MongoDB document."""
        return cls(
            schedule_id=doc["schedule_id"],
            workflow_id=doc["workflow_id"],
            user_id=doc["user_id"],
            org_id=doc["org_id"],
            namespace=doc["namespace"],
            schedule_type=ScheduleType(doc["schedule_type"]),
            run_at=doc.get("run_at"),
            interval_seconds=doc.get("interval_seconds"),
            cron_expression=doc.get("cron_expression"),
            enabled=doc.get("enabled", True),
            last_run_at=doc.get("last_run_at"),
            next_run_at=doc.get("next_run_at"),
            default_inputs=doc.get("default_inputs", {}),
            created_at=doc.get("created_at", datetime.now(timezone.utc)),
            updated_at=doc.get("updated_at", datetime.now(timezone.utc)),
            description=doc.get("description"),
            origin=doc.get("origin", "legacy_unmanaged"),
            definition_identity=doc.get("definition_identity", {}),
            creation_context=doc.get("creation_context", {}),
        )

    def to_status_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serialisable status dict for API responses."""
        return {
            "schedule_id": self.schedule_id,
            "workflow_id": self.workflow_id,
            "schedule_type": self.schedule_type.value,
            "enabled": self.enabled,
            "cron_expression": self.cron_expression,
            "interval_seconds": self.interval_seconds,
            "run_at": self.run_at.isoformat() if self.run_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "description": self.description,
            "origin": self.origin,
            "definition_identity": self.definition_identity,
            "creation_context": self.creation_context,
        }
