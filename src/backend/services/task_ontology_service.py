"""Canonical task taxonomy and supporting task-view ontology helpers.

This module centralises the task category/source vocabulary used by task
creation, Jira imports, MCP tools, and the task board UI.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterable

from ..db.repositories.concepts_repository import ConceptsRepository
from . import concept_service

logger = logging.getLogger(__name__)

TASK_SPECIFICATION_TYPE_ID = "#V#task_specification"
TASK_SOURCE_TYPE_ID = "#V#task_source"

# Keep these predicate IDs in canonical slug form. ``create_concept()`` lowercases
# concept IDs during canonicalisation, so mixed-case identifiers would bootstrap
# under a different persisted ID than the one later used for relationship writes.
PREDICATE_HAS_CURRENT_WORK_PRODUCT = "#V#hascurrentworkproduct"
PREDICATE_HAS_TASK_SOURCE = "#V#hastasksource"
PREDICATE_REPORTS_TO = "#V#reportsto"
PREDICATE_HAS_TASK_ROLE = "#V#hastaskrole"
PREDICATE_HAS_NEXT_CHECKPOINT = "#V#hasnextcheckpoint"
PREDICATE_HAS_PROGRESS_SIGNAL = "#V#hasprogresssignal"
PREDICATE_HAS_EVIDENCE = "#V#hasevidence"
PREDICATE_HAS_TASK_REFERENCE_CODE = "#V#hastaskreferencecode"

LEGACY_PREDICATE_HAS_TASK_SOURCE = "#V#hasTaskSource"
LEGACY_PREDICATE_REPORTS_TO = "#V#reportsTo"
LEGACY_PREDICATE_HAS_TASK_ROLE = "#V#hasTaskRole"
LEGACY_PREDICATE_HAS_NEXT_CHECKPOINT = "#V#hasNextCheckpoint"
LEGACY_PREDICATE_HAS_PROGRESS_SIGNAL = "#V#hasProgressSignal"
LEGACY_PREDICATE_HAS_EVIDENCE = "#V#hasEvidence"
LEGACY_PREDICATE_HAS_TASK_REFERENCE_CODE = "#V#hasTaskReferenceCode"

TASK_SOURCE_RELATIONSHIP_PREDICATES: tuple[str, ...] = (
    PREDICATE_HAS_TASK_SOURCE,
    LEGACY_PREDICATE_HAS_TASK_SOURCE,
)
REPORTS_TO_RELATIONSHIP_PREDICATES: tuple[str, ...] = (
    PREDICATE_REPORTS_TO,
    LEGACY_PREDICATE_REPORTS_TO,
)
TASK_ROLE_TEXT_PREDICATES: tuple[str, ...] = (
    PREDICATE_HAS_TASK_ROLE,
    LEGACY_PREDICATE_HAS_TASK_ROLE,
    "hasTaskRole",
)
NEXT_CHECKPOINT_TEXT_PREDICATES: tuple[str, ...] = (
    PREDICATE_HAS_NEXT_CHECKPOINT,
    LEGACY_PREDICATE_HAS_NEXT_CHECKPOINT,
    "hasNextCheckpoint",
)
PROGRESS_SIGNAL_TEXT_PREDICATES: tuple[str, ...] = (
    PREDICATE_HAS_PROGRESS_SIGNAL,
    LEGACY_PREDICATE_HAS_PROGRESS_SIGNAL,
    "hasProgressSignal",
)
EVIDENCE_TEXT_PREDICATES: tuple[str, ...] = (
    PREDICATE_HAS_EVIDENCE,
    LEGACY_PREDICATE_HAS_EVIDENCE,
    "hasEvidence",
)
TASK_REFERENCE_CODE_TEXT_PREDICATES: tuple[str, ...] = (
    PREDICATE_HAS_TASK_REFERENCE_CODE,
    LEGACY_PREDICATE_HAS_TASK_REFERENCE_CODE,
    "hasTaskReferenceCode",
)

TASK_TYPE_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "concept_id": "#V#one_off_task_specification",
        "slug": "one_off",
        "label": "One-off",
        "description": (
            "A single bounded action or deliverable that does not repeat on a cadence."
        ),
    },
    {
        "concept_id": "#V#recurring_task_specification",
        "slug": "recurring",
        "label": "Recurring",
        "description": (
            "A task specification that repeats on a schedule or recurring trigger."
        ),
    },
    {
        "concept_id": "#V#delegated_task_specification",
        "slug": "delegated",
        "label": "Delegated",
        "description": (
            "A task whose progress depends primarily on another person or team acting."
        ),
    },
    {
        "concept_id": "#V#approval_or_confirmation_task_specification",
        "slug": "approval_confirmation",
        "label": "Approval / Confirmation",
        "description": (
            "A task centred on obtaining approval, sign-off, or explicit confirmation."
        ),
    },
    {
        "concept_id": "#V#tracking_or_monitoring_task_specification",
        "slug": "tracking_monitoring",
        "label": "Tracking / Monitoring",
        "description": (
            "A task focused on observation, follow-up, or monitoring until a condition is met."
        ),
    },
    {
        "concept_id": "#V#reference_or_archive_task_specification",
        "slug": "reference_archive",
        "label": "Reference / Archive",
        "description": (
            "A task retained mainly for reference, record-keeping, or archival purposes."
        ),
    },
)

TASK_SOURCE_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "concept_id": "#V#von_native_task_source",
        "slug": "von_native",
        "label": "Von native",
        "description": (
            "A task created directly inside Von rather than imported from an external system."
        ),
    },
    {
        "concept_id": "#V#jira_imported_task_source",
        "slug": "jira_imported",
        "label": "Jira migrated",
        "description": (
            "A task whose authoritative provenance came from a Jira issue import or backfill."
        ),
    },
)

DEFAULT_TASK_TYPE_ID = TASK_TYPE_DEFINITIONS[0]["concept_id"]
DEFAULT_TASK_SOURCE_ID = TASK_SOURCE_DEFINITIONS[0]["concept_id"]
JIRA_IMPORTED_TASK_SOURCE_ID = TASK_SOURCE_DEFINITIONS[1]["concept_id"]

_TASK_TYPE_IDS = {item["concept_id"] for item in TASK_TYPE_DEFINITIONS}
_TASK_TYPE_BY_ID = {item["concept_id"]: dict(item) for item in TASK_TYPE_DEFINITIONS}
_TASK_TYPE_BY_SLUG = {item["slug"]: item["concept_id"] for item in TASK_TYPE_DEFINITIONS}

_TASK_SOURCE_IDS = {item["concept_id"] for item in TASK_SOURCE_DEFINITIONS}
_TASK_SOURCE_BY_ID = {item["concept_id"]: dict(item) for item in TASK_SOURCE_DEFINITIONS}
_TASK_SOURCE_BY_SLUG = {
    item["slug"]: item["concept_id"] for item in TASK_SOURCE_DEFINITIONS
}
_TASK_SOURCE_BY_SLUG.update(
    {
        "jira": JIRA_IMPORTED_TASK_SOURCE_ID,
        "imported_jira": JIRA_IMPORTED_TASK_SOURCE_ID,
        "imported": JIRA_IMPORTED_TASK_SOURCE_ID,
        "native": DEFAULT_TASK_SOURCE_ID,
        "von": DEFAULT_TASK_SOURCE_ID,
    }
)

_PREDICATE_PARENT_CANDIDATES: tuple[str, ...] = ("#V#binary_predicate", "#V#predicate")

_PREDICATE_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        PREDICATE_HAS_CURRENT_WORK_PRODUCT,
        "has current work product",
        "Links a task to its explicitly selected current work product. The product owns its content and revision; the link grants no access or execution authority.",
    ),
    (
        PREDICATE_HAS_TASK_SOURCE,
        "has task source",
        "Links a task specification to the source classification that introduced it.",
    ),
    (
        PREDICATE_REPORTS_TO,
        "reports to",
        "Links a task specification to the person or role it should report to.",
    ),
    (
        PREDICATE_HAS_TASK_ROLE,
        "has task role",
        "Stores the role or responsibility framing associated with a task.",
    ),
    (
        PREDICATE_HAS_NEXT_CHECKPOINT,
        "has next checkpoint",
        "Stores the next checkpoint, reminder, or checkpoint note for a task.",
    ),
    (
        PREDICATE_HAS_PROGRESS_SIGNAL,
        "has progress signal",
        "Stores how progress or confirmation should be recognised for a task.",
    ),
    (
        PREDICATE_HAS_EVIDENCE,
        "has evidence",
        "Stores evidence expectations or supporting artefact summaries for a task.",
    ),
    (
        PREDICATE_HAS_TASK_REFERENCE_CODE,
        "has task reference code",
        "Stores a compact reference code or external-facing task number.",
    ),
)

_task_ontology_ensured = False
_task_ontology_lock = threading.Lock()


def _safe_get_concept(concept_id: str) -> dict[str, Any] | None:
    try:
        return concept_service.get_concept_by_concept_id(concept_id)
    except Exception:
        return None


def _dedupe_ids(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        concept_id = value.strip()
        if not concept_id or concept_id in seen:
            continue
        seen.add(concept_id)
        ordered.append(concept_id)
    return ordered


def _resolve_predicate_parent_ids() -> list[str]:
    parents = [
        concept_id
        for concept_id in _PREDICATE_PARENT_CANDIDATES
        if _safe_get_concept(concept_id) is not None
    ]
    return parents or ["#V#predicate"]


def _ensure_type_concept(
    *,
    concept_id: str,
    label: str,
    description: str,
    parent_id: str,
) -> tuple[bool, bool, str | None]:
    created = False
    changed = False
    doc = _safe_get_concept(concept_id)

    if doc is None:
        concept_service.create_concept(
            name=label,
            concept_id=concept_id,
            parent_concept_ids=[parent_id],
            create_as_instance=False,
            description=description,
        )
        created = True
        changed = True

    try:
        edge_changed = ConceptsRepository.mutate_relationship_edge(
            source_id=concept_id,
            kind="is_a_type_of",
            target_id=parent_id,
            action="add",
        )
        changed = changed or bool(edge_changed)
    except Exception as exc:
        return created, changed, str(exc)

    return created, changed, None


def _ensure_instance_concept(
    *,
    concept_id: str,
    label: str,
    description: str,
    instance_of_type: str,
) -> tuple[bool, bool, str | None]:
    created = False
    changed = False
    doc = _safe_get_concept(concept_id)

    if doc is None:
        concept_service.create_concept(
            name=label,
            concept_id=concept_id,
            parent_concept_ids=[instance_of_type],
            create_as_instance=True,
            description=description,
        )
        created = True
        changed = True

    try:
        edge_changed = ConceptsRepository.mutate_relationship_edge(
            source_id=concept_id,
            kind="is_an_instance_of",
            target_id=instance_of_type,
            action="add",
        )
        changed = changed or bool(edge_changed)
    except Exception as exc:
        return created, changed, str(exc)

    return created, changed, None


def _ensure_predicate_concept(
    *,
    predicate_id: str,
    label: str,
    description: str,
) -> tuple[bool, bool, str | None]:
    created = False
    changed = False
    doc = _safe_get_concept(predicate_id)
    parent_ids = _resolve_predicate_parent_ids()

    if doc is None:
        concept_service.create_concept(
            name=label,
            concept_id=predicate_id,
            parent_concept_ids=parent_ids,
            create_as_instance=True,
            description=description,
        )
        created = True
        changed = True

    for parent_id in parent_ids:
        try:
            edge_changed = ConceptsRepository.mutate_relationship_edge(
                source_id=predicate_id,
                kind="is_an_instance_of",
                target_id=parent_id,
                action="add",
            )
            changed = changed or bool(edge_changed)
        except Exception as exc:
            return created, changed, str(exc)

    return created, changed, None


def ensure_task_ontology(*, force: bool = False) -> dict[str, Any]:
    """Ensure canonical task category/source concepts and predicates exist."""

    global _task_ontology_ensured

    if _task_ontology_ensured and not force:
        return {
            "cached": True,
            "success": True,
            "created_concept_ids": [],
            "updated_concept_ids": [],
            "errors": [],
        }

    with _task_ontology_lock:
        if _task_ontology_ensured and not force:
            return {
                "cached": True,
                "success": True,
                "created_concept_ids": [],
                "updated_concept_ids": [],
                "errors": [],
            }

        created_concept_ids: list[str] = []
        updated_concept_ids: list[str] = []
        errors: list[str] = []

        try:
            created, changed, error = _ensure_type_concept(
                concept_id=TASK_SOURCE_TYPE_ID,
                label="Task Source",
                description=(
                    "A classification concept for the source system or origin surface of a task."
                ),
                parent_id="#V#thing",
            )
            if created:
                created_concept_ids.append(TASK_SOURCE_TYPE_ID)
            if changed and not created:
                updated_concept_ids.append(TASK_SOURCE_TYPE_ID)
            if error:
                errors.append(f"{TASK_SOURCE_TYPE_ID}:{error}")
        except Exception as exc:
            errors.append(f"{TASK_SOURCE_TYPE_ID}:{exc}")

        for definition in TASK_TYPE_DEFINITIONS:
            try:
                created, changed, error = _ensure_type_concept(
                    concept_id=definition["concept_id"],
                    label=definition["label"],
                    description=definition["description"],
                    parent_id=TASK_SPECIFICATION_TYPE_ID,
                )
                if created:
                    created_concept_ids.append(definition["concept_id"])
                if changed and not created:
                    updated_concept_ids.append(definition["concept_id"])
                if error:
                    errors.append(f"{definition['concept_id']}:{error}")
            except Exception as exc:
                errors.append(f"{definition['concept_id']}:{exc}")

        for definition in TASK_SOURCE_DEFINITIONS:
            try:
                created, changed, error = _ensure_instance_concept(
                    concept_id=definition["concept_id"],
                    label=definition["label"],
                    description=definition["description"],
                    instance_of_type=TASK_SOURCE_TYPE_ID,
                )
                if created:
                    created_concept_ids.append(definition["concept_id"])
                if changed and not created:
                    updated_concept_ids.append(definition["concept_id"])
                if error:
                    errors.append(f"{definition['concept_id']}:{error}")
            except Exception as exc:
                errors.append(f"{definition['concept_id']}:{exc}")

        for predicate_id, label, description in _PREDICATE_SPECS:
            try:
                created, changed, error = _ensure_predicate_concept(
                    predicate_id=predicate_id,
                    label=label,
                    description=description,
                )
                if created:
                    created_concept_ids.append(predicate_id)
                if changed and not created:
                    updated_concept_ids.append(predicate_id)
                if error:
                    errors.append(f"{predicate_id}:{error}")
            except Exception as exc:
                errors.append(f"{predicate_id}:{exc}")

        success = len(errors) == 0
        if success:
            _task_ontology_ensured = True
        else:
            logger.warning("ensure_task_ontology encountered issues: %s", errors)

        return {
            "cached": False,
            "success": success,
            "created_concept_ids": created_concept_ids,
            "updated_concept_ids": updated_concept_ids,
            "errors": errors,
        }


def normalise_task_type_ids(
    raw_values: Any,
    *,
    allow_default: bool = False,
) -> list[str]:
    values: list[str]
    if raw_values is None:
        values = []
    elif isinstance(raw_values, str):
        values = [raw_values]
    elif isinstance(raw_values, list):
        values = [value for value in raw_values if isinstance(value, str)]
    else:
        raise ValueError("task_type_ids must be a string, list, or null")

    normalised: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        resolved = _TASK_TYPE_BY_SLUG.get(cleaned.lower()) or cleaned
        if resolved not in _TASK_TYPE_IDS:
            raise ValueError(f"Unknown task type: {cleaned}")
        if resolved not in normalised:
            normalised.append(resolved)

    if not normalised and allow_default:
        return [DEFAULT_TASK_TYPE_ID]
    return normalised


def normalise_task_source_id(
    raw_value: Any,
    *,
    allow_default: bool = False,
) -> str | None:
    if raw_value is None:
        return DEFAULT_TASK_SOURCE_ID if allow_default else None
    if not isinstance(raw_value, str):
        raise ValueError("task_source_id must be a string or null")

    cleaned = raw_value.strip()
    if not cleaned:
        return DEFAULT_TASK_SOURCE_ID if allow_default else None

    resolved = _TASK_SOURCE_BY_SLUG.get(cleaned.lower()) or cleaned
    if resolved not in _TASK_SOURCE_IDS:
        raise ValueError(f"Unknown task source: {cleaned}")
    return resolved


def is_known_task_type_id(concept_id: Any) -> bool:
    return isinstance(concept_id, str) and concept_id.strip() in _TASK_TYPE_IDS


def is_known_task_source_id(concept_id: Any) -> bool:
    return isinstance(concept_id, str) and concept_id.strip() in _TASK_SOURCE_IDS


def get_task_type_definition(concept_id: str) -> dict[str, str] | None:
    entry = _TASK_TYPE_BY_ID.get(str(concept_id or "").strip())
    return dict(entry) if isinstance(entry, dict) else None


def get_task_source_definition(concept_id: str) -> dict[str, str] | None:
    entry = _TASK_SOURCE_BY_ID.get(str(concept_id or "").strip())
    return dict(entry) if isinstance(entry, dict) else None


def get_task_taxonomy() -> dict[str, Any]:
    return {
        "task_types": [dict(item) for item in TASK_TYPE_DEFINITIONS],
        "task_sources": [dict(item) for item in TASK_SOURCE_DEFINITIONS],
        "defaults": {
            "task_type_id": DEFAULT_TASK_TYPE_ID,
            "task_source_id": DEFAULT_TASK_SOURCE_ID,
        },
    }


__all__ = [
    "DEFAULT_TASK_SOURCE_ID",
    "DEFAULT_TASK_TYPE_ID",
    "EVIDENCE_TEXT_PREDICATES",
    "JIRA_IMPORTED_TASK_SOURCE_ID",
    "NEXT_CHECKPOINT_TEXT_PREDICATES",
    "PREDICATE_HAS_EVIDENCE",
    "PREDICATE_HAS_NEXT_CHECKPOINT",
    "PREDICATE_HAS_PROGRESS_SIGNAL",
    "PREDICATE_HAS_TASK_REFERENCE_CODE",
    "PREDICATE_HAS_TASK_ROLE",
    "PREDICATE_HAS_TASK_SOURCE",
    "PREDICATE_REPORTS_TO",
    "PROGRESS_SIGNAL_TEXT_PREDICATES",
    "REPORTS_TO_RELATIONSHIP_PREDICATES",
    "TASK_SOURCE_DEFINITIONS",
    "TASK_SOURCE_RELATIONSHIP_PREDICATES",
    "TASK_SOURCE_TYPE_ID",
    "TASK_REFERENCE_CODE_TEXT_PREDICATES",
    "TASK_ROLE_TEXT_PREDICATES",
    "TASK_TYPE_DEFINITIONS",
    "ensure_task_ontology",
    "get_task_source_definition",
    "get_task_taxonomy",
    "get_task_type_definition",
    "is_known_task_source_id",
    "is_known_task_type_id",
    "normalise_task_source_id",
    "normalise_task_type_ids",
]
