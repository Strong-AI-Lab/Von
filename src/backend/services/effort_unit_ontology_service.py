"""Effort-unit ontology bootstrap and lifecycle helpers.

This module centralises the canonical effort-unit model so task/workflow
pathways can use one consistent representation.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterable

from ..db.repositories.concepts_repository import ConceptsRepository
from . import concept_service

logger = logging.getLogger(__name__)


EFFORT_UNIT_TYPE_ID = "#V#effort_unit"
EFFORT_UNIT_PREFERRED_PARENT_IDS: tuple[str, ...] = (
    "#V#practice",
    "#V#conceptual_work",
    "#V#thing",
)

# Canonical predicates for effort-unit modelling.
PREDICATE_HAS_EFFORT_UNIT_GOAL = "#V#has_effort_unit_goal"
PREDICATE_COMPLETION_TRIGGERS_SUCCESSOR_EFFORT_UNIT_TYPE = (
    "#V#completion_triggers_successor_effort_unit_type"
)
PREDICATE_HAS_SUCCESSOR_EFFORT_UNIT_TYPE = "#V#has_successor_effort_unit_type"
PREDICATE_COMPLETION_PRODUCES_SUPPORT_OBJECT = "#V#completion_produces_support_object"

EFFORT_UNIT_ALIGNMENT_TYPE_IDS: tuple[str, ...] = (
    "#V#project",
    "#V#programme",
    "#V#task_specification",
    "#V#ai_workflow",
    "#V#durable_workflow",
)

_PREDICATE_PARENT_CANDIDATES: tuple[str, ...] = ("#V#binary_predicate", "#V#predicate")

_EFFORT_UNIT_PREDICATE_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        PREDICATE_HAS_EFFORT_UNIT_GOAL,
        "has effort unit goal",
        "Links an effort unit to a goal concept that motivates or constrains it.",
    ),
    (
        PREDICATE_COMPLETION_TRIGGERS_SUCCESSOR_EFFORT_UNIT_TYPE,
        "completion triggers successor effort unit type",
        (
            "Type-level lifecycle relation indicating which effort-unit type is "
            "typically triggered when this effort-unit type is completed."
        ),
    ),
    (
        PREDICATE_HAS_SUCCESSOR_EFFORT_UNIT_TYPE,
        "has successor effort unit type",
        (
            "Instance-level persisted linkage from a completed effort unit to "
            "successor effort-unit types implied by lifecycle semantics."
        ),
    ),
    (
        PREDICATE_COMPLETION_PRODUCES_SUPPORT_OBJECT,
        "completion produces support object",
        (
            "Links an effort unit (or effort-unit type) to support artefacts "
            "typically produced on completion."
        ),
    ),
)


_effort_unit_ontology_ensured = False
_effort_unit_ontology_lock = threading.Lock()


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


def _relationship_ids(relationships: dict[str, Any], key: str) -> list[str]:
    raw = relationships.get(key)
    if isinstance(raw, str):
        return _dedupe_ids([raw])
    if isinstance(raw, list):
        return _dedupe_ids(raw)
    return []


def _resolve_effort_unit_parent_id() -> str:
    for concept_id in EFFORT_UNIT_PREFERRED_PARENT_IDS:
        if _safe_get_concept(concept_id) is not None:
            return concept_id
    return "#V#thing"


def _resolve_predicate_parent_ids() -> list[str]:
    parents = [
        concept_id
        for concept_id in _PREDICATE_PARENT_CANDIDATES
        if _safe_get_concept(concept_id) is not None
    ]
    return parents or ["#V#predicate"]


def _ensure_effort_unit_type() -> tuple[bool, str | None]:
    created = False
    effort_unit_doc = _safe_get_concept(EFFORT_UNIT_TYPE_ID)
    parent_id = _resolve_effort_unit_parent_id()

    if effort_unit_doc is None:
        concept_service.create_concept(
            name="Effort Unit",
            concept_id=EFFORT_UNIT_TYPE_ID,
            parent_concept_ids=[parent_id],
            create_as_instance=False,
            description=(
                "A reusable conceptual category for purposeful work units, such as "
                "projects, programmes, task specifications, and workflows."
            ),
            notes=(
                "Used as a cross-cutting classification for effort modelling. "
                "Member types retain their existing domain-specific hierarchies."
            ),
        )
        created = True

    try:
        ConceptsRepository.mutate_relationship_edge(
            source_id=EFFORT_UNIT_TYPE_ID,
            kind="is_a_type_of",
            target_id=parent_id,
            action="add",
        )
    except Exception as exc:
        return created, str(exc)

    return created, None


def _ensure_effort_unit_predicate(
    *,
    predicate_id: str,
    display_name: str,
    description: str,
) -> tuple[bool, bool, str | None]:
    created = False
    changed = False
    predicate_doc = _safe_get_concept(predicate_id)
    parent_ids = _resolve_predicate_parent_ids()

    if predicate_doc is None:
        concept_service.create_concept(
            name=display_name,
            concept_id=predicate_id,
            parent_concept_ids=parent_ids,
            create_as_instance=True,
            description=description,
        )
        created = True
        changed = True

    for parent_id in parent_ids:
        try:
            parent_changed = ConceptsRepository.mutate_relationship_edge(
                source_id=predicate_id,
                kind="is_an_instance_of",
                target_id=parent_id,
                action="add",
            )
        except Exception as exc:
            return created, changed, str(exc)
        changed = changed or bool(parent_changed)

    return created, changed, None


def align_effort_unit_type_classification(
    *,
    target_type_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Align canonical effort-like types as instances of ``#V#effort_unit``."""

    if _safe_get_concept(EFFORT_UNIT_TYPE_ID) is None:
        return {
            "success": False,
            "updated_type_ids": [],
            "unchanged_type_ids": [],
            "skipped_missing_type_ids": [],
            "errors": ["missing_effort_unit_type"],
        }

    updated: list[str] = []
    unchanged: list[str] = []
    skipped_missing: list[str] = []
    errors: list[str] = []
    for type_id in _dedupe_ids(target_type_ids or EFFORT_UNIT_ALIGNMENT_TYPE_IDS):
        if _safe_get_concept(type_id) is None:
            skipped_missing.append(type_id)
            continue
        try:
            changed = ConceptsRepository.mutate_relationship_edge(
                source_id=type_id,
                kind="is_an_instance_of",
                target_id=EFFORT_UNIT_TYPE_ID,
                action="add",
            )
        except Exception as exc:
            errors.append(f"{type_id}:{exc}")
            continue
        if changed:
            updated.append(type_id)
        else:
            unchanged.append(type_id)

    return {
        "success": len(errors) == 0,
        "updated_type_ids": updated,
        "unchanged_type_ids": unchanged,
        "skipped_missing_type_ids": skipped_missing,
        "errors": errors,
    }


def ensure_effort_unit_ontology(*, force: bool = False) -> dict[str, Any]:
    """Ensure canonical effort-unit type/predicates and type alignment exist."""

    global _effort_unit_ontology_ensured

    if _effort_unit_ontology_ensured and not force:
        return {
            "cached": True,
            "success": True,
            "created_concept_ids": [],
            "updated_concept_ids": [],
            "alignment": {
                "success": True,
                "updated_type_ids": [],
                "unchanged_type_ids": [],
                "skipped_missing_type_ids": [],
                "errors": [],
            },
            "errors": [],
        }

    with _effort_unit_ontology_lock:
        if _effort_unit_ontology_ensured and not force:
            return {
                "cached": True,
                "success": True,
                "created_concept_ids": [],
                "updated_concept_ids": [],
                "alignment": {
                    "success": True,
                    "updated_type_ids": [],
                    "unchanged_type_ids": [],
                    "skipped_missing_type_ids": [],
                    "errors": [],
                },
                "errors": [],
            }

        created_concept_ids: list[str] = []
        updated_concept_ids: list[str] = []
        errors: list[str] = []

        try:
            type_created, type_error = _ensure_effort_unit_type()
            if type_created:
                created_concept_ids.append(EFFORT_UNIT_TYPE_ID)
            if type_error:
                errors.append(f"{EFFORT_UNIT_TYPE_ID}:{type_error}")
        except Exception as exc:
            errors.append(f"{EFFORT_UNIT_TYPE_ID}:{exc}")

        for predicate_id, display_name, description in _EFFORT_UNIT_PREDICATE_SPECS:
            try:
                created, changed, predicate_error = _ensure_effort_unit_predicate(
                    predicate_id=predicate_id,
                    display_name=display_name,
                    description=description,
                )
            except Exception as exc:
                errors.append(f"{predicate_id}:{exc}")
                continue

            if created:
                created_concept_ids.append(predicate_id)
            if changed and not created:
                updated_concept_ids.append(predicate_id)
            if predicate_error:
                errors.append(f"{predicate_id}:{predicate_error}")

        alignment_report = align_effort_unit_type_classification()
        if not alignment_report.get("success"):
            errors.extend(alignment_report.get("errors") or [])

        success = len(errors) == 0
        if success:
            _effort_unit_ontology_ensured = True
        else:
            logger.warning(
                "ensure_effort_unit_ontology encountered issues: %s",
                errors,
            )

        return {
            "cached": False,
            "success": success,
            "created_concept_ids": created_concept_ids,
            "updated_concept_ids": updated_concept_ids,
            "alignment": alignment_report,
            "errors": errors,
        }


def extract_effort_unit_type_ids(effort_unit_doc: dict[str, Any]) -> list[str]:
    relationships = effort_unit_doc.get("relationships") or {}
    if not isinstance(relationships, dict):
        return []
    return _relationship_ids(relationships, "is_an_instance_of")


def resolve_successor_effort_unit_type_ids(
    *,
    effort_unit_doc: dict[str, Any],
) -> list[str]:
    """Resolve successor effort-unit type IDs from instance+type relationships."""

    relationships = effort_unit_doc.get("relationships") or {}
    if not isinstance(relationships, dict):
        return []

    direct_successors = _relationship_ids(
        relationships,
        PREDICATE_COMPLETION_TRIGGERS_SUCCESSOR_EFFORT_UNIT_TYPE,
    )
    inherited_successors: list[str] = []
    for type_id in extract_effort_unit_type_ids(effort_unit_doc):
        type_doc = _safe_get_concept(type_id)
        if type_doc is None:
            continue
        type_rels = type_doc.get("relationships") or {}
        if not isinstance(type_rels, dict):
            continue
        inherited_successors.extend(
            _relationship_ids(
                type_rels,
                PREDICATE_COMPLETION_TRIGGERS_SUCCESSOR_EFFORT_UNIT_TYPE,
            )
        )

    return _dedupe_ids([*direct_successors, *inherited_successors])


def persist_successor_effort_unit_type_links(
    *,
    source_effort_unit_id: str,
    successor_type_ids: Iterable[str],
) -> dict[str, Any]:
    """Persist completion-time successor-type linkage for one effort unit."""

    source_id = str(source_effort_unit_id or "").strip()
    if not source_id:
        return {
            "success": False,
            "source_effort_unit_id": source_effort_unit_id,
            "linked_type_ids": [],
            "already_linked_type_ids": [],
            "skipped_missing_target_type_ids": [],
            "errors": ["missing_source_effort_unit_id"],
        }

    linked: list[str] = []
    already_linked: list[str] = []
    skipped_missing: list[str] = []
    errors: list[str] = []

    for successor_type_id in _dedupe_ids(successor_type_ids):
        if _safe_get_concept(successor_type_id) is None:
            skipped_missing.append(successor_type_id)
            continue
        try:
            changed = ConceptsRepository.mutate_relationship_edge(
                source_id=source_id,
                kind=PREDICATE_HAS_SUCCESSOR_EFFORT_UNIT_TYPE,
                target_id=successor_type_id,
                action="add",
                maintain_inverse=False,
            )
        except Exception as exc:
            errors.append(f"{successor_type_id}:{exc}")
            continue

        if changed:
            linked.append(successor_type_id)
        else:
            already_linked.append(successor_type_id)

    return {
        "success": len(errors) == 0,
        "source_effort_unit_id": source_id,
        "linked_type_ids": linked,
        "already_linked_type_ids": already_linked,
        "skipped_missing_target_type_ids": skipped_missing,
        "errors": errors,
    }


__all__ = [
    "EFFORT_UNIT_ALIGNMENT_TYPE_IDS",
    "EFFORT_UNIT_TYPE_ID",
    "PREDICATE_COMPLETION_PRODUCES_SUPPORT_OBJECT",
    "PREDICATE_COMPLETION_TRIGGERS_SUCCESSOR_EFFORT_UNIT_TYPE",
    "PREDICATE_HAS_EFFORT_UNIT_GOAL",
    "PREDICATE_HAS_SUCCESSOR_EFFORT_UNIT_TYPE",
    "align_effort_unit_type_classification",
    "ensure_effort_unit_ontology",
    "extract_effort_unit_type_ids",
    "persist_successor_effort_unit_type_links",
    "resolve_successor_effort_unit_type_ids",
]
