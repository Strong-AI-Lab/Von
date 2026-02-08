"""Workflow concept authority helpers.

WS1 requires Vontology to be authoritative for workflow identity.  This module
provides one canonical pathway to:
1. bootstrap missing workflow concepts for registered workflows, and
2. classify workflow concept authority drift for parity diagnostics.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from ..services import concept_service
from ..services.concept_service import ConceptNotFoundError
from .workflow_registry import WorkflowRegistry

logger = logging.getLogger(__name__)


# Ordered from preferred canonical type to legacy fallbacks.
# Keep all candidates here so workflow typing policy is managed in one place.
WORKFLOW_INSTANCE_TYPE_ID_CANDIDATES: tuple[str, ...] = (
    "#V#ai_workflow",
    "#V#durable_workflow",
    "#V#workflow",
    "#V#llm_workflow",
)


def _titleise_workflow_id(workflow_id: str) -> str:
    slug = workflow_id[3:] if workflow_id.startswith("#V#") else workflow_id
    words = [part for part in slug.replace("-", "_").split("_") if part]
    if not words:
        return workflow_id
    return " ".join(word.capitalize() for word in words)


def _extract_instance_of(concept_doc: Dict[str, Any]) -> List[str]:
    relationships = concept_doc.get("relationships") or {}
    if not isinstance(relationships, dict):
        return []
    raw_values = relationships.get("is_an_instance_of", [])
    if isinstance(raw_values, str):
        return [raw_values] if raw_values else []
    if isinstance(raw_values, list):
        return [item for item in raw_values if isinstance(item, str) and item]
    return []


def _load_concept(concept_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return concept_service.get_concept_by_concept_id(concept_id), None
    except ConceptNotFoundError:
        return None, None
    except Exception as exc:  # pragma: no cover - defensive
        return None, str(exc)


@lru_cache(maxsize=1)
def resolve_available_workflow_type_ids() -> tuple[str, ...]:
    """Return existing workflow type concepts in canonical preference order."""
    available: list[str] = []
    for type_id in WORKFLOW_INSTANCE_TYPE_ID_CANDIDATES:
        concept_doc, error = _load_concept(type_id)
        if error:
            logger.warning(
                "workflow_authority: failed to inspect type %s: %s",
                type_id,
                error,
            )
            continue
        if concept_doc is not None:
            available.append(type_id)

    if available:
        return tuple(available)

    # Fall back to the preferred canonical candidate so bootstrap can still
    # produce deterministic typing in sparse/dev ontologies.
    return (WORKFLOW_INSTANCE_TYPE_ID_CANDIDATES[0],)


def clear_workflow_type_resolution_cache() -> None:
    """Test helper: clear cached workflow type resolution."""
    resolve_available_workflow_type_ids.cache_clear()


def bootstrap_workflow_concepts(
    *,
    registry: WorkflowRegistry,
    create_missing: bool = True,
    enforce_required_type: bool = True,
) -> Dict[str, Any]:
    """Ensure registered workflows have concept identities and required typing."""
    workflow_ids = sorted(set(registry.all_workflow_ids()))
    required_type_ids = list(resolve_available_workflow_type_ids())
    preferred_type_id = required_type_ids[0] if required_type_ids else None

    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    errors: dict[str, str] = {}

    for workflow_id in workflow_ids:
        registration = registry.get_registration(workflow_id)
        concept_doc, load_error = _load_concept(workflow_id)
        if load_error:
            errors[workflow_id] = f"lookup_failed:{load_error}"
            continue

        if concept_doc is None:
            if not create_missing:
                unchanged.append(workflow_id)
                continue

            try:
                parent_ids = [preferred_type_id] if preferred_type_id else []
                concept_service.create_concept(
                    name=_titleise_workflow_id(workflow_id),
                    concept_id=workflow_id,
                    description=registration.purpose if registration else None,
                    parent_concept_ids=parent_ids,
                    create_as_instance=True,
                )
                created.append(workflow_id)
            except Exception as exc:  # pragma: no cover - defensive
                errors[workflow_id] = f"create_failed:{exc}"
            continue

        if not enforce_required_type or not required_type_ids:
            unchanged.append(workflow_id)
            continue

        current_instance_of = _extract_instance_of(concept_doc)
        if any(type_id in current_instance_of for type_id in required_type_ids):
            unchanged.append(workflow_id)
            continue

        try:
            merged_instance_of = list(current_instance_of)
            if preferred_type_id and preferred_type_id not in merged_instance_of:
                merged_instance_of.append(preferred_type_id)

            relationships = dict(concept_doc.get("relationships") or {})
            relationships["is_an_instance_of"] = merged_instance_of
            concept_service.update_concept(workflow_id, {"relationships": relationships})
            updated.append(workflow_id)
        except Exception as exc:  # pragma: no cover - defensive
            errors[workflow_id] = f"type_enforcement_failed:{exc}"

    return {
        "counts": {
            "registry_workflows": len(workflow_ids),
            "created": len(created),
            "updated": len(updated),
            "unchanged": len(unchanged),
            "errors": len(errors),
        },
        "required_type_ids": required_type_ids,
        "preferred_type_id": preferred_type_id,
        "created_workflow_ids": created,
        "updated_workflow_ids": updated,
        "unchanged_workflow_ids": unchanged,
        "errors_by_workflow_id": errors,
    }


def build_workflow_concept_authority_report(
    *,
    registry: WorkflowRegistry,
) -> Dict[str, Any]:
    """Classify workflow concept authority drift for registered workflows."""
    workflow_ids = sorted(set(registry.all_workflow_ids()))
    required_type_ids = list(resolve_available_workflow_type_ids())

    missing_concepts: list[str] = []
    missing_required_type: dict[str, list[str]] = {}
    lookup_errors: dict[str, str] = {}
    valid: list[str] = []

    for workflow_id in workflow_ids:
        concept_doc, load_error = _load_concept(workflow_id)
        if load_error:
            lookup_errors[workflow_id] = load_error
            continue
        if concept_doc is None:
            missing_concepts.append(workflow_id)
            continue

        instance_of = _extract_instance_of(concept_doc)
        if required_type_ids and not any(
            type_id in instance_of for type_id in required_type_ids
        ):
            missing_required_type[workflow_id] = instance_of
            continue
        valid.append(workflow_id)

    drift_detected = bool(missing_concepts or missing_required_type or lookup_errors)

    return {
        "drift_detected": drift_detected,
        "required_type_ids": required_type_ids,
        "counts": {
            "registry_workflows": len(workflow_ids),
            "missing_concepts": len(missing_concepts),
            "missing_required_type": len(missing_required_type),
            "lookup_errors": len(lookup_errors),
            "valid": len(valid),
        },
        "missing_concept_workflow_ids": missing_concepts,
        "missing_required_type_by_workflow_id": missing_required_type,
        "lookup_errors_by_workflow_id": lookup_errors,
        "valid_workflow_ids": valid,
    }

