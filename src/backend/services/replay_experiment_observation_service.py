"""Reusable support helpers for replay experiment observations.

This module shapes telemetry evidence for replay experiments. It deliberately
does not author prompt bodies, select production prompts, or decide promotion
policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


PROMPT_VARIANT_REPLAY_REPORT_SCHEMA_VERSION = "prompt_variant_replay_report.v1"
LIVE_PROMPT_SAMPLER_OBSERVATION_SCHEMA_VERSION = (
    "live_prompt_sampler_experiment_observation.v1"
)
PROMPT_VARIANT_SELECTION_KEYS = frozenset(
    {
        "base_prompt_concept_id",
        "selected_prompt_concept_id",
        "selected_prompt_id",
        "match_reason",
        "fallback_reason",
        "matched_model",
        "matched_model_family",
        "matched_model_capability",
        "model_selection",
    }
)
PROMPT_VARIANT_PROMPT_ID_KEYS = frozenset(
    {
        "base_prompt_id",
        "base_prompt_concept_id",
        "prompt_concept_id",
        "prompt_id",
        "resolved_prompt_concept_id",
        "selected_prompt_id",
        "selected_prompt_concept_id",
    }
)
NON_SUCCESS_COMPLETION_STATUSES = frozenset(
    {
        "blocked",
        "deny",
        "denied",
        "error",
        "fail",
        "failed",
        "follow_up_required",
        "incomplete",
        "needs_replay",
        "partial",
        "requires_follow_up",
    }
)


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _dedupe_texts(values: Sequence[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _safe_text(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


def _iter_nested_mappings(value: Any, *, max_depth: int = 6) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def _walk(item: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(item, Mapping):
            mapping = {
                str(key): child for key, child in item.items() if isinstance(key, str)
            }
            found.append(mapping)
            for child in mapping.values():
                _walk(child, depth + 1)
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for child in list(item)[:80]:
                _walk(child, depth + 1)

    _walk(value, 0)
    return found


def extract_prompt_variant_observations(
    llm_debug_data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None, str | None, str | None]] = set()
    for mapping in _iter_nested_mappings(llm_debug_data):
        selection = mapping.get("prompt_variant_selection")
        if not isinstance(selection, Mapping):
            continue
        compact = {
            key: value
            for key, value in {
                str(key): item
                for key, item in selection.items()
                if isinstance(key, str)
            }.items()
            if key in PROMPT_VARIANT_SELECTION_KEYS
        }
        if not compact:
            continue
        selected_prompt_id = (
            _safe_text(compact.get("selected_prompt_concept_id"))
            or _safe_text(compact.get("selected_prompt_id"))
            or None
        )
        base_prompt_id = _safe_text(compact.get("base_prompt_concept_id")) or None
        key = (
            base_prompt_id,
            selected_prompt_id,
            _safe_text(compact.get("match_reason")) or None,
            _safe_text(compact.get("fallback_reason")) or None,
        )
        if key in seen:
            continue
        seen.add(key)
        observations.append(
            {
                **compact,
                "base_prompt_concept_id": base_prompt_id,
                "selected_prompt_concept_id": selected_prompt_id,
            }
        )
    return observations


def extract_prompt_ids_from_debug(llm_debug_data: Mapping[str, Any]) -> list[str]:
    prompt_ids: list[str] = []
    for mapping in _iter_nested_mappings(llm_debug_data, max_depth=5):
        for key, value in mapping.items():
            if key in PROMPT_VARIANT_PROMPT_ID_KEYS:
                prompt_id = _safe_text(value)
                if prompt_id:
                    prompt_ids.append(prompt_id)
    return _dedupe_texts(prompt_ids)


def select_prompt_variant_observation(
    observations: Sequence[Mapping[str, Any]],
    *,
    expected_prompt_variant_id: str | None,
) -> dict[str, Any]:
    expected = _safe_text(expected_prompt_variant_id)
    if expected:
        for observation in observations:
            selected = _safe_text(observation.get("selected_prompt_concept_id"))
            if selected and selected.lower() == expected.lower():
                return dict(observation)
    for observation in observations:
        selected = _safe_text(observation.get("selected_prompt_concept_id"))
        base = _safe_text(observation.get("base_prompt_concept_id"))
        if selected and selected != base:
            return dict(observation)
    return dict(observations[0]) if observations else {}


def build_prompt_variant_evaluation(
    *,
    llm_debug_data: Mapping[str, Any],
    arm_metadata: Mapping[str, Any] | None,
    requested_model: str | None,
) -> dict[str, Any]:
    arm = _as_mapping(arm_metadata)
    expected_base_prompt_id = _safe_text(arm.get("base_prompt_id")) or None
    expected_variant_id = _safe_text(arm.get("candidate_prompt_variant_id")) or None
    observations = extract_prompt_variant_observations(llm_debug_data)
    selected_observation = select_prompt_variant_observation(
        observations,
        expected_prompt_variant_id=expected_variant_id,
    )
    prompt_ids = extract_prompt_ids_from_debug(llm_debug_data)
    observed_base_prompt_id = (
        _safe_text(selected_observation.get("base_prompt_concept_id"))
        or expected_base_prompt_id
    )
    selected_prompt_id = _safe_text(
        selected_observation.get("selected_prompt_concept_id")
    )
    if not selected_prompt_id and prompt_ids:
        selected_prompt_id = prompt_ids[0]

    blockers: list[str] = []
    candidate_selected: bool | None = None
    if expected_variant_id:
        if not observations:
            blockers.append("prompt_variant_selection_not_observed")
        if not selected_prompt_id:
            blockers.append("selected_prompt_id_missing")
            candidate_selected = False
        else:
            candidate_selected = selected_prompt_id.lower() == expected_variant_id.lower()
            if not candidate_selected:
                blockers.append("candidate_prompt_variant_not_selected")
    if (
        expected_base_prompt_id
        and observed_base_prompt_id
        and observed_base_prompt_id.lower() != expected_base_prompt_id.lower()
    ):
        blockers.append("observed_base_prompt_differs_from_requested_base_prompt")

    return {
        "schema_version": PROMPT_VARIANT_REPLAY_REPORT_SCHEMA_VERSION,
        "requested_model": _safe_text(requested_model) or None,
        "base_prompt_id": observed_base_prompt_id or expected_base_prompt_id,
        "requested_base_prompt_id": expected_base_prompt_id,
        "candidate_prompt_variant_id": expected_variant_id,
        "selected_prompt_id": selected_prompt_id or None,
        "candidate_prompt_variant_selected": candidate_selected,
        "normal_prompt_variant_resolution_observed": bool(observations),
        "match_reason": _safe_text(selected_observation.get("match_reason")) or None,
        "fallback_reason": _safe_text(selected_observation.get("fallback_reason"))
        or None,
        "observed_prompt_ids": prompt_ids,
        "prompt_variant_selection": selected_observation or None,
        "prompt_variant_selection_count": len(observations),
        "promotion_blockers": _dedupe_texts(blockers),
        "policy_update": {
            "authorised": False,
            "reason": (
                "Prompt-variant replay arms only measure represented prompt "
                "variant resolution. This runner does not inject raw prompt text "
                "or mutate production prompt policy."
            ),
        },
    }


def extract_response_surface_consistency(
    llm_debug_data: Mapping[str, Any],
) -> dict[str, Any]:
    for payload in (
        llm_debug_data,
        _as_mapping(llm_debug_data.get("turn_execution_diagnostics")),
    ):
        surfaces = _as_mapping(payload.get("response_surfaces"))
        consistency = _as_mapping(surfaces.get("evidence_consistency"))
        if consistency:
            return consistency
    return {}


def completion_gate_status(
    completion_gate: Mapping[str, Any],
) -> str | None:
    return (
        _safe_text(
            completion_gate.get("status")
            or completion_gate.get("verdict")
            or completion_gate.get("decision")
        )
        or None
    )


def build_replay_scoring_consistency(
    *,
    llm_debug_data: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    response_text: str,
) -> dict[str, Any]:
    completion_gate = _as_mapping(llm_debug_data.get("completion_gate_verdict"))
    completion_status = completion_gate_status(completion_gate)
    response_surface_consistency = extract_response_surface_consistency(llm_debug_data)
    surface_status = _safe_text(response_surface_consistency.get("status")) or None
    caveats = [
        _safe_text(item)
        for item in _as_list(response_surface_consistency.get("scoring_caveats"))
        if _safe_text(item)
    ]
    disagreement_codes = [
        _safe_text(item)
        for item in _as_list(response_surface_consistency.get("disagreement_codes"))
        if _safe_text(item)
    ]
    blockers: list[str] = []
    if surface_status == "inconsistent":
        blockers.append("response_surface_inconsistent")
    blockers.extend(caveats)
    if completion_status and completion_status.lower() in NON_SUCCESS_COMPLETION_STATUSES:
        if response_text:
            blockers.append("completion_gate_non_success_with_user_visible_response")
        if bool(evaluation.get("should_user_be_happy")):
            blockers.append("completion_gate_non_success_on_happy_arm")
    if disagreement_codes:
        blockers.extend(f"response_surface_{code}" for code in disagreement_codes)
    return {
        "schema_version": "replay_scoring_consistency.v1",
        "completion_gate_status": completion_status,
        "response_surface_status": surface_status,
        "response_surface_scoring_caveats": caveats,
        "response_surface_disagreement_codes": disagreement_codes,
        "promotion_blockers": _dedupe_texts(blockers),
        "non_promotable": bool(blockers),
    }


def normalise_experiment_verdict(summary: Mapping[str, Any]) -> str:
    evaluation = _as_mapping(summary.get("evaluation"))
    scoring_consistency = _as_mapping(summary.get("replay_scoring_consistency"))
    prompt_variant_evaluation = _as_mapping(summary.get("prompt_variant_evaluation"))
    if (
        bool(evaluation.get("should_user_be_happy"))
        and not bool(scoring_consistency.get("non_promotable"))
        and not _as_list(prompt_variant_evaluation.get("promotion_blockers"))
    ):
        return "pass"
    if bool(evaluation.get("should_user_be_happy")):
        return "partial"
    return "fail"


def compact_tool_invocations(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    telemetry = _as_mapping(summary.get("telemetry"))
    for item in _as_list(telemetry.get("tool_history"))[:40]:
        if not isinstance(item, Mapping):
            continue
        tool_name = _safe_text(item.get("tool") or item.get("method"))
        if not tool_name:
            continue
        compact.append(
            {
                "tool_name": tool_name,
                "status": _safe_text(item.get("status") or item.get("result_status"))
                or None,
                "success": item.get("success")
                if isinstance(item.get("success"), bool)
                else None,
            }
        )
    return compact


def build_experiment_observation_from_arm_summary(
    summary: Mapping[str, Any],
    *,
    default_replay_set_id: str | None = None,
) -> dict[str, Any]:
    arm = _as_mapping(summary.get("arm"))
    prompt = _as_mapping(summary.get("prompt"))
    conversation = _as_mapping(summary.get("conversation"))
    telemetry = _as_mapping(summary.get("telemetry"))
    evaluation = _as_mapping(summary.get("evaluation"))
    prompt_variant_evaluation = _as_mapping(summary.get("prompt_variant_evaluation"))
    scoring_consistency = _as_mapping(summary.get("replay_scoring_consistency"))
    response = _as_mapping(summary.get("response"))
    request_id = _safe_text(conversation.get("request_id")) or None
    selected_workflow_id = _safe_text(telemetry.get("selected_workflow_id")) or None
    candidate_prompt_variant_id = _safe_text(
        prompt_variant_evaluation.get("candidate_prompt_variant_id")
    )
    selected_prompt_id = _safe_text(prompt_variant_evaluation.get("selected_prompt_id"))
    candidate_valid = not _as_list(prompt_variant_evaluation.get("promotion_blockers"))
    label_parts = [
        "prompt_variant_arm",
        _safe_text(arm.get("label")) or _safe_text(arm.get("arm_id")) or "single_arm",
    ]
    replay_set_id = (
        _safe_text(arm.get("replay_set_id")) or _safe_text(default_replay_set_id) or None
    )
    return {
        "schema_version": LIVE_PROMPT_SAMPLER_OBSERVATION_SCHEMA_VERSION,
        "label": ":".join(label_parts),
        "verdict": normalise_experiment_verdict(summary),
        "observed_outcome": {
            "schema_version": LIVE_PROMPT_SAMPLER_OBSERVATION_SCHEMA_VERSION,
            "arm_id": _safe_text(arm.get("arm_id")) or None,
            "arm_label": _safe_text(arm.get("label")) or None,
            "replay_set_id": replay_set_id,
            "replay_case_id": _safe_text(arm.get("replay_case_id"))
            or _safe_text(prompt.get("id"))
            or None,
            "request_id": request_id,
            "model": _safe_text(telemetry.get("model"))
            or _safe_text(arm.get("requested_model"))
            or None,
            "base_prompt_id": _safe_text(prompt_variant_evaluation.get("base_prompt_id"))
            or None,
            "candidate_prompt_variant_id": candidate_prompt_variant_id or None,
            "selected_prompt_id": selected_prompt_id or None,
            "candidate_prompt_variant_selected": prompt_variant_evaluation.get(
                "candidate_prompt_variant_selected"
            ),
            "should_user_be_happy": bool(evaluation.get("should_user_be_happy")),
            "telemetry_non_promotable": bool(scoring_consistency.get("non_promotable")),
        },
        "workflow_execution": {
            "workflow_id": selected_workflow_id,
            "selected_execution_mode": _safe_text(
                telemetry.get("selected_execution_mode")
            )
            or None,
            "request_id": request_id,
            "history_location": _as_mapping(conversation.get("history_location")),
        },
        "candidate_validation": {
            "candidate_kind": "prompt_variant",
            "valid": candidate_valid,
            "base_prompt_id": _safe_text(prompt_variant_evaluation.get("base_prompt_id"))
            or None,
            "candidate_prompt_variant_id": candidate_prompt_variant_id or None,
            "selected_prompt_id": selected_prompt_id or None,
            "normal_prompt_variant_resolution_observed": bool(
                prompt_variant_evaluation.get(
                    "normal_prompt_variant_resolution_observed"
                )
            ),
            "promotion_blockers": _as_list(
                prompt_variant_evaluation.get("promotion_blockers")
            ),
        },
        "trace_summary": {
            "request_id": request_id,
            "response_length": len(_safe_text(response.get("text"))),
            "tool_count": telemetry.get("tool_count"),
            "completion_gate_status": scoring_consistency.get(
                "completion_gate_status"
            ),
            "response_surface_status": scoring_consistency.get(
                "response_surface_status"
            ),
        },
        "policy_decisions": [
            {
                "decision": "production_prompt_policy_unchanged",
                "authorised": False,
                "reason": (
                    "Live prompt sampler records replay evidence only; prompt "
                    "promotion remains a represented Vontology workflow decision."
                ),
            }
        ],
        "quality_signals": {
            "should_user_be_happy": bool(evaluation.get("should_user_be_happy")),
            "telemetry_non_promotable": bool(scoring_consistency.get("non_promotable")),
            "reasons": _as_list(evaluation.get("reasons"))[:20],
        },
        "repair_hints": [
            {
                "scope": "prompt_variant_resolution",
                "reason_code": reason_code,
            }
            for reason_code in _as_list(
                prompt_variant_evaluation.get("promotion_blockers")
            )
        ],
        "tool_invocations": compact_tool_invocations(summary),
        "assertion_classes": [
            "model_prompt_variant_arm_replay",
            "experiment_observation",
        ],
        "turn_execution_request_ids": [request_id] if request_id else [],
    }


def record_experiment_observations(
    *,
    run_id: str,
    arm_summaries: Sequence[Mapping[str, Any]],
    default_replay_set_id: str | None = None,
    gateway: Any | None = None,
) -> dict[str, Any]:
    observations = [
        build_experiment_observation_from_arm_summary(
            summary,
            default_replay_set_id=default_replay_set_id,
        )
        for summary in arm_summaries
        if isinstance(summary, Mapping)
    ]
    turn_execution_request_ids = _dedupe_texts(
        [
            request_id
            for observation in observations
            for request_id in _as_list(observation.get("turn_execution_request_ids"))
        ]
    )
    if not observations:
        return {
            "success": False,
            "error": "no_arm_summaries_to_record",
            "recorded_observation_count": 0,
        }
    if gateway is None:
        from src.backend.integrations.internal_mcp import (
            InternalMCPGateway,
            InternalMCPTransport,
            build_default_catalogue,
        )

        gateway = InternalMCPGateway(
            catalogue=build_default_catalogue(),
            transport=InternalMCPTransport(),
            enabled=True,
        )
    result = gateway.invoke(
        "experiment_record_observation",
        {
            "run_id": run_id,
            "observations": observations,
            "turn_execution_request_ids": turn_execution_request_ids,
        },
    )
    payload = _as_mapping(result.payload)
    recorded_observations = _as_list(payload.get("recorded_observations"))
    return {
        "success": bool(payload.get("success")),
        "run_id": payload.get("run_id") or run_id,
        "recorded_observation_count": (
            len(recorded_observations) if recorded_observations else len(observations)
        ),
        "turn_execution_request_ids": turn_execution_request_ids,
        "mcp_tool": "experiment_record_observation",
        "mcp_duration_ms": getattr(result, "duration_ms", None),
        "error": payload.get("error"),
    }
