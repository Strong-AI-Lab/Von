"""Lightweight workflow executor for orchestrator-managed workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .action_registry import ActionRegistry, WorkflowActionResult, WorkflowEnvironment
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


@dataclass(frozen=True)
class WorkflowActionInvocation:
    action_id: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    description: str | None = None


@dataclass(frozen=True)
class WorkflowTransitionSpec:
    to_state: str
    condition: Callable[[Dict[str, Any]], bool]
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


@dataclass
class WorkflowResult:
    data: Dict[str, Any]
    completed: bool
    final_state: str
    error: str | None = None


def state_has_on_failure_transition(state_spec: WorkflowStateSpec) -> bool:
    """Return True when a state declares an explicit `on_failure` route."""
    for transition in state_spec.transitions:
        reason = transition.reason
        if isinstance(reason, str) and reason.strip().lower() == "on_failure":
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
        current_state = definition.initial_state
        transitions = 0
        termination_states = set(definition.termination_states) | {
            state for state, spec in definition.states.items() if spec.terminal
        }
        validation_mode = get_metadata_validation_mode()
        enforce_metadata_failures = metadata_validation_failures_are_enforced(
            validation_mode
        )

        while transitions < self._max_transitions:
            transitions += 1
            state_spec = definition.states.get(current_state)
            if state_spec is None:
                error = f"unknown_state:{current_state}"
                return WorkflowResult(
                    data=context,
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
                return WorkflowResult(
                    data=context,
                    completed=False,
                    final_state=current_state,
                    error=error,
                )

            state_has_failure_route = state_has_on_failure_transition(state_spec)
            context_before_actions = dict(context)
            for action in state_spec.actions:
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
                if not result.ok:
                    if state_has_failure_route:
                        # Safety envelope (JVNAUTOSCI-1087): preserve the failure
                        # in context and let transition rules decide recovery.
                        break
                    if trace is not None:
                        trace.finish_failed(result.error or "action_failed")
                    return WorkflowResult(
                        data=context,
                        completed=False,
                        final_state=current_state,
                        error=result.error or "action_failed",
                    )
                context.update(result.outputs)

            if state_has_failure_route and bool(context.get("last_action_failed")):
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
                return WorkflowResult(
                    data=context,
                    completed=False,
                    final_state=current_state,
                    error=error,
                )

            if current_state in termination_states:
                return WorkflowResult(
                    data=context, completed=True, final_state=current_state
                )

            next_state = None
            transition_reason = None
            for transition in state_spec.transitions:
                try:
                    if transition.condition(context):
                        next_state = transition.to_state
                        transition_reason = transition.reason
                        break
                except Exception:
                    continue

            if next_state is None:
                return WorkflowResult(
                    data=context,
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

        return WorkflowResult(
            data=context,
            completed=False,
            final_state=current_state,
            error="transition_limit",
        )
