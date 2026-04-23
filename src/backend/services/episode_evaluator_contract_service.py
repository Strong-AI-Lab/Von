"""Shared support surface for canonical multi-axis episode evaluator state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATOR_AXIS_IDS,
    EPISODE_EVALUATOR_AXIS_VERSION,
    EPISODE_EVALUATOR_CONTRACT_SCHEMA_VERSION,
)

_KNOWN_STATUS_VALUES = {
    "pass",
    "follow_up_required",
    "fail",
    "inconclusive",
    "not_evaluated",
}
_EVALUATED_STATUS_VALUES = {
    "pass",
    "follow_up_required",
    "fail",
    "inconclusive",
}
_ACTIONABLE_STATUS_VALUES = {"follow_up_required", "fail"}


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _normalise_strings(values: Any, *, limit: int = 40) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _normalise_status(value: Any, *, default: str = "not_evaluated") -> str:
    cleaned = (_clean_text(value) or "").lower()
    return cleaned if cleaned in _KNOWN_STATUS_VALUES else default


def _merge_axis_defaults(
    default_axis: Mapping[str, Any],
    override_axis: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(default_axis)
    if not isinstance(override_axis, Mapping):
        return merged

    for key in (
        "axis_version",
        "status",
        "summary",
        "counterfactual_recommended_action",
        "measured_at_utc",
    ):
        if key == "status":
            merged[key] = _normalise_status(
                override_axis.get(key),
                default=_normalise_status(default_axis.get("status")),
            )
            continue
        cleaned = _clean_text(override_axis.get(key))
        if cleaned is not None:
            merged[key] = cleaned

    for key in ("score", "utility_estimate", "confidence"):
        parsed = _safe_float(override_axis.get(key))
        if parsed is not None:
            merged[key] = parsed

    for key in (
        "reason_codes",
        "evidence_receipt_ids",
        "source_memory_ids",
        "subject_workflow_ids",
        "subject_tool_names",
        "subject_concept_ids",
    ):
        override_values = _normalise_strings(override_axis.get(key), limit=120)
        if override_values:
            merged[key] = override_values

    return merged


def _default_axis_payload(
    *,
    axis_id: str,
    status: str,
    summary: str,
    confidence: float | None,
    reason_codes: Sequence[str] | None,
    evidence_receipt_ids: Sequence[str] | None,
    source_memory_ids: Sequence[str] | None,
    subject_workflow_ids: Sequence[str] | None,
    subject_tool_names: Sequence[str] | None,
    subject_concept_ids: Sequence[str] | None,
    counterfactual_recommended_action: str | None = None,
    measured_at_utc: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "axis_id": axis_id,
        "axis_version": EPISODE_EVALUATOR_AXIS_VERSION,
        "status": _normalise_status(status),
        "summary": summary,
        "reason_codes": _normalise_strings(reason_codes, limit=40),
        "evidence_receipt_ids": _normalise_strings(evidence_receipt_ids, limit=60),
        "source_memory_ids": _normalise_strings(source_memory_ids, limit=60),
        "subject_workflow_ids": _normalise_strings(subject_workflow_ids, limit=40),
        "subject_tool_names": _normalise_strings(subject_tool_names, limit=60),
        "subject_concept_ids": _normalise_strings(subject_concept_ids, limit=80),
        "measured_at_utc": _clean_text(measured_at_utc),
    }
    if confidence is not None:
        payload["confidence"] = confidence
    if counterfactual_recommended_action:
        payload["counterfactual_recommended_action"] = counterfactual_recommended_action
    return payload


def _build_execution_correctness_axis(
    *,
    legacy_verdict: str | None,
    legacy_confidence: float | None,
    summary: str | None,
    completion_gate: Mapping[str, Any] | None,
    critic_summary: Mapping[str, Any] | None,
    evidence_receipt_ids: Sequence[str] | None,
    source_memory_ids: Sequence[str] | None,
    subject_workflow_ids: Sequence[str] | None,
    subject_tool_names: Sequence[str] | None,
    subject_concept_ids: Sequence[str] | None,
    measured_at_utc: str | None,
) -> dict[str, Any]:
    gate = _mapping_or_empty(completion_gate)
    critic = _mapping_or_empty(critic_summary)
    status = _normalise_status(legacy_verdict, default="inconclusive")
    reason_codes = _normalise_strings(gate.get("blocking_failure_codes"), limit=20)
    if bool(gate.get("requires_follow_up")):
        reason_codes.append("completion_requires_follow_up")
        status = "follow_up_required"
    if _safe_int(critic.get("not_verified_count")) > 0:
        reason_codes.append("critic_not_verified_present")
    if _safe_int(critic.get("inconclusive_count")) > 0:
        reason_codes.append("critic_inconclusive_present")
    if _safe_int(critic.get("error_count")) > 0:
        reason_codes.append("critic_error_present")
    if not reason_codes and status == "pass":
        reason_codes = ["completion_gate_clear"]

    summary_text = _clean_text(summary)
    if summary_text is None:
        verified = max(0, _safe_int(critic.get("verified_count")))
        unresolved = (
            max(0, _safe_int(critic.get("not_verified_count")))
            + max(0, _safe_int(critic.get("inconclusive_count")))
            + max(0, _safe_int(critic.get("error_count")))
        )
        summary_text = (
            "Execution correctness derived from critic summary and completion gate: "
            f"{verified} verified checks, {unresolved} unresolved checks."
        )

    counterfactual = None
    if status in _ACTIONABLE_STATUS_VALUES:
        counterfactual = "resolve_required_effects_before_claiming_completion"

    return _default_axis_payload(
        axis_id="execution_correctness",
        status=status,
        summary=summary_text,
        confidence=legacy_confidence,
        reason_codes=reason_codes,
        evidence_receipt_ids=evidence_receipt_ids,
        source_memory_ids=source_memory_ids,
        subject_workflow_ids=subject_workflow_ids,
        subject_tool_names=subject_tool_names,
        subject_concept_ids=subject_concept_ids,
        counterfactual_recommended_action=counterfactual,
        measured_at_utc=measured_at_utc,
    )


def _build_grounded_helpfulness_axis(
    *,
    format_over_content_diagnostic: Mapping[str, Any] | None,
    evidence_receipt_ids: Sequence[str] | None,
    source_memory_ids: Sequence[str] | None,
    subject_workflow_ids: Sequence[str] | None,
    subject_tool_names: Sequence[str] | None,
    subject_concept_ids: Sequence[str] | None,
    measured_at_utc: str | None,
) -> dict[str, Any]:
    diagnostic = _mapping_or_empty(format_over_content_diagnostic)
    diagnostic_status = (_clean_text(diagnostic.get("status")) or "").lower()
    status = "not_evaluated"
    if diagnostic_status in {"pass", "clear", "grounded", "supported"}:
        status = "pass"
    elif diagnostic_status in {"fail", "ungrounded", "unsupported"}:
        status = "fail"
    elif diagnostic_status:
        status = "inconclusive"

    summary_text = _clean_text(diagnostic.get("summary"))
    if summary_text is None:
        if status == "not_evaluated":
            summary_text = (
                "Grounded helpfulness has not yet been scored by a dedicated evaluator."
            )
        else:
            summary_text = (
                "Grounded helpfulness derived from the retained format-over-content "
                "diagnostic."
            )

    counterfactual = None
    if status in {"fail", "inconclusive"}:
        counterfactual = "gather_or_use_stronger_answer_supporting_evidence"

    return _default_axis_payload(
        axis_id="grounded_helpfulness",
        status=status,
        summary=summary_text,
        confidence=_safe_float(diagnostic.get("confidence")),
        reason_codes=_normalise_strings(diagnostic.get("reason_codes"), limit=20),
        evidence_receipt_ids=evidence_receipt_ids,
        source_memory_ids=source_memory_ids,
        subject_workflow_ids=subject_workflow_ids,
        subject_tool_names=subject_tool_names,
        subject_concept_ids=subject_concept_ids,
        counterfactual_recommended_action=counterfactual,
        measured_at_utc=measured_at_utc,
    )


def _build_placeholder_axis(
    *,
    axis_id: str,
    summary: str,
    evidence_receipt_ids: Sequence[str] | None,
    source_memory_ids: Sequence[str] | None,
    subject_workflow_ids: Sequence[str] | None,
    subject_tool_names: Sequence[str] | None,
    subject_concept_ids: Sequence[str] | None,
    measured_at_utc: str | None,
) -> dict[str, Any]:
    return _default_axis_payload(
        axis_id=axis_id,
        status="not_evaluated",
        summary=summary,
        confidence=None,
        reason_codes=["axis_not_yet_scored"],
        evidence_receipt_ids=evidence_receipt_ids,
        source_memory_ids=source_memory_ids,
        subject_workflow_ids=subject_workflow_ids,
        subject_tool_names=subject_tool_names,
        subject_concept_ids=subject_concept_ids,
        measured_at_utc=measured_at_utc,
    )


def _build_default_axes(
    *,
    legacy_verdict: str | None,
    legacy_confidence: float | None,
    summary: str | None,
    completion_gate: Mapping[str, Any] | None,
    critic_summary: Mapping[str, Any] | None,
    format_over_content_diagnostic: Mapping[str, Any] | None,
    evidence_receipt_ids: Sequence[str] | None,
    source_memory_ids: Sequence[str] | None,
    subject_workflow_ids: Sequence[str] | None,
    subject_tool_names: Sequence[str] | None,
    subject_concept_ids: Sequence[str] | None,
    measured_at_utc: str | None,
) -> dict[str, dict[str, Any]]:
    return {
        "execution_correctness": _build_execution_correctness_axis(
            legacy_verdict=legacy_verdict,
            legacy_confidence=legacy_confidence,
            summary=summary,
            completion_gate=completion_gate,
            critic_summary=critic_summary,
            evidence_receipt_ids=evidence_receipt_ids,
            source_memory_ids=source_memory_ids,
            subject_workflow_ids=subject_workflow_ids,
            subject_tool_names=subject_tool_names,
            subject_concept_ids=subject_concept_ids,
            measured_at_utc=measured_at_utc,
        ),
        "grounded_helpfulness": _build_grounded_helpfulness_axis(
            format_over_content_diagnostic=format_over_content_diagnostic,
            evidence_receipt_ids=evidence_receipt_ids,
            source_memory_ids=source_memory_ids,
            subject_workflow_ids=subject_workflow_ids,
            subject_tool_names=subject_tool_names,
            subject_concept_ids=subject_concept_ids,
            measured_at_utc=measured_at_utc,
        ),
        "calibration_and_abstention": _build_placeholder_axis(
            axis_id="calibration_and_abstention",
            summary=(
                "Calibration and abstention quality have not yet been scored by a "
                "dedicated evaluator."
            ),
            evidence_receipt_ids=evidence_receipt_ids,
            source_memory_ids=source_memory_ids,
            subject_workflow_ids=subject_workflow_ids,
            subject_tool_names=subject_tool_names,
            subject_concept_ids=subject_concept_ids,
            measured_at_utc=measured_at_utc,
        ),
        "recovery_quality": _build_placeholder_axis(
            axis_id="recovery_quality",
            summary=(
                "Recovery quality has not yet been scored by a dedicated evaluator."
            ),
            evidence_receipt_ids=evidence_receipt_ids,
            source_memory_ids=source_memory_ids,
            subject_workflow_ids=subject_workflow_ids,
            subject_tool_names=subject_tool_names,
            subject_concept_ids=subject_concept_ids,
            measured_at_utc=measured_at_utc,
        ),
        "long_horizon_task_state_integrity": _build_placeholder_axis(
            axis_id="long_horizon_task_state_integrity",
            summary=(
                "Long-horizon task-state integrity has not yet been scored by a "
                "dedicated evaluator."
            ),
            evidence_receipt_ids=evidence_receipt_ids,
            source_memory_ids=source_memory_ids,
            subject_workflow_ids=subject_workflow_ids,
            subject_tool_names=subject_tool_names,
            subject_concept_ids=subject_concept_ids,
            measured_at_utc=measured_at_utc,
        ),
    }


def _normalise_explicit_axes(explicit_axes: Any) -> dict[str, dict[str, Any]]:
    candidate = explicit_axes
    if isinstance(candidate, Mapping) and isinstance(candidate.get("axes"), Sequence):
        candidate = candidate.get("axes")

    axes: dict[str, dict[str, Any]] = {}
    if isinstance(candidate, Sequence) and not isinstance(
        candidate,
        (str, bytes, bytearray),
    ):
        for item in candidate:
            axis = _mapping_or_empty(item)
            axis_id = _clean_text(axis.get("axis_id"))
            if not axis_id or axis_id not in EPISODE_EVALUATOR_AXIS_IDS:
                continue
            axes[axis_id] = axis
        return axes

    if isinstance(candidate, Mapping):
        for axis_id in EPISODE_EVALUATOR_AXIS_IDS:
            axis = _mapping_or_empty(candidate.get(axis_id))
            if axis:
                axes[axis_id] = {"axis_id": axis_id, **axis}
    return axes


def _derive_rollup_verdict(
    axes: Sequence[Mapping[str, Any]],
    *,
    legacy_verdict: str | None,
) -> str:
    statuses = [
        _normalise_status(axis.get("status"))
        for axis in axes
        if _normalise_status(axis.get("status")) in _EVALUATED_STATUS_VALUES
    ]
    if "fail" in statuses:
        return "fail"
    if "follow_up_required" in statuses:
        return "follow_up_required"
    if "pass" in statuses:
        return "pass"
    if "inconclusive" in statuses:
        return "inconclusive"
    return _normalise_status(legacy_verdict, default="inconclusive")


def _derive_rollup_confidence(
    axes: Sequence[Mapping[str, Any]],
    *,
    legacy_confidence: float | None,
    verdict: str,
) -> float | None:
    if legacy_confidence is not None:
        return legacy_confidence
    evaluated_confidences = [
        confidence
        for axis in axes
        if _normalise_status(axis.get("status")) in _EVALUATED_STATUS_VALUES
        for confidence in [_safe_float(axis.get("confidence"))]
        if confidence is not None
    ]
    if not evaluated_confidences:
        return None
    if verdict in _ACTIONABLE_STATUS_VALUES or verdict == "inconclusive":
        return round(min(evaluated_confidences), 3)
    return round(sum(evaluated_confidences) / float(len(evaluated_confidences)), 3)


def _derive_rollup_unresolved_count(
    axes: Sequence[Mapping[str, Any]],
    *,
    legacy_unresolved_check_count: int | None,
) -> int:
    derived = sum(
        1
        for axis in axes
        if _normalise_status(axis.get("status")) in _ACTIONABLE_STATUS_VALUES
        or _normalise_status(axis.get("status")) == "inconclusive"
    )
    legacy = max(0, int(legacy_unresolved_check_count or 0))
    return max(legacy, derived)


def build_episode_evaluator_contract(
    *,
    legacy_verdict: str | None,
    legacy_confidence: float | None,
    legacy_unresolved_check_count: int | None,
    summary: str | None,
    completion_gate: Mapping[str, Any] | None = None,
    critic_summary: Mapping[str, Any] | None = None,
    format_over_content_diagnostic: Mapping[str, Any] | None = None,
    explicit_axes: Any = None,
    evidence_receipt_ids: Sequence[str] | None = None,
    source_memory_ids: Sequence[str] | None = None,
    subject_workflow_ids: Sequence[str] | None = None,
    subject_tool_names: Sequence[str] | None = None,
    subject_concept_ids: Sequence[str] | None = None,
    measured_at_utc: str | None = None,
) -> dict[str, Any]:
    default_axes = _build_default_axes(
        legacy_verdict=legacy_verdict,
        legacy_confidence=legacy_confidence,
        summary=summary,
        completion_gate=completion_gate,
        critic_summary=critic_summary,
        format_over_content_diagnostic=format_over_content_diagnostic,
        evidence_receipt_ids=evidence_receipt_ids,
        source_memory_ids=source_memory_ids,
        subject_workflow_ids=subject_workflow_ids,
        subject_tool_names=subject_tool_names,
        subject_concept_ids=subject_concept_ids,
        measured_at_utc=measured_at_utc,
    )
    explicit_axis_map = _normalise_explicit_axes(explicit_axes)
    axes = [
        _merge_axis_defaults(default_axes[axis_id], explicit_axis_map.get(axis_id, {}))
        for axis_id in EPISODE_EVALUATOR_AXIS_IDS
    ]

    verdict = _derive_rollup_verdict(axes, legacy_verdict=legacy_verdict)
    confidence = _derive_rollup_confidence(
        axes,
        legacy_confidence=legacy_confidence,
        verdict=verdict,
    )
    unresolved_check_count = _derive_rollup_unresolved_count(
        axes,
        legacy_unresolved_check_count=legacy_unresolved_check_count,
    )
    evaluated_axis_ids = [
        str(axis.get("axis_id"))
        for axis in axes
        if _normalise_status(axis.get("status")) in _EVALUATED_STATUS_VALUES
    ]
    actionable_axis_ids = [
        str(axis.get("axis_id"))
        for axis in axes
        if _normalise_status(axis.get("status")) in _ACTIONABLE_STATUS_VALUES
    ]
    return {
        "schema_version": EPISODE_EVALUATOR_CONTRACT_SCHEMA_VERSION,
        "axes": axes,
        "axis_ids": list(EPISODE_EVALUATOR_AXIS_IDS),
        "evaluated_axis_ids": evaluated_axis_ids,
        "actionable_axis_ids": actionable_axis_ids,
        "verdict": verdict,
        "confidence": confidence,
        "unresolved_check_count": unresolved_check_count,
    }


def build_episode_evaluator_projection_fields(
    contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    mapping = _mapping_or_empty(contract)
    axes_payload = mapping.get("axes")
    axes = (
        [item for item in axes_payload if isinstance(item, Mapping)]
        if isinstance(axes_payload, Sequence)
        and not isinstance(axes_payload, (str, bytes, bytearray))
        else []
    )
    status_map = {
        _clean_text(axis.get("axis_id")): _normalise_status(axis.get("status"))
        for axis in axes
        if _clean_text(axis.get("axis_id"))
    }
    confidence_map = {
        axis_id: confidence
        for axis in axes
        for axis_id in [_clean_text(axis.get("axis_id"))]
        for confidence in [_safe_float(axis.get("confidence"))]
        if axis_id and confidence is not None
    }
    reason_code_map = {
        axis_id: _normalise_strings(axis.get("reason_codes"), limit=30)
        for axis in axes
        for axis_id in [_clean_text(axis.get("axis_id"))]
        if axis_id
    }
    actionable_axis_ids = [
        axis_id
        for axis_id, status in status_map.items()
        if status in _ACTIONABLE_STATUS_VALUES
    ]
    return {
        "evaluator_schema_version": _clean_text(mapping.get("schema_version"))
        or EPISODE_EVALUATOR_CONTRACT_SCHEMA_VERSION,
        "evaluator_axes": [dict(axis) for axis in axes],
        "evaluator_axis_ids": [
            axis_id
            for axis_id in EPISODE_EVALUATOR_AXIS_IDS
            if axis_id in status_map or not axes
        ]
        if axes
        else list(EPISODE_EVALUATOR_AXIS_IDS),
        "evaluator_axis_statuses": status_map,
        "evaluator_axis_confidences": confidence_map,
        "evaluator_axis_reason_codes": reason_code_map,
        "evaluator_evaluated_axis_ids": _normalise_strings(
            mapping.get("evaluated_axis_ids"),
            limit=20,
        ),
        "evaluator_actionable_axis_ids": actionable_axis_ids,
    }


__all__ = [
    "EPISODE_EVALUATOR_AXIS_IDS",
    "EPISODE_EVALUATOR_AXIS_VERSION",
    "EPISODE_EVALUATOR_CONTRACT_SCHEMA_VERSION",
    "build_episode_evaluator_contract",
    "build_episode_evaluator_projection_fields",
]
