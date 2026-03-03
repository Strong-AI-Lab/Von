"""Lightweight workflow executor for orchestrator-managed workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .action_registry import (
    ActionRegistry,
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
    set_workflow_result_envelope,
)
from .trace_model import WorkflowExecutionTrace

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
    resolved: Dict[str, Any] = {}
    for input_key, input_value in action_inputs.items():
        symbol = _extract_context_binding_symbol(input_value)
        if not symbol:
            resolved[input_key] = input_value
            continue
        found, concrete_value = _resolve_context_symbol(context=context, symbol=symbol)
        resolved[input_key] = concrete_value if found else None
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
        raw_key = condition_spec.get("key")
        key = str(raw_key or "").strip()
        if not key:
            raise ValueError("workflow_condition_invalid:context_flag_key_missing")
        expected = _coerce_bool_like(
            condition_spec.get("expected", True),
            field_name="context_flag_expected",
        )
        return {"kind": "context_flag", "key": key, "expected": expected}

    if kind == "context_value_equals":
        raw_key = condition_spec.get("key")
        key = str(raw_key or "").strip()
        if not key:
            raise ValueError("workflow_condition_invalid:context_value_key_missing")
        if "value" not in condition_spec:
            raise ValueError("workflow_condition_invalid:context_value_missing")
        return {
            "kind": "context_value_equals",
            "key": key,
            "value": condition_spec.get("value"),
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
        return bool(context.get(key)) is expected
    if kind == "context_value_equals":
        key = str(condition_spec.get("key") or "")
        return context.get(key) == condition_spec.get("value")
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
    action_id: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    description: str | None = None


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
            state_has_break_route = state_has_on_break_transition(state_spec)
            state_has_continue_route = state_has_on_continue_transition(state_spec)
            context_before_actions = dict(context)
            for action in state_spec.actions:
                context_before_action = dict(context)
                resolved_inputs = resolve_action_inputs_from_context(
                    action_inputs=action.inputs,
                    context=context,
                )
                result = self._registry.execute(
                    action.action_id,
                    inputs=resolved_inputs,
                    context=context,
                    env=environment,
                    trace=trace,
                )
                if trace is not None:
                    trace.record_action(
                        action_id=action.action_id,
                        inputs=resolved_inputs,
                        outputs=result.outputs,
                        status=result.status,
                        error=result.error,
                        call_id=result.call_id,
                        duration_ms=result.duration_ms,
                    )
                action_outcome = normalise_action_outcome(result.status)
                if (
                    action_outcome != WORKFLOW_ACTION_OUTCOME_FAILURE
                    and isinstance(result.outputs, Mapping)
                ):
                    context.update(result.outputs)
                    apply_tool_output_context_mappings(
                        context=context,
                        metadata=state_spec.metadata,
                        action_outputs=result.outputs,
                        state_id=current_state,
                        action_id=action.action_id,
                    )
                step_envelope = build_step_result_envelope(
                    workflow_id=definition.workflow_id,
                    state_id=current_state,
                    action_id=action.action_id,
                    action_status=result.status,
                    action_outcome=action_outcome,
                    action_error=result.error,
                    action_outputs=result.outputs if isinstance(result.outputs, Mapping) else {},
                    control_signal=get_last_control_signal(context),
                    control_signal_scope=get_last_control_signal_scope(context),
                    duration_ms=result.duration_ms,
                    context_before=context_before_action,
                    context_after=context,
                )
                append_step_result_envelope(context=context, envelope=step_envelope)
                if trace is not None and step_envelope["control_signal"] != "none":
                    event = {
                        "status": "control_signal",
                        "workflow_id": definition.workflow_id,
                        "state_id": current_state,
                        "action_id": action.action_id,
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

            if state_has_unknown_route and bool(context.get("last_action_unknown")):
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

            if control_signal == WORKFLOW_CONTROL_SIGNAL_RETURN:
                if trace is not None:
                    trace.finish_completed()
                return _build_result(
                    completed=True,
                    final_state=current_state,
                )

            if current_state in termination_states:
                if trace is not None:
                    trace.finish_completed()
                return _build_result(
                    completed=True,
                    final_state=current_state,
                )

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
                if trace is not None:
                    trace.finish_failed("no_transition")
                return _build_result(
                    completed=False,
                    final_state=current_state,
                    error="no_transition",
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
