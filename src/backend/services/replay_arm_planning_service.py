"""Reusable support helpers for replay experiment arm metadata.

The helpers in this module shape replay metadata only. They do not choose
models, prompts, promotion policy, or workflow behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


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


def build_replay_arm_plan(
    *,
    model_arms: Sequence[Mapping[str, Any]],
    base_prompt_id: str | None,
    prompt_variant_ids: Sequence[Any],
    workflow_stage_id: str | None,
    target_workflow_id: str | None,
    replay_set_id: str | None,
    replay_case_id: str | None,
    default_replay_set_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build replay arm metadata for model/prompt-variant experiments."""

    base_prompt = _safe_text(base_prompt_id) or None
    variants = _dedupe_texts(prompt_variant_ids)
    stage_id = _safe_text(workflow_stage_id) or None
    workflow_id = _safe_text(target_workflow_id) or None
    default_set_id = _safe_text(default_replay_set_id) or None
    set_id = _safe_text(replay_set_id) or (default_set_id if variants else None)
    case_id = _safe_text(replay_case_id) or None

    if not variants:
        planned: list[dict[str, Any]] = []
        for model_arm in model_arms:
            arm = dict(model_arm)
            if base_prompt:
                arm["base_prompt_id"] = base_prompt
            if stage_id:
                arm["workflow_stage_id"] = stage_id
            if workflow_id:
                arm["target_workflow_id"] = workflow_id
            if set_id:
                arm["replay_set_id"] = set_id
            if case_id:
                arm["replay_case_id"] = case_id
            planned.append(arm)
        return planned

    planned = []
    for model_arm in model_arms:
        model_label = _safe_text(model_arm.get("label")) or _safe_text(
            model_arm.get("arm_id")
        )
        base_arm = dict(model_arm)
        base_arm.update(
            {
                "arm_id": f"arm_{len(planned) + 1}",
                "model_arm_id": _safe_text(model_arm.get("arm_id")) or None,
                "label": f"{model_label}:base_prompt" if model_label else "base_prompt",
                "base_prompt_id": base_prompt,
                "candidate_prompt_variant_id": None,
                "workflow_stage_id": stage_id,
                "target_workflow_id": workflow_id,
                "replay_set_id": set_id,
                "replay_case_id": case_id,
            }
        )
        planned.append(base_arm)
        for variant_id in variants:
            variant_label = variant_id.rsplit("#", 1)[-1].replace("#V", "V")
            arm = dict(model_arm)
            arm.update(
                {
                    "arm_id": f"arm_{len(planned) + 1}",
                    "model_arm_id": _safe_text(model_arm.get("arm_id")) or None,
                    "label": (
                        f"{model_label}:{variant_label}"
                        if model_label
                        else variant_label
                    ),
                    "base_prompt_id": base_prompt,
                    "candidate_prompt_variant_id": variant_id,
                    "workflow_stage_id": stage_id,
                    "target_workflow_id": workflow_id,
                    "replay_set_id": set_id,
                    "replay_case_id": case_id,
                }
            )
            planned.append(arm)
    return planned


def build_arm_session_name(
    *,
    base_session_name: str,
    arm_metadata: Mapping[str, Any] | None,
    default_session_name: str,
) -> str:
    """Attach stable arm metadata to the live replay session name."""

    if not arm_metadata:
        return _safe_text(base_session_name) or default_session_name
    cleaned_base = _safe_text(base_session_name) or default_session_name
    arm_id = _safe_text(arm_metadata.get("arm_id")) or "arm"
    label = _safe_text(arm_metadata.get("label")) or arm_id
    return f"{cleaned_base} [{arm_id}:{label}]"


def build_arm_run_environment(
    *,
    shared_run_environment: Mapping[str, Any],
    requested_model: str | None,
    session_name: str,
    arm_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Project replay arm metadata into a per-arm run-environment summary."""

    run_environment = dict(shared_run_environment)
    run_environment["requested_model"] = _safe_text(requested_model) or None
    run_environment["session_name"] = _safe_text(session_name) or None
    if arm_metadata:
        run_environment["comparison_arm_id"] = (
            _safe_text(arm_metadata.get("arm_id")) or None
        )
        run_environment["comparison_arm_label"] = (
            _safe_text(arm_metadata.get("label")) or None
        )
        for optional_key in (
            "model_arm_id",
            "base_prompt_id",
            "candidate_prompt_variant_id",
            "workflow_stage_id",
            "target_workflow_id",
            "replay_set_id",
            "replay_case_id",
        ):
            optional_value = _safe_text(arm_metadata.get(optional_key))
            if optional_value:
                run_environment[optional_key] = optional_value
    return run_environment
