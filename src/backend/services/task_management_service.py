"""Task management service for JVNAUTOSCI-1040.

Provides CRUD operations for task concepts, linking tasks to conversations,
assignees, and tracking status via Vontology relationships and text relations.

All tasks are stored as first-class Vontology concepts (type: #V#task_specification).
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import can_access_concept
from ..security.visibility_predicates import (
    CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
    SPECIFIC_TO_ORG_PREDICATES_READ,
    SPECIFIC_TO_USER_PREDICATES_WRITE,
    get_specific_to_org_values,
    set_specific_to_org_values,
    set_specific_to_user_values,
)
from ..services.text_value_service import (
    get_texts_for_concept,
    get_texts_for_concepts,
    upsert_singleton_text_relation,
    upsert_text_for_concept,
)
from ..utils.concept_id_utils import (
    ensure_v_concept_prefix,
)
from .effort_unit_ontology_service import (
    ensure_effort_unit_ontology,
    extract_effort_unit_type_ids,
    persist_successor_effort_unit_type_links,
    resolve_successor_effort_unit_type_ids,
)
from .organisation_membership_service import is_user_member_of_organisation
from .task_work_product_service import resolve_task_work_product
from .task_ontology_service import (
    PREDICATE_HAS_CURRENT_WORK_PRODUCT,
    DEFAULT_TASK_SOURCE_ID,
    EVIDENCE_TEXT_PREDICATES,
    JIRA_IMPORTED_TASK_SOURCE_ID,
    NEXT_CHECKPOINT_TEXT_PREDICATES,
    PREDICATE_HAS_EVIDENCE,
    PREDICATE_HAS_NEXT_CHECKPOINT,
    PREDICATE_HAS_PROGRESS_SIGNAL,
    PREDICATE_HAS_TASK_REFERENCE_CODE,
    PREDICATE_HAS_TASK_ROLE,
    PREDICATE_HAS_TASK_SOURCE,
    PREDICATE_REPORTS_TO,
    PROGRESS_SIGNAL_TEXT_PREDICATES,
    REPORTS_TO_RELATIONSHIP_PREDICATES,
    TASK_REFERENCE_CODE_TEXT_PREDICATES,
    TASK_ROLE_TEXT_PREDICATES,
    TASK_SOURCE_DEFINITIONS,
    TASK_SOURCE_RELATIONSHIP_PREDICATES,
    TASK_TYPE_DEFINITIONS,
    ensure_task_ontology,  # noqa: F401  # retained as the task-ontology test seam
    get_task_source_definition,
    get_task_type_definition,
    is_known_task_type_id,
    normalise_task_source_id,
    normalise_task_type_ids,
)
from .task_ontology_service import (
    get_task_taxonomy as get_task_taxonomy_definition,
)
from .workflow_event_integration_service import (
    maybe_launch_effort_unit_completed_workflow,
    maybe_launch_task_created_workflow,
    maybe_launch_task_status_workflow,
)

logger = logging.getLogger(__name__)

TASK_LIST_LOAD_TELEMETRY_SCHEMA_VERSION = "task_list_load_telemetry.v1"
TASK_SEARCH_ACCESSIBLE_CANDIDATE_LIMIT = 400
TASK_LIST_MAX_PAGE_SIZE = 200
TASK_ORGANISATION_SCOPE_CURRENT_PLUS_UNSCOPED = "current_plus_unscoped"
TASK_ORGANISATION_SCOPE_UNSCOPED_ONLY = "unscoped_only"


def _round_duration_ms(duration_seconds: float) -> float:
    return round(max(0.0, float(duration_seconds)) * 1000.0, 2)


def _new_task_list_load_timer(source: str):
    started = time.perf_counter()
    last = started
    stages: list[dict[str, Any]] = []

    def mark(stage: str, **metadata: Any) -> None:
        nonlocal last
        now = time.perf_counter()
        stage_payload: dict[str, Any] = {
            "stage": stage,
            "duration_ms": _round_duration_ms(now - last),
            "since_start_ms": _round_duration_ms(now - started),
        }
        stage_payload.update(
            {key: value for key, value in metadata.items() if value is not None}
        )
        stages.append(stage_payload)
        last = now

    def finish(**metadata: Any) -> dict[str, Any]:
        now = time.perf_counter()
        payload: dict[str, Any] = {
            "schema_version": TASK_LIST_LOAD_TELEMETRY_SCHEMA_VERSION,
            "source": source,
            "total_ms": _round_duration_ms(now - started),
            "stages": stages,
        }
        payload.update(
            {key: value for key, value in metadata.items() if value is not None}
        )
        return payload

    return mark, finish


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

_TASK_RELATIONSHIP_PREDICATE_ALIAS_MAP: Dict[str, tuple[str, ...]] = {
    PREDICATE_HAS_TASK_SOURCE: TASK_SOURCE_RELATIONSHIP_PREDICATES,
    PREDICATE_REPORTS_TO: REPORTS_TO_RELATIONSHIP_PREDICATES,
}

_TASK_TEXT_PREDICATE_ALIAS_MAP: Dict[str, tuple[str, ...]] = {
    PREDICATE_HAS_TASK_ROLE: TASK_ROLE_TEXT_PREDICATES,
    PREDICATE_HAS_NEXT_CHECKPOINT: NEXT_CHECKPOINT_TEXT_PREDICATES,
    PREDICATE_HAS_PROGRESS_SIGNAL: PROGRESS_SIGNAL_TEXT_PREDICATES,
    PREDICATE_HAS_EVIDENCE: EVIDENCE_TEXT_PREDICATES,
    PREDICATE_HAS_TASK_REFERENCE_CODE: TASK_REFERENCE_CODE_TEXT_PREDICATES,
}

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
TASK_METADATA_KEY_BULK_TASK_COLLECTIONS = "bulk_task_collections"
TASK_METADATA_KEY_CREATION_FINGERPRINT = "agent_creation_fingerprint"
TASK_METADATA_KEY_CREATION_REQUEST_ID = "agent_creation_request_id"

# Bulk task collection metadata is an explicit visibility marker. Jira-origin
# tasks are not hidden merely because they came from Jira.
BULK_TASK_VISIBILITY_EXCLUDE = "exclude"
BULK_TASK_VISIBILITY_INCLUDE = "include"
BULK_TASK_VISIBILITY_ONLY = "only"
VALID_BULK_TASK_VISIBILITY_VALUES = {
    BULK_TASK_VISIBILITY_EXCLUDE,
    BULK_TASK_VISIBILITY_INCLUDE,
    BULK_TASK_VISIBILITY_ONLY,
}
JIRA_MIGRATION_BULK_COLLECTION_ID = "#V#jira_task_migration_bulk_collection"
JIRA_MIGRATION_BULK_COLLECTION_KIND = "jira_migration"
JIRA_MIGRATION_BULK_COLLECTION_LABEL = "Jira migration backlog"
JIRA_MIGRATION_BULK_COLLECTION_REASON = "jira_migration_bulk_collection"
JIRA_MIGRATION_LABEL_CANDIDATES = ("migrated", "jira-migration", "jira_migration")

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


class TaskOrganisationScopeAccessError(TaskManagementError):
    """The actor may not move a task into or out of an organisation scope."""

    def __init__(self, reason_code: str, safe_message: str):
        super().__init__(safe_message)
        self.reason_code = reason_code
        self.safe_message = safe_message


def _task_concept_id_for_agent_creation_fingerprint(
    creation_fingerprint: str,
) -> str:
    """Return the stable concept ID used for an idempotent agent task create."""

    digest = hashlib.sha256(creation_fingerprint.encode("utf-8")).hexdigest()
    return f"#V#task_agent_{digest[:32]}"


def _generate_task_concept_id(
    title: str,
    *,
    agent_creation_fingerprint: str | None = None,
) -> str:
    """Generate a unique concept_id for a task.

    Agent-created tasks use their server-issued request fingerprint so retries
    can reconcile through the indexed ``concept_id`` path. Other callers retain
    the human-readable title slug plus a short UUID suffix.
    """
    if agent_creation_fingerprint:
        return _task_concept_id_for_agent_creation_fingerprint(
            agent_creation_fingerprint
        )

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


def _relationship_id_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidate = _normalise_optional_concept_id(value)
        return [candidate] if candidate else []
    if not isinstance(value, list):
        return []

    seen: set[str] = set()
    ordered: list[str] = []
    for item in value:
        candidate = _normalise_optional_concept_id(item)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return ordered


def _normalise_optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidTaskDataError(f"{field_name} must be a string")
    cleaned = value.strip()
    return cleaned or None


def _upsert_optional_task_text(
    *,
    task_concept_id: str,
    predicate: str,
    value: str | None,
    lang: str = "en-NZ",
) -> None:
    predicate_aliases = _text_predicate_aliases(predicate)
    if value is None:
        _clear_task_text_relations(
            task_concept_id=task_concept_id,
            predicates=predicate_aliases,
            log_field_name=predicate,
        )
        return

    legacy_predicates = tuple(
        alias for alias in predicate_aliases if alias != predicate
    )
    if legacy_predicates:
        _clear_task_text_relations(
            task_concept_id=task_concept_id,
            predicates=legacy_predicates,
            log_field_name=predicate,
        )

    # These are current task fields, not an additive collection of notes.
    # Otherwise repeated revisions eventually push the current checkpoint
    # beyond the bounded task reader. Keep replaced values recoverable.
    upsert_singleton_text_relation(
        subject_concept_id=task_concept_id,
        predicate=predicate,
        text=value,
        lang=lang,
        garbage_collect=False,
    )


def _persist_required_task_texts(
    *,
    task_concept_id: str,
    title: str,
    description: str,
    priority: str,
) -> list[dict[str, str]]:
    """Persist mandatory task text fields concurrently and report failures.

    These independent relations formerly ran serially after the concept insert.
    On a remote Mongo deployment that can consume the ordinary-turn effect
    budget before a caller can perform its canonical read-back. Their writes
    are independent and idempotent, so concurrent persistence preserves their
    semantics while bounding their wall-clock contribution.
    """

    required_texts = (
        (PREDICATE_HAS_NAME, title, "en-NZ"),
        (PREDICATE_HAS_DESCRIPTION, description, "en-NZ"),
        (PREDICATE_HAS_TASK_STATUS, TASK_STATUS_PENDING, "en"),
        (PREDICATE_HAS_PRIORITY, priority, "en"),
    )
    failures: list[dict[str, str]] = []

    def persist(predicate: str, text: str, lang: str) -> None:
        upsert_text_for_concept(
            subject_concept_id=task_concept_id,
            predicate=predicate,
            text=text,
            lang=lang,
        )

    # The copied worker contexts share the request AccessEvaluator. Resolve
    # this one task decision before submitting so every worker only reads its
    # cached decision instead of mutating that shared evaluator concurrently.
    # This happens after the concept insert, so a failure must be returned as
    # receipt evidence rather than escape into fingerprint reconciliation.
    try:
        can_access_concept(task_concept_id)
    except Exception as exc:
        logger.warning("Failed to verify required task text access: %s", exc)
        return [
            {
                "predicate": "required_task_texts",
                "exception_type": type(exc).__name__,
            }
        ]

    try:
        with ThreadPoolExecutor(max_workers=len(required_texts)) as executor:
            pending = {
                executor.submit(
                    contextvars.copy_context().run,
                    persist,
                    predicate,
                    text,
                    lang,
                ): predicate
                for predicate, text, lang in required_texts
            }
            for future in as_completed(pending):
                predicate = pending[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.warning(
                        "Failed to store required task text field %s: %s",
                        predicate,
                        exc,
                    )
                    failures.append(
                        {
                            "predicate": predicate,
                            "exception_type": type(exc).__name__,
                        }
                    )
    except Exception as exc:
        logger.warning("Failed to run required task text workers: %s", exc)
        failures.append(
            {
                "predicate": "required_task_texts",
                "exception_type": type(exc).__name__,
            }
        )
    return failures


def _replace_single_relationship_target(
    *,
    task_concept_id: str,
    predicate: str,
    new_target_id: str | None,
) -> None:
    _, task_doc = _get_task_doc(task_concept_id)
    relationships = task_doc.get("relationships") or {}
    predicate_aliases = _relationship_predicate_aliases(predicate)
    existing_targets_by_predicate = {
        alias: _relationship_id_list(relationships.get(alias))
        for alias in predicate_aliases
    }
    canonical_existing_targets = existing_targets_by_predicate.get(predicate, [])

    for alias, existing_targets in existing_targets_by_predicate.items():
        for existing_target in existing_targets:
            if alias == predicate and existing_target == new_target_id:
                continue
            ConceptsRepository.mutate_relationship_edge(
                source_id=task_concept_id,
                kind=alias,
                target_id=existing_target,
                action="remove",
                maintain_inverse=False,
            )

    if new_target_id and new_target_id not in canonical_existing_targets:
        ConceptsRepository.mutate_relationship_edge(
            source_id=task_concept_id,
            kind=predicate,
            target_id=new_target_id,
            action="add",
            maintain_inverse=False,
        )


def _infer_task_source_id(
    *,
    explicit_source_id: str | None,
    external_references: Mapping[str, Any] | None = None,
) -> str:
    if explicit_source_id:
        return explicit_source_id
    jira_reference = (
        external_references.get("jira")
        if isinstance(external_references, Mapping)
        else None
    )
    if isinstance(jira_reference, Mapping) and jira_reference.get("external_id"):
        return JIRA_IMPORTED_TASK_SOURCE_ID
    return DEFAULT_TASK_SOURCE_ID


def _task_type_ids_from_doc(doc: Dict[str, Any]) -> list[str]:
    relationships = doc.get("relationships") or {}
    direct_types = _relationship_id_list(relationships.get("is_an_instance_of"))
    return [type_id for type_id in direct_types if is_known_task_type_id(type_id)]


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


def _clean_metadata_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _derive_concept_label(concept_id: str) -> str:
    cleaned = str(concept_id or "").strip()
    if cleaned.startswith("#V#"):
        cleaned = cleaned[3:]
    label = " ".join(part for part in cleaned.replace("-", "_").split("_") if part)
    return label.title() if label else str(concept_id or "")


def build_jira_migration_bulk_task_collection(
    *,
    label: str | None = None,
    hidden_by_default: bool = True,
) -> Dict[str, Any]:
    """Return the canonical Jira-migration bulk task collection descriptor."""

    return {
        "collection_id": JIRA_MIGRATION_BULK_COLLECTION_ID,
        "label": label or JIRA_MIGRATION_BULK_COLLECTION_LABEL,
        "kind": JIRA_MIGRATION_BULK_COLLECTION_KIND,
        "hidden_by_default": bool(hidden_by_default),
        "reason": JIRA_MIGRATION_BULK_COLLECTION_REASON,
        "task_source_id": JIRA_IMPORTED_TASK_SOURCE_ID,
    }


def _normalise_bulk_task_collection_entry(value: Any) -> Dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None

    collection_id = _clean_metadata_string(
        value.get("collection_id") or value.get("id")
    )
    if not collection_id:
        return None

    label = _clean_metadata_string(value.get("label")) or _derive_concept_label(
        collection_id
    )
    kind = _clean_metadata_string(value.get("kind")) or "bulk_task_collection"
    reason = _clean_metadata_string(value.get("reason"))
    task_source_id = _clean_metadata_string(
        value.get("task_source_id") or value.get("source_id")
    )

    normalised: Dict[str, Any] = {
        "collection_id": collection_id,
        "label": label,
        "kind": kind,
        "hidden_by_default": bool(value.get("hidden_by_default")),
    }
    if reason:
        normalised["reason"] = reason
    if task_source_id:
        normalised["task_source_id"] = task_source_id
    return normalised


def normalise_bulk_task_collections(value: Any) -> list[Dict[str, Any]]:
    """Normalise persisted bulk-task collection metadata."""

    if isinstance(value, Mapping):
        raw_entries: list[Any] = [value]
    elif isinstance(value, list):
        raw_entries = list(value)
    elif value is None:
        raw_entries = []
    else:
        raw_entries = []

    seen: set[str] = set()
    collections: list[Dict[str, Any]] = []
    for raw_entry in raw_entries:
        entry = _normalise_bulk_task_collection_entry(raw_entry)
        if not entry:
            continue
        collection_id = str(entry["collection_id"])
        if collection_id in seen:
            continue
        seen.add(collection_id)
        collections.append(entry)
    return collections


def _normalise_bulk_task_collection_ids(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        raw_values: Iterable[Any] = [values]
    elif isinstance(values, Iterable):
        raw_values = values
    else:
        raw_values = [values]

    collection_ids: set[str] = set()
    for raw_value in raw_values:
        cleaned = _clean_metadata_string(raw_value)
        if cleaned:
            collection_ids.add(cleaned)
    return collection_ids


def _normalise_bulk_task_visibility(value: Any) -> str:
    cleaned = _clean_metadata_string(value) or BULK_TASK_VISIBILITY_INCLUDE
    cleaned = cleaned.lower()
    if cleaned not in VALID_BULK_TASK_VISIBILITY_VALUES:
        raise InvalidTaskDataError(
            "bulk_visibility must be one of: "
            f"{sorted(VALID_BULK_TASK_VISIBILITY_VALUES)}"
        )
    return cleaned


def _hidden_bulk_collections_for_task(
    task: Mapping[str, Any],
    *,
    collection_ids: set[str] | None = None,
) -> list[Dict[str, Any]]:
    collections = normalise_bulk_task_collections(
        task.get("bulk_task_collections")
        or task.get("hidden_by_default_bulk_task_collections")
    )
    filtered: list[Dict[str, Any]] = []
    for collection in collections:
        if not bool(collection.get("hidden_by_default")):
            continue
        collection_id = str(collection.get("collection_id") or "")
        if collection_ids and collection_id not in collection_ids:
            continue
        filtered.append(collection)
    return filtered


def _build_bulk_task_collection_summaries(
    tasks: Iterable[Mapping[str, Any]],
    *,
    collection_ids: set[str] | None = None,
) -> list[Dict[str, Any]]:
    summaries: dict[str, Dict[str, Any]] = {}
    for task in tasks:
        task_id = _clean_metadata_string(task.get("task_concept_id"))
        for collection in _hidden_bulk_collections_for_task(
            task,
            collection_ids=collection_ids,
        ):
            collection_id = str(collection.get("collection_id") or "")
            if not collection_id:
                continue
            summary = summaries.setdefault(
                collection_id,
                {
                    "collection_id": collection_id,
                    "label": collection.get("label")
                    or _derive_concept_label(collection_id),
                    "kind": collection.get("kind") or "bulk_task_collection",
                    "hidden_by_default": True,
                    "reason": collection.get("reason"),
                    "task_source_id": collection.get("task_source_id"),
                    "count": 0,
                    "sample_task_concept_ids": [],
                },
            )
            summary["count"] = int(summary.get("count") or 0) + 1
            samples = summary.get("sample_task_concept_ids")
            if isinstance(samples, list) and task_id and len(samples) < 10:
                samples.append(task_id)

    return sorted(
        summaries.values(),
        key=lambda item: (
            str(item.get("label") or "").casefold(),
            str(item.get("collection_id") or ""),
        ),
    )


def apply_bulk_task_visibility(
    tasks: Iterable[Mapping[str, Any]],
    *,
    bulk_visibility: str | None = BULK_TASK_VISIBILITY_INCLUDE,
    bulk_collection_ids: Iterable[str] | str | None = None,
) -> Dict[str, Any]:
    """Apply hidden-by-default bulk collection visibility to an already-filtered task list."""

    visibility = _normalise_bulk_task_visibility(bulk_visibility)
    collection_ids = _normalise_bulk_task_collection_ids(bulk_collection_ids)
    task_list = [dict(task) for task in tasks if isinstance(task, Mapping)]

    hidden_task_ids: set[str] = set()
    hidden_tasks: list[Dict[str, Any]] = []
    visible_tasks: list[Dict[str, Any]] = []
    for task in task_list:
        task_id = str(task.get("task_concept_id") or "")
        hidden_collections = _hidden_bulk_collections_for_task(
            task,
            collection_ids=collection_ids,
        )
        is_hidden_bulk_task = bool(hidden_collections)
        if is_hidden_bulk_task and task_id not in hidden_task_ids:
            hidden_task_ids.add(task_id)
            hidden_tasks.append(task)

        if visibility == BULK_TASK_VISIBILITY_EXCLUDE and is_hidden_bulk_task:
            continue
        if visibility == BULK_TASK_VISIBILITY_ONLY and not is_hidden_bulk_task:
            continue
        visible_tasks.append(task)

    summaries = _build_bulk_task_collection_summaries(
        task_list,
        collection_ids=collection_ids,
    )
    return {
        "tasks": visible_tasks,
        "bulk_visibility": visibility,
        "bulk_collection_ids": sorted(collection_ids),
        "hidden_bulk_task_total": len(hidden_tasks),
        "hidden_bulk_task_collections": summaries,
    }


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
    event_timestamp: str | None = None,
) -> Dict[str, Any]:
    timestamp = _parse_datetime(event_timestamp)
    event = {
        "event_id": f"event_{uuid.uuid4().hex[:12]}",
        "event_type": event_type,
        "timestamp": (timestamp or _now()).isoformat(),
        "actor_concept_id": _normalise_optional_concept_id(actor_concept_id),
        "details": details or {},
    }
    update_doc: Dict[str, Any] = {
        "$push": {f"metadata.{TASK_METADATA_KEY_HISTORY}": event}
    }
    if touch_updated_at:
        update_doc.setdefault("$set", {})
        update_doc["$set"]["updated_at"] = _now()
    query: Dict[str, Any] = {"concept_id": task_concept_id}
    signature = (details or {}).get("import_signature")
    if signature:
        query[f"metadata.{TASK_METADATA_KEY_HISTORY}"] = {
            "$not": {
                "$elemMatch": {
                    "event_type": event_type,
                    "details.import_signature": signature,
                }
            }
        }
    ConceptsRepository.update_one(query, update_doc)
    return event


def _append_task_metadata_entry(
    *,
    task_concept_id: str,
    metadata_key: str,
    entry: Dict[str, Any],
) -> Dict[str, Any]:
    query: Dict[str, Any] = {"concept_id": task_concept_id}
    source = entry.get("source") or {}
    source_match = None
    if source.get("source_system") and source.get("external_id"):
        source_match = {
            "source.source_system": source["source_system"],
            "source.external_id": source["external_id"],
        }
        query[f"metadata.{metadata_key}"] = {"$not": {"$elemMatch": source_match}}
    result = ConceptsRepository.update_one(
        query,
        {
            "$push": {f"metadata.{metadata_key}": entry},
            "$set": {"updated_at": _now()},
        },
    )
    if source_match and getattr(result, "matched_count", 1) == 0:
        _, doc = _get_task_doc(task_concept_id)
        for existing in _list_metadata_items(doc, metadata_key):
            previous_source = existing.get("source") or {}
            if all(
                previous_source.get(key.split(".")[-1]) == value
                for key, value in source_match.items()
            ):
                return existing
        raise TaskManagementError(
            "Imported activity was not stored or found on read-back"
        )
    return entry


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


def _first_relationship_value_from_aliases(
    relationships: Mapping[str, Any],
    predicates: Iterable[str],
) -> str | None:
    for predicate in predicates:
        value = _first_relationship_value(relationships.get(predicate))
        if value:
            return value
    return None


def _relationship_predicate_aliases(predicate: str) -> tuple[str, ...]:
    return _TASK_RELATIONSHIP_PREDICATE_ALIAS_MAP.get(predicate, (predicate,))


def _text_predicate_aliases(predicate: str) -> tuple[str, ...]:
    return _TASK_TEXT_PREDICATE_ALIAS_MAP.get(predicate, (predicate,))


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
    _clear_task_text_relations(
        task_concept_id=task_concept_id,
        predicates=(predicate,),
        log_field_name=log_field_name,
    )


def _clear_task_text_relations(
    *,
    task_concept_id: str,
    predicates: Iterable[str],
    log_field_name: str,
) -> None:
    from ..services.text_value_service import delete_text_relation

    seen_relation_ids: set[str] = set()
    for predicate in predicates:
        for existing in get_texts_for_concept(
            task_concept_id,
            predicate=predicate,
            limit=100,
        ):
            relation_id = existing.get("relation_id")
            if not isinstance(relation_id, str) or not relation_id:
                continue
            if relation_id in seen_relation_ids:
                continue
            seen_relation_ids.add(relation_id)
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
    task_type_ids: list[str] | str | None = None,
    task_source_id: str | None = None,
    report_to_concept_id: str | None = None,
    task_role: str | None = None,
    next_checkpoint: str | None = None,
    progress_signal: str | None = None,
    evidence: str | None = None,
    notes: str | None = None,
    reference_code: str | None = None,
    agent_creation_fingerprint: str | None = None,
    agent_creation_request_id: str | None = None,
    project_concept_id: str | None = None,
    collection_concept_ids: list[str] | None = None,
    visibility_owner_concept_id: str | None = None,
    initial_external_references: Mapping[str, Any] | None = None,
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
        task_type_ids: Canonical task category concept IDs (or slugs)
        task_source_id: Canonical task source concept ID (or slug)
        report_to_concept_id: Optional concept ID the task reports to
        task_role: Optional task role text
        next_checkpoint: Optional next checkpoint text
        progress_signal: Optional progress-signal text
        evidence: Optional evidence text
        notes: Optional notes text
        reference_code: Optional compact task number / reference code
        agent_creation_fingerprint: Optional server-issued retry fingerprint
        agent_creation_request_id: Optional server-issued request identity

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
    try:
        canonical_task_type_ids = normalise_task_type_ids(
            task_type_ids,
            allow_default=True,
        )
    except ValueError as exc:
        raise InvalidTaskDataError(str(exc)) from exc
    try:
        canonical_task_source_id = normalise_task_source_id(
            task_source_id,
            allow_default=True,
        )
    except ValueError as exc:
        raise InvalidTaskDataError(str(exc)) from exc

    report_to_concept_id = _normalise_optional_concept_id(report_to_concept_id)
    task_role = _normalise_optional_text(task_role, field_name="task_role")
    next_checkpoint = _normalise_optional_text(
        next_checkpoint,
        field_name="next_checkpoint",
    )
    progress_signal = _normalise_optional_text(
        progress_signal,
        field_name="progress_signal",
    )
    evidence = _normalise_optional_text(evidence, field_name="evidence")
    notes = _normalise_optional_text(notes, field_name="notes")
    reference_code = _normalise_optional_text(
        reference_code,
        field_name="reference_code",
    )
    agent_creation_fingerprint = _normalise_optional_text(
        agent_creation_fingerprint,
        field_name="agent_creation_fingerprint",
    )
    agent_creation_request_id = _normalise_optional_text(
        agent_creation_request_id,
        field_name="agent_creation_request_id",
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
    task_concept_id = _generate_task_concept_id(
        title,
        agent_creation_fingerprint=agent_creation_fingerprint,
    )

    # Task creation is a latency-bounded user effect, not an ontology migration
    # surface. The canonical relationship IDs below remain valid write inputs
    # without synchronously re-checking and repairing the complete task and
    # effort-unit ontologies. Those explicit maintenance operations can take
    # many remote-database round trips and previously consumed the whole MCP
    # deadline before the task itself was written.

    # Normalise concept IDs
    if assignee_concept_id:
        assignee_concept_id = ensure_v_concept_prefix(assignee_concept_id)
    if created_by_concept_id:
        created_by_concept_id = ensure_v_concept_prefix(created_by_concept_id)
    if organisation_concept_id:
        organisation_concept_id = ensure_v_concept_prefix(organisation_concept_id)
    if report_to_concept_id:
        report_to_concept_id = ensure_v_concept_prefix(report_to_concept_id)
    epic_task_concept_id = _normalise_optional_concept_id(epic_task_concept_id)
    if epic_task_concept_id and epic_task_concept_id == task_concept_id:
        raise InvalidTaskDataError("A task cannot reference itself as epic")
    if epic_task_concept_id:
        _get_task_doc(epic_task_concept_id)

    now = _now()

    if project_concept_id or collection_concept_ids:
        from .task_project_service import validate_task_membership

        project, collection_concept_ids = validate_task_membership(
            project_concept_id, collection_concept_ids
        )
        if project and organisation_concept_id is None:
            organisation_concept_id = project.get("organisation_concept_id")

    # Build relationships
    relationships: Dict[str, Any] = {
        "is_an_instance_of": [TASK_SPECIFICATION_TYPE_ID, *canonical_task_type_ids],
    }

    if assignee_concept_id:
        relationships[PREDICATE_HAS_ASSIGNEE] = [assignee_concept_id]
    if created_by_concept_id:
        relationships[PREDICATE_HAS_CREATED_BY] = [created_by_concept_id]
    if canonical_task_source_id:
        relationships[PREDICATE_HAS_TASK_SOURCE] = [canonical_task_source_id]
    if report_to_concept_id:
        relationships[PREDICATE_REPORTS_TO] = [report_to_concept_id]
    if epic_task_concept_id:
        relationships[PREDICATE_HAS_EPIC_TASK] = [epic_task_concept_id]

    # Visibility scoping - task visible to creator and assignee
    visible_to_users = []
    if created_by_concept_id:
        visible_to_users.append(created_by_concept_id)
    if assignee_concept_id and assignee_concept_id not in visible_to_users:
        visible_to_users.append(assignee_concept_id)
    if report_to_concept_id and report_to_concept_id not in visible_to_users:
        visible_to_users.append(report_to_concept_id)
    if visibility_owner_concept_id:
        # Import provenance and assignment do not confer access to a private
        # source corpus. Used by the trusted importer, not exposed as a UI field.
        visible_to_users = [visibility_owner_concept_id]
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
            TASK_METADATA_KEY_EXTERNAL_REFERENCES: dict(
                initial_external_references or {}
            ),
            TASK_METADATA_KEY_BULK_TASK_COLLECTIONS: [],
            TASK_METADATA_KEY_CREATION_FINGERPRINT: agent_creation_fingerprint,
            TASK_METADATA_KEY_CREATION_REQUEST_ID: agent_creation_request_id,
            "project_concept_id": project_concept_id,
            "collection_concept_ids": collection_concept_ids or [],
        },
    }

    try:
        ConceptsRepository.insert_one(concept_doc)
        logger.info(f"Created task concept: {task_concept_id}")
    except Exception as e:
        logger.error(f"Failed to create task concept: {e}")
        raise TaskManagementError(f"Failed to create task: {e}") from e

    required_text_persistence_failures = _persist_required_task_texts(
        task_concept_id=task_concept_id,
        title=title,
        description=description,
        priority=priority,
    )

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

    for predicate, value in (
        (PREDICATE_HAS_TASK_ROLE, task_role),
        (PREDICATE_HAS_NEXT_CHECKPOINT, next_checkpoint),
        (PREDICATE_HAS_PROGRESS_SIGNAL, progress_signal),
        (PREDICATE_HAS_EVIDENCE, evidence),
        (PREDICATE_HAS_TASK_REFERENCE_CODE, reference_code),
        ("hasNote", notes),
    ):
        if value is None:
            continue
        try:
            upsert_text_for_concept(
                subject_concept_id=task_concept_id,
                predicate=predicate,
                text=value,
                lang="en-NZ",
            )
        except Exception as exc:
            logger.warning("Failed to store task text field %s: %s", predicate, exc)

    task_type_payload = [
        definition
        for type_id in canonical_task_type_ids
        for definition in [get_task_type_definition(type_id)]
        if isinstance(definition, dict)
    ]
    source_definition = (
        get_task_source_definition(canonical_task_source_id)
        if canonical_task_source_id
        else None
    )

    result = {
        "task_concept_id": task_concept_id,
        "title": title,
        "project_concept_id": project_concept_id,
        "collection_concept_ids": collection_concept_ids or [],
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
        "task_type_ids": canonical_task_type_ids,
        "task_types": task_type_payload,
        "primary_task_type_id": (
            canonical_task_type_ids[0] if canonical_task_type_ids else None
        ),
        "primary_task_type_label": (
            task_type_payload[0]["label"] if task_type_payload else None
        ),
        "task_source_id": canonical_task_source_id,
        "task_source_label": (
            source_definition.get("label")
            if isinstance(source_definition, dict)
            else None
        ),
        "task_source_slug": (
            source_definition.get("slug")
            if isinstance(source_definition, dict)
            else None
        ),
        "report_to_concept_id": report_to_concept_id,
        "task_role": task_role,
        "next_checkpoint": next_checkpoint,
        "progress_signal": progress_signal,
        "evidence": evidence,
        "notes": notes,
        "reference_code": reference_code,
        "created_at": now.isoformat(),
        "required_text_persistence_failures": required_text_persistence_failures,
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
                "task_type_ids": canonical_task_type_ids,
                "task_source_id": canonical_task_source_id,
                "report_to_concept_id": report_to_concept_id,
                "task_role": task_role,
                "next_checkpoint": next_checkpoint,
                "progress_signal": progress_signal,
                "evidence": evidence,
                "reference_code": reference_code,
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
    return {**_build_task_response(doc), "current_work_product": resolve_task_work_product(doc)}


def find_task_by_agent_creation_fingerprint(
    *,
    created_by_concept_id: str,
    organisation_concept_id: str,
    creation_fingerprint: str,
) -> Dict[str, Any] | None:
    """Find a prior actor-scoped task create for retry reconciliation."""

    created_by = _normalise_optional_concept_id(created_by_concept_id)
    organisation = _normalise_optional_concept_id(organisation_concept_id)
    fingerprint = _normalise_optional_text(
        creation_fingerprint,
        field_name="creation_fingerprint",
    )
    if not created_by or not organisation or not fingerprint:
        return None
    task_concept_id = _task_concept_id_for_agent_creation_fingerprint(fingerprint)
    doc = ConceptsRepository.find_one({"concept_id": task_concept_id})
    if not isinstance(doc, dict) or not _is_task_doc(doc):
        return None
    metadata = doc.get("metadata")
    relationships = doc.get("relationships")
    if not isinstance(metadata, dict) or not isinstance(relationships, dict):
        return None
    if (
        metadata.get(TASK_METADATA_KEY_ORGANISATION) != organisation
        or metadata.get(TASK_METADATA_KEY_CREATION_FINGERPRINT) != fingerprint
        or created_by
        not in _relationship_id_list(relationships.get(PREDICATE_HAS_CREATED_BY))
    ):
        return None
    return _build_task_response(doc)


def _build_task_responses(
    docs: Iterable[Dict[str, Any]],
    *,
    texts_by_task: Mapping[str, List[Dict[str, Any]]] | None = None,
) -> List[Dict[str, Any]]:
    """Build task responses while resolving their text relations in one read.

    Task listings need the same text-backed fields as an individual task, but
    resolving each task separately turns a single listing into an Atlas round
    trip per task.  Keep the individual response builder for point reads and
    provide its already-resolved rows for collection reads instead.
    """
    task_docs = list(docs)
    if not task_docs:
        return []

    task_ids = [
        concept_id
        for doc in task_docs
        for concept_id in [doc.get("concept_id")]
        if isinstance(concept_id, str) and concept_id
    ]
    if texts_by_task is None:
        texts_by_task = {}
        should_fetch_texts = True
    else:
        should_fetch_texts = False
    if task_ids and should_fetch_texts:
        try:
            texts_by_task = get_texts_for_concepts(task_ids) or {}
        except Exception:
            # Match the existing best-effort text relation behaviour: a
            # relation-store failure must not hide otherwise readable tasks.
            texts_by_task = {}

    conversation_details = _get_task_conversation_details(task_docs)

    return [
        _build_task_response(
            doc,
            texts=texts_by_task.get(str(doc.get("concept_id") or ""), []),
            conversation_details=conversation_details,
        )
        for doc in task_docs
    ]


def _get_task_conversation_details(
    task_docs: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Resolve task conversation projections in bounded collection reads."""
    conversation_ids = {
        conversation_id
        for doc in task_docs
        for conversation_id in [
            _first_relationship_value(
                (doc.get("relationships") or {}).get(
                    PREDICATE_HAS_ORIGINATING_CONVERSATION
                )
            )
        ]
        if isinstance(conversation_id, str) and conversation_id
    }
    if not conversation_ids:
        return {}

    try:
        conversation_docs = list(
            ConceptsRepository.find(
                {"concept_id": {"$in": sorted(conversation_ids)}}
            )
        )
    except Exception:
        return {}

    valid_conversations = [
        doc
        for doc in conversation_docs
        if CONVERSATION_TYPE_ID
        in _relationship_id_list((doc.get("relationships") or {}).get("is_an_instance_of"))
    ]
    if not valid_conversations:
        return {}

    valid_ids = [
        concept_id
        for doc in valid_conversations
        for concept_id in [doc.get("concept_id")]
        if isinstance(concept_id, str) and concept_id
    ]
    try:
        texts_by_conversation = get_texts_for_concepts(valid_ids) or {}
    except Exception:
        texts_by_conversation = {}

    details: Dict[str, Dict[str, Any]] = {}
    sessions_missing_names: set[str] = set()
    for doc in valid_conversations:
        conversation_id = str(doc.get("concept_id") or "")
        metadata = doc.get("metadata") or {}
        session_id = metadata.get("session_id")
        name = None
        topic = None
        for text_item in texts_by_conversation.get(conversation_id, []):
            predicate = text_item.get("predicate", "")
            text_value = text_item.get("text", "")
            if predicate in ("#V#hasSessionId", "hasSessionId"):
                session_id = text_value
            elif predicate in (PREDICATE_HAS_NAME, "hasName"):
                name = text_value
            elif predicate in ("#V#hasTopic", "hasTopic"):
                topic = text_value
        details[conversation_id] = {
            "session_id": session_id,
            "name": name,
            "topic": topic,
        }
        if isinstance(session_id, str) and session_id and not (name or topic):
            sessions_missing_names.add(session_id)

    if sessions_missing_names:
        try:
            from .chat_history_service import get_chat_history_collection_service

            chat_history_coll = get_chat_history_collection_service()
            if chat_history_coll is not None:
                session_names = {
                    str(row.get("session_id")): row.get("session_name")
                    for row in chat_history_coll.find(
                        {"session_id": {"$in": sorted(sessions_missing_names)}},
                        {"session_id": 1, "session_name": 1},
                    )
                    if isinstance(row, dict)
                    and isinstance(row.get("session_id"), str)
                    and isinstance(row.get("session_name"), str)
                    and row.get("session_name")
                }
                for detail in details.values():
                    session_id = detail.get("session_id")
                    if not detail.get("name") and session_id in session_names:
                        detail["name"] = session_names[session_id]
        except Exception:
            pass

    return details


def _task_doc_organisation_concept_id(doc: Mapping[str, Any]) -> str | None:
    relationships_raw = doc.get("relationships")
    metadata_raw = doc.get("metadata")
    relationships = (
        relationships_raw if isinstance(relationships_raw, dict) else {}
    )
    metadata = metadata_raw if isinstance(metadata_raw, Mapping) else {}
    canonical_organisation_id = _normalise_optional_concept_id(
        _first_relationship_value(
            relationships.get(CANONICAL_SPECIFIC_TO_ORG_PREDICATE)
        )
    )
    represented_organisation_ids = get_specific_to_org_values(relationships)
    metadata_organisation_id = _normalise_optional_concept_id(
        metadata.get(TASK_METADATA_KEY_ORGANISATION)
    )
    return (
        canonical_organisation_id
        or (represented_organisation_ids[0] if represented_organisation_ids else None)
        or metadata_organisation_id
    )


def _build_task_response(
    doc: Dict[str, Any],
    *,
    texts: List[Dict[str, Any]] | None = None,
    conversation_details: Mapping[str, Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Build a task response dict from a concept document."""
    task_concept_id = doc.get("concept_id", "")
    relationships = doc.get("relationships", {})

    # Get text relations for title, description, status, priority, start/due dates
    if texts is None:
        try:
            texts = get_texts_for_concept(task_concept_id) or []
        except Exception:
            texts = []

    title = None
    description = None
    status = TASK_STATUS_PENDING
    priority = PRIORITY_MEDIUM
    start_date = None
    due_date = None
    task_role = None
    next_checkpoint = None
    progress_signal = None
    evidence = None
    notes = None
    reference_code = None

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
        elif predicate in TASK_ROLE_TEXT_PREDICATES:
            task_role = text_value
        elif predicate in NEXT_CHECKPOINT_TEXT_PREDICATES:
            next_checkpoint = text_value
        elif predicate in PROGRESS_SIGNAL_TEXT_PREDICATES:
            progress_signal = text_value
        elif predicate in EVIDENCE_TEXT_PREDICATES:
            evidence = text_value
        elif predicate in TASK_REFERENCE_CODE_TEXT_PREDICATES:
            reference_code = text_value
        elif predicate in ("hasNote", "#V#hasNote"):
            notes = text_value

    # Get relationship values
    assignee = _first_relationship_value(relationships.get(PREDICATE_HAS_ASSIGNEE))
    created_by = _first_relationship_value(relationships.get(PREDICATE_HAS_CREATED_BY))
    report_to = _first_relationship_value_from_aliases(
        relationships,
        REPORTS_TO_RELATIONSHIP_PREDICATES,
    )
    originating_conversation = _first_relationship_value(
        relationships.get(PREDICATE_HAS_ORIGINATING_CONVERSATION)
    )
    task_type_ids = _task_type_ids_from_doc(doc)

    metadata = doc.get("metadata", {})
    organisation_concept_id = _task_doc_organisation_concept_id(doc)
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
        _normalise_optional_concept_id(reporter_raw)
        if reporter_raw is not None
        else None
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
    bulk_task_collections = normalise_bulk_task_collections(
        metadata.get(TASK_METADATA_KEY_BULK_TASK_COLLECTIONS)
    )
    hidden_by_default_bulk_task_collections = [
        collection
        for collection in bulk_task_collections
        if bool(collection.get("hidden_by_default"))
    ]
    explicit_task_source = _first_relationship_value_from_aliases(
        relationships,
        TASK_SOURCE_RELATIONSHIP_PREDICATES,
    )
    task_source_id = _infer_task_source_id(
        explicit_source_id=explicit_task_source,
        external_references=external_references,
    )
    task_source_definition = get_task_source_definition(task_source_id)
    task_type_payload = [
        definition
        for type_id in task_type_ids
        for definition in [get_task_type_definition(type_id)]
        if isinstance(definition, dict)
    ]
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
    if originating_conversation and conversation_details is not None:
        conv_details = conversation_details.get(originating_conversation)
        if conv_details:
            conversation_session_id = conv_details.get("session_id")
            conversation_name = conv_details.get("name") or conv_details.get("topic")
    elif originating_conversation:
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
        "report_to_concept_id": report_to,
        "originating_conversation_id": originating_conversation,
        "conversation_session_id": conversation_session_id,
        "conversation_name": conversation_name,
        "start_date": start_date,
        "due_date": due_date,
        "organisation_concept_id": organisation_concept_id,
        "labels": labels,
        "components": components,
        "fix_versions": fix_versions,
        "sprint_values": sprint_values,
        "backlog_rank": backlog_rank,
        "reporter_concept_id": reporter_concept_id,
        "watcher_concept_ids": watcher_concept_ids,
        "task_type_ids": task_type_ids,
        "task_types": task_type_payload,
        "primary_task_type_id": task_type_ids[0] if task_type_ids else None,
        "primary_task_type_label": (
            task_type_payload[0]["label"] if task_type_payload else None
        ),
        "task_source_id": task_source_id,
        "task_source_label": (
            task_source_definition.get("label")
            if isinstance(task_source_definition, dict)
            else None
        ),
        "task_source_slug": (
            task_source_definition.get("slug")
            if isinstance(task_source_definition, dict)
            else None
        ),
        "is_imported_jira_task": task_source_id == JIRA_IMPORTED_TASK_SOURCE_ID,
        "task_role": task_role,
        "next_checkpoint": next_checkpoint,
        "progress_signal": progress_signal,
        "evidence": evidence,
        "notes": notes,
        "reference_code": reference_code,
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
        "project_concept_id": metadata.get("project_concept_id"),
        "collection_concept_ids": metadata.get("collection_concept_ids", []),
        "bulk_task_collections": bulk_task_collections,
        "hidden_by_default_bulk_task_collections": (
            hidden_by_default_bulk_task_collections
        ),
        "is_hidden_by_default_bulk_task": bool(hidden_by_default_bulk_task_collections),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


def update_task_status(
    task_concept_id: str,
    status: str,
    *,
    actor_concept_id: str | None = None,
) -> Dict[str, Any]:
    """Update a task's status.

    Args:
        task_concept_id: The task's concept_id
        status: New status (pending, in_progress, completed, cancelled, blocked)
        actor_concept_id: Trusted actor performing the transition, when available

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
    transition_actor_id = _normalise_optional_concept_id(
        actor_concept_id
    ) or existing_task.get("created_by_concept_id")
    previous_status = existing_task.get("status")
    if isinstance(previous_status, str) and previous_status == status:
        # No state transition occurred, so skip duplicate writes/events.
        return existing_task

    # Update status text relation
    try:
        _upsert_optional_task_text(
            task_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_TASK_STATUS,
            value=status,
            lang="en",
        )
    except Exception as e:
        raise TaskManagementError(f"Failed to update task status: {e}") from e

    # A transition must replace the current value, including when returning to
    # a previously used status. Do not emit completion effects for a stale read.
    persisted_task = get_task(task_concept_id)
    if persisted_task.get("status") != status:
        raise TaskManagementError("Task status update did not match canonical read-back")

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
            actor_concept_id=transition_actor_id,
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
                created_by_concept_id=transition_actor_id,
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
            previous_status=(
                previous_status if isinstance(previous_status, str) else None
            ),
            new_status=status,
            updated_at_iso=updated_at_iso,
            created_by_concept_id=transition_actor_id,
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


def assign_task(
    task_concept_id: str, assignee_concept_id: str, *, update_visibility: bool = True
) -> Dict[str, Any]:
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

    # Source assignment records do not grant the source participant new access.
    if update_visibility:
        # Also add new assignee to the canonical user visibility predicate.
        for predicate in SPECIFIC_TO_USER_PREDICATES_WRITE:
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
    limit: int | None = None,
    return_metadata: bool = False,
) -> List[Dict[str, Any]] | Dict[str, Any]:
    """Get all tasks assigned to a user.

    Args:
        user_concept_id: The user's concept_id
        status_filter: Optional status to filter by
        include_created: If True, also include tasks created by the user
        limit: Optional maximum number of accessible task concepts to hydrate
        return_metadata: Return page metadata in addition to the task rows

    Returns:
        Task dicts, or a bounded page with count and total metadata when
        ``return_metadata`` is requested.
    """
    normalised_id = ensure_v_concept_prefix(user_concept_id)
    if not normalised_id:
        if return_metadata:
            return {
                "tasks": [],
                "count": 0,
                "total": 0,
                "total_is_exhaustive": True,
                "has_more": False,
                "limit": limit,
            }
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

    page_limit: int | None = None
    if limit is not None:
        try:
            page_limit = max(1, min(int(limit), TASK_LIST_MAX_PAGE_SIZE))
        except (TypeError, ValueError):
            page_limit = 50

    total: int | None = None
    if return_metadata:
        try:
            # ConceptsRepository applies the trusted actor visibility filter
            # before counting, so inaccessible tasks neither leak nor consume
            # this page's cap.
            total = int(ConceptsRepository.count_documents(query))
        except Exception:
            # A failed count must not turn a bounded page into a fabricated
            # total. The readable task rows still provide a useful response.
            total = None

    cursor = ConceptsRepository.find(
        query,
        sort=[("updated_at", -1), ("created_at", -1), ("concept_id", 1)],
        limit=page_limit or 0,
    )
    tasks = _build_task_responses(cursor)

    # Apply status filter if provided
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]

    if return_metadata:
        total_is_exhaustive = total is not None and not (
            status_filter and page_limit is not None and total > page_limit
        )
        reported_total = len(tasks) if total_is_exhaustive and status_filter else total
        has_more: bool | None
        if total is None:
            has_more = None
        elif status_filter and not total_is_exhaustive:
            # The bounded source page has more actor-visible candidates, but
            # their text-backed statuses are unknown until hydrated.
            has_more = None
        elif status_filter and total_is_exhaustive:
            has_more = False
        else:
            has_more = total > (page_limit if page_limit is not None else len(tasks))
        return {
            "tasks": tasks,
            "count": len(tasks),
            "total": reported_total if total_is_exhaustive else None,
            "total_is_exhaustive": total_is_exhaustive,
            "has_more": has_more,
            "limit": page_limit,
        }

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
    return _build_task_responses(cursor)


def list_tasks(
    *,
    organisation_concept_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    priority_filter: Optional[str] = None,
    task_type_ids: list[str] | str | None = None,
    task_source_ids: list[str] | str | None = None,
    limit: int | None = 50,
) -> List[Dict[str, Any]]:
    """List tasks with optional filters.

    Args:
        organisation_concept_id: Filter by organisation
        status_filter: Filter by status
        priority_filter: Filter by priority
        task_type_ids: Filter by canonical task category IDs/slugs
        task_source_ids: Filter by canonical task source IDs/slugs
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

    cursor = ConceptsRepository.find(
        query,
        sort=[("updated_at", -1), ("created_at", -1), ("concept_id", 1)],
    )
    tasks = _build_task_responses(cursor)

    # Apply status and priority filters (post-query since they're in text relations)
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    if priority_filter:
        tasks = [t for t in tasks if t.get("priority") == priority_filter]
    if task_type_ids is not None:
        try:
            required_type_ids = set(normalise_task_type_ids(task_type_ids))
        except ValueError as exc:
            raise InvalidTaskDataError(str(exc)) from exc
        if required_type_ids:
            tasks = [
                task
                for task in tasks
                if required_type_ids.intersection(set(task.get("task_type_ids") or []))
            ]
    if task_source_ids is not None:
        raw_source_values = (
            [task_source_ids]
            if isinstance(task_source_ids, str)
            else (task_source_ids or [])
        )
        normalised_source_ids: set[str] = set()
        for raw_value in raw_source_values:
            try:
                source_id = normalise_task_source_id(raw_value)
            except ValueError as exc:
                raise InvalidTaskDataError(str(exc)) from exc
            if source_id:
                normalised_source_ids.add(source_id)
        if normalised_source_ids:
            tasks = [
                task
                for task in tasks
                if task.get("task_source_id") in normalised_source_ids
            ]

    if limit is not None:
        try:
            limit_value = max(1, int(limit))
        except (TypeError, ValueError):
            limit_value = 50
        tasks = tasks[:limit_value]

    return tasks


def _and_query_clauses(*clauses: Mapping[str, Any] | None) -> Dict[str, Any]:
    active_clauses: list[Dict[str, Any]] = []
    for clause in clauses:
        if not clause:
            continue
        if set(clause.keys()) == {"$and"} and isinstance(clause.get("$and"), list):
            active_clauses.extend(
                dict(nested_clause)
                for nested_clause in clause["$and"]
                if isinstance(nested_clause, Mapping) and nested_clause
            )
            continue
        active_clauses.append(dict(clause))
    if not active_clauses:
        return {}
    if len(active_clauses) == 1:
        return active_clauses[0]
    return {"$and": active_clauses}


def _hidden_bulk_collection_query(
    collection_ids: set[str],
) -> Dict[str, Any]:
    elem_match: Dict[str, Any] = {"hidden_by_default": True}
    if collection_ids:
        elem_match["collection_id"] = {"$in": sorted(collection_ids)}
    return {
        f"metadata.{TASK_METADATA_KEY_BULK_TASK_COLLECTIONS}": {
            "$elemMatch": elem_match,
        }
    }


def _apply_bulk_visibility_query_filter(
    base_query: Mapping[str, Any],
    *,
    bulk_visibility: str,
    collection_ids: set[str],
) -> Dict[str, Any]:
    hidden_clause = _hidden_bulk_collection_query(collection_ids)
    if bulk_visibility == BULK_TASK_VISIBILITY_ONLY:
        return _and_query_clauses(base_query, hidden_clause)
    if bulk_visibility == BULK_TASK_VISIBILITY_EXCLUDE:
        return _and_query_clauses(base_query, {"$nor": [hidden_clause]})
    return dict(base_query)


def _has_nonempty_filter_values(value: list[str] | str | None) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(item, str) and bool(item.strip()) for item in value)
    return False


def _build_task_listing_query(
    *,
    organisation_concept_id: str | None = None,
    organisation_scope_mode: str | None = None,
    user_concept_id: str | None = None,
    include_created: bool = False,
    assignee_concept_id: str | None = None,
    created_by_concept_id: str | None = None,
) -> Dict[str, Any]:
    base_query: Dict[str, Any] = {
        "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
    }

    organisation_id = None
    if organisation_concept_id:
        organisation_id = _normalise_optional_concept_id(organisation_concept_id)
        if not organisation_id:
            raise InvalidTaskDataError(
                f"Invalid organisation_concept_id: {organisation_concept_id}"
            )
    organisation_field = f"metadata.{TASK_METADATA_KEY_ORGANISATION}"
    if organisation_scope_mode == TASK_ORGANISATION_SCOPE_CURRENT_PLUS_UNSCOPED:
        if not organisation_id:
            raise InvalidTaskDataError(
                "current_plus_unscoped requires organisation_concept_id"
            )
        base_query["$or"] = [
            {organisation_field: organisation_id},
            {organisation_field: None},
            {organisation_field: {"$exists": False}},
        ]
    elif organisation_scope_mode == TASK_ORGANISATION_SCOPE_UNSCOPED_ONLY:
        base_query["$or"] = [
            {organisation_field: None},
            {organisation_field: {"$exists": False}},
        ]
    elif organisation_scope_mode is not None:
        raise InvalidTaskDataError(
            f"Invalid organisation_scope_mode: {organisation_scope_mode}"
        )
    elif organisation_id:
        # Preserve the existing exact-organisation contract for direct callers.
        base_query[organisation_field] = organisation_id

    clauses: list[Mapping[str, Any]] = [base_query]

    if user_concept_id:
        user_id = _normalise_optional_concept_id(user_concept_id)
        if not user_id:
            raise InvalidTaskDataError(f"Invalid user_concept_id: {user_concept_id}")
        if include_created:
            clauses.append(
                {
                    "$or": [
                        {f"relationships.{PREDICATE_HAS_ASSIGNEE}": user_id},
                        {f"relationships.{PREDICATE_HAS_CREATED_BY}": user_id},
                    ]
                }
            )
        else:
            clauses.append({f"relationships.{PREDICATE_HAS_ASSIGNEE}": user_id})

    if assignee_concept_id is not None:
        assignee_id = _normalise_optional_concept_id(assignee_concept_id)
        if not assignee_id:
            raise InvalidTaskDataError(
                f"Invalid assignee_concept_id: {assignee_concept_id}"
            )
        clauses.append({f"relationships.{PREDICATE_HAS_ASSIGNEE}": assignee_id})

    if created_by_concept_id is not None:
        creator_id = _normalise_optional_concept_id(created_by_concept_id)
        if not creator_id:
            raise InvalidTaskDataError(
                f"Invalid created_by_concept_id: {created_by_concept_id}"
            )
        clauses.append({f"relationships.{PREDICATE_HAS_CREATED_BY}": creator_id})

    return _and_query_clauses(*clauses)


def _task_doc_matches_organisation_scope(
    doc: Mapping[str, Any],
    *,
    organisation_concept_id: str | None,
    organisation_scope_mode: str | None,
) -> bool:
    if organisation_scope_mode is None:
        return True
    effective_organisation_id = _task_doc_organisation_concept_id(doc)
    if organisation_scope_mode == TASK_ORGANISATION_SCOPE_UNSCOPED_ONLY:
        return effective_organisation_id is None
    if organisation_scope_mode == TASK_ORGANISATION_SCOPE_CURRENT_PLUS_UNSCOPED:
        return effective_organisation_id in {None, organisation_concept_id}
    return False


def _build_bulk_visibility_summary_for_query(
    base_query: Mapping[str, Any],
    *,
    collection_ids: set[str],
    organisation_concept_id: str | None = None,
    organisation_scope_mode: str | None = None,
) -> Dict[str, Any]:
    hidden_query = _and_query_clauses(
        base_query,
        _hidden_bulk_collection_query(collection_ids),
    )
    projection = {
        "concept_id": 1,
        "relationships": 1,
        f"metadata.{TASK_METADATA_KEY_ORGANISATION}": 1,
        f"metadata.{TASK_METADATA_KEY_BULK_TASK_COLLECTIONS}": 1,
    }
    hidden_task_stubs: list[Dict[str, Any]] = []
    for doc in ConceptsRepository.find(hidden_query, projection=projection):
        if not isinstance(doc, Mapping):
            continue
        if not _task_doc_matches_organisation_scope(
            doc,
            organisation_concept_id=organisation_concept_id,
            organisation_scope_mode=organisation_scope_mode,
        ):
            continue
        metadata_raw = doc.get("metadata")
        metadata: Mapping[str, Any] = (
            metadata_raw if isinstance(metadata_raw, Mapping) else {}
        )
        hidden_task_stubs.append(
            {
                "task_concept_id": doc.get("concept_id"),
                "bulk_task_collections": metadata.get(
                    TASK_METADATA_KEY_BULK_TASK_COLLECTIONS
                ),
            }
        )
    return {
        "hidden_bulk_task_total": len(hidden_task_stubs),
        "hidden_bulk_task_collections": _build_bulk_task_collection_summaries(
            hidden_task_stubs,
            collection_ids=collection_ids,
        ),
    }


def _filter_task_response_list(
    tasks: Iterable[Dict[str, Any]],
    *,
    status_filter: str | None = None,
    priority_filter: str | None = None,
    task_type_ids: list[str] | str | None = None,
    task_source_ids: list[str] | str | None = None,
) -> list[Dict[str, Any]]:
    filtered_tasks = list(tasks)

    if status_filter:
        filtered_tasks = [
            task for task in filtered_tasks if task.get("status") == status_filter
        ]
    if priority_filter:
        filtered_tasks = [
            task for task in filtered_tasks if task.get("priority") == priority_filter
        ]
    if task_type_ids is not None:
        try:
            required_type_ids = set(normalise_task_type_ids(task_type_ids))
        except ValueError as exc:
            raise InvalidTaskDataError(str(exc)) from exc
        if required_type_ids:
            filtered_tasks = [
                task
                for task in filtered_tasks
                if required_type_ids.intersection(set(task.get("task_type_ids") or []))
            ]
    if task_source_ids is not None:
        raw_source_values = (
            [task_source_ids]
            if isinstance(task_source_ids, str)
            else (task_source_ids or [])
        )
        normalised_source_ids: set[str] = set()
        for raw_value in raw_source_values:
            try:
                source_id = normalise_task_source_id(raw_value)
            except ValueError as exc:
                raise InvalidTaskDataError(str(exc)) from exc
            if source_id:
                normalised_source_ids.add(source_id)
        if normalised_source_ids:
            filtered_tasks = [
                task
                for task in filtered_tasks
                if task.get("task_source_id") in normalised_source_ids
            ]

    return filtered_tasks


def list_tasks_with_visibility(
    *,
    organisation_concept_id: str | None = None,
    organisation_scope_mode: str | None = None,
    user_concept_id: str | None = None,
    include_created: bool = False,
    assignee_concept_id: str | None = None,
    created_by_concept_id: str | None = None,
    status_filter: str | None = None,
    priority_filter: str | None = None,
    task_type_ids: list[str] | str | None = None,
    task_source_ids: list[str] | str | None = None,
    bulk_visibility: str | None = BULK_TASK_VISIBILITY_INCLUDE,
    bulk_collection_ids: list[str] | str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_total: bool = True,
    include_bulk_summary: bool = True,
) -> Dict[str, Any]:
    """List tasks with repository-level user-scope and bulk-visibility filters.

    This keeps All Tasks from building responses for hidden bulk collections
    before it has selected the page the UI actually needs.
    """

    mark_load, finish_load = _new_task_list_load_timer(
        "task_management.list_tasks_with_visibility"
    )

    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    visibility = _normalise_bulk_task_visibility(bulk_visibility)
    collection_ids = _normalise_bulk_task_collection_ids(bulk_collection_ids)
    include_total = include_total is not False
    include_bulk_summary = include_bulk_summary is not False
    mark_load(
        "normalise_inputs",
        limit=limit,
        offset=offset,
        bulk_visibility=visibility,
        bulk_collection_count=len(collection_ids),
        include_total=include_total,
        include_bulk_summary=include_bulk_summary,
    )

    base_query = _build_task_listing_query(
        organisation_concept_id=organisation_concept_id,
        organisation_scope_mode=organisation_scope_mode,
        user_concept_id=user_concept_id,
        include_created=include_created,
        assignee_concept_id=assignee_concept_id,
        created_by_concept_id=created_by_concept_id,
    )
    visibility_query = _apply_bulk_visibility_query_filter(
        base_query,
        bulk_visibility=visibility,
        collection_ids=collection_ids,
    )
    mark_load(
        "build_queries",
        organisation_scope_mode=organisation_scope_mode,
        has_organisation_scope=bool(organisation_concept_id),
        has_user_scope=bool(user_concept_id),
        include_created=bool(include_created),
        has_assignee_scope=bool(assignee_concept_id),
        has_created_by_scope=bool(created_by_concept_id),
    )

    visibility_summary = {
        "hidden_bulk_task_total": 0,
        "hidden_bulk_task_collections": [],
    }
    if include_bulk_summary:
        visibility_summary = _build_bulk_visibility_summary_for_query(
            base_query,
            collection_ids=collection_ids,
            organisation_concept_id=organisation_concept_id,
            organisation_scope_mode=organisation_scope_mode,
        )
        mark_load(
            "bulk_visibility_summary",
            hidden_bulk_task_total=visibility_summary["hidden_bulk_task_total"],
            hidden_bulk_collection_count=len(
                visibility_summary["hidden_bulk_task_collections"]
            ),
        )
    else:
        mark_load("bulk_visibility_summary_skipped")

    sort_spec = [("updated_at", -1), ("created_at", -1), ("concept_id", 1)]
    requires_post_filter = any(
        (
            bool(status_filter),
            bool(priority_filter),
            _has_nonempty_filter_values(task_type_ids),
            _has_nonempty_filter_values(task_source_ids),
        )
    )
    mark_load(
        "prepare_sort_and_filters",
        requires_post_filter=requires_post_filter,
        has_status_filter=bool(status_filter),
        has_priority_filter=bool(priority_filter),
        has_task_type_filter=_has_nonempty_filter_values(task_type_ids),
        has_task_source_filter=_has_nonempty_filter_values(task_source_ids),
    )

    if requires_post_filter:
        docs = list(
            ConceptsRepository.find(
                visibility_query,
                sort=sort_spec,
            )
        )
        mark_load("repository_find_all", raw_count=len(docs))

        docs = [
            doc
            for doc in docs
            if _task_doc_matches_organisation_scope(
                doc,
                organisation_concept_id=organisation_concept_id,
                organisation_scope_mode=organisation_scope_mode,
            )
        ]
        mark_load("organisation_scope_filter", filtered_count=len(docs))

        tasks = _build_task_responses(docs)
        mark_load("build_task_responses", response_count=len(tasks))

        tasks = _filter_task_response_list(
            tasks,
            status_filter=status_filter,
            priority_filter=priority_filter,
            task_type_ids=task_type_ids,
            task_source_ids=task_source_ids,
        )
        mark_load("post_filter", filtered_count=len(tasks))

        total = len(tasks)
        paged_tasks = tasks[offset : offset + limit]
        has_more = total > offset + len(paged_tasks)
        total_is_exhaustive = True
        mark_load("paginate", returned_count=len(paged_tasks), total=total)
    else:
        count_failed = False
        total: int | None = None
        if include_total:
            try:
                total = int(ConceptsRepository.count_documents(visibility_query) or 0)
            except Exception:
                count_failed = True
            mark_load("repository_count", total=total, failed=count_failed)
        else:
            mark_load("repository_count_skipped")

        docs = list(
            ConceptsRepository.find(
                visibility_query,
                sort=sort_spec,
                skip=offset,
                limit=limit if include_total else limit + 1,
            )
        )
        mark_load("repository_find_page", raw_count=len(docs))

        if include_total:
            has_more = total is not None and total > offset + min(len(docs), limit)
            total_is_exhaustive = total is not None
        else:
            has_more = len(docs) > limit
            total_is_exhaustive = False
            docs = docs[:limit]

        docs = [
            doc
            for doc in docs
            if _task_doc_matches_organisation_scope(
                doc,
                organisation_concept_id=organisation_concept_id,
                organisation_scope_mode=organisation_scope_mode,
            )
        ]
        mark_load("organisation_scope_filter", filtered_count=len(docs))
        if organisation_scope_mode is not None:
            # The indexed metadata mirror bounds the page, while represented
            # relationships make the final authority decision. Legacy drift can
            # therefore make a metadata count an upper bound rather than exact.
            total_is_exhaustive = False

        paged_tasks = _build_task_responses(docs)
        mark_load("build_task_responses", response_count=len(paged_tasks))

        if include_total and total == 0 and paged_tasks:
            total = offset + len(paged_tasks)
            has_more = False
            mark_load("infer_total_from_page", total=total)

    payload = {
        "tasks": paged_tasks,
        "total": total,
        "count": len(paged_tasks),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "total_is_exhaustive": total_is_exhaustive,
        "bulk_visibility": visibility,
        "bulk_collection_ids": sorted(collection_ids),
        "bulk_summary_included": include_bulk_summary,
        "hidden_bulk_task_total": visibility_summary["hidden_bulk_task_total"],
        "hidden_bulk_task_collections": visibility_summary[
            "hidden_bulk_task_collections"
        ],
        "organisation_concept_id": organisation_concept_id,
        "organisation_scope_mode": organisation_scope_mode,
    }
    mark_load("response_payload", returned_count=len(paged_tasks), total=total)
    payload["load_telemetry"] = finish_load(
        request={
            "limit": limit,
            "offset": offset,
            "bulk_visibility": visibility,
            "bulk_collection_count": len(collection_ids),
            "organisation_concept_id": organisation_concept_id,
            "organisation_scope_mode": organisation_scope_mode,
            "has_user_scope": bool(user_concept_id),
            "include_created": bool(include_created),
            "has_assignee_scope": bool(assignee_concept_id),
            "has_created_by_scope": bool(created_by_concept_id),
            "has_status_filter": bool(status_filter),
            "has_priority_filter": bool(priority_filter),
            "has_task_type_filter": _has_nonempty_filter_values(task_type_ids),
            "has_task_source_filter": _has_nonempty_filter_values(task_source_ids),
            "requires_post_filter": requires_post_filter,
            "include_total": include_total,
            "include_bulk_summary": include_bulk_summary,
        },
        returned_count=len(paged_tasks),
        total=total,
    )
    return payload


def _task_text_search_predicates() -> tuple[str, ...]:
    """Return every task text predicate searched by ``search_tasks(query=...)``."""
    return tuple(
        dict.fromkeys(
            (
                PREDICATE_HAS_NAME,
                "hasName",
                PREDICATE_HAS_DESCRIPTION,
                "hasDescription",
                *TASK_REFERENCE_CODE_TEXT_PREDICATES,
                *TASK_ROLE_TEXT_PREDICATES,
                *NEXT_CHECKPOINT_TEXT_PREDICATES,
                *PROGRESS_SIGNAL_TEXT_PREDICATES,
                *EVIDENCE_TEXT_PREDICATES,
                "#V#hasNote",
                "hasNote",
            )
        )
    )


def _task_response_text_predicates() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *_task_text_search_predicates(),
                PREDICATE_HAS_TASK_STATUS,
                "hasTaskStatus",
                PREDICATE_HAS_PRIORITY,
                "hasPriority",
                PREDICATE_HAS_START_DATE,
                "hasStartDate",
                PREDICATE_HAS_DUE_DATE,
                "hasDueDate",
            )
        )
    )


def _task_text_rows_match_search_query(
    texts: Iterable[Mapping[str, Any]],
    query_lower: str,
) -> bool:
    return any(
        str(text.get("predicate") or "") in _task_text_search_predicates()
        and query_lower in str(text.get("text") or "").lower()
        for text in texts
    )


def _query_matching_task_taxonomy_ids(
    query_lower: str,
) -> tuple[set[str], set[str]]:
    matching_types = {
        definition["concept_id"]
        for definition in TASK_TYPE_DEFINITIONS
        if query_lower in definition["label"].lower()
        or query_lower in definition["slug"].lower()
    }
    matching_sources = {
        definition["concept_id"]
        for definition in TASK_SOURCE_DEFINITIONS
        if query_lower in definition["label"].lower()
        or query_lower in definition["slug"].lower()
    }
    return matching_types, matching_sources


def _actor_scoped_task_ids(
    query_filter: Mapping[str, Any],
    *,
    extra_filter: Mapping[str, Any] | None = None,
) -> tuple[list[str], bool]:
    scoped_filter: Dict[str, Any] = dict(query_filter)
    if extra_filter:
        scoped_filter = {"$and": [scoped_filter, dict(extra_filter)]}
    rows = list(
        ConceptsRepository.find(
            scoped_filter,
            projection={"concept_id": 1},
            sort=[("updated_at", -1), ("created_at", -1), ("concept_id", 1)],
            limit=TASK_SEARCH_ACCESSIBLE_CANDIDATE_LIMIT + 1,
        )
    )
    truncated = len(rows) > TASK_SEARCH_ACCESSIBLE_CANDIDATE_LIMIT
    return (
        [
            concept_id
            for row in rows[:TASK_SEARCH_ACCESSIBLE_CANDIDATE_LIMIT]
            for concept_id in [row.get("concept_id")]
            if isinstance(concept_id, str) and concept_id
        ],
        truncated,
    )


def _actor_scoped_task_query_candidates(
    query_filter: Mapping[str, Any],
    query: str,
) -> tuple[list[Dict[str, Any]], Mapping[str, List[Dict[str, Any]]], dict[str, Any]]:
    """Read a bounded, access-controlled task window before matching task text."""
    candidate_ids, truncated = _actor_scoped_task_ids(query_filter)
    if not candidate_ids:
        return [], {}, {
            "applied": True,
            "bounded": True,
            "truncated": truncated,
            "total_is_exhaustive": not truncated,
        }

    try:
        texts_by_task = get_texts_for_concepts(
            candidate_ids,
            predicates=_task_response_text_predicates(),
            limit_per_concept=len(_task_response_text_predicates()),
        )
    except Exception as exc:
        raise TaskManagementError(
            "Accessible task text search is temporarily unavailable"
        ) from exc

    query_lower = query.strip().lower()
    matching_ids = {
        task_id
        for task_id in candidate_ids
        if _task_text_rows_match_search_query(
            texts_by_task.get(task_id, []), query_lower
        )
    }
    matching_types, matching_sources = _query_matching_task_taxonomy_ids(query_lower)
    if matching_types:
        type_ids, type_truncated = _actor_scoped_task_ids(
            query_filter,
            extra_filter={"relationships.is_an_instance_of": {"$in": sorted(matching_types)}},
        )
        matching_ids.update(type_ids)
        truncated = truncated or type_truncated
    if matching_sources:
        source_clauses = [
            {
                f"relationships.{predicate}": {"$in": sorted(matching_sources)}
            }
            for predicate in TASK_SOURCE_RELATIONSHIP_PREDICATES
        ]
        if JIRA_IMPORTED_TASK_SOURCE_ID in matching_sources:
            source_clauses.append(
                {"metadata.external_references.jira.external_id": {"$exists": True}}
            )
        if DEFAULT_TASK_SOURCE_ID in matching_sources:
            source_clauses.append(
                {
                    "$and": [
                        {
                            f"relationships.{predicate}": {"$exists": False}
                        }
                        for predicate in TASK_SOURCE_RELATIONSHIP_PREDICATES
                    ]
                    + [
                        {
                            "metadata.external_references.jira.external_id": {
                                "$exists": False
                            }
                        }
                    ]
                }
            )
        source_ids, source_truncated = _actor_scoped_task_ids(
            query_filter,
            extra_filter={"$or": source_clauses},
        )
        matching_ids.update(source_ids)
        truncated = truncated or source_truncated

    if not matching_ids:
        return [], {}, {
            "applied": True,
            "bounded": True,
            "truncated": truncated,
            "total_is_exhaustive": not truncated,
        }

    full_docs = list(
        ConceptsRepository.find(
            {"$and": [dict(query_filter), {"concept_id": {"$in": sorted(matching_ids)}}]},
            sort=[("updated_at", -1), ("created_at", -1), ("concept_id", 1)],
        )
    )
    missing_text_ids = [
        str(doc.get("concept_id") or "")
        for doc in full_docs
        if str(doc.get("concept_id") or "") not in texts_by_task
    ]
    if missing_text_ids:
        try:
            texts_by_task.update(
                get_texts_for_concepts(
                    missing_text_ids,
                    predicates=_task_response_text_predicates(),
                    limit_per_concept=len(_task_response_text_predicates()),
                )
            )
        except Exception as exc:
            raise TaskManagementError(
                "Accessible task text search is temporarily unavailable"
            ) from exc
    return full_docs, texts_by_task, {
        "applied": True,
        "bounded": True,
        "truncated": truncated,
        "total_is_exhaustive": not truncated,
    }


def search_tasks(
    *,
    query: str | None = None,
    project_concept_id: str | None = None,
    collection_concept_id: str | None = None,
    status_filter: str | None = None,
    statuses: list[str] | None = None,
    task_type_ids: list[str] | str | None = None,
    task_source_id: str | None = None,
    task_source_ids: list[str] | str | None = None,
    assignee_concept_id: str | None = None,
    created_by_concept_id: str | None = None,
    report_to_concept_id: str | None = None,
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
    bulk_visibility: str | None = BULK_TASK_VISIBILITY_INCLUDE,
    bulk_collection_ids: list[str] | str | None = None,
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
    if project_concept_id:
        query_filter["metadata.project_concept_id"] = project_concept_id
    if collection_concept_id:
        from .task_project_service import collection_task_query

        query_filter.setdefault("$and", []).append(
            collection_task_query(collection_concept_id)
        )
    if organisation_concept_id:
        org_id = _normalise_optional_concept_id(organisation_concept_id)
        if not org_id:
            raise InvalidTaskDataError(
                f"Invalid organisation_concept_id: {organisation_concept_id}"
            )
        query_filter[f"metadata.{TASK_METADATA_KEY_ORGANISATION}"] = org_id

    assignee_id = None
    if assignee_concept_id is not None:
        assignee_id = _normalise_optional_concept_id(assignee_concept_id)
        if not assignee_id:
            raise InvalidTaskDataError(
                f"Invalid assignee_concept_id: {assignee_concept_id}"
            )
        query_filter[f"relationships.{PREDICATE_HAS_ASSIGNEE}"] = assignee_id

    creator_id = None
    if created_by_concept_id is not None:
        creator_id = _normalise_optional_concept_id(created_by_concept_id)
        if not creator_id:
            raise InvalidTaskDataError(
                f"Invalid created_by_concept_id: {created_by_concept_id}"
            )
        query_filter[f"relationships.{PREDICATE_HAS_CREATED_BY}"] = creator_id

    report_to_id = None
    if report_to_concept_id is not None:
        report_to_id = _normalise_optional_concept_id(report_to_concept_id)
        if not report_to_id:
            raise InvalidTaskDataError(
                f"Invalid report_to_concept_id: {report_to_concept_id}"
            )
        report_to_predicates = _relationship_predicate_aliases(PREDICATE_REPORTS_TO)
        query_filter["$or"] = [
            {f"relationships.{predicate}": report_to_id}
            for predicate in report_to_predicates
        ]

    query_candidate_prefilter: dict[str, Any] = {
        "applied": False,
        "total_is_exhaustive": True,
    }
    query_texts_by_task: Mapping[str, List[Dict[str, Any]]] | None = None
    if isinstance(query, str) and query.strip():
        docs, query_texts_by_task, query_candidate_prefilter = (
            _actor_scoped_task_query_candidates(query_filter, query)
        )
    else:
        # Use deterministic storage ordering before in-memory filters so paged reads do
        # not drift or duplicate items across offsets.
        docs = list(
            ConceptsRepository.find(
                query_filter,
                sort=[("updated_at", -1), ("created_at", -1), ("concept_id", 1)],
            )
        )
    tasks = _build_task_responses(docs, texts_by_task=query_texts_by_task)

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

    if task_type_ids is not None:
        try:
            required_type_ids = set(normalise_task_type_ids(task_type_ids))
        except ValueError as exc:
            raise InvalidTaskDataError(str(exc)) from exc
        if required_type_ids:
            tasks = [
                task
                for task in tasks
                if required_type_ids.intersection(set(task.get("task_type_ids") or []))
            ]

    raw_source_values: list[str] = []
    if isinstance(task_source_id, str) and task_source_id.strip():
        raw_source_values.append(task_source_id)
    if isinstance(task_source_ids, str) and task_source_ids.strip():
        raw_source_values.append(task_source_ids)
    elif isinstance(task_source_ids, list):
        raw_source_values.extend(
            value
            for value in task_source_ids
            if isinstance(value, str) and value.strip()
        )
    if raw_source_values:
        source_filters: set[str] = set()
        for raw_value in raw_source_values:
            try:
                source_id = normalise_task_source_id(raw_value)
            except ValueError as exc:
                raise InvalidTaskDataError(str(exc)) from exc
            if source_id:
                source_filters.add(source_id)
        if source_filters:
            tasks = [
                task for task in tasks if task.get("task_source_id") in source_filters
            ]

    if assignee_id is not None:
        tasks = [
            task for task in tasks if task.get("assignee_concept_id") == assignee_id
        ]

    if creator_id is not None:
        tasks = [
            task for task in tasks if task.get("created_by_concept_id") == creator_id
        ]

    if report_to_id is not None:
        tasks = [
            task for task in tasks if task.get("report_to_concept_id") == report_to_id
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
            task for task in tasks if task.get("parent_task_concept_id") == parent_id
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
            or query_lower in str(task.get("reference_code") or "").lower()
            or query_lower in str(task.get("task_role") or "").lower()
            or query_lower in str(task.get("next_checkpoint") or "").lower()
            or query_lower in str(task.get("progress_signal") or "").lower()
            or query_lower in str(task.get("evidence") or "").lower()
            or query_lower in str(task.get("notes") or "").lower()
            or query_lower in str(task.get("task_source_label") or "").lower()
            or query_lower in str(task.get("task_source_slug") or "").lower()
            or any(
                query_lower in str(item.get("label") or "").lower()
                or query_lower in str(item.get("slug") or "").lower()
                for item in (task.get("task_types") or [])
                if isinstance(item, dict)
            )
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

    visibility_payload = apply_bulk_task_visibility(
        tasks,
        bulk_visibility=bulk_visibility,
        bulk_collection_ids=bulk_collection_ids,
    )
    tasks = visibility_payload["tasks"]
    total = len(tasks)
    paged = tasks[offset : offset + limit]
    return {
        "tasks": paged,
        "total": total,
        "count": len(paged),
        "offset": offset,
        "limit": limit,
        "bulk_visibility": visibility_payload["bulk_visibility"],
        "bulk_collection_ids": visibility_payload["bulk_collection_ids"],
        "hidden_bulk_task_total": visibility_payload["hidden_bulk_task_total"],
        "hidden_bulk_task_collections": visibility_payload[
            "hidden_bulk_task_collections"
        ],
        "query_candidate_prefilter": query_candidate_prefilter,
        "total_is_exhaustive": bool(
            query_candidate_prefilter.get("total_is_exhaustive", True)
        ),
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
    parent_task_concept_id, parent_doc = _get_task_doc(parent_task_concept_id)
    parent_metadata = parent_doc.get("metadata") or {}
    project_id = parent_metadata.get("project_concept_id")
    if project_id and organisation_concept_id is None:
        organisation_concept_id = parent_metadata.get(TASK_METADATA_KEY_ORGANISATION)
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
        project_concept_id=project_id,
        collection_concept_ids=parent_metadata.get("collection_concept_ids") or [],
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
    created_at: str | None = None,
    source: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    task_concept_id, _ = _get_task_doc(task_concept_id)
    if not isinstance(body, str) or not body.strip():
        raise InvalidTaskDataError("Comment body must be a non-empty string")

    created_at_value = _isoformat(_parse_datetime(created_at)) or _now().isoformat()
    comment = {
        "comment_id": f"comment_{uuid.uuid4().hex[:12]}",
        "body": body.strip(),
        "author_concept_id": _normalise_optional_concept_id(author_concept_id),
        "created_at": created_at_value,
    }
    if isinstance(source, Mapping):
        comment["source"] = dict(source)
    stored_entry = _append_task_metadata_entry(
        task_concept_id=task_concept_id,
        metadata_key=TASK_METADATA_KEY_COMMENTS,
        entry=comment,
    )
    if stored_entry != comment:
        return stored_entry
    try:
        _append_task_history_event(
            task_concept_id=task_concept_id,
            event_type="task_comment_added",
            actor_concept_id=author_concept_id,
            details={"comment_id": comment["comment_id"]},
            touch_updated_at=False,
            event_timestamp=created_at_value,
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


def get_task_comment(
    task_concept_id: str,
    comment_id: str,
) -> Dict[str, Any] | None:
    """Read one task comment by its stable ID without a pagination ceiling."""

    task_concept_id, doc = _get_task_doc(task_concept_id)
    cleaned_comment_id = str(comment_id or "").strip()
    if not cleaned_comment_id:
        return None
    return next(
        (
            comment
            for comment in _list_metadata_items(doc, TASK_METADATA_KEY_COMMENTS)
            if comment.get("comment_id") == cleaned_comment_id
        ),
        None,
    )


def find_task_comment_by_effect_fingerprint(
    task_concept_id: str,
    effect_fingerprint: str,
) -> Dict[str, Any] | None:
    """Find a prior actor-bound comment effect for retry reconciliation."""

    task_concept_id, doc = _get_task_doc(task_concept_id)
    cleaned_fingerprint = str(effect_fingerprint or "").strip()
    if not cleaned_fingerprint:
        return None
    for comment in _list_metadata_items(doc, TASK_METADATA_KEY_COMMENTS):
        source = comment.get("source")
        if (
            isinstance(source, Mapping)
            and source.get("effect_fingerprint") == cleaned_fingerprint
        ):
            return comment
    return None


def add_task_attachment_bytes(
    task_concept_id: str,
    *,
    data: bytes,
    filename: str,
    actor_concept_id: str,
    media_type: str | None = None,
    note: str | None = None,
) -> Dict[str, Any]:
    """Store an attachment in Von and link its verified file-copy receipt."""
    from urllib.parse import quote
    from .computer_file_copy_service import (
        import_bytes_file_copy,
        fetch_file_copy_bytes,
    )

    task_concept_id, doc = _get_task_doc(task_concept_id)
    if not actor_concept_id:
        raise InvalidTaskDataError("An authenticated actor is required")
    if not isinstance(data, bytes) or not data or len(data) > 100 * 1024 * 1024:
        raise InvalidTaskDataError("Attachment must contain 1 byte to 100 MiB")
    if not isinstance(filename, str) or not filename.strip():
        raise InvalidTaskDataError("filename is required")
    org_id = doc.get("metadata", {}).get(TASK_METADATA_KEY_ORGANISATION)
    digest = hashlib.sha256(data).hexdigest()
    scope = hashlib.sha256(
        f"{actor_concept_id}:{task_concept_id}".encode()
    ).hexdigest()[:24]
    receipt = import_bytes_file_copy(
        data=data,
        user_concept_id=actor_concept_id,
        organisation_concept_id=org_id,
        original_filename=filename.strip(),
        content_type=media_type,
        source_system="von_task_attachment",
        source_identifier=task_concept_id,
        blob_key=f"task-attachments/{scope}/{digest}",
        metadata_in_attributes=True,
        infer_typing=False,
        maintain_relationship_inverses=False,
        visibility_scope_mode="user_org_default" if org_id else "user_only_default",
    )
    if not receipt.get("success"):
        raise TaskManagementError("Attachment file was not stored")
    file_id = receipt["concept_id"]
    readback = fetch_file_copy_bytes(
        file_copy_concept_id=file_id,
        user_concept_id=actor_concept_id,
        organisation_concept_id=org_id,
        allow_large=True,
    )
    if (
        not readback.get("success")
        or hashlib.sha256(readback["data"]).hexdigest() != digest
    ):
        raise TaskManagementError("Attachment file read-back failed")
    return add_task_attachment(
        task_concept_id,
        filename=filename,
        uri=f"/von/api/files/{quote(file_id, safe='')}/download",
        media_type=media_type,
        size_bytes=len(data),
        added_by_concept_id=actor_concept_id,
        note=note,
        file_copy_concept_id=file_id,
        source={
            "source_system": "von_task_attachment",
            "external_id": digest,
            "sha256": digest,
        },
    )


def add_task_attachment(
    task_concept_id: str,
    *,
    filename: str,
    uri: str,
    media_type: str | None = None,
    size_bytes: int | None = None,
    added_by_concept_id: str | None = None,
    note: str | None = None,
    created_at: str | None = None,
    source: Mapping[str, Any] | None = None,
    file_copy_concept_id: str | None = None,
) -> Dict[str, Any]:
    task_concept_id, _ = _get_task_doc(task_concept_id)
    if not isinstance(filename, str) or not filename.strip():
        raise InvalidTaskDataError("filename is required")
    if not isinstance(uri, str) or not uri.strip():
        raise InvalidTaskDataError("uri is required")

    created_at_value = _isoformat(_parse_datetime(created_at)) or _now().isoformat()
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
        "created_at": created_at_value,
    }
    if isinstance(source, Mapping):
        attachment["source"] = dict(source)
    normalised_file_copy_concept_id = _normalise_optional_concept_id(
        file_copy_concept_id
    )
    if isinstance(normalised_file_copy_concept_id, str):
        attachment["file_copy_concept_id"] = normalised_file_copy_concept_id
    stored_entry = _append_task_metadata_entry(
        task_concept_id=task_concept_id,
        metadata_key=TASK_METADATA_KEY_ATTACHMENTS,
        entry=attachment,
    )
    if stored_entry != attachment:
        return stored_entry
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
            event_timestamp=created_at_value,
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
    created_at: str | None = None,
    source: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    task_concept_id, _ = _get_task_doc(task_concept_id)
    if not isinstance(time_spent_minutes, int) or time_spent_minutes <= 0:
        raise InvalidTaskDataError("time_spent_minutes must be a positive integer")

    started = _parse_datetime(started_at) if started_at else None
    created_at_value = _isoformat(_parse_datetime(created_at)) or _now().isoformat()
    entry = {
        "worklog_id": f"worklog_{uuid.uuid4().hex[:12]}",
        "time_spent_minutes": time_spent_minutes,
        "author_concept_id": _normalise_optional_concept_id(author_concept_id),
        "comment": (
            comment.strip() if isinstance(comment, str) and comment.strip() else None
        ),
        "started_at": _isoformat(started) or _now().isoformat(),
        "created_at": created_at_value,
    }
    if isinstance(source, Mapping):
        entry["source"] = dict(source)
    stored_entry = _append_task_metadata_entry(
        task_concept_id=task_concept_id,
        metadata_key=TASK_METADATA_KEY_WORKLOG,
        entry=entry,
    )
    if stored_entry != entry:
        return stored_entry
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
            event_timestamp=created_at_value,
        )
    except Exception as e:
        logger.debug("Failed to append worklog history event: %s", e)
    return entry


def record_task_history_event(
    task_concept_id: str,
    *,
    event_type: str,
    actor_concept_id: str | None = None,
    details: Dict[str, Any] | None = None,
    event_timestamp: str | None = None,
) -> Dict[str, Any]:
    task_concept_id, _ = _get_task_doc(task_concept_id)
    if not isinstance(event_type, str) or not event_type.strip():
        raise InvalidTaskDataError("event_type is required")
    return _append_task_history_event(
        task_concept_id=task_concept_id,
        event_type=event_type.strip(),
        actor_concept_id=actor_concept_id,
        details=details,
        event_timestamp=event_timestamp,
    )


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
    preserve_visibility: bool = False,
) -> Dict[str, Any]:
    task_concept_id = _normalise_task_concept_id(task_concept_id)
    _, task_doc = _get_task_doc(task_concept_id)

    if not isinstance(fields, dict) or not fields:
        raise InvalidTaskDataError("fields must be a non-empty dict")

    existing_task = _build_task_response(task_doc)
    membership = None
    if "project_concept_id" in fields or "collection_concept_ids" in fields:
        from .task_project_service import validate_task_membership

        project_id = fields.get(
            "project_concept_id", existing_task.get("project_concept_id")
        )
        collection_ids = fields.get(
            "collection_concept_ids", existing_task.get("collection_concept_ids", [])
        )
        if not isinstance(collection_ids, list) or any(
            not isinstance(value, str) for value in collection_ids
        ):
            raise InvalidTaskDataError(
                "collection_concept_ids must be a list of concept IDs"
            )
        try:
            _, collection_ids = validate_task_membership(project_id, collection_ids)
        except ValueError as exc:
            raise InvalidTaskDataError(str(exc)) from exc
        membership = {
            "project_concept_id": project_id,
            "collection_concept_ids": collection_ids,
        }
    # An explicit reference is validated before any other requested edits.
    product_field_present = "current_work_product_concept_id" in fields
    product_id = None
    if product_field_present:
        raw_product_id = fields["current_work_product_concept_id"]
        product_id = _normalise_optional_concept_id(raw_product_id)
        if raw_product_id not in (None, "") and not product_id:
            raise InvalidTaskDataError("Invalid current work product reference")
        if product_id and (
            not can_access_concept(product_id)
            or not ConceptsRepository.find_one(
                {"concept_id": product_id}, projection={"_id": 1}
            )
        ):
            raise InvalidTaskDataError("Current work product is not accessible")
    changed_fields: list[str] = []
    warnings: list[str] = []
    existing_task_type_ids = set(existing_task.get("task_type_ids") or [])

    organisation_field_present = "organisation_concept_id" in fields
    existing_organisation_concept_id = _normalise_optional_concept_id(
        existing_task.get("organisation_concept_id")
    )
    organisation_concept_id = existing_organisation_concept_id
    if organisation_field_present:
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
        if organisation_concept_id != existing_organisation_concept_id:
            actor_id = _normalise_optional_concept_id(actor_concept_id)
            if not actor_id:
                raise TaskOrganisationScopeAccessError(
                    "authenticated_actor_required",
                    "An authenticated actor is required to change task organisation scope",
                )
            for required_membership_org_id in dict.fromkeys(
                [existing_organisation_concept_id, organisation_concept_id]
            ):
                if not required_membership_org_id:
                    continue
                if not is_user_member_of_organisation(
                    actor_id,
                    required_membership_org_id,
                ):
                    raise TaskOrganisationScopeAccessError(
                        "organisation_membership_required",
                        "You must be a represented member of both the source and destination organisations",
                    )

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
                raise InvalidTaskDataError(
                    "due_date must be an ISO 8601 datetime string"
                )

    if next_start is not None and next_due is not None and next_start > next_due:
        raise InvalidTaskDataError("start_date must be before or equal to due_date")

    if product_field_present:
        _replace_single_relationship_target(
            task_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_CURRENT_WORK_PRODUCT,
            new_target_id=product_id,
        )
        changed_fields.append("current_work_product_concept_id")

    if "status" in fields:
        update_task_status(
            task_concept_id,
            str(fields.get("status") or ""),
            actor_concept_id=actor_concept_id,
        )
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
            if preserve_visibility:
                assign_task(task_concept_id, str(raw_assignee), update_visibility=False)
            else:
                assign_task(task_concept_id, str(raw_assignee))
            changed_fields.append("assignee_concept_id")

    created_by_field_present = (
        "created_by_concept_id" in fields or "creator_concept_id" in fields
    )
    if created_by_field_present:
        raw_created_by = fields.get(
            "created_by_concept_id", fields.get("creator_concept_id")
        )
        existing_created_by_raw = (task_doc.get("relationships") or {}).get(
            PREDICATE_HAS_CREATED_BY, []
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

    task_type_field_present = "task_type_ids" in fields or "task_type_id" in fields
    if task_type_field_present:
        raw_task_types = fields.get("task_type_ids", fields.get("task_type_id"))
        try:
            next_task_type_ids = set(normalise_task_type_ids(raw_task_types))
        except ValueError as exc:
            raise InvalidTaskDataError(str(exc)) from exc

        for existing_type_id in sorted(existing_task_type_ids - next_task_type_ids):
            ConceptsRepository.mutate_relationship_edge(
                source_id=task_concept_id,
                kind="is_an_instance_of",
                target_id=existing_type_id,
                action="remove",
                maintain_inverse=False,
            )
        for new_type_id in sorted(next_task_type_ids - existing_task_type_ids):
            ConceptsRepository.mutate_relationship_edge(
                source_id=task_concept_id,
                kind="is_an_instance_of",
                target_id=new_type_id,
                action="add",
                maintain_inverse=False,
            )
        changed_fields.append("task_type_ids")

    task_source_field_present = "task_source_id" in fields or "source_id" in fields
    if task_source_field_present:
        try:
            source_id = normalise_task_source_id(
                fields.get("task_source_id", fields.get("source_id")),
                allow_default=False,
            )
        except ValueError as exc:
            raise InvalidTaskDataError(str(exc)) from exc
        _replace_single_relationship_target(
            task_concept_id=task_concept_id,
            predicate=PREDICATE_HAS_TASK_SOURCE,
            new_target_id=source_id,
        )
        changed_fields.append("task_source_id")

    report_to_field_present = (
        "report_to_concept_id" in fields or "reports_to_concept_id" in fields
    )
    if report_to_field_present:
        report_to_raw = fields.get(
            "report_to_concept_id",
            fields.get("reports_to_concept_id"),
        )
        report_to_concept_id = _normalise_optional_concept_id(report_to_raw)
        if report_to_raw not in (None, "") and report_to_concept_id is None:
            raise InvalidTaskDataError(f"Invalid report_to_concept_id: {report_to_raw}")
        _replace_single_relationship_target(
            task_concept_id=task_concept_id,
            predicate=PREDICATE_REPORTS_TO,
            new_target_id=report_to_concept_id,
        )
        if report_to_concept_id:
            for predicate in SPECIFIC_TO_USER_PREDICATES_WRITE:
                ConceptsRepository.mutate_relationship_edge(
                    source_id=task_concept_id,
                    kind=predicate,
                    target_id=report_to_concept_id,
                    action="add",
                )
        changed_fields.append("report_to_concept_id")

    for field_name, predicate in (
        ("task_role", PREDICATE_HAS_TASK_ROLE),
        ("next_checkpoint", PREDICATE_HAS_NEXT_CHECKPOINT),
        ("progress_signal", PREDICATE_HAS_PROGRESS_SIGNAL),
        ("evidence", PREDICATE_HAS_EVIDENCE),
        ("reference_code", PREDICATE_HAS_TASK_REFERENCE_CODE),
        ("notes", "hasNote"),
    ):
        if field_name not in fields:
            continue
        value = _normalise_optional_text(fields.get(field_name), field_name=field_name)
        _upsert_optional_task_text(
            task_concept_id=task_concept_id,
            predicate=predicate,
            value=value,
            lang="en-NZ",
        )
        changed_fields.append(field_name)

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
                raise InvalidTaskDataError(
                    "due_date must be an ISO 8601 datetime string"
                )
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

    if organisation_field_present:
        if organisation_concept_id != existing_organisation_concept_id:
            actor_id = _normalise_optional_concept_id(actor_concept_id)
            assert actor_id is not None

            current_relationships = task_doc.get("relationships")
            if not isinstance(current_relationships, dict):
                current_relationships = {}
            updated_relationships = set_specific_to_org_values(
                current_relationships,
                [organisation_concept_id] if organisation_concept_id else [],
            )
            set_values: Dict[str, Any] = {
                f"metadata.{TASK_METADATA_KEY_ORGANISATION}": organisation_concept_id,
                "updated_at": _now(),
            }
            unset_values: Dict[str, str] = {}
            for predicate in SPECIFIC_TO_ORG_PREDICATES_READ:
                field_path = f"relationships.{predicate}"
                if predicate in updated_relationships:
                    set_values[field_path] = updated_relationships[predicate]
                elif predicate in current_relationships:
                    unset_values[field_path] = ""
            scope_history_event = {
                "event_id": f"event_{uuid.uuid4().hex[:12]}",
                "event_type": "task_organisation_scope_changed",
                "timestamp": _now().isoformat(),
                "actor_concept_id": actor_id,
                "details": {
                    "from_organisation_concept_id": existing_organisation_concept_id,
                    "to_organisation_concept_id": organisation_concept_id,
                },
            }
            update_document: Dict[str, Any] = {
                "$set": set_values,
                "$push": {f"metadata.{TASK_METADATA_KEY_HISTORY}": scope_history_event},
            }
            if unset_values:
                update_document["$unset"] = unset_values
            ConceptsRepository.update_one(
                {"concept_id": task_concept_id},
                update_document,
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
        if parent_raw is None or (
            isinstance(parent_raw, str) and not parent_raw.strip()
        ):
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

    if "bulk_task_collections" in fields:
        bulk_collections = normalise_bulk_task_collections(
            fields.get("bulk_task_collections")
        )
        _replace_task_metadata_list(
            task_concept_id=task_concept_id,
            metadata_key=TASK_METADATA_KEY_BULK_TASK_COLLECTIONS,
            entries=bulk_collections,
        )
        changed_fields.append("bulk_task_collections")

    unknown_keys = sorted(
        key
        for key in fields.keys()
        if key
        not in {
            "status",
            "current_work_product_concept_id",
            "assignee_concept_id",
            "assignee_id",
            "created_by_concept_id",
            "creator_concept_id",
            "title",
            "description",
            "priority",
            "task_type_ids",
            "task_type_id",
            "task_source_id",
            "source_id",
            "report_to_concept_id",
            "reports_to_concept_id",
            "task_role",
            "next_checkpoint",
            "progress_signal",
            "evidence",
            "notes",
            "reference_code",
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
            "bulk_task_collections",
            "project_concept_id",
            "collection_concept_ids",
        }
    )
    if unknown_keys:
        warnings.append(f"Ignored unsupported fields: {unknown_keys}")

    if membership is not None:
        ConceptsRepository.update_one(
            {"concept_id": task_concept_id},
            {"$set": {f"metadata.{key}": value for key, value in membership.items()}},
        )
        changed_fields.extend(membership)

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
        "$or": [
            {
                f"metadata.{TASK_METADATA_KEY_EXTERNAL_REFERENCES}.{source_key}.external_id": external_value
            },
            {
                f"metadata.{TASK_METADATA_KEY_EXTERNAL_REFERENCES}.{source_key}.previous_issue_keys": external_value
            },
        ],
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
    if source_key == "jira":
        try:
            _replace_single_relationship_target(
                task_concept_id=task_concept_id,
                predicate=PREDICATE_HAS_TASK_SOURCE,
                new_target_id=JIRA_IMPORTED_TASK_SOURCE_ID,
            )
        except Exception as exc:
            logger.debug(
                "Failed to persist Jira task-source relationship for %s: %s",
                task_concept_id,
                exc,
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


def _task_doc_task_source_id(doc: Mapping[str, Any]) -> str:
    relationships = doc.get("relationships")
    metadata = doc.get("metadata")
    if not isinstance(relationships, Mapping):
        relationships = {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    external_references = metadata.get(TASK_METADATA_KEY_EXTERNAL_REFERENCES)
    explicit_task_source = _first_relationship_value_from_aliases(
        relationships,
        TASK_SOURCE_RELATIONSHIP_PREDICATES,
    )
    return _infer_task_source_id(
        explicit_source_id=explicit_task_source,
        external_references=(
            external_references if isinstance(external_references, Mapping) else {}
        ),
    )


def _task_doc_has_migration_label(
    doc: Mapping[str, Any],
    *,
    label_candidates: set[str],
) -> bool:
    metadata = doc.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    labels = _metadata_string_list(metadata.get(TASK_METADATA_KEY_LABELS))
    label_values = {label.casefold() for label in labels}
    return bool(label_values.intersection(label_candidates))


def _task_doc_bulk_collections(doc: Mapping[str, Any]) -> list[Dict[str, Any]]:
    metadata = doc.get("metadata")
    if not isinstance(metadata, Mapping):
        return []
    return normalise_bulk_task_collections(
        metadata.get(TASK_METADATA_KEY_BULK_TASK_COLLECTIONS)
    )


def _task_doc_has_bulk_collection(
    doc: Mapping[str, Any],
    *,
    collection_id: str,
) -> bool:
    return any(
        collection.get("collection_id") == collection_id
        for collection in _task_doc_bulk_collections(doc)
    )


def backfill_jira_migration_bulk_task_collections(
    *,
    dry_run: bool = True,
    actor_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
    label_candidates: Iterable[str] | None = None,
    include_unlabelled_imported: bool = False,
    limit: int | None = None,
) -> Dict[str, Any]:
    """Preview or mark Jira-migration imported tasks as a hidden bulk collection."""

    collection = build_jira_migration_bulk_task_collection()
    collection_id = collection["collection_id"]
    normalised_label_candidates = {
        str(label).strip().casefold()
        for label in (label_candidates or JIRA_MIGRATION_LABEL_CANDIDATES)
        if str(label).strip()
    }

    query: Dict[str, Any] = {
        "relationships.is_an_instance_of": TASK_SPECIFICATION_TYPE_ID,
    }
    org_id = _normalise_optional_concept_id(organisation_concept_id)
    if organisation_concept_id and not org_id:
        raise InvalidTaskDataError(
            f"Invalid organisation_concept_id: {organisation_concept_id}"
        )
    if org_id:
        query[f"metadata.{TASK_METADATA_KEY_ORGANISATION}"] = org_id

    max_docs: int | None = None
    if limit is not None:
        try:
            max_docs = max(1, int(limit))
        except (TypeError, ValueError):
            raise InvalidTaskDataError("limit must be an integer") from None

    docs = ConceptsRepository.find(
        query,
        sort=[("updated_at", -1), ("created_at", -1), ("concept_id", 1)],
        limit=max_docs or 0,
    )

    inspected_count = 0
    imported_jira_count = 0
    candidate_count = 0
    already_marked_count = 0
    updated_count = 0
    sample_task_concept_ids: list[str] = []
    actor_id = _normalise_optional_concept_id(actor_concept_id)

    for doc in docs:
        if not isinstance(doc, Mapping):
            continue
        inspected_count += 1
        task_concept_id = _clean_metadata_string(doc.get("concept_id"))
        if not task_concept_id:
            continue
        task_source_id = _task_doc_task_source_id(doc)
        if task_source_id != JIRA_IMPORTED_TASK_SOURCE_ID:
            continue
        imported_jira_count += 1

        has_migration_label = _task_doc_has_migration_label(
            doc,
            label_candidates=normalised_label_candidates,
        )
        if not has_migration_label and not include_unlabelled_imported:
            continue

        if _task_doc_has_bulk_collection(doc, collection_id=collection_id):
            already_marked_count += 1
            continue

        candidate_count += 1
        if len(sample_task_concept_ids) < 20:
            sample_task_concept_ids.append(task_concept_id)

        if dry_run:
            continue

        next_collections = _task_doc_bulk_collections(doc) + [collection]
        ConceptsRepository.update_one(
            {"concept_id": task_concept_id},
            {
                "$set": {
                    f"metadata.{TASK_METADATA_KEY_BULK_TASK_COLLECTIONS}": (
                        next_collections
                    ),
                    "updated_at": _now(),
                }
            },
        )
        updated_count += 1
        try:
            _append_task_history_event(
                task_concept_id=task_concept_id,
                event_type="task_bulk_collection_marked",
                actor_concept_id=actor_id,
                details={
                    "collection_id": collection_id,
                    "reason": JIRA_MIGRATION_BULK_COLLECTION_REASON,
                },
                touch_updated_at=False,
            )
        except Exception as exc:
            logger.debug(
                "Failed to append bulk-collection backfill history for %s: %s",
                task_concept_id,
                exc,
            )

    return {
        "success": True,
        "dry_run": bool(dry_run),
        "collection": collection,
        "selection": {
            "task_source_id": JIRA_IMPORTED_TASK_SOURCE_ID,
            "label_candidates": sorted(normalised_label_candidates),
            "include_unlabelled_imported": bool(include_unlabelled_imported),
            "organisation_concept_id": org_id,
            "limit": max_docs,
        },
        "inspected_count": inspected_count,
        "imported_jira_count": imported_jira_count,
        "candidate_count": candidate_count,
        "already_marked_count": already_marked_count,
        "updated_count": updated_count,
        "sample_task_concept_ids": sample_task_concept_ids,
    }


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


def get_task_taxonomy() -> Dict[str, Any]:
    """Return the static task taxonomy without mutating or repairing storage."""
    return get_task_taxonomy_definition()


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
    "TASK_METADATA_KEY_BULK_TASK_COLLECTIONS",
    "BULK_TASK_VISIBILITY_EXCLUDE",
    "BULK_TASK_VISIBILITY_INCLUDE",
    "BULK_TASK_VISIBILITY_ONLY",
    "JIRA_MIGRATION_BULK_COLLECTION_ID",
    "JIRA_MIGRATION_BULK_COLLECTION_KIND",
    "JIRA_MIGRATION_BULK_COLLECTION_LABEL",
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
    "list_tasks_with_visibility",
    "search_tasks",
    "get_task_taxonomy",
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
    "record_task_history_event",
    "update_task_fields",
    "find_task_by_external_reference",
    "upsert_task_external_reference",
    "build_jira_migration_bulk_task_collection",
    "normalise_bulk_task_collections",
    "apply_bulk_task_visibility",
    "backfill_jira_migration_bulk_task_collections",
    "bulk_update_tasks",
    "delete_task",
]
