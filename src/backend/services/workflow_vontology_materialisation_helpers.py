"""Shared helpers for workflow-related Vontology materialisation."""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from collections.abc import Mapping, Sequence
from collections.abc import Iterator
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..utils.concept_id_utils import canonicalise_vontology_concept_id
from . import concept_service

_WORKFLOW_PARENT_ID_SANITISER = re.compile(r"[^a-z0-9]+")


def load_concept(concept_id: str) -> dict[str, Any] | None:
    """Return one concept document by concept_id, or None when missing."""

    concept_id_clean = str(concept_id or "").strip()
    if not concept_id_clean:
        return None
    try:
        concept_doc = ConceptsRepository.find_one({"concept_id": concept_id_clean})
    except Exception:
        return None
    return dict(concept_doc) if isinstance(concept_doc, Mapping) else None


def normalise_relationship_targets(value: Any) -> list[str]:
    """Return a clean list of relationship targets from string/list input."""

    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if not isinstance(value, list):
        return []
    targets: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if cleaned:
            targets.append(cleaned)
    return targets


def ensure_instance_typing(
    *,
    concept_id: str,
    type_ids: Sequence[str],
    remove_type_parent_ids: Sequence[str] = (),
) -> bool:
    """Ensure a concept is typed as an instance of the supplied type IDs.

    When ``remove_type_parent_ids`` is supplied, those IDs are removed from
    ``relationships.is_a_type_of`` so previously mis-typed workflow concepts stop
    presenting as ontology types.
    """

    concept_doc = load_concept(concept_id)
    if concept_doc is None:
        return False

    relationships = dict(concept_doc.get("relationships") or {})
    instance_targets = normalise_relationship_targets(
        relationships.get("is_an_instance_of")
    )
    parent_targets = normalise_relationship_targets(relationships.get("is_a_type_of"))

    changed = False
    for type_id in type_ids:
        type_id_clean = str(type_id or "").strip()
        if not type_id_clean or type_id_clean in instance_targets:
            continue
        instance_targets.append(type_id_clean)
        changed = True

    if remove_type_parent_ids:
        remove_set = {
            str(type_id).strip()
            for type_id in remove_type_parent_ids
            if isinstance(type_id, str) and str(type_id).strip()
        }
        filtered_parent_targets = [
            target for target in parent_targets if target not in remove_set
        ]
        if filtered_parent_targets != parent_targets:
            parent_targets = filtered_parent_targets
            changed = True

    if not changed:
        return False

    relationships["is_an_instance_of"] = instance_targets
    relationships["is_a_type_of"] = parent_targets
    concept_service.update_concept(concept_id, {"relationships": relationships})
    return True


def stable_named_instance_concept_id(name: str, *, prefix: str) -> str:
    """Build a stable concept_id from a display name and short prefix."""

    raw_name = str(name or "").strip()
    canonical = canonicalise_vontology_concept_id(raw_name)
    slug_source = canonical[3:] if canonical else ""
    slug = _WORKFLOW_PARENT_ID_SANITISER.sub("_", slug_source).strip("_")
    if not slug:
        slug = "unnamed"
    if len(slug) > 96:
        slug = slug[:96].rstrip("_")
    digest = hashlib.sha256(raw_name.casefold().encode("utf-8")).hexdigest()[:8]
    prefix_clean = _WORKFLOW_PARENT_ID_SANITISER.sub(
        "_", str(prefix or "").strip().lower()
    ).strip("_")
    prefix_clean = prefix_clean or "concept"
    return f"#V#{prefix_clean}_{slug}_{digest}"


@contextmanager
def suspend_event_workflow_integration() -> Iterator[None]:
    """Temporarily disable event-driven workflow launches during materialisation.

    Canonical workflow publication and repair should not recursively trigger
    event-driven mutation workflows while authoring workflow concepts, steps,
    and text relations. Bulk authoring also suppresses per-mutation workflow
    discovery cache invalidation because bootstrap services perform one
    authoritative invalidation after publication completes.
    """

    prior_event = os.environ.get("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE")
    prior_discovery = os.environ.get(
        "VON_WORKFLOW_DISCOVERY_CACHE_INVALIDATION_ENABLE"
    )
    os.environ["VON_EVENT_WORKFLOW_INTEGRATION_ENABLE"] = "0"
    os.environ["VON_WORKFLOW_DISCOVERY_CACHE_INVALIDATION_ENABLE"] = "0"
    try:
        from .relationship_extent_index_service import (
            defer_relationship_extent_index_sync,
        )

        with defer_relationship_extent_index_sync():
            yield
    finally:
        if prior_event is None:
            os.environ.pop("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", None)
        else:
            os.environ["VON_EVENT_WORKFLOW_INTEGRATION_ENABLE"] = prior_event
        if prior_discovery is None:
            os.environ.pop("VON_WORKFLOW_DISCOVERY_CACHE_INVALIDATION_ENABLE", None)
        else:
            os.environ["VON_WORKFLOW_DISCOVERY_CACHE_INVALIDATION_ENABLE"] = (
                prior_discovery
            )


__all__ = [
    "ensure_instance_typing",
    "load_concept",
    "normalise_relationship_targets",
    "stable_named_instance_concept_id",
    "suspend_event_workflow_integration",
]
