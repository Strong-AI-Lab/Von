"""Deterministic duplicate-guard checks for create_concepts.

This module centralises bounded pre-create lookups so all MCP surfaces apply the
same duplicate policy for create_concepts.

Policy:
- Types and predicates are treated as singleton identities by concept_id/name.
- Instances are treated as singleton identities by default (including workflows)
  to prevent accidental suffix-based duplicates (e.g. ``#V#my_workflow_2``) when
  a lookup was skipped.
- Callers can explicitly opt-in to legacy duplicate-instance behaviour via
  ``allow_duplicate_instances=True`` when true homonyms are intended.
- Callers with deterministic stable identities can select
  ``duplicate_resolution_mode="canonical_id_only"`` to retain exact identity
  reuse while skipping semantic name resolution after an exact miss.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..utils.concept_id_utils import canonicalise_vontology_concept_id
from .relationship_write_service import compute_kind_from_relationships
from ..workflows.workflow_concept_authority_service import (
    WORKFLOW_INSTANCE_TYPE_ID_CANDIDATES,
    resolve_available_workflow_type_ids,
)

_SAFE_CREATE_DUPLICATE_RESOLUTION_STAGES = {
    "code_string",
    "exact",
    "casefold_exact",
    "diacritic_insensitive",
    "token_exact",
}
_DUPLICATE_GUARD_PROJECTION = {
    "concept_id": 1,
    "kind": 1,
    "computed_kind": 1,
    "relationships.is_a_type_of": 1,
    "relationships.#V#is_a_type_of": 1,
    "relationships.is_an_instance_of": 1,
    "relationships.#V#is_an_instance_of": 1,
}


@dataclass(frozen=True)
class CreateConceptDuplicateGuardMatch:
    existing_concept_id: str
    match_source: str
    guard_scope: str
    identity_conflict: bool = False
    existing_kind: str | None = None
    requested_kind: str | None = None
    existing_parent_ids: tuple[str, ...] = ()
    requested_parent_id: str | None = None
    mismatch_reasons: tuple[str, ...] = ()


def _normalise_kind(kind: str | None) -> str:
    raw = str(kind or "type").strip().lower()
    if raw == "individual":
        return "instance"
    if raw in {"type", "instance", "predicate"}:
        return raw
    return "type"


def _workflow_type_ids() -> set[str]:
    ids = set(WORKFLOW_INSTANCE_TYPE_ID_CANDIDATES)
    try:
        ids.update(resolve_available_workflow_type_ids())
    except Exception:
        pass
    return {cid for cid in ids if isinstance(cid, str) and cid}


def _extract_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, list):
        return [
            str(item).strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ]
    return []


def _normalised_relationships(doc: dict[str, Any]) -> dict[str, Any]:
    relationships = doc.get("relationships")
    if not isinstance(relationships, dict):
        return {}

    normalised = dict(relationships)
    for canonical_key, compatibility_key in (
        ("is_a_type_of", "#V#is_a_type_of"),
        ("is_an_instance_of", "#V#is_an_instance_of"),
    ):
        if canonical_key not in normalised:
            compatibility_value = normalised.get(compatibility_key)
            if compatibility_value:
                normalised[canonical_key] = compatibility_value
    return normalised


def _infer_kind(doc: dict[str, Any]) -> str | None:
    relationships = _normalised_relationships(doc)
    if any(
        relationships.get(key)
        for key in ("is_a_type_of", "is_an_instance_of")
    ):
        inferred = compute_kind_from_relationships(relationships)
        return "instance" if inferred == "individual" else inferred

    for field_name in ("computed_kind", "kind"):
        computed = doc.get(field_name)
        if not isinstance(computed, str):
            continue
        raw = computed.strip().lower()
        if raw == "individual":
            return "instance"
        if raw in {"type", "instance", "predicate"}:
            return raw
    return None


def _existing_parent_ids(doc: dict[str, Any], requested_kind: str) -> tuple[str, ...]:
    relationships = _normalised_relationships(doc)
    relationship_key = (
        "is_a_type_of" if requested_kind == "type" else "is_an_instance_of"
    )
    parent_ids: list[str] = []
    seen: set[str] = set()
    for raw_parent_id in _extract_str_list(relationships.get(relationship_key)):
        canonical_parent_id = (
            canonicalise_vontology_concept_id(raw_parent_id) or raw_parent_id
        )
        if canonical_parent_id in seen:
            continue
        parent_ids.append(canonical_parent_id)
        seen.add(canonical_parent_id)
    return tuple(parent_ids)


def _identity_mismatch_details(
    *,
    doc: dict[str, Any],
    scope: str,
    parent_id_for_concept: str | None,
) -> tuple[str | None, str, tuple[str, ...], str | None, tuple[str, ...]]:
    requested_kind = "instance" if scope == "workflow_instance" else scope
    existing_kind = _infer_kind(doc)
    existing_parent_ids = _existing_parent_ids(doc, requested_kind)
    requested_parent_id = canonicalise_vontology_concept_id(parent_id_for_concept)

    mismatch_reasons: list[str] = []
    if existing_kind != requested_kind:
        mismatch_reasons.append("kind_mismatch")
    if (
        scope in {"instance", "workflow_instance"}
        and requested_parent_id
        and requested_parent_id not in set(existing_parent_ids)
    ):
        mismatch_reasons.append("parent_mismatch")
    return (
        existing_kind,
        requested_kind,
        existing_parent_ids,
        requested_parent_id,
        tuple(mismatch_reasons),
    )


def _guard_scope_for_request(
    kind: str,
    parent_id_for_concept: str | None,
    *,
    allow_duplicate_instances: bool,
) -> str | None:
    if kind == "type":
        return "type"
    if kind == "predicate":
        return "predicate"
    if kind != "instance":
        return None

    if allow_duplicate_instances:
        return None

    canonical_parent = canonicalise_vontology_concept_id(parent_id_for_concept)
    if canonical_parent and canonical_parent in _workflow_type_ids():
        return "workflow_instance"
    return "instance"


def _matches_guard_scope(
    *,
    doc: dict[str, Any],
    scope: str,
    parent_id_for_concept: str | None,
) -> bool:
    (
        existing_kind,
        _requested_kind,
        existing_parent_ids,
        requested_parent_id,
        mismatch_reasons,
    ) = _identity_mismatch_details(
        doc=doc,
        scope=scope,
        parent_id_for_concept=parent_id_for_concept,
    )
    if scope == "workflow_instance":
        if existing_kind != "instance":
            return False
        if requested_parent_id and requested_parent_id in set(existing_parent_ids):
            return True
        return bool(set(existing_parent_ids).intersection(_workflow_type_ids()))
    return not mismatch_reasons


def _find_concept(concept_id: str) -> dict[str, Any] | None:
    if not isinstance(concept_id, str) or not concept_id.strip():
        return None
    doc = ConceptsRepository.find_one(
        {"concept_id": concept_id.strip()},
        _DUPLICATE_GUARD_PROJECTION,
    )
    return doc if isinstance(doc, dict) else None


def find_existing_concept_for_create_concepts(
    *,
    concept_name: str,
    kind: str | None,
    parent_id_for_concept: str | None,
    preferred_language: str | None = None,
    allow_duplicate_instances: bool = False,
    duplicate_resolution_mode: str | None = None,
) -> CreateConceptDuplicateGuardMatch | None:
    """Return an existing concept match when duplicate creation should be blocked.

    ``canonical_id_only`` retains the normal exact match and scope checks, but
    deliberately returns after an exact miss instead of searching text values.
    """

    normalised_kind = _normalise_kind(kind)
    scope = _guard_scope_for_request(
        normalised_kind,
        parent_id_for_concept,
        allow_duplicate_instances=allow_duplicate_instances,
    )
    if scope is None:
        return None

    canonical_requested_id = canonicalise_vontology_concept_id(concept_name)
    if canonical_requested_id:
        doc = _find_concept(canonical_requested_id)
        if isinstance(doc, dict):
            (
                existing_kind,
                requested_kind,
                existing_parent_ids,
                requested_parent_id,
                mismatch_reasons,
            ) = _identity_mismatch_details(
                doc=doc,
                scope=scope,
                parent_id_for_concept=parent_id_for_concept,
            )
            return CreateConceptDuplicateGuardMatch(
                existing_concept_id=canonical_requested_id,
                match_source="canonical_concept_id",
                guard_scope=scope,
                identity_conflict=bool(mismatch_reasons),
                existing_kind=existing_kind,
                requested_kind=requested_kind,
                existing_parent_ids=existing_parent_ids,
                requested_parent_id=requested_parent_id,
                mismatch_reasons=mismatch_reasons,
            )

    if duplicate_resolution_mode == "canonical_id_only":
        return None

    # Secondary bounded lookup: deterministic resolver over hasName/code aliases.
    try:
        from .concept_resolution_service import resolve_concept_by_name

        resolution = resolve_concept_by_name(
            name=str(concept_name or "").strip(),
            preferred_languages=(
                [preferred_language.strip()]
                if isinstance(preferred_language, str) and preferred_language.strip()
                else None
            ),
            match_code_strings=True,
            max_results=5,
        )
    except Exception:
        return None

    if not isinstance(resolution, dict) or resolution.get("status") != "resolved":
        return None

    # JVNAUTOSCI-1211:
    # create_concepts duplicate blocking must stay conservative. We only accept
    # strict match stages and ignore heuristic stages (for example
    # "person_signature"), which can over-match non-person labels like tool names.
    match_payload = resolution.get("match")
    match_stage = (
        str(match_payload.get("stage")).strip()
        if isinstance(match_payload, dict)
        else ""
    )
    if match_stage not in _SAFE_CREATE_DUPLICATE_RESOLUTION_STAGES:
        return None

    resolved_id = resolution.get("resolved_concept_id")
    if not isinstance(resolved_id, str) or not resolved_id.strip():
        return None

    canonical_resolved = (
        canonicalise_vontology_concept_id(resolved_id) or resolved_id.strip()
    )
    if canonical_requested_id and canonical_resolved == canonical_requested_id:
        return None

    doc = _find_concept(canonical_resolved)
    if isinstance(doc, dict) and _matches_guard_scope(
        doc=doc,
        scope=scope,
        parent_id_for_concept=parent_id_for_concept,
    ):
        return CreateConceptDuplicateGuardMatch(
            existing_concept_id=canonical_resolved,
            match_source="name_resolution",
            guard_scope=scope,
        )
    return None


def build_duplicate_prevented_create_concepts_result(
    *,
    requested_name: str,
    requested_kind: str,
    existing_concept_id: str,
    guard_scope: str,
    match_source: str,
) -> dict[str, Any]:
    return {
        "success": False,
        "message": (
            f"Concept '{requested_name}' already exists as '{existing_concept_id}'. "
            "Duplicate create was blocked by deterministic pre-create lookup guard."
        ),
        "concept": None,
        "error_code": "already_exists",
        "existing_concept_id": existing_concept_id,
        "canonical_concept_id": existing_concept_id,
        "input_name": requested_name,
        "requested_name": requested_name,
        "requested_kind": requested_kind,
        "duplicate_prevented": True,
        "duplicate_guard_scope": guard_scope,
        "duplicate_match_source": match_source,
        "suggestion": (
            "Use fetch_concept_content or fetch_concept on existing_concept_id before "
            "attempting create_concepts again."
        ),
    }


def build_canonical_identity_conflict_create_concepts_result(
    *,
    requested_name: str,
    requested_kind: str,
    existing_concept_id: str,
    guard_scope: str,
    existing_kind: str | None,
    existing_parent_ids: tuple[str, ...],
    requested_parent_id: str | None,
    mismatch_reasons: tuple[str, ...],
) -> dict[str, Any]:
    mismatch_summary = ", ".join(mismatch_reasons) or "incompatible identity"
    return {
        "success": False,
        "effect_status": "failed",
        "changed": False,
        "message": (
            f"Canonical concept identity '{existing_concept_id}' is already occupied "
            f"by an incompatible concept ({mismatch_summary}). No concept was created."
        ),
        "concept": None,
        "error_code": "canonical_identity_conflict",
        "existing_concept_id": existing_concept_id,
        "canonical_concept_id": existing_concept_id,
        "input_name": requested_name,
        "requested_name": requested_name,
        "requested_kind": requested_kind,
        "existing_kind": existing_kind,
        "requested_parent_id": requested_parent_id,
        "existing_parent_ids": list(existing_parent_ids),
        "identity_mismatch_reasons": list(mismatch_reasons),
        "duplicate_prevented": True,
        "duplicate_guard_scope": guard_scope,
        "duplicate_match_source": "canonical_concept_id",
        "suggestion": (
            "Use a different canonical name, or inspect the existing concept and "
            "explicitly update its typing if that existing identity is the intended one."
        ),
    }
