"""Durable execution attempts for Von-assigned task specifications.

``TaskSpecification`` remains the durable work intent.  This service owns one
``TaskExecution`` concept per explicit launch and records its operational queue
identity, but deliberately does not infer that a completed queue turn completed
the task. TaskSpecification completion remains an explicit, separately
authorised general-task transition backed by work-product evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.visibility_predicates import (
    set_specific_to_org_values,
    set_specific_to_user_values,
)
from .namespace_service import resolve_canonical_namespace
from .text_value_service import upsert_singleton_text_relation

logger = logging.getLogger(__name__)

TASK_EXECUTION_TYPE_ID = "#V#task_execution"
VON_SYSTEM_CONCEPT_ID = "#V#von_system"

PREDICATE_EXECUTES_TASK = "#V#executesTask"
PREDICATE_HAS_EXECUTOR = "#V#hasExecutor"
PREDICATE_HAS_CREATED_BY = "#V#hasCreatedBy"
PREDICATE_HAS_ORIGINATING_CONVERSATION = "#V#hasOriginatingConversation"
PREDICATE_HAS_EXECUTION_STATUS = "#V#hasExecutionStatus"
PREDICATE_HAS_START_TIME = "#V#hasStartTime"
PREDICATE_HAS_END_TIME = "#V#hasEndTime"
PREDICATE_HAS_RESULT = "#V#hasResult"
PREDICATE_HAS_PROGRESS_NOTE = "#V#hasProgressNote"

TASK_EXECUTION_STATUS_PENDING = "pending"
TASK_EXECUTION_STATUS_IN_PROGRESS = "in_progress"
TASK_EXECUTION_STATUS_COMPLETED = "completed"
TASK_EXECUTION_STATUS_FAILED = "failed"
TASK_EXECUTION_STATUS_CANCELLED = "cancelled"

TASK_EXECUTION_ACTIVE_STATUSES = frozenset(
    {TASK_EXECUTION_STATUS_PENDING, TASK_EXECUTION_STATUS_IN_PROGRESS}
)
TASK_EXECUTION_TERMINAL_STATUSES = frozenset(
    {
        TASK_EXECUTION_STATUS_COMPLETED,
        TASK_EXECUTION_STATUS_FAILED,
        TASK_EXECUTION_STATUS_CANCELLED,
    }
)
TASK_EXECUTION_VALID_STATUSES = (
    TASK_EXECUTION_ACTIVE_STATUSES | TASK_EXECUTION_TERMINAL_STATUSES
)

_ALLOWED_TRANSITIONS = {
    TASK_EXECUTION_STATUS_PENDING: {
        TASK_EXECUTION_STATUS_IN_PROGRESS,
        TASK_EXECUTION_STATUS_FAILED,
        TASK_EXECUTION_STATUS_CANCELLED,
    },
    TASK_EXECUTION_STATUS_IN_PROGRESS: set(TASK_EXECUTION_TERMINAL_STATUSES),
}


class TaskExecutionError(RuntimeError):
    """Base error for durable task execution operations."""


class InvalidTaskExecutionData(TaskExecutionError, ValueError):
    """Raised when launch or transition data is malformed."""


class TaskExecutionNotFoundError(TaskExecutionError, LookupError):
    """Raised when an execution is absent or outside the trusted actor scope."""


class TaskExecutionAccessError(TaskExecutionError, PermissionError):
    """Raised when a task or execution does not match the trusted actor scope."""

    def __init__(self, reason_code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.reason_code = reason_code
        self.safe_message = safe_message


class TaskExecutionTransitionError(TaskExecutionError):
    """Raised for an invalid or racing lifecycle transition."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _required_text(value: Any, *, field: str, max_chars: int = 2_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidTaskExecutionData(f"{field} is required")
    cleaned = value.strip()
    if len(cleaned) > max_chars:
        raise InvalidTaskExecutionData(f"{field} is too long")
    return cleaned


def _concept_id(value: Any, *, field: str) -> str:
    cleaned = _required_text(value, field=field)
    if not cleaned.startswith("#V#") or len(cleaned) <= 3:
        raise InvalidTaskExecutionData(f"{field} must be a #V# concept ID")
    return cleaned


def _json_object(value: Any, *, field: str, max_chars: int = 100_000) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidTaskExecutionData(f"{field} must be an object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidTaskExecutionData(f"{field} must contain JSON values") from exc
    if len(encoded) > max_chars:
        raise InvalidTaskExecutionData(f"{field} is too large")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - guarded above
        raise InvalidTaskExecutionData(f"{field} must be an object")
    return decoded


def task_execution_concept_id_for_launch(
    *,
    task_concept_id: str,
    creator_concept_id: str,
    organisation_concept_id: str,
    launch_request_id: str,
) -> str:
    """Return the stable execution identity for one logical launch request."""

    material = "\x1f".join(
        (
            _concept_id(task_concept_id, field="task_concept_id"),
            _concept_id(creator_concept_id, field="creator_concept_id"),
            _concept_id(organisation_concept_id, field="organisation_concept_id"),
            _required_text(
                launch_request_id,
                field="launch_request_id",
                max_chars=200,
            ),
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"#V#task_execution_{digest}"


def task_execution_enqueue_submission_id_for_launch(
    *,
    task_concept_id: str,
    creator_concept_id: str,
    organisation_concept_id: str,
    launch_request_id: str,
) -> str:
    """Return the stable queue idempotency identity for one logical launch."""

    execution_id = task_execution_concept_id_for_launch(
        task_concept_id=task_concept_id,
        creator_concept_id=creator_concept_id,
        organisation_concept_id=organisation_concept_id,
        launch_request_id=launch_request_id,
    )
    return f"task-launch:{execution_id.removeprefix('#V#task_execution_')}"


def _metadata(doc: Mapping[str, Any]) -> dict[str, Any]:
    raw = doc.get("metadata")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _execution_response(
    doc: Mapping[str, Any],
    *,
    created: bool | None = None,
) -> dict[str, Any]:
    metadata = _metadata(doc)
    result = {
        "task_execution_concept_id": doc.get("concept_id"),
        "task_concept_id": metadata.get("task_concept_id"),
        "status": metadata.get("execution_status"),
        "creator_concept_id": metadata.get("creator_concept_id"),
        "executor_concept_id": metadata.get("executor_concept_id"),
        "organisation_concept_id": metadata.get("organisation_concept_id"),
        "namespace": metadata.get("namespace"),
        "originating_conversation_id": metadata.get("originating_conversation_id"),
        "conversation_session_id": metadata.get("conversation_session_id"),
        "conversation_name": metadata.get("conversation_name"),
        "launch_request_id": metadata.get("launch_request_id"),
        "enqueue_submission_id": metadata.get("enqueue_submission_id"),
        "queue_id": metadata.get("queue_id"),
        "execution_envelope_version": metadata.get("execution_envelope_version"),
        "execution_envelope": metadata.get("execution_envelope"),
        "progress_note": metadata.get("progress_note"),
        "result": metadata.get("result"),
        "started_at": _iso(metadata.get("started_at")),
        "ended_at": _iso(metadata.get("ended_at")),
        "task_terminalised_at": _iso(metadata.get("task_terminalised_at")),
        "created_at": _iso(doc.get("created_at")),
        "updated_at": _iso(doc.get("updated_at")),
    }
    if created is not None:
        result["created"] = created
    return result


def _task_launch_values(
    *,
    task: Mapping[str, Any],
    creator_concept_id: str,
    organisation_concept_id: str,
    namespace: str,
) -> dict[str, str | None]:
    task_id = _concept_id(task.get("task_concept_id"), field="task_concept_id")
    creator_id = _concept_id(creator_concept_id, field="creator_concept_id")
    org_id = _concept_id(
        organisation_concept_id,
        field="organisation_concept_id",
    )
    task_org_id = _concept_id(
        task.get("organisation_concept_id"),
        field="task.organisation_concept_id",
    )
    if task_org_id != org_id:
        raise TaskExecutionAccessError(
            "task_organisation_mismatch",
            "The task does not belong to the active organisation",
        )
    if task.get("assignee_concept_id") != VON_SYSTEM_CONCEPT_ID:
        raise TaskExecutionAccessError(
            "task_not_assigned_to_von",
            "The task must be assigned to Von before it can be executed",
        )
    if task.get("created_by_concept_id") != creator_id:
        raise TaskExecutionAccessError(
            "task_creator_mismatch",
            "Only the task creator can authorise Von to execute this task",
        )
    task_status = str(task.get("status") or "").strip().lower()
    if task_status not in {"pending", "in_progress"}:
        raise TaskExecutionAccessError(
            "task_not_executable",
            "Only a pending or in-progress task can be executed",
        )
    conversation_id = _concept_id(
        task.get("originating_conversation_id"),
        field="task.originating_conversation_id",
    )
    conversation_session_id = _required_text(
        task.get("conversation_session_id"),
        field="task.conversation_session_id",
    )
    canonical_namespace = resolve_canonical_namespace(None, creator_id, org_id)
    supplied_namespace = resolve_canonical_namespace(namespace, creator_id, org_id)
    if not canonical_namespace or supplied_namespace != canonical_namespace:
        raise TaskExecutionAccessError(
            "task_execution_scope_mismatch",
            "The execution namespace does not match the trusted actor context",
        )
    conversation_name_raw = task.get("conversation_name")
    conversation_name = (
        conversation_name_raw.strip()
        if isinstance(conversation_name_raw, str) and conversation_name_raw.strip()
        else None
    )
    return {
        "task_concept_id": task_id,
        "creator_concept_id": creator_id,
        "organisation_concept_id": org_id,
        "namespace": canonical_namespace,
        "originating_conversation_id": conversation_id,
        "conversation_session_id": conversation_session_id,
        "conversation_name": conversation_name,
    }


def _persist_singleton_text(
    *,
    execution_concept_id: str,
    predicate: str,
    text: str,
) -> None:
    try:
        upsert_singleton_text_relation(
            subject_concept_id=execution_concept_id,
            predicate=predicate,
            text=text,
            lang="en-NZ",
        )
    except Exception as exc:  # noqa: BLE001 - metadata remains authoritative
        # The concept metadata remains the operational source of truth.  The
        # ontology text projection is inspectability support and can be repaired.
        logger.warning(
            "Failed to persist TaskExecution text projection %s for %s: %s",
            predicate,
            execution_concept_id,
            exc,
        )


def create_task_execution(
    *,
    task: Mapping[str, Any],
    creator_concept_id: str,
    organisation_concept_id: str,
    namespace: str,
    launch_request_id: str,
    enqueue_submission_id: str,
    execution_envelope: Mapping[str, Any],
    execution_envelope_version: int = 1,
) -> dict[str, Any]:
    """Create or reconcile one durable execution for a logical launch."""

    values = _task_launch_values(
        task=task,
        creator_concept_id=creator_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    launch_id = _required_text(
        launch_request_id,
        field="launch_request_id",
        max_chars=200,
    )
    enqueue_id = _required_text(
        enqueue_submission_id,
        field="enqueue_submission_id",
        max_chars=200,
    )
    if execution_envelope_version != 1:
        raise InvalidTaskExecutionData("execution_envelope_version must be 1")
    envelope = _json_object(execution_envelope, field="execution_envelope")
    execution_concept_id = task_execution_concept_id_for_launch(
        task_concept_id=str(values["task_concept_id"]),
        creator_concept_id=str(values["creator_concept_id"]),
        organisation_concept_id=str(values["organisation_concept_id"]),
        launch_request_id=launch_id,
    )
    now = _now()
    relationships: dict[str, Any] = {
        "is_an_instance_of": [TASK_EXECUTION_TYPE_ID],
        PREDICATE_EXECUTES_TASK: [values["task_concept_id"]],
        PREDICATE_HAS_EXECUTOR: [VON_SYSTEM_CONCEPT_ID],
        PREDICATE_HAS_CREATED_BY: [values["creator_concept_id"]],
        PREDICATE_HAS_ORIGINATING_CONVERSATION: [values["originating_conversation_id"]],
    }
    relationships = set_specific_to_user_values(
        relationships,
        [str(values["creator_concept_id"])],
    )
    relationships = set_specific_to_org_values(
        relationships,
        [str(values["organisation_concept_id"])],
    )
    metadata: dict[str, Any] = {
        "concept_type": "task_execution",
        **values,
        "executor_concept_id": VON_SYSTEM_CONCEPT_ID,
        "launch_request_id": launch_id,
        "enqueue_submission_id": enqueue_id,
        "queue_id": None,
        "execution_status": TASK_EXECUTION_STATUS_PENDING,
        "execution_envelope_version": execution_envelope_version,
        "execution_envelope": envelope,
        "status_history": [
            {
                "status": TASK_EXECUTION_STATUS_PENDING,
                "at": now,
                "source": "explicit_task_launch",
            }
        ],
    }
    doc = {
        "concept_id": execution_concept_id,
        "guid": str(uuid.uuid4()),
        "relationships": relationships,
        "metadata": metadata,
        "created_at": now,
        "updated_at": now,
    }
    created = True
    try:
        ConceptsRepository.insert_one(doc)
    except DuplicateKeyError:
        created = False
        existing = ConceptsRepository.find_one({"concept_id": execution_concept_id})
        if not isinstance(existing, Mapping):
            raise TaskExecutionNotFoundError(
                "The existing task execution is not accessible"
            )
        existing_metadata = _metadata(existing)
        exact_fields = {
            "task_concept_id": values["task_concept_id"],
            "creator_concept_id": values["creator_concept_id"],
            "organisation_concept_id": values["organisation_concept_id"],
            "namespace": values["namespace"],
            "originating_conversation_id": values["originating_conversation_id"],
            "conversation_session_id": values["conversation_session_id"],
            "launch_request_id": launch_id,
            "enqueue_submission_id": enqueue_id,
            "executor_concept_id": VON_SYSTEM_CONCEPT_ID,
            "execution_envelope_version": execution_envelope_version,
            "execution_envelope": envelope,
        }
        if any(
            existing_metadata.get(key) != value for key, value in exact_fields.items()
        ):
            raise TaskExecutionAccessError(
                "task_execution_identity_conflict",
                "The launch identity is already bound to different task work",
            )
        doc = existing
    except Exception as exc:
        raise TaskExecutionError(f"Failed to create task execution: {exc}") from exc

    if created:
        _persist_singleton_text(
            execution_concept_id=execution_concept_id,
            predicate=PREDICATE_HAS_EXECUTION_STATUS,
            text=TASK_EXECUTION_STATUS_PENDING,
        )
    return _execution_response(doc, created=created)


def _get_task_execution_doc(
    task_execution_concept_id: str,
    *,
    actor_concept_id: str,
    organisation_concept_id: str,
) -> dict[str, Any]:
    execution_id = _concept_id(
        task_execution_concept_id,
        field="task_execution_concept_id",
    )
    actor_id = _concept_id(actor_concept_id, field="actor_concept_id")
    org_id = _concept_id(
        organisation_concept_id,
        field="organisation_concept_id",
    )
    doc = ConceptsRepository.find_one({"concept_id": execution_id})
    if not isinstance(doc, Mapping):
        raise TaskExecutionNotFoundError("Task execution not found")
    metadata = _metadata(doc)
    if metadata.get("concept_type") != "task_execution":
        raise TaskExecutionNotFoundError("Task execution not found")
    if metadata.get("creator_concept_id") != actor_id:
        raise TaskExecutionAccessError(
            "task_execution_actor_mismatch",
            "The task execution belongs to a different actor",
        )
    if metadata.get("organisation_concept_id") != org_id:
        raise TaskExecutionAccessError(
            "task_execution_organisation_mismatch",
            "The task execution belongs to a different organisation",
        )
    return dict(doc)


def get_task_execution(
    task_execution_concept_id: str,
    *,
    actor_concept_id: str,
    organisation_concept_id: str,
) -> dict[str, Any]:
    doc = _get_task_execution_doc(
        task_execution_concept_id,
        actor_concept_id=actor_concept_id,
        organisation_concept_id=organisation_concept_id,
    )
    return _execution_response(doc)


def bind_task_execution_queue_record(
    task_execution_concept_id: str,
    *,
    actor_concept_id: str,
    organisation_concept_id: str,
    enqueue_submission_id: str,
    queue_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the exact durable queue row to its execution attempt."""

    doc = _get_task_execution_doc(
        task_execution_concept_id,
        actor_concept_id=actor_concept_id,
        organisation_concept_id=organisation_concept_id,
    )
    execution_id = str(doc["concept_id"])
    metadata = _metadata(doc)
    enqueue_id = _required_text(
        enqueue_submission_id,
        field="enqueue_submission_id",
        max_chars=200,
    )
    if metadata.get("enqueue_submission_id") != enqueue_id:
        raise TaskExecutionAccessError(
            "task_execution_enqueue_mismatch",
            "The queue submission does not match the task execution",
        )
    queue_id = _required_text(
        queue_record.get("queue_id"), field="queue_record.queue_id"
    )
    queue_execution_id = queue_record.get("task_execution_concept_id")
    queue_task_id = queue_record.get("task_concept_id")
    if queue_execution_id != execution_id or queue_task_id != metadata.get(
        "task_concept_id"
    ):
        raise TaskExecutionAccessError(
            "task_execution_queue_link_mismatch",
            "The queue record is linked to different task work",
        )
    existing_queue_id = metadata.get("queue_id")
    if existing_queue_id and existing_queue_id != queue_id:
        raise TaskExecutionTransitionError(
            "The task execution is already bound to another queue record"
        )
    if not existing_queue_id:
        now = _now()
        result = ConceptsRepository.update_one(
            {
                "concept_id": execution_id,
                "metadata.enqueue_submission_id": enqueue_id,
                "metadata.queue_id": None,
            },
            {
                "$set": {
                    "metadata.queue_id": queue_id,
                    "metadata.queue_bound_at": now,
                    "updated_at": now,
                }
            },
        )
        if int(getattr(result, "modified_count", 0) or 0) == 0:
            refreshed = _get_task_execution_doc(
                execution_id,
                actor_concept_id=actor_concept_id,
                organisation_concept_id=organisation_concept_id,
            )
            if _metadata(refreshed).get("queue_id") != queue_id:
                raise TaskExecutionTransitionError(
                    "The task execution queue binding changed concurrently"
                )
    return get_task_execution(
        execution_id,
        actor_concept_id=actor_concept_id,
        organisation_concept_id=organisation_concept_id,
    )


def reconcile_task_execution_queue_record(
    queue_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Create/bind the exact TaskExecution projected by a durable queue row.

    The queue row is the launch outbox.  This idempotent projection lets the
    server recover if it crashes after accepting the queue row but before the
    corresponding Vontology concept is created or bound.
    """

    if not isinstance(queue_record, Mapping):
        raise InvalidTaskExecutionData("queue_record must be an object")
    task_id = _concept_id(
        queue_record.get("task_concept_id"),
        field="queue_record.task_concept_id",
    )
    execution_id = _concept_id(
        queue_record.get("task_execution_concept_id"),
        field="queue_record.task_execution_concept_id",
    )
    actor_id = _concept_id(
        queue_record.get("user_concept_id"),
        field="queue_record.user_concept_id",
    )
    org_id = _concept_id(
        queue_record.get("organisation_concept_id"),
        field="queue_record.organisation_concept_id",
    )
    namespace = _required_text(
        queue_record.get("namespace"),
        field="queue_record.namespace",
    )
    enqueue_id = _required_text(
        queue_record.get("enqueue_submission_id"),
        field="queue_record.enqueue_submission_id",
        max_chars=200,
    )
    queue_id = _required_text(
        queue_record.get("queue_id"),
        field="queue_record.queue_id",
    )
    session_id = _required_text(
        queue_record.get("session_id"),
        field="queue_record.session_id",
    )
    if queue_record.get("execution_envelope_version") != 1:
        raise InvalidTaskExecutionData(
            "queue_record.execution_envelope_version must be 1"
        )
    envelope = _json_object(
        queue_record.get("execution_envelope"),
        field="queue_record.execution_envelope",
    )
    launch_id = _required_text(
        envelope.get("initiation_id"),
        field="queue_record.execution_envelope.initiation_id",
        max_chars=200,
    )
    workflow_inputs = envelope.get("workflow_inputs")
    if not isinstance(workflow_inputs, Mapping):
        raise InvalidTaskExecutionData(
            "queue_record.execution_envelope.workflow_inputs must be an object"
        )
    expected_values = {
        "task_concept_id": task_id,
        "task_execution_concept_id": execution_id,
        "authority_actor_concept_id": actor_id,
        "authority_organisation_concept_id": org_id,
        "authority_namespace": namespace,
        "executor_concept_id": VON_SYSTEM_CONCEPT_ID,
        "conversation_session_id": session_id,
    }
    for field, expected in expected_values.items():
        if workflow_inputs.get(field) != expected:
            raise TaskExecutionAccessError(
                "task_execution_queue_link_mismatch",
                f"The queue execution envelope {field} does not match its launch",
            )
    expected_execution_id = task_execution_concept_id_for_launch(
        task_concept_id=task_id,
        creator_concept_id=actor_id,
        organisation_concept_id=org_id,
        launch_request_id=launch_id,
    )
    expected_enqueue_id = task_execution_enqueue_submission_id_for_launch(
        task_concept_id=task_id,
        creator_concept_id=actor_id,
        organisation_concept_id=org_id,
        launch_request_id=launch_id,
    )
    if execution_id != expected_execution_id or enqueue_id != expected_enqueue_id:
        raise TaskExecutionAccessError(
            "task_execution_queue_identity_mismatch",
            "The queue identity does not match its logical task launch",
        )

    # The queue scope and envelope were frozen by the trusted launch route. Bind
    # that exact actor while re-reading the current TaskSpecification so a
    # cancelled, completed, reassigned, or moved task cannot start later.
    from ..security.access_control import override_current_actor
    from .task_management_service import TaskNotFoundError, get_task

    with override_current_actor(actor_id, org_id):
        frozen_task = {
            "task_concept_id": task_id,
            "status": "pending",
            "assignee_concept_id": VON_SYSTEM_CONCEPT_ID,
            "created_by_concept_id": actor_id,
            "organisation_concept_id": org_id,
            "originating_conversation_id": workflow_inputs.get(
                "originating_conversation_concept_id"
            ),
            "conversation_session_id": session_id,
            "conversation_name": queue_record.get("session_name"),
        }
        create_task_execution(
            task=frozen_task,
            creator_concept_id=actor_id,
            organisation_concept_id=org_id,
            namespace=namespace,
            launch_request_id=launch_id,
            enqueue_submission_id=enqueue_id,
            execution_envelope=envelope,
            execution_envelope_version=1,
        )
        bound_execution = bind_task_execution_queue_record(
            execution_id,
            actor_concept_id=actor_id,
            organisation_concept_id=org_id,
            enqueue_submission_id=enqueue_id,
            queue_record={
                **dict(queue_record),
                "queue_id": queue_id,
                "task_concept_id": task_id,
                "task_execution_concept_id": execution_id,
            },
        )

        # Re-read after materialising the attempt. If task authority changed
        # while the launch outbox was pending, the caller can terminalise both
        # the queue row and this now-durable execution without invoking Von.
        try:
            current_task = dict(get_task(task_id))
        except TaskNotFoundError as exc:
            raise TaskExecutionAccessError(
                "task_no_longer_available",
                "The task is no longer available for execution",
            ) from exc
        current_values = _task_launch_values(
            task=current_task,
            creator_concept_id=actor_id,
            organisation_concept_id=org_id,
            namespace=namespace,
        )
        if current_values["conversation_session_id"] != session_id:
            raise TaskExecutionAccessError(
                "task_execution_conversation_mismatch",
                "The task conversation changed before execution",
            )
        if workflow_inputs.get(
            "originating_conversation_concept_id"
        ) != current_values.get("originating_conversation_id"):
            raise TaskExecutionAccessError(
                "task_execution_conversation_mismatch",
                "The task originating conversation changed before execution",
            )
        return bound_execution


def transition_task_execution(
    task_execution_concept_id: str,
    *,
    actor_concept_id: str,
    organisation_concept_id: str,
    enqueue_submission_id: str,
    queue_id: str | None,
    status: str,
    result: str | None = None,
    progress_note: str | None = None,
    source: str = "queue_dispatcher",
) -> dict[str, Any]:
    """Move one execution attempt without changing its TaskSpecification."""

    doc = _get_task_execution_doc(
        task_execution_concept_id,
        actor_concept_id=actor_concept_id,
        organisation_concept_id=organisation_concept_id,
    )
    execution_id = str(doc["concept_id"])
    metadata = _metadata(doc)
    target_status = _required_text(status, field="status", max_chars=50).lower()
    if target_status not in TASK_EXECUTION_VALID_STATUSES:
        raise InvalidTaskExecutionData("Invalid task execution status")
    enqueue_id = _required_text(
        enqueue_submission_id,
        field="enqueue_submission_id",
        max_chars=200,
    )
    if metadata.get("enqueue_submission_id") != enqueue_id:
        raise TaskExecutionAccessError(
            "task_execution_enqueue_mismatch",
            "The queue submission does not match the task execution",
        )
    persisted_queue_id = metadata.get("queue_id")
    queue_id_clean = (
        _required_text(queue_id, field="queue_id") if queue_id is not None else None
    )
    if persisted_queue_id != queue_id_clean:
        raise TaskExecutionAccessError(
            "task_execution_queue_mismatch",
            "The queue record does not match the task execution",
        )
    current_status = str(metadata.get("execution_status") or "")
    if current_status == target_status:
        return _execution_response(doc)
    if target_status not in _ALLOWED_TRANSITIONS.get(current_status, set()):
        raise TaskExecutionTransitionError(
            f"Cannot transition task execution from {current_status or 'unknown'} "
            f"to {target_status}"
        )
    now = _now()
    source_clean = _required_text(source, field="source", max_chars=100)
    set_fields: dict[str, Any] = {
        "metadata.execution_status": target_status,
        "updated_at": now,
    }
    if target_status == TASK_EXECUTION_STATUS_IN_PROGRESS:
        set_fields["metadata.started_at"] = now
    if target_status in TASK_EXECUTION_TERMINAL_STATUSES:
        set_fields["metadata.ended_at"] = now
    if result is not None:
        set_fields["metadata.result"] = _required_text(
            result,
            field="result",
            max_chars=100_000,
        )
    if progress_note is not None:
        set_fields["metadata.progress_note"] = _required_text(
            progress_note,
            field="progress_note",
            max_chars=20_000,
        )
    update_result = ConceptsRepository.update_one(
        {
            "concept_id": execution_id,
            "metadata.execution_status": current_status,
            "metadata.enqueue_submission_id": enqueue_id,
            "metadata.queue_id": queue_id_clean,
        },
        {
            "$set": set_fields,
            "$push": {
                "metadata.status_history": {
                    "status": target_status,
                    "at": now,
                    "source": source_clean,
                }
            },
        },
    )
    if int(getattr(update_result, "modified_count", 0) or 0) == 0:
        refreshed = _get_task_execution_doc(
            execution_id,
            actor_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
        )
        if _metadata(refreshed).get("execution_status") == target_status:
            return _execution_response(refreshed)
        raise TaskExecutionTransitionError(
            "The task execution state changed concurrently"
        )

    _persist_singleton_text(
        execution_concept_id=execution_id,
        predicate=PREDICATE_HAS_EXECUTION_STATUS,
        text=target_status,
    )
    if target_status == TASK_EXECUTION_STATUS_IN_PROGRESS:
        _persist_singleton_text(
            execution_concept_id=execution_id,
            predicate=PREDICATE_HAS_START_TIME,
            text=now.isoformat(),
        )
    if target_status in TASK_EXECUTION_TERMINAL_STATUSES:
        _persist_singleton_text(
            execution_concept_id=execution_id,
            predicate=PREDICATE_HAS_END_TIME,
            text=now.isoformat(),
        )
    if result is not None:
        _persist_singleton_text(
            execution_concept_id=execution_id,
            predicate=PREDICATE_HAS_RESULT,
            text=str(set_fields["metadata.result"]),
        )
    if progress_note is not None:
        _persist_singleton_text(
            execution_concept_id=execution_id,
            predicate=PREDICATE_HAS_PROGRESS_NOTE,
            text=str(set_fields["metadata.progress_note"]),
        )
    return get_task_execution(
        execution_id,
        actor_concept_id=actor_concept_id,
        organisation_concept_id=organisation_concept_id,
    )


def mark_task_execution_dispatch_failed(
    task_execution_concept_id: str,
    *,
    actor_concept_id: str,
    organisation_concept_id: str,
    enqueue_submission_id: str,
    error: str,
) -> dict[str, Any]:
    """Record that this attempt failed before any queue row was bound."""

    return transition_task_execution(
        task_execution_concept_id,
        actor_concept_id=actor_concept_id,
        organisation_concept_id=organisation_concept_id,
        enqueue_submission_id=enqueue_submission_id,
        queue_id=None,
        status=TASK_EXECUTION_STATUS_FAILED,
        result=_required_text(error, field="error", max_chars=4_000),
        source="queue_enqueue",
    )


__all__ = [
    "TASK_EXECUTION_ACTIVE_STATUSES",
    "TASK_EXECUTION_STATUS_CANCELLED",
    "TASK_EXECUTION_STATUS_COMPLETED",
    "TASK_EXECUTION_STATUS_FAILED",
    "TASK_EXECUTION_STATUS_IN_PROGRESS",
    "TASK_EXECUTION_STATUS_PENDING",
    "TASK_EXECUTION_TERMINAL_STATUSES",
    "TASK_EXECUTION_TYPE_ID",
    "VON_SYSTEM_CONCEPT_ID",
    "InvalidTaskExecutionData",
    "TaskExecutionAccessError",
    "TaskExecutionError",
    "TaskExecutionNotFoundError",
    "TaskExecutionTransitionError",
    "bind_task_execution_queue_record",
    "create_task_execution",
    "get_task_execution",
    "mark_task_execution_dispatch_failed",
    "reconcile_task_execution_queue_record",
    "task_execution_concept_id_for_launch",
    "task_execution_enqueue_submission_id_for_launch",
    "transition_task_execution",
]
