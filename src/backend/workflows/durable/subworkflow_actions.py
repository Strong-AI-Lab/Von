"""Durable action handlers for explicit subworkflow invocation.

JVNAUTOSCI-1310:
- make parent->child workflow composition executable via a first-class action,
- keep invocation semantics explicit and traceable, and
- keep failure propagation policy deterministic.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Mapping, Sequence

from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import WorkflowDefinition, WorkflowExecutor
from ..subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE,
)
from ..execution_contracts import (
    WORKFLOW_CONTROL_SIGNAL_NONE,
    append_runtime_event,
    increment_runtime_metric,
    normalise_control_signal,
)
from ..trace_model import WorkflowExecutionTrace
from ..vontology_loader import load_workflow_definition_from_vontology

_FAILURE_MODE_INPUT_KEYS: tuple[str, ...] = ("failure_mode", "__failure_mode")
_RESERVED_SUBWORKFLOW_INPUT_KEYS: set[str] = {
    "workflow_id",
    "__parent_workflow_id",
    "__parent_state_id",
    "__workflow_invocation_chain",
    "failure_mode",
    "__failure_mode",
    "max_transitions",
}
_INVOCATION_CHAIN_KEY = "__workflow_invocation_chain"
_MAX_SUBWORKFLOW_DEPTH_ENV = "VON_WORKFLOW_SUBWORKFLOW_MAX_DEPTH"
_DEFAULT_SUBWORKFLOW_DEPTH_LIMIT = 8
_MAX_SUBWORKFLOW_INVOCATIONS_ENV = "VON_WORKFLOW_SUBWORKFLOW_MAX_INVOCATIONS"
_DEFAULT_SUBWORKFLOW_INVOCATION_LIMIT = 64
_DEFAULT_MAX_TRANSITIONS = 40
_MAX_TRANSITIONS_LIMIT = 300


def _normalise_text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_chain(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    chain: list[str] = []
    for item in value:
        text = _normalise_text(item)
        if text:
            chain.append(text)
    return chain


def _coerce_max_depth() -> int:
    raw = os.getenv(_MAX_SUBWORKFLOW_DEPTH_ENV)
    if raw is None:
        return _DEFAULT_SUBWORKFLOW_DEPTH_LIMIT
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_SUBWORKFLOW_DEPTH_LIMIT
    return max(1, min(32, parsed))


def _coerce_invocation_limit() -> int:
    raw = os.getenv(_MAX_SUBWORKFLOW_INVOCATIONS_ENV)
    if raw is None:
        return _DEFAULT_SUBWORKFLOW_INVOCATION_LIMIT
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_SUBWORKFLOW_INVOCATION_LIMIT
    return max(1, min(512, parsed))


def _coerce_max_transitions(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = _DEFAULT_MAX_TRANSITIONS
    return max(5, min(_MAX_TRANSITIONS_LIMIT, parsed))


def _resolve_failure_mode(inputs: Mapping[str, Any]) -> str:
    for key in _FAILURE_MODE_INPUT_KEYS:
        candidate = _normalise_text(inputs.get(key))
        if candidate in {
            WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
            WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE,
        }:
            return candidate
    return WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE


def _extract_child_inputs(
    *,
    inputs: Mapping[str, Any],
    invocation_chain: Sequence[str],
    parent_workflow_id: str,
    parent_state_id: str,
) -> Dict[str, Any]:
    child_inputs: Dict[str, Any] = {}
    for key, value in inputs.items():
        key_text = _normalise_text(key)
        if not key_text or key_text in _RESERVED_SUBWORKFLOW_INPUT_KEYS:
            continue
        child_inputs[key_text] = value
    child_inputs[_INVOCATION_CHAIN_KEY] = list(invocation_chain)
    if parent_workflow_id:
        child_inputs.setdefault("__parent_workflow_id", parent_workflow_id)
    if parent_state_id:
        child_inputs.setdefault("__parent_state_id", parent_state_id)
    return child_inputs


def _append_parent_trace_event(
    *,
    request_trace: Any,
    invocation_event: Mapping[str, Any],
) -> None:
    metadata = getattr(request_trace, "metadata", None)
    if not isinstance(metadata, dict):
        return
    existing = metadata.get("subworkflow_invocations")
    if not isinstance(existing, list):
        existing = []
        metadata["subworkflow_invocations"] = existing
    existing.append(dict(invocation_event))


def _build_subworkflow_handler(
    *,
    registry: ActionRegistry,
    definition_loader: Callable[[str], WorkflowDefinition | None],
) -> Callable[[WorkflowActionRequest], WorkflowActionResult]:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        inputs = dict(request.inputs or {})
        child_workflow_id = _normalise_text(inputs.get("workflow_id"))
        parent_workflow_id = _normalise_text(inputs.get("__parent_workflow_id"))
        parent_state_id = _normalise_text(inputs.get("__parent_state_id"))
        failure_mode = _resolve_failure_mode(inputs)

        if not child_workflow_id:
            return WorkflowActionResult(
                status="failed",
                error="subworkflow_workflow_id_missing",
            )

        invocation_chain = _normalise_chain(request.data.get(_INVOCATION_CHAIN_KEY))
        if not invocation_chain and parent_workflow_id:
            invocation_chain = [parent_workflow_id]

        if child_workflow_id in invocation_chain:
            return WorkflowActionResult(
                status="failed",
                error=(
                    "subworkflow_recursive_invocation:"
                    f"{'->'.join([*invocation_chain, child_workflow_id])}"
                ),
            )

        max_depth = _coerce_max_depth()
        if len(invocation_chain) >= max_depth:
            return WorkflowActionResult(
                status="failed",
                error=(
                    "subworkflow_invocation_depth_exceeded:"
                    f"max_depth={max_depth}"
                ),
            )

        invocation_limit = _coerce_invocation_limit()
        try:
            invocation_count = int(request.data.get("__workflow_subworkflow_invocation_count", 0))
        except (TypeError, ValueError):
            invocation_count = 0
        if invocation_count >= invocation_limit:
            return WorkflowActionResult(
                status="failed",
                error=(
                    "subworkflow_invocation_budget_exceeded:"
                    f"max_invocations={invocation_limit}"
                ),
            )

        definition = definition_loader(child_workflow_id)
        if definition is None:
            return WorkflowActionResult(
                status="failed",
                error=f"subworkflow_definition_not_found:{child_workflow_id}",
            )

        child_chain = [*invocation_chain, child_workflow_id]
        child_inputs = _extract_child_inputs(
            inputs=inputs,
            invocation_chain=child_chain,
            parent_workflow_id=parent_workflow_id,
            parent_state_id=parent_state_id,
        )
        child_inputs["__workflow_subworkflow_invocation_count"] = invocation_count + 1
        request.data["__workflow_subworkflow_invocation_count"] = invocation_count + 1
        child_trace = WorkflowExecutionTrace(
            workflow_id=child_workflow_id,
            user_namespace=request.environment.user_namespace,
            metadata={
                "parent_workflow_id": parent_workflow_id or None,
                "parent_state_id": parent_state_id or None,
                "failure_mode": failure_mode,
                "invocation_chain": list(child_chain),
                "invocation_count": invocation_count + 1,
                "invocation_limit": invocation_limit,
            },
        )
        executor = WorkflowExecutor(
            registry=registry,
            max_transitions=_coerce_max_transitions(inputs.get("max_transitions")),
        )
        child_result = executor.run(
            definition,
            environment=request.environment,
            data=child_inputs,
            trace=child_trace,
        )

        invocation_event: Dict[str, Any] = {
            "parent_workflow_id": parent_workflow_id or None,
            "parent_state_id": parent_state_id or None,
            "child_workflow_id": child_workflow_id,
            "failure_mode": failure_mode,
            "invocation_chain": list(child_chain),
            "invocation_count": invocation_count + 1,
            "invocation_limit": invocation_limit,
            "child_completed": bool(child_result.completed),
            "child_final_state": _normalise_text(child_result.final_state),
            "child_error": _normalise_text(child_result.error) or None,
        }
        _append_parent_trace_event(
            request_trace=request.trace,
            invocation_event=invocation_event,
        )
        increment_runtime_metric(context=request.data, key="subworkflow_invocations")
        append_runtime_event(
            context=request.data,
            event={
                "status": "subworkflow_invoked",
                "child_workflow_id": child_workflow_id,
                "child_completed": bool(child_result.completed),
                "child_final_state": _normalise_text(child_result.final_state),
                "invocation_count": invocation_count + 1,
                "invocation_limit": invocation_limit,
            },
        )

        outputs: Dict[str, Any] = {
            "result": dict(child_result.data),
            "subworkflow_invocation": invocation_event,
            "subworkflow_result_envelope": (
                dict(child_result.result_envelope)
                if isinstance(child_result.result_envelope, Mapping)
                else None
            ),
        }
        child_control_signal = normalise_control_signal(
            (
                child_result.result_envelope or {}
            ).get("control_signal")
            if isinstance(child_result.result_envelope, Mapping)
            else None
        )
        if child_control_signal != WORKFLOW_CONTROL_SIGNAL_NONE:
            outputs["control_signal"] = child_control_signal
            outputs["workflow_control"] = {"signal": child_control_signal}
            child_scope = (
                (child_result.result_envelope or {}).get("control_signal_scope")
                if isinstance(child_result.result_envelope, Mapping)
                else None
            )
            if isinstance(child_scope, str) and child_scope.strip():
                outputs["control_scope"] = child_scope.strip()
                outputs["workflow_control"]["scope"] = child_scope.strip()
        if child_result.completed:
            return WorkflowActionResult(status="success", outputs=outputs)

        failure_error = child_result.error or child_result.final_state or "subworkflow_failed"
        formatted_error = f"subworkflow_failed:{child_workflow_id}:{failure_error}"
        if failure_mode == WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE:
            outputs["child_workflow_failed"] = True
            outputs["subworkflow_error"] = formatted_error
            outputs["subworkflow_final_state"] = child_result.final_state
            return WorkflowActionResult(status="success", outputs=outputs)

        return WorkflowActionResult(
            status="failed",
            error=formatted_error,
            outputs=outputs,
        )

    return _handle


def register_subworkflow_actions(
    registry: ActionRegistry,
    *,
    definition_loader: Callable[[str], WorkflowDefinition | None] | None = None,
) -> None:
    """Register the canonical subworkflow invocation action.

    Keep this action explicitly registered so runnability checks can reason over
    a known action ID instead of relying on fallback tool routing.
    """
    loader = definition_loader or load_workflow_definition_from_vontology
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
            handler=_build_subworkflow_handler(
                registry=registry,
                definition_loader=loader,
            ),
            description="Invoke a child workflow using explicit subworkflow contracts.",
        )
    )


__all__ = ["register_subworkflow_actions"]
