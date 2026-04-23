"""Vontology-backed profiles for represented episode self-improvement policy."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_WORKFLOW_ID,
    EPISODE_SELF_IMPROVEMENT_PROMOTION_WORKFLOW_ID,
    EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID,
)
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation

DEFAULT_EPISODE_SELF_IMPROVEMENT_PROFILE_PREDICATE = (
    "#V#has_episode_self_improvement_profile_json"
)
DEFAULT_EPISODE_SELF_IMPROVEMENT_PROFILE_CONCEPT_ID = (
    "#V#episode_self_improvement_profile_workflow_revision_default"
)
EPISODE_SELF_IMPROVEMENT_PROFILE_TYPE_ID = "#V#episode_self_improvement_profile"
EPISODE_SELF_IMPROVEMENT_PROFILE_LINK_PREDICATE = (
    "#V#has_episode_self_improvement_profile"
)
DEFAULT_PROFILE_WORKFLOW_IDS: tuple[str, ...] = (
    EPISODE_EVALUATION_WORKFLOW_ID,
    EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID,
    EPISODE_SELF_IMPROVEMENT_PROMOTION_WORKFLOW_ID,
)

_PROFILE_TEXT_PREDICATES: tuple[str, ...] = (
    "#V#has_episode_self_improvement_profile_json",
    "has_episode_self_improvement_profile_json",
    "hasEpisodeSelfImprovementProfileJson",
    "hasContent",
)
_WORKFLOW_LINK_TEXT_PREDICATES: tuple[str, ...] = (
    "#V#has_episode_self_improvement_profile",
    "has_episode_self_improvement_profile",
    "hasEpisodeSelfImprovementProfile",
)
_SUPPORTED_TARGET_SURFACES = {"workflow"}
_CANDIDATE_SELECTION_POLICY_VERSION = "episode_self_improvement.candidate_selection.v1"
_BENCHMARK_POLICY_VERSION = "episode_self_improvement.benchmark.v1"
_DEFAULT_CANDIDATE_SELECTION_POLICY: dict[str, Any] = {
    "policy_version": _CANDIDATE_SELECTION_POLICY_VERSION,
    "max_candidate_launches": 3,
    "priority_order": ["high", "medium", "low"],
    "eligible_target_surfaces": ["workflow"],
    "dedupe_identity_fields_by_surface": {"workflow": ["target_workflow_id"]},
}
_DEFAULT_BENCHMARK_POLICY: dict[str, Any] = {
    "policy_version": _BENCHMARK_POLICY_VERSION,
    "scan_limit": 200,
    "max_audit_cases": 5,
}
_CANONICAL_EPISODE_SELF_IMPROVEMENT_PROFILE_BLUEPRINTS: tuple[dict[str, Any], ...] = (
    {
        "profile_concept_id": DEFAULT_EPISODE_SELF_IMPROVEMENT_PROFILE_CONCEPT_ID,
        "profile_id": "workflow_revision_default",
        "description": (
            "Canonical represented profile for critique-driven workflow self-improvement. "
            "This profile controls candidate launch budget, target-surface eligibility, "
            "priority ordering, dedupe identity, and benchmark evidence budget."
        ),
        "candidate_selection_policy": dict(_DEFAULT_CANDIDATE_SELECTION_POLICY),
        "benchmark_policy": dict(_DEFAULT_BENCHMARK_POLICY),
    },
)
_CANONICAL_PROFILE_BY_CONCEPT_ID: dict[str, dict[str, Any]] = {
    str(item["profile_concept_id"]): dict(item)
    for item in _CANONICAL_EPISODE_SELF_IMPROVEMENT_PROFILE_BLUEPRINTS
}


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_strings(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(text)
    return tuple(output)


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _profile_display_name(profile: Mapping[str, Any]) -> str:
    profile_id = _safe_str(profile.get("profile_id")) or "episode self improvement"
    pretty = profile_id.replace("_", " ").strip()
    return (
        f"{pretty[:1].upper() + pretty[1:] if pretty else 'Episode self improvement'} profile"
    )


def canonical_episode_self_improvement_profile_concept_ids() -> tuple[str, ...]:
    return tuple(
        str(item["profile_concept_id"])
        for item in _CANONICAL_EPISODE_SELF_IMPROVEMENT_PROFILE_BLUEPRINTS
    )


def _normalise_dedupe_identity_fields_by_surface(
    raw_value: Any,
) -> dict[str, list[str]]:
    if not isinstance(raw_value, Mapping):
        return {}
    normalised: dict[str, list[str]] = {}
    for raw_surface, raw_fields in raw_value.items():
        surface = _safe_str(raw_surface)
        if surface is None:
            continue
        fields = list(_normalise_strings(raw_fields))
        if fields:
            normalised[surface] = fields
    return normalised


def _normalise_candidate_selection_policy(raw_policy: Any) -> dict[str, Any]:
    if not isinstance(raw_policy, Mapping):
        return {}
    policy = dict(raw_policy)
    return {
        "policy_version": _safe_str(policy.get("policy_version")),
        "max_candidate_launches": _coerce_optional_int(
            policy.get("max_candidate_launches")
        ),
        "priority_order": list(_normalise_strings(policy.get("priority_order"))),
        "eligible_target_surfaces": list(
            _normalise_strings(policy.get("eligible_target_surfaces"))
        ),
        "dedupe_identity_fields_by_surface": _normalise_dedupe_identity_fields_by_surface(
            policy.get("dedupe_identity_fields_by_surface")
        ),
    }


def _normalise_benchmark_policy(raw_policy: Any) -> dict[str, Any]:
    if not isinstance(raw_policy, Mapping):
        return {}
    policy = dict(raw_policy)
    return {
        "policy_version": _safe_str(policy.get("policy_version")),
        "scan_limit": _coerce_optional_int(policy.get("scan_limit")),
        "max_audit_cases": _coerce_optional_int(policy.get("max_audit_cases")),
    }


def _validate_candidate_selection_policy(policy: Any) -> list[str]:
    if not isinstance(policy, Mapping) or not policy:
        return ["missing_candidate_selection_policy"]
    errors: list[str] = []
    if _safe_str(policy.get("policy_version")) is None:
        errors.append("missing_candidate_selection_policy_version")
    max_candidate_launches = _coerce_optional_int(policy.get("max_candidate_launches"))
    if max_candidate_launches is None or max_candidate_launches < 1:
        errors.append("invalid_max_candidate_launches")
    priority_order = policy.get("priority_order")
    if not isinstance(priority_order, Sequence) or isinstance(priority_order, (str, bytes)):
        errors.append("missing_priority_order")
    elif not list(priority_order):
        errors.append("empty_priority_order")
    eligible_target_surfaces = policy.get("eligible_target_surfaces")
    if not isinstance(eligible_target_surfaces, Sequence) or isinstance(
        eligible_target_surfaces,
        (str, bytes),
    ):
        errors.append("missing_eligible_target_surfaces")
    else:
        eligible_surface_values = [str(item) for item in eligible_target_surfaces]
        if not eligible_surface_values:
            errors.append("empty_eligible_target_surfaces")
        unsupported = sorted(
            surface
            for surface in eligible_surface_values
            if surface not in _SUPPORTED_TARGET_SURFACES
        )
        errors.extend(
            f"unsupported_candidate_target_surface:{surface}" for surface in unsupported
        )
    dedupe_identity_fields = policy.get("dedupe_identity_fields_by_surface")
    if not isinstance(dedupe_identity_fields, Mapping) or not dedupe_identity_fields:
        errors.append("missing_dedupe_identity_fields_by_surface")
    return errors


def _validate_benchmark_policy(policy: Any) -> list[str]:
    if not isinstance(policy, Mapping) or not policy:
        return ["missing_benchmark_policy"]
    errors: list[str] = []
    if _safe_str(policy.get("policy_version")) is None:
        errors.append("missing_benchmark_policy_version")
    scan_limit = _coerce_optional_int(policy.get("scan_limit"))
    if scan_limit is None or scan_limit < 1:
        errors.append("invalid_benchmark_scan_limit")
    max_audit_cases = _coerce_optional_int(policy.get("max_audit_cases"))
    if max_audit_cases is None or max_audit_cases < 1:
        errors.append("invalid_benchmark_max_audit_cases")
    return errors


def _validate_profile(profile: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    errors.extend(
        _validate_candidate_selection_policy(profile.get("candidate_selection_policy"))
    )
    errors.extend(_validate_benchmark_policy(profile.get("benchmark_policy")))
    return errors


def _normalise_profile(
    raw_profile: Mapping[str, Any],
    *,
    profile_concept_id: str,
) -> dict[str, Any]:
    return {
        "profile_concept_id": profile_concept_id,
        "profile_id": _safe_str(raw_profile.get("profile_id")) or profile_concept_id,
        "description": _safe_str(raw_profile.get("description")),
        "candidate_selection_policy": _normalise_candidate_selection_policy(
            raw_profile.get("candidate_selection_policy")
        ),
        "benchmark_policy": _normalise_benchmark_policy(
            raw_profile.get("benchmark_policy")
        ),
    }


def resolve_episode_self_improvement_profile_concept_id(
    *,
    workflow_id: str | None = None,
    profile_concept_id: str | None = None,
) -> str | None:
    explicit_id = _safe_str(profile_concept_id)
    if explicit_id:
        return explicit_id

    workflow_concept_id = _safe_str(workflow_id)
    if workflow_concept_id:
        for predicate in _WORKFLOW_LINK_TEXT_PREDICATES:
            texts = get_texts_for_concept(
                workflow_concept_id,
                predicate=predicate,
                limit=1,
            )
            if not texts:
                continue
            text = _safe_str((texts[0] or {}).get("text"))
            if text and text.startswith("#V#"):
                return text

    return DEFAULT_EPISODE_SELF_IMPROVEMENT_PROFILE_CONCEPT_ID


def load_episode_self_improvement_profile(
    *,
    workflow_id: str | None = None,
    profile_concept_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    resolved_profile_id = resolve_episode_self_improvement_profile_concept_id(
        workflow_id=workflow_id,
        profile_concept_id=profile_concept_id,
    )
    diagnostics: dict[str, Any] = {
        "requested_workflow_id": _safe_str(workflow_id),
        "requested_profile_concept_id": _safe_str(profile_concept_id),
        "resolved_profile_concept_id": resolved_profile_id,
        "loaded_profile_concept_id": None,
        "source_predicate": None,
        "missing_profile_concept_ids": [],
        "malformed_profile_concept_ids": [],
        "validation_errors": [],
        "error_code": None,
    }
    if not resolved_profile_id:
        diagnostics["missing_profile_concept_ids"] = [None]
        diagnostics["error_code"] = "episode_self_improvement_profile_unavailable"
        return None, diagnostics

    concept_doc = _safe_get_concept(resolved_profile_id)
    if not isinstance(concept_doc, Mapping):
        diagnostics["missing_profile_concept_ids"] = [resolved_profile_id]
        diagnostics["error_code"] = "episode_self_improvement_profile_unavailable"
        return None, diagnostics

    for predicate in _PROFILE_TEXT_PREDICATES:
        texts = get_texts_for_concept(
            resolved_profile_id,
            predicate=predicate,
            limit=10,
        )
        for row in texts:
            text = _safe_str((row or {}).get("text"))
            if not text:
                continue
            try:
                raw_profile = json.loads(text)
            except json.JSONDecodeError:
                diagnostics["malformed_profile_concept_ids"].append(resolved_profile_id)
                continue
            if not isinstance(raw_profile, Mapping):
                continue
            profile = _normalise_profile(
                raw_profile,
                profile_concept_id=resolved_profile_id,
            )
            validation_errors = _validate_profile(profile)
            if validation_errors:
                diagnostics["loaded_profile_concept_id"] = resolved_profile_id
                diagnostics["source_predicate"] = predicate
                diagnostics["validation_errors"] = validation_errors
                diagnostics["error_code"] = "episode_self_improvement_profile_invalid"
                return None, diagnostics
            diagnostics["loaded_profile_concept_id"] = resolved_profile_id
            diagnostics["source_predicate"] = predicate
            return profile, diagnostics

    diagnostics["missing_profile_concept_ids"] = [resolved_profile_id]
    diagnostics["error_code"] = "episode_self_improvement_profile_unavailable"
    return None, diagnostics


def ensure_canonical_episode_self_improvement_profiles(
    *,
    concept_ids: Sequence[str] | None = None,
    profile_predicate: str = DEFAULT_EPISODE_SELF_IMPROVEMENT_PROFILE_PREDICATE,
    workflow_link_predicate: str = EPISODE_SELF_IMPROVEMENT_PROFILE_LINK_PREDICATE,
    language: str = "en-NZ",
    policy: str = "replace_others",
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    garbage_collect: bool = True,
    create_missing_concepts: bool = True,
    link_workflow_ids: Sequence[str] | None = DEFAULT_PROFILE_WORKFLOW_IDS,
) -> dict[str, Any]:
    requested_concept_ids = (
        _normalise_strings(concept_ids)
        or canonical_episode_self_improvement_profile_concept_ids()
    )
    workflow_ids = _normalise_strings(link_workflow_ids)
    provenance_payload = dict(provenance) if isinstance(provenance, Mapping) else None
    context_payload = dict(context) if isinstance(context, Mapping) else None
    type_created = False
    created_profile_concept_ids: list[str] = []
    persisted_profile_concept_ids: list[str] = []
    linked_workflow_ids: list[str] = []
    unknown_canonical_profile_concept_ids: list[str] = []
    missing_concept_ids: list[str] = []
    errors_by_concept_id: dict[str, str] = {}

    profile_type = _safe_get_concept(EPISODE_SELF_IMPROVEMENT_PROFILE_TYPE_ID)
    if not isinstance(profile_type, Mapping) and create_missing_concepts:
        try:
            concept_service.create_concept(
                name="Episode self improvement profile",
                concept_id=EPISODE_SELF_IMPROVEMENT_PROFILE_TYPE_ID,
                description=(
                    "Type for Vontology-backed episode self-improvement policy profiles."
                ),
                parent_concept_ids=["#V#thing"],
                create_as_instance=False,
            )
            type_created = True
        except Exception as exc:
            errors_by_concept_id[EPISODE_SELF_IMPROVEMENT_PROFILE_TYPE_ID] = (
                f"type_create_failed:{exc}"
            )

    for concept_id in requested_concept_ids:
        seed_profile = _CANONICAL_PROFILE_BY_CONCEPT_ID.get(concept_id)
        if not isinstance(seed_profile, Mapping):
            unknown_canonical_profile_concept_ids.append(concept_id)
            continue

        concept_doc = _safe_get_concept(concept_id)
        if not isinstance(concept_doc, Mapping):
            if not create_missing_concepts:
                missing_concept_ids.append(concept_id)
                continue
            try:
                concept_service.create_concept(
                    name=_profile_display_name(seed_profile),
                    concept_id=concept_id,
                    description=_safe_str(seed_profile.get("description")),
                    parent_concept_ids=[EPISODE_SELF_IMPROVEMENT_PROFILE_TYPE_ID],
                    create_as_instance=True,
                )
                created_profile_concept_ids.append(concept_id)
                concept_doc = _safe_get_concept(concept_id)
            except Exception as exc:
                errors_by_concept_id[concept_id] = f"create_failed:{exc}"
                continue

        if not isinstance(concept_doc, Mapping):
            missing_concept_ids.append(concept_id)
            continue

        try:
            upsert_singleton_text_relation(
                subject_concept_id=concept_id,
                predicate=profile_predicate,
                text=json.dumps(dict(seed_profile), ensure_ascii=True, sort_keys=True),
                lang=language,
                policy=policy,
                provenance=provenance_payload,
                context=context_payload,
                garbage_collect=garbage_collect,
            )
            persisted_profile_concept_ids.append(concept_id)
        except Exception as exc:
            errors_by_concept_id[concept_id] = f"profile_persist_failed:{exc}"
            continue

        for workflow_id in workflow_ids:
            try:
                upsert_singleton_text_relation(
                    subject_concept_id=workflow_id,
                    predicate=workflow_link_predicate,
                    text=concept_id,
                    lang=language,
                    policy=policy,
                    provenance=provenance_payload,
                    context=context_payload,
                    garbage_collect=garbage_collect,
                )
                linked_workflow_ids.append(workflow_id)
            except Exception as exc:
                errors_by_concept_id[workflow_id] = f"workflow_link_failed:{exc}"

    linked_workflow_ids = list(dict.fromkeys(linked_workflow_ids))
    return {
        "success": (
            not errors_by_concept_id
            and not missing_concept_ids
            and not unknown_canonical_profile_concept_ids
        ),
        "profile_type_id": EPISODE_SELF_IMPROVEMENT_PROFILE_TYPE_ID,
        "profile_type_created": type_created,
        "requested_concept_ids": list(requested_concept_ids),
        "created_profile_concept_ids": created_profile_concept_ids,
        "persisted_profile_concept_ids": persisted_profile_concept_ids,
        "linked_workflow_ids": linked_workflow_ids,
        "missing_concept_ids": missing_concept_ids,
        "unknown_canonical_profile_concept_ids": unknown_canonical_profile_concept_ids,
        "errors_by_concept_id": errors_by_concept_id,
    }


__all__ = [
    "DEFAULT_EPISODE_SELF_IMPROVEMENT_PROFILE_CONCEPT_ID",
    "DEFAULT_EPISODE_SELF_IMPROVEMENT_PROFILE_PREDICATE",
    "EPISODE_SELF_IMPROVEMENT_PROFILE_LINK_PREDICATE",
    "EPISODE_SELF_IMPROVEMENT_PROFILE_TYPE_ID",
    "canonical_episode_self_improvement_profile_concept_ids",
    "ensure_canonical_episode_self_improvement_profiles",
    "load_episode_self_improvement_profile",
    "resolve_episode_self_improvement_profile_concept_id",
]
