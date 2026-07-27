"""Deterministic parent resolution for create_concepts.

JVNAUTOSCI-1101 requires create_concepts to recover from missing workflow-parent
IDs (notably ``#V#workflow_definition``) without relying on free-form retries.
This module centralises parent resolution so all surfaces can share one policy.

Vontology is a graph, not a tree. This resolver only selects the initial parent
required for concept creation; it does not imply uniqueness of parentage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Tuple

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import bypass_access_control
from ..utils.concept_id_utils import canonicalise_vontology_concept_id
from .relationship_write_service import compute_kind_from_relationships
from ..workflows.workflow_concept_authority_service import (
    WORKFLOW_INSTANCE_TYPE_ID_CANDIDATES,
    resolve_available_workflow_type_ids,
)


_WORKFLOW_DEFINITION_SLUGS = frozenset({"workflowdefinition", "workflowdef"})
_PARENT_VISIBILITY_PROJECTION = {"concept_id": 1}
_PARENT_KIND_PROJECTION = {
    "concept_id": 1,
    "kind": 1,
    "computed_kind": 1,
    "relationships.is_a_type_of": 1,
    "relationships.#V#is_a_type_of": 1,
    "relationships.is_an_instance_of": 1,
    "relationships.#V#is_an_instance_of": 1,
}


@dataclass(frozen=True)
class ParentResolutionResult:
    requested_parent_id: str
    canonical_parent_id: str | None
    resolved_parent_id: str | None
    fallback_used: bool
    fallback_candidates_checked: Tuple[str, ...]
    fallback_selected_parent_id: str | None
    resolved_parent_kind: str | None = None

    @property
    def success(self) -> bool:
        return isinstance(self.resolved_parent_id, str) and bool(self.resolved_parent_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_parent_id": self.requested_parent_id,
            "canonical_parent_id": self.canonical_parent_id,
            "resolved_parent_id": self.resolved_parent_id,
            "fallback_used": self.fallback_used,
            "fallback_candidates_checked": list(self.fallback_candidates_checked),
            "fallback_selected_parent_id": self.fallback_selected_parent_id,
            "resolved_parent_kind": self.resolved_parent_kind,
        }


@dataclass(frozen=True)
class _VisibleParentClassification:
    concept_id: str
    structural_kind: str | None


def _find_concept(concept_id: str) -> _VisibleParentClassification | None:
    visible_concept = ConceptsRepository.find_one(
        {"concept_id": concept_id},
        _PARENT_VISIBILITY_PROJECTION,
    )
    if not isinstance(visible_concept, Mapping):
        return None

    # Relationship sanitisation correctly removes targets the actor cannot
    # inspect, but those removals must not change the parent's own structural
    # classification. After actor visibility of the parent is established,
    # derive only the scalar kind under bypass and do not return the raw edges.
    with bypass_access_control():
        structural_projection = ConceptsRepository.find_one(
            {"concept_id": concept_id},
            _PARENT_KIND_PROJECTION,
        )
    if not isinstance(structural_projection, Mapping):
        return None
    return _VisibleParentClassification(
        concept_id=concept_id,
        structural_kind=_concept_kind(structural_projection),
    )


def _concept_kind(concept: Mapping[str, Any] | None) -> str | None:
    if not isinstance(concept, Mapping):
        return None

    relationships = concept.get("relationships")
    if isinstance(relationships, Mapping):
        normalised_relationships = dict(relationships)
        for canonical_key, compatibility_key in (
            ("is_a_type_of", "#V#is_a_type_of"),
            ("is_an_instance_of", "#V#is_an_instance_of"),
        ):
            if canonical_key not in normalised_relationships:
                compatibility_value = normalised_relationships.get(compatibility_key)
                if compatibility_value:
                    normalised_relationships[canonical_key] = compatibility_value

        if any(
            normalised_relationships.get(key)
            for key in ("is_a_type_of", "is_an_instance_of")
        ):
            return compute_kind_from_relationships(normalised_relationships)

    for field_name in ("computed_kind", "kind"):
        raw_kind = concept.get(field_name)
        if not isinstance(raw_kind, str):
            continue
        kind = raw_kind.strip().lower()
        if kind in {"type", "predicate", "individual", "instance"}:
            return kind
    return None


def _canonicalise_candidates(candidates: Iterable[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        canonical = canonicalise_vontology_concept_id(candidate)
        if not canonical or canonical in seen:
            continue
        ordered.append(canonical)
        seen.add(canonical)
    return tuple(ordered)


def _is_workflow_definition_parent(canonical_parent_id: str | None) -> bool:
    if not canonical_parent_id or not canonical_parent_id.startswith("#V#"):
        return False
    slug = canonical_parent_id[3:].replace("_", "").replace("-", "").lower()
    return slug in _WORKFLOW_DEFINITION_SLUGS


def _fallback_candidates_for_parent(canonical_parent_id: str | None) -> tuple[str, ...]:
    if not _is_workflow_definition_parent(canonical_parent_id):
        return ()

    # Prefer currently available workflow supertypes; if discovery cannot find
    # any, fall back to canonical workflow type candidates in stable order.
    dynamic_candidates = tuple(resolve_available_workflow_type_ids())
    if dynamic_candidates:
        return _canonicalise_candidates(dynamic_candidates)
    return _canonicalise_candidates(WORKFLOW_INSTANCE_TYPE_ID_CANDIDATES)


def resolve_parent_for_create_concepts(parent_id: str) -> ParentResolutionResult:
    """Resolve an initial parent for create_concepts with bounded recovery.

    Recovery is deterministic and intentionally narrow. We only retry with a
    canonical candidate list for known workflow-parent aliases.
    """

    canonical_parent = canonicalise_vontology_concept_id(parent_id)
    canonical_concept = _find_concept(canonical_parent) if canonical_parent else None
    if canonical_parent and canonical_concept is not None:
        return ParentResolutionResult(
            requested_parent_id=parent_id,
            canonical_parent_id=canonical_parent,
            resolved_parent_id=canonical_parent,
            fallback_used=False,
            fallback_candidates_checked=(),
            fallback_selected_parent_id=None,
            resolved_parent_kind=canonical_concept.structural_kind,
        )

    fallback_candidates = _fallback_candidates_for_parent(canonical_parent)
    checked: list[str] = []
    for candidate in fallback_candidates:
        if candidate == canonical_parent:
            continue
        checked.append(candidate)
        candidate_concept = _find_concept(candidate)
        if candidate_concept is not None:
            return ParentResolutionResult(
                requested_parent_id=parent_id,
                canonical_parent_id=canonical_parent,
                resolved_parent_id=candidate,
                fallback_used=True,
                fallback_candidates_checked=tuple(checked),
                fallback_selected_parent_id=candidate,
                resolved_parent_kind=candidate_concept.structural_kind,
            )

    return ParentResolutionResult(
        requested_parent_id=parent_id,
        canonical_parent_id=canonical_parent,
        resolved_parent_id=None,
        fallback_used=False,
        fallback_candidates_checked=tuple(checked),
        fallback_selected_parent_id=None,
    )
