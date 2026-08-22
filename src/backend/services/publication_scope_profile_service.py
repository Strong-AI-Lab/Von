"""Resolve represented publication-scope profiles without granting authority.

Publication preferences live on Vontology profile concepts.  Type and predicate
concepts link to those profiles through ``#V#has_publication_scope_profile``;
each profile carries one ``publication_scope_profile.v1`` JSON text value.
This module owns only bounded graph traversal, validation, applicability,
precedence, conflict detection, and carrier projection.  It deliberately does
not contain a table from domain concepts or predicates to publication scopes.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from .text_value_service import get_texts_for_concepts

PUBLICATION_SCOPE_PROFILE_SCHEMA_VERSION = "publication_scope_profile.v1"
PUBLICATION_SCOPE_DECISION_SCHEMA_VERSION = "publication_scope_decision.v1"
PUBLICATION_SCOPE_PROFILE_LINK_PREDICATE = "#V#has_publication_scope_profile"
PUBLICATION_SCOPE_PROFILE_TEXT_PREDICATE = "#V#has_publication_scope_profile_json"

_PROFILE_LINK_PREDICATES = (
    PUBLICATION_SCOPE_PROFILE_LINK_PREDICATE,
    "has_publication_scope_profile",
    "hasPublicationScopeProfile",
)
_PROFILE_TEXT_PREDICATES = (
    PUBLICATION_SCOPE_PROFILE_TEXT_PREDICATE,
    "has_publication_scope_profile_json",
    "hasPublicationScopeProfileJson",
)
_TYPE_PARENT_PREDICATES = (
    "is_a_type_of",
    "#V#is_a_type_of",
)
_SUPPORTED_PLANES = frozenset({"instance", "assertion"})
_SUPPORTED_OUTCOMES = frozenset(
    {
        "global_general",
        "organisation_general",
        "user_only_default",
        "restricted_context_required",
        "external_secure_storage",
    }
)
_ORDINARY_VONTOLOGY_OUTCOMES = frozenset(
    {"global_general", "organisation_general", "user_only_default"}
)
_OUTCOME_ALIASES = {
    "global": "global_general",
    "base_publication": "global_general",
    "organisation": "organisation_general",
    "organization": "organisation_general",
    "org": "organisation_general",
    "user": "user_only_default",
    "private": "user_only_default",
    "user_private": "user_only_default",
    "restricted": "restricted_context_required",
    "secure_storage": "external_secure_storage",
    "no_kb": "external_secure_storage",
}
_APPLICABILITY_KEYS = (
    "source_kinds",
    "lifecycle_states",
    "subject_type_ids",
    "object_type_ids",
    "role_ids",
)
_MAX_ROOT_IDS = 24
_MAX_LINEAGE_DEPTH = 16
_MAX_LINEAGE_CONCEPTS = 512
_MAX_PROFILES = 256


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _concept_id(value: Any) -> str | None:
    token = _text(value)
    return token if token.startswith("#V#") else None


def _strings(value: Any, *, concept_ids: bool = False) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        token = _concept_id(item) if concept_ids else _text(item)
        if not token or token in seen:
            continue
        seen.add(token)
        result.append(token)
    return tuple(result)


def _normalise_outcome(value: Any) -> str | None:
    token = _text(value).lower().replace("-", "_").replace(" ", "_")
    token = _OUTCOME_ALIASES.get(token, token)
    return token if token in _SUPPORTED_OUTCOMES else None


def _relationship_targets(
    relationships: Mapping[str, Any] | None,
    predicates: Sequence[str],
) -> tuple[str, ...]:
    if not isinstance(relationships, Mapping):
        return ()
    targets: list[str] = []
    for predicate in predicates:
        targets.extend(_strings(relationships.get(predicate), concept_ids=True))
    return _strings(targets, concept_ids=True)


def _load_lineage(root_ids: Sequence[str]) -> dict[str, Any]:
    roots = _strings(root_ids, concept_ids=True)[:_MAX_ROOT_IDS]
    parents_by_id: dict[str, tuple[str, ...]] = {}
    documents_by_id: dict[str, Mapping[str, Any]] = {}
    discovery_depths: dict[str, int] = {root: 0 for root in roots}
    pending: deque[str] = deque(roots)
    loaded_ids: set[str] = set()
    truncated = False

    while pending:
        batch_ids: list[str] = []
        batch_seen: set[str] = set()
        while pending and len(batch_ids) < 64:
            concept_id = pending[0]
            depth = discovery_depths.get(concept_id, _MAX_LINEAGE_DEPTH + 1)
            if depth > _MAX_LINEAGE_DEPTH:
                pending.popleft()
                truncated = True
                continue
            pending.popleft()
            if concept_id in loaded_ids or concept_id in batch_seen:
                continue
            batch_seen.add(concept_id)
            batch_ids.append(concept_id)
        if not batch_ids:
            continue
        if len(loaded_ids) + len(batch_ids) > _MAX_LINEAGE_CONCEPTS:
            batch_ids = batch_ids[: max(0, _MAX_LINEAGE_CONCEPTS - len(loaded_ids))]
            truncated = True
        if not batch_ids:
            break

        rows = list(
            ConceptsRepository.find(
                {"concept_id": {"$in": batch_ids}},
                {
                    "concept_id": 1,
                    "relationships": 1,
                },
                limit=len(batch_ids),
            )
        )
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            concept_id = _concept_id(row.get("concept_id"))
            if not concept_id:
                continue
            documents_by_id[concept_id] = row
            parents_by_id[concept_id] = _relationship_targets(
                row.get("relationships")
                if isinstance(row.get("relationships"), Mapping)
                else None,
                _TYPE_PARENT_PREDICATES,
            )
        loaded_ids.update(batch_ids)

        for concept_id in batch_ids:
            depth = discovery_depths.get(concept_id)
            if depth is None:
                continue
            for parent_id in parents_by_id.get(concept_id, ()):
                proposed_depth = depth + 1
                previous_depth = discovery_depths.get(parent_id)
                if previous_depth is None or proposed_depth < previous_depth:
                    discovery_depths[parent_id] = proposed_depth
                    pending.append(parent_id)

    # Compute each root's independent lineage after the shared bounded graph
    # read.  This preserves incomparable roots while avoiding repeated Atlas
    # reads for their shared ancestors.
    depths_by_root: dict[str, dict[str, int]] = {}
    for root in roots:
        root_depths = {root: 0}
        root_pending: deque[str] = deque([root])
        while root_pending:
            concept_id = root_pending.popleft()
            depth = root_depths[concept_id]
            if depth >= _MAX_LINEAGE_DEPTH:
                if parents_by_id.get(concept_id):
                    truncated = True
                continue
            for parent_id in parents_by_id.get(concept_id, ()):
                proposed_depth = depth + 1
                previous_depth = root_depths.get(parent_id)
                if previous_depth is None or proposed_depth < previous_depth:
                    root_depths[parent_id] = proposed_depth
                    root_pending.append(parent_id)
        depths_by_root[root] = root_depths

    unavailable_root_ids = [root for root in roots if root not in documents_by_id]
    return {
        "root_ids": roots,
        "depths_by_root": depths_by_root,
        "parents_by_id": parents_by_id,
        "documents_by_id": documents_by_id,
        "unavailable_root_ids": unavailable_root_ids,
        "truncated": truncated,
    }


def _normalise_profile(
    raw_profile: Mapping[str, Any],
    *,
    profile_concept_id: str,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    errors: list[str] = []
    if (
        _text(raw_profile.get("schema_version"))
        != PUBLICATION_SCOPE_PROFILE_SCHEMA_VERSION
    ):
        errors.append("schema_version_unsupported")
    plane = _text(raw_profile.get("plane")).lower()
    if plane not in _SUPPORTED_PLANES:
        errors.append("plane_invalid")
    outcome = _normalise_outcome(
        raw_profile.get("recommended_scope_mode")
        or raw_profile.get("recommended_outcome")
    )
    if outcome is None:
        errors.append("recommended_scope_mode_invalid")
    status = _text(raw_profile.get("status")).lower() or "active"
    if status not in {"active", "draft", "inactive", "superseded"}:
        errors.append("status_invalid")
    version = _text(raw_profile.get("version"))
    if not version:
        errors.append("version_missing")
    profile_id = _text(raw_profile.get("profile_id")) or profile_concept_id

    raw_applicability = raw_profile.get("applicability")
    applicability = (
        dict(raw_applicability) if isinstance(raw_applicability, Mapping) else {}
    )
    normalised_applicability: dict[str, Any] = {}
    for key in _APPLICABILITY_KEYS:
        values = _strings(
            applicability.get(key),
            concept_ids=key.endswith("_ids"),
        )
        if values:
            normalised_applicability[key] = list(values)
    if "stable_identity_required" in applicability:
        required = applicability.get("stable_identity_required")
        if not isinstance(required, bool):
            errors.append("stable_identity_required_invalid")
        else:
            normalised_applicability["stable_identity_required"] = required

    if errors:
        return None, tuple(errors)
    return (
        {
            "schema_version": PUBLICATION_SCOPE_PROFILE_SCHEMA_VERSION,
            "profile_concept_id": profile_concept_id,
            "profile_id": profile_id,
            "version": version,
            "status": status,
            "plane": plane,
            "recommended_scope_mode": outcome,
            "applicability": normalised_applicability,
            "owner_concept_id": _concept_id(raw_profile.get("owner_concept_id")),
            "source": _text(raw_profile.get("source")) or None,
            "description": _text(raw_profile.get("description")) or None,
        },
        (),
    )


def _load_linked_profiles(lineage: Mapping[str, Any]) -> dict[str, Any]:
    documents_by_id = lineage.get("documents_by_id")
    if not isinstance(documents_by_id, Mapping):
        documents_by_id = {}
    profile_bindings: dict[str, list[str]] = {}
    profile_ids: list[str] = []
    for subject_id, document in documents_by_id.items():
        if not isinstance(document, Mapping):
            continue
        relationships = document.get("relationships")
        linked = _relationship_targets(
            relationships if isinstance(relationships, Mapping) else None,
            _PROFILE_LINK_PREDICATES,
        )
        if linked:
            profile_bindings[str(subject_id)] = list(linked)
            profile_ids.extend(linked)
    ordered_profile_ids = list(_strings(profile_ids, concept_ids=True))[:_MAX_PROFILES]
    text_query_metadata: dict[str, Any] = {}
    rows_by_profile = get_texts_for_concepts(
        ordered_profile_ids,
        predicates=_PROFILE_TEXT_PREDICATES,
        limit_per_concept=10,
        query_metadata=text_query_metadata,
    )

    profiles_by_id: dict[str, dict[str, Any]] = {}
    errors_by_profile_id: dict[str, list[str]] = {}
    source_predicate_by_profile_id: dict[str, str] = {}
    for profile_id in ordered_profile_ids:
        rows = rows_by_profile.get(profile_id) or []
        selected_rows: list[Mapping[str, Any]] = []
        for predicate in _PROFILE_TEXT_PREDICATES:
            selected_rows = [
                row
                for row in rows
                if isinstance(row, Mapping) and _text(row.get("predicate")) == predicate
            ]
            if selected_rows:
                source_predicate_by_profile_id[profile_id] = predicate
                break
        if not selected_rows:
            errors_by_profile_id[profile_id] = ["profile_json_missing"]
            continue
        normalised_payloads: list[dict[str, Any]] = []
        profile_errors: list[str] = []
        for row in selected_rows:
            try:
                raw_profile = json.loads(_text(row.get("text")))
            except (TypeError, ValueError, json.JSONDecodeError):
                profile_errors.append("profile_json_invalid")
                continue
            if not isinstance(raw_profile, Mapping):
                profile_errors.append("profile_json_not_object")
                continue
            normalised, errors = _normalise_profile(
                raw_profile,
                profile_concept_id=profile_id,
            )
            profile_errors.extend(errors)
            if normalised is not None:
                normalised_payloads.append(normalised)
        distinct_payloads = {
            json.dumps(item, sort_keys=True, ensure_ascii=True)
            for item in normalised_payloads
        }
        if len(distinct_payloads) > 1:
            profile_errors.append("multiple_active_payloads")
        if profile_errors or not normalised_payloads:
            errors_by_profile_id[profile_id] = list(dict.fromkeys(profile_errors))
            continue
        profiles_by_id[profile_id] = normalised_payloads[0]

    return {
        "profile_bindings": profile_bindings,
        "profiles_by_id": profiles_by_id,
        "errors_by_profile_id": errors_by_profile_id,
        "source_predicate_by_profile_id": source_predicate_by_profile_id,
        "text_query_metadata": text_query_metadata,
        "truncated": len(set(profile_ids)) > _MAX_PROFILES,
    }


def _applicability_context(
    *,
    source_context: Mapping[str, Any] | None,
    source_kind: Any,
    lifecycle_state: Any,
    role_concept_ids: Sequence[str] | None,
    subject_type_concept_ids: Sequence[str] | None,
    object_type_concept_ids: Sequence[str] | None,
    stable_identity_present: Any,
) -> dict[str, Any]:
    context = dict(source_context) if isinstance(source_context, Mapping) else {}
    source_kind_value = _text(source_kind or context.get("source_kind")).lower()
    lifecycle_value = _text(lifecycle_state or context.get("lifecycle_state")).lower()
    stable_value = (
        stable_identity_present
        if isinstance(stable_identity_present, bool)
        else context.get("stable_identity_present")
    )
    return {
        "source_kind": source_kind_value or None,
        "lifecycle_state": lifecycle_value or None,
        "role_ids": list(
            _strings(
                role_concept_ids or context.get("role_ids"),
                concept_ids=True,
            )
        ),
        "subject_type_ids": list(_strings(subject_type_concept_ids, concept_ids=True)),
        "object_type_ids": list(_strings(object_type_concept_ids, concept_ids=True)),
        "stable_identity_present": (
            stable_value if isinstance(stable_value, bool) else None
        ),
    }


def _profile_applies(
    profile: Mapping[str, Any],
    context: Mapping[str, Any],
) -> tuple[bool, int, list[str]]:
    applicability = profile.get("applicability")
    if not isinstance(applicability, Mapping) or not applicability:
        return True, 0, []
    matched_fields: list[str] = []
    scalar_mapping = {
        "source_kinds": "source_kind",
        "lifecycle_states": "lifecycle_state",
    }
    for profile_key, context_key in scalar_mapping.items():
        allowed_values = set(_strings(applicability.get(profile_key)))
        if not allowed_values:
            continue
        actual_value = _text(context.get(context_key)).lower()
        if not actual_value or actual_value not in {
            item.lower() for item in allowed_values
        }:
            return False, 0, matched_fields
        matched_fields.append(profile_key)
    for key in ("subject_type_ids", "object_type_ids", "role_ids"):
        required_values = set(_strings(applicability.get(key), concept_ids=True))
        if not required_values:
            continue
        actual_values = set(_strings(context.get(key), concept_ids=True))
        if not actual_values.intersection(required_values):
            return False, 0, matched_fields
        matched_fields.append(key)
    if "stable_identity_required" in applicability:
        required = bool(applicability.get("stable_identity_required"))
        if context.get("stable_identity_present") is not required:
            return False, 0, matched_fields
        matched_fields.append("stable_identity_required")
    return True, len(matched_fields), matched_fields


def _is_strict_descendant(
    child_id: str,
    ancestor_id: str,
    parents_by_id: Mapping[str, Sequence[str]],
) -> bool:
    if child_id == ancestor_id:
        return False
    pending = deque([child_id])
    seen = {child_id}
    while pending:
        current = pending.popleft()
        for parent in parents_by_id.get(current, ()):
            if parent == ancestor_id:
                return True
            if parent not in seen:
                seen.add(parent)
                pending.append(parent)
    return False


def _candidate_projection(candidate: Mapping[str, Any]) -> dict[str, Any]:
    profile = candidate.get("profile")
    profile = profile if isinstance(profile, Mapping) else {}
    return {
        "profile_concept_id": profile.get("profile_concept_id"),
        "profile_id": profile.get("profile_id"),
        "version": profile.get("version"),
        "profile_status": profile.get("status"),
        "plane": profile.get("plane"),
        "recommended_scope_mode": profile.get("recommended_scope_mode"),
        "applicability": dict(profile.get("applicability") or {}),
        "owner_concept_id": profile.get("owner_concept_id"),
        "source": profile.get("source"),
        "description": profile.get("description"),
        "bound_subject_concept_id": candidate.get("bound_subject_concept_id"),
        "lineage_depth": candidate.get("lineage_depth"),
        "applicability_specificity": candidate.get("applicability_specificity"),
        "matched_applicability_fields": list(
            candidate.get("matched_applicability_fields") or ()
        ),
        "source_predicate": candidate.get("source_predicate"),
    }


def _resolve_candidates(
    *,
    plane: str,
    root_ids: Sequence[str],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    lineage = _load_lineage(root_ids)
    loaded = _load_linked_profiles(lineage)
    depths_by_root = lineage["depths_by_root"]
    parents_by_id = lineage["parents_by_id"]
    profiles_by_id = loaded["profiles_by_id"]
    profile_bindings = loaded["profile_bindings"]
    errors_by_profile_id = loaded["errors_by_profile_id"]
    source_predicates = loaded["source_predicate_by_profile_id"]

    candidates: list[dict[str, Any]] = []
    invalid_profile_ids: list[str] = []
    inactive_profile_ids: list[str] = []
    for bound_subject_id, profile_ids in profile_bindings.items():
        lineage_depths = [
            depth_map[bound_subject_id]
            for depth_map in depths_by_root.values()
            if bound_subject_id in depth_map
        ]
        if not lineage_depths:
            continue
        lineage_depth = min(lineage_depths)
        for profile_id in profile_ids:
            if profile_id in errors_by_profile_id:
                invalid_profile_ids.append(profile_id)
                continue
            profile = profiles_by_id.get(profile_id)
            if not isinstance(profile, Mapping):
                invalid_profile_ids.append(profile_id)
                continue
            if profile.get("status") != "active":
                inactive_profile_ids.append(profile_id)
                continue
            if profile.get("plane") != plane:
                # A concept can legitimately carry independent profiles for
                # both semantic planes. The other plane is non-applicable,
                # not malformed input for this decision.
                continue
            applies, specificity, matched_fields = _profile_applies(profile, context)
            if not applies:
                continue
            candidates.append(
                {
                    "profile": profile,
                    "bound_subject_concept_id": bound_subject_id,
                    "lineage_depth": lineage_depth,
                    "applicability_specificity": specificity,
                    "matched_applicability_fields": matched_fields,
                    "source_predicate": source_predicates.get(profile_id),
                }
            )

    undominated: list[dict[str, Any]] = []
    for candidate in candidates:
        subject_id = str(candidate["bound_subject_concept_id"])
        specificity = int(candidate["applicability_specificity"])
        dominated = False
        for other in candidates:
            if other is candidate:
                continue
            other_subject_id = str(other["bound_subject_concept_id"])
            other_specificity = int(other["applicability_specificity"])
            if _is_strict_descendant(other_subject_id, subject_id, parents_by_id):
                dominated = True
                break
            if other_subject_id == subject_id and other_specificity > specificity:
                dominated = True
                break
        if not dominated:
            undominated.append(candidate)

    outcomes = sorted(
        {
            str((candidate.get("profile") or {}).get("recommended_scope_mode"))
            for candidate in undominated
        }
    )
    recommended = outcomes[0] if len(outcomes) == 1 else None
    lineage_projection = {
        root: [
            {"concept_id": concept_id, "depth": depth}
            for concept_id, depth in sorted(
                depth_map.items(), key=lambda item: (item[1], item[0])
            )
        ]
        for root, depth_map in depths_by_root.items()
    }
    return {
        "recommended_scope_mode": recommended,
        "matched_profiles": [_candidate_projection(item) for item in candidates],
        "decisive_profiles": [_candidate_projection(item) for item in undominated],
        "conflicting_scope_modes": outcomes if len(outcomes) > 1 else [],
        "invalid_profile_ids": list(dict.fromkeys(invalid_profile_ids)),
        "profile_errors": {
            profile_id: list(errors_by_profile_id.get(profile_id) or ())
            for profile_id in dict.fromkeys(invalid_profile_ids)
        },
        "inactive_profile_ids": list(dict.fromkeys(inactive_profile_ids)),
        "lineage": lineage_projection,
        "unavailable_root_ids": list(lineage["unavailable_root_ids"]),
        "lineage_truncated": bool(lineage["truncated"]),
        "profile_scan_truncated": bool(loaded["truncated"]),
        "text_query_metadata": dict(loaded["text_query_metadata"]),
    }


def _carrier_projection(plane: str, selected_scope_mode: str | None) -> dict[str, Any]:
    if selected_scope_mode == "restricted_context_required":
        return {
            "supported": False,
            "carrier": "restricted_context",
            "error_code": "restricted_publication_context_carrier_required",
        }
    if selected_scope_mode == "external_secure_storage":
        return {
            "supported": False,
            "carrier": "external_secure_storage",
            "error_code": "ordinary_vontology_storage_prohibited",
        }
    if selected_scope_mode is None:
        return {"supported": False, "carrier": None, "error_code": None}
    if plane == "instance":
        return {
            "supported": True,
            "carrier": "canonical_concept",
            "tool": "create_concepts",
            "scope_mode": selected_scope_mode,
        }
    if selected_scope_mode == "global_general":
        return {
            "supported": True,
            "carrier": "canonical_relationship_candidate",
            "tool": "add_relationship",
            "scope_mode": None,
            "precondition": (
                "The canonical relationship inherits its source publication "
                "context; the governed effect must verify that exact context."
            ),
        }
    assertion_scope_mode = (
        "organisation" if selected_scope_mode == "organisation_general" else "user"
    )
    return {
        "supported": True,
        "carrier": "scoped_assertion",
        "tool": "upsert_scoped_assertion",
        "scope_mode": assertion_scope_mode,
    }


def _required_authority(plane: str, selected_scope_mode: str | None) -> dict[str, Any]:
    if selected_scope_mode is None:
        return {
            "status": "unresolved",
            "profile_is_authority_grant": False,
        }
    if selected_scope_mode in {
        "restricted_context_required",
        "external_secure_storage",
    }:
        return {
            "status": "carrier_required",
            "profile_is_authority_grant": False,
        }
    if plane == "instance":
        authority_kind = {
            "global_general": "global_ontology_administrator",
            "organisation_general": "organisation_ontology_administrator",
            "user_only_default": "private_publication_context_owner",
        }[selected_scope_mode]
        enforced_by = "create_concepts"
    elif selected_scope_mode == "global_general":
        authority_kind = "canonical_source_publication_authority"
        enforced_by = "add_relationship"
    elif selected_scope_mode == "organisation_general":
        authority_kind = "trusted_organisation_assertion_context"
        enforced_by = "upsert_scoped_assertion"
    else:
        authority_kind = "trusted_actor_assertion_context"
        enforced_by = "upsert_scoped_assertion"
    return {
        "status": "deferred_to_effect_boundary",
        "authority_kind": authority_kind,
        "enforced_by": enforced_by,
        "profile_is_authority_grant": False,
    }


def _entity_hint(
    *,
    role: str,
    type_ids: Sequence[str] | None,
    context: Mapping[str, Any],
) -> dict[str, Any] | None:
    roots = _strings(type_ids, concept_ids=True)
    if not roots:
        return None
    resolution = _resolve_candidates(
        plane="instance",
        root_ids=roots,
        context=context,
    )
    return {
        "role": role,
        "type_concept_ids": list(roots),
        "recommended_scope_mode": resolution.get("recommended_scope_mode"),
        "matched_profiles": resolution.get("matched_profiles"),
        "conflicting_scope_modes": resolution.get("conflicting_scope_modes"),
        "invalid_profile_ids": resolution.get("invalid_profile_ids"),
        "used_to_select_assertion_scope": False,
    }


def resolve_publication_scope_profile(
    *,
    plane: str,
    type_concept_ids: Sequence[str] | None = None,
    predicate_concept_id: str | None = None,
    subject_type_concept_ids: Sequence[str] | None = None,
    object_type_concept_ids: Sequence[str] | None = None,
    source_context: Mapping[str, Any] | None = None,
    source_kind: str | None = None,
    lifecycle_state: str | None = None,
    role_concept_ids: Sequence[str] | None = None,
    stable_identity_present: bool | None = None,
    selected_scope_mode: str | None = None,
    selection_reason: str | None = None,
    producer: str | None = None,
) -> dict[str, Any]:
    """Resolve fresh represented advice for one instance or assertion decision."""

    resolved_plane = _text(plane).lower()
    if resolved_plane not in _SUPPORTED_PLANES:
        return {
            "schema_version": PUBLICATION_SCOPE_DECISION_SCHEMA_VERSION,
            "success": False,
            "status": "invalid_request",
            "error_code": "publication_scope_plane_invalid",
            "error": "plane must be 'instance' or 'assertion'",
            "mutation_performed": False,
        }
    if resolved_plane == "instance":
        root_ids = _strings(type_concept_ids, concept_ids=True)
        if not root_ids:
            return {
                "schema_version": PUBLICATION_SCOPE_DECISION_SCHEMA_VERSION,
                "success": False,
                "status": "invalid_request",
                "error_code": "publication_scope_type_ids_required",
                "error": "type_concept_ids are required for instance resolution",
                "mutation_performed": False,
            }
    else:
        predicate_id = _concept_id(predicate_concept_id)
        if not predicate_id:
            return {
                "schema_version": PUBLICATION_SCOPE_DECISION_SCHEMA_VERSION,
                "success": False,
                "status": "invalid_request",
                "error_code": "publication_scope_predicate_id_required",
                "error": ("predicate_concept_id is required for assertion resolution"),
                "mutation_performed": False,
            }
        root_ids = (predicate_id,)

    context = _applicability_context(
        source_context=source_context,
        source_kind=source_kind,
        lifecycle_state=lifecycle_state,
        role_concept_ids=role_concept_ids,
        subject_type_concept_ids=subject_type_concept_ids,
        object_type_concept_ids=object_type_concept_ids,
        stable_identity_present=stable_identity_present,
    )
    resolution = _resolve_candidates(
        plane=resolved_plane,
        root_ids=root_ids,
        context=context,
    )
    recommended_scope_mode = resolution.get("recommended_scope_mode")
    explicit_selection_supplied = bool(_text(selected_scope_mode))
    selected = _normalise_outcome(selected_scope_mode)
    reason = _text(selection_reason)
    conflicts = list(resolution.get("conflicting_scope_modes") or ())
    invalid_profile_ids = list(resolution.get("invalid_profile_ids") or ())
    unavailable_subject_ids = list(resolution.get("unavailable_root_ids") or ())
    protected_scope_modes = sorted(
        {
            scope_mode
            for scope_mode in [recommended_scope_mode, *conflicts]
            if scope_mode in {"restricted_context_required", "external_secure_storage"}
        }
    )

    error_code: str | None = None
    status: str
    selection_source: str | None = None
    requires_model_judgement = False
    if explicit_selection_supplied and selected is None:
        status = "invalid_selection"
        error_code = "publication_scope_selection_invalid"
    elif unavailable_subject_ids:
        status = "unavailable_subject"
        error_code = "publication_scope_subject_unavailable"
        selected = None
    elif invalid_profile_ids:
        status = "invalid_profile"
        error_code = "publication_scope_profile_invalid"
        selected = None
    elif len(protected_scope_modes) > 1:
        # Competing protected carriers cannot safely be collapsed into an
        # ordinary publication choice, even with a model-supplied reason.
        status = "ambiguous"
        error_code = "ambiguous_protected_publication_carrier"
        selected = None
        requires_model_judgement = True
    elif protected_scope_modes:
        # A protected profile names the absence of a safe ordinary-Vontology
        # carrier. An ordinary scope argument cannot convert that into authority.
        selected = protected_scope_modes[0]
        status = "unsupported_carrier"
        error_code = (
            "restricted_publication_context_carrier_required"
            if selected == "restricted_context_required"
            else "ordinary_vontology_storage_prohibited"
        )
        selection_source = "represented_profile"
    elif conflicts and selected is None:
        status = "ambiguous"
        error_code = "ambiguous_publication_scope_profile"
        requires_model_judgement = True
    elif conflicts and selected is not None and not reason:
        status = "invalid_selection"
        error_code = "publication_scope_conflict_reason_required"
        selected = None
        requires_model_judgement = True
    elif conflicts and selected is not None:
        status = "resolved"
        selection_source = "explicit_justified_override"
    elif selected is not None:
        if recommended_scope_mode is None:
            if not reason:
                status = "invalid_selection"
                error_code = "publication_scope_selection_reason_required"
                selected = None
                requires_model_judgement = True
            else:
                status = "resolved"
                selection_source = "explicit_model_judgement_no_profile"
        elif selected == recommended_scope_mode:
            status = "resolved"
            selection_source = "represented_profile_confirmed"
        elif not reason:
            status = "invalid_selection"
            error_code = "publication_scope_override_reason_required"
            selected = None
            requires_model_judgement = True
        else:
            status = "resolved"
            selection_source = "explicit_justified_override"
    elif recommended_scope_mode is not None:
        selected = recommended_scope_mode
        status = "resolved"
        selection_source = "represented_profile"
    else:
        status = "no_profile"
        error_code = "publication_scope_profile_not_found"
        requires_model_judgement = True

    carrier = _carrier_projection(resolved_plane, selected)
    if (
        selected
        in {
            "restricted_context_required",
            "external_secure_storage",
        }
        and status == "resolved"
    ):
        status = "unsupported_carrier"
        error_code = str(
            carrier.get("error_code") or "publication_scope_carrier_unavailable"
        )
    if selected in _ORDINARY_VONTOLOGY_OUTCOMES and not bool(carrier.get("supported")):
        status = "unsupported_carrier"
        error_code = "publication_scope_carrier_unavailable"

    entity_hints: list[dict[str, Any]] = []
    if resolved_plane == "assertion":
        for role, ids in (
            ("subject", subject_type_concept_ids),
            ("object", object_type_concept_ids),
        ):
            hint = _entity_hint(role=role, type_ids=ids, context=context)
            if hint is not None:
                entity_hints.append(hint)

    authority_projection = _required_authority(resolved_plane, selected)
    canonical_read_back_required = bool(selected in _ORDINARY_VONTOLOGY_OUTCOMES)
    decision_evidence = {
        "profile_concept_ids": [
            item.get("profile_concept_id")
            for item in resolution.get("decisive_profiles") or ()
            if isinstance(item, Mapping) and item.get("profile_concept_id")
        ],
        "profile_versions": {
            str(item.get("profile_concept_id")): item.get("version")
            for item in resolution.get("decisive_profiles") or ()
            if isinstance(item, Mapping) and item.get("profile_concept_id")
        },
        "profile_lineage": dict(resolution.get("lineage") or {}),
        "profile_applicability": [
            {
                "profile_concept_id": item.get("profile_concept_id"),
                "applicability": dict(item.get("applicability") or {}),
                "matched_fields": list(item.get("matched_applicability_fields") or ()),
            }
            for item in resolution.get("decisive_profiles") or ()
            if isinstance(item, Mapping)
        ],
        "recommended_scope_mode": recommended_scope_mode,
        "selected_scope_mode": selected,
        "selection_source": selection_source,
        "selection_reason": reason or None,
        "producer": _text(producer) or None,
        "applicability_context": context,
        "actor_authority_decision": authority_projection,
        "canonical_read_back": {
            "required": canonical_read_back_required,
            "status": (
                "required_after_governed_effect"
                if canonical_read_back_required
                else "not_applicable_without_ordinary_vontology_effect"
            ),
        },
    }
    return {
        "schema_version": PUBLICATION_SCOPE_DECISION_SCHEMA_VERSION,
        "success": status == "resolved",
        "status": status,
        "plane": resolved_plane,
        "requested_subject_concept_ids": list(root_ids),
        "recommended_scope_mode": recommended_scope_mode,
        "selected_scope_mode": selected,
        "selection_source": selection_source,
        "requires_model_judgement": requires_model_judgement,
        "mutation_performed": False,
        "matched_profiles": resolution.get("matched_profiles"),
        "decisive_profiles": resolution.get("decisive_profiles"),
        "conflicting_scope_modes": conflicts,
        "invalid_profile_ids": invalid_profile_ids,
        "profile_errors": resolution.get("profile_errors"),
        "inactive_profile_ids": resolution.get("inactive_profile_ids"),
        "lineage": resolution.get("lineage"),
        "unavailable_subject_concept_ids": unavailable_subject_ids,
        "lineage_truncated": resolution.get("lineage_truncated"),
        "profile_scan_truncated": resolution.get("profile_scan_truncated"),
        "entity_context_hints": entity_hints,
        "assertion_scope_selected_from_entity_scope": False,
        "carrier": carrier,
        "required_authority": authority_projection,
        "decision_evidence": decision_evidence,
        "error_code": error_code,
    }


__all__ = [
    "PUBLICATION_SCOPE_DECISION_SCHEMA_VERSION",
    "PUBLICATION_SCOPE_PROFILE_LINK_PREDICATE",
    "PUBLICATION_SCOPE_PROFILE_SCHEMA_VERSION",
    "PUBLICATION_SCOPE_PROFILE_TEXT_PREDICATE",
    "resolve_publication_scope_profile",
]
