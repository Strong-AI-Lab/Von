"""Durable action handlers for explicit subworkflow invocation.

JVNAUTOSCI-1310:
- make parent->child workflow composition executable via a first-class action,
- keep invocation semantics explicit and traceable, and
- keep failure propagation policy deterministic.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Mapping, Sequence

from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import (
    LAST_WORKFLOW_APPROVAL_GATE_KEY,
    LAST_WORKFLOW_IDEMPOTENCY_EVENT_KEY,
    LAST_WORKFLOW_RETRY_EVENT_KEY,
    LAST_WORKFLOW_TERMINAL_EFFECT_EVENT_KEY,
    LAST_WORKFLOW_TOOL_OUTPUT_MAPPING_EVENT_KEY,
    WORKFLOW_APPROVAL_GATE_EVENTS_KEY,
    WORKFLOW_IDEMPOTENCY_EVENTS_KEY,
    WORKFLOW_IDEMPOTENCY_RECORDS_KEY,
    WORKFLOW_RETRY_EVENTS_KEY,
    WORKFLOW_TERMINAL_EFFECT_EVENTS_KEY,
    WORKFLOW_TOOL_OUTPUT_MAPPING_EVENTS_KEY,
    WorkflowDefinition,
    WorkflowExecutor,
)
from ..execution_contracts import (
    LAST_CONTROL_SIGNAL_BREAK_KEY,
    LAST_CONTROL_SIGNAL_CONTINUE_KEY,
    LAST_CONTROL_SIGNAL_ERROR_KEY,
    LAST_CONTROL_SIGNAL_KEY,
    LAST_CONTROL_SIGNAL_RETURN_KEY,
    LAST_CONTROL_SIGNAL_SCOPE_KEY,
    LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
    WORKFLOW_CONTROL_SIGNAL_NONE,
    WORKFLOW_RESULT_ENVELOPE_KEY,
    WORKFLOW_RETURN_PAYLOAD_KEY,
    WORKFLOW_RUNTIME_EVENTS_KEY,
    WORKFLOW_RUNTIME_METRICS_KEY,
    WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
    append_runtime_event,
    increment_runtime_metric,
    normalise_control_signal,
)
from ..metadata_validation import LAST_METADATA_EVENT_KEY, WORKFLOW_METADATA_EVENTS_KEY
from ..plan_state_runtime import (
    LAST_WORKFLOW_COMPLETION_GATE_KEY,
    LAST_WORKFLOW_PLAN_STATE_EVENT_KEY,
    WORKFLOW_COMPLETION_GATE_KEY,
    WORKFLOW_PLAN_STATE_EVENTS_KEY,
    WORKFLOW_PLAN_STATE_KEY,
)
from ..subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE,
)
from ..tool_invocation_evidence import (
    derive_tool_invocation_records_from_step_envelopes,
)
from ..trace_model import WorkflowExecutionTrace
from ..vontology_loader import load_workflow_definition_from_vontology
from ..workflow_launch_input_contracts import (
    WORKFLOW_LAUNCH_INPUT_EXCLUDED_AMBIENT_INPUT_KEYS,
    normalise_workflow_launch_input_contract,
    resolve_workflow_launch_inputs,
)
from .llm_cost_tracking import merge_nested_llm_calls_for_cost
from .nested_workflow_authority import (
    NESTED_WORKFLOW_DEFINITION_NOT_FOUND,
    resolve_nested_workflow_definition,
)

_FAILURE_MODE_INPUT_KEYS: tuple[str, ...] = ("failure_mode", "__failure_mode")
_RESERVED_SUBWORKFLOW_INPUT_KEYS: set[str] = {
    "workflow_id",
    "__parent_workflow_id",
    "__parent_state_id",
    "__workflow_invocation_chain",
    "__workflow_ambient_input_keys",
    "failure_mode",
    "__failure_mode",
    "inherit_parent_context",
    "max_transitions",
}
_NAMESPACED_WORKFLOW_LAUNCH_INPUT_RESERVED_KEYS: frozenset[str] = frozenset(
    {
        "workflow_id",
        "failure_mode",
        "selected_workflow_id",
        "prompt",
        "user_prompt",
    }
)
_INVOCATION_CHAIN_KEY = "__workflow_invocation_chain"
_AMBIENT_INPUT_KEYS_KEY = "__workflow_ambient_input_keys"
_INVOCATION_LEDGER_KEY = "__workflow_subworkflow_invocation_ledger"
_MAX_SUBWORKFLOW_DEPTH_ENV = "VON_WORKFLOW_SUBWORKFLOW_MAX_DEPTH"
_DEFAULT_SUBWORKFLOW_DEPTH_LIMIT = 8
_MAX_SUBWORKFLOW_INVOCATIONS_ENV = "VON_WORKFLOW_SUBWORKFLOW_MAX_INVOCATIONS"
_DEFAULT_SUBWORKFLOW_INVOCATION_LIMIT = 64
_DEFAULT_MAX_TRANSITIONS = 40
_MAX_TRANSITIONS_LIMIT = 300
logger = logging.getLogger(__name__)

_INTERNAL_CHILD_RESULT_KEYS: frozenset[str] = frozenset(
    {
        "__parent_state_id",
        "__parent_workflow_id",
        "__workflow_invocation_chain",
        "__workflow_subworkflow_invocation_count",
        LAST_CONTROL_SIGNAL_KEY,
        LAST_CONTROL_SIGNAL_SCOPE_KEY,
        LAST_CONTROL_SIGNAL_BREAK_KEY,
        LAST_CONTROL_SIGNAL_CONTINUE_KEY,
        LAST_CONTROL_SIGNAL_RETURN_KEY,
        LAST_CONTROL_SIGNAL_ERROR_KEY,
        WORKFLOW_RETURN_PAYLOAD_KEY,
        WORKFLOW_RUNTIME_EVENTS_KEY,
        WORKFLOW_RUNTIME_METRICS_KEY,
        WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
        LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
        WORKFLOW_RESULT_ENVELOPE_KEY,
        WORKFLOW_TOOL_OUTPUT_MAPPING_EVENTS_KEY,
        LAST_WORKFLOW_TOOL_OUTPUT_MAPPING_EVENT_KEY,
        WORKFLOW_TERMINAL_EFFECT_EVENTS_KEY,
        LAST_WORKFLOW_TERMINAL_EFFECT_EVENT_KEY,
        WORKFLOW_APPROVAL_GATE_EVENTS_KEY,
        LAST_WORKFLOW_APPROVAL_GATE_KEY,
        WORKFLOW_RETRY_EVENTS_KEY,
        LAST_WORKFLOW_RETRY_EVENT_KEY,
        WORKFLOW_IDEMPOTENCY_EVENTS_KEY,
        LAST_WORKFLOW_IDEMPOTENCY_EVENT_KEY,
        WORKFLOW_IDEMPOTENCY_RECORDS_KEY,
        WORKFLOW_METADATA_EVENTS_KEY,
        LAST_METADATA_EVENT_KEY,
        WORKFLOW_PLAN_STATE_KEY,
        WORKFLOW_PLAN_STATE_EVENTS_KEY,
        LAST_WORKFLOW_PLAN_STATE_EVENT_KEY,
        WORKFLOW_COMPLETION_GATE_KEY,
        LAST_WORKFLOW_COMPLETION_GATE_KEY,
    }
)


def _normalise_namespaced_workflow_launch_inputs(raw_value: Any) -> dict[str, Any]:
    if not isinstance(raw_value, Mapping):
        return {}

    normalised: dict[str, Any] = {}
    for key, value in raw_value.items():
        key_text = _normalise_text(key)
        if (
            not key_text
            or key_text.startswith("__")
            or key_text in _NAMESPACED_WORKFLOW_LAUNCH_INPUT_RESERVED_KEYS
        ):
            continue
        normalised[key_text] = value
    return normalised


_TELEMETRY_CHILD_RESULT_KEYS: frozenset[str] = frozenset(
    {
        "aux_llm_calls",
        "llm_calls",
        "llm_step_envelope",
        "tool_invocations",
        "tool_messages",
    }
)


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


SUBWORKFLOW_RESOURCE_EXHAUSTION_ERROR_PREFIXES: tuple[str, ...] = (
    "subworkflow_invocation_budget_exceeded",
    "subworkflow_invocation_depth_exceeded",
)


def is_subworkflow_resource_exhaustion_error(error: Any) -> bool:
    """Structurally classify our own budget/depth error codes.

    Retries share the monotonic per-turn invocation counter, so an execution
    that failed on resource exhaustion cannot succeed on retry.
    """

    return (
        str(error or "")
        .strip()
        .startswith(SUBWORKFLOW_RESOURCE_EXHAUSTION_ERROR_PREFIXES)
    )


def get_subworkflow_invocation_budget_state(request_data: Any) -> dict[str, Any]:
    limit = _coerce_invocation_limit()
    try:
        count = int(request_data.get("__workflow_subworkflow_invocation_count", 0))
    except (AttributeError, TypeError, ValueError):
        count = 0
    return {
        "invocation_count": count,
        "invocation_limit": limit,
        "remaining": max(0, limit - count),
        "exhausted": count >= limit,
    }


def summarise_subworkflow_invocation_ledger(request_data: Any) -> dict[str, Any]:
    return _summarise_subworkflow_invocation_ledger(request_data)


def _note_subworkflow_invocation(
    request_data: Any,
    *,
    child_workflow_id: str,
    parent_workflow_id: str | None,
    parent_state_id: str | None,
    invocation_index: int,
    invocation_limit: int,
    route: str,
) -> None:
    """Record a subworkflow invocation in the per-turn ledger and the log.

    The ledger is the diagnosable trail for subworkflow budget exhaustion:
    when the budget is exceeded, the failure names what consumed it instead
    of leaving an opaque counter (JVNAUTOSCI-2502).
    """

    entry = {
        "index": invocation_index,
        "child_workflow_id": child_workflow_id,
        "parent_workflow_id": parent_workflow_id or None,
        "parent_state_id": parent_state_id or None,
        "route": route,
    }
    try:
        ledger = request_data.get(_INVOCATION_LEDGER_KEY)
        if not isinstance(ledger, list):
            ledger = []
            request_data[_INVOCATION_LEDGER_KEY] = ledger
        ledger.append(entry)
    except Exception:
        pass
    logger.info(
        "[subworkflow] invocation %d/%d parent=%s state=%s child=%s route=%s",
        invocation_index,
        invocation_limit,
        parent_workflow_id or "-",
        parent_state_id or "-",
        child_workflow_id,
        route,
    )


def _summarise_subworkflow_invocation_ledger(request_data: Any) -> dict[str, Any]:
    ledger = None
    try:
        ledger = request_data.get(_INVOCATION_LEDGER_KEY)
    except Exception:
        ledger = None
    entries = ledger if isinstance(ledger, list) else []
    per_child: Dict[str, int] = {}
    per_parent_state: Dict[str, int] = {}
    for raw in entries:
        if not isinstance(raw, Mapping):
            continue
        child = str(raw.get("child_workflow_id") or "").strip() or "unknown"
        per_child[child] = per_child.get(child, 0) + 1
        state = str(raw.get("parent_state_id") or "").strip() or "unknown"
        per_parent_state[state] = per_parent_state.get(state, 0) + 1
    return {
        "schema_version": "subworkflow_invocation_ledger_summary.v1",
        "total_invocations": len(entries),
        "per_child_workflow": dict(
            sorted(per_child.items(), key=lambda item: -item[1])
        ),
        "per_parent_state": dict(
            sorted(per_parent_state.items(), key=lambda item: -item[1])
        ),
    }


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


def _normalise_input_key_list(value: Any) -> set[str]:
    if isinstance(value, str):
        cleaned = _normalise_text(value)
        return {cleaned} if cleaned else set()
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {cleaned for item in value if (cleaned := _normalise_text(item))}


def _extract_child_inputs(
    *,
    inputs: Mapping[str, Any],
    parent_context: Mapping[str, Any],
    invocation_chain: Sequence[str],
    parent_workflow_id: str,
    parent_state_id: str,
) -> Dict[str, Any]:
    child_inputs: Dict[str, Any] = {}
    if bool(inputs.get("inherit_parent_context")):
        for key, value in parent_context.items():
            key_text = _normalise_text(key)
            if (
                not key_text
                or key_text.startswith("__workflow")
                or key_text in _INTERNAL_CHILD_RESULT_KEYS
            ):
                continue
            child_inputs[key_text] = value
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


def _apply_child_launch_input_contract(
    *,
    child_workflow_id: str,
    definition: WorkflowDefinition,
    child_inputs: Dict[str, Any],
    ambient_input_keys: set[str] | None = None,
) -> dict[str, Any] | None:
    """Resolve a child workflow's represented launch contract before execution."""

    workflow_metadata = getattr(definition, "metadata", None)
    if not isinstance(workflow_metadata, Mapping):
        return None
    launch_input_contract = workflow_metadata.get("launch_input_contract")
    if not isinstance(launch_input_contract, Mapping):
        return None
    launch_input_contract_source = workflow_metadata.get("launch_input_contract_source")
    normalised_contract, _contract_error = normalise_workflow_launch_input_contract(
        launch_input_contract
    )
    excluded_keys = [
        _normalise_text(key)
        for key in (
            (
                normalised_contract.get(
                    WORKFLOW_LAUNCH_INPUT_EXCLUDED_AMBIENT_INPUT_KEYS
                )
                if isinstance(normalised_contract, Mapping)
                else []
            )
            or []
        )
        if _normalise_text(key)
    ]
    ambient_keys = set(ambient_input_keys or set())
    pre_resolution_excluded_applied: list[str] = []
    if ambient_keys and excluded_keys:
        for key in excluded_keys:
            if key in ambient_keys and key in child_inputs:
                child_inputs.pop(key, None)
                pre_resolution_excluded_applied.append(key)
    namespaced_launch_inputs = _normalise_namespaced_workflow_launch_inputs(
        child_inputs.get("workflow_launch_inputs")
    )
    resolution_inputs: Dict[str, Any] = dict(child_inputs)
    if namespaced_launch_inputs:
        resolution_inputs.update(namespaced_launch_inputs)

    resolution = resolve_workflow_launch_inputs(
        workflow_id=child_workflow_id,
        contract=launch_input_contract,
        inputs=resolution_inputs,
        contract_source=(
            str(launch_input_contract_source).strip()
            if isinstance(launch_input_contract_source, str)
            and launch_input_contract_source.strip()
            else None
        ),
    )
    resolved_inputs = {
        _normalise_text(key): value
        for key, value in resolution.resolved_inputs.items()
        if _normalise_text(key)
    }
    diagnostics: dict[str, Any] = dict(resolution.diagnostics)
    if namespaced_launch_inputs:
        diagnostics["namespaced_workflow_launch_inputs_applied"] = sorted(
            namespaced_launch_inputs.keys()
        )
    excluded_applied: list[str] = list(pre_resolution_excluded_applied)
    for key in excluded_keys:
        if key in resolved_inputs:
            child_inputs[key] = resolved_inputs[key]
            continue
        if key in child_inputs:
            child_inputs.pop(key, None)
            excluded_applied.append(key)
    for key, value in resolved_inputs.items():
        if namespaced_launch_inputs:
            child_inputs[key] = value
        else:
            child_inputs.setdefault(key, value)
    if excluded_applied:
        diagnostics["excluded_ambient_inputs_applied"] = sorted(
            dict.fromkeys(excluded_applied)
        )
    child_inputs["workflow_launch_input_resolution"] = diagnostics
    return diagnostics


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


def _compact_child_result_payload(
    child_data: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Strip child runtime telemetry before copying child context into parent state.

    Parent workflows should consume child business outputs via explicit
    `result.<field>` mappings, not by inheriting the child's full execution
    telemetry and checkpoint history. Keeping that boundary compact prevents
    recursive/nested step-envelope persistence from blowing up durable
    checkpointing while preserving the child outputs that workflow metadata maps.
    """

    if not isinstance(child_data, Mapping):
        return {}

    compact_payload: Dict[str, Any] = {}
    for raw_key, value in child_data.items():
        key = _normalise_text(raw_key)
        if not key:
            continue
        if key in _INTERNAL_CHILD_RESULT_KEYS or key in _TELEMETRY_CHILD_RESULT_KEYS:
            continue
        compact_payload[key] = value
    return compact_payload


def _derive_child_step_invocations(
    result: Any,
    *,
    child_workflow_id: str,
) -> list[dict[str, Any]]:
    data = getattr(result, "data", None)
    if not isinstance(data, Mapping):
        return []
    context_payload = {
        key: data.get(key)
        for key in (
            "concept_id",
            "paper_concept_id",
            "file_copy_concept_id",
            "computer_file_copy_concept_id",
            "source_file_copy_concept_id",
            "represented_artefact_concept_id",
        )
        if data.get(key) not in (None, "", [], {})
    }
    if "concept_id" not in context_payload and context_payload.get(
        "represented_artefact_concept_id"
    ):
        context_payload["concept_id"] = context_payload[
            "represented_artefact_concept_id"
        ]
    return derive_tool_invocation_records_from_step_envelopes(
        data.get(WORKFLOW_STEP_RESULT_ENVELOPES_KEY),
        context_payload=context_payload,
        workflow_id_filter=child_workflow_id,
    )


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
                error=(f"subworkflow_invocation_depth_exceeded:max_depth={max_depth}"),
            )

        invocation_limit = _coerce_invocation_limit()
        try:
            invocation_count = int(
                request.data.get("__workflow_subworkflow_invocation_count", 0)
            )
        except (TypeError, ValueError):
            invocation_count = 0
        if invocation_count >= invocation_limit:
            ledger_summary = _summarise_subworkflow_invocation_ledger(request.data)
            logger.warning(
                "[subworkflow] invocation budget exceeded: child=%s parent=%s "
                "state=%s limit=%d ledger=%s",
                child_workflow_id,
                parent_workflow_id or "-",
                parent_state_id or "-",
                invocation_limit,
                ledger_summary,
            )
            return WorkflowActionResult(
                status="failed",
                error=(
                    "subworkflow_invocation_budget_exceeded:"
                    f"max_invocations={invocation_limit}"
                ),
                outputs={
                    "subworkflow_invocation_ledger_summary": ledger_summary,
                    "subworkflow_budget_exhausted": True,
                    "denied_child_workflow_id": child_workflow_id,
                },
            )

        authority_resolution = resolve_nested_workflow_definition(
            workflow_id=child_workflow_id,
            environment=request.environment,
            fallback_loader=definition_loader,
            execution_scope=request.execution_scope,
        )
        definition = authority_resolution.definition
        if definition is None:
            error_code = authority_resolution.error_code
            error = (
                f"subworkflow_definition_not_found:{child_workflow_id}"
                if error_code == NESTED_WORKFLOW_DEFINITION_NOT_FOUND
                else (
                    "subworkflow_definition_resolution_failed:"
                    f"{child_workflow_id}:{error_code or 'unknown'}"
                )
            )
            return WorkflowActionResult(
                status="failed",
                error=error,
                outputs={
                    "subworkflow_authority_resolution": (
                        authority_resolution.to_projection()
                    )
                },
            )

        child_chain = [*invocation_chain, child_workflow_id]
        ambient_input_keys = _normalise_input_key_list(
            inputs.get(_AMBIENT_INPUT_KEYS_KEY)
        )
        child_inputs = _extract_child_inputs(
            inputs=inputs,
            parent_context=request.data,
            invocation_chain=child_chain,
            parent_workflow_id=parent_workflow_id,
            parent_state_id=parent_state_id,
        )
        _apply_child_launch_input_contract(
            child_workflow_id=child_workflow_id,
            definition=definition,
            child_inputs=child_inputs,
            ambient_input_keys=ambient_input_keys,
        )
        child_inputs["__workflow_subworkflow_invocation_count"] = invocation_count + 1
        request.data["__workflow_subworkflow_invocation_count"] = invocation_count + 1
        _note_subworkflow_invocation(
            request.data,
            child_workflow_id=child_workflow_id,
            parent_workflow_id=parent_workflow_id,
            parent_state_id=parent_state_id,
            invocation_index=invocation_count + 1,
            invocation_limit=invocation_limit,
            route="workflow_invoke_subworkflow",
        )
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
                "authority_resolution": authority_resolution.to_projection(),
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
            _execution_scope=request.execution_scope,
        )
        merge_nested_llm_calls_for_cost(request.data, child_result.data)

        invocation_event: Dict[str, Any] = {
            "parent_workflow_id": parent_workflow_id or None,
            "parent_state_id": parent_state_id or None,
            "child_workflow_id": child_workflow_id,
            "failure_mode": failure_mode,
            "invocation_chain": list(child_chain),
            "invocation_count": invocation_count + 1,
            "invocation_limit": invocation_limit,
            "authority_resolution": authority_resolution.to_projection(),
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
                "child_error": _normalise_text(child_result.error) or None,
                "invocation_count": invocation_count + 1,
                "invocation_limit": invocation_limit,
            },
        )

        child_result_payload = _compact_child_result_payload(child_result.data)
        child_invocations = _derive_child_step_invocations(
            child_result,
            child_workflow_id=child_workflow_id,
        )
        if child_invocations:
            child_result_payload["invocations"] = list(child_invocations)
        outputs: Dict[str, Any] = {
            "result": child_result_payload,
            "subworkflow_invocation": invocation_event,
            "subworkflow_result_envelope": (
                dict(child_result.result_envelope)
                if isinstance(child_result.result_envelope, Mapping)
                else None
            ),
        }
        if child_invocations:
            outputs["invocations"] = list(child_invocations)
        child_control_signal = normalise_control_signal(
            (child_result.result_envelope or {}).get("control_signal")
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

        failure_error = (
            child_result.error or child_result.final_state or "subworkflow_failed"
        )
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
    overwrite: bool = False,
) -> None:
    """Register the canonical subworkflow invocation action.

    Keep this action explicitly registered so runnability checks can reason over
    a known action ID instead of relying on fallback tool routing.
    """
    loader = definition_loader or load_workflow_definition_from_vontology
    spec = ActionSpec(
        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
        handler=_build_subworkflow_handler(
            registry=registry,
            definition_loader=loader,
        ),
        description="Invoke a child workflow using explicit subworkflow contracts.",
        required_tool_operation_class="workflow_execute",
    )
    if overwrite:
        registry.replace(spec)
    else:
        registry.register_if_absent(spec)


__all__ = ["register_subworkflow_actions"]
