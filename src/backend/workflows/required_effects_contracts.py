"""Generic workflow-authored required-effects contracts for turn completion.

These contracts let a workflow definition declare required evidence or other
non-mutation effects that must be observed before a turn can safely claim
completion.
"""

from __future__ import annotations

from typing import Any, Mapping

WORKFLOW_REQUIRED_EFFECTS_CONTRACT_SCHEMA_VERSION = (
    "workflow_required_effects_contract.v1"
)


def _normalise_text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _normalise_text(item)
        lowered = text.lower()
        if not text or lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(text)
    return ordered


def _normalise_match_mode(value: Any, *, default: str) -> str:
    token = _normalise_text(value).lower().replace("-", "_")
    if token in {"all", "every"}:
        return "all"
    if token in {"any", "one", "some"}:
        return "any"
    return default


def _normalise_required_effect_template(
    raw_effect: Any,
    *,
    index: int,
) -> dict[str, Any]:
    if not isinstance(raw_effect, Mapping):
        raise ValueError(f"required_effect_{index}_not_object")

    effect_id = _normalise_text(
        raw_effect.get("effect_id") or raw_effect.get("id") or f"effect_{index}"
    )
    if not effect_id:
        raise ValueError(f"required_effect_{index}_id_missing")

    effect_type = _normalise_text(raw_effect.get("effect_type"))
    if not effect_type:
        raise ValueError(f"required_effect_{index}_type_missing")

    required_tools = _normalise_string_list(
        raw_effect.get("required_tools")
        if "required_tools" in raw_effect
        else raw_effect.get("tools")
    )
    if not required_tools:
        raise ValueError(f"required_effect_{index}_tools_missing")

    activation_required_tools = _normalise_string_list(
        raw_effect.get("activation_required_tools")
        if "activation_required_tools" in raw_effect
        else (
            raw_effect.get("activation_tools")
            or raw_effect.get("activate_when_tools_present")
        )
    )

    normalised = {
        "effect_id": effect_id,
        "effect_type": effect_type,
        "description": _normalise_text(raw_effect.get("description")),
        "required_tools": required_tools,
        "required_tools_match": _normalise_match_mode(
            raw_effect.get("required_tools_match")
            if "required_tools_match" in raw_effect
            else raw_effect.get("tool_match_mode"),
            default="any",
        ),
        "activation_required_tools": activation_required_tools,
        "activation_required_tools_match": _normalise_match_mode(
            raw_effect.get("activation_required_tools_match")
            if "activation_required_tools_match" in raw_effect
            else raw_effect.get("activation_match_mode"),
            default="any",
        ),
        "missing_failure_code": _normalise_text(
            raw_effect.get("missing_failure_code")
            or raw_effect.get("not_executed_failure_code")
        ),
        "failed_failure_code": _normalise_text(
            raw_effect.get("failed_failure_code")
            or raw_effect.get("not_satisfied_failure_code")
        ),
        "not_executed_reason": _normalise_text(
            raw_effect.get("not_executed_reason")
            or raw_effect.get("missing_reason")
        ),
        "not_satisfied_reason": _normalise_text(
            raw_effect.get("not_satisfied_reason")
            or raw_effect.get("failed_reason")
        ),
    }
    return normalised


def normalise_workflow_required_effects_contract(
    value: Any,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("workflow_required_effects_contract_not_object")

    raw_effects = value.get("required_effects")
    if not isinstance(raw_effects, list):
        raise ValueError("workflow_required_effects_contract_effects_not_list")

    required_effects = [
        _normalise_required_effect_template(raw_effect, index=index + 1)
        for index, raw_effect in enumerate(raw_effects)
    ]
    if not required_effects:
        raise ValueError("workflow_required_effects_contract_effects_empty")

    return {
        "schema_version": WORKFLOW_REQUIRED_EFFECTS_CONTRACT_SCHEMA_VERSION,
        "contract_id": _normalise_text(
            value.get("contract_id") or value.get("id")
        ),
        "required_effects": required_effects,
    }


__all__ = [
    "WORKFLOW_REQUIRED_EFFECTS_CONTRACT_SCHEMA_VERSION",
    "normalise_workflow_required_effects_contract",
]
