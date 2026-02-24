"""Task management service for JVNAUTOSCI-1040.

Provides CRUD operations for task concepts, linking tasks to conversations,
assignees, and tracking status via Vontology relationships and text relations.

All tasks are stored as first-class Vontology concepts (type: #V#task_specification).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.text_value_service import (
    get_texts_for_concept,
    upsert_text_for_concept,
)
from ..utils.concept_id_utils import (
    ensure_v_concept_prefix,
)
from ..security.visibility_predicates import (
    SPECIFIC_TO_ORG_PREDICATES_WRITE,
    SPECIFIC_TO_USER_PREDICATES,
    set_specific_to_org_values,
    set_specific_to_user_values,
)
from .effort_unit_ontology_service import (
    ensure_effort_unit_ontology,
    extract_effort_unit_type_ids,
    persist_successor_effort_unit_type_links,
    resolve_successor_effort_unit_type_ids,
)
from .workflow_event_integration_service import (
    maybe_launch_effort_unit_completed_workflow,
    maybe_launch_task_created_workflow,
    maybe_launch_task_status_workflow,
)

logger = logging.getLogger(__name__)

# Task type concept IDs
TASK_SPECIFICATION_TYPE_ID = "#V#task_specification"
TASK_EXECUTION_TYPE_ID = "#V#task_execution"
CONVERSATION_TYPE_ID = "#V#conversation"

# Task-related predicates
PREDICATE_HAS_ASSIGNEE = "#V#hasAssignee"
PREDICATE_HAS_CREATED_BY = "#V#hasCreatedBy"
PREDICATE_HAS_ORIGINATING_CONVERSATION = "#V#hasOriginatingConversation"
PREDICATE_HAS_START_DATE = "#V#hasStartDate"
PREDICATE_HAS_DUE_DATE = "#V#hasDueDate"
PREDICATE_HAS_PRIORITY = "#V#hasPriority"
PREDICATE_HAS_TASK_STATUS = "#V#hasTaskStatus"
PREDICATE_HAS_DESCRIPTION = "#V#hasDescription"
PREDICATE_HAS_NAME = "#V#hasName"
PREDICATE_HAS_PARENT_TASK = "#V#hasParentTask"
PREDICATE_HAS_SUBTASK = "#V#hasSubtask"
PREDICATE_HAS_EPIC_TASK = "#V#hasEpicTask"

# Task dependency/link predicates
PREDICATE_TASK_DEPENDS_ON = "#V#dependsOnTask"
PREDICATE_TASK_REQUIRED_BY = "#V#isRequiredByTask"
PREDICATE_TASK_BLOCKS = "#V#blocksTask"
PREDICATE_TASK_BLOCKED_BY = "#V#isBlockedByTask"
PREDICATE_TASK_RELATED_TO = "#V#relatesToTask"

TASK_LINK_TYPE_TO_PREDICATE: Dict[str, str] = {
    "depends_on": PREDICATE_TASK_DEPENDS_ON,
    "required_by": PREDICATE_TASK_REQUIRED_BY,
    "blocks": PREDICATE_TASK_BLOCKS,
    "blocked_by": PREDICATE_TASK_BLOCKED_BY,
    "relates_to": PREDICATE_TASK_RELATED_TO,
}

TASK_LINK_TYPE_INVERSES: Dict[str, str] = {
    "depends_on": "required_by",
    "required_by": "depends_on",
    "blocks": "blocked_by",
    "blocked_by": "blocks",
}

TASK_LINK_PREDICATES: set[str] = set(TASK_LINK_TYPE_TO_PREDICATE.values())

# Metadata keys
TASK_METADATA_KEY_CONCEPT_TYPE = "concept_type"
TASK_METADATA_KEY_ORGANISATION = "organisation_concept_id"
TASK_METADATA_KEY_LABELS = "labels"
TASK_METADATA_KEY_COMPONENTS = "components"
TASK_METADATA_KEY_FIX_VERSIONS = "fix_versions"
TASK_METADATA_KEY_SPRINT_VALUES = "sprint_values"
TASK_METADATA_KEY_BACKLOG_RANK = "backlog_rank"
TASK_METADATA_KEY_JIRA_REPORTER_CONCEPT_ID = "jira_reporter_concept_id"
TASK_METADATA_KEY_JIRA_WATCHER_CONCEPT_IDS = "jira_watcher_concept_ids"
TASK_METADATA_KEY_COMMENTS = "comments"
TASK_METADATA_KEY_ATTACHMENTS = "attachments"
TASK_METADATA_KEY_WORKLOG = "worklog"
TASK_METADATA_KEY_HISTORY = "task_history"
TASK_METADATA_KEY_EXTERNAL_REFERENCES = "external_references"

# Valid task statuses
TASK_STATUS_PENDING = "pending"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_STATUS_BLOCKED = "blocked"

VALID_TASK_STATUSES = {
    TASK_STATUS_PENDING,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_BLOCKED,
}

# Valid priorities
PRIORITY_LOW = "low"
PRIORITY_MEDIUM = "medium"
PRIORITY_HIGH = "high"
PRIORITY_CRITICAL = "critical"

VALID_PRIORITIES = {PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH, PRIORITY_CRITICAL}

# Transition IDs intentionally mirror Jira-style "get transitions" and
# "transition" operations while preserving Von-native status values.
TASK_TRANSITION_DEFINITIONS: Dict[str, List[Dict[str, str]]] = {
    TASK_STATUS_PENDING: [
        {
            "transition_id": "start_progress",
            "name": "Start progress",
            "to_status": TASK_STATUS_IN_PROGRESS,
        },
        {
            "transition_id": "mark_blocked",
            "name": "Mark blocked",
            "to_status": TASK_STATUS_BLOCKED,
        },
        {
            "transition_id": "cancel_task",
            "name": "Cancel task",
            "to_status": TASK_STATUS_CANCELLED,
        },
    ],
    TASK_STATUS_IN_PROGRESS: [
        {
            "transition_id": "complete_task",
            "name": "Complete task",
            "to_status": TASK_STATUS_COMPLETED,
        },
        {
            "transition_id": "mark_blocked",
            "name": "Mark blocked",
            "to_status": TASK_STATUS_BLOCKED,
        },
        {
            "transition_id": "move_to_pending",
            "name": "Move to pending",
            "to_status": TASK_STATUS_PENDING,
        },
        {
            "transition_id": "cancel_task",
            "name": "Cancel task",
            "to_status": TASK_STATUS_CANCELLED,
        },
    ],
    TASK_STATUS_BLOCKED: [
        {
            "transition_id": "resume_progress",
            "name": "Resume progress",
            "to_status": TASK_STATUS_IN_PROGRESS,
        },
        {
            "transition_id": "move_to_pending",
            "name": "Move to pending",
            "to_status": TASK_STATUS_PENDING,
        },
        {
            "transition_id": "cancel_task",
            "name": "Cancel task",
            "to_status": TASK_STATUS_CANCELLED,
        },
    ],
    TASK_STATUS_COMPLETED: [
        {
            "transition_id": "reopen_pending",
            "name": "Reopen to pending",
            "to_status": TASK_STATUS_PENDING,
        },
        {
            "transition_id": "reopen_in_progress",
            "name": "Reopen to in progress",
            "to_status": TASK_STATUS_IN_PROGRESS,
        },
    ],
    TASK_STATUS_CANCELLED: [
        {
            "transition_id": "reopen_pending",
            "name": "Reopen to pending",
            "to_status": TASK_STATUS_PENDING,
        }
    ],
}


class TaskManagementError(Exception):
    """Base exception for task management operations."""

    pass


class TaskNotFoundError(TaskManagementError):
    """Task concept not found."""

    pass


class InvalidTaskDataError(TaskManagementError):
    """Invalid task data provided."""

    pass


def _generate_task_concept_id(title: str) -> str:
    """Generate a unique concept_id for a task.

    Uses a slug from the title plus a short UUID suffix for uniqueness.
    """
    # Create slug from title
    slug = title.lower().strip()
    slug = "".join(c if c.isalnum() or c == " " else "" for c in slug)
    slug = "_".join(slug.split()[:5])  # Max 5 words
    if not slug:
        slug = "task"

    # Add short UUID suffix
    short_uuid = uuid.uuid4().hex[:8]
    concept_id = f"#V#task_{slug}_{short_uuid}"

    return concept_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_task_concept_id(task_concept_id: str) -> str:
    normalised = ensure_v_concept_prefix(task_concept_id)
    if not normalised:
        raise TaskNotFoundError(f"Invalid task_concept_id: {task_concept_id}")
    return normalised


def _normalise_optional_concept_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return ensure_v_concept_prefix(cleaned)


def _normalise_transition_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    return cleaned or None


def _normalise_link_type(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidTaskDataError("link_type is required")
    cleaned = value.strip()
    if cleaned.startswith("#V#"):
        return cleaned

    normalised = (
        cleaned.lower()
        .replace("-", "_")
        .replace(" ", "_")
        .replace("__", "_")
        .strip("_")
    )
    aliases = {
        "is_blocked_by": "blocked_by",
        "is_required_by": "required_by",
        "depends": "depends_on",
        "related": "relates_to",
        "relation": "relates_to",
    }
    return aliases.get(normalised, normalised)


def _resolve_link_predicate(link_type: str) -> tuple[str, str, str | None]:
    normalised = _normalise_link_type(link_type)
    if normalised.startswith("#V#"):
        return normalised, normalised, None
    predicate = TASK_LINK_TYPE_TO_PREDICATE.get(normalised)
    if not predicate:
        supported = sorted(TASK_LINK_TYPE_TO_PREDICATE.keys())
        raise InvalidTaskDataError(
            f"Unsupported link_type '{link_type}'. Supported values: {supported}"
        )
    inverse = TASK_LINK_TYPE_INVERSES.get(normalised)
    return normalised, predicate, inverse


def _isoformat(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalise_string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidTaskDataError(f"{field_name} must be a list of non-empty strings")

    seen: set[str] = set()
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(cleaned)
    return result


def _metadata_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    seen: set[str] = set()
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(cleaned)
    return result


def _normalise_optional_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    raise InvalidTaskDataError(f"{field_name} must be a string")


def _normalise_labels(labels: Any) -> list[str]:
    return _normalise_string_list(labels, field_name="labels")


def _is_task_doc(doc: Dict[str, Any]) -> bool:
    relationships = doc.get("relationships", {})
    instance_of = relationships.get("is_an_instance_of", [])
    return TASK_SPECIFICATION_TYPE_ID in instance_of


def _get_task_doc(task_concept_id: str) -> tuple[str, Dict[str, Any]]:
    normalised_id = _normalise_task_concept_id(task_concept_id)
    doc = ConceptsRepository.find_one({"concept_id": normalised_id})
    if not doc:
        raise TaskNotFoundError(f"Task not found: {normalised_id}")
    if not _is_task_doc(doc):
        raise TaskNotFoundError(f"Concept is not a task: {normalised_id}")
    return normalised_id, doc


def _append_task_history_event(
    *,
    task_concept_id: str,
    event_type: str,
    actor_concept_id: str | None = None,
    details: Dict[str, Any] | None = None,
    touch_updated_at: bool = True,
) -> Dict[str, Any]:
    event = {
        "event_id": f"event_{uuid.uuid4().hex[:12]}",
        "event_type": event_type,
        "timestamp": _now().isoformat(),
        "actor_concept_id": _normalise_optional_concept_id(actor_concept_id),
        "details": details or {},
    }
    update_doc: Dict[str, Any] = {"$push": {f"metadata.{TASK_METADATA_KEY_HISTORY}": event}}
    if touch_updated_at:
        update_doc.setdefault("$set", {})
        update_doc["$set"]["updated_at"] = _now()
    ConceptsRepository.update_one({"concept_id": task_concept_id}, update_doc)
    return event


def _append_task_metadata_entry(
    *,
    task_concept_id: str,
    metadata_key: str,
    entry: Dict[str, Any],
) -> None:
    ConceptsRepository.update_one(
        {"concept_id": task_concept_id},
        {
            "$push": {f"metadata.{metadata_key}": entry},
            "$set": {"updated_at": _now()},
        },
    )


def _replace_task_metadata_list(
    *,
    task_concept_id: str,
    metadata_key: str,
    entries: list[Dict[str, Any]] | list[str],
) -> None:
    ConceptsRepository.update_one(
        {"concept_id": task_concept_id},
        {
            "$set": {
                f"metadata.{metadata_key}": entries,
                "updated_at": _now(),
            }
        },
    )


def _set_task_metadata_value(
    *,
    task_concept_id: str,
    metadata_key: str,
    value: Any,
) -> None:
    ConceptsRepository.update_one(
        {"concept_id": task_concept_id},
        {
            "$set": {
                f"metadata.{metadata_key}": value,
                "updated_at": _now(),
            }
        },
    )


def _task_parent_id_from_doc(doc: Dict[str, Any]) -> str | None:
    relationships = doc.get("relationships", {})
    parent_candidates = relationships.get(PREDICATE_HAS_PARENT_TASK) or []
    if isinstance(parent_candidates, list):
        for candidate in parent_candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    if isinstance(parent_candidates, str) and parent_candidates.strip():
        return parent_candidates
    return None


def _task_epic_id_from_doc(doc: Dict[str, Any]) -> str | None:
    relationships = doc.get("relationships", {})
    epic_candidates = relationships.get(PREDICATE_HAS_EPIC_TASK) or []
    if isinstance(epic_candidates, list):
        for candidate in epic_candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    if isinstance(epic_candidates, str) and epic_candidates.strip():
        return epic_candidates
    return None


def _first_relationship_value(raw: Any) -> str | None:
    if isinstance(raw, list):
        for candidate in raw:
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return None
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _task_subtask_ids_from_doc(doc: Dict[str, Any]) -> list[str]:
    relationships = doc.get("relationships", {})
    raw = relationships.get(PREDICATE_HAS_SUBTASK) or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item.strip()]


def _clear_task_datetime_text_relations(
    *,
    task_concept_id: str,
    predicate: str,
    log_field_name: str,
) -> None:
    from ..services.text_value_service import delete_text_relation

    for existing in get_texts_for_concept(
        task_concept_id,
        predicate=predicate,
        limit=100,
    ):
        relation_id = existing.get("relation_id")
        if isinstance(relation_id, str) and relation_id:
            try:
                delete_text_relation(task_concept_id, relation_id)
            except Exception as e:
                logger.debug(
                    "Failed to delete %s text relation: %s",
                    log_field_name,
                    e,
                )


def _task_links_from_doc(doc: Dict[str, Any]) -> list[Dict[str, str]]:
    relationships = doc.get("relationships", {})
    links: list[Dict[str, str]] = []
    for predicate, targets in relationships.items():
        if not isinstance(predicate, str) or predicate not in TASK_LINK_PREDICATES:
            continue
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list):
            continue
        for target in targets:
            if not isinstance(target, str) or not target.strip():
                continue
            normalised_link_type = next(
                (
                    link_type
                    for link_type, link_predicate in TASK_LINK_TYPE_TO_PREDICATE.items()
                    if link_predicate == predicate
                ),
                predicate,
            )
            links.append(
                {
                    "link_type": normalised_link_type,
                    "predicate": predicate,
                    "target_task_concept_id": target,
                }
            )
    return links


def _list_metadata_items(doc: Dict[str, Any], key: str) -> list[Dict[str, Any]]:
    metadata = doc.get("metadata") or {}
    raw = metadata.get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _would_create_parent_cycle(
    *,
    task_concept_id: str,
    candidate_parent_id: str,
) -> bool:
    # Walk parent chain of candidate parent. If we encounter task_concept_id
    # we would create a cycle.
    seen: set[str] = set()
    current = candidate_parent_id
    hop_cap = 256
    while current and hop_cap > 0:
        hop_cap -= 1
        if current == task_concept_id:
            return True
        if current in seen:
            return False
        seen.add(current)
        current_doc = ConceptsRepository.find_one(
            {"concept_id": current},
            projection={f"relationships.{PREDICATE_HAS_PARENT_TASK}": 1},
        )
        if not current_doc:
            return False
        current = _task_parent_id_from_doc(current_doc) or ""
    return False


def create_task(
    title: str,
    description: str,
    *,
    assignee_concept_id: Optional[str] = None,
    originating_session_id: Optional[str] = None,
    created_by_concept_id: Optional[str] = None,
    start_date: datetime | str | None = None,
    due_date: datetime | str | None = None,
    epic_task_concept_id: Optional[str] = None,
    components: list[str] | None = None,
    fix_versions: list[str] | None = None,
    sprint_values: list[str] | None = None,
    backlog_rank: str | None = None,
    priority: str = PRIORITY_MEDIUM,
    organisation_concept_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new task as a Vontology concept.

    Args:
        title: Task title (will be stored as hasName text relation)
        description: Task description (will be stored as hasDescription text relation)
        assignee_concept_id: Person/org concept_id responsible for the task
        originating_session_id: Chat session_id where task was created
        created_by_concept_id: Person/agent who created the task
        start_date: Optional start datetime (ISO string or datetime)
        due_date: Optional deadline datetime (ISO string or datetime)
        epic_task_concept_id: Optional epic concept_id for Jira-style epic linkage
        components: Optional Jira component names for planning parity
        fix_versions: Optional release/fix-version values for planning parity
        sprint_values: Optional sprint values for planning parity
        backlog_rank: Optional backlog rank marker for ordering parity
        priority: Task priority (low, medium, high, critical)
        organisation_concept_id: Organisation context, if applicable

    Returns:
        Dict with task_concept_id, title, status, and other metadata

    Raises:
        InvalidTaskDataError: If required data is missing or invalid
    """
    if not title or not isinstance(title, str):
        raise InvalidTaskDataError("Task title is required")

    if not description or not isinstance(description, str):
        raise InvalidTaskDataError("Task description is required")

    if priority not in VALID_PRIORITIES:
        raise InvalidTaskDataError(
            f"Invalid priority '{priority}'. Must be one of: {VALID_PRIORITIES}"
        )

    normalised_components = _normalise_string_list(components, field_name="components")
    normalised_fix_versions = _normalise_string_list(
        fix_versions,
        field_name="fix_versions",
    )
    normalised_sprint_values = _normalise_string_list(
        sprint_values,
        field_name="sprint_values",
    )
    normalised_backlog_rank = _normalise_optional_string(
        backlog_rank,
        field_name="backlog_rank",
    )

    parsed_start_date = _parse_datetime(start_date)
    if start_date is not None and parsed_start_date is None:
        raise InvalidTaskDataError("start_date must be an ISO 8601 datetime string")

    parsed_due_date = _parse_datetime(due_date)
    if due_date is not None and parsed_due_date is None:
        raise InvalidTaskDataError("due_date must be an ISO 8601 datetime string")

    if (
        parsed_start_date is not None
        and parsed_due_date is not None
        and parsed_start_date > parsed_due_date
    ):
        raise InvalidTaskDataError("start_date must be before or equal to due_date")

    # Generate unique concept_id
    task_concept_id = _generate_task_concept_id(title)

    # Best-effort ontology bootstrap so effort-unit predicates/types are available
    # for downstream lifecycle wiring. Task writes must remain available even if
    # ontology bootstrap encounters recoverable issues.
    try:
        ensure_effort_unit_ontology()
    except Exception as exc:
        logger.debug("Effort-unit ontology bootstrap skipped during task create: %s", exc)

    # Normalise concept IDs
    if assignee_concept_id:
        assignee_concept_id = ensure_v_concept_prefix(assignee_concept_id)
    if created_by_concept_id:
        created_by_concept_id = ensure_v_concept_prefix(created_by_concept_id)
    if organisation_concept_id:
        organisation_concept_id = ensure_v_concept_prefix(organisation_concept_id)
    epic_task_concept_id = _normalise_optional_concept_id(epic_task_concept_id)
    if epic_task_concept_id and epic_task_concept_id == task_concept_id:
        raise InvalidTaskDataError("A task cannot reference itself as epic")
    if epic_task_concept_id:
        _get_task_doc(epic_task_concept_id)

    now = _now()

    # Build relationships
    relationships: Dict[str, Any] = {
        "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID],
    }

    if assignee_concept_id:
        relationships[PREDICATE_HAS_ASSIGNEE] = [assignee_concept_id]
    if created_by_concept_id:
        relationships[PREDICATE_HAS_CREATED_BY] = [created_by_concept_id]
    if epic_task_concept_id:
        relationships[PREDICATE_HAS_EPIC_TASK] = [epic_task_concept_id]

    # Visibility scoping - task visible to creator and assignee
    visible_to_users = []
    if created_by_concept_id:
        visible_to_users.append(created_by_concept_id)
    if assignee_concept_id and assignee_concept_id not in visible_to_users:
        visible_to_users.append(assignee_concept_id)
    if visible_to_users:
        relationships = set_specific_to_user_values(relationships, visible_to_users)

    # Organisation scoping
    if organisation_concept_id:
        relationships = set_specific_to_org_values(
            relationships,
            [organisation_concept_id],
        )

    # Handle conversation linkage (lazy creation)
    conversation_concept_id = None
    if originating_session_id:
        from .conversation_concept_service import get_or_create_conversation_concept

        conversation_concept_id = get_or_create_conversation_concept(
            session_id=originating_session_id,
            owner_concept_id=created_by_concept_id,
            organisation_concept_id=organisation_concept_id,
        )
        if conversation_concept_id:
            relationships[PREDICATE_HAS_ORIGINATING_CONVERSATION] = [
                conversation_concept_id
            ]

    # Create the concept document
    concept_doc: Dict[str, Any] = {
        "concept_id": task_concept_id,
        "guid": str(uuid.uuid4()),
        "relationships": relationships,
        "created_at": now,
        "updated_at": now,
        "metadata": {
            TASK_METADATA_KEY_CONCEPT_TYPE: "task_specification",
            TASK_METADATA_KEY_ORGANISATION: organisation_concept_id,
            TASK_METADATA_KEY_LABELS: [],
            TASK_METADATA_KEY_COMPONENTS: normalised_components,
            TASK_METADATA_KEY_FIX_VERSIONS: normalised_fix_versions,
            TASK_METADATA_KEY_SPRINT_VALUES: normalised_sprint_values,
            TASK_METADATA_KEY_BACKLOG_RANK: normalised_backlog_rank,
            TASK_METADATA_KEY_COMMENTS: [],
            TASK_METADATA_KEY_ATTACHMENTS: [],
            TASK_METADATA_KEY_WORKLOG: [],
            TASK_METADATA_KEY_HISTORY: [],
            TASK_METADATA_KEY_EXTERNAL_REFERENCES: {},
        },
    }

    try:
        ConceptsRepository.insert_one(concept_doc)
        logger.info(f"Created task concept: {task_concept_id}")
    except Exception as e:
        logger.error(f"Failed to create task concept: {e}")
        raise TaskManagementError(f"Failed to create task: {e}") from e

    # Store title as hasName text relation
    try:
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_NAME,
            text=title,
            lang="en-NZ",
        )
    except Exception as e:
        logger.warning(f"Failed to store task title: {e}")

    # Store description as hasDescription text relation
    try:
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_DESCRIPTION,
            text=description,
            lang="en-NZ",
        )
    except Exception as e:
        logger.warning(f"Failed to store task description: {e}")

    # Store status as text relation
    try:
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_TASK_STATUS,
            text=TASK_STATUS_PENDING,
            lang="en",
        )
    except Exception as e:
        logger.warning(f"Failed to store task status: {e}")

    # Store priority as text relation
    try:
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_PRIORITY,
            text=priority,
            lang="en",
        )
    except Exception as e:
        logger.warning(f"Failed to store task priority: {e}")

    # Store start date if provided
    if parsed_start_date:
        try:
            upsert_text_for_concept(
                subject_concept_id=task_concept_id,
                predicate=PREDICATE_HAS_START_DATE,
                text=parsed_start_date.isoformat(),
                lang="en",
            )
        except Exception as e:
            logger.warning(f"Failed to store task start date: {e}")

    # Store due date if provided
    if parsed_due_date:
        try:
            upsert_text_for_concept(
                subject_concept_id=task_concept_id,
                predicate=PREDICATE_HAS_DUE_DATE,
                text=parsed_due_date.isoformat(),
                lang="en",
            )
        except Exception as e:
            logger.warning(f"Failed to store task due date: {e}")

    result = {
        "task_concept_id": task_concept_id,
        "title": title,
        "description": description,
        "status": TASK_STATUS_PENDING,
        "priority": priority,
        "assignee_concept_id": assignee_concept_id,
        "created_by_concept_id": created_by_concept_id,
        "originating_conversation_id": conversation_concept_id,
        "start_date": parsed_start_date.isoformat() if parsed_start_date else None,
        "due_date": parsed_due_date.isoformat() if parsed_due_date else None,
        "epic_task_concept_id": epic_task_concept_id,
        "components": normalised_components,
        "fix_versions": normalised_fix_versions,
        "sprint_values": normalised_sprint_values,
        "backlog_rank": normalised_backlog_rank,
        "organisation_concept_id": organisation_concept_id,
        "created_at": now.isoformat(),
    }

    try:
        result["history_event"] = _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_created",
            actor_concept_id=created_by_concept_id,
            details={
                "title": title,
                "priority": priority,
                "assignee_concept_id": assignee_concept_id,
                "originating_conversation_id": conversation_concept_id,
                "start_date": (
                    parsed_start_date.isoformat() if parsed_start_date else None
                ),
                "due_date": parsed_due_date.isoformat() if parsed_due_date else None,
                "epic_task_concept_id": epic_task_concept_id,
                "components": normalised_components,
                "fix_versions": normalised_fix_versions,
                "sprint_values": normalised_sprint_values,
                "backlog_rank": normalised_backlog_rank,
            },
            touch_updated_at=False,
        )
    except Exception as e:
        logger.debug("Failed to append task_created history event: %s", e)

    workflow_event_launch: dict[str, Any] | None = None
    # Event-driven workflow launch is best-effort and must not block task writes.
    try:
        workflow_event_launch = maybe_launch_task_created_workflow(
            task_concept_id=task_concept_id,
            created_by_concept_id=created_by_concept_id,
            organisation_concept_id=organisation_concept_id,
            title=title,
            priority=priority,
        )
        if isinstance(workflow_event_launch, dict):
            result["workflow_event_launch"] = workflow_event_launch
            if not bool(workflow_event_launch.get("triggered")):
                logger.info(
                    "Task-created workflow not triggered for %s: reason=%s hint=%s",
                    task_concept_id,
                    workflow_event_launch.get("reason"),
                    workflow_event_launch.get("hint"),
                )
    except Exception as e:
        logger.warning(
            "Task-created workflow launch skipped for %s: %s", task_concept_id, e
        )

    return result


def get_task(task_concept_id: str) -> Dict[str, Any]:
    """Get a task by concept_id.

    Args:
        task_concept_id: The task's concept_id

    Returns:
        Dict with task details

    Raises:
        TaskNotFoundError: If task not found
    """
    task_concept_id, doc = _get_task_doc(task_concept_id)
    return _build_task_response(doc)


def _build_task_response(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Build a task response dict from a concept document."""
    task_concept_id = doc.get("concept_id", "")
    relationships = doc.get("relationships", {})

    # Get text relations for title, description, status, priority, start/due dates
    texts = []
    try:
        texts = get_texts_for_concept(task_concept_id) or []
    except Exception:
        pass

    title = None
    description = None
    status = TASK_STATUS_PENDING
    priority = PRIORITY_MEDIUM
    start_date = None
    due_date = None

    for text_item in texts:
        predicate = text_item.get("predicate", "")
        text_value = text_item.get("text", "")

        if predicate in (PREDICATE_HAS_NAME, "hasName"):
            title = text_value
        elif predicate in (PREDICATE_HAS_DESCRIPTION, "hasDescription"):
            description = text_value
        elif predicate in (PREDICATE_HAS_TASK_STATUS, "hasTaskStatus"):
            status = text_value
        elif predicate in (PREDICATE_HAS_PRIORITY, "hasPriority"):
            priority = text_value
        elif predicate in (PREDICATE_HAS_START_DATE, "hasStartDate"):
            start_date = text_value
        elif predicate in (PREDICATE_HAS_DUE_DATE, "hasDueDate"):
            due_date = text_value

    # Get relationship values
    assignee = _first_relationship_value(relationships.get(PREDICATE_HAS_ASSIGNEE))
    created_by = _first_relationship_value(relationships.get(PREDICATE_HAS_CREATED_BY))
    originating_conversation = _first_relationship_value(
        relationships.get(PREDICATE_HAS_ORIGINATING_CONVERSATION)
    )

    metadata = doc.get("metadata", {})
    labels = _metadata_string_list(metadata.get(TASK_METADATA_KEY_LABELS))
    components = _metadata_string_list(metadata.get(TASK_METADATA_KEY_COMPONENTS))
    fix_versions = _metadata_string_list(metadata.get(TASK_METADATA_KEY_FIX_VERSIONS))
    sprint_values = _metadata_string_list(metadata.get(TASK_METADATA_KEY_SPRINT_VALUES))
    backlog_rank_raw = metadata.get(TASK_METADATA_KEY_BACKLOG_RANK)
    backlog_rank = (
        backlog_rank_raw.strip()
        if isinstance(backlog_rank_raw, str) and backlog_rank_raw.strip()
        else None
    )
    reporter_raw = metadata.get(TASK_METADATA_KEY_JIRA_REPORTER_CONCEPT_ID)
    reporter_concept_id = (
        _normalise_optional_concept_id(reporter_raw) if reporter_raw is not None else None
    )
    watcher_concept_ids_raw = metadata.get(TASK_METADATA_KEY_JIRA_WATCHER_CONCEPT_IDS)
    watcher_concept_ids: list[str] = []
    if isinstance(watcher_concept_ids_raw, list):
        for item in watcher_concept_ids_raw:
            normalised = _normalise_optional_concept_id(item)
            if normalised and normalised not in watcher_concept_ids:
                watcher_concept_ids.append(normalised)

    comments = _list_metadata_items(doc, TASK_METADATA_KEY_COMMENTS)
    attachments = _list_metadata_items(doc, TASK_METADATA_KEY_ATTACHMENTS)
    worklog = _list_metadata_items(doc, TASK_METADATA_KEY_WORKLOG)
    history = _list_metadata_items(doc, TASK_METADATA_KEY_HISTORY)
    raw_external_references = metadata.get(TASK_METADATA_KEY_EXTERNAL_REFERENCES)
    external_references = (
        raw_external_references if isinstance(raw_external_references, dict) else {}
    )
    parent_task_id = _task_parent_id_from_doc(doc)
    epic_task_id = _task_epic_id_from_doc(doc)
    subtask_ids = _task_subtask_ids_from_doc(doc)
    links = _task_links_from_doc(doc)
    worklog_total_minutes = 0
    for entry in worklog:
        spent = entry.get("time_spent_minutes")
        if isinstance(spent, int):
            worklog_total_minutes += spent

    # Fetch conversation details if available
    conversation_session_id = None
    conversation_name = None
    if originating_conversation:
        try:
            from .conversation_concept_service import get_conversation_concept

            conv_details = get_conversation_concept(originating_conversation)
            if conv_details:
                conversation_session_id = conv_details.get("session_id")
                conversation_name = conv_details.get("name") or conv_details.get(
                    "topic"
                )
        except Exception:
            pass

        # Fallback: get session name from chat_history if not in conversation concept
        if conversation_session_id and not conversation_name:
            try:
                from .chat_history_service import get_chat_history_collection_service

                chat_history_coll = get_chat_history_collection_service()
                if chat_history_coll is not None:
                    # Query by session_id (any user's session with that ID)
                    session_doc = chat_history_coll.find_one(
                        {"session_id": conversation_session_id},
                        {"session_name": 1},
                    )
                    if session_doc and session_doc.get("session_name"):
                        conversation_name = session_doc["session_name"]
            except Exception:
                pass

    return {
        "task_concept_id": task_concept_id,
        "title": title or doc.get("name", "Untitled Task"),
        "description": description,
        "status": status,
        "priority": priority,
        "assignee_concept_id": assignee,
        "created_by_concept_id": created_by,
        "originating_conversation_id": originating_conversation,
        "conversation_session_id": conversation_session_id,
        "conversation_name": conversation_name,
        "start_date": start_date,
        "due_date": due_date,
        "organisation_concept_id": metadata.get(TASK_METADATA_KEY_ORGANISATION),
        "labels": labels,
        "components": components,
        "fix_versions": fix_versions,
        "sprint_values": sprint_values,
        "backlog_rank": backlog_rank,
        "reporter_concept_id": reporter_concept_id,
        "watcher_concept_ids": watcher_concept_ids,
        "parent_task_concept_id": parent_task_id,
        "epic_task_concept_id": epic_task_id,
        "subtask_concept_ids": subtask_ids,
        "task_links": links,
        "comments_count": len(comments),
        "attachments_count": len(attachments),
        "worklog_entries_count": len(worklog),
        "worklog_total_minutes": worklog_total_minutes,
        "history_count": len(history),
        "external_references": external_references,
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


def update_task_status(task_concept_id: str, status: str) -> Dict[str, Any]:
    """Update a task's status.

    Args:
        task_concept_id: The task's concept_id
        status: New status (pending, in_progress, completed, cancelled, blocked)

    Returns:
        Updated task dict

    Raises:
        TaskNotFoundError: If task not found
        InvalidTaskDataError: If status is invalid
    """
    status = str(status or "").strip().lower()
    if status not in VALID_TASK_STATUSES:
        raise InvalidTaskDataError(
            f"Invalid status '{status}'. Must be one of: {VALID_TASK_STATUSES}"
        )

    task_concept_id, doc = _get_task_doc(task_concept_id)

    existing_task = _build_task_response(doc)
    previous_status = existing_task.get("status")
    if isinstance(previous_status, str) and previous_status == status:
        # No state transition occurred, so skip duplicate writes/events.
        return existing_task

    # Update status text relation
    try:
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_TASK_STATUS,
            text=status,
            lang="en",
        )
    except Exception as e:
        raise TaskManagementError(f"Failed to update task status: {e}") from e

    # Update timestamp
    now = _now()
    ConceptsRepository.update_one(
        {"concept_id": task_concept_id},
        {
            "$set": {"updated_at": now},
            "$inc": {"metadata.task_status_transition_count": 1},
        },
    )

    logger.info(f"Updated task {task_concept_id} status to {status}")
    try:
        _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_status_changed",
            actor_concept_id=existing_task.get("created_by_concept_id"),
            details={
                "from_status": previous_status,
                "to_status": status,
            },
        )
    except Exception as e:
        logger.debug("Failed to append task status history event: %s", e)
    updated_task = get_task(task_concept_id)
    updated_at = updated_task.get("updated_at")
    if isinstance(updated_at, datetime):
        updated_at_iso: str | None = updated_at.isoformat()
    elif updated_at is not None:
        updated_at_iso = str(updated_at)
    else:
        updated_at_iso = None

    if status == TASK_STATUS_COMPLETED:
        try:
            ensure_effort_unit_ontology()
            _, refreshed_doc = _get_task_doc(task_concept_id)
            successor_type_ids = resolve_successor_effort_unit_type_ids(
                effort_unit_doc=refreshed_doc
            )
            successor_linkage = persist_successor_effort_unit_type_links(
                source_effort_unit_id=task_concept_id,
                successor_type_ids=successor_type_ids,
            )
            updated_task["successor_effort_unit_linkage"] = successor_linkage

            completion_event_launch = maybe_launch_effort_unit_completed_workflow(
                effort_unit_concept_id=task_concept_id,
                effort_unit_type_ids=extract_effort_unit_type_ids(refreshed_doc),
                successor_effort_unit_type_ids=successor_type_ids,
                completed_at_iso=updated_at_iso,
                created_by_concept_id=updated_task.get("created_by_concept_id"),
                organisation_concept_id=updated_task.get("organisation_concept_id"),
            )
            updated_task["effort_unit_completion_workflow_event_launch"] = (
                completion_event_launch
            )
            if not bool(completion_event_launch.get("triggered")):
                logger.info(
                    "Effort-unit completion workflow not triggered for %s: reason=%s hint=%s",
                    task_concept_id,
                    completion_event_launch.get("reason"),
                    completion_event_launch.get("hint"),
                )
        except Exception as exc:
            logger.warning(
                "Effort-unit completion lifecycle linkage skipped for %s: %s",
                task_concept_id,
                exc,
            )

    try:
        workflow_event_launch = maybe_launch_task_status_workflow(
            task_concept_id=task_concept_id,
            previous_status=previous_status if isinstance(previous_status, str) else None,
            new_status=status,
            updated_at_iso=updated_at_iso,
            created_by_concept_id=updated_task.get("created_by_concept_id"),
            organisation_concept_id=updated_task.get("organisation_concept_id"),
        )
        if isinstance(workflow_event_launch, dict):
            updated_task["workflow_event_launch"] = workflow_event_launch
            if not bool(workflow_event_launch.get("triggered")):
                logger.info(
                    "Task-status workflow not triggered for %s (%s -> %s): reason=%s hint=%s",
                    task_concept_id,
                    previous_status,
                    status,
                    workflow_event_launch.get("reason"),
                    workflow_event_launch.get("hint"),
                )
    except Exception as e:
        logger.warning(
            "Task-status workflow launch skipped for %s (%s -> %s): %s",
            task_concept_id,
            previous_status,
            status,
            e,
        )

    return updated_task


def assign_task(task_concept_id: str, assignee_concept_id: str) -> Dict[str, Any]:
    """Assign or reassign a task to a person or organisation.

    Args:
        task_concept_id: The task's concept_id
        assignee_concept_id: The assignee's concept_id

    Returns:
        Updated task dict

    Raises:
        TaskNotFoundError: If task not found
    """
    normalised_task_id = _normalise_task_concept_id(task_concept_id)
    normalised_assignee_id = _normalise_optional_concept_id(assignee_concept_id)
    if not normalised_assignee_id:
        raise InvalidTaskDataError(
            f"Invalid assignee_concept_id: {assignee_concept_id}"
        )
    task_concept_id = normalised_task_id
    assignee_concept_id = normalised_assignee_id

    # Verify task exists
    _, doc = _get_task_doc(task_concept_id)

    # Remove existing assignee(s) first, then add new one
    existing_assignees = doc.get("relationships", {}).get(PREDICATE_HAS_ASSIGNEE, [])
    if isinstance(existing_assignees, str):
        existing_assignees = [existing_assignees]
    if not isinstance(existing_assignees, list):
        existing_assignees = []
    for old_assignee in existing_assignees:
        ConceptsRepository.mutate_relationship_edge(
            source_id=task_concept_id,
            kind=PREDICATE_HAS_ASSIGNEE,
            target_id=old_assignee,
            action="remove",
        )

    ConceptsRepository.mutate_relationship_edge(
        source_id=task_concept_id,
        kind=PREDICATE_HAS_ASSIGNEE,
        target_id=assignee_concept_id,
        action="add",
    )

    # Also add new assignee to all specific_to_user predicate variants for visibility.
    for predicate in SPECIFIC_TO_USER_PREDICATES:
        ConceptsRepository.mutate_relationship_edge(
            source_id=task_concept_id,
            kind=predicate,
            target_id=assignee_concept_id,
            action="add",
        )

    # Update timestamp
    now = _now()
    ConceptsRepository.update_one(
        {"concept_id": task_concept_id},
        {"$set": {"updated_at": now}},
    )

    logger.info(f"Assigned task {task_concept_id} to {assignee_concept_id}")
    try:
        _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_assigned",
            details={
                "assignee_concept_id": assignee_concept_id,
                "previous_assignees": existing_assignees,
            },
        )
    except Exception as e:
        logger.debug("Failed to append task assignment history event: %s", e)
    return get_task(task_concept_id)


def get_tasks_for_user(
    user_concept_id: str,
    *,
    status_filter: Optional[str] = None,
    include_created: bool = False,
) -> List[Dict[str, Any]]:
    """Get all tasks assigned to a user.

    Args:
        user_concept_id: The user's concept_id
        status_filter: Optional status to filter by
        include_created: If True, also include tasks created by the user

    Returns:
        List of task dicts
    """
    normalised_id = ensure_v_concept_prefix(user_concept_id)
    if not normalised_id:
        return []
    user_concept_id = normalised_id

    # Query for tasks assigned to this user
    query: Dict[str, Any] = {
        "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
        f"relationships.{PREDICATE_HAS_ASSIGNEE}": user_concept_id,
    }

    if include_created:
        # Expand query to include tasks created by user
        query = {
            "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
            "$or": [
                {f"relationships.{PREDICATE_HAS_ASSIGNEE}": user_concept_id},
                {f"relationships.{PREDICATE_HAS_CREATED_BY}": user_concept_id},
            ],
        }

    cursor = ConceptsRepository.find(query)
    tasks = [_build_task_response(doc) for doc in cursor]

    # Apply status filter if provided
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]

    return tasks


def get_tasks_for_conversation(session_id: str) -> List[Dict[str, Any]]:
    """Get all tasks originating from a conversation.

    Args:
        session_id: The chat session_id

    Returns:
        List of task dicts
    """
    # First, find the conversation concept
    from .conversation_concept_service import get_conversation_concept_by_session_id

    conversation_concept_id = get_conversation_concept_by_session_id(session_id)
    if not conversation_concept_id:
        return []

    # Query for tasks linked to this conversation
    query = {
        "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
        f"relationships.{PREDICATE_HAS_ORIGINATING_CONVERSATION}": conversation_concept_id,
    }

    cursor = ConceptsRepository.find(query)
    return [_build_task_response(doc) for doc in cursor]


def list_tasks(
    *,
    organisation_concept_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    priority_filter: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List tasks with optional filters.

    Args:
        organisation_concept_id: Filter by organisation
        status_filter: Filter by status
        priority_filter: Filter by priority
        limit: Maximum number of tasks to return

    Returns:
        List of task dicts
    """
    query: Dict[str, Any] = {
        "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
    }

    if organisation_concept_id:
        organisation_concept_id = ensure_v_concept_prefix(organisation_concept_id)
        query["metadata.organisation_concept_id"] = organisation_concept_id

    cursor = ConceptsRepository.find(query, limit=limit)
    tasks = [_build_task_response(doc) for doc in cursor]

    # Apply status and priority filters (post-query since they're in text relations)
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    if priority_filter:
        tasks = [t for t in tasks if t.get("priority") == priority_filter]

    return tasks


def search_tasks(
    *,
    query: str | None = None,
    status_filter: str | None = None,
    statuses: list[str] | None = None,
    assignee_concept_id: str | None = None,
    labels: list[str] | None = None,
    components: list[str] | None = None,
    fix_versions: list[str] | None = None,
    sprint_values: list[str] | None = None,
    backlog_rank: str | None = None,
    parent_task_concept_id: str | None = None,
    epic_task_concept_id: str | None = None,
    has_parent: bool | None = None,
    has_subtasks: bool | None = None,
    has_epic: bool | None = None,
    has_backlog_rank: bool | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    due_from: str | None = None,
    due_to: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    updated_from: str | None = None,
    updated_to: str | None = None,
    dependency_state: str | None = None,
    organisation_concept_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search tasks with Jira-style filters over Vontology-backed task concepts."""

    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    query_filter: Dict[str, Any] = {
        "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID
    }
    if organisation_concept_id:
        org_id = _normalise_optional_concept_id(organisation_concept_id)
        if not org_id:
            raise InvalidTaskDataError(
                f"Invalid organisation_concept_id: {organisation_concept_id}"
            )
        query_filter[f"metadata.{TASK_METADATA_KEY_ORGANISATION}"] = org_id

    # Use deterministic storage ordering before in-memory filters so paged reads do
    # not drift or duplicate items across offsets.
    docs = list(
        ConceptsRepository.find(
            query_filter,
            sort=[("updated_at", -1), ("created_at", -1), ("concept_id", 1)],
        )
    )
    tasks = [_build_task_response(doc) for doc in docs]

    status_values: set[str] = set()
    if isinstance(status_filter, str) and status_filter.strip():
        status_values.add(status_filter.strip().lower())
    if isinstance(statuses, list):
        for raw in statuses:
            if isinstance(raw, str) and raw.strip():
                status_values.add(raw.strip().lower())
    if status_values:
        tasks = [
            task
            for task in tasks
            if isinstance(task.get("status"), str)
            and str(task.get("status")).lower() in status_values
        ]

    if assignee_concept_id is not None:
        assignee_id = _normalise_optional_concept_id(assignee_concept_id)
        if not assignee_id:
            raise InvalidTaskDataError(
                f"Invalid assignee_concept_id: {assignee_concept_id}"
            )
        tasks = [
            task
            for task in tasks
            if task.get("assignee_concept_id") == assignee_id
        ]

    if labels is not None:
        required_labels = {
            label.lower() for label in _normalise_labels(labels) if label.strip()
        }
        if required_labels:
            tasks = [
                task
                for task in tasks
                if required_labels.issubset(
                    {
                        str(item).lower()
                        for item in (task.get("labels") or [])
                        if isinstance(item, str)
                    }
                )
            ]

    if components is not None:
        required_components = {
            component.lower()
            for component in _normalise_string_list(
                components,
                field_name="components",
            )
            if component.strip()
        }
        if required_components:
            tasks = [
                task
                for task in tasks
                if required_components.issubset(
                    {
                        str(item).lower()
                        for item in (task.get("components") or [])
                        if isinstance(item, str)
                    }
                )
            ]

    if fix_versions is not None:
        required_fix_versions = {
            fix_version.lower()
            for fix_version in _normalise_string_list(
                fix_versions,
                field_name="fix_versions",
            )
            if fix_version.strip()
        }
        if required_fix_versions:
            tasks = [
                task
                for task in tasks
                if required_fix_versions.issubset(
                    {
                        str(item).lower()
                        for item in (task.get("fix_versions") or [])
                        if isinstance(item, str)
                    }
                )
            ]

    if sprint_values is not None:
        required_sprints = {
            sprint.lower()
            for sprint in _normalise_string_list(
                sprint_values,
                field_name="sprint_values",
            )
            if sprint.strip()
        }
        if required_sprints:
            tasks = [
                task
                for task in tasks
                if required_sprints.issubset(
                    {
                        str(item).lower()
                        for item in (task.get("sprint_values") or [])
                        if isinstance(item, str)
                    }
                )
            ]

    if backlog_rank is not None:
        rank_value = _normalise_optional_string(
            backlog_rank,
            field_name="backlog_rank",
        )
        if rank_value:
            tasks = [
                task
                for task in tasks
                if isinstance(task.get("backlog_rank"), str)
                and str(task.get("backlog_rank")).strip() == rank_value
            ]
        else:
            tasks = [
                task
                for task in tasks
                if not isinstance(task.get("backlog_rank"), str)
                or not str(task.get("backlog_rank")).strip()
            ]

    if parent_task_concept_id is not None:
        parent_id = _normalise_optional_concept_id(parent_task_concept_id)
        if not parent_id:
            raise InvalidTaskDataError(
                f"Invalid parent_task_concept_id: {parent_task_concept_id}"
            )
        tasks = [
            task
            for task in tasks
            if task.get("parent_task_concept_id") == parent_id
        ]

    if epic_task_concept_id is not None:
        epic_id = _normalise_optional_concept_id(epic_task_concept_id)
        if not epic_id:
            raise InvalidTaskDataError(
                f"Invalid epic_task_concept_id: {epic_task_concept_id}"
            )
        tasks = [task for task in tasks if task.get("epic_task_concept_id") == epic_id]

    if isinstance(has_parent, bool):
        tasks = [
            task
            for task in tasks
            if bool(task.get("parent_task_concept_id")) is has_parent
        ]

    if isinstance(has_subtasks, bool):
        tasks = [
            task
            for task in tasks
            if bool(task.get("subtask_concept_ids")) is has_subtasks
        ]

    if isinstance(has_epic, bool):
        tasks = [
            task for task in tasks if bool(task.get("epic_task_concept_id")) is has_epic
        ]

    if isinstance(has_backlog_rank, bool):
        tasks = [
            task
            for task in tasks
            if bool(
                isinstance(task.get("backlog_rank"), str)
                and str(task.get("backlog_rank")).strip()
            )
            is has_backlog_rank
        ]

    start_from_dt = _parse_datetime(start_from)
    start_to_dt = _parse_datetime(start_to)
    if start_from or start_to:
        tasks = [
            task
            for task in tasks
            if (
                (parsed_start := _parse_datetime(task.get("start_date"))) is not None
                and (start_from_dt is None or parsed_start >= start_from_dt)
                and (start_to_dt is None or parsed_start <= start_to_dt)
            )
        ]

    due_from_dt = _parse_datetime(due_from)
    due_to_dt = _parse_datetime(due_to)
    if due_from or due_to:
        tasks = [
            task
            for task in tasks
            if (
                (parsed_due := _parse_datetime(task.get("due_date"))) is not None
                and (due_from_dt is None or parsed_due >= due_from_dt)
                and (due_to_dt is None or parsed_due <= due_to_dt)
            )
        ]

    created_from_dt = _parse_datetime(created_from)
    created_to_dt = _parse_datetime(created_to)
    if created_from or created_to:
        tasks = [
            task
            for task in tasks
            if (
                (parsed_created := _parse_datetime(task.get("created_at"))) is not None
                and (created_from_dt is None or parsed_created >= created_from_dt)
                and (created_to_dt is None or parsed_created <= created_to_dt)
            )
        ]

    updated_from_dt = _parse_datetime(updated_from)
    updated_to_dt = _parse_datetime(updated_to)
    if updated_from or updated_to:
        tasks = [
            task
            for task in tasks
            if (
                (parsed_updated := _parse_datetime(task.get("updated_at"))) is not None
                and (updated_from_dt is None or parsed_updated >= updated_from_dt)
                and (updated_to_dt is None or parsed_updated <= updated_to_dt)
            )
        ]

    if isinstance(query, str) and query.strip():
        query_lower = query.strip().lower()
        tasks = [
            task
            for task in tasks
            if query_lower in str(task.get("title") or "").lower()
            or query_lower in str(task.get("description") or "").lower()
        ]

    if isinstance(dependency_state, str) and dependency_state.strip():
        state = _normalise_link_type(dependency_state)
        if state == "blocking":
            state = "blocks"
        if state == "blocked":
            state = "blocked_by"
        valid_states = {
            "depends_on",
            "required_by",
            "blocks",
            "blocked_by",
            "relates_to",
        }
        if state not in valid_states:
            raise InvalidTaskDataError(
                f"Invalid dependency_state '{dependency_state}'. Supported values: {sorted(valid_states)}"
            )
        tasks = [
            task
            for task in tasks
            if any(
                isinstance(link, dict) and link.get("link_type") == state
                for link in (task.get("task_links") or [])
            )
        ]

    floor = datetime.min.replace(tzinfo=timezone.utc)
    tasks.sort(
        key=lambda task: (
            _parse_datetime(task.get("updated_at")) or floor,
            _parse_datetime(task.get("created_at")) or floor,
        ),
        reverse=True,
    )

    total = len(tasks)
    paged = tasks[offset : offset + limit]
    return {
        "tasks": paged,
        "total": total,
        "count": len(paged),
        "offset": offset,
        "limit": limit,
    }


def get_task_transitions(task_concept_id: str) -> Dict[str, Any]:
    task = get_task(task_concept_id)
    current_status = str(task.get("status") or TASK_STATUS_PENDING).lower()
    transitions = TASK_TRANSITION_DEFINITIONS.get(current_status, [])
    return {
        "task_concept_id": task.get("task_concept_id"),
        "current_status": current_status,
        "transitions": transitions,
        "count": len(transitions),
    }


def transition_task(
    task_concept_id: str,
    *,
    transition_id: str | None = None,
    to_status: str | None = None,
    actor_concept_id: str | None = None,
) -> Dict[str, Any]:
    transition_set = get_task_transitions(task_concept_id)
    transitions = transition_set.get("transitions") or []
    current_status = str(transition_set.get("current_status") or TASK_STATUS_PENDING)

    selected_transition: Dict[str, str] | None = None
    if isinstance(to_status, str) and to_status.strip():
        desired_status = to_status.strip().lower()
        for transition in transitions:
            if str(transition.get("to_status") or "").lower() == desired_status:
                selected_transition = transition
                break
    elif isinstance(transition_id, str) and transition_id.strip():
        desired_transition_id = _normalise_transition_id(transition_id)
        for transition in transitions:
            if (
                _normalise_transition_id(transition.get("transition_id"))
                == desired_transition_id
            ):
                selected_transition = transition
                break
    else:
        raise InvalidTaskDataError(
            "Provide either transition_id or to_status for task transition"
        )

    if not selected_transition:
        expected_transitions = [
            {
                "transition_id": item.get("transition_id"),
                "to_status": item.get("to_status"),
            }
            for item in transitions
        ]
        raise InvalidTaskDataError(
            f"Transition not available from status '{current_status}'. "
            f"Expected one of: {expected_transitions}"
        )

    next_status = str(selected_transition.get("to_status") or "").lower()
    updated_task = update_task_status(task_concept_id, next_status)
    try:
        _append_task_history_event(
            task_concept_id=str(updated_task.get("task_concept_id")),
            event_type="task_transitioned",
            actor_concept_id=actor_concept_id,
            details={
                "transition_id": selected_transition.get("transition_id"),
                "transition_name": selected_transition.get("name"),
                "from_status": current_status,
                "to_status": next_status,
            },
        )
    except Exception as e:
        logger.debug("Failed to append task transition history event: %s", e)

    return {
        "task_concept_id": updated_task.get("task_concept_id"),
        "from_status": current_status,
        "to_status": next_status,
        "transition": selected_transition,
        "task": updated_task,
    }


def unassign_task(task_concept_id: str) -> Dict[str, Any]:
    task_concept_id, doc = _get_task_doc(task_concept_id)
    current_assignees = doc.get("relationships", {}).get(PREDICATE_HAS_ASSIGNEE, [])
    if isinstance(current_assignees, str):
        current_assignees = [current_assignees]
    if not isinstance(current_assignees, list):
        current_assignees = []

    removed_assignees = [
        assignee
        for assignee in current_assignees
        if isinstance(assignee, str) and assignee
    ]
    if not removed_assignees:
        return get_task(task_concept_id)

    for assignee in removed_assignees:
        ConceptsRepository.mutate_relationship_edge(
            source_id=task_concept_id,
            kind=PREDICATE_HAS_ASSIGNEE,
            target_id=assignee,
            action="remove",
        )

    ConceptsRepository.update_one(
        {"concept_id": task_concept_id},
        {"$set": {"updated_at": _now()}},
    )
    try:
        _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_unassigned",
            details={"removed_assignees": removed_assignees},
        )
    except Exception as e:
        logger.debug("Failed to append unassign history event: %s", e)
    return get_task(task_concept_id)


def set_task_parent(
    task_concept_id: str,
    parent_task_concept_id: str | None,
    *,
    actor_concept_id: str | None = None,
) -> Dict[str, Any]:
    task_concept_id, task_doc = _get_task_doc(task_concept_id)
    old_parent_id = _task_parent_id_from_doc(task_doc)

    new_parent_id = _normalise_optional_concept_id(parent_task_concept_id)
    if parent_task_concept_id and not new_parent_id:
        raise InvalidTaskDataError(
            f"Invalid parent_task_concept_id: {parent_task_concept_id}"
        )

    if new_parent_id == task_concept_id:
        raise InvalidTaskDataError("A task cannot be its own parent")

    if new_parent_id:
        _get_task_doc(new_parent_id)
        if _would_create_parent_cycle(
            task_concept_id=task_concept_id,
            candidate_parent_id=new_parent_id,
        ):
            raise InvalidTaskDataError(
                "Cannot assign parent task: this would create a hierarchy cycle"
            )

    if old_parent_id and old_parent_id != new_parent_id:
        ConceptsRepository.mutate_relationship_edge(
            source_id=task_concept_id,
            kind=PREDICATE_HAS_PARENT_TASK,
            target_id=old_parent_id,
            action="remove",
            maintain_inverse=False,
        )
        ConceptsRepository.mutate_relationship_edge(
            source_id=old_parent_id,
            kind=PREDICATE_HAS_SUBTASK,
            target_id=task_concept_id,
            action="remove",
            maintain_inverse=False,
        )

    if new_parent_id and new_parent_id != old_parent_id:
        ConceptsRepository.mutate_relationship_edge(
            source_id=task_concept_id,
            kind=PREDICATE_HAS_PARENT_TASK,
            target_id=new_parent_id,
            action="add",
            maintain_inverse=False,
        )
        ConceptsRepository.mutate_relationship_edge(
            source_id=new_parent_id,
            kind=PREDICATE_HAS_SUBTASK,
            target_id=task_concept_id,
            action="add",
            maintain_inverse=False,
        )

    ConceptsRepository.update_one(
        {"concept_id": task_concept_id},
        {"$set": {"updated_at": _now()}},
    )
    try:
        _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_parent_changed",
            actor_concept_id=actor_concept_id,
            details={
                "previous_parent_task_concept_id": old_parent_id,
                "parent_task_concept_id": new_parent_id,
            },
        )
    except Exception as e:
        logger.debug("Failed to append parent-change history event: %s", e)

    return get_task(task_concept_id)


def create_subtask(
    parent_task_concept_id: str,
    *,
    title: str,
    description: str,
    assignee_concept_id: str | None = None,
    created_by_concept_id: str | None = None,
    start_date: datetime | str | None = None,
    due_date: datetime | str | None = None,
    epic_task_concept_id: str | None = None,
    priority: str = PRIORITY_MEDIUM,
    organisation_concept_id: str | None = None,
    originating_session_id: str | None = None,
) -> Dict[str, Any]:
    parent_task_concept_id, _ = _get_task_doc(parent_task_concept_id)
    created = create_task(
        title=title,
        description=description,
        assignee_concept_id=assignee_concept_id,
        created_by_concept_id=created_by_concept_id,
        start_date=start_date,
        due_date=due_date,
        epic_task_concept_id=epic_task_concept_id,
        priority=priority,
        organisation_concept_id=organisation_concept_id,
        originating_session_id=originating_session_id,
    )
    subtask_id = str(created.get("task_concept_id"))
    subtask = set_task_parent(
        subtask_id,
        parent_task_concept_id,
        actor_concept_id=created_by_concept_id,
    )
    return {
        "task": subtask,
        "parent_task_concept_id": parent_task_concept_id,
        "subtask_concept_id": subtask_id,
    }


def set_task_epic(
    task_concept_id: str,
    epic_task_concept_id: str | None,
    *,
    actor_concept_id: str | None = None,
) -> Dict[str, Any]:
    task_concept_id, task_doc = _get_task_doc(task_concept_id)
    old_epic_id = _task_epic_id_from_doc(task_doc)

    new_epic_id = _normalise_optional_concept_id(epic_task_concept_id)
    if epic_task_concept_id and not new_epic_id:
        raise InvalidTaskDataError(
            f"Invalid epic_task_concept_id: {epic_task_concept_id}"
        )
    if new_epic_id == task_concept_id:
        raise InvalidTaskDataError("A task cannot be its own epic")
    if new_epic_id:
        _get_task_doc(new_epic_id)

    if old_epic_id and old_epic_id != new_epic_id:
        ConceptsRepository.mutate_relationship_edge(
            source_id=task_concept_id,
            kind=PREDICATE_HAS_EPIC_TASK,
            target_id=old_epic_id,
            action="remove",
            maintain_inverse=False,
        )

    if new_epic_id and new_epic_id != old_epic_id:
        ConceptsRepository.mutate_relationship_edge(
            source_id=task_concept_id,
            kind=PREDICATE_HAS_EPIC_TASK,
            target_id=new_epic_id,
            action="add",
            maintain_inverse=False,
        )

    ConceptsRepository.update_one(
        {"concept_id": task_concept_id},
        {"$set": {"updated_at": _now()}},
    )
    try:
        _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_epic_changed",
            actor_concept_id=actor_concept_id,
            details={
                "previous_epic_task_concept_id": old_epic_id,
                "epic_task_concept_id": new_epic_id,
            },
        )
    except Exception as e:
        logger.debug("Failed to append epic-change history event: %s", e)

    return get_task(task_concept_id)


def link_tasks(
    source_task_concept_id: str,
    target_task_concept_id: str,
    *,
    link_type: str,
    actor_concept_id: str | None = None,
) -> Dict[str, Any]:
    source_task_concept_id, _ = _get_task_doc(source_task_concept_id)
    target_task_concept_id, _ = _get_task_doc(target_task_concept_id)
    if source_task_concept_id == target_task_concept_id:
        raise InvalidTaskDataError("Cannot link a task to itself")

    normalised_link_type, predicate, inverse_link_type = _resolve_link_predicate(
        link_type
    )
    linked = ConceptsRepository.mutate_relationship_edge(
        source_id=source_task_concept_id,
        kind=predicate,
        target_id=target_task_concept_id,
        action="add",
        maintain_inverse=False,
    )

    inverse_predicate = None
    if inverse_link_type:
        inverse_predicate = TASK_LINK_TYPE_TO_PREDICATE.get(inverse_link_type)
        if inverse_predicate:
            linked = (
                ConceptsRepository.mutate_relationship_edge(
                    source_id=target_task_concept_id,
                    kind=inverse_predicate,
                    target_id=source_task_concept_id,
                    action="add",
                    maintain_inverse=False,
                )
                or linked
            )

    now = _now()
    ConceptsRepository.update_one(
        {"concept_id": source_task_concept_id},
        {"$set": {"updated_at": now}},
    )
    ConceptsRepository.update_one(
        {"concept_id": target_task_concept_id},
        {"$set": {"updated_at": now}},
    )
    try:
        _append_task_history_event(
            task_concept_id=source_task_concept_id,
            event_type="task_link_added",
            actor_concept_id=actor_concept_id,
            details={
                "target_task_concept_id": target_task_concept_id,
                "link_type": normalised_link_type,
                "predicate": predicate,
            },
        )
    except Exception as e:
        logger.debug("Failed to append source link history event: %s", e)

    return {
        "source_task_concept_id": source_task_concept_id,
        "target_task_concept_id": target_task_concept_id,
        "link_type": normalised_link_type,
        "predicate": predicate,
        "inverse_link_type": inverse_link_type,
        "inverse_predicate": inverse_predicate,
        "linked": bool(linked),
    }


def unlink_tasks(
    source_task_concept_id: str,
    target_task_concept_id: str,
    *,
    link_type: str,
    actor_concept_id: str | None = None,
) -> Dict[str, Any]:
    source_task_concept_id, _ = _get_task_doc(source_task_concept_id)
    target_task_concept_id, _ = _get_task_doc(target_task_concept_id)

    normalised_link_type, predicate, inverse_link_type = _resolve_link_predicate(
        link_type
    )
    unlinked = ConceptsRepository.mutate_relationship_edge(
        source_id=source_task_concept_id,
        kind=predicate,
        target_id=target_task_concept_id,
        action="remove",
        maintain_inverse=False,
    )

    inverse_predicate = None
    if inverse_link_type:
        inverse_predicate = TASK_LINK_TYPE_TO_PREDICATE.get(inverse_link_type)
        if inverse_predicate:
            unlinked = (
                ConceptsRepository.mutate_relationship_edge(
                    source_id=target_task_concept_id,
                    kind=inverse_predicate,
                    target_id=source_task_concept_id,
                    action="remove",
                    maintain_inverse=False,
                )
                or unlinked
            )

    now = _now()
    ConceptsRepository.update_one(
        {"concept_id": source_task_concept_id},
        {"$set": {"updated_at": now}},
    )
    ConceptsRepository.update_one(
        {"concept_id": target_task_concept_id},
        {"$set": {"updated_at": now}},
    )
    try:
        _append_task_history_event(
            task_concept_id=source_task_concept_id,
            event_type="task_link_removed",
            actor_concept_id=actor_concept_id,
            details={
                "target_task_concept_id": target_task_concept_id,
                "link_type": normalised_link_type,
                "predicate": predicate,
            },
        )
    except Exception as e:
        logger.debug("Failed to append source unlink history event: %s", e)

    return {
        "source_task_concept_id": source_task_concept_id,
        "target_task_concept_id": target_task_concept_id,
        "link_type": normalised_link_type,
        "predicate": predicate,
        "inverse_link_type": inverse_link_type,
        "inverse_predicate": inverse_predicate,
        "unlinked": bool(unlinked),
    }


def add_task_comment(
    task_concept_id: str,
    *,
    body: str,
    author_concept_id: str | None = None,
) -> Dict[str, Any]:
    task_concept_id, _ = _get_task_doc(task_concept_id)
    if not isinstance(body, str) or not body.strip():
        raise InvalidTaskDataError("Comment body must be a non-empty string")

    comment = {
        "comment_id": f"comment_{uuid.uuid4().hex[:12]}",
        "body": body.strip(),
        "author_concept_id": _normalise_optional_concept_id(author_concept_id),
        "created_at": _now().isoformat(),
    }
    _append_task_metadata_entry(
        task_concept_id=task_concept_id,
        metadata_key=TASK_METADATA_KEY_COMMENTS,
        entry=comment,
    )
    try:
        _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_comment_added",
            actor_concept_id=author_concept_id,
            details={"comment_id": comment["comment_id"]},
            touch_updated_at=False,
        )
    except Exception as e:
        logger.debug("Failed to append comment history event: %s", e)
    return comment


def list_task_comments(
    task_concept_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    task_concept_id, doc = _get_task_doc(task_concept_id)
    comments = _list_metadata_items(doc, TASK_METADATA_KEY_COMMENTS)
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    total = len(comments)
    window = comments[offset : offset + limit]
    return {
        "task_concept_id": task_concept_id,
        "comments": window,
        "total": total,
        "count": len(window),
        "offset": offset,
        "limit": limit,
    }


def add_task_attachment(
    task_concept_id: str,
    *,
    filename: str,
    uri: str,
    media_type: str | None = None,
    size_bytes: int | None = None,
    added_by_concept_id: str | None = None,
    note: str | None = None,
) -> Dict[str, Any]:
    task_concept_id, _ = _get_task_doc(task_concept_id)
    if not isinstance(filename, str) or not filename.strip():
        raise InvalidTaskDataError("filename is required")
    if not isinstance(uri, str) or not uri.strip():
        raise InvalidTaskDataError("uri is required")

    attachment = {
        "attachment_id": f"attachment_{uuid.uuid4().hex[:12]}",
        "filename": filename.strip(),
        "uri": uri.strip(),
        "media_type": media_type.strip() if isinstance(media_type, str) else None,
        "size_bytes": (
            size_bytes if isinstance(size_bytes, int) and size_bytes >= 0 else None
        ),
        "note": note.strip() if isinstance(note, str) and note.strip() else None,
        "added_by_concept_id": _normalise_optional_concept_id(added_by_concept_id),
        "created_at": _now().isoformat(),
    }
    _append_task_metadata_entry(
        task_concept_id=task_concept_id,
        metadata_key=TASK_METADATA_KEY_ATTACHMENTS,
        entry=attachment,
    )
    try:
        _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_attachment_added",
            actor_concept_id=added_by_concept_id,
            details={
                "attachment_id": attachment["attachment_id"],
                "filename": attachment["filename"],
            },
            touch_updated_at=False,
        )
    except Exception as e:
        logger.debug("Failed to append attachment history event: %s", e)
    return attachment


def list_task_attachments(
    task_concept_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    task_concept_id, doc = _get_task_doc(task_concept_id)
    attachments = _list_metadata_items(doc, TASK_METADATA_KEY_ATTACHMENTS)
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    total = len(attachments)
    window = attachments[offset : offset + limit]
    return {
        "task_concept_id": task_concept_id,
        "attachments": window,
        "total": total,
        "count": len(window),
        "offset": offset,
        "limit": limit,
    }


def add_task_worklog(
    task_concept_id: str,
    *,
    time_spent_minutes: int,
    author_concept_id: str | None = None,
    comment: str | None = None,
    started_at: str | None = None,
) -> Dict[str, Any]:
    task_concept_id, _ = _get_task_doc(task_concept_id)
    if not isinstance(time_spent_minutes, int) or time_spent_minutes <= 0:
        raise InvalidTaskDataError("time_spent_minutes must be a positive integer")

    started = _parse_datetime(started_at) if started_at else None
    entry = {
        "worklog_id": f"worklog_{uuid.uuid4().hex[:12]}",
        "time_spent_minutes": time_spent_minutes,
        "author_concept_id": _normalise_optional_concept_id(author_concept_id),
        "comment": comment.strip() if isinstance(comment, str) and comment.strip() else None,
        "started_at": _isoformat(started) or _now().isoformat(),
        "created_at": _now().isoformat(),
    }
    _append_task_metadata_entry(
        task_concept_id=task_concept_id,
        metadata_key=TASK_METADATA_KEY_WORKLOG,
        entry=entry,
    )
    try:
        _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_worklog_added",
            actor_concept_id=author_concept_id,
            details={
                "worklog_id": entry["worklog_id"],
                "time_spent_minutes": time_spent_minutes,
            },
            touch_updated_at=False,
        )
    except Exception as e:
        logger.debug("Failed to append worklog history event: %s", e)
    return entry


def list_task_worklog(
    task_concept_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    task_concept_id, doc = _get_task_doc(task_concept_id)
    worklog = _list_metadata_items(doc, TASK_METADATA_KEY_WORKLOG)
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    total_minutes = 0
    for row in worklog:
        minutes = row.get("time_spent_minutes")
        if isinstance(minutes, int):
            total_minutes += minutes

    window = worklog[offset : offset + limit]
    return {
        "task_concept_id": task_concept_id,
        "worklog": window,
        "total": len(worklog),
        "count": len(window),
        "offset": offset,
        "limit": limit,
        "total_time_spent_minutes": total_minutes,
    }


def get_task_history(
    task_concept_id: str,
    *,
    limit: int = 200,
    offset: int = 0,
) -> Dict[str, Any]:
    task_concept_id, doc = _get_task_doc(task_concept_id)
    history = _list_metadata_items(doc, TASK_METADATA_KEY_HISTORY)
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    window = history[offset : offset + limit]
    return {
        "task_concept_id": task_concept_id,
        "history": window,
        "total": len(history),
        "count": len(window),
        "offset": offset,
        "limit": limit,
    }


def update_task_fields(
    task_concept_id: str,
    *,
    fields: Dict[str, Any],
    actor_concept_id: str | None = None,
) -> Dict[str, Any]:
    task_concept_id = _normalise_task_concept_id(task_concept_id)
    _, task_doc = _get_task_doc(task_concept_id)

    if not isinstance(fields, dict) or not fields:
        raise InvalidTaskDataError("fields must be a non-empty dict")

    existing_task = _build_task_response(task_doc)
    changed_fields: list[str] = []
    warnings: list[str] = []

    current_start = _parse_datetime(existing_task.get("start_date"))
    current_due = _parse_datetime(existing_task.get("due_date"))
    next_start = current_start
    next_due = current_due

    if "start_date" in fields:
        raw_start = fields.get("start_date")
        if raw_start is None or (isinstance(raw_start, str) and not raw_start.strip()):
            next_start = None
        else:
            next_start = _parse_datetime(raw_start)
            if next_start is None:
                raise InvalidTaskDataError(
                    "start_date must be an ISO 8601 datetime string"
                )

    if "due_date" in fields:
        raw_due = fields.get("due_date")
        if raw_due is None or (isinstance(raw_due, str) and not raw_due.strip()):
            next_due = None
        else:
            next_due = _parse_datetime(raw_due)
            if next_due is None:
                raise InvalidTaskDataError("due_date must be an ISO 8601 datetime string")

    if next_start is not None and next_due is not None and next_start > next_due:
        raise InvalidTaskDataError("start_date must be before or equal to due_date")

    if "status" in fields:
        update_task_status(task_concept_id, str(fields.get("status") or ""))
        changed_fields.append("status")

    assignee_field_present = "assignee_concept_id" in fields or "assignee_id" in fields
    if assignee_field_present:
        raw_assignee = fields.get("assignee_concept_id", fields.get("assignee_id"))
        if raw_assignee is None or (
            isinstance(raw_assignee, str) and not raw_assignee.strip()
        ):
            unassign_task(task_concept_id)
            changed_fields.append("assignee_concept_id")
        else:
            assign_task(task_concept_id, str(raw_assignee))
            changed_fields.append("assignee_concept_id")

    created_by_field_present = (
        "created_by_concept_id" in fields or "creator_concept_id" in fields
    )
    if created_by_field_present:
        raw_created_by = fields.get("created_by_concept_id", fields.get("creator_concept_id"))
        existing_created_by_raw = (
            (task_doc.get("relationships") or {}).get(PREDICATE_HAS_CREATED_BY, [])
        )
        if isinstance(existing_created_by_raw, str):
            existing_created_by = [existing_created_by_raw]
        elif isinstance(existing_created_by_raw, list):
            existing_created_by = [
                candidate
                for candidate in existing_created_by_raw
                if isinstance(candidate, str) and candidate.strip()
            ]
        else:
            existing_created_by = []

        if raw_created_by is None or (
            isinstance(raw_created_by, str) and not raw_created_by.strip()
        ):
            for existing_creator in existing_created_by:
                ConceptsRepository.mutate_relationship_edge(
                    source_id=task_concept_id,
                    kind=PREDICATE_HAS_CREATED_BY,
                    target_id=existing_creator,
                    action="remove",
                    maintain_inverse=False,
                )
        else:
            created_by_concept_id = _normalise_optional_concept_id(raw_created_by)
            if not created_by_concept_id:
                raise InvalidTaskDataError(
                    f"Invalid created_by_concept_id: {raw_created_by}"
                )
            if not ConceptsRepository.find_one(
                {"concept_id": created_by_concept_id},
                projection={"_id": 1},
            ):
                raise InvalidTaskDataError(
                    f"created_by_concept_id not found: {created_by_concept_id}"
                )

            for existing_creator in existing_created_by:
                if existing_creator == created_by_concept_id:
                    continue
                ConceptsRepository.mutate_relationship_edge(
                    source_id=task_concept_id,
                    kind=PREDICATE_HAS_CREATED_BY,
                    target_id=existing_creator,
                    action="remove",
                    maintain_inverse=False,
                )

            if created_by_concept_id not in existing_created_by:
                ConceptsRepository.mutate_relationship_edge(
                    source_id=task_concept_id,
                    kind=PREDICATE_HAS_CREATED_BY,
                    target_id=created_by_concept_id,
                    action="add",
                    maintain_inverse=False,
                )
        changed_fields.append("created_by_concept_id")

    if "title" in fields:
        title_value = fields.get("title")
        if not isinstance(title_value, str) or not title_value.strip():
            raise InvalidTaskDataError("title must be a non-empty string")
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_NAME,
            text=title_value.strip(),
            lang="en-NZ",
        )
        changed_fields.append("title")

    if "description" in fields:
        description_value = fields.get("description")
        if not isinstance(description_value, str) or not description_value.strip():
            raise InvalidTaskDataError("description must be a non-empty string")
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_DESCRIPTION,
            text=description_value.strip(),
            lang="en-NZ",
        )
        changed_fields.append("description")

    if "priority" in fields:
        priority_value = str(fields.get("priority") or "").strip().lower()
        if priority_value not in VALID_PRIORITIES:
            raise InvalidTaskDataError(
                f"Invalid priority '{priority_value}'. Must be one of: {VALID_PRIORITIES}"
            )
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_PRIORITY,
            text=priority_value,
            lang="en",
        )
        changed_fields.append("priority")

    if "due_date" in fields:
        due_value = fields.get("due_date")
        if due_value is None or (isinstance(due_value, str) and not due_value.strip()):
            _clear_task_datetime_text_relations(
                task_concept_id=task_concept_id,
                predicate=PREDICATE_HAS_DUE_DATE,
                log_field_name="due-date",
            )
            changed_fields.append("due_date")
        else:
            parsed_due = _parse_datetime(due_value)
            if not parsed_due:
                raise InvalidTaskDataError("due_date must be an ISO 8601 datetime string")
            upsert_text_for_concept(
                subject_concept_id=task_concept_id,
                predicate=PREDICATE_HAS_DUE_DATE,
                text=parsed_due.isoformat(),
                lang="en",
            )
            changed_fields.append("due_date")

    if "start_date" in fields:
        start_value = fields.get("start_date")
        if start_value is None or (
            isinstance(start_value, str) and not start_value.strip()
        ):
            _clear_task_datetime_text_relations(
                task_concept_id=task_concept_id,
                predicate=PREDICATE_HAS_START_DATE,
                log_field_name="start-date",
            )
            changed_fields.append("start_date")
        else:
            parsed_start = _parse_datetime(start_value)
            if not parsed_start:
                raise InvalidTaskDataError(
                    "start_date must be an ISO 8601 datetime string"
                )
            upsert_text_for_concept(
                subject_concept_id=task_concept_id,
                predicate=PREDICATE_HAS_START_DATE,
                text=parsed_start.isoformat(),
                lang="en",
            )
            changed_fields.append("start_date")

    if "labels" in fields:
        labels_value = _normalise_labels(fields.get("labels"))
        _replace_task_metadata_list(
            task_concept_id=task_concept_id,
            metadata_key=TASK_METADATA_KEY_LABELS,
            entries=labels_value,
        )
        changed_fields.append("labels")

    if "components" in fields:
        components_value = _normalise_string_list(
            fields.get("components"),
            field_name="components",
        )
        _replace_task_metadata_list(
            task_concept_id=task_concept_id,
            metadata_key=TASK_METADATA_KEY_COMPONENTS,
            entries=components_value,
        )
        changed_fields.append("components")

    if "fix_versions" in fields:
        fix_versions_value = _normalise_string_list(
            fields.get("fix_versions"),
            field_name="fix_versions",
        )
        _replace_task_metadata_list(
            task_concept_id=task_concept_id,
            metadata_key=TASK_METADATA_KEY_FIX_VERSIONS,
            entries=fix_versions_value,
        )
        changed_fields.append("fix_versions")

    if "sprint_values" in fields:
        sprint_values_value = _normalise_string_list(
            fields.get("sprint_values"),
            field_name="sprint_values",
        )
        _replace_task_metadata_list(
            task_concept_id=task_concept_id,
            metadata_key=TASK_METADATA_KEY_SPRINT_VALUES,
            entries=sprint_values_value,
        )
        changed_fields.append("sprint_values")

    if "backlog_rank" in fields:
        backlog_rank_value = _normalise_optional_string(
            fields.get("backlog_rank"),
            field_name="backlog_rank",
        )
        _set_task_metadata_value(
            task_concept_id=task_concept_id,
            metadata_key=TASK_METADATA_KEY_BACKLOG_RANK,
            value=backlog_rank_value,
        )
        changed_fields.append("backlog_rank")

    if "organisation_concept_id" in fields:
        raw_org = fields.get("organisation_concept_id")
        if raw_org is None or (isinstance(raw_org, str) and not raw_org.strip()):
            organisation_concept_id = None
        else:
            organisation_concept_id = _normalise_optional_concept_id(raw_org)
            if not organisation_concept_id:
                raise InvalidTaskDataError(
                    f"Invalid organisation_concept_id: {raw_org}"
                )
            if not ConceptsRepository.find_one(
                {"concept_id": organisation_concept_id},
                projection={"_id": 1},
            ):
                raise InvalidTaskDataError(
                    f"organisation_concept_id not found: {organisation_concept_id}"
                )
        _set_task_metadata_value(
            task_concept_id=task_concept_id,
            metadata_key=TASK_METADATA_KEY_ORGANISATION,
            value=organisation_concept_id,
        )
        relationship_updates: Dict[str, Any] = {}
        org_targets = [organisation_concept_id] if organisation_concept_id else []
        for predicate in SPECIFIC_TO_ORG_PREDICATES_WRITE:
            relationship_updates[f"relationships.{predicate}"] = list(org_targets)
        ConceptsRepository.update_one(
            {"concept_id": task_concept_id},
            {"$set": relationship_updates},
        )
        changed_fields.append("organisation_concept_id")

    if "reporter_concept_id" in fields:
        raw_reporter = fields.get("reporter_concept_id")
        if raw_reporter is None or (
            isinstance(raw_reporter, str) and not raw_reporter.strip()
        ):
            reporter_concept_id = None
        else:
            reporter_concept_id = _normalise_optional_concept_id(raw_reporter)
            if not reporter_concept_id:
                raise InvalidTaskDataError(
                    f"Invalid reporter_concept_id: {raw_reporter}"
                )
            if not ConceptsRepository.find_one(
                {"concept_id": reporter_concept_id},
                projection={"_id": 1},
            ):
                raise InvalidTaskDataError(
                    f"reporter_concept_id not found: {reporter_concept_id}"
                )
        _set_task_metadata_value(
            task_concept_id=task_concept_id,
            metadata_key=TASK_METADATA_KEY_JIRA_REPORTER_CONCEPT_ID,
            value=reporter_concept_id,
        )
        changed_fields.append("reporter_concept_id")

    watcher_field_present = (
        "watcher_concept_ids" in fields or "watchers_concept_ids" in fields
    )
    if watcher_field_present:
        raw_watchers = fields.get(
            "watcher_concept_ids",
            fields.get("watchers_concept_ids"),
        )
        watcher_concept_ids: list[str] = []
        if raw_watchers is None:
            watcher_concept_ids = []
        elif isinstance(raw_watchers, list):
            for raw_watcher in raw_watchers:
                watcher_concept_id = _normalise_optional_concept_id(raw_watcher)
                if not watcher_concept_id:
                    raise InvalidTaskDataError(
                        "watcher_concept_ids must contain valid concept IDs"
                    )
                if not ConceptsRepository.find_one(
                    {"concept_id": watcher_concept_id},
                    projection={"_id": 1},
                ):
                    raise InvalidTaskDataError(
                        f"watcher_concept_id not found: {watcher_concept_id}"
                    )
                if watcher_concept_id not in watcher_concept_ids:
                    watcher_concept_ids.append(watcher_concept_id)
        else:
            raise InvalidTaskDataError(
                "watcher_concept_ids must be a list (or null to clear)"
            )
        _replace_task_metadata_list(
            task_concept_id=task_concept_id,
            metadata_key=TASK_METADATA_KEY_JIRA_WATCHER_CONCEPT_IDS,
            entries=watcher_concept_ids,
        )
        changed_fields.append("watcher_concept_ids")

    if "parent_task_concept_id" in fields:
        parent_raw = fields.get("parent_task_concept_id")
        if parent_raw is None or (isinstance(parent_raw, str) and not parent_raw.strip()):
            set_task_parent(task_concept_id, None, actor_concept_id=actor_concept_id)
        else:
            set_task_parent(
                task_concept_id,
                str(parent_raw),
                actor_concept_id=actor_concept_id,
            )
        changed_fields.append("parent_task_concept_id")

    if "epic_task_concept_id" in fields:
        epic_raw = fields.get("epic_task_concept_id")
        if epic_raw is None or (isinstance(epic_raw, str) and not epic_raw.strip()):
            set_task_epic(task_concept_id, None, actor_concept_id=actor_concept_id)
        else:
            set_task_epic(
                task_concept_id,
                str(epic_raw),
                actor_concept_id=actor_concept_id,
            )
        changed_fields.append("epic_task_concept_id")

    unknown_keys = sorted(
        key
        for key in fields.keys()
        if key
        not in {
            "status",
            "assignee_concept_id",
            "assignee_id",
            "created_by_concept_id",
            "creator_concept_id",
            "title",
            "description",
            "priority",
            "start_date",
            "due_date",
            "labels",
            "components",
            "fix_versions",
            "sprint_values",
            "backlog_rank",
            "organisation_concept_id",
            "reporter_concept_id",
            "watcher_concept_ids",
            "watchers_concept_ids",
            "parent_task_concept_id",
            "epic_task_concept_id",
        }
    )
    if unknown_keys:
        warnings.append(f"Ignored unsupported fields: {unknown_keys}")

    if changed_fields:
        ConceptsRepository.update_one(
            {"concept_id": task_concept_id},
            {"$set": {"updated_at": _now()}},
        )
        try:
            _append_task_history_event(
                task_concept_id=task_concept_id,
                event_type="task_fields_updated",
                actor_concept_id=actor_concept_id,
                details={"changed_fields": sorted(set(changed_fields))},
                touch_updated_at=False,
            )
        except Exception as e:
            logger.debug("Failed to append task field-update history event: %s", e)

    return {
        "task": get_task(task_concept_id),
        "changed_fields": sorted(set(changed_fields)),
        "warnings": warnings,
    }


def _normalise_external_reference_source_system(source_system: Any) -> str:
    if not isinstance(source_system, str) or not source_system.strip():
        raise InvalidTaskDataError("source_system is required")
    cleaned = source_system.strip().lower().replace("-", "_").replace(" ", "_")
    if not cleaned:
        raise InvalidTaskDataError("source_system is required")
    if any(not (ch.isalnum() or ch == "_") for ch in cleaned):
        raise InvalidTaskDataError(
            "source_system may only include letters, digits, and underscores"
        )
    return cleaned


def _normalise_external_reference_id(external_id: Any) -> str:
    if not isinstance(external_id, str) or not external_id.strip():
        raise InvalidTaskDataError("external_id is required")
    return external_id.strip()


def find_task_by_external_reference(
    *,
    source_system: str,
    external_id: str,
    organisation_concept_id: str | None = None,
) -> Dict[str, Any] | None:
    source_key = _normalise_external_reference_source_system(source_system)
    external_value = _normalise_external_reference_id(external_id)

    query_base: Dict[str, Any] = {
        "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
        f"metadata.{TASK_METADATA_KEY_EXTERNAL_REFERENCES}.{source_key}.external_id": external_value,
    }
    doc: Dict[str, Any] | None = None
    if organisation_concept_id:
        org_id = _normalise_optional_concept_id(organisation_concept_id)
        if not org_id:
            raise InvalidTaskDataError(
                f"Invalid organisation_concept_id: {organisation_concept_id}"
            )
        scoped_query = dict(query_base)
        scoped_query[f"metadata.{TASK_METADATA_KEY_ORGANISATION}"] = org_id
        doc = ConceptsRepository.find_one(scoped_query)
        if not isinstance(doc, dict) or not _is_task_doc(doc):
            # Legacy imports were often unscoped; allow a constrained fallback
            # to null/absent organisation scope for idempotent repair runs.
            legacy_query = dict(query_base)
            legacy_query[f"metadata.{TASK_METADATA_KEY_ORGANISATION}"] = None
            doc = ConceptsRepository.find_one(legacy_query)
    else:
        doc = ConceptsRepository.find_one(query_base)

    if not isinstance(doc, dict) or not _is_task_doc(doc):
        return None
    return _build_task_response(doc)


def upsert_task_external_reference(
    task_concept_id: str,
    *,
    source_system: str,
    external_id: str,
    reference_payload: Mapping[str, Any] | None = None,
    actor_concept_id: str | None = None,
) -> Dict[str, Any]:
    task_concept_id, _ = _get_task_doc(task_concept_id)
    source_key = _normalise_external_reference_source_system(source_system)
    external_value = _normalise_external_reference_id(external_id)

    payload: Dict[str, Any] = (
        dict(reference_payload) if isinstance(reference_payload, Mapping) else {}
    )
    payload["external_id"] = external_value
    payload["source_system"] = source_key
    payload["updated_at"] = _now().isoformat()

    ConceptsRepository.update_one(
        {"concept_id": task_concept_id},
        {
            "$set": {
                f"metadata.{TASK_METADATA_KEY_EXTERNAL_REFERENCES}.{source_key}": payload,
                "updated_at": _now(),
            }
        },
    )
    try:
        _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_external_reference_upserted",
            actor_concept_id=actor_concept_id,
            details={
                "source_system": source_key,
                "external_id": external_value,
            },
            touch_updated_at=False,
        )
    except Exception as e:
        logger.debug("Failed to append external-reference history event: %s", e)

    return get_task(task_concept_id)


def bulk_update_tasks(
    task_concept_ids: Iterable[str],
    *,
    fields: Dict[str, Any],
    actor_concept_id: str | None = None,
) -> Dict[str, Any]:
    if not isinstance(fields, dict) or not fields:
        raise InvalidTaskDataError("fields must be a non-empty dict")

    task_ids = [
        _normalise_task_concept_id(task_id)
        for task_id in task_concept_ids
        if isinstance(task_id, str) and task_id.strip()
    ]
    if not task_ids:
        raise InvalidTaskDataError("task_concept_ids must include at least one task")

    results: list[Dict[str, Any]] = []
    success_count = 0
    failure_count = 0
    for task_id in task_ids:
        try:
            update_result = update_task_fields(
                task_id,
                fields=fields,
                actor_concept_id=actor_concept_id,
            )
            success_count += 1
            results.append(
                {
                    "task_concept_id": task_id,
                    "success": True,
                    "result": update_result,
                }
            )
        except Exception as exc:
            failure_count += 1
            results.append(
                {
                    "task_concept_id": task_id,
                    "success": False,
                    "error": str(exc),
                }
            )

    return {
        "success_count": success_count,
        "failure_count": failure_count,
        "total": len(task_ids),
        "results": results,
    }


def delete_task(task_concept_id: str) -> bool:
    """Delete (archive) a task.

    Args:
        task_concept_id: The task's concept_id

    Returns:
        True if deleted

    Raises:
        TaskNotFoundError: If task not found
    """
    task_concept_id, _ = _get_task_doc(task_concept_id)

    # Soft delete by setting status to cancelled
    # (In future, could move to an archive collection)
    update_task_status(task_concept_id, TASK_STATUS_CANCELLED)
    try:
        _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_deleted",
            details={"deletion_mode": "soft_cancelled"},
        )
    except Exception as e:
        logger.debug("Failed to append task deletion history event: %s", e)

    logger.info(f"Deleted (cancelled) task: {task_concept_id}")
    return True


__all__ = [
    "TASK_SPECIFICATION_TYPE_ID",
    "TASK_EXECUTION_TYPE_ID",
    "CONVERSATION_TYPE_ID",
    "TASK_STATUS_PENDING",
    "TASK_STATUS_IN_PROGRESS",
    "TASK_STATUS_COMPLETED",
    "TASK_STATUS_CANCELLED",
    "TASK_STATUS_BLOCKED",
    "VALID_TASK_STATUSES",
    "PRIORITY_LOW",
    "PRIORITY_MEDIUM",
    "PRIORITY_HIGH",
    "PRIORITY_CRITICAL",
    "VALID_PRIORITIES",
    "TaskManagementError",
    "TaskNotFoundError",
    "InvalidTaskDataError",
    "create_task",
    "get_task",
    "update_task_status",
    "assign_task",
    "unassign_task",
    "get_tasks_for_user",
    "get_tasks_for_conversation",
    "list_tasks",
    "search_tasks",
    "get_task_transitions",
    "transition_task",
    "set_task_parent",
    "set_task_epic",
    "create_subtask",
    "link_tasks",
    "unlink_tasks",
    "add_task_comment",
    "list_task_comments",
    "add_task_attachment",
    "list_task_attachments",
    "add_task_worklog",
    "list_task_worklog",
    "get_task_history",
    "update_task_fields",
    "find_task_by_external_reference",
    "upsert_task_external_reference",
    "bulk_update_tasks",
    "delete_task",
]
