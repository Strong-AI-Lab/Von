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


def _normalise_optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            parsed = int(stripped)
            return parsed if parsed > 0 else None
    return None


def _normalise_optional_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if str(key)}


def _normalise_recovery_strategy(
    raw_strategy: Any,
    *,
    effect_id: str,
    index: int,
) -> dict[str, Any]:
    if not isinstance(raw_strategy, Mapping):
        raise ValueError(f"{effect_id}_recovery_strategy_{index}_not_object")

    tool_name = _normalise_text(
        raw_strategy.get("tool")
        or raw_strategy.get("tool_name")
        or raw_strategy.get("recover_tool")
    )
    if not tool_name:
        raise ValueError(f"{effect_id}_recovery_strategy_{index}_tool_missing")

    strategy_id = _normalise_text(
        raw_strategy.get("strategy_id")
        or raw_strategy.get("id")
        or f"{effect_id}_recovery_{index}"
    )
    if not strategy_id:
        raise ValueError(f"{effect_id}_recovery_strategy_{index}_id_missing")

    recover_tools = _normalise_string_list(
        raw_strategy.get("recovers_tools")
        if "recovers_tools" in raw_strategy
        else raw_strategy.get("missing_tools") or raw_strategy.get("applies_to_tools")
    )
    if not recover_tools:
        recover_tools = [tool_name]

    normalised: dict[str, Any] = {
        "strategy_id": strategy_id,
        "tool": tool_name,
        "recovers_tools": recover_tools,
        "description": _normalise_text(raw_strategy.get("description")),
        "target_concept_source": _normalise_text(
            raw_strategy.get("target_concept_source")
            or raw_strategy.get("bind_target_concept_from")
        ),
        "target_concept_argument_name": _normalise_text(
            raw_strategy.get("target_concept_argument_name")
            or raw_strategy.get("target_argument")
        ),
        "target_concept_max_count": _normalise_optional_positive_int(
            raw_strategy.get("target_concept_max_count")
        ),
        "default_payload": _normalise_optional_mapping(
            raw_strategy.get("default_payload")
        ),
    }
    return {
        key: value
        for key, value in normalised.items()
        if value not in ("", [], {}, None)
    }


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
            (
                raw_effect.get("required_tools_match")
                if "required_tools_match" in raw_effect
                else raw_effect.get("tool_match_mode")
            ),
            default="any",
        ),
        "activation_required_tools": activation_required_tools,
        "activation_required_tools_match": _normalise_match_mode(
            (
                raw_effect.get("activation_required_tools_match")
                if "activation_required_tools_match" in raw_effect
                else raw_effect.get("activation_match_mode")
            ),
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
            raw_effect.get("not_executed_reason") or raw_effect.get("missing_reason")
        ),
        "not_satisfied_reason": _normalise_text(
            raw_effect.get("not_satisfied_reason") or raw_effect.get("failed_reason")
        ),
    }
    required_payload_fields = _normalise_string_list(
        raw_effect.get("required_payload_fields")
        or raw_effect.get("readback_required_fields")
    )
    if required_payload_fields:
        normalised["required_payload_fields"] = required_payload_fields
    targets_source_expressions = _normalise_string_list(
        raw_effect.get("targets_source_expressions")
        or raw_effect.get("target_source_expressions")
    )
    single_targets_source_expression = _normalise_text(
        raw_effect.get("targets_source_expression")
        or raw_effect.get("target_source_expression")
        or raw_effect.get("source_expression")
    )
    if single_targets_source_expression:
        targets_source_expressions.append(single_targets_source_expression)
    targets_source_expressions = _normalise_string_list(targets_source_expressions)
    if targets_source_expressions:
        normalised["targets_source_expressions"] = targets_source_expressions
    targets_context_key = _normalise_text(
        raw_effect.get("targets_context_key") or raw_effect.get("target_context_key")
    )
    if targets_context_key:
        normalised["targets_context_key"] = targets_context_key
    targets_extractor = _normalise_text(
        raw_effect.get("targets_extractor") or raw_effect.get("target_extractor")
    )
    if targets_extractor:
        normalised["targets_extractor"] = targets_extractor
    target_resolution_failure_code = _normalise_text(
        raw_effect.get("target_resolution_failure_code")
        or raw_effect.get("targets_resolution_failure_code")
    )
    if target_resolution_failure_code:
        normalised["target_resolution_failure_code"] = target_resolution_failure_code
    raw_recovery_strategies = (
        raw_effect.get("recovery_strategies")
        or raw_effect.get("recovery")
        or raw_effect.get("tool_recovery_strategies")
    )
    if raw_recovery_strategies is not None:
        if not isinstance(raw_recovery_strategies, list):
            raise ValueError(f"{effect_id}_recovery_strategies_not_list")
        recovery_strategies = [
            _normalise_recovery_strategy(
                raw_strategy,
                effect_id=effect_id,
                index=strategy_index + 1,
            )
            for strategy_index, raw_strategy in enumerate(raw_recovery_strategies)
        ]
        if recovery_strategies:
            normalised["recovery_strategies"] = recovery_strategies
    targets = _normalise_string_list(raw_effect.get("targets"))
    if targets:
        normalised["targets"] = targets
    wrong_target_failure_code = _normalise_text(
        raw_effect.get("wrong_target_failure_code")
    )
    if wrong_target_failure_code:
        normalised["wrong_target_failure_code"] = wrong_target_failure_code
    wrong_target_reason = _normalise_text(raw_effect.get("wrong_target_reason"))
    if wrong_target_reason:
        normalised["wrong_target_reason"] = wrong_target_reason
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
        "contract_id": _normalise_text(value.get("contract_id") or value.get("id")),
        "required_effects": required_effects,
    }


__all__ = [
    "WORKFLOW_REQUIRED_EFFECTS_CONTRACT_SCHEMA_VERSION",
    "normalise_workflow_required_effects_contract",
]
