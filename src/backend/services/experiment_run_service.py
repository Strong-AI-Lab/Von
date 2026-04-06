"""First-class experiment specification and experiment-run helpers."""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, cast

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure, PyMongoError

from ..db.mongo_client import get_db
from . import concept_service
from .concept_service import (
    ConceptNotFoundError,
    get_concept_by_concept_id_exact,
)
from .namespace_service import parse_namespace, resolve_canonical_namespace
from .relationship_write_service import add_relationship
from .testing_theory_service import compute_testing_theory_diff
from .testing_workflow_contracts import (
    BELONGS_TO_EXPERIMENT_SUITE_PREDICATE_ID,
    EXPERIMENT_CREATE_SPEC_ACTION_ID,
    EXPERIMENT_RUN_SCHEMA_VERSION,
    EXPERIMENT_RUN_STATUSES,
    EXPERIMENT_RUN_STATUS_COMPLETED,
    EXPERIMENT_RUN_STATUS_FAILED,
    EXPERIMENT_RUN_STATUS_PENDING,
    EXPERIMENT_RUN_STATUS_RUNNING,
    EXPERIMENT_RUNS_COLLECTION,
    EXPERIMENT_RUN_TYPE_ID,
    EXPERIMENT_SPEC_SCHEMA_VERSION,
    EXPERIMENT_SPEC_TYPE_ID,
    EXPERIMENT_START_RUN_ACTION_ID,
    EXPERIMENT_VERDICT_FAIL,
    EXPERIMENT_VERDICT_INCONCLUSIVE,
    EXPERIMENT_VERDICT_PARTIAL,
    EXPERIMENT_VERDICT_PASS,
    EXPERIMENT_VERDICTS,
    HAS_EXPERIMENT_VERDICT_PREDICATE_ID,
    HAS_EXPECTED_OUTCOME_PREDICATE_ID,
    HAS_OBSERVED_OUTCOME_PREDICATE_ID,
    INCLUDES_THEORY_PREDICATE_ID,
    TESTING_EXPERIMENT_SCENARIO_TEMPLATE_SCHEMA_VERSION,
    TESTING_REGRESSION_SUITE_POLICY_SCHEMA_VERSION,
    TESTS_CAPABILITY_PREDICATE_ID,
    TESTS_WORKFLOW_PREDICATE_ID,
)
from .text_value_service import upsert_singleton_text_relation, upsert_text_for_concept
from .workflow_prediction_service import build_workflow_prediction_envelope
from .workflow_selection_experience import finalise_selection_experience
from .workflow_vontology_materialisation_helpers import stable_named_instance_concept_id
from ..workflows.workflow_concept_authority_service import (
    upsert_workflow_publication_lifecycle,
)

logger = logging.getLogger(__name__)

_RUN_INDEXES_READY = False
_SCENARIO_TEMPLATE_OMIT = object()
_DEFAULT_REGRESSION_SUITE_POLICY: dict[str, Any] = {
    "schema_version": TESTING_REGRESSION_SUITE_POLICY_SCHEMA_VERSION,
    "default_execution_tier": "tier1",
    "tiers": {
        "tier1": {"mode": "cases"},
        "tier2": {
            "mode": "benchmark",
            "output_root_default": "data/testing_workflows/benchmarks",
        },
        "benchmark": {"alias_for": "tier2"},
        "tier_2": {"alias_for": "tier2"},
    },
}
_MEETING_INVITATION_SCENARIO_TEMPLATE: dict[str, Any] = {
    "schema_version": TESTING_EXPERIMENT_SCENARIO_TEMPLATE_SCHEMA_VERSION,
    "name": "Meeting invitation workflow derivation and validation",
    "description": (
        "Testing spec for deriving, selecting, and validating a meeting-invitation workflow."
    ),
    "fixture_payload": {
        "invitation_text": {"$input": "invitation_text"},
    },
    "expected_outcomes": [
        {
            "label": "meeting_type_classification",
            "expected": {
                "$input": "expected_meeting_type",
                "$default": "meeting_workflow_candidate",
            },
        },
        {
            "label": "structured_meeting_fields",
            "expected_fields": {
                "$input": "expected_structure_fields",
                "$default": ["title", "time", "participants"],
            },
        },
        {
            "label": "mutation_safety",
            "expected": "no_canonical_mutations_without_gate",
        },
        {
            "$if_input": "expected_downstream_actions",
            "then": {
                "label": "downstream_actions",
                "expected_actions": {"$input": "expected_downstream_actions"},
            },
        },
    ],
    "theory_setup": {
        "seed_claims": [
            {
                "source_id": "#V#meeting_invitation_testing_workflow",
                "predicate": "#V#has_hypothesis",
                "target": {
                    "$input": "expected_meeting_type",
                    "$default": "meeting invitation should resolve to a safe workflow candidate",
                },
                "target_kind": "text",
            },
            {
                "source_id": "#V#meeting_invitation_testing_workflow",
                "predicate": "#V#has_assumption",
                "target": "No canonical calendar or task mutation is permitted during testing.",
                "target_kind": "text",
            },
        ],
    },
    "forbidden_side_effects": [
        "canonical_calendar_mutation",
        "canonical_task_mutation",
        "external_action_without_gate",
    ],
    "verdict_rules": {
        "require_all_expected_outcomes": True,
    },
    "replay_policy": {
        "retain_failing_cases": True,
        "retain_passing_cases": False,
    },
    "promotion_policy": {
        "requires_manual_gate": True,
    },
    "metadata": {
        "scenario": "meeting_invitation_testing",
    },
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _safe_str(value: Any, *, limit: int | None = None) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif value is None:
        text = ""
    else:
        text = str(value).strip()
    return text[:limit] if limit is not None else text


def _clone_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): copy.deepcopy(item) for key, item in value.items()}


def _clone_sequence(value: Any) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return []
    return copy.deepcopy(list(value))


def _normalise_strings(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if isinstance(values, (bytes, bytearray)) or not isinstance(values, Sequence):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_int(
    value: Any,
    *,
    default: int = 0,
    minimum: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _coerce_float(
    value: Any,
    *,
    default: float = 0.0,
    minimum: float | None = None,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _normalise_assertion_classes(value: Any) -> list[str]:
    return _normalise_strings(value)


def _extend_unique_mapping_sequence(
    items: list[dict[str, Any]],
    values: Sequence[Any],
) -> list[dict[str, Any]]:
    existing_keys = {
        repr(sorted(item.items())) for item in items if isinstance(item, Mapping)
    }
    for value in values:
        if not isinstance(value, Mapping):
            continue
        payload = {str(key): copy.deepcopy(item) for key, item in value.items()}
        payload_key = repr(sorted(payload.items()))
        if payload_key in existing_keys:
            continue
        existing_keys.add(payload_key)
        items.append(payload)
    return items


def _append_unique_hints(
    items: list[dict[str, Any]],
    hints: Sequence[Any],
) -> list[dict[str, Any]]:
    seen = {
        (
            _safe_str(item.get("scope")),
            _safe_str(item.get("reason_code")),
            _safe_str(item.get("state_id")),
        )
        for item in items
        if isinstance(item, Mapping)
    }
    for value in hints:
        if not isinstance(value, Mapping):
            continue
        payload = {str(key): copy.deepcopy(item) for key, item in value.items()}
        key = (
            _safe_str(payload.get("scope")),
            _safe_str(payload.get("reason_code")),
            _safe_str(payload.get("state_id")),
        )
        if key in seen:
            continue
        seen.add(key)
        items.append(payload)
    return items


def _extract_numeric_summary_value(summary: Any, field: str) -> float | None:
    if not isinstance(summary, Mapping):
        return None
    value = summary.get(field)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _summarise_prediction_envelope(result: Mapping[str, Any] | None) -> dict[str, Any]:
    envelope = (
        result.get("prediction_envelope")
        if isinstance(result, Mapping)
        and isinstance(result.get("prediction_envelope"), Mapping)
        else {}
    )
    envelope_mapping = envelope if isinstance(envelope, Mapping) else {}
    duration = envelope.get("duration_ms") if isinstance(envelope, Mapping) else {}
    llm_usage = envelope.get("llm_usage") if isinstance(envelope, Mapping) else {}
    total_tokens = (
        llm_usage.get("total_tokens")
        if isinstance(llm_usage, Mapping) and isinstance(llm_usage.get("total_tokens"), Mapping)
        else {}
    )
    sample_window = result.get("sample_window") if isinstance(result, Mapping) else {}
    data_sufficiency = (
        envelope.get("data_sufficiency")
        if isinstance(envelope, Mapping) and isinstance(envelope.get("data_sufficiency"), Mapping)
        else {}
    )
    return {
        "candidate_trace_count": _coerce_int(
            (sample_window or {}).get("candidate_trace_count"),
            minimum=0,
        ),
        "matched_trace_count": _coerce_int(
            (sample_window or {}).get("matched_trace_count"),
            minimum=0,
        ),
        "completion_rate": _coerce_float(
            (envelope or {}).get("completion_rate"),
            default=0.0,
            minimum=0.0,
        ),
        "failure_rate": _coerce_float(
            (envelope or {}).get("failure_rate"),
            default=0.0,
            minimum=0.0,
        ),
        "duration_p50_ms": _extract_numeric_summary_value(duration, "p50"),
        "duration_p90_ms": _extract_numeric_summary_value(duration, "p90"),
        "total_tokens_p50": _extract_numeric_summary_value(total_tokens, "p50"),
        "quality_proxy": _clone_mapping(envelope_mapping.get("quality_proxy")),
        "data_sufficiency": _clone_mapping(data_sufficiency),
    }


def _normalise_verdict(value: Any) -> str | None:
    verdict = _safe_str(value).lower()
    if verdict in EXPERIMENT_VERDICTS:
        return verdict
    return None


def _has_template_input_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value) > 0
    return True


def _resolve_testing_template_value(
    value: Any,
    *,
    inputs: Mapping[str, Any],
) -> Any:
    if isinstance(value, Mapping):
        if "$input" in value:
            input_key = _safe_str(value.get("$input"))
            if input_key and _has_template_input_value(inputs.get(input_key)):
                return copy.deepcopy(inputs.get(input_key))
            if "$default" in value:
                return _resolve_testing_template_value(
                    value.get("$default"),
                    inputs=inputs,
                )
            return _SCENARIO_TEMPLATE_OMIT
        if "$if_input" in value and "then" in value:
            input_key = _safe_str(value.get("$if_input"))
            if input_key and _has_template_input_value(inputs.get(input_key)):
                return _resolve_testing_template_value(
                    value.get("then"),
                    inputs=inputs,
                )
            return _SCENARIO_TEMPLATE_OMIT
        resolved_mapping: dict[str, Any] = {}
        for key, item in value.items():
            resolved_item = _resolve_testing_template_value(item, inputs=inputs)
            if resolved_item is _SCENARIO_TEMPLATE_OMIT:
                continue
            resolved_mapping[str(key)] = resolved_item
        return resolved_mapping
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        resolved_items: list[Any] = []
        for item in value:
            resolved_item = _resolve_testing_template_value(item, inputs=inputs)
            if resolved_item is _SCENARIO_TEMPLATE_OMIT:
                continue
            resolved_items.append(resolved_item)
        return resolved_items
    return copy.deepcopy(value)


def _normalise_regression_suite_policy(
    suite_policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    policy = copy.deepcopy(_DEFAULT_REGRESSION_SUITE_POLICY)
    if not isinstance(suite_policy, Mapping):
        return policy

    schema_version = _safe_str(suite_policy.get("schema_version"))
    if schema_version:
        policy["schema_version"] = schema_version
    default_execution_tier = _safe_str(suite_policy.get("default_execution_tier"))
    if default_execution_tier:
        policy["default_execution_tier"] = default_execution_tier.lower()

    raw_tiers = suite_policy.get("tiers")
    if isinstance(raw_tiers, Mapping):
        merged_tiers = dict(policy.get("tiers") or {})
        for raw_tier_key, raw_tier_policy in raw_tiers.items():
            tier_key = _safe_str(raw_tier_key).lower()
            if not tier_key or not isinstance(raw_tier_policy, Mapping):
                continue
            existing_tier_policy = merged_tiers.get(tier_key)
            baseline = (
                dict(existing_tier_policy)
                if isinstance(existing_tier_policy, Mapping)
                else {}
            )
            baseline.update(copy.deepcopy(dict(raw_tier_policy)))
            merged_tiers[tier_key] = baseline
        policy["tiers"] = merged_tiers
    return policy


def _resolve_regression_suite_mode(
    *,
    execution_tier: str,
    suite_policy: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    resolved_policy = _normalise_regression_suite_policy(suite_policy)
    schema_version = _safe_str(resolved_policy.get("schema_version"))
    if (
        schema_version
        and schema_version != TESTING_REGRESSION_SUITE_POLICY_SCHEMA_VERSION
    ):
        return None, {
            "success": False,
            "error": "invalid_suite_policy_schema_version",
            "suite_policy_schema_version": schema_version,
        }

    tiers = resolved_policy.get("tiers")
    if not isinstance(tiers, Mapping):
        return None, {"success": False, "error": "suite_policy_tiers_required"}

    default_tier = (
        _safe_str(resolved_policy.get("default_execution_tier")).lower() or "tier1"
    )
    candidate_tier = _safe_str(execution_tier).lower() or default_tier
    visited: set[str] = set()
    resolved_tier = candidate_tier
    tier_policy: Mapping[str, Any] | None = None
    while resolved_tier:
        if resolved_tier in visited:
            return None, {
                "success": False,
                "error": "suite_policy_alias_cycle",
                "execution_tier": resolved_tier,
            }
        visited.add(resolved_tier)
        tier_policy = tiers.get(resolved_tier)
        if not isinstance(tier_policy, Mapping):
            if resolved_tier != default_tier:
                resolved_tier = default_tier
                continue
            return None, {
                "success": False,
                "error": "unknown_execution_tier",
                "execution_tier": candidate_tier,
            }
        alias_for = _safe_str(tier_policy.get("alias_for")).lower()
        if alias_for:
            resolved_tier = alias_for
            continue
        break

    if not isinstance(tier_policy, Mapping):
        return None, {
            "success": False,
            "error": "unknown_execution_tier",
            "execution_tier": candidate_tier,
        }

    suite_mode = _safe_str(tier_policy.get("mode")).lower() or "cases"
    return {
        "resolved_policy": resolved_policy,
        "execution_tier": resolved_tier,
        "suite_mode": suite_mode,
        "tier_policy": dict(tier_policy),
        "suite_policy_schema_version": (
            _safe_str(resolved_policy.get("schema_version"))
            or TESTING_REGRESSION_SUITE_POLICY_SCHEMA_VERSION
        ),
    }, None


def _coerce_datetime_string(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    text = _safe_str(value)
    return text or None


def _coerce_namespace_context(
    *,
    namespace: Any = None,
    user_id: Any = None,
    org_id: Any = None,
) -> tuple[str | None, str | None, str | None]:
    resolved_namespace = resolve_canonical_namespace(namespace, user_id, org_id)
    resolved_user_id = _safe_str(user_id) or None
    resolved_org_id = _safe_str(org_id) or None
    if resolved_namespace:
        try:
            parsed = parse_namespace(resolved_namespace)
        except ValueError:
            parsed = {}
        user_slug = _safe_str(parsed.get("user_id"))
        org_slug = _safe_str(parsed.get("org_id"))
        if user_slug:
            resolved_user_id = f"#V#{user_slug}"
        if org_slug:
            resolved_org_id = f"#V#{org_slug}"
    return resolved_namespace, resolved_user_id, resolved_org_id


def _get_concept_or_none(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id_exact(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _ensure_experiment_spec_concept(
    *,
    experiment_spec_id: str,
    name: str,
    description: str,
    user_id: str | None,
    org_id: str | None,
    namespace: str | None,
) -> tuple[dict[str, Any], bool]:
    existing = _get_concept_or_none(experiment_spec_id)
    if isinstance(existing, Mapping):
        return dict(existing), False

    created = concept_service.create_concept(
        name=name,
        concept_id=experiment_spec_id,
        description=description,
        parent_concept_ids=[EXPERIMENT_SPEC_TYPE_ID],
        create_as_instance=True,
        created_by_concept_id=user_id,
        organisation_concept_id=org_id,
        event_namespace=namespace,
    )
    upsert_singleton_text_relation(
        subject_concept_id=experiment_spec_id,
        predicate="hasDescription",
        text=description,
        lang="en-NZ",
        context={
            "source": "experiment_run_service",
            "reason": EXPERIMENT_CREATE_SPEC_ACTION_ID,
        },
        garbage_collect=True,
    )
    return created, True


def _ensure_experiment_run_concept(
    *,
    run_id: str,
    name: str,
    description: str,
    user_id: str | None,
    org_id: str | None,
    namespace: str | None,
) -> tuple[dict[str, Any], bool]:
    existing = _get_concept_or_none(run_id)
    if isinstance(existing, Mapping):
        return dict(existing), False

    created = concept_service.create_concept(
        name=name,
        concept_id=run_id,
        description=description,
        parent_concept_ids=[EXPERIMENT_RUN_TYPE_ID],
        create_as_instance=True,
        created_by_concept_id=user_id,
        organisation_concept_id=org_id,
        event_namespace=namespace,
    )
    upsert_singleton_text_relation(
        subject_concept_id=run_id,
        predicate="hasDescription",
        text=description,
        lang="en-NZ",
        context={
            "source": "experiment_run_service",
            "reason": EXPERIMENT_START_RUN_ACTION_ID,
        },
        garbage_collect=True,
    )
    return created, True


def _normalise_spec_state(
    raw: Mapping[str, Any] | None,
    *,
    experiment_spec_id: str,
) -> dict[str, Any]:
    payload = _clone_mapping(raw)
    payload["schema_version"] = EXPERIMENT_SPEC_SCHEMA_VERSION
    payload["experiment_spec_id"] = experiment_spec_id
    payload["target_workflow_ids"] = _normalise_strings(payload.get("target_workflow_ids"))
    payload["target_capability_ids"] = _normalise_strings(
        payload.get("target_capability_ids")
    )
    payload["candidate_workflow_ids"] = _normalise_strings(
        payload.get("candidate_workflow_ids")
    )
    payload["baseline_workflow_id"] = _safe_str(payload.get("baseline_workflow_id")) or None
    payload["expected_outcomes"] = _clone_sequence(payload.get("expected_outcomes"))
    payload["allowed_side_effects"] = _clone_sequence(payload.get("allowed_side_effects"))
    payload["forbidden_side_effects"] = _clone_sequence(
        payload.get("forbidden_side_effects")
    )
    payload["fixture_payload"] = _clone_mapping(payload.get("fixture_payload"))
    payload["theory_setup"] = _clone_mapping(payload.get("theory_setup"))
    payload["verdict_rules"] = _clone_mapping(payload.get("verdict_rules"))
    payload["replay_policy"] = _clone_mapping(payload.get("replay_policy"))
    payload["promotion_policy"] = _clone_mapping(payload.get("promotion_policy"))
    payload["metadata"] = _clone_mapping(payload.get("metadata"))
    return payload


def _normalise_observation(
    raw: Mapping[str, Any],
    *,
    now_iso: str,
) -> dict[str, Any] | None:
    observation_type = _safe_str(
        raw.get("observation_type") or raw.get("type") or raw.get("label"),
        limit=120,
    )
    if not observation_type:
        observation_type = "observation"

    expected_outcome = copy.deepcopy(raw.get("expected_outcome"))
    observed_outcome = copy.deepcopy(raw.get("observed_outcome"))
    explicit_verdict = _normalise_verdict(raw.get("verdict"))
    matched_expected = raw.get("matched_expected_outcome")
    if not isinstance(matched_expected, bool):
        matched_expected = None

    if explicit_verdict is not None:
        verdict = explicit_verdict
    elif isinstance(matched_expected, bool):
        verdict = EXPERIMENT_VERDICT_PASS if matched_expected else EXPERIMENT_VERDICT_FAIL
    elif "success" in raw:
        verdict = (
            EXPERIMENT_VERDICT_PASS
            if _coerce_bool(raw.get("success"))
            else EXPERIMENT_VERDICT_FAIL
        )
    elif expected_outcome is not None and observed_outcome is not None:
        verdict = (
            EXPERIMENT_VERDICT_PASS
            if expected_outcome == observed_outcome
            else EXPERIMENT_VERDICT_FAIL
        )
        matched_expected = expected_outcome == observed_outcome
    elif _coerce_bool(raw.get("partial")):
        verdict = EXPERIMENT_VERDICT_PARTIAL
    else:
        verdict = EXPERIMENT_VERDICT_INCONCLUSIVE

    return {
        "observation_id": _safe_str(raw.get("observation_id"))
        or f"experiment_obs_{uuid.uuid4().hex[:24]}",
        "observation_type": observation_type,
        "label": _safe_str(raw.get("label"), limit=200) or observation_type,
        "verdict": verdict,
        "expected_outcome": expected_outcome,
        "observed_outcome": observed_outcome,
        "matched_expected_outcome": matched_expected,
        "assertion_classes": _normalise_assertion_classes(raw.get("assertion_classes")),
        "evidence": _clone_mapping(raw.get("evidence")),
        "metrics": _clone_mapping(raw.get("metrics")),
        "policy_decisions": _clone_sequence(raw.get("policy_decisions")),
        "tool_invocations": _clone_sequence(raw.get("tool_invocations")),
        "workflow_execution": _clone_mapping(raw.get("workflow_execution")),
        "candidate_validation": _clone_mapping(raw.get("candidate_validation")),
        "trace_summary": _clone_mapping(raw.get("trace_summary")),
        "side_effect_audit": _clone_mapping(raw.get("side_effect_audit")),
        "repair_hints": _clone_sequence(raw.get("repair_hints")),
        "quality_signals": _clone_mapping(raw.get("quality_signals")),
        "degradation_assessment": _clone_mapping(raw.get("degradation_assessment")),
        "turn_execution_request_ids": _normalise_strings(
            raw.get("turn_execution_request_ids")
        ),
        "created_at_utc": _coerce_datetime_string(raw.get("created_at_utc")) or now_iso,
        "updated_at_utc": now_iso,
    }


def _normalise_observations(raw: Any, *, now_iso: str) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping):
        raw = [raw]
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence):
        return []

    observations: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        normalised = _normalise_observation(item, now_iso=now_iso)
        if normalised is not None:
            observations.append(normalised)
    return observations


def _normalise_run_state(
    raw: Mapping[str, Any] | None,
    *,
    run_id: str,
) -> dict[str, Any]:
    payload = _clone_mapping(raw)
    payload["schema_version"] = EXPERIMENT_RUN_SCHEMA_VERSION
    payload["run_id"] = run_id
    status = _safe_str(payload.get("status")).lower() or EXPERIMENT_RUN_STATUS_PENDING
    if status not in EXPERIMENT_RUN_STATUSES:
        status = EXPERIMENT_RUN_STATUS_PENDING
    payload["status"] = status
    payload["candidate_workflow_ids"] = _normalise_strings(
        payload.get("candidate_workflow_ids")
    )
    payload["target_workflow_ids"] = _normalise_strings(payload.get("target_workflow_ids"))
    payload["baseline_workflow_id"] = _safe_str(payload.get("baseline_workflow_id")) or None
    payload["turn_execution_request_ids"] = _normalise_strings(
        payload.get("turn_execution_request_ids")
    )
    payload["expected_outcomes"] = _clone_sequence(payload.get("expected_outcomes"))
    payload["observations"] = _normalise_observations(
        payload.get("observations"),
        now_iso=_safe_str(payload.get("updated_at_utc")) or _utcnow_iso(),
    )
    payload["metrics"] = _clone_mapping(payload.get("metrics"))
    payload["evidence"] = _clone_mapping(payload.get("evidence"))
    payload["verdict_summary"] = _clone_mapping(payload.get("verdict_summary"))
    payload["degradation_assessment"] = _clone_mapping(
        payload.get("degradation_assessment")
    )
    payload["promotion_recommendation"] = _clone_mapping(
        payload.get("promotion_recommendation")
    )
    payload["learning_signal"] = _clone_mapping(payload.get("learning_signal"))
    payload["replay_case"] = _clone_mapping(payload.get("replay_case"))
    payload["metadata"] = _clone_mapping(payload.get("metadata"))
    payload["selection_experience_id"] = _safe_str(
        payload.get("selection_experience_id")
    ) or None
    payload["benchmark_tier"] = _safe_str(payload.get("benchmark_tier")) or None
    payload["verdict"] = _normalise_verdict(payload.get("verdict"))
    return payload


def _persist_experiment_spec_state(
    *,
    experiment_spec_id: str,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    normalised = _normalise_spec_state(state, experiment_spec_id=experiment_spec_id)
    concept_service.update_concept(
        experiment_spec_id,
        {
            "concept_data.experiment_spec": normalised,
            "attributes.experiment_spec.updated_at_utc": normalised.get("updated_at_utc"),
            "attributes.experiment_spec.target_workflow_ids": normalised.get(
                "target_workflow_ids"
            ),
        },
    )
    return normalised


def _persist_experiment_run_state(
    *,
    run_id: str,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    normalised = _normalise_run_state(state, run_id=run_id)
    concept_service.update_concept(
        run_id,
        {
            "concept_data.experiment_run": normalised,
            "attributes.experiment_run.status": normalised.get("status"),
            "attributes.experiment_run.verdict": normalised.get("verdict"),
            "attributes.experiment_run.updated_at_utc": normalised.get("updated_at_utc"),
            "attributes.experiment_run.experiment_spec_id": normalised.get(
                "experiment_spec_id"
            ),
            "attributes.experiment_run.theory_id": normalised.get("theory_id"),
        },
    )
    return normalised


def _build_experiment_run_projection(state: Mapping[str, Any]) -> dict[str, Any]:
    return _normalise_run_state(state, run_id=_safe_str(state.get("run_id")))


def _ensure_run_indexes(collection: Any) -> None:
    global _RUN_INDEXES_READY
    if _RUN_INDEXES_READY:
        return

    try:
        existing_indexes = [idx.get("name") for idx in collection.list_indexes()]
        if "run_id_unique" not in existing_indexes:
            collection.create_index(
                [("run_id", ASCENDING)],
                unique=True,
                name="run_id_unique",
            )
        if "namespace_created_desc" not in existing_indexes:
            collection.create_index(
                [("namespace", ASCENDING), ("created_at_utc", DESCENDING)],
                name="namespace_created_desc",
            )
        if "spec_created_desc" not in existing_indexes:
            collection.create_index(
                [("experiment_spec_id", ASCENDING), ("created_at_utc", DESCENDING)],
                name="spec_created_desc",
            )
        if "theory_created_desc" not in existing_indexes:
            collection.create_index(
                [("theory_id", ASCENDING), ("created_at_utc", DESCENDING)],
                name="theory_created_desc",
            )
        if "workflow_created_desc" not in existing_indexes:
            collection.create_index(
                [("target_workflow_ids", ASCENDING), ("created_at_utc", DESCENDING)],
                name="workflow_created_desc",
            )
        if "verdict_created_desc" not in existing_indexes:
            collection.create_index(
                [("verdict", ASCENDING), ("created_at_utc", DESCENDING)],
                name="verdict_created_desc",
            )
    except OperationFailure as exc:
        logger.warning("Index creation partially failed for experiment_runs: %s", exc)
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not ensure experiment_runs indexes: %s", exc)
    _RUN_INDEXES_READY = True


def get_experiment_runs_collection():
    db = get_db()
    if db is None:
        return None
    coll = db[EXPERIMENT_RUNS_COLLECTION]
    _ensure_run_indexes(coll)
    return coll


def upsert_experiment_run_projection(
    *,
    record: Mapping[str, Any],
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {"updated": False, "reason": "invalid_record"}

    run_id = _safe_str(record.get("run_id"))
    if not run_id:
        return {"updated": False, "reason": "missing_run_id"}

    coll = get_experiment_runs_collection()
    if coll is None:
        return {"updated": False, "reason": "collection_unavailable"}

    now = _utcnow()
    payload = _build_experiment_run_projection(record)
    payload["run_id"] = run_id
    if _safe_str(namespace):
        payload.setdefault("namespace", _safe_str(namespace))
    if _safe_str(user_id):
        payload.setdefault("user_id", _safe_str(user_id))
    if _safe_str(org_id):
        payload.setdefault("org_id", _safe_str(org_id))
    payload.setdefault("schema_version", EXPERIMENT_RUN_SCHEMA_VERSION)
    payload.setdefault("created_at_utc", now.isoformat())
    payload["updated_at_utc"] = now.isoformat()

    try:
        result = coll.update_one(
            {"run_id": run_id},
            {"$set": payload, "$setOnInsert": {"inserted_at": now}},
            upsert=True,
        )
    except PyMongoError as exc:
        logger.warning(
            "Failed to upsert experiment_runs projection for run_id=%s: %s",
            run_id,
            exc,
        )
        return {"updated": False, "reason": "mongo_error", "run_id": run_id}

    updated = bool(getattr(result, "modified_count", 0) > 0)
    inserted = getattr(result, "upserted_id", None) is not None
    matched = bool(getattr(result, "matched_count", 0) > 0)
    return {
        "updated": updated or inserted,
        "inserted": inserted,
        "matched": matched,
        "run_id": run_id,
    }


def get_experiment_spec_state(experiment_spec_id: str) -> dict[str, Any] | None:
    resolved_spec_id = _safe_str(experiment_spec_id)
    if not resolved_spec_id:
        return None
    concept = _get_concept_or_none(resolved_spec_id)
    if not isinstance(concept, Mapping):
        return None
    concept_data = concept.get("concept_data")
    if not isinstance(concept_data, Mapping):
        return None
    state = concept_data.get("experiment_spec")
    if not isinstance(state, Mapping):
        return None
    return _normalise_spec_state(state, experiment_spec_id=resolved_spec_id)


def get_experiment_run_state(run_id: str) -> dict[str, Any] | None:
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id:
        return None
    concept = _get_concept_or_none(resolved_run_id)
    if not isinstance(concept, Mapping):
        return None
    concept_data = concept.get("concept_data")
    if not isinstance(concept_data, Mapping):
        return None
    state = concept_data.get("experiment_run")
    if not isinstance(state, Mapping):
        return None
    return _normalise_run_state(state, run_id=resolved_run_id)


def get_experiment_run_projection(run_id: str) -> dict[str, Any] | None:
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id:
        return None

    coll = get_experiment_runs_collection()
    if coll is not None:
        doc = coll.find_one({"run_id": resolved_run_id}, {"_id": 0})
        if isinstance(doc, Mapping):
            return dict(doc)

    state = get_experiment_run_state(resolved_run_id)
    if state is None:
        return None
    return _build_experiment_run_projection(state)


def _record_relation(
    *,
    source_id: str,
    predicate: str,
    targets: Sequence[str],
) -> list[dict[str, Any]]:
    relations: list[dict[str, Any]] = []
    for target in _normalise_strings(targets):
        try:
            result = add_relationship(source_id=source_id, predicate=predicate, target=target)
        except Exception as exc:
            logger.warning(
                "[experiment_run] could not add relation %s %s %s: %s",
                source_id,
                predicate,
                target,
                exc,
            )
            continue
        if isinstance(result, Mapping):
            relations.append(dict(result))
    return relations


def _record_text_outcomes(
    *,
    source_id: str,
    predicate: str,
    values: Sequence[Any],
) -> None:
    for value in values:
        text = _safe_str(value, limit=2000)
        if not text:
            continue
        try:
            upsert_text_for_concept(
                subject_concept_id=source_id,
                predicate=predicate,
                text=text,
                lang="en-NZ",
                context={"source": "experiment_run_service", "predicate": predicate},
            )
        except Exception as exc:
            logger.warning(
                "[experiment_run] could not upsert %s text for %s: %s",
                predicate,
                source_id,
                exc,
            )


def create_experiment_spec(
    *,
    name: str,
    experiment_spec_id: str | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    description: str | None = None,
    target_workflow_ids: Sequence[str] = (),
    target_capability_ids: Sequence[str] = (),
    candidate_workflow_ids: Sequence[str] = (),
    baseline_workflow_id: str | None = None,
    theory_id: str | None = None,
    experiment_suite_id: str | None = None,
    fixture_payload: Mapping[str, Any] | None = None,
    theory_setup: Mapping[str, Any] | None = None,
    expected_outcomes: Sequence[Any] = (),
    allowed_side_effects: Sequence[Any] = (),
    forbidden_side_effects: Sequence[Any] = (),
    verdict_rules: Mapping[str, Any] | None = None,
    replay_policy: Mapping[str, Any] | None = None,
    promotion_policy: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        from .testing_workflow_vontology_service import (
            ensure_testing_type_concept_support,
        )

        ensure_testing_type_concept_support()
    except Exception:
        pass

    resolved_namespace, resolved_user_id, resolved_org_id = _coerce_namespace_context(
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
    )
    spec_name = _safe_str(name, limit=200) or "Testing experiment"
    resolved_spec_id = _safe_str(experiment_spec_id) or stable_named_instance_concept_id(
        spec_name,
        prefix="experiment_spec",
    )
    description_text = _safe_str(description, limit=1200) or f"Experiment spec for {spec_name}."
    now_iso = _utcnow_iso()

    _ensure_experiment_spec_concept(
        experiment_spec_id=resolved_spec_id,
        name=spec_name,
        description=description_text,
        user_id=resolved_user_id,
        org_id=resolved_org_id,
        namespace=resolved_namespace,
    )

    state = {
        "schema_version": EXPERIMENT_SPEC_SCHEMA_VERSION,
        "experiment_spec_id": resolved_spec_id,
        "label": spec_name,
        "description": description_text,
        "namespace": resolved_namespace,
        "user_id": resolved_user_id,
        "org_id": resolved_org_id,
        "theory_id": _safe_str(theory_id) or None,
        "experiment_suite_id": _safe_str(experiment_suite_id) or None,
        "target_workflow_ids": _normalise_strings(target_workflow_ids),
        "target_capability_ids": _normalise_strings(target_capability_ids),
        "candidate_workflow_ids": _normalise_strings(candidate_workflow_ids),
        "baseline_workflow_id": _safe_str(baseline_workflow_id) or None,
        "fixture_payload": _clone_mapping(fixture_payload),
        "theory_setup": _clone_mapping(theory_setup),
        "expected_outcomes": _clone_sequence(expected_outcomes),
        "allowed_side_effects": _clone_sequence(allowed_side_effects),
        "forbidden_side_effects": _clone_sequence(forbidden_side_effects),
        "verdict_rules": _clone_mapping(verdict_rules),
        "replay_policy": _clone_mapping(replay_policy),
        "promotion_policy": _clone_mapping(promotion_policy),
        "metadata": _clone_mapping(metadata),
        "created_at_utc": now_iso,
        "updated_at_utc": now_iso,
    }
    persisted = _persist_experiment_spec_state(
        experiment_spec_id=resolved_spec_id,
        state=state,
    )

    linked_relations: list[dict[str, Any]] = []
    linked_relations.extend(
        _record_relation(
            source_id=resolved_spec_id,
            predicate=TESTS_WORKFLOW_PREDICATE_ID,
            targets=persisted.get("target_workflow_ids") or [],
        )
    )
    linked_relations.extend(
        _record_relation(
            source_id=resolved_spec_id,
            predicate=TESTS_CAPABILITY_PREDICATE_ID,
            targets=persisted.get("target_capability_ids") or [],
        )
    )
    if _safe_str(theory_id):
        linked_relations.extend(
            _record_relation(
                source_id=resolved_spec_id,
                predicate=INCLUDES_THEORY_PREDICATE_ID,
                targets=[_safe_str(theory_id)],
            )
        )
    if _safe_str(experiment_suite_id):
        linked_relations.extend(
            _record_relation(
                source_id=resolved_spec_id,
                predicate=BELONGS_TO_EXPERIMENT_SUITE_PREDICATE_ID,
                targets=[_safe_str(experiment_suite_id)],
            )
        )
    _record_text_outcomes(
        source_id=resolved_spec_id,
        predicate=HAS_EXPECTED_OUTCOME_PREDICATE_ID,
        values=persisted.get("expected_outcomes") or [],
    )

    return {
        "success": True,
        "experiment_spec_id": resolved_spec_id,
        "experiment_spec": persisted,
        "linked_relations": linked_relations,
    }


def _build_run_id() -> str:
    return f"#V#experiment_run_{uuid.uuid4().hex[:24]}"


def start_experiment_run(
    *,
    experiment_spec_id: str,
    run_id: str | None = None,
    theory_id: str | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    target_workflow_ids: Sequence[str] = (),
    candidate_workflow_ids: Sequence[str] = (),
    baseline_workflow_id: str | None = None,
    benchmark_tier: str | None = None,
    benchmark_world_id: str | None = None,
    selection_experience_id: str | None = None,
    turn_execution_request_ids: Sequence[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        from .testing_workflow_vontology_service import (
            ensure_testing_type_concept_support,
        )

        ensure_testing_type_concept_support()
    except Exception:
        pass

    resolved_spec_id = _safe_str(experiment_spec_id)
    if not resolved_spec_id:
        return {"success": False, "error": "experiment_spec_id_required"}

    spec_state = get_experiment_spec_state(resolved_spec_id)
    if spec_state is None:
        return {"success": False, "error": "experiment_spec_not_found"}

    resolved_namespace, resolved_user_id, resolved_org_id = _coerce_namespace_context(
        namespace=namespace or spec_state.get("namespace"),
        user_id=user_id or spec_state.get("user_id"),
        org_id=org_id or spec_state.get("org_id"),
    )
    resolved_run_id = _safe_str(run_id) or _build_run_id()
    label = _safe_str(spec_state.get("label")) or resolved_spec_id
    description = f"Experiment run for {label}."
    now_iso = _utcnow_iso()

    _ensure_experiment_run_concept(
        run_id=resolved_run_id,
        name=f"{label} run",
        description=description,
        user_id=resolved_user_id,
        org_id=resolved_org_id,
        namespace=resolved_namespace,
    )

    state = {
        "schema_version": EXPERIMENT_RUN_SCHEMA_VERSION,
        "run_id": resolved_run_id,
        "experiment_spec_id": resolved_spec_id,
        "label": f"{label} run",
        "description": description,
        "namespace": resolved_namespace,
        "user_id": resolved_user_id,
        "org_id": resolved_org_id,
        "theory_id": _safe_str(theory_id) or _safe_str(spec_state.get("theory_id")) or None,
        "benchmark_tier": _safe_str(benchmark_tier) or "tier1",
        "benchmark_world_id": _safe_str(benchmark_world_id) or None,
        "target_workflow_ids": _normalise_strings(
            target_workflow_ids or spec_state.get("target_workflow_ids") or []
        ),
        "candidate_workflow_ids": _normalise_strings(
            candidate_workflow_ids or spec_state.get("candidate_workflow_ids") or []
        ),
        "baseline_workflow_id": _safe_str(baseline_workflow_id)
        or _safe_str(spec_state.get("baseline_workflow_id"))
        or None,
        "expected_outcomes": _clone_sequence(spec_state.get("expected_outcomes")),
        "turn_execution_request_ids": _normalise_strings(turn_execution_request_ids),
        "selection_experience_id": _safe_str(selection_experience_id)
        or _safe_str(spec_state.get("selection_experience_id"))
        or None,
        "observations": [],
        "metrics": {},
        "evidence": {
            "tool_invocations": [],
            "workflow_execution": [],
            "policy_decisions": [],
            "candidate_validation_results": [],
            "trace_summaries": [],
            "side_effect_audits": [],
            "repair_hints": [],
            "quality_signals": [],
            "assertion_classes": [],
            "suite_runs": [],
        },
        "metadata": _clone_mapping(metadata),
        "status": EXPERIMENT_RUN_STATUS_RUNNING,
        "verdict": None,
        "created_at_utc": now_iso,
        "started_at_utc": now_iso,
        "updated_at_utc": now_iso,
    }
    persisted = _persist_experiment_run_state(run_id=resolved_run_id, state=state)
    projection = upsert_experiment_run_projection(
        record=persisted,
        namespace=resolved_namespace,
        user_id=resolved_user_id,
        org_id=resolved_org_id,
    )

    linked_relations: list[dict[str, Any]] = []
    if persisted.get("theory_id"):
        linked_relations.extend(
            _record_relation(
                source_id=resolved_run_id,
                predicate=INCLUDES_THEORY_PREDICATE_ID,
                targets=[_safe_str(persisted.get("theory_id"))],
            )
        )
    linked_relations.extend(
        _record_relation(
            source_id=resolved_run_id,
            predicate=TESTS_WORKFLOW_PREDICATE_ID,
            targets=persisted.get("target_workflow_ids") or [],
        )
    )

    return {
        "success": True,
        "run_id": resolved_run_id,
        "experiment_run": persisted,
        "projection": projection,
        "linked_relations": linked_relations,
    }


def record_experiment_observation(
    *,
    run_id: str,
    observations: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    turn_execution_request_ids: Sequence[str] = (),
) -> dict[str, Any]:
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id:
        return {"success": False, "error": "run_id_required"}

    state = get_experiment_run_state(resolved_run_id)
    if state is None:
        return {"success": False, "error": "experiment_run_not_found"}

    now_iso = _utcnow_iso()
    appended = _normalise_observations(observations, now_iso=now_iso)
    if not appended:
        return {"success": False, "error": "no_valid_observations"}

    observed_outcomes: list[Any] = []
    evidence: dict[str, Any] = (
        dict(cast(Mapping[str, Any], state.get("evidence")))
        if isinstance(state.get("evidence"), Mapping)
        else {}
    )
    tool_invocations: list[Any] = list(
        cast(Sequence[Any], evidence.get("tool_invocations") or [])
    )
    workflow_execution: list[dict[str, Any]] = [
        copy.deepcopy(dict(item))
        for item in cast(Sequence[Any], evidence.get("workflow_execution") or [])
        if isinstance(item, Mapping)
    ]
    policy_decisions: list[Any] = list(
        cast(Sequence[Any], evidence.get("policy_decisions") or [])
    )
    candidate_validation_results: list[dict[str, Any]] = [
        copy.deepcopy(dict(item))
        for item in cast(Sequence[Any], evidence.get("candidate_validation_results") or [])
        if isinstance(item, Mapping)
    ]
    trace_summaries: list[dict[str, Any]] = [
        copy.deepcopy(dict(item))
        for item in cast(Sequence[Any], evidence.get("trace_summaries") or [])
        if isinstance(item, Mapping)
    ]
    side_effect_audits: list[dict[str, Any]] = [
        copy.deepcopy(dict(item))
        for item in cast(Sequence[Any], evidence.get("side_effect_audits") or [])
        if isinstance(item, Mapping)
    ]
    repair_hints: list[dict[str, Any]] = [
        copy.deepcopy(dict(item))
        for item in cast(Sequence[Any], evidence.get("repair_hints") or [])
        if isinstance(item, Mapping)
    ]
    quality_signals: list[dict[str, Any]] = [
        copy.deepcopy(dict(item))
        for item in cast(Sequence[Any], evidence.get("quality_signals") or [])
        if isinstance(item, Mapping)
    ]
    assertion_classes = _normalise_assertion_classes(evidence.get("assertion_classes"))
    turn_ids = _normalise_strings(
        [
            *state.get("turn_execution_request_ids", []),
            *turn_execution_request_ids,
            *[
                request_id
                for item in appended
                for request_id in item.get("turn_execution_request_ids") or []
            ],
        ]
    )

    for item in appended:
        observed_outcome = item.get("observed_outcome")
        if observed_outcome is not None:
            observed_outcomes.append(observed_outcome)
        tool_invocations.extend(_clone_sequence(item.get("tool_invocations")))
        workflow_payload = item.get("workflow_execution")
        if isinstance(workflow_payload, Mapping) and workflow_payload:
            workflow_execution.append(copy.deepcopy(dict(workflow_payload)))
        policy_decisions.extend(_clone_sequence(item.get("policy_decisions")))
        candidate_validation = item.get("candidate_validation")
        if isinstance(candidate_validation, Mapping) and candidate_validation:
            candidate_validation_results = _extend_unique_mapping_sequence(
                candidate_validation_results,
                [candidate_validation],
            )
        trace_summary = item.get("trace_summary")
        if isinstance(trace_summary, Mapping) and trace_summary:
            trace_summaries = _extend_unique_mapping_sequence(
                trace_summaries,
                [trace_summary],
            )
        side_effect_audit = item.get("side_effect_audit")
        if isinstance(side_effect_audit, Mapping) and side_effect_audit:
            side_effect_audits = _extend_unique_mapping_sequence(
                side_effect_audits,
                [side_effect_audit],
            )
        repair_hints = _append_unique_hints(
            repair_hints,
            _clone_sequence(item.get("repair_hints")),
        )
        quality_signal = item.get("quality_signals")
        if isinstance(quality_signal, Mapping) and quality_signal:
            quality_signals = _extend_unique_mapping_sequence(
                quality_signals,
                [quality_signal],
            )
        assertion_classes = _normalise_assertion_classes(
            [*assertion_classes, *(item.get("assertion_classes") or [])]
        )

    state["observations"] = [*(state.get("observations") or []), *appended]
    state["turn_execution_request_ids"] = turn_ids
    state["evidence"] = {
        **evidence,
        "tool_invocations": tool_invocations,
        "workflow_execution": workflow_execution,
        "policy_decisions": policy_decisions,
        "candidate_validation_results": candidate_validation_results,
        "trace_summaries": trace_summaries,
        "side_effect_audits": side_effect_audits,
        "repair_hints": repair_hints,
        "quality_signals": quality_signals,
        "assertion_classes": assertion_classes,
    }
    state["updated_at_utc"] = now_iso
    persisted = _persist_experiment_run_state(run_id=resolved_run_id, state=state)
    projection = upsert_experiment_run_projection(
        record=persisted,
        namespace=_safe_str(persisted.get("namespace")) or None,
        user_id=_safe_str(persisted.get("user_id")) or None,
        org_id=_safe_str(persisted.get("org_id")) or None,
    )
    _record_text_outcomes(
        source_id=resolved_run_id,
        predicate=HAS_OBSERVED_OUTCOME_PREDICATE_ID,
        values=observed_outcomes,
    )

    return {
        "success": True,
        "run_id": resolved_run_id,
        "recorded_observations": appended,
        "experiment_run": persisted,
        "projection": projection,
    }


def _compute_observation_verdict_counts(
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts = {
        EXPERIMENT_VERDICT_PASS: 0,
        EXPERIMENT_VERDICT_FAIL: 0,
        EXPERIMENT_VERDICT_PARTIAL: 0,
        EXPERIMENT_VERDICT_INCONCLUSIVE: 0,
    }
    for item in observations:
        verdict = _normalise_verdict(item.get("verdict"))
        if verdict is None:
            verdict = EXPERIMENT_VERDICT_INCONCLUSIVE
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


def _has_forbidden_side_effect(
    *,
    spec_state: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
) -> bool:
    forbidden_terms = {
        _safe_str(item).lower()
        for item in spec_state.get("forbidden_side_effects") or []
        if _safe_str(item)
    }
    if not forbidden_terms:
        return False

    for observation in observations:
        evidence = observation.get("evidence")
        evidence_dict = evidence if isinstance(evidence, Mapping) else {}
        policy_decisions = observation.get("policy_decisions")
        policy_list = policy_decisions if isinstance(policy_decisions, list) else []
        observed = []
        observed.extend(_normalise_strings(evidence_dict.get("side_effects")))
        observed.extend(
            _safe_str(item.get("decision") or item.get("type"))
            for item in policy_list
            if isinstance(item, Mapping)
        )
        for item in observed:
            if item.lower() in forbidden_terms:
                return True
    return False


def _count_policy_violation_events(observations: Sequence[Mapping[str, Any]]) -> int:
    violations = 0
    for observation in observations:
        side_effect_audit = observation.get("side_effect_audit")
        if isinstance(side_effect_audit, Mapping):
            violations += _coerce_int(
                side_effect_audit.get("blocked_event_count"),
                minimum=0,
            )
        candidate_validation = observation.get("candidate_validation")
        if isinstance(candidate_validation, Mapping) and candidate_validation.get("valid") is False:
            violations += 1
    return violations


def _build_degradation_assessment(
    *,
    spec_state: Mapping[str, Any],
    run_state: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    candidate_workflow_id = _safe_str(
        next(iter(run_state.get("target_workflow_ids") or []), None)
    ) or _safe_str(next(iter(run_state.get("candidate_workflow_ids") or []), None))
    baseline_workflow_id = _safe_str(run_state.get("baseline_workflow_id")) or _safe_str(
        spec_state.get("baseline_workflow_id")
    )
    namespace = _safe_str(run_state.get("namespace")) or None
    policy = _clone_mapping(
        (_clone_mapping(spec_state.get("promotion_policy"))).get("degradation_policy")
    ) or _clone_mapping((_clone_mapping(spec_state.get("verdict_rules"))).get("degradation_policy"))
    thresholds = {
        "minimum_baseline_runs": _coerce_int(
            policy.get("minimum_baseline_runs"),
            default=3,
            minimum=1,
        ),
        "max_completion_rate_drop": _coerce_float(
            policy.get("max_completion_rate_drop"),
            default=0.10,
            minimum=0.0,
        ),
        "max_failure_rate_increase": _coerce_float(
            policy.get("max_failure_rate_increase"),
            default=0.10,
            minimum=0.0,
        ),
        "max_p50_duration_ratio": _coerce_float(
            policy.get("max_p50_duration_ratio"),
            default=1.5,
            minimum=1.0,
        ),
        "max_total_token_ratio": _coerce_float(
            policy.get("max_total_token_ratio"),
            default=1.5,
            minimum=1.0,
        ),
        "max_policy_violation_count": _coerce_int(
            policy.get("max_policy_violation_count"),
            default=0,
            minimum=0,
        ),
    }
    assessment: dict[str, Any] = {
        "evaluated": False,
        "degraded": False,
        "candidate_workflow_id": candidate_workflow_id or None,
        "baseline_workflow_id": baseline_workflow_id or None,
        "thresholds": thresholds,
        "reasons": [],
        "policy_violation_count": _count_policy_violation_events(observations),
    }
    if not candidate_workflow_id or not baseline_workflow_id:
        assessment["reason"] = "baseline_or_candidate_workflow_missing"
        return assessment

    try:
        candidate_envelope = build_workflow_prediction_envelope(
            workflow_id=candidate_workflow_id,
            namespace=namespace,
        )
        baseline_envelope = build_workflow_prediction_envelope(
            workflow_id=baseline_workflow_id,
            namespace=namespace,
        )
    except Exception as exc:
        logger.warning(
            "[experiment_run] could not compute degradation envelopes for %s vs %s: %s",
            candidate_workflow_id,
            baseline_workflow_id,
            exc,
        )
        assessment["reason"] = "prediction_envelope_lookup_failed"
        assessment["error"] = f"{type(exc).__name__}: {exc}"
        return assessment

    candidate_summary = _summarise_prediction_envelope(candidate_envelope)
    baseline_summary = _summarise_prediction_envelope(baseline_envelope)
    assessment["candidate_envelope"] = candidate_summary
    assessment["baseline_envelope"] = baseline_summary

    baseline_runs = _coerce_int(baseline_summary.get("matched_trace_count"), minimum=0)
    if baseline_runs < thresholds["minimum_baseline_runs"]:
        assessment["reason"] = "insufficient_baseline_history"
        return assessment

    reasons: list[str] = []
    completion_drop = _coerce_float(
        baseline_summary.get("completion_rate"),
        default=0.0,
        minimum=0.0,
    ) - _coerce_float(candidate_summary.get("completion_rate"), default=0.0, minimum=0.0)
    failure_increase = _coerce_float(
        candidate_summary.get("failure_rate"),
        default=0.0,
        minimum=0.0,
    ) - _coerce_float(baseline_summary.get("failure_rate"), default=0.0, minimum=0.0)
    candidate_p50 = candidate_summary.get("duration_p50_ms")
    baseline_p50 = baseline_summary.get("duration_p50_ms")
    duration_ratio = None
    if isinstance(candidate_p50, (int, float)) and isinstance(baseline_p50, (int, float)) and baseline_p50 > 0:
        duration_ratio = round(float(candidate_p50) / float(baseline_p50), 4)
    candidate_tokens = candidate_summary.get("total_tokens_p50")
    baseline_tokens = baseline_summary.get("total_tokens_p50")
    token_ratio = None
    if isinstance(candidate_tokens, (int, float)) and isinstance(baseline_tokens, (int, float)) and baseline_tokens > 0:
        token_ratio = round(float(candidate_tokens) / float(baseline_tokens), 4)

    if completion_drop > thresholds["max_completion_rate_drop"]:
        reasons.append("completion_rate_regressed")
    if failure_increase > thresholds["max_failure_rate_increase"]:
        reasons.append("failure_rate_regressed")
    if duration_ratio is not None and duration_ratio > thresholds["max_p50_duration_ratio"]:
        reasons.append("duration_regressed")
    if token_ratio is not None and token_ratio > thresholds["max_total_token_ratio"]:
        reasons.append("cost_regressed")
    if assessment["policy_violation_count"] > thresholds["max_policy_violation_count"]:
        reasons.append("policy_violation_budget_exceeded")

    assessment["evaluated"] = True
    assessment["degraded"] = bool(reasons)
    assessment["reasons"] = reasons
    assessment["metrics"] = {
        "completion_rate_drop": round(completion_drop, 4),
        "failure_rate_increase": round(failure_increase, 4),
        "duration_ratio": duration_ratio,
        "token_ratio": token_ratio,
    }
    if reasons:
        try:
            lifecycle = upsert_workflow_publication_lifecycle(
                workflow_id=candidate_workflow_id,
                phase="demoted_due_to_degradation",
                published=False,
                validation_passed=False,
                postconditions_verified=False,
                last_error=";".join(reasons),
            )
        except Exception as exc:
            logger.warning(
                "[experiment_run] could not demote degraded workflow %s: %s",
                candidate_workflow_id,
                exc,
            )
            assessment["demotion"] = {
                "applied": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            assessment["demotion"] = {"applied": True, "lifecycle": lifecycle}
    return assessment


def compute_experiment_verdict(
    *,
    run_id: str,
) -> dict[str, Any]:
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id:
        return {"success": False, "error": "run_id_required"}

    state = get_experiment_run_state(resolved_run_id)
    if state is None:
        return {"success": False, "error": "experiment_run_not_found"}

    spec_state = get_experiment_spec_state(_safe_str(state.get("experiment_spec_id")))
    if spec_state is None:
        return {"success": False, "error": "experiment_spec_not_found"}

    observations = [
        item for item in (state.get("observations") or []) if isinstance(item, Mapping)
    ]
    counts = _compute_observation_verdict_counts(observations)
    total = sum(counts.values())
    require_all_expected = _coerce_bool(
        (spec_state.get("verdict_rules") or {}).get("require_all_expected_outcomes"),
        default=False,
    )
    minimum_pass_count = _coerce_int(
        (spec_state.get("verdict_rules") or {}).get("minimum_pass_count"),
        default=1,
        minimum=1,
    )
    expected_outcome_count = len(spec_state.get("expected_outcomes") or [])
    pass_count = counts.get(EXPERIMENT_VERDICT_PASS, 0)
    fail_count = counts.get(EXPERIMENT_VERDICT_FAIL, 0)
    partial_count = counts.get(EXPERIMENT_VERDICT_PARTIAL, 0)
    inconclusive_count = counts.get(EXPERIMENT_VERDICT_INCONCLUSIVE, 0)
    has_forbidden_side_effect = _has_forbidden_side_effect(
        spec_state=spec_state,
        observations=observations,
    )
    degradation_assessment = _build_degradation_assessment(
        spec_state=spec_state,
        run_state=state,
        observations=observations,
    )

    if total == 0:
        verdict = EXPERIMENT_VERDICT_INCONCLUSIVE
        reason = "no_observations_recorded"
    elif has_forbidden_side_effect:
        verdict = EXPERIMENT_VERDICT_FAIL
        reason = "forbidden_side_effect_observed"
    elif degradation_assessment.get("degraded") is True:
        verdict = EXPERIMENT_VERDICT_FAIL
        reason = "degraded_against_baseline"
    elif fail_count == 0 and partial_count == 0 and inconclusive_count == 0:
        if require_all_expected and expected_outcome_count > 0 and pass_count < expected_outcome_count:
            verdict = EXPERIMENT_VERDICT_INCONCLUSIVE
            reason = "expected_outcomes_not_fully_observed"
        elif pass_count >= minimum_pass_count:
            verdict = EXPERIMENT_VERDICT_PASS
            reason = "all_recorded_observations_passed"
        else:
            verdict = EXPERIMENT_VERDICT_INCONCLUSIVE
            reason = "minimum_pass_count_not_met"
    elif fail_count > 0 and pass_count == 0 and partial_count == 0:
        verdict = EXPERIMENT_VERDICT_FAIL
        reason = "all_recorded_observations_failed"
    elif pass_count > 0 or partial_count > 0:
        verdict = EXPERIMENT_VERDICT_PARTIAL
        reason = "mixed_or_partial_observation_outcomes"
    else:
        verdict = EXPERIMENT_VERDICT_INCONCLUSIVE
        reason = "insufficient_observation_signal"

    promotion_ready_assertion_ids: list[str] = []
    theory_id = _safe_str(state.get("theory_id"))
    if theory_id and verdict == EXPERIMENT_VERDICT_PASS:
        try:
            diff_result = compute_testing_theory_diff(theory_id=theory_id)
            diff = diff_result.get("diff") if isinstance(diff_result, Mapping) else None
            if isinstance(diff, Mapping):
                promotion_ready_assertion_ids = _normalise_strings(
                    diff.get("promotion_ready_assertion_ids")
                )
        except Exception as exc:
            logger.warning(
                "[experiment_run] could not compute theory diff for %s: %s",
                theory_id,
                exc,
            )

    summary = {
        "reason": reason,
        "observation_counts": counts,
        "observation_total": total,
        "expected_outcome_count": expected_outcome_count,
        "has_forbidden_side_effect": has_forbidden_side_effect,
        "degradation_assessment": degradation_assessment,
    }
    promotion_recommendation = {
        "recommended": verdict == EXPERIMENT_VERDICT_PASS
        and degradation_assessment.get("degraded") is not True,
        "requires_promotion_gate": True,
        "theory_id": theory_id or None,
        "baseline_workflow_id": _safe_str(state.get("baseline_workflow_id"))
        or _safe_str(spec_state.get("baseline_workflow_id"))
        or None,
        "promotion_ready_assertion_ids": promotion_ready_assertion_ids,
        "reason": (
            "degraded_against_baseline"
            if degradation_assessment.get("degraded") is True
            else "pass_with_promotable_theory_assertions"
            if verdict == EXPERIMENT_VERDICT_PASS and promotion_ready_assertion_ids
            else "explicit_promotion_gate_required"
        ),
    }
    now_iso = _utcnow_iso()

    state["verdict"] = verdict
    state["verdict_summary"] = summary
    state["degradation_assessment"] = degradation_assessment
    state["promotion_recommendation"] = promotion_recommendation
    state["status"] = (
        EXPERIMENT_RUN_STATUS_COMPLETED
        if verdict != EXPERIMENT_VERDICT_FAIL
        else EXPERIMENT_RUN_STATUS_FAILED
    )
    state["completed_at_utc"] = now_iso
    state["updated_at_utc"] = now_iso
    existing_metrics: dict[str, Any] = (
        dict(cast(Mapping[str, Any], state.get("metrics")))
        if isinstance(state.get("metrics"), Mapping)
        else {}
    )
    state["metrics"] = {
        **existing_metrics,
        "observation_total": total,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "partial_count": partial_count,
        "inconclusive_count": inconclusive_count,
        "expected_outcome_count": expected_outcome_count,
        "policy_violation_count": _coerce_int(
            degradation_assessment.get("policy_violation_count"),
            minimum=0,
        ),
    }
    if isinstance(degradation_assessment.get("metrics"), Mapping):
        for key, value in cast(Mapping[str, Any], degradation_assessment.get("metrics")).items():
            state["metrics"][str(key)] = value
    evidence: dict[str, Any] = (
        dict(cast(Mapping[str, Any], state.get("evidence")))
        if isinstance(state.get("evidence"), Mapping)
        else {}
    )
    evidence["degradation_assessment"] = copy.deepcopy(degradation_assessment)
    state["evidence"] = evidence

    persisted = _persist_experiment_run_state(run_id=resolved_run_id, state=state)
    projection = upsert_experiment_run_projection(
        record=persisted,
        namespace=_safe_str(persisted.get("namespace")) or None,
        user_id=_safe_str(persisted.get("user_id")) or None,
        org_id=_safe_str(persisted.get("org_id")) or None,
    )
    try:
        upsert_singleton_text_relation(
            subject_concept_id=resolved_run_id,
            predicate=HAS_EXPERIMENT_VERDICT_PREDICATE_ID,
            text=verdict,
            lang="en-NZ",
            context={"source": "experiment_run_service", "reason": "compute_verdict"},
            garbage_collect=True,
        )
    except Exception as exc:
        logger.warning(
            "[experiment_run] could not write verdict text relation for %s: %s",
            resolved_run_id,
            exc,
        )

    return {
        "success": True,
        "run_id": resolved_run_id,
        "verdict": verdict,
        "verdict_summary": summary,
        "degradation_assessment": degradation_assessment,
        "promotion_recommendation": promotion_recommendation,
        "observations": _clone_sequence(persisted.get("observations")),
        "experiment_run": persisted,
        "projection": projection,
    }


def _build_candidate_workflow_rows(
    *,
    candidate_workflow_ids: Sequence[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for workflow_id in candidate_workflow_ids:
        cleaned = _safe_str(workflow_id)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        rows.append({"concept_id": cleaned, "name": cleaned, "description": ""})
    return rows


def _verdict_to_selection_outcome(verdict: str | None) -> str:
    if verdict == EXPERIMENT_VERDICT_PASS:
        return "completed"
    if verdict == EXPERIMENT_VERDICT_FAIL:
        return "failed"
    if verdict == EXPERIMENT_VERDICT_PARTIAL:
        return "partial"
    return "follow_up_required"


def emit_experiment_learning_signal(
    *,
    run_id: str,
    selection_experience_id: str | None = None,
    turn_text: str | None = None,
    expected_workflow_id: str | None = None,
    baseline_workflow_id: str | None = None,
) -> dict[str, Any]:
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id:
        return {"success": False, "error": "run_id_required"}

    state = get_experiment_run_state(resolved_run_id)
    if state is None:
        return {"success": False, "error": "experiment_run_not_found"}
    spec_state = get_experiment_spec_state(_safe_str(state.get("experiment_spec_id")))
    if spec_state is None:
        return {"success": False, "error": "experiment_spec_not_found"}

    verdict = _normalise_verdict(state.get("verdict"))
    if verdict is None:
        verdict_result = compute_experiment_verdict(run_id=resolved_run_id)
        if not verdict_result.get("success"):
            return verdict_result
        state = verdict_result.get("experiment_run") or state
        verdict = _normalise_verdict((state or {}).get("verdict"))

    resolved_selection_experience_id = (
        _safe_str(selection_experience_id)
        or _safe_str(state.get("selection_experience_id"))
        or None
    )
    candidate_workflow_ids = _normalise_strings(
        [
            *(state.get("candidate_workflow_ids") or []),
            *(state.get("target_workflow_ids") or []),
        ]
    )
    expected_id = _safe_str(expected_workflow_id) or _safe_str(
        next(iter(state.get("target_workflow_ids") or []), None)
    )
    baseline_id = (
        _safe_str(baseline_workflow_id)
        or _safe_str(state.get("baseline_workflow_id"))
        or _safe_str(spec_state.get("baseline_workflow_id"))
        or (candidate_workflow_ids[0] if candidate_workflow_ids else expected_id)
    )
    spec_fixture: dict[str, Any] = (
        dict(cast(Mapping[str, Any], spec_state.get("fixture_payload")))
        if isinstance(spec_state.get("fixture_payload"), Mapping)
        else {}
    )
    turn_text_value = _safe_str(turn_text, limit=2000) or _safe_str(
        spec_fixture.get("invitation_text") or spec_fixture.get("turn_text"),
        limit=2000,
    )
    replay_case = {
        "case_id": f"experiment_case_{resolved_run_id[3:] if resolved_run_id.startswith('#V#') else resolved_run_id}",
        "run_id": resolved_run_id,
        "experiment_spec_id": _safe_str(state.get("experiment_spec_id")) or None,
        "turn_text": turn_text_value,
        "expected_workflow_id": expected_id or None,
        "baseline_workflow_id": baseline_id or None,
        "candidate_workflows": _build_candidate_workflow_rows(
            candidate_workflow_ids=candidate_workflow_ids
        ),
        "verdict": verdict,
        "evidence": {
            "turn_execution_request_ids": _normalise_strings(
                state.get("turn_execution_request_ids")
            ),
            "observation_total": _coerce_int(
                (state.get("metrics") or {}).get("observation_total"),
                default=len(state.get("observations") or []),
                minimum=0,
            ),
        },
    }
    outcome_metadata = {
        "experiment_run_id": resolved_run_id,
        "experiment_spec_id": _safe_str(state.get("experiment_spec_id")) or None,
        "experiment_verdict": verdict,
        "completion_gate_requires_follow_up": verdict != EXPERIMENT_VERDICT_PASS,
        "turn_execution_request_ids": replay_case["evidence"]["turn_execution_request_ids"],
    }
    selection_update = None
    if resolved_selection_experience_id:
        selection_update = finalise_selection_experience(
            experience_id=resolved_selection_experience_id,
            outcome=_verdict_to_selection_outcome(verdict),
            outcome_metadata=outcome_metadata,
        )

    now_iso = _utcnow_iso()
    learning_signal = {
        "schema_version": "experiment_learning_signal.v1",
        "emitted_at_utc": now_iso,
        "selection_experience_id": resolved_selection_experience_id,
        "selection_outcome": _verdict_to_selection_outcome(verdict),
        "selection_reward": (
            float(selection_update.reward)
            if selection_update is not None and selection_update.reward is not None
            else None
        ),
        "replay_case": replay_case,
    }

    state["learning_signal"] = learning_signal
    state["replay_case"] = replay_case
    state["updated_at_utc"] = now_iso
    persisted = _persist_experiment_run_state(run_id=resolved_run_id, state=state)
    projection = upsert_experiment_run_projection(
        record=persisted,
        namespace=_safe_str(persisted.get("namespace")) or None,
        user_id=_safe_str(persisted.get("user_id")) or None,
        org_id=_safe_str(persisted.get("org_id")) or None,
    )
    return {
        "success": True,
        "run_id": resolved_run_id,
        "learning_signal": learning_signal,
        "selection_experience": selection_update.to_dict()
        if selection_update is not None
        else None,
        "experiment_run": persisted,
        "projection": projection,
    }


def prepare_experiment_spec_from_template(
    *,
    scenario_template: Mapping[str, Any] | None,
    template_inputs: Mapping[str, Any] | None = None,
    experiment_spec_id: str | None = None,
    name: str | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(scenario_template, Mapping):
        return {"success": False, "error": "scenario_template_required"}

    schema_version = (
        _safe_str(scenario_template.get("schema_version"))
        or TESTING_EXPERIMENT_SCENARIO_TEMPLATE_SCHEMA_VERSION
    )
    if schema_version != TESTING_EXPERIMENT_SCENARIO_TEMPLATE_SCHEMA_VERSION:
        return {
            "success": False,
            "error": "invalid_scenario_template_schema_version",
            "scenario_template_schema_version": schema_version,
        }

    resolved_template = _resolve_testing_template_value(
        scenario_template,
        inputs=dict(template_inputs or {}),
    )
    if not isinstance(resolved_template, Mapping):
        return {"success": False, "error": "scenario_template_resolution_failed"}

    spec_name = (
        _safe_str(name, limit=200)
        or _safe_str(resolved_template.get("name"), limit=200)
        or "Testing experiment"
    )
    expected_outcomes = _clone_sequence(resolved_template.get("expected_outcomes"))
    theory_setup = _clone_mapping(resolved_template.get("theory_setup"))
    if expected_outcomes and not theory_setup.get("expected_observations"):
        theory_setup["expected_observations"] = copy.deepcopy(expected_outcomes)

    verdict_rules = _clone_mapping(resolved_template.get("verdict_rules"))
    if (
        verdict_rules.get("require_all_expected_outcomes") is True
        and not verdict_rules.get("minimum_pass_count")
    ):
        verdict_rules["minimum_pass_count"] = len(expected_outcomes)

    promotion_policy = _clone_mapping(resolved_template.get("promotion_policy"))
    theory_slice_inputs = _clone_mapping(resolved_template.get("theory_slice_inputs"))
    if not _safe_str(theory_slice_inputs.get("name"), limit=200):
        theory_slice_inputs["name"] = f"{spec_name} theory slice"
    if expected_outcomes and not theory_slice_inputs.get("expected_observations"):
        theory_slice_inputs["expected_observations"] = copy.deepcopy(expected_outcomes)
    if promotion_policy and not theory_slice_inputs.get("promotion_policy"):
        theory_slice_inputs["promotion_policy"] = copy.deepcopy(promotion_policy)

    candidate_workflow_ids = _normalise_strings(
        resolved_template.get("candidate_workflow_ids")
    ) or _normalise_strings((template_inputs or {}).get("candidate_workflow_ids"))
    target_workflow_ids = _normalise_strings(
        resolved_template.get("target_workflow_ids")
    ) or candidate_workflow_ids[:1]
    baseline_workflow_id = _safe_str(
        resolved_template.get("baseline_workflow_id")
    ) or _safe_str((template_inputs or {}).get("baseline_workflow_id"))

    result = create_experiment_spec(
        name=spec_name,
        experiment_spec_id=experiment_spec_id,
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        description=_safe_str(resolved_template.get("description")) or None,
        target_workflow_ids=target_workflow_ids,
        target_capability_ids=_normalise_strings(
            resolved_template.get("target_capability_ids")
        ),
        candidate_workflow_ids=candidate_workflow_ids,
        baseline_workflow_id=baseline_workflow_id or None,
        theory_id=_safe_str(resolved_template.get("theory_id")) or None,
        experiment_suite_id=_safe_str(resolved_template.get("experiment_suite_id"))
        or None,
        fixture_payload=copy.deepcopy(resolved_template.get("fixture_payload")),
        theory_setup=theory_setup or None,
        expected_outcomes=expected_outcomes,
        allowed_side_effects=_normalise_strings(
            resolved_template.get("allowed_side_effects")
        ),
        forbidden_side_effects=_normalise_strings(
            resolved_template.get("forbidden_side_effects")
        ),
        verdict_rules=verdict_rules or None,
        replay_policy=_clone_mapping(resolved_template.get("replay_policy")) or None,
        promotion_policy=promotion_policy or None,
        metadata=_clone_mapping(resolved_template.get("metadata")) or None,
    )
    if not result.get("success"):
        return {
            **result,
            "scenario_template_schema_version": schema_version,
        }

    theory_slice_inputs["experiment_spec_id"] = result.get("experiment_spec_id")
    return {
        **result,
        "scenario_template_schema_version": schema_version,
        "theory_slice_name": _safe_str(theory_slice_inputs.get("name")) or None,
        "theory_slice_expected_observations": _clone_sequence(
            theory_slice_inputs.get("expected_observations")
        ),
        "theory_slice_promotion_policy": _clone_mapping(
            theory_slice_inputs.get("promotion_policy")
        ),
        "theory_slice_inputs": theory_slice_inputs,
        "seed_claims": _clone_sequence(theory_setup.get("seed_claims")),
    }


def prepare_meeting_invitation_experiment_spec(
    *,
    invitation_text: str,
    experiment_spec_id: str | None = None,
    name: str | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    candidate_workflow_ids: Sequence[str] = (),
    expected_meeting_type: str | None = None,
    expected_structure_fields: Sequence[str] = (),
    expected_downstream_actions: Sequence[str] = (),
) -> dict[str, Any]:
    invitation = _safe_str(invitation_text, limit=8000)
    if not invitation:
        return {"success": False, "error": "invitation_text_required"}

    return prepare_experiment_spec_from_template(
        scenario_template=_MEETING_INVITATION_SCENARIO_TEMPLATE,
        template_inputs={
            "invitation_text": invitation,
            "candidate_workflow_ids": candidate_workflow_ids,
            "expected_meeting_type": expected_meeting_type,
            "expected_structure_fields": expected_structure_fields,
            "expected_downstream_actions": expected_downstream_actions,
        },
        experiment_spec_id=experiment_spec_id,
        name=name,
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
    )


def execute_regression_suite(
    *,
    execution_tier: str = "tier1",
    cases: Sequence[Mapping[str, Any]] = (),
    benchmark_scenario: Mapping[str, Any] | None = None,
    output_root: str | None = None,
    run_id: str | None = None,
    suite_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    suite_resolution, suite_error = _resolve_regression_suite_mode(
        execution_tier=execution_tier,
        suite_policy=suite_policy,
    )
    if suite_error is not None:
        return suite_error
    assert suite_resolution is not None

    tier = str(suite_resolution.get("execution_tier") or "tier1")
    suite_mode = str(suite_resolution.get("suite_mode") or "cases")
    tier_policy = suite_resolution.get("tier_policy")
    tier_output_root_default = (
        _safe_str(tier_policy.get("output_root_default"))
        if isinstance(tier_policy, Mapping)
        else ""
    )

    if suite_mode == "benchmark":
        if not isinstance(benchmark_scenario, Mapping):
            return {
                "success": False,
                "error": "benchmark_scenario_required_for_benchmark_suite",
                "execution_tier": tier,
            }
        try:
            from .kb_clone_benchmark_service import (
                build_default_benchmark_app,
                run_kb_clone_benchmark,
            )

            db = get_db()
            mongo_client = getattr(db, "client", None) if db is not None else None
            if mongo_client is None:
                return {"success": False, "error": "mongo_client_unavailable"}
            result = run_kb_clone_benchmark(
                scenario=benchmark_scenario,
                output_root=output_root or tier_output_root_default or "data/testing_workflows/benchmarks",
                app=build_default_benchmark_app(),
                mongo_client=mongo_client,
            )
        except Exception as exc:
            logger.warning("[experiment_run] Tier 2 benchmark execution failed: %s", exc)
            return {"success": False, "error": "benchmark_execution_failed", "details": str(exc)}
        aggregate = result.get("metrics", {}).get("aggregate", {})
        result = {
            "success": True,
            "execution_tier": tier,
            "suite_mode": suite_mode,
            "suite_policy_schema_version": suite_resolution.get(
                "suite_policy_schema_version"
            ),
            "suite_result": result,
            "verdict": (
                EXPERIMENT_VERDICT_PASS
                if _coerce_bool(aggregate.get("all_runs_passed"))
                else EXPERIMENT_VERDICT_PARTIAL
            ),
        }
        return _maybe_record_suite_observation(
            result=result,
            run_id=run_id,
        )

    if suite_mode != "cases":
        return {
            "success": False,
            "error": "unsupported_regression_suite_mode",
            "execution_tier": tier,
            "suite_mode": suite_mode,
        }

    case_rows = [dict(item) for item in cases if isinstance(item, Mapping)]
    if not case_rows:
        return {
            "success": False,
            "error": "cases_required_for_case_suite",
            "execution_tier": tier,
        }

    pass_count = 0
    fail_count = 0
    for row in case_rows:
        expected = _normalise_verdict(row.get("expected_verdict")) or EXPERIMENT_VERDICT_PASS
        observed = _normalise_verdict(row.get("observed_verdict") or row.get("verdict"))
        if observed is None:
            observed = EXPERIMENT_VERDICT_INCONCLUSIVE
        row["expected_verdict"] = expected
        row["observed_verdict"] = observed
        row["case_passed"] = observed == expected
        if row["case_passed"]:
            pass_count += 1
        else:
            fail_count += 1

    verdict = (
        EXPERIMENT_VERDICT_PASS
        if fail_count == 0
        else EXPERIMENT_VERDICT_FAIL
        if pass_count == 0
        else EXPERIMENT_VERDICT_PARTIAL
    )
    result = {
        "success": True,
        "execution_tier": tier,
        "suite_mode": suite_mode,
        "suite_policy_schema_version": suite_resolution.get(
            "suite_policy_schema_version"
        ),
        "suite_result": {
            "case_count": len(case_rows),
            "pass_count": pass_count,
            "fail_count": fail_count,
            "pass_rate": round(pass_count / max(1, len(case_rows)), 4),
            "cases": case_rows,
        },
        "verdict": verdict,
    }
    return _maybe_record_suite_observation(
        result=result,
        run_id=run_id,
    )


def _maybe_record_suite_observation(
    *,
    result: Mapping[str, Any],
    run_id: str | None,
) -> dict[str, Any]:
    payload = dict(result)
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id or not payload.get("success"):
        return payload

    execution_tier = _safe_str(payload.get("execution_tier")) or "tier1"
    verdict = _normalise_verdict(payload.get("verdict")) or EXPERIMENT_VERDICT_INCONCLUSIVE
    suite_result = payload.get("suite_result")
    suite_metrics: Mapping[str, Any] = {}
    if isinstance(suite_result, Mapping):
        nested_metrics = suite_result.get("metrics")
        if isinstance(nested_metrics, Mapping):
            suite_metrics = nested_metrics
        else:
            suite_metrics = suite_result
    recorded = record_experiment_observation(
        run_id=resolved_run_id,
        observations=[
            {
                "label": f"regression_suite_{execution_tier}",
                "verdict": verdict,
                "observed_outcome": verdict,
                "evidence": {
                    "suite_result": copy.deepcopy(suite_result),
                    "execution_tier": execution_tier,
                },
                "metrics": dict(suite_metrics),
            }
        ],
    )
    if not recorded.get("success"):
        return {
            **payload,
            "success": False,
            "error": "experiment_run_observation_record_failed",
            "observation_recording": recorded,
        }

    return {
        **payload,
        "observation_recording": recorded,
    }


__all__ = [
    "compute_experiment_verdict",
    "create_experiment_spec",
    "emit_experiment_learning_signal",
    "execute_regression_suite",
    "get_experiment_run_projection",
    "get_experiment_run_state",
    "get_experiment_runs_collection",
    "get_experiment_spec_state",
    "prepare_experiment_spec_from_template",
    "prepare_meeting_invitation_experiment_spec",
    "record_experiment_observation",
    "start_experiment_run",
    "upsert_experiment_run_projection",
]
