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
- Explicit external identifiers are checked before title-derived canonical or
  semantic reuse. Domain-specific identifiers are never inferred from names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

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
    "person_comma_order_exact",
}
_PERSON_TYPE_ID = "#V#person"
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
    resolution_source: str | None = None


@dataclass(frozen=True)
class CreateConceptDuplicateGuardBlock:
    error_code: str
    message: str
    match_source: str
    guard_scope: str
    candidate_concept_ids: tuple[str, ...] = ()
    external_identifier_scheme: str | None = None
    external_identifier_value: str | None = None
    resolution_source: str | None = None
    retryable: bool = False


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
    if any(relationships.get(key) for key in ("is_a_type_of", "is_an_instance_of")):
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


def _parent_is_compatible(
    *,
    existing_parent_ids: Sequence[str],
    requested_parent_id: str | None,
) -> bool:
    """Accept direct or descendant typing without broadening identity matching.

    The duplicate guard already requires a conservative exact-name/code match.
    Once that stable identity evidence exists, an instance of a subtype of the
    requested parent is compatible with the broader requested type.  Requiring
    the parent edge to be direct would, for example, reject an existing Student
    when a caller asks to create the same named Person.
    """

    if not requested_parent_id:
        return True
    canonical_existing = {
        canonicalise_vontology_concept_id(parent_id) or parent_id
        for parent_id in existing_parent_ids
        if isinstance(parent_id, str) and parent_id
    }
    if requested_parent_id in canonical_existing:
        return True
    try:
        from ..vontology.utils_vontology import (
            get_vontology_node_and_descendant_ids,
        )

        descendants = {
            canonicalise_vontology_concept_id(concept_id) or concept_id
            for concept_id in get_vontology_node_and_descendant_ids(
                requested_parent_id,
                include_descendants=True,
            )
            if isinstance(concept_id, str) and concept_id
        }
    except Exception:
        # Identity reuse is fail-closed when hierarchy read-back is unavailable.
        return False
    return bool(canonical_existing.intersection(descendants))


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
        and not _parent_is_compatible(
            existing_parent_ids=existing_parent_ids,
            requested_parent_id=requested_parent_id,
        )
    ):
        mismatch_reasons.append("parent_mismatch")
    return (
        existing_kind,
        requested_kind,
        existing_parent_ids,
        requested_parent_id,
        tuple(mismatch_reasons),
    )


def _external_identity_mismatch_details(
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
        if requested_parent_id and _parent_is_compatible(
            existing_parent_ids=existing_parent_ids,
            requested_parent_id=requested_parent_id,
        ):
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
    requested_concept_id: str | None = None,
    kind: str | None,
    parent_id_for_concept: str | None,
    preferred_language: str | None = None,
    allow_duplicate_instances: bool = False,
    duplicate_resolution_mode: str | None = None,
    external_identifiers: Any = None,
    external_identity_concept_ids: Sequence[str] = (),
    identity_candidate_concept_ids: Sequence[str] = (),
    identity_rejected_candidate_concept_ids: Sequence[str] = (),
) -> CreateConceptDuplicateGuardMatch | CreateConceptDuplicateGuardBlock | None:
    """Return an existing concept match when duplicate creation should be blocked.

    ``canonical_id_only`` retains the normal exact match and scope checks, but
    deliberately returns after an exact miss instead of searching text values.
    When the create request supplies a stable concept ID, that ID is the exact
    identity to check; the display-name-derived ID remains the compatibility
    default only when no explicit ID was supplied.
    """

    from .concept_external_identity_service import (
        normalise_create_external_identifiers,
        resolve_external_identity_candidates,
    )

    normalised_kind = _normalise_kind(kind)
    normalised_external_identifiers = normalise_create_external_identifiers(
        external_identifiers=external_identifiers,
        concept_name=concept_name,
        kind=normalised_kind,
    )
    scope = _guard_scope_for_request(
        normalised_kind,
        parent_id_for_concept,
        # True homonyms may share a name, but an asserted external identity
        # remains singleton even when duplicate instance names were requested.
        allow_duplicate_instances=(
            allow_duplicate_instances and not normalised_external_identifiers
        ),
    )
    if scope is None:
        return None

    if normalised_external_identifiers:
        external_identifier = normalised_external_identifiers[0]
        match_source = f"external_identifier:{external_identifier.scheme}"
        try:
            external_resolution = resolve_external_identity_candidates(
                external_identifier,
                canonical_concept_id_candidates=external_identity_concept_ids,
                asserted_candidate_concept_ids=identity_candidate_concept_ids,
                rejected_candidate_concept_ids=(
                    identity_rejected_candidate_concept_ids
                ),
            )
        except Exception:
            return CreateConceptDuplicateGuardBlock(
                error_code="external_identity_resolution_failed",
                message=(
                    "External identity lookup failed before concept creation. "
                    "No concept was created."
                ),
                match_source=match_source,
                guard_scope=scope,
                external_identifier_scheme=external_identifier.scheme,
                external_identifier_value=external_identifier.value,
                resolution_source="lookup_error",
                retryable=True,
            )

        if external_resolution.status == "not_found":
            # An asserted external identity is authoritative. A title or
            # title-derived concept ID must not substitute for an identity miss.
            return None

        if external_resolution.status == "ambiguous":
            marker_lookup_incomplete = (
                external_resolution.resolution_source
                == "persisted_identity_marker_lookup_incomplete"
            )
            return CreateConceptDuplicateGuardBlock(
                error_code=(
                    "external_identity_marker_lookup_incomplete"
                    if marker_lookup_incomplete
                    else "ambiguous_external_identity"
                ),
                message=(
                    (
                        "The bounded exact-marker lookup was not exhaustive. No "
                        "concept was selected or created."
                    )
                    if marker_lookup_incomplete
                    else (
                        "More than one visible concept carries the requested "
                        "external identity. No concept was selected or created."
                    )
                ),
                match_source=match_source,
                guard_scope=scope,
                candidate_concept_ids=external_resolution.candidate_concept_ids,
                external_identifier_scheme=external_identifier.scheme,
                external_identifier_value=external_identifier.value,
                resolution_source=external_resolution.resolution_source,
            )

        if external_resolution.status == "unverified":
            return CreateConceptDuplicateGuardBlock(
                error_code="external_identity_candidates_require_confirmation",
                message=(
                    "Visible concepts contain possible references to the requested "
                    "external identity, but no exact identity marker or "
                    "caller-confirmed candidate establishes which entity to reuse. "
                    "No concept was selected or created."
                ),
                match_source=match_source,
                guard_scope=scope,
                candidate_concept_ids=external_resolution.candidate_concept_ids,
                external_identifier_scheme=external_identifier.scheme,
                external_identifier_value=external_identifier.value,
                resolution_source=external_resolution.resolution_source,
                retryable=True,
            )

        if (
            external_resolution.status == "resolved"
            and len(external_resolution.candidate_concept_ids) == 1
        ):
            existing_concept_id = external_resolution.candidate_concept_ids[0]
            doc = _find_concept(existing_concept_id)
            if not isinstance(doc, dict):
                return CreateConceptDuplicateGuardBlock(
                    error_code="external_identity_resolution_failed",
                    message=(
                        "The externally identified concept changed during "
                        "creation preflight. No concept was created."
                    ),
                    match_source=match_source,
                    guard_scope=scope,
                    external_identifier_scheme=external_identifier.scheme,
                    external_identifier_value=external_identifier.value,
                    resolution_source=external_resolution.resolution_source,
                    retryable=True,
                )
            (
                existing_kind,
                requested_kind,
                existing_parent_ids,
                requested_parent_id,
                mismatch_reasons,
            ) = _external_identity_mismatch_details(
                doc=doc,
                scope=scope,
                parent_id_for_concept=parent_id_for_concept,
            )
            return CreateConceptDuplicateGuardMatch(
                existing_concept_id=existing_concept_id,
                match_source=match_source,
                guard_scope=scope,
                identity_conflict=bool(mismatch_reasons),
                existing_kind=existing_kind,
                requested_kind=requested_kind,
                existing_parent_ids=existing_parent_ids,
                requested_parent_id=requested_parent_id,
                mismatch_reasons=mismatch_reasons,
                resolution_source=external_resolution.resolution_source,
            )

        return CreateConceptDuplicateGuardBlock(
            error_code="external_identity_resolution_failed",
            message=(
                "External identity lookup returned an invalid resolution state. "
                "No concept was created."
            ),
            match_source=match_source,
            guard_scope=scope,
            external_identifier_scheme=external_identifier.scheme,
            external_identifier_value=external_identifier.value,
            resolution_source=external_resolution.resolution_source,
            retryable=True,
        )

    explicit_requested_id = (
        requested_concept_id.strip()
        if isinstance(requested_concept_id, str) and requested_concept_id.strip()
        else None
    )
    canonical_requested_id = canonicalise_vontology_concept_id(
        explicit_requested_id if explicit_requested_id is not None else concept_name
    )
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

        canonical_parent_id = canonicalise_vontology_concept_id(parent_id_for_concept)
        resolver_instance_of = (
            _PERSON_TYPE_ID
            if normalised_kind == "instance" and canonical_parent_id == _PERSON_TYPE_ID
            else None
        )

        resolution = resolve_concept_by_name(
            name=str(concept_name or "").strip(),
            preferred_languages=(
                [preferred_language.strip()]
                if isinstance(preferred_language, str) and preferred_language.strip()
                else None
            ),
            instance_of=resolver_instance_of,
            match_code_strings=True,
            max_results=5,
        )
    except Exception:
        return None

    if not isinstance(resolution, dict):
        return None

    if (
        resolution.get("status") == "ambiguous"
        and resolver_instance_of == _PERSON_TYPE_ID
    ):
        candidates = resolution.get("candidates")
        comma_order_candidate_ids = (
            tuple(
                sorted(
                    {
                        str(candidate.get("concept_id")).strip()
                        for candidate in candidates
                        if isinstance(candidate, dict)
                        and candidate.get("stage") == "person_comma_order_exact"
                        and isinstance(candidate.get("concept_id"), str)
                        and str(candidate.get("concept_id")).strip()
                    }
                )
            )
            if isinstance(candidates, list)
            else ()
        )
        if comma_order_candidate_ids:
            return CreateConceptDuplicateGuardBlock(
                error_code="ambiguous_person_name_order_match",
                message=(
                    "More than one visible person has the exact natural-order "
                    "equivalent of the supplied comma-form name. No concept was "
                    "selected or created."
                ),
                match_source="person_comma_order_name_resolution",
                guard_scope=scope,
                candidate_concept_ids=comma_order_candidate_ids,
                resolution_source="person_comma_order_exact",
                retryable=True,
            )

    if resolution.get("status") != "resolved":
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
    if (
        match_stage == "person_comma_order_exact"
        and resolver_instance_of != _PERSON_TYPE_ID
    ):
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
    error_code: str = "canonical_identity_conflict",
    match_source: str = "canonical_concept_id",
) -> dict[str, Any]:
    mismatch_summary = ", ".join(mismatch_reasons) or "incompatible identity"
    identity_label = (
        "External concept identity"
        if match_source.startswith("external_identifier:")
        else "Canonical concept identity"
    )
    return {
        "success": False,
        "effect_status": "failed",
        "changed": False,
        "message": (
            f"{identity_label} '{existing_concept_id}' is already occupied "
            f"by an incompatible concept ({mismatch_summary}). No concept was created."
        ),
        "concept": None,
        "error_code": error_code,
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
        "duplicate_match_source": match_source,
        "suggestion": (
            "Inspect the existing concept and explicitly reconcile its typing "
            "before deciding whether to reuse or update it."
        ),
    }


def build_duplicate_guard_block_create_concepts_result(
    *,
    requested_name: str,
    requested_kind: str,
    block: CreateConceptDuplicateGuardBlock,
) -> dict[str, Any]:
    return {
        "success": False,
        "effect_status": "failed",
        "changed": False,
        "message": block.message,
        "concept": None,
        "error_code": block.error_code,
        "input_name": requested_name,
        "requested_name": requested_name,
        "requested_kind": requested_kind,
        "duplicate_prevented": True,
        "duplicate_guard_scope": block.guard_scope,
        "duplicate_match_source": block.match_source,
        "candidate_concept_ids": list(block.candidate_concept_ids),
        "external_identifier": {
            "scheme": block.external_identifier_scheme,
            "value": block.external_identifier_value,
        },
        "external_identity_resolution_source": block.resolution_source,
        "retryable": block.retryable,
        "suggestion": (
            (
                "Inspect the candidate people using grounded distinguishing "
                "evidence, then explicitly reuse the identified person. Do not "
                "create another person while the exact name-order match is "
                "ambiguous."
            )
            if block.error_code == "ambiguous_person_name_order_match"
            else
            (
                "Inspect every returned candidate. If grounded evidence "
                "establishes one as this entity, retry with that ID in "
                "identity_candidate_concept_ids. If none matches, retry with "
                "the exact returned set in "
                "identity_rejected_candidate_concept_ids."
            )
            if (
                block.candidate_concept_ids
                and block.resolution_source == "legacy_text_reference"
            )
            else (
                (
                    "Inspect the candidate concepts. If grounded evidence "
                    "establishes one candidate as this entity, retry with that ID "
                    "in identity_candidate_concept_ids; otherwise retain the "
                    "ambiguity."
                )
                if block.candidate_concept_ids
                else "Retry after the external identity lookup is available."
            )
        ),
    }
