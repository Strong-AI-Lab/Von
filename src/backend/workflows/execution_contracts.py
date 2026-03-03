"""Shared workflow execution contracts for control signals and result envelopes.

JVNAUTOSCI-1311 introduced first-class control-flow primitives and canonical
step/workflow return envelopes. Keep these helpers central so conversation-turn
and durable executors remain behaviourally aligned.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, MutableMapping


WORKFLOW_CONTROL_SIGNAL_NONE = "none"
WORKFLOW_CONTROL_SIGNAL_BREAK = "break"
WORKFLOW_CONTROL_SIGNAL_CONTINUE = "continue"
WORKFLOW_CONTROL_SIGNAL_RETURN = "return"
WORKFLOW_CONTROL_SIGNAL_ERROR = "error"

WORKFLOW_CONTROL_SIGNAL_ALLOWED: tuple[str, ...] = (
    WORKFLOW_CONTROL_SIGNAL_NONE,
    WORKFLOW_CONTROL_SIGNAL_BREAK,
    WORKFLOW_CONTROL_SIGNAL_CONTINUE,
    WORKFLOW_CONTROL_SIGNAL_RETURN,
    WORKFLOW_CONTROL_SIGNAL_ERROR,
)

WORKFLOW_CONTROL_ACTION_BREAK_ID = "workflow_control.break"
WORKFLOW_CONTROL_ACTION_CONTINUE_ID = "workflow_control.continue"
WORKFLOW_CONTROL_ACTION_FORK_ID = "workflow_control.fork"
WORKFLOW_CONTROL_ACTION_JOIN_ID = "workflow_control.join"

WORKFLOW_CONTROL_BREAK_ACTION_IDS: tuple[str, ...] = (
    WORKFLOW_CONTROL_ACTION_BREAK_ID,
)
WORKFLOW_CONTROL_CONTINUE_ACTION_IDS: tuple[str, ...] = (
    WORKFLOW_CONTROL_ACTION_CONTINUE_ID,
)
WORKFLOW_CONTROL_FORK_ACTION_IDS: tuple[str, ...] = (
    WORKFLOW_CONTROL_ACTION_FORK_ID,
)
WORKFLOW_CONTROL_JOIN_ACTION_IDS: tuple[str, ...] = (
    WORKFLOW_CONTROL_ACTION_JOIN_ID,
)

WORKFLOW_FORK_FAILURE_POLICY_FAIL_FAST = "fail_fast"
WORKFLOW_FORK_FAILURE_POLICY_COLLECT_ERRORS = "collect_errors"
WORKFLOW_FORK_FAILURE_POLICY_ALLOW_PARTIAL_SUCCESS = "allow_partial_success"
WORKFLOW_FORK_ALLOWED_FAILURE_POLICIES: tuple[str, ...] = (
    WORKFLOW_FORK_FAILURE_POLICY_FAIL_FAST,
    WORKFLOW_FORK_FAILURE_POLICY_COLLECT_ERRORS,
    WORKFLOW_FORK_FAILURE_POLICY_ALLOW_PARTIAL_SUCCESS,
)

WORKFLOW_FORK_MERGE_POLICY_LAST_WRITER_WINS = "deterministic_last_writer_wins"
WORKFLOW_FORK_ALLOWED_MERGE_POLICIES: tuple[str, ...] = (
    WORKFLOW_FORK_MERGE_POLICY_LAST_WRITER_WINS,
)

WORKFLOW_STEP_RESULT_ENVELOPE_SCHEMA_VERSION = "workflow_step_result_envelope.v1"
WORKFLOW_RESULT_ENVELOPE_SCHEMA_VERSION = "workflow_result_envelope.v1"

WORKFLOW_STEP_RESULT_ENVELOPES_KEY = "workflow_step_result_envelopes"
LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY = "last_workflow_step_result_envelope"
WORKFLOW_RESULT_ENVELOPE_KEY = "workflow_result_envelope"

WORKFLOW_RUNTIME_METRICS_KEY = "workflow_runtime_metrics"
WORKFLOW_RUNTIME_EVENTS_KEY = "workflow_control_flow_events"
WORKFLOW_FORK_CONTEXTS_KEY = "__workflow_fork_contexts"

LAST_CONTROL_SIGNAL_KEY = "last_control_signal"
LAST_CONTROL_SIGNAL_SCOPE_KEY = "last_control_signal_scope"
LAST_CONTROL_SIGNAL_BREAK_KEY = "last_control_signal_break"
LAST_CONTROL_SIGNAL_CONTINUE_KEY = "last_control_signal_continue"
LAST_CONTROL_SIGNAL_RETURN_KEY = "last_control_signal_return"
LAST_CONTROL_SIGNAL_ERROR_KEY = "last_control_signal_error"
WORKFLOW_RETURN_PAYLOAD_KEY = "workflow_return_payload"


def _normalise_text(value: Any) -> str:
    return str(value or "").strip()


def normalise_control_signal(
    value: Any,
    *,
    default: str = WORKFLOW_CONTROL_SIGNAL_NONE,
) -> str:
    raw = _normalise_text(value).lower()
    if raw in WORKFLOW_CONTROL_SIGNAL_ALLOWED:
        return raw
    return default


def normalise_signal_scope(value: Any) -> str | None:
    text = _normalise_text(value)
    return text or None


def resolve_control_signal_from_outputs(
    *,
    action_outcome: str,
    outputs: Mapping[str, Any] | None,
) -> tuple[str, str | None, Any | None]:
    payload = outputs if isinstance(outputs, Mapping) else {}
    nested_control = payload.get("workflow_control")
    nested_control_map = nested_control if isinstance(nested_control, Mapping) else {}

    signal = normalise_control_signal(
        payload.get("control_signal")
        or payload.get("workflow_control_signal")
        or nested_control_map.get("signal")
    )
    scope = normalise_signal_scope(
        payload.get("control_scope")
        or payload.get("control_signal_scope")
        or nested_control_map.get("scope")
    )
    return_payload = payload.get("return_payload")
    if return_payload is None and signal == WORKFLOW_CONTROL_SIGNAL_RETURN:
        return_payload = payload.get("result")

    if signal == WORKFLOW_CONTROL_SIGNAL_NONE and action_outcome == "failure":
        signal = WORKFLOW_CONTROL_SIGNAL_ERROR

    return signal, scope, return_payload


def clear_control_signal_context(context: MutableMapping[str, Any]) -> None:
    context[LAST_CONTROL_SIGNAL_KEY] = WORKFLOW_CONTROL_SIGNAL_NONE
    context[LAST_CONTROL_SIGNAL_SCOPE_KEY] = None
    context[LAST_CONTROL_SIGNAL_BREAK_KEY] = False
    context[LAST_CONTROL_SIGNAL_CONTINUE_KEY] = False
    context[LAST_CONTROL_SIGNAL_RETURN_KEY] = False
    context[LAST_CONTROL_SIGNAL_ERROR_KEY] = False


def stamp_control_signal_context(
    *,
    context: MutableMapping[str, Any],
    signal: str,
    scope: str | None = None,
    return_payload: Any | None = None,
) -> None:
    normalised_signal = normalise_control_signal(signal)
    clear_control_signal_context(context)
    context[LAST_CONTROL_SIGNAL_KEY] = normalised_signal
    context[LAST_CONTROL_SIGNAL_SCOPE_KEY] = normalise_signal_scope(scope)
    context[LAST_CONTROL_SIGNAL_BREAK_KEY] = (
        normalised_signal == WORKFLOW_CONTROL_SIGNAL_BREAK
    )
    context[LAST_CONTROL_SIGNAL_CONTINUE_KEY] = (
        normalised_signal == WORKFLOW_CONTROL_SIGNAL_CONTINUE
    )
    context[LAST_CONTROL_SIGNAL_RETURN_KEY] = (
        normalised_signal == WORKFLOW_CONTROL_SIGNAL_RETURN
    )
    context[LAST_CONTROL_SIGNAL_ERROR_KEY] = (
        normalised_signal == WORKFLOW_CONTROL_SIGNAL_ERROR
    )

    if normalised_signal == WORKFLOW_CONTROL_SIGNAL_RETURN:
        context[WORKFLOW_RETURN_PAYLOAD_KEY] = return_payload
    elif WORKFLOW_RETURN_PAYLOAD_KEY in context:
        context[WORKFLOW_RETURN_PAYLOAD_KEY] = None


def get_last_control_signal(context: Mapping[str, Any]) -> str:
    return normalise_control_signal(context.get(LAST_CONTROL_SIGNAL_KEY))


def get_last_control_signal_scope(context: Mapping[str, Any]) -> str | None:
    return normalise_signal_scope(context.get(LAST_CONTROL_SIGNAL_SCOPE_KEY))


def append_runtime_event(
    *,
    context: MutableMapping[str, Any],
    event: Mapping[str, Any],
) -> None:
    existing = context.get(WORKFLOW_RUNTIME_EVENTS_KEY)
    if not isinstance(existing, list):
        existing = []
        context[WORKFLOW_RUNTIME_EVENTS_KEY] = existing
    existing.append(dict(event))


def increment_runtime_metric(
    *,
    context: MutableMapping[str, Any],
    key: str,
    amount: int = 1,
) -> None:
    metrics = context.get(WORKFLOW_RUNTIME_METRICS_KEY)
    if not isinstance(metrics, dict):
        metrics = {}
        context[WORKFLOW_RUNTIME_METRICS_KEY] = metrics
    metrics[key] = int(metrics.get(key, 0)) + int(amount)


def _build_mutation_summary(
    *,
    context_before: Mapping[str, Any],
    context_after: Mapping[str, Any],
    declared_output_payload: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    before_keys = set(context_before.keys())
    after_keys = set(context_after.keys())
    added_keys = sorted(after_keys - before_keys)
    removed_keys = sorted(before_keys - after_keys)

    changed_keys = sorted(
        key
        for key in before_keys.intersection(after_keys)
        if context_before.get(key) != context_after.get(key)
    )
    declared_output_keys = sorted(
        key
        for key in (declared_output_payload or {}).keys()
        if isinstance(key, str) and key
    )
    return {
        "declared_output_keys": declared_output_keys,
        "added_keys": added_keys,
        "changed_keys": changed_keys,
        "removed_keys": removed_keys,
        "changed_key_count": len(added_keys) + len(changed_keys) + len(removed_keys),
    }


def build_step_result_envelope(
    *,
    workflow_id: str,
    state_id: str,
    action_id: str,
    action_status: str,
    action_outcome: str,
    action_error: str | None,
    action_outputs: Mapping[str, Any] | None,
    control_signal: str,
    control_signal_scope: str | None,
    duration_ms: float | None,
    context_before: Mapping[str, Any],
    context_after: Mapping[str, Any],
) -> Dict[str, Any]:
    output_payload = (
        dict(action_outputs)
        if isinstance(action_outputs, Mapping)
        else {}
    )
    return {
        "schema_version": WORKFLOW_STEP_RESULT_ENVELOPE_SCHEMA_VERSION,
        "workflow_id": str(workflow_id or "").strip(),
        "state_id": str(state_id or "").strip(),
        "action_id": str(action_id or "").strip(),
        "action_status": str(action_status or "").strip(),
        "action_outcome": str(action_outcome or "").strip(),
        "control_signal": normalise_control_signal(control_signal),
        "control_signal_scope": normalise_signal_scope(control_signal_scope),
        "output_payload": output_payload,
        "mutation_summary": _build_mutation_summary(
            context_before=context_before,
            context_after=context_after,
            declared_output_payload=output_payload,
        ),
        "diagnostics": {
            "error": str(action_error) if action_error else None,
            "duration_ms": duration_ms,
        },
    }


def append_step_result_envelope(
    *,
    context: MutableMapping[str, Any],
    envelope: Mapping[str, Any],
) -> None:
    existing = context.get(WORKFLOW_STEP_RESULT_ENVELOPES_KEY)
    if not isinstance(existing, list):
        existing = []
        context[WORKFLOW_STEP_RESULT_ENVELOPES_KEY] = existing
    payload = dict(envelope)
    existing.append(payload)
    context[LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY] = payload


def build_workflow_result_envelope(
    *,
    workflow_id: str,
    completed: bool,
    final_state: str,
    error: str | None,
    control_signal: str,
    return_payload: Any,
    context: Mapping[str, Any],
    transition_count: int,
) -> Dict[str, Any]:
    step_envelopes = context.get(WORKFLOW_STEP_RESULT_ENVELOPES_KEY)
    step_count = len(step_envelopes) if isinstance(step_envelopes, list) else 0
    runtime_metrics = context.get(WORKFLOW_RUNTIME_METRICS_KEY)
    metrics_payload = dict(runtime_metrics) if isinstance(runtime_metrics, Mapping) else {}
    return {
        "schema_version": WORKFLOW_RESULT_ENVELOPE_SCHEMA_VERSION,
        "workflow_id": str(workflow_id or "").strip(),
        "completed": bool(completed),
        "final_state": str(final_state or "").strip(),
        "control_signal": normalise_control_signal(control_signal),
        "declared_output_payload": return_payload,
        "mutation_summary": {
            "step_count": step_count,
            "context_key_count": len(context),
        },
        "diagnostics": {
            "error": str(error) if error else None,
            "transition_count": int(transition_count),
            "metrics": metrics_payload,
        },
    }


def set_workflow_result_envelope(
    *,
    context: MutableMapping[str, Any],
    envelope: Mapping[str, Any],
) -> None:
    context[WORKFLOW_RESULT_ENVELOPE_KEY] = dict(envelope)

