"""Support scaffolding for failure-case prompt replay experiments.

This module prepares deterministic experiment inputs from collected turn
evidence. It does not classify the failure, generate prompt hypotheses, score
variants, or decide whether a prompt should be promoted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from .replay_arm_planning_service import build_replay_arm_plan
from .testing_workflow_contracts import TESTING_EXPERIMENT_SCENARIO_TEMPLATE_SCHEMA_VERSION

FAILURE_CASE_PROMPT_REPLAY_EXPERIMENT_SCHEMA_VERSION = (
    "failure_case_prompt_replay_experiment.v1"
)
FAILURE_CASE_PROMPT_REPLAY_FIXTURE_SCHEMA_VERSION = (
    "failure_case_prompt_replay_fixture.v1"
)
FAILURE_CASE_PROMPT_REPLAY_PREPARE_ACTION_ID = (
    "failure_case.prompt_replay.prepare_experiment"
)
FAILURE_CASE_PROMPT_REPLAY_RECORD_OBSERVATIONS_ACTION_ID = (
    "failure_case.prompt_replay.record_observations"
)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _mapping(value: Any) -> dict[str, Any]:
    return (
        {str(key): item for key, item in value.items() if isinstance(key, str)}
        if isinstance(value, Mapping)
        else {}
    )


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [_mapping(item) for item in value if isinstance(item, Mapping)]


def _dedupe_strings(values: Sequence[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _safe_str(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


def _normalise_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return _dedupe_strings([value])
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return _dedupe_strings(list(value))
    return []


def _first_path(payload: Mapping[str, Any], *paths: Sequence[str]) -> Any:
    for path in paths:
        cursor: Any = payload
        found = True
        for part in path:
            if not isinstance(cursor, Mapping) or part not in cursor:
                found = False
                break
            cursor = cursor.get(part)
        if found and cursor not in (None, "", [], {}):
            return cursor
    return None


def _short_hash(*parts: Any) -> str:
    joined = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()[:20]


def _stable_concept_id(prefix: str, *parts: Any) -> str:
    return f"#V#{prefix}_{_short_hash(*parts)}"


def _base_prompt_from_intake(
    *,
    failure_case_intake: Mapping[str, Any],
    explicit_base_prompt_id: str | None,
) -> str | None:
    explicit = _safe_str(explicit_base_prompt_id)
    if explicit:
        return explicit

    selections = _first_path(
        failure_case_intake,
        ("prompt_metadata", "prompt_variant_selections"),
    )
    for item in _mapping_list(selections):
        prompt_id = _safe_str(item.get("base_prompt_concept_id"))
        if prompt_id:
            return prompt_id

    applied_prompt_ids = _normalise_strings(
        _first_path(failure_case_intake, ("prompt_metadata", "applied_prompt_ids"))
    )
    if applied_prompt_ids:
        return applied_prompt_ids[0]

    prompt_ids = _normalise_strings(
        _first_path(failure_case_intake, ("prompt_metadata", "prompt_ids"))
    )
    return prompt_ids[0] if prompt_ids else None


def _normalise_model_arm(entry: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    arm = {str(key): item for key, item in entry.items() if isinstance(key, str)}
    requested_model = (
        _safe_str(arm.get("requested_model"))
        or _safe_str(arm.get("model"))
        or _safe_str(arm.get("model_id"))
    )
    arm["arm_id"] = _safe_str(arm.get("arm_id")) or f"arm_{index}"
    arm["label"] = _safe_str(arm.get("label")) or requested_model or arm["arm_id"]
    arm["requested_model"] = requested_model
    return arm


def _model_arms_from_inputs(
    *,
    model_arms: Sequence[Mapping[str, Any]],
    failure_case_intake: Mapping[str, Any],
    target_model: str | None,
    comparator_model: str | None,
) -> list[dict[str, Any]]:
    provided = [
        _normalise_model_arm(item, index=index)
        for index, item in enumerate(model_arms, start=1)
        if isinstance(item, Mapping)
    ]
    if provided:
        return provided

    observed_target = (
        _safe_str(target_model)
        or _safe_str(_first_path(failure_case_intake, ("model", "target_model")))
        or _safe_str(_first_path(failure_case_intake, ("model", "primary_model")))
    )
    observed_comparator = _safe_str(comparator_model) or _safe_str(
        _first_path(failure_case_intake, ("model", "comparator_model"))
    )

    planned: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _append(label: str, model: str | None) -> None:
        cleaned = _safe_str(model)
        if not cleaned:
            return
        key = cleaned.casefold()
        if key in seen:
            return
        seen.add(key)
        planned.append(
            {
                "arm_id": f"arm_{len(planned) + 1}",
                "label": label,
                "requested_model": cleaned,
            }
        )

    _append("target_model", observed_target)
    _append("comparator_model", observed_comparator)
    return planned


def prepare_failure_case_prompt_replay_experiment(
    *,
    failure_case_intake: Mapping[str, Any] | None,
    failure_request_id: str | None = None,
    target_workflow_id: str | None = None,
    workflow_stage_id: str | None = None,
    base_prompt_id: str | None = None,
    prompt_variant_ids: Sequence[Any] = (),
    model_arms: Sequence[Mapping[str, Any]] = (),
    target_model: str | None = None,
    comparator_model: str | None = None,
    replay_set_id: str | None = None,
    replay_case_id: str | None = None,
    experiment_spec_id: str | None = None,
    experiment_name: str | None = None,
    experiment_description: str | None = None,
    target_capability_ids: Sequence[Any] = (),
    candidate_workflow_ids: Sequence[Any] = (),
    baseline_workflow_id: str | None = None,
    expected_outcomes: Sequence[Any] = (),
    allowed_side_effects: Sequence[Any] = (),
    forbidden_side_effects: Sequence[Any] = (),
    verdict_rules: Mapping[str, Any] | None = None,
    replay_policy: Mapping[str, Any] | None = None,
    promotion_policy: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare canonical experiment inputs for a represented workflow step."""

    intake = _mapping(failure_case_intake)
    request_id = _safe_str(failure_request_id) or _safe_str(intake.get("request_id"))
    if not request_id:
        return {
            "schema_version": FAILURE_CASE_PROMPT_REPLAY_EXPERIMENT_SCHEMA_VERSION,
            "success": False,
            "error": "failure_case_request_id_required",
            "policy_boundary": _policy_boundary(),
        }

    resolved_workflow_id = (
        _safe_str(target_workflow_id)
        or _safe_str(_first_path(intake, ("workflow", "selected_workflow_id")))
        or _safe_str(_first_path(intake, ("workflow", "expected_workflow_id")))
    )
    resolved_stage_id = _safe_str(workflow_stage_id) or _safe_str(
        _first_path(intake, ("workflow", "stage_id"))
    )
    resolved_base_prompt_id = _base_prompt_from_intake(
        failure_case_intake=intake,
        explicit_base_prompt_id=base_prompt_id,
    )
    variants = _normalise_strings(prompt_variant_ids)
    resolved_model_arms = _model_arms_from_inputs(
        model_arms=model_arms,
        failure_case_intake=intake,
        target_model=target_model,
        comparator_model=comparator_model,
    )

    stable_key_parts = (
        request_id,
        resolved_workflow_id,
        resolved_stage_id,
        resolved_base_prompt_id,
        ",".join(variants),
    )
    resolved_replay_case_id = _safe_str(replay_case_id) or _stable_concept_id(
        "failure_case_replay_case",
        *stable_key_parts,
    )
    resolved_replay_set_id = _safe_str(replay_set_id) or _stable_concept_id(
        "failure_case_prompt_replay_set",
        request_id,
        resolved_workflow_id,
        resolved_stage_id,
    )
    resolved_experiment_spec_id = _safe_str(experiment_spec_id) or _stable_concept_id(
        "failure_case_prompt_experiment",
        *stable_key_parts,
    )
    arm_plan = build_replay_arm_plan(
        model_arms=resolved_model_arms,
        base_prompt_id=resolved_base_prompt_id,
        prompt_variant_ids=variants,
        workflow_stage_id=resolved_stage_id,
        target_workflow_id=resolved_workflow_id,
        replay_set_id=resolved_replay_set_id,
        replay_case_id=resolved_replay_case_id,
        default_replay_set_id=resolved_replay_set_id,
    )

    missing_replay_inputs: list[str] = []
    if not variants:
        missing_replay_inputs.append("prompt_variant_ids")
    if not resolved_model_arms:
        missing_replay_inputs.append("model_arms")
    if not resolved_base_prompt_id:
        missing_replay_inputs.append("base_prompt_id")

    replay_case = {
        "schema_version": "failure_case_replay_case.v1",
        "replay_case_id": resolved_replay_case_id,
        "failure_request_id": request_id,
        "target_workflow_id": resolved_workflow_id,
        "workflow_stage_id": resolved_stage_id,
        "base_prompt_id": resolved_base_prompt_id,
        "source_evidence_refs": _first_path(intake, ("telemetry", "evidence_refs")) or [],
    }
    fixture_payload = {
        "schema_version": FAILURE_CASE_PROMPT_REPLAY_FIXTURE_SCHEMA_VERSION,
        "failure_case_intake": intake,
        "failure_case_replay_case": replay_case,
        "base_prompt_id": resolved_base_prompt_id,
        "candidate_prompt_variant_ids": variants,
        "replay_arm_plan": arm_plan,
    }
    metadata_payload = {
        "schema_version": FAILURE_CASE_PROMPT_REPLAY_EXPERIMENT_SCHEMA_VERSION,
        "failure_case_request_id": request_id,
        "replay_case_id": resolved_replay_case_id,
        "replay_set_id": resolved_replay_set_id,
        "base_prompt_id": resolved_base_prompt_id,
        "candidate_prompt_variant_ids": variants,
        "workflow_stage_id": resolved_stage_id,
        "target_workflow_id": resolved_workflow_id,
        "ready_for_prompt_variant_replay": not missing_replay_inputs,
        "missing_replay_inputs": list(missing_replay_inputs),
        "workflow_authored_metadata": _mapping(metadata),
    }

    spec_name = (
        _safe_str(experiment_name)
        or f"Failure-case prompt replay for {request_id}"
    )
    description = (
        _safe_str(experiment_description)
        or f"Replay-backed prompt-variant experiment scaffold for {request_id}."
    )
    target_workflow_ids = [resolved_workflow_id] if resolved_workflow_id else []
    experiment_create_spec_inputs = {
        "name": spec_name,
        "experiment_spec_id": resolved_experiment_spec_id,
        "description": description,
        "target_workflow_ids": target_workflow_ids,
        "target_capability_ids": _normalise_strings(target_capability_ids),
        "candidate_workflow_ids": _normalise_strings(candidate_workflow_ids),
        "baseline_workflow_id": _safe_str(baseline_workflow_id),
        "fixture_payload": fixture_payload,
        "expected_outcomes": list(expected_outcomes or []),
        "allowed_side_effects": list(allowed_side_effects or []),
        "forbidden_side_effects": list(forbidden_side_effects or []),
        "verdict_rules": _mapping(verdict_rules),
        "replay_policy": _mapping(replay_policy),
        "promotion_policy": _mapping(promotion_policy),
        "metadata": metadata_payload,
    }

    scenario_template = {
        "schema_version": TESTING_EXPERIMENT_SCENARIO_TEMPLATE_SCHEMA_VERSION,
        **{
            key: value
            for key, value in experiment_create_spec_inputs.items()
            if value not in (None, [], {})
        },
    }

    return {
        "schema_version": FAILURE_CASE_PROMPT_REPLAY_EXPERIMENT_SCHEMA_VERSION,
        "success": True,
        "failure_case_prompt_replay_prepared": True,
        "failure_request_id": request_id,
        "replay_case_id": resolved_replay_case_id,
        "replay_set_id": resolved_replay_set_id,
        "experiment_spec_id": resolved_experiment_spec_id,
        "experiment_name": spec_name,
        "experiment_description": description,
        "target_workflow_ids": target_workflow_ids,
        "target_capability_ids": experiment_create_spec_inputs["target_capability_ids"],
        "candidate_workflow_ids": experiment_create_spec_inputs["candidate_workflow_ids"],
        "baseline_workflow_id": experiment_create_spec_inputs["baseline_workflow_id"],
        "experiment_fixture_payload": fixture_payload,
        "experiment_expected_outcomes": experiment_create_spec_inputs["expected_outcomes"],
        "experiment_allowed_side_effects": experiment_create_spec_inputs[
            "allowed_side_effects"
        ],
        "experiment_forbidden_side_effects": experiment_create_spec_inputs[
            "forbidden_side_effects"
        ],
        "experiment_verdict_rules": experiment_create_spec_inputs["verdict_rules"],
        "experiment_replay_policy": experiment_create_spec_inputs["replay_policy"],
        "experiment_promotion_policy": experiment_create_spec_inputs[
            "promotion_policy"
        ],
        "experiment_metadata": metadata_payload,
        "experiment_turn_execution_request_ids": [request_id],
        "experiment_create_spec_inputs": experiment_create_spec_inputs,
        "scenario_template": scenario_template,
        "failure_case_replay_case": replay_case,
        "base_prompt_id": resolved_base_prompt_id,
        "candidate_prompt_variant_ids": variants,
        "model_arms": resolved_model_arms,
        "replay_arm_plan": arm_plan,
        "ready_for_prompt_variant_replay": not missing_replay_inputs,
        "missing_replay_inputs": missing_replay_inputs,
        "policy_boundary": _policy_boundary(),
    }


def _policy_boundary() -> dict[str, Any]:
    return {
        "classification_performed": False,
        "prompt_hypothesis_generated": False,
        "prompt_body_generated": False,
        "replay_scored": False,
        "promotion_recommendation_generated": False,
        "experiment_spec_persisted": False,
        "reason": (
            "This helper only shapes replay experiment inputs. Failure "
            "classification, prompt-candidate authoring, scoring, and promotion "
            "remain represented workflow/prompt policy."
        ),
    }


__all__ = [
    "FAILURE_CASE_PROMPT_REPLAY_EXPERIMENT_SCHEMA_VERSION",
    "FAILURE_CASE_PROMPT_REPLAY_FIXTURE_SCHEMA_VERSION",
    "FAILURE_CASE_PROMPT_REPLAY_PREPARE_ACTION_ID",
    "FAILURE_CASE_PROMPT_REPLAY_RECORD_OBSERVATIONS_ACTION_ID",
    "prepare_failure_case_prompt_replay_experiment",
]
