"""Vontology-backed policy profiles for low-imposition knowledge acquisition.

This module gives workflow-governed acquisition behaviours the same kind of
authoritative Vontology profile surface that representation intents now use.
The initial canonical profile targets relation-completion inside
``#V#rumination_workflow``, but the loader/ensure helpers are intentionally
generic so other acquisition workflows can adopt the same pattern.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation

DEFAULT_KNOWLEDGE_ACQUISITION_PROFILE_PREDICATE = (
    "#V#has_knowledge_acquisition_profile_json"
)
DEFAULT_KNOWLEDGE_ACQUISITION_PROFILE_CONCEPT_ID = (
    "#V#knowledge_acquisition_profile_low_imposition_relation_completion"
)
KNOWLEDGE_ACQUISITION_PROFILE_TYPE_ID = "#V#knowledge_acquisition_profile"
KNOWLEDGE_ACQUISITION_PROFILE_LINK_PREDICATE = "#V#has_knowledge_acquisition_profile"
DEFAULT_PROFILE_WORKFLOW_IDS: tuple[str, ...] = ("#V#rumination_workflow",)

_PROFILE_TEXT_PREDICATES: tuple[str, ...] = (
    "#V#has_knowledge_acquisition_profile_json",
    "has_knowledge_acquisition_profile_json",
    "hasKnowledgeAcquisitionProfileJson",
    "hasContent",
)
_WORKFLOW_LINK_TEXT_PREDICATES: tuple[str, ...] = (
    "#V#has_knowledge_acquisition_profile",
    "has_knowledge_acquisition_profile",
    "hasKnowledgeAcquisitionProfile",
)

_DEFAULT_DECISION_POLICY: dict[str, bool] = {
    "retrieve_existing_context_first": True,
    "auto_apply_low_risk_defaults": True,
    "requires_explicit_user_decision_for_high_risk": True,
    "ask_at_most_one_question_per_run": True,
    "fail_closed_on_missing_profile": True,
}

_RELATION_CANDIDATE_PRIORITY_POLICY_VERSION = (
    "knowledge_acquisition_profile.relation_candidate_priority.v1"
)
_RELATION_AUTO_APPLY_POLICY_VERSION = "knowledge_acquisition_profile.v2"
_DEFAULT_RELATION_CANDIDATE_PRIORITY_POLICY: dict[str, Any] = {
    "policy_version": _RELATION_CANDIDATE_PRIORITY_POLICY_VERSION,
    "default_priority": 0,
    "predicate_priorities": {
        "#V#has_affiliation": 80,
        "#V#member_of_organisation": 78,
        "#V#works_on_project": 76,
        "#V#depends_on": 74,
        "#V#supervised_by": 72,
        "#V#authored_by": 70,
        "#V#has_author": 70,
        "#V#related_to": 10,
    },
}
_DEFAULT_RELATION_AUTO_APPLY_POLICY: dict[str, Any] = {
    "policy_version": _RELATION_AUTO_APPLY_POLICY_VERSION,
    "default_threshold": 0.95,
    "source_adjustments": {
        "human_validated": 0.08,
        "user_confirmed": 0.06,
        "explicit_user_input": 0.05,
        "llm_extraction": 0.0,
        "heuristic_inference": -0.05,
        "unknown": 0.0,
    },
    "min_evidence_count": 1,
    "predicate_policies": {
        "#V#has_affiliation": {"threshold": 0.96},
        "#V#member_of_organisation": {"threshold": 0.96},
        "#V#works_on_project": {"threshold": 0.96},
        "#V#depends_on": {"threshold": 0.98},
        "#V#supervised_by": {"threshold": 0.97},
        "#V#authored_by": {"threshold": 0.95},
        "#V#has_author": {"threshold": 0.95},
        "#V#related_to": {"threshold": 0.95},
    },
}

_CANONICAL_KNOWLEDGE_ACQUISITION_PROFILE_BLUEPRINTS: tuple[dict[str, Any], ...] = (
    {
        "profile_concept_id": DEFAULT_KNOWLEDGE_ACQUISITION_PROFILE_CONCEPT_ID,
        "profile_id": "low_imposition_relation_completion",
        "dispatch_mode": "relation_completion",
        "description": (
            "Low-imposition knowledge-acquisition profile for workflow-governed "
            "relation completion. Retrieve existing context first, auto-apply "
            "only high-confidence low-risk defaults, and ask at most one narrow "
            "question per run when machine-side evidence is insufficient."
        ),
        "decision_policy": dict(_DEFAULT_DECISION_POLICY),
        "question_limit": 1,
        "detail_limit": 80,
        "relation_candidate_priority_policy": dict(
            _DEFAULT_RELATION_CANDIDATE_PRIORITY_POLICY
        ),
        "relation_auto_apply_policy": dict(_DEFAULT_RELATION_AUTO_APPLY_POLICY),
    },
)
_CANONICAL_PROFILE_BY_CONCEPT_ID: dict[str, dict[str, Any]] = {
    str(item["profile_concept_id"]): dict(item)
    for item in _CANONICAL_KNOWLEDGE_ACQUISITION_PROFILE_BLUEPRINTS
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


def _coerce_optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    """Return ``None`` for absent concepts so bootstrap can create them cleanly."""

    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _profile_display_name(profile: Mapping[str, Any]) -> str:
    profile_id = _safe_str(profile.get("profile_id")) or "knowledge acquisition"
    pretty = profile_id.replace("_", " ").strip()
    return f"{pretty[:1].upper() + pretty[1:] if pretty else 'Knowledge acquisition'} profile"


def canonical_knowledge_acquisition_profile_concept_ids() -> tuple[str, ...]:
    """Return canonical knowledge-acquisition profile IDs in deterministic order."""

    return tuple(
        str(item["profile_concept_id"])
        for item in _CANONICAL_KNOWLEDGE_ACQUISITION_PROFILE_BLUEPRINTS
    )


def canonical_knowledge_acquisition_profile_blueprints() -> tuple[dict[str, Any], ...]:
    """Return copy-on-read canonical profile blueprints."""

    return tuple(
        dict(item) for item in _CANONICAL_KNOWLEDGE_ACQUISITION_PROFILE_BLUEPRINTS
    )


def _normalise_source_adjustments(raw_value: Any) -> dict[str, float]:
    if not isinstance(raw_value, Mapping):
        return {}
    normalised: dict[str, float] = {}
    for raw_key, raw_score in raw_value.items():
        key = _safe_str(raw_key)
        score = _coerce_optional_float(raw_score)
        if key is None or score is None:
            continue
        normalised[key] = score
    return normalised


def _normalise_relation_candidate_priority_policy(raw_policy: Any) -> dict[str, Any]:
    if not isinstance(raw_policy, Mapping):
        return {}
    policy = dict(raw_policy)
    raw_predicate_priorities = policy.get("predicate_priorities")
    predicate_priorities: dict[str, int] = {}
    if isinstance(raw_predicate_priorities, Mapping):
        for raw_predicate, raw_priority in raw_predicate_priorities.items():
            predicate_id = _safe_str(raw_predicate)
            priority = _coerce_optional_int(raw_priority)
            if predicate_id is None or priority is None:
                continue
            predicate_priorities[predicate_id] = priority
    return {
        "policy_version": _safe_str(policy.get("policy_version")),
        "default_priority": _coerce_optional_int(policy.get("default_priority")),
        "predicate_priorities": predicate_priorities,
    }


def _normalise_relation_auto_apply_policy(raw_policy: Any) -> dict[str, Any]:
    if not isinstance(raw_policy, Mapping):
        return {}
    policy = dict(raw_policy)
    raw_predicate_policies = policy.get("predicate_policies")
    predicate_policies: dict[str, dict[str, Any]] = {}
    if isinstance(raw_predicate_policies, Mapping):
        for raw_predicate, raw_details in raw_predicate_policies.items():
            predicate_id = _safe_str(raw_predicate)
            if predicate_id is None or not isinstance(raw_details, Mapping):
                continue
            predicate_policy: dict[str, Any] = {}
            threshold = _coerce_optional_float(raw_details.get("threshold"))
            min_evidence_count = _coerce_optional_int(
                raw_details.get("min_evidence_count")
            )
            if threshold is not None:
                predicate_policy["threshold"] = threshold
            if min_evidence_count is not None:
                predicate_policy["min_evidence_count"] = min_evidence_count
            source_adjustments = _normalise_source_adjustments(
                raw_details.get("source_adjustments")
            )
            if source_adjustments:
                predicate_policy["source_adjustments"] = source_adjustments
            predicate_policies[predicate_id] = predicate_policy
    return {
        "policy_version": _safe_str(policy.get("policy_version")),
        "default_threshold": _coerce_optional_float(policy.get("default_threshold")),
        "source_adjustments": _normalise_source_adjustments(
            policy.get("source_adjustments")
        ),
        "min_evidence_count": _coerce_optional_int(policy.get("min_evidence_count")),
        "predicate_policies": predicate_policies,
    }


def _validate_relation_candidate_priority_policy(policy: Any) -> list[str]:
    if not isinstance(policy, Mapping) or not policy:
        return ["missing_relation_candidate_priority_policy"]
    errors: list[str] = []
    if _safe_str(policy.get("policy_version")) is None:
        errors.append("missing_relation_candidate_priority_policy_version")
    if policy.get("default_priority") is None:
        errors.append("missing_relation_candidate_default_priority")
    predicate_priorities = policy.get("predicate_priorities")
    if not isinstance(predicate_priorities, Mapping) or not predicate_priorities:
        errors.append("missing_relation_candidate_predicate_priorities")
    return errors


def _validate_relation_auto_apply_policy(policy: Any) -> list[str]:
    if not isinstance(policy, Mapping) or not policy:
        return ["missing_relation_auto_apply_policy"]
    errors: list[str] = []
    if _safe_str(policy.get("policy_version")) is None:
        errors.append("missing_relation_auto_apply_policy_version")
    if policy.get("default_threshold") is None:
        errors.append("missing_relation_auto_apply_default_threshold")
    if policy.get("min_evidence_count") is None:
        errors.append("missing_relation_auto_apply_min_evidence_count")
    source_adjustments = policy.get("source_adjustments")
    if not isinstance(source_adjustments, Mapping) or not source_adjustments:
        errors.append("missing_relation_auto_apply_source_adjustments")
    predicate_policies = policy.get("predicate_policies")
    if not isinstance(predicate_policies, Mapping) or not predicate_policies:
        errors.append("missing_relation_auto_apply_predicate_policies")
    return errors


def _validate_profile(profile: Mapping[str, Any]) -> list[str]:
    dispatch_mode = _safe_str(profile.get("dispatch_mode"))
    if dispatch_mode != "relation_completion":
        return []
    errors: list[str] = []
    errors.extend(
        _validate_relation_candidate_priority_policy(
            profile.get("relation_candidate_priority_policy")
        )
    )
    errors.extend(
        _validate_relation_auto_apply_policy(
            profile.get("relation_auto_apply_policy")
        )
    )
    return errors


def _normalise_profile(
    raw_profile: Mapping[str, Any],
    *,
    profile_concept_id: str,
) -> dict[str, Any]:
    decision_policy = raw_profile.get("decision_policy")
    decision_policy = dict(decision_policy) if isinstance(decision_policy, Mapping) else {}

    relation_policy = raw_profile.get("relation_auto_apply_policy")
    relation_policy = _normalise_relation_auto_apply_policy(relation_policy)
    priority_policy = _normalise_relation_candidate_priority_policy(
        raw_profile.get("relation_candidate_priority_policy")
    )

    return {
        "profile_concept_id": profile_concept_id,
        "profile_id": _safe_str(raw_profile.get("profile_id")) or profile_concept_id,
        "dispatch_mode": _safe_str(raw_profile.get("dispatch_mode")) or "relation_completion",
        "description": _safe_str(raw_profile.get("description")),
        "decision_policy": {
            **dict(_DEFAULT_DECISION_POLICY),
            **decision_policy,
        },
        "question_limit": max(
            1,
            int(raw_profile.get("question_limit", 1) or 1),
        ),
        "detail_limit": max(
            1,
            int(raw_profile.get("detail_limit", 80) or 80),
        ),
        "relation_candidate_priority_policy": priority_policy,
        "relation_auto_apply_policy": relation_policy,
    }


def resolve_knowledge_acquisition_profile_concept_id(
    *,
    workflow_id: str | None = None,
    profile_concept_id: str | None = None,
) -> str | None:
    """Resolve a profile concept ID from explicit input, workflow link, or default."""

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

    return DEFAULT_KNOWLEDGE_ACQUISITION_PROFILE_CONCEPT_ID


def load_knowledge_acquisition_profile(
    *,
    workflow_id: str | None = None,
    profile_concept_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Load a knowledge-acquisition policy profile from Vontology."""

    resolved_profile_id = resolve_knowledge_acquisition_profile_concept_id(
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
        diagnostics["error_code"] = "knowledge_acquisition_profile_unavailable"
        return None, diagnostics

    concept_doc = _safe_get_concept(resolved_profile_id)
    if not isinstance(concept_doc, Mapping):
        diagnostics["missing_profile_concept_ids"] = [resolved_profile_id]
        diagnostics["error_code"] = "knowledge_acquisition_profile_unavailable"
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
            if isinstance(raw_profile, Mapping):
                normalised_profile = _normalise_profile(
                    raw_profile,
                    profile_concept_id=resolved_profile_id,
                )
                validation_errors = _validate_profile(normalised_profile)
                if validation_errors:
                    diagnostics["loaded_profile_concept_id"] = resolved_profile_id
                    diagnostics["source_predicate"] = predicate
                    diagnostics["validation_errors"] = validation_errors
                    diagnostics["error_code"] = "knowledge_acquisition_profile_invalid"
                    return None, diagnostics
                diagnostics["loaded_profile_concept_id"] = resolved_profile_id
                diagnostics["source_predicate"] = predicate
                return (
                    normalised_profile,
                    diagnostics,
                )

    diagnostics["missing_profile_concept_ids"] = [resolved_profile_id]
    diagnostics["error_code"] = "knowledge_acquisition_profile_unavailable"
    return None, diagnostics


def ensure_canonical_knowledge_acquisition_profiles(
    *,
    concept_ids: Sequence[str] | None = None,
    profile_predicate: str = DEFAULT_KNOWLEDGE_ACQUISITION_PROFILE_PREDICATE,
    workflow_link_predicate: str = KNOWLEDGE_ACQUISITION_PROFILE_LINK_PREDICATE,
    language: str = "en-NZ",
    policy: str = "replace_others",
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    garbage_collect: bool = True,
    create_missing_concepts: bool = True,
    link_workflow_ids: Sequence[str] | None = DEFAULT_PROFILE_WORKFLOW_IDS,
) -> dict[str, Any]:
    """Ensure canonical knowledge-acquisition profiles exist and are linked."""

    requested_concept_ids = (
        _normalise_strings(concept_ids) or canonical_knowledge_acquisition_profile_concept_ids()
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

    profile_type = _safe_get_concept(KNOWLEDGE_ACQUISITION_PROFILE_TYPE_ID)
    if not isinstance(profile_type, Mapping) and create_missing_concepts:
        try:
            concept_service.create_concept(
                name="Knowledge acquisition profile",
                concept_id=KNOWLEDGE_ACQUISITION_PROFILE_TYPE_ID,
                description=(
                    "Type for workflow-governed knowledge-acquisition policy profiles."
                ),
                parent_concept_ids=["#V#thing"],
                create_as_instance=False,
            )
            type_created = True
        except Exception as exc:
            errors_by_concept_id[KNOWLEDGE_ACQUISITION_PROFILE_TYPE_ID] = (
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
                    parent_concept_ids=[KNOWLEDGE_ACQUISITION_PROFILE_TYPE_ID],
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

        profile_json = json.dumps(
            dict(seed_profile),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            upsert_singleton_text_relation(
                subject_concept_id=concept_id,
                predicate=profile_predicate,
                lang=language,
                text=profile_json,
                policy=policy,
                provenance=provenance_payload,
                context=context_payload,
                garbage_collect=garbage_collect,
            )
            persisted_profile_concept_ids.append(concept_id)
        except Exception as exc:
            errors_by_concept_id[concept_id] = f"profile_upsert_failed:{exc}"

    link_target_id = (
        persisted_profile_concept_ids[0]
        if persisted_profile_concept_ids
        else _safe_str(DEFAULT_KNOWLEDGE_ACQUISITION_PROFILE_CONCEPT_ID)
    )
    if link_target_id:
        for workflow_id in workflow_ids:
            try:
                upsert_singleton_text_relation(
                    subject_concept_id=workflow_id,
                    predicate=workflow_link_predicate,
                    lang=language,
                    text=link_target_id,
                    policy=policy,
                    provenance=provenance_payload,
                    context=context_payload,
                    garbage_collect=garbage_collect,
                )
                linked_workflow_ids.append(workflow_id)
            except Exception as exc:
                errors_by_concept_id[workflow_id] = f"workflow_link_failed:{exc}"

    return {
        "success": not (
            unknown_canonical_profile_concept_ids
            or missing_concept_ids
            or errors_by_concept_id
        ),
        "requested_profile_concept_ids": list(requested_concept_ids),
        "canonical_profile_concept_ids": list(
            canonical_knowledge_acquisition_profile_concept_ids()
        ),
        "profile_type_id": KNOWLEDGE_ACQUISITION_PROFILE_TYPE_ID,
        "profile_type_created": type_created,
        "created_profile_concept_ids": created_profile_concept_ids,
        "persisted_profile_concept_ids": persisted_profile_concept_ids,
        "linked_workflow_ids": linked_workflow_ids,
        "unknown_canonical_profile_concept_ids": unknown_canonical_profile_concept_ids,
        "missing_concept_ids": missing_concept_ids,
        "errors_by_concept_id": errors_by_concept_id,
        "counts": {
            "requested": len(requested_concept_ids),
            "created_concepts": len(created_profile_concept_ids),
            "persisted_profiles": len(persisted_profile_concept_ids),
            "linked_workflows": len(linked_workflow_ids),
            "missing_concepts": len(missing_concept_ids),
            "unknown_canonical_ids": len(unknown_canonical_profile_concept_ids),
            "errors": len(errors_by_concept_id),
        },
    }
