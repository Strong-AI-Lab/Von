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


@dataclass(frozen=True)
class CreateConceptDuplicateGuardMatch:
    existing_concept_id: str
    match_source: str
    guard_scope: str


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


def _infer_kind(doc: dict[str, Any]) -> str | None:
    computed = doc.get("computed_kind")
    if isinstance(computed, str):
        raw = computed.strip().lower()
        if raw == "individual":
            return "instance"
        if raw in {"type", "instance", "predicate"}:
            return raw

    relationships = doc.get("relationships")
    if not isinstance(relationships, dict):
        return None

    is_type_of = _extract_str_list(relationships.get("is_a_type_of"))
    is_instance_of = _extract_str_list(relationships.get("is_an_instance_of"))
    if is_type_of:
        return "type"
    if "#V#predicate" in is_instance_of:
        return "predicate"
    if is_instance_of:
        return "instance"
    return None


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
    inferred_kind = _infer_kind(doc)
    relationships = doc.get("relationships")
    if isinstance(relationships, dict):
        instance_of = set(_extract_str_list(relationships.get("is_an_instance_of")))
    else:
        instance_of = set()

    if scope == "type":
        return inferred_kind == "type"
    if scope == "predicate":
        return inferred_kind == "predicate" or "#V#predicate" in instance_of
    if scope == "instance":
        if inferred_kind != "instance":
            return False
        canonical_parent = canonicalise_vontology_concept_id(parent_id_for_concept)
        if canonical_parent:
            return canonical_parent in instance_of
        return True
    if scope == "workflow_instance":
        canonical_parent = canonicalise_vontology_concept_id(parent_id_for_concept)
        if canonical_parent and canonical_parent in instance_of:
            return True
        return bool(instance_of.intersection(_workflow_type_ids()))
    return False


def _find_concept(concept_id: str) -> dict[str, Any] | None:
    if not isinstance(concept_id, str) or not concept_id.strip():
        return None
    doc = ConceptsRepository.find_one({"concept_id": concept_id.strip()})
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
        if isinstance(doc, dict) and _matches_guard_scope(
            doc=doc,
            scope=scope,
            parent_id_for_concept=parent_id_for_concept,
        ):
            return CreateConceptDuplicateGuardMatch(
                existing_concept_id=canonical_requested_id,
                match_source="canonical_concept_id",
                guard_scope=scope,
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
