"""Lightweight workflow executor for orchestrator-managed workflows."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, MutableMapping, Sequence

from .action_registry import (
    ActionRegistry,
    WORKFLOW_ACTION_OUTCOME_SUCCESS,
    WORKFLOW_ACTION_OUTCOME_FAILURE,
    WORKFLOW_ACTION_OUTCOME_UNKNOWN,
    WorkflowActionResult,
    WorkflowEnvironment,
    normalise_action_outcome,
)
from .metadata_validation import (
    METADATA_VALIDATION_MODE_OFF,
    apply_metadata_validation_mode,
    append_metadata_validation_event,
    format_metadata_validation_error,
    get_metadata_validation_mode,
    metadata_validation_failures_are_enforced,
    skipped_metadata_validation,
    validate_state_metadata_post_action,
    validate_state_metadata_pre_action,
)
from .context_paths import measure_cardinality, resolve_context_path
from .execution_contracts import (
    WORKFLOW_CONTROL_SIGNAL_BREAK,
    WORKFLOW_CONTROL_SIGNAL_CONTINUE,
    WORKFLOW_CONTROL_SIGNAL_RETURN,
    WORKFLOW_RETURN_PAYLOAD_KEY,
    append_runtime_event,
    append_step_result_envelope,
    build_step_result_envelope,
    build_workflow_result_envelope,
    clear_control_signal_context,
    get_last_control_signal,
    get_last_control_signal_scope,
    resolve_control_signal_from_outputs,
    set_workflow_result_envelope,
    snapshot_workflow_mapping,
    stamp_control_signal_context,
)
from .plan_state_runtime import (
    apply_workflow_step_checkpoint,
    evaluate_workflow_completion_gate,
    mark_workflow_plan_state_entry,
)
from .trace_model import WorkflowExecutionTrace

logger = logging.getLogger(__name__)

_CONTEXT_BINDING_KEYS: tuple[str, ...] = (
    "$context_key",
    "context_key",
    "workflow_context_key",
    "from_context_key",
)
_NESTED_CONTEXT_KEYS: tuple[str, ...] = (
    "facts",
    "state",
    "flags",
    "variables",
    "effects",
    "preconditions",
    "conditions",
)
WORKFLOW_TOOL_OUTPUT_MAPPING_EVENTS_KEY = "workflow_tool_output_mapping_events"
LAST_WORKFLOW_TOOL_OUTPUT_MAPPING_EVENT_KEY = "last_workflow_tool_output_mapping_event"
WORKFLOW_TERMINAL_EFFECT_EVENTS_KEY = "workflow_terminal_effect_events"
LAST_WORKFLOW_TERMINAL_EFFECT_EVENT_KEY = "last_workflow_terminal_effect_event"
WORKFLOW_APPROVAL_GATE_EVENTS_KEY = "workflow_approval_gate_events"
LAST_WORKFLOW_APPROVAL_GATE_KEY = "last_workflow_approval_gate"
WORKFLOW_RETRY_EVENTS_KEY = "workflow_retry_events"
LAST_WORKFLOW_RETRY_EVENT_KEY = "last_workflow_retry_event"
WORKFLOW_IDEMPOTENCY_EVENTS_KEY = "workflow_idempotency_events"
LAST_WORKFLOW_IDEMPOTENCY_EVENT_KEY = "last_workflow_idempotency_event"
WORKFLOW_IDEMPOTENCY_RECORDS_KEY = "workflow_idempotency_records"

WORKFLOW_RETRY_POLICY_SCHEMA_VERSION = "workflow_step_retry_policy.v1"
WORKFLOW_APPROVAL_GATE_SCHEMA_VERSION = "workflow_step_approval_gate.v1"
WORKFLOW_IDEMPOTENCY_POLICY_SCHEMA_VERSION = "workflow_step_idempotency_policy.v1"
WORKFLOW_STEP_EXECUTION_MODE_LLM = "llm"
WORKFLOW_STEP_EXECUTION_MODE_DETERMINISTIC = "deterministic"
WORKFLOW_STEP_EXECUTION_MODE_SUBWORKFLOW = "subworkflow"
WORKFLOW_STEP_EXECUTION_MODE_CONTROL = "control"
WORKFLOW_STEP_EXECUTION_MODES = frozenset(
    {
        WORKFLOW_STEP_EXECUTION_MODE_LLM,
        WORKFLOW_STEP_EXECUTION_MODE_DETERMINISTIC,
        WORKFLOW_STEP_EXECUTION_MODE_SUBWORKFLOW,
        WORKFLOW_STEP_EXECUTION_MODE_CONTROL,
    }
)


def normalise_workflow_step_execution_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in WORKFLOW_STEP_EXECUTION_MODES:
        return raw
    return WORKFLOW_STEP_EXECUTION_MODE_DETERMINISTIC


def _extract_context_binding_symbol(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in _CONTEXT_BINDING_KEYS:
        symbol = value.get(key)
        if isinstance(symbol, str) and symbol.strip():
            return symbol.strip()
    return None


def _resolve_context_symbol(
    *,
    context: Mapping[str, Any],
    symbol: str,
) -> tuple[bool, Any]:
    if symbol in context:
        return True, context[symbol]

    symbol_without_prefix = symbol[3:] if symbol.startswith("#V#") else symbol
    if symbol_without_prefix in context:
        return True, context[symbol_without_prefix]

    for container_key in _NESTED_CONTEXT_KEYS:
        container = context.get(container_key)
        if isinstance(container, Mapping):
            if symbol in container:
                return True, container[symbol]
            if symbol_without_prefix in container:
                return True, container[symbol_without_prefix]
    return False, None


def resolve_action_inputs_from_context(
    *,
    action_inputs: Mapping[str, Any],
    context: Mapping[str, Any],
) -> Dict[str, Any]:
    """Resolve dynamic action input bindings against the current workflow context.

    Vontology mappings may encode input values as mapping objects such as
    ``{"$context_key": "target_type_id"}``. These are resolved to concrete
    payload values before action execution.
    """
    def _resolve_nested_input_value(value: Any) -> Any:
        symbol = _extract_context_binding_symbol(value)
        if symbol:
            found, concrete_value = _resolve_context_symbol(
                context=context,
                symbol=symbol,
            )
            return concrete_value if found else None

        if isinstance(value, Mapping):
            return {
                str(nested_key): _resolve_nested_input_value(nested_value)
                for nested_key, nested_value in value.items()
            }

        if isinstance(value, list):
            return [_resolve_nested_input_value(item) for item in value]

        if isinstance(value, tuple):
            return tuple(_resolve_nested_input_value(item) for item in value)

        return value

    resolved: Dict[str, Any] = {}
    for input_key, input_value in action_inputs.items():
        resolved[input_key] = _resolve_nested_input_value(input_value)
    return resolved


def _normalise_context_key_for_mapping(raw_symbol: str) -> str:
    symbol = str(raw_symbol or "").strip()
    if symbol.startswith("#V#workflow_context_key_"):
        return symbol[len("#V#workflow_context_key_") :]
    if symbol.startswith("workflow_context_key_"):
        return symbol[len("workflow_context_key_") :]
    if symbol.startswith("#V#"):
        return symbol[3:]
    return symbol


def _normalise_tool_output_context_mappings(
    value: Any,
) -> List[Dict[str, str]]:
    raw_items: List[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        raw_items = [value]
    elif isinstance(value, list):
        raw_items = [item for item in value if isinstance(item, Mapping)]
    else:
        return []

    normalised: List[Dict[str, str]] = []
    for item in raw_items:
        tool_output_field = str(
            item.get("tool_output_field")
            or item.get("tool_output_field_name")
            or item.get("tool_field")
            or item.get("output_field")
            or ""
        ).strip()
        context_key_raw = str(
            item.get("context_key")
            or item.get("target_context_key_concept_id")
            or item.get("context_key_concept_id")
            or item.get("workflow_context_key")
            or item.get("target_context_key")
            or ""
        ).strip()
        context_key = _normalise_context_key_for_mapping(context_key_raw)
        mapping_concept_id = str(item.get("mapping_concept_id") or "").strip()
        if not tool_output_field or not context_key:
            continue
        mapping_spec = {
            "tool_output_field": tool_output_field,
            "context_key": context_key,
        }
        if mapping_concept_id:
            mapping_spec["mapping_concept_id"] = mapping_concept_id
        normalised.append(mapping_spec)
    return normalised


def _lookup_nested_field(
    root: Mapping[str, Any],
    path_parts: Sequence[str],
) -> tuple[bool, Any]:
    current: Any = root
    for part in path_parts:
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current.get(part)
    return True, current


def _extract_tool_output_field_value(
    *,
    action_outputs: Mapping[str, Any],
    tool_output_field: str,
) -> tuple[bool, Any, str | None]:
    field = str(tool_output_field or "").strip()
    if not field:
        return False, None, None

    path_parts = [part.strip() for part in field.split(".") if part.strip()]
    output_roots: List[tuple[str, Mapping[str, Any]]] = [("outputs", action_outputs)]

    for root_name in ("result", "mcp_result", "payload", "data"):
        nested = action_outputs.get(root_name)
        if isinstance(nested, Mapping):
            output_roots.append((root_name, nested))

    for root_name, root in output_roots:
        if field in root:
            resolved_path = field if root_name == "outputs" else f"{root_name}.{field}"
            return True, root[field], resolved_path
        if path_parts:
            found, value = _lookup_nested_field(root, path_parts)
            if found:
                joined = ".".join(path_parts)
                resolved_path = joined if root_name == "outputs" else f"{root_name}.{joined}"
                return True, value, resolved_path

    return False, None, None


def apply_tool_output_context_mappings(
    *,
    context: Dict[str, Any],
    metadata: Mapping[str, Any],
    action_outputs: Mapping[str, Any],
    state_id: str,
    action_id: str,
) -> List[Dict[str, Any]]:
    """Apply declared tool-output→context mappings for a workflow state.

    Mapping declarations are carried in ``metadata["tool_output_context_mappings"]``.
    This helper executes those declarations after each action so metadata
    validation can enforce ``writes_context_keys`` contracts deterministically.
    """

    mapping_specs = _normalise_tool_output_context_mappings(
        metadata.get("tool_output_context_mappings")
    )
    if not mapping_specs:
        return []

    events: List[Dict[str, Any]] = []
    for mapping_spec in mapping_specs:
        tool_output_field = mapping_spec["tool_output_field"]
        context_key = mapping_spec["context_key"]
        mapping_concept_id = mapping_spec.get("mapping_concept_id")

        found, value, resolved_path = _extract_tool_output_field_value(
            action_outputs=action_outputs,
            tool_output_field=tool_output_field,
        )
        if found:
            context[context_key] = value

        event: Dict[str, Any] = {
            "status": "tool_output_mapping",
            "state_id": state_id,
            "action_id": action_id,
            "tool_output_field": tool_output_field,
            "context_key": context_key,
            "value_present": found,
            "applied": found,
        }
        if mapping_concept_id:
            event["mapping_concept_id"] = mapping_concept_id
        if resolved_path:
            event["resolved_path"] = resolved_path
        if not found:
            event["reason_code"] = "tool_output_field_missing"
        events.append(event)

    if events:
        existing = context.get(WORKFLOW_TOOL_OUTPUT_MAPPING_EVENTS_KEY)
        if not isinstance(existing, list):
            existing = []
            context[WORKFLOW_TOOL_OUTPUT_MAPPING_EVENTS_KEY] = existing
        existing.extend(events)
        context[LAST_WORKFLOW_TOOL_OUTPUT_MAPPING_EVENT_KEY] = events[-1]

    return events


def _normalise_metadata_symbols(value: Any) -> List[str]:
    if isinstance(value, str):
        candidate = value.strip()
        return [candidate] if candidate else []
    if not isinstance(value, list):
        return []
    symbols: List[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        candidate = item.strip()
        if candidate:
            symbols.append(candidate)
    return symbols


def _looks_like_terminal_effect_symbol(symbol: str) -> bool:
    token = str(symbol or "").strip().lower()
    if token.startswith("#v#"):
        token = token[3:]
    return token.endswith("_terminal") or token.endswith(":terminal")


def materialise_terminal_effect_context(
    *,
    context: Dict[str, Any],
    state_spec: WorkflowStateSpec,
    state_id: str,
) -> List[Dict[str, Any]]:
    """Materialise terminal-effect evidence into context before validation.

    Vontology-authored workflows can declare synthetic terminal effects
    (e.g., ``#V#workflow_effect_<workflow>_<state>_terminal``) on terminal
    states. These effects are execution-boundary evidence rather than outputs
    from a concrete action, so the engine materialises them deterministically
    before post-action metadata validation.
    """

    metadata = state_spec.metadata if isinstance(state_spec.metadata, Mapping) else {}
    effect_symbols = [
        symbol
        for symbol in _normalise_metadata_symbols(metadata.get("effects"))
        if _looks_like_terminal_effect_symbol(symbol)
    ]
    if not effect_symbols:
        return []

    context["workflow_terminal"] = True
    context["workflow_terminal_state"] = state_id

    events: List[Dict[str, Any]] = []
    for symbol in effect_symbols:
        was_present = bool(context.get(symbol))
        context[symbol] = True
        alias = symbol[3:] if symbol.startswith("#V#") else symbol
        alias_was_present = bool(context.get(alias)) if alias else False
        if alias:
            context[alias] = True

        events.append(
            {
                "status": "terminal_effect_materialised",
                "state_id": state_id,
                "symbol": symbol,
                "alias": alias if alias else None,
                "already_present": was_present,
                "alias_already_present": alias_was_present,
                "applied": (not was_present) or (bool(alias) and not alias_was_present),
            }
        )

    existing_events = context.get(WORKFLOW_TERMINAL_EFFECT_EVENTS_KEY)
    if not isinstance(existing_events, list):
        existing_events = []
        context[WORKFLOW_TERMINAL_EFFECT_EVENTS_KEY] = existing_events
    existing_events.extend(events)
    context[LAST_WORKFLOW_TERMINAL_EFFECT_EVENT_KEY] = events[-1]

    return events


def _append_context_event(
    *,
    context: Dict[str, Any],
    key: str,
    last_key: str,
    event: Mapping[str, Any],
) -> None:
    existing = context.get(key)
    if not isinstance(existing, list):
        existing = []
        context[key] = existing
    existing.append(dict(event))
    context[last_key] = dict(event)


def _apply_action_result_context(
    *,
    context: Dict[str, Any],
    action_id: str,
    result: WorkflowActionResult,
) -> str:
    """Mirror ActionRegistry outcome stamping for synthetic engine-side results."""

    action_outcome = normalise_action_outcome(result.status)
    context["last_action_id"] = action_id
    context["last_action_status"] = result.status
    context["last_action_outcome"] = action_outcome
    context["last_action_succeeded"] = action_outcome == WORKFLOW_ACTION_OUTCOME_SUCCESS
    context["last_action_failed"] = action_outcome == WORKFLOW_ACTION_OUTCOME_FAILURE
    context["last_action_unknown"] = action_outcome == WORKFLOW_ACTION_OUTCOME_UNKNOWN
    context["last_step_ok"] = action_outcome == WORKFLOW_ACTION_OUTCOME_SUCCESS
    context["last_step_outcome"] = action_outcome
    context["last_action_error"] = result.error
    context["last_action_call_id"] = result.call_id
    context["last_action_duration_ms"] = result.duration_ms

    control_signal, control_scope, return_payload = resolve_control_signal_from_outputs(
        action_outcome=action_outcome,
        outputs=result.outputs,
    )
    stamp_control_signal_context(
        context=context,
        signal=control_signal,
        scope=control_scope,
        return_payload=return_payload,
    )
    return action_outcome


def execute_workflow_step_invocation(
    *,
    registry: ActionRegistry,
    workflow_id: str,
    workflow_state_id: str,
    workflow_state_metadata: Mapping[str, Any] | None,
    action: WorkflowActionInvocation,
    resolved_inputs: MutableMapping[str, Any],
    context: Dict[str, Any],
    env: WorkflowEnvironment,
    trace: WorkflowExecutionTrace | None = None,
) -> WorkflowActionResult:
    if action.execution_mode == WORKFLOW_STEP_EXECUTION_MODE_LLM:
        from .llm_step_executor import execute_llm_step
        from .action_registry import WorkflowActionRequest

        request = WorkflowActionRequest(
            action_id=action.target_id,
            inputs=resolved_inputs,
            environment=env,
            data=context,
            trace=trace,
            action_target_id=action.target_id,
            contract_concept_id=action.contract_concept_id,
            execution_mode=action.execution_mode,
            prompt_contract=(
                dict(action.prompt_contract)
                if isinstance(action.prompt_contract, Mapping)
                else None
            ),
            llm_policy=(
                dict(action.llm_policy)
                if isinstance(action.llm_policy, Mapping)
                else None
            ),
            validation_policy=(
                dict(action.validation_policy)
                if isinstance(action.validation_policy, Mapping)
                else None
            ),
            workflow_id=workflow_id,
            workflow_state_id=workflow_state_id,
            workflow_state_metadata=(
                dict(workflow_state_metadata)
                if isinstance(workflow_state_metadata, Mapping)
                else None
            ),
        )
        return execute_llm_step(request)

    return registry.execute(
        action.target_id,
        inputs=resolved_inputs,
        context=context,
        env=env,
        trace=trace,
        workflow_id=workflow_id,
        workflow_state_id=workflow_state_id,
        workflow_state_metadata=workflow_state_metadata,
    )


def _resolve_condition_path_key(condition_spec: Mapping[str, Any]) -> str:
    candidate = condition_spec.get("path")
    if candidate is None:
        candidate = condition_spec.get("key")
    return str(candidate or "").strip()


def _normalise_condition_path_key(
    condition_spec: Mapping[str, Any],
    *,
    missing_reason_code: str,
) -> str:
    key = _resolve_condition_path_key(condition_spec)
    if not key:
        raise ValueError(f"workflow_condition_invalid:{missing_reason_code}")
    return key


def _normalise_compare_operator(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "==": "eq",
        "=": "eq",
        "eq": "eq",
        "!=": "ne",
        "<>": "ne",
        "ne": "ne",
        ">": "gt",
        "gt": "gt",
        ">=": "gte",
        "gte": "gte",
        "<": "lt",
        "lt": "lt",
        "<=": "lte",
        "lte": "lte",
    }
    operator = aliases.get(raw, "")
    if not operator:
        raise ValueError("workflow_condition_invalid:compare_operator_invalid")
    return operator


def _normalise_positive_int(
    value: Any,
    *,
    field_name: str,
    min_value: int = 0,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"workflow_condition_invalid:{field_name}_not_int") from exc
    if parsed < min_value:
        raise ValueError(f"workflow_condition_invalid:{field_name}_below_minimum")
    return parsed


def _coerce_numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _evaluate_compare(
    *,
    left: Any,
    operator: str,
    right: Any,
) -> bool:
    left_num = _coerce_numeric(left)
    right_num = _coerce_numeric(right)
    if left_num is not None and right_num is not None:
        if operator == "eq":
            return left_num == right_num
        if operator == "ne":
            return left_num != right_num
        if operator == "gt":
            return left_num > right_num
        if operator == "gte":
            return left_num >= right_num
        if operator == "lt":
            return left_num < right_num
        if operator == "lte":
            return left_num <= right_num

    left_text = "" if left is None else str(left)
    right_text = "" if right is None else str(right)
    if operator == "eq":
        return left_text == right_text
    if operator == "ne":
        return left_text != right_text
    if operator == "gt":
        return left_text > right_text
    if operator == "gte":
        return left_text >= right_text
    if operator == "lt":
        return left_text < right_text
    if operator == "lte":
        return left_text <= right_text
    return False


def _coerce_bool_like(
    value: Any,
    *,
    field_name: str,
) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"workflow_condition_invalid:{field_name}_not_boolean")


def _normalise_transition_condition_spec(
    condition_spec: Mapping[str, Any],
) -> Dict[str, Any]:
    if not isinstance(condition_spec, Mapping):
        raise ValueError("workflow_condition_invalid:condition_spec_not_mapping")

    raw_kind = condition_spec.get("kind")
    kind = str(raw_kind or "").strip().lower()
    if not kind:
        raise ValueError("workflow_condition_invalid:condition_kind_missing")

    if kind == "always":
        return {"kind": "always"}

    if kind == "context_flag":
        key = _normalise_condition_path_key(
            condition_spec,
            missing_reason_code="context_flag_key_missing",
        )
        expected = _coerce_bool_like(
            condition_spec.get("expected", True),
            field_name="context_flag_expected",
        )
        return {"kind": "context_flag", "key": key, "expected": expected}

    if kind == "context_value_equals":
        key = _normalise_condition_path_key(
            condition_spec,
            missing_reason_code="context_value_key_missing",
        )
        if "value" not in condition_spec:
            raise ValueError("workflow_condition_invalid:context_value_missing")
        return {
            "kind": "context_value_equals",
            "key": key,
            "value": condition_spec.get("value"),
        }

    if kind == "context_exists":
        key = _normalise_condition_path_key(
            condition_spec,
            missing_reason_code="context_exists_key_missing",
        )
        expected = _coerce_bool_like(
            condition_spec.get("expected", True),
            field_name="context_exists_expected",
        )
        return {"kind": "context_exists", "key": key, "expected": expected}

    if kind == "context_is_null":
        key = _normalise_condition_path_key(
            condition_spec,
            missing_reason_code="context_is_null_key_missing",
        )
        expected = _coerce_bool_like(
            condition_spec.get("expected", True),
            field_name="context_is_null_expected",
        )
        return {"kind": "context_is_null", "key": key, "expected": expected}

    if kind == "context_compare":
        key = _normalise_condition_path_key(
            condition_spec,
            missing_reason_code="context_compare_key_missing",
        )
        if "value" not in condition_spec:
            raise ValueError("workflow_condition_invalid:context_compare_value_missing")
        return {
            "kind": "context_compare",
            "key": key,
            "operator": _normalise_compare_operator(condition_spec.get("operator")),
            "value": condition_spec.get("value"),
        }

    if kind == "context_cardinality":
        key = _normalise_condition_path_key(
            condition_spec,
            missing_reason_code="context_cardinality_key_missing",
        )
        if "value" not in condition_spec:
            raise ValueError(
                "workflow_condition_invalid:context_cardinality_value_missing"
            )
        return {
            "kind": "context_cardinality",
            "key": key,
            "operator": _normalise_compare_operator(condition_spec.get("operator")),
            "value": _normalise_positive_int(
                condition_spec.get("value"),
                field_name="context_cardinality_value",
                min_value=0,
            ),
        }

    if kind == "transition_result_truth":
        expected = _coerce_bool_like(
            condition_spec.get("expected", True),
            field_name="transition_result_truth_expected",
        )
        return {
            "kind": "transition_result_truth",
            "expected": expected,
        }

    if kind == "control_signal":
        signal = str(condition_spec.get("signal") or "").strip().lower()
        if signal not in {"break", "continue", "return", "error"}:
            raise ValueError("workflow_condition_invalid:control_signal_invalid")
        scope = str(condition_spec.get("scope") or "").strip()
        payload: Dict[str, Any] = {"kind": "control_signal", "signal": signal}
        if scope:
            payload["scope"] = scope
        return payload

    if kind == "all":
        children = condition_spec.get("conditions")
        if not isinstance(children, list) or not children:
            raise ValueError("workflow_condition_invalid:all_conditions_missing")
        return {
            "kind": "all",
            "conditions": [
                _normalise_transition_condition_spec(item)
                for item in children
                if isinstance(item, Mapping)
            ],
        }

    if kind == "any":
        children = condition_spec.get("conditions")
        if not isinstance(children, list) or not children:
            raise ValueError("workflow_condition_invalid:any_conditions_missing")
        normalised_children = [
            _normalise_transition_condition_spec(item)
            for item in children
            if isinstance(item, Mapping)
        ]
        if not normalised_children:
            raise ValueError("workflow_condition_invalid:any_conditions_missing")
        return {
            "kind": "any",
            "conditions": normalised_children,
        }

    if kind == "not":
        child = condition_spec.get("condition")
        if not isinstance(child, Mapping):
            raise ValueError("workflow_condition_invalid:not_condition_missing")
        return {
            "kind": "not",
            "condition": _normalise_transition_condition_spec(child),
        }

    raise ValueError(f"workflow_condition_invalid:unsupported_kind:{kind}")


def _evaluate_transition_result_truth(context: Mapping[str, Any]) -> bool:
    transition_result = context.get("transition_result")
    if transition_result is not None:
        if isinstance(transition_result, Mapping) and "result" in transition_result:
            return bool(transition_result.get("result"))
        return bool(transition_result)

    if "result" in context:
        result_value = context.get("result")
        if isinstance(result_value, Mapping) and "result" in result_value:
            return bool(result_value.get("result"))
        return bool(result_value)
    return bool(context.get("last_step_ok"))


def evaluate_transition_condition_spec(
    *,
    context: Mapping[str, Any],
    condition_spec: Mapping[str, Any],
) -> bool:
    kind = str(condition_spec.get("kind") or "").strip().lower()
    if kind == "always":
        return True
    if kind == "context_flag":
        key = str(condition_spec.get("key") or "")
        expected = bool(condition_spec.get("expected", True))
        found, value = resolve_context_path(context=context, path=key)
        if not found:
            return (False) is expected
        return bool(value) is expected
    if kind == "context_value_equals":
        key = str(condition_spec.get("key") or "")
        found, value = resolve_context_path(context=context, path=key)
        if not found:
            return False
        return value == condition_spec.get("value")
    if kind == "context_exists":
        key = str(condition_spec.get("key") or "")
        expected = bool(condition_spec.get("expected", True))
        found, _value = resolve_context_path(context=context, path=key)
        return found is expected
    if kind == "context_is_null":
        key = str(condition_spec.get("key") or "")
        expected = bool(condition_spec.get("expected", True))
        found, value = resolve_context_path(context=context, path=key)
        return (found and value is None) is expected
    if kind == "context_compare":
        key = str(condition_spec.get("key") or "")
        found, value = resolve_context_path(context=context, path=key)
        if not found:
            return False
        return _evaluate_compare(
            left=value,
            operator=str(condition_spec.get("operator") or ""),
            right=condition_spec.get("value"),
        )
    if kind == "context_cardinality":
        key = str(condition_spec.get("key") or "")
        found, value = resolve_context_path(context=context, path=key)
        if not found:
            return False
        cardinality = measure_cardinality(value)
        if cardinality is None:
            return False
        return _evaluate_compare(
            left=cardinality,
            operator=str(condition_spec.get("operator") or ""),
            right=condition_spec.get("value"),
        )
    if kind == "transition_result_truth":
        expected = bool(condition_spec.get("expected", True))
        return _evaluate_transition_result_truth(context) is expected
    if kind == "control_signal":
        expected_signal = str(condition_spec.get("signal") or "").strip().lower()
        observed_signal = get_last_control_signal(context)
        if observed_signal != expected_signal:
            return False
        expected_scope = str(condition_spec.get("scope") or "").strip()
        if not expected_scope:
            return True
        observed_scope = get_last_control_signal_scope(context)
        return bool(observed_scope) and observed_scope == expected_scope
    if kind == "all":
        children = condition_spec.get("conditions") or []
        if not isinstance(children, list):
            return False
        return all(
            evaluate_transition_condition_spec(
                context=context,
                condition_spec=child,
            )
            for child in children
            if isinstance(child, Mapping)
        )
    if kind == "any":
        children = condition_spec.get("conditions") or []
        if not isinstance(children, list):
            return False
        return any(
            evaluate_transition_condition_spec(
                context=context,
                condition_spec=child,
            )
            for child in children
            if isinstance(child, Mapping)
        )
    if kind == "not":
        child = condition_spec.get("condition")
        if not isinstance(child, Mapping):
            return False
        return not evaluate_transition_condition_spec(
            context=context,
            condition_spec=child,
        )
    return False


def compile_transition_condition_spec(
    condition_spec: Mapping[str, Any],
) -> Dict[str, Any]:
    return _normalise_transition_condition_spec(condition_spec)


def build_transition_condition(
    condition_spec: Mapping[str, Any],
) -> tuple[Dict[str, Any], Callable[[Dict[str, Any]], bool]]:
    normalised = compile_transition_condition_spec(condition_spec)

    def _condition(context: Dict[str, Any]) -> bool:
        return evaluate_transition_condition_spec(
            context=context,
            condition_spec=normalised,
        )

    return normalised, _condition


@dataclass(frozen=True)
class WorkflowActionInvocation:
    action_id: str | None = None
    inputs: Mapping[str, Any] = field(default_factory=dict)
    description: str | None = None
    contract_concept_id: str | None = None
    execution_mode: str = WORKFLOW_STEP_EXECUTION_MODE_DETERMINISTIC
    prompt_contract: Mapping[str, Any] | None = None
    llm_policy: Mapping[str, Any] | None = None
    validation_policy: Mapping[str, Any] | None = None
    subworkflow_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "execution_mode",
            normalise_workflow_step_execution_mode(self.execution_mode),
        )

    @property
    def is_llm_step(self) -> bool:
        return self.execution_mode == WORKFLOW_STEP_EXECUTION_MODE_LLM

    @property
    def is_subworkflow_step(self) -> bool:
        return self.execution_mode == WORKFLOW_STEP_EXECUTION_MODE_SUBWORKFLOW

    @property
    def target_id(self) -> str:
        action_id = str(self.action_id or "").strip()
        if action_id:
            return action_id
        subworkflow_id = str(self.subworkflow_id or "").strip()
        if subworkflow_id:
            return subworkflow_id
        prompt_contract = (
            self.prompt_contract
            if isinstance(self.prompt_contract, Mapping)
            else {}
        )
        resolved_prompt_id = str(
            prompt_contract.get("resolved_prompt_concept_id") or ""
        ).strip()
        if resolved_prompt_id:
            return f"llm_prompt:{resolved_prompt_id}"
        requested_prompt_ids = prompt_contract.get("requested_prompt_concept_ids") or []
        if isinstance(requested_prompt_ids, Sequence) and not isinstance(
            requested_prompt_ids, str
        ):
            for prompt_id in requested_prompt_ids:
                candidate = str(prompt_id or "").strip()
                if candidate:
                    return f"llm_prompt:{candidate}"
        return f"workflow_step.{self.execution_mode}"


WorkflowStepExecutionSpec = WorkflowActionInvocation


@dataclass(frozen=True)
class WorkflowTransitionSpec:
    to_state: str
    condition: Callable[[Dict[str, Any]], bool]
    condition_spec: Mapping[str, Any] | None = None
    description: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class WorkflowStateSpec:
    state_id: str
    actions: Sequence[WorkflowActionInvocation] = ()
    transitions: Sequence[WorkflowTransitionSpec] = ()
    terminal: bool = False
    # Optional metadata from Vontology graph (preconditions, effects, etc.).
    # Not consumed by the engine itself but available for introspection and
    # future constraint-checking.  See JVNAUTOSCI-922 Phase 3.2.
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_id: str
    initial_state: str
    states: Mapping[str, WorkflowStateSpec]
    termination_states: Sequence[str] = ()
    purpose: str | None = None
    # Workflow-level metadata resolved from Vontology representation.
    # This complements per-state metadata and enables reusable runtime policy
    # gates (for example background launch cadence) without bespoke code paths.
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowResult:
    data: Dict[str, Any]
    completed: bool
    final_state: str
    error: str | None = None
    result_envelope: Dict[str, Any] | None = None


def state_has_on_failure_transition(state_spec: WorkflowStateSpec) -> bool:
    """Return True when a state declares an explicit `on_failure` route."""
    return state_has_transition_reason(state_spec, "on_failure")


def state_has_on_unknown_transition(state_spec: WorkflowStateSpec) -> bool:
    """Return True when a state declares an explicit `on_unknown` route."""
    return state_has_transition_reason(state_spec, "on_unknown")


def state_has_on_break_transition(state_spec: WorkflowStateSpec) -> bool:
    """Return True when a state declares an explicit `on_break` route."""
    return state_has_transition_reason(state_spec, "on_break")


def state_has_on_continue_transition(state_spec: WorkflowStateSpec) -> bool:
    """Return True when a state declares an explicit `on_continue` route."""
    return state_has_transition_reason(state_spec, "on_continue")


def state_has_on_approval_required_transition(state_spec: WorkflowStateSpec) -> bool:
    """Return True when a state declares an explicit approval-blocked route."""

    return state_has_transition_reason(state_spec, "on_approval_required")


def state_has_transition_reason(state_spec: WorkflowStateSpec, reason: str) -> bool:
    """Return True when a state declares a transition with `reason`."""
    desired_reason = str(reason or "").strip().lower()
    for transition in state_spec.transitions:
        transition_reason = transition.reason
        if (
            isinstance(transition_reason, str)
            and transition_reason.strip().lower() == desired_reason
        ):
            return True
    return False


def _normalise_retry_policy_spec(value: Any) -> Dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None

    schema_version = str(value.get("schema_version") or "").strip()
    if schema_version and schema_version != WORKFLOW_RETRY_POLICY_SCHEMA_VERSION:
        raise ValueError("workflow_retry_policy_invalid:schema_version_unsupported")

    try:
        max_attempts = int(value.get("max_attempts") or value.get("attempts") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow_retry_policy_invalid:max_attempts_not_int") from exc
    if max_attempts < 1:
        raise ValueError("workflow_retry_policy_invalid:max_attempts_below_minimum")

    backoff_policy = str(
        value.get("backoff_policy") or value.get("backoff") or "none"
    ).strip().lower()
    if backoff_policy not in {"none", "fixed", "exponential"}:
        raise ValueError("workflow_retry_policy_invalid:backoff_policy_invalid")

    try:
        initial_delay_ms = int(
            value.get("initial_delay_ms") or value.get("delay_ms") or 0
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow_retry_policy_invalid:delay_ms_not_int") from exc
    if initial_delay_ms < 0:
        raise ValueError("workflow_retry_policy_invalid:delay_ms_negative")

    try:
        max_delay_ms = int(value.get("max_delay_ms") or initial_delay_ms or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow_retry_policy_invalid:max_delay_ms_not_int") from exc
    if max_delay_ms < 0:
        raise ValueError("workflow_retry_policy_invalid:max_delay_ms_negative")

    outcomes = value.get("retry_on_outcomes") or value.get("outcomes")
    if outcomes is None:
        outcomes = ["failure"]
    if not isinstance(outcomes, Sequence) or isinstance(
        outcomes, (str, bytes, bytearray)
    ):
        raise ValueError("workflow_retry_policy_invalid:retry_on_outcomes_not_list")
    normalised_outcomes = sorted(
        {
            str(item or "").strip().lower()
            for item in outcomes
            if str(item or "").strip().lower()
            in {
                WORKFLOW_ACTION_OUTCOME_FAILURE,
                WORKFLOW_ACTION_OUTCOME_UNKNOWN,
            }
        }
    )
    if not normalised_outcomes:
        raise ValueError("workflow_retry_policy_invalid:retry_on_outcomes_empty")

    return {
        "schema_version": WORKFLOW_RETRY_POLICY_SCHEMA_VERSION,
        "max_attempts": max_attempts,
        "backoff_policy": backoff_policy,
        "initial_delay_ms": initial_delay_ms,
        "max_delay_ms": max_delay_ms,
        "retry_on_outcomes": normalised_outcomes,
    }


def _normalise_approval_gate_spec(value: Any) -> Dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None

    schema_version = str(value.get("schema_version") or "").strip()
    if schema_version and schema_version != WORKFLOW_APPROVAL_GATE_SCHEMA_VERSION:
        raise ValueError("workflow_approval_gate_invalid:schema_version_unsupported")

    approval_context_key = str(
        value.get("approval_context_key")
        or value.get("context_key")
        or value.get("key")
        or ""
    ).strip()
    if not approval_context_key:
        raise ValueError("workflow_approval_gate_invalid:approval_context_key_missing")

    expected = _coerce_bool_like(
        value.get("expected", True),
        field_name="approval_gate_expected",
    )
    return {
        "schema_version": WORKFLOW_APPROVAL_GATE_SCHEMA_VERSION,
        "approval_context_key": approval_context_key,
        "expected": expected,
        "approval_label": str(value.get("approval_label") or "").strip() or None,
    }


def _normalise_idempotency_policy_spec(value: Any) -> Dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None

    schema_version = str(value.get("schema_version") or "").strip()
    if schema_version and schema_version != WORKFLOW_IDEMPOTENCY_POLICY_SCHEMA_VERSION:
        raise ValueError(
            "workflow_idempotency_policy_invalid:schema_version_unsupported"
        )

    raw_paths = value.get("key_paths") or value.get("context_keys") or value.get("paths")
    if not isinstance(raw_paths, Sequence) or isinstance(
        raw_paths, (str, bytes, bytearray)
    ):
        raise ValueError("workflow_idempotency_policy_invalid:key_paths_not_list")
    key_paths = [
        str(item or "").strip()
        for item in raw_paths
        if isinstance(item, str) and str(item or "").strip()
    ]
    if not key_paths:
        raise ValueError("workflow_idempotency_policy_invalid:key_paths_empty")

    return {
        "schema_version": WORKFLOW_IDEMPOTENCY_POLICY_SCHEMA_VERSION,
        "key_paths": list(dict.fromkeys(key_paths)),
        "namespace": str(value.get("namespace") or "").strip() or None,
    }


def _compute_retry_delay_ms(
    *,
    retry_policy: Mapping[str, Any],
    attempt_number: int,
) -> int:
    initial_delay_ms = int(retry_policy.get("initial_delay_ms") or 0)
    max_delay_ms = int(retry_policy.get("max_delay_ms") or initial_delay_ms or 0)
    backoff_policy = str(retry_policy.get("backoff_policy") or "none").strip().lower()
    if backoff_policy == "none" or initial_delay_ms <= 0:
        return 0
    if backoff_policy == "fixed":
        return min(initial_delay_ms, max_delay_ms)
    multiplier = max(0, attempt_number - 1)
    return min(initial_delay_ms * (2**multiplier), max_delay_ms)


def _build_idempotency_key(
    *,
    workflow_id: str,
    state_id: str,
    action_id: str,
    context: Mapping[str, Any],
    idempotency_policy: Mapping[str, Any],
) -> str:
    parts: list[str] = [workflow_id, state_id, action_id]
    namespace = str(idempotency_policy.get("namespace") or "").strip()
    if namespace:
        parts.append(namespace)
    for path in idempotency_policy.get("key_paths", []):
        found, value = resolve_context_path(context=context, path=str(path))
        if not found:
            raise ValueError(f"workflow_idempotency_key_missing:{path}")
        parts.append(f"{path}={repr(value)}")
    return "::".join(parts)


class WorkflowExecutor:
    """Execute a workflow definition using a registry of declarative actions."""

    def __init__(self, *, registry: ActionRegistry, max_transitions: int = 12) -> None:
        self._registry = registry
        self._max_transitions = max(3, int(max_transitions))

    def run(
        self,
        definition: WorkflowDefinition,
        *,
        environment: WorkflowEnvironment,
        data: Dict[str, Any] | None = None,
        trace: WorkflowExecutionTrace | None = None,
    ) -> WorkflowResult:
        context: Dict[str, Any] = data or {}
        clear_control_signal_context(context)

        # Initialise declared workflow variables (defaults only, not overriding
        # values already present from caller-supplied data).
        variable_declarations = (definition.metadata or {}).get(
            "variable_declarations"
        )
        if isinstance(variable_declarations, (list, tuple)):
            for var_decl in variable_declarations:
                if not isinstance(var_decl, Mapping):
                    continue
                var_name = str(var_decl.get("name") or "").strip()
                if var_name and var_name not in context:
                    context[var_name] = var_decl.get("default_value")

        current_state = definition.initial_state
        transitions = 0
        termination_states = set(definition.termination_states) | {
            state for state, spec in definition.states.items() if spec.terminal
        }
        validation_mode = get_metadata_validation_mode()
        enforce_metadata_failures = metadata_validation_failures_are_enforced(
            validation_mode
        )

        def _build_result(
            *,
            completed: bool,
            final_state: str,
            error: str | None = None,
        ) -> WorkflowResult:
            result_envelope = build_workflow_result_envelope(
                workflow_id=definition.workflow_id,
                completed=completed,
                final_state=final_state,
                error=error,
                control_signal=get_last_control_signal(context),
                return_payload=context.get(WORKFLOW_RETURN_PAYLOAD_KEY),
                context=context,
                transition_count=transitions,
            )
            set_workflow_result_envelope(context=context, envelope=result_envelope)
            return WorkflowResult(
                data=context,
                completed=completed,
                final_state=final_state,
                error=error,
                result_envelope=result_envelope,
            )

        def _complete_with_gate(final_state: str) -> WorkflowResult:
            gate_ok, gate_result = evaluate_workflow_completion_gate(
                context=context,
                workflow_id=definition.workflow_id,
                definition_metadata=definition.metadata,
                final_state=final_state,
            )
            if not gate_ok:
                blocking_reasons = []
                if isinstance(gate_result, Mapping):
                    blocking_reasons = [
                        str(item).strip()
                        for item in gate_result.get("blocking_reason_codes") or []
                        if str(item).strip()
                    ]
                error = "workflow_completion_gate_unmet"
                if blocking_reasons:
                    error = f"{error}:{'|'.join(blocking_reasons)}"
                if trace is not None:
                    trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=final_state,
                    error=error,
                )
            if trace is not None:
                trace.finish_completed()
            return _build_result(
                completed=True,
                final_state=final_state,
            )

        while transitions < self._max_transitions:
            transitions += 1
            state_spec = definition.states.get(current_state)
            if state_spec is None:
                error = f"unknown_state:{current_state}"
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                )

            mark_workflow_plan_state_entry(
                context=context,
                workflow_id=definition.workflow_id,
                state_id=current_state,
                definition_metadata=definition.metadata,
                state_metadata=state_spec.metadata,
            )

            if trace is not None:
                trace.record_state_transition(
                    current_state,
                    current_state,
                    verdict={"status": "enter"},
                )

            if validation_mode == METADATA_VALIDATION_MODE_OFF:
                off_probe = validate_state_metadata_pre_action(
                    state_id=current_state,
                    metadata=state_spec.metadata,
                    context=context,
                )
                if off_probe.applied:
                    pre_validation = skipped_metadata_validation(
                        state_id=current_state,
                        phase="pre_action",
                        reason="disabled_by_rollout_mode",
                        mode=validation_mode,
                    )
                else:
                    pre_validation = apply_metadata_validation_mode(
                        result=off_probe,
                        mode=validation_mode,
                    )
            else:
                pre_validation = apply_metadata_validation_mode(
                    result=validate_state_metadata_pre_action(
                        state_id=current_state,
                        metadata=state_spec.metadata,
                        context=context,
                    ),
                    mode=validation_mode,
                )
            append_metadata_validation_event(context=context, result=pre_validation)
            if trace is not None and pre_validation.applied:
                trace.record_state_transition(
                    current_state,
                    current_state,
                    verdict=pre_validation.to_trace_verdict(),
                )
            if not pre_validation.ok and enforce_metadata_failures:
                error = format_metadata_validation_error(pre_validation)
                if trace is not None:
                    trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                )

            state_has_failure_route = state_has_on_failure_transition(state_spec)
            state_has_unknown_route = state_has_on_unknown_transition(state_spec)
            state_has_approval_route = state_has_on_approval_required_transition(
                state_spec
            )
            state_has_break_route = state_has_on_break_transition(state_spec)
            state_has_continue_route = state_has_on_continue_transition(state_spec)
            try:
                retry_policy = _normalise_retry_policy_spec(
                    state_spec.metadata.get("retry_policy")
                )
                approval_gate = _normalise_approval_gate_spec(
                    state_spec.metadata.get("approval_gate")
                )
                idempotency_policy = _normalise_idempotency_policy_spec(
                    state_spec.metadata.get("idempotency_policy")
                )
            except ValueError as exc:
                if trace is not None:
                    trace.finish_failed(str(exc))
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=str(exc),
                )

            if retry_policy is not None and len(state_spec.actions) > 1:
                error = "workflow_retry_policy_invalid:multi_action_state_unsupported"
                if trace is not None:
                    trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                )
            if idempotency_policy is not None and len(state_spec.actions) > 1:
                error = (
                    "workflow_idempotency_policy_invalid:"
                    "multi_action_state_unsupported"
                )
                if trace is not None:
                    trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                )

            context_before_actions = dict(context)
            approval_blocked = False
            if approval_gate is not None:
                context["approval_required"] = False
                context["approval_state"] = None

            state_attempt = 0
            while True:
                state_attempt += 1
                retry_requested = False
                for action in state_spec.actions:
                    context_before_action = dict(context)
                    action_target_id = action.target_id
                    resolved_inputs = resolve_action_inputs_from_context(
                        action_inputs=action.inputs,
                        context=context,
                    )
                    result: WorkflowActionResult
                    action_outcome: str

                    idempotency_key: str | None = None
                    raw_records: dict[str, Any] | None = None
                    existing_record: Mapping[str, Any] | None = None
                    if idempotency_policy is not None:
                        try:
                            idempotency_key = _build_idempotency_key(
                                workflow_id=definition.workflow_id,
                                state_id=current_state,
                                action_id=action_target_id,
                                context=context,
                                idempotency_policy=idempotency_policy,
                            )
                        except ValueError as exc:
                            if trace is not None:
                                trace.finish_failed(str(exc))
                            return _build_result(
                                completed=False,
                                final_state=current_state,
                                error=str(exc),
                            )
                        raw_records = context.get(WORKFLOW_IDEMPOTENCY_RECORDS_KEY)
                        if not isinstance(raw_records, dict):
                            raw_records = {}
                            context[WORKFLOW_IDEMPOTENCY_RECORDS_KEY] = raw_records
                        existing_record_raw = raw_records.get(idempotency_key)
                        if isinstance(existing_record_raw, Mapping):
                            existing_record = existing_record_raw
                    if existing_record is not None:
                        action_output_snapshot: dict[str, Any] = {}
                        cached_outputs_raw = existing_record.get("outputs")
                        cached_outputs = (
                            {
                                str(key): value
                                for key, value in cached_outputs_raw.items()
                                if isinstance(key, str) and key
                            }
                            if isinstance(cached_outputs_raw, Mapping)
                            else {}
                        )
                        result = WorkflowActionResult(
                            status="success",
                            outputs=cached_outputs,
                            call_id=str(existing_record.get("call_id") or "") or None,
                            duration_ms=0.0,
                        )
                        action_outcome = _apply_action_result_context(
                            context=context,
                            action_id=action_target_id,
                            result=result,
                        )
                        if trace is not None:
                            trace.record_action(
                                action_id=action_target_id,
                                inputs=resolved_inputs,
                                outputs=result.outputs,
                                status=result.status,
                                error=result.error,
                                call_id=result.call_id,
                                duration_ms=result.duration_ms,
                            )
                        if cached_outputs:
                            cached_output_snapshot = snapshot_workflow_mapping(
                                cached_outputs
                            )
                            action_output_snapshot = snapshot_workflow_mapping(
                                cached_output_snapshot
                            )
                            context.update(cached_output_snapshot)
                            apply_tool_output_context_mappings(
                                context=context,
                                metadata=state_spec.metadata,
                                action_outputs=cached_output_snapshot,
                                state_id=current_state,
                                action_id=action_target_id,
                            )
                        idempotency_event = {
                            "status": "idempotent_reuse",
                            "workflow_id": definition.workflow_id,
                            "state_id": current_state,
                            "action_id": action_target_id,
                            "idempotency_key": idempotency_key,
                        }
                        _append_context_event(
                            context=context,
                            key=WORKFLOW_IDEMPOTENCY_EVENTS_KEY,
                            last_key=LAST_WORKFLOW_IDEMPOTENCY_EVENT_KEY,
                            event=idempotency_event,
                        )
                        append_runtime_event(
                            context=context,
                            event=idempotency_event,
                        )
                        if trace is not None:
                            trace.record_state_transition(
                                current_state,
                                current_state,
                                verdict=idempotency_event,
                            )
                    else:
                        if approval_gate is not None:
                            prior_approval_state = str(
                                context.get("approval_state") or ""
                            ).strip()
                            found, approval_value = resolve_context_path(
                                context=context,
                                path=str(
                                    approval_gate.get("approval_context_key") or ""
                                ),
                            )
                            approved = found and (
                                bool(approval_value)
                                is bool(approval_gate.get("expected", True))
                            )
                            approval_event = {
                                "type": "mutation_guardrail",
                                "status": "approval_gate_checked",
                                "guardrail_surface": "workflow_approval_gate",
                                "decision": (
                                    "allowed" if approved else "approval_required"
                                ),
                                "stage": current_state,
                                "workflow_id": definition.workflow_id,
                                "workflow_step_id": current_state,
                                "state_id": current_state,
                                "action_id": action_target_id,
                                "approval_context_key": approval_gate.get(
                                    "approval_context_key"
                                ),
                                "approval_label": approval_gate.get("approval_label"),
                                "approval_state": "approved"
                                if approved
                                else "blocked",
                                "approval_required": not approved,
                                "approval_transition": (
                                    "entered_approval_required"
                                    if (not approved and prior_approval_state != "blocked")
                                    else (
                                        "exited_approval_required"
                                        if (approved and prior_approval_state == "blocked")
                                        else "approval_state_unchanged"
                                    )
                                ),
                            }
                            _append_context_event(
                                context=context,
                                key=WORKFLOW_APPROVAL_GATE_EVENTS_KEY,
                                last_key=LAST_WORKFLOW_APPROVAL_GATE_KEY,
                                event=approval_event,
                            )
                            append_runtime_event(
                                context=context,
                                event=approval_event,
                            )
                            try:
                                from .workflow_baseline_telemetry import (
                                    record_mutation_guardrail_event,
                                )

                                record_mutation_guardrail_event(approval_event)
                            except Exception:
                                pass
                            if trace is not None:
                                trace.record_state_transition(
                                    current_state,
                                    current_state,
                                    verdict=approval_event,
                                )
                            if not approved:
                                context["approval_required"] = True
                                context["approval_state"] = "blocked"
                                approval_blocked = True
                                break
                            context["approval_required"] = False
                            context["approval_state"] = "approved"

                        result = execute_workflow_step_invocation(
                            registry=self._registry,
                            workflow_id=definition.workflow_id,
                            workflow_state_id=current_state,
                            workflow_state_metadata=state_spec.metadata,
                            action=action,
                            resolved_inputs=resolved_inputs,
                            context=context,
                            env=environment,
                            trace=trace,
                        )
                        if trace is not None:
                            trace.record_action(
                                action_id=action_target_id,
                                inputs=resolved_inputs,
                                outputs=result.outputs,
                                status=result.status,
                                error=result.error,
                                call_id=result.call_id,
                                duration_ms=result.duration_ms,
                            )
                        action_outcome = normalise_action_outcome(result.status)
                        action_output_snapshot: dict[str, Any] = {}
                        if isinstance(result.outputs, Mapping):
                            action_output_snapshot = snapshot_workflow_mapping(
                                result.outputs
                            )
                        if action_outcome != WORKFLOW_ACTION_OUTCOME_FAILURE:
                            context.update(action_output_snapshot)
                            apply_tool_output_context_mappings(
                                context=context,
                                metadata=state_spec.metadata,
                                action_outputs=action_output_snapshot,
                                state_id=current_state,
                                action_id=action_target_id,
                            )
                            if (
                                idempotency_policy is not None
                                and raw_records is not None
                                and idempotency_key is not None
                                and action_outcome
                                == WORKFLOW_ACTION_OUTCOME_SUCCESS
                            ):
                                raw_records[idempotency_key] = {
                                    "outputs": snapshot_workflow_mapping(
                                        action_output_snapshot
                                    ),
                                    "call_id": result.call_id,
                                }
                                idempotency_event = {
                                    "status": "idempotent_recorded",
                                    "workflow_id": definition.workflow_id,
                                    "state_id": current_state,
                                    "action_id": action_target_id,
                                    "idempotency_key": idempotency_key,
                                }
                                _append_context_event(
                                    context=context,
                                    key=WORKFLOW_IDEMPOTENCY_EVENTS_KEY,
                                    last_key=LAST_WORKFLOW_IDEMPOTENCY_EVENT_KEY,
                                    event=idempotency_event,
                                )
                                append_runtime_event(
                                    context=context,
                                    event=idempotency_event,
                                )

                    step_envelope = build_step_result_envelope(
                        workflow_id=definition.workflow_id,
                        state_id=current_state,
                        action_id=action_target_id,
                        action_status=result.status,
                        action_outcome=action_outcome,
                        action_error=result.error,
                        action_outputs=action_output_snapshot,
                        control_signal=get_last_control_signal(context),
                        control_signal_scope=get_last_control_signal_scope(context),
                        duration_ms=result.duration_ms,
                        context_before=context_before_action,
                        context_after=context,
                    )
                    step_envelope["state_attempt"] = state_attempt
                    append_step_result_envelope(context=context, envelope=step_envelope)

                    if environment.step_callback:
                        try:
                            environment.step_callback(step_envelope)
                        except Exception:
                            logger.warning(
                                "[workflow_engine] Step callback failed for %s",
                                definition.workflow_id,
                                exc_info=True,
                            )

                    if trace is not None and step_envelope["control_signal"] != "none":
                        event = {
                            "status": "control_signal",
                            "workflow_id": definition.workflow_id,
                            "state_id": current_state,
                            "action_id": action_target_id,
                            "control_signal": step_envelope["control_signal"],
                            "control_signal_scope": step_envelope.get(
                                "control_signal_scope"
                            ),
                        }
                        append_runtime_event(context=context, event=event)
                        trace.record_state_transition(
                            current_state,
                            current_state,
                            verdict=event,
                        )

                    if (
                        retry_policy is not None
                        and action_outcome
                        in set(retry_policy.get("retry_on_outcomes") or [])
                        and state_attempt < int(retry_policy.get("max_attempts") or 1)
                    ):
                        delay_ms = _compute_retry_delay_ms(
                            retry_policy=retry_policy,
                            attempt_number=state_attempt,
                        )
                        retry_event = {
                            "status": "retry_scheduled",
                            "workflow_id": definition.workflow_id,
                            "state_id": current_state,
                            "action_id": action_target_id,
                            "attempt_number": state_attempt,
                            "next_attempt_number": state_attempt + 1,
                            "delay_ms": delay_ms,
                            "action_outcome": action_outcome,
                        }
                        _append_context_event(
                            context=context,
                            key=WORKFLOW_RETRY_EVENTS_KEY,
                            last_key=LAST_WORKFLOW_RETRY_EVENT_KEY,
                            event=retry_event,
                        )
                        append_runtime_event(context=context, event=retry_event)
                        if trace is not None:
                            trace.record_state_transition(
                                current_state,
                                current_state,
                                verdict=retry_event,
                            )
                        if delay_ms > 0:
                            time.sleep(delay_ms / 1000.0)
                        retry_requested = True
                        break

                    if action_outcome == WORKFLOW_ACTION_OUTCOME_FAILURE:
                        if state_has_failure_route:
                            # Safety envelope (JVNAUTOSCI-1087): preserve the failure
                            # in context and let transition rules decide recovery.
                            break
                        if trace is not None:
                            trace.finish_failed(result.error or "action_failed")
                        return _build_result(
                            completed=False,
                            final_state=current_state,
                            error=result.error or "action_failed",
                        )
                    if action_outcome == WORKFLOW_ACTION_OUTCOME_UNKNOWN:
                        if state_has_unknown_route:
                            # Explicit unknown-routing is required to avoid silent
                            # progression on ambiguous action outcomes.
                            break
                        unknown_error = result.error or "action_unknown"
                        if trace is not None:
                            trace.finish_failed(unknown_error)
                        return _build_result(
                            completed=False,
                            final_state=current_state,
                            error=unknown_error,
                        )

                    control_signal = get_last_control_signal(context)
                    if control_signal in {
                        WORKFLOW_CONTROL_SIGNAL_BREAK,
                        WORKFLOW_CONTROL_SIGNAL_CONTINUE,
                        WORKFLOW_CONTROL_SIGNAL_RETURN,
                    }:
                        break

                if approval_blocked or not retry_requested:
                    break

            if current_state in termination_states:
                materialise_terminal_effect_context(
                    context=context,
                    state_spec=state_spec,
                    state_id=current_state,
                )

            control_signal = get_last_control_signal(context)
            if control_signal == WORKFLOW_CONTROL_SIGNAL_BREAK and not state_has_break_route:
                if trace is not None:
                    trace.finish_failed("workflow_break_outside_loop_scope")
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error="workflow_break_outside_loop_scope",
                )
            if (
                control_signal == WORKFLOW_CONTROL_SIGNAL_CONTINUE
                and not state_has_continue_route
            ):
                if trace is not None:
                    trace.finish_failed("workflow_continue_outside_loop_scope")
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error="workflow_continue_outside_loop_scope",
                )

            if approval_blocked and not state_has_approval_route:
                if trace is not None:
                    trace.finish_failed("approval_required")
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error="approval_required",
                )

            if approval_blocked and state_has_approval_route:
                post_validation = skipped_metadata_validation(
                    state_id=current_state,
                    phase="post_action",
                    reason="approval_required_with_on_approval_required_route",
                    mode=validation_mode,
                )
            elif state_has_unknown_route and bool(context.get("last_action_unknown")):
                post_validation = skipped_metadata_validation(
                    state_id=current_state,
                    phase="post_action",
                    reason="action_unknown_with_on_unknown_route",
                    mode=validation_mode,
                )
            elif state_has_failure_route and bool(context.get("last_action_failed")):
                post_validation = skipped_metadata_validation(
                    state_id=current_state,
                    phase="post_action",
                    reason="action_failed_with_on_failure_route",
                    mode=validation_mode,
                )
            elif validation_mode == METADATA_VALIDATION_MODE_OFF:
                off_probe = validate_state_metadata_post_action(
                    state_id=current_state,
                    metadata=state_spec.metadata,
                    context_before=context_before_actions,
                    context_after=context,
                )
                if off_probe.applied:
                    post_validation = skipped_metadata_validation(
                        state_id=current_state,
                        phase="post_action",
                        reason="disabled_by_rollout_mode",
                        mode=validation_mode,
                    )
                else:
                    post_validation = apply_metadata_validation_mode(
                        result=off_probe,
                        mode=validation_mode,
                    )
            else:
                post_validation = apply_metadata_validation_mode(
                    result=validate_state_metadata_post_action(
                        state_id=current_state,
                        metadata=state_spec.metadata,
                        context_before=context_before_actions,
                        context_after=context,
                    ),
                    mode=validation_mode,
                )

            append_metadata_validation_event(context=context, result=post_validation)
            if trace is not None and post_validation.applied:
                trace.record_state_transition(
                    current_state,
                    current_state,
                    verdict=post_validation.to_trace_verdict(),
                )
            if not post_validation.ok and enforce_metadata_failures:
                error = format_metadata_validation_error(post_validation)
                if trace is not None:
                    trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                )

            apply_workflow_step_checkpoint(
                context=context,
                workflow_id=definition.workflow_id,
                state_id=current_state,
                step_index=transitions,
                definition_metadata=definition.metadata,
                state_metadata=state_spec.metadata,
                blocked=approval_blocked,
            )

            if control_signal == WORKFLOW_CONTROL_SIGNAL_RETURN:
                return _complete_with_gate(current_state)

            if current_state in termination_states:
                return _complete_with_gate(current_state)

            next_state = None
            transition_reason = None
            for transition in state_spec.transitions:
                try:
                    condition_result = bool(transition.condition(context))
                    if (
                        not condition_result
                        and isinstance(transition.condition_spec, Mapping)
                    ):
                        condition_result = evaluate_transition_condition_spec(
                            context=context,
                            condition_spec=transition.condition_spec,
                        )
                    if condition_result:
                        next_state = transition.to_state
                        transition_reason = transition.reason
                        break
                except Exception:
                    continue

            if next_state is None:
                error = "approval_required" if approval_blocked else "no_transition"
                if trace is not None:
                    trace.finish_failed(error)
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error=error,
                )

            if trace is not None:
                trace.record_state_transition(
                    current_state,
                    next_state,
                    reason=transition_reason,
                )
            current_state = next_state
            clear_control_signal_context(context)

        if trace is not None:
            trace.finish_failed("transition_limit")
        return _build_result(
            completed=False,
            final_state=current_state,
            error="transition_limit",
        )
