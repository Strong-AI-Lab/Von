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
)
from ..subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE,
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
from ..tool_invocation_evidence import (
    derive_tool_invocation_records_from_step_envelopes,
)
from ..metadata_validation import LAST_METADATA_EVENT_KEY, WORKFLOW_METADATA_EVENTS_KEY
from ..plan_state_runtime import (
    LAST_WORKFLOW_COMPLETION_GATE_KEY,
    LAST_WORKFLOW_PLAN_STATE_EVENT_KEY,
    WORKFLOW_COMPLETION_GATE_KEY,
    WORKFLOW_PLAN_STATE_EVENTS_KEY,
    WORKFLOW_PLAN_STATE_KEY,
)
from ..trace_model import WorkflowExecutionTrace
from ..vontology_loader import load_workflow_definition_from_vontology
from ..definitions import (
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
)

_FAILURE_MODE_INPUT_KEYS: tuple[str, ...] = ("failure_mode", "__failure_mode")
_RESERVED_SUBWORKFLOW_INPUT_KEYS: set[str] = {
    "workflow_id",
    "__parent_workflow_id",
    "__parent_state_id",
    "__workflow_invocation_chain",
    "failure_mode",
    "__failure_mode",
    "inherit_parent_context",
    "max_transitions",
}
_INVOCATION_CHAIN_KEY = "__workflow_invocation_chain"
_MAX_SUBWORKFLOW_DEPTH_ENV = "VON_WORKFLOW_SUBWORKFLOW_MAX_DEPTH"
_DEFAULT_SUBWORKFLOW_DEPTH_LIMIT = 8
_MAX_SUBWORKFLOW_INVOCATIONS_ENV = "VON_WORKFLOW_SUBWORKFLOW_MAX_INVOCATIONS"
_DEFAULT_SUBWORKFLOW_INVOCATION_LIMIT = 64
_DEFAULT_MAX_TRANSITIONS = 40
_MAX_TRANSITIONS_LIMIT = 300
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


def _truthy_env_value(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_agent_test_instance() -> bool:
    return _truthy_env_value(os.getenv("VON_AGENT_TEST_INSTANCE"))


def _agent_test_workflow_experience_profile_concept_id(
    *,
    inputs: Mapping[str, Any],
    parent_context: Mapping[str, Any],
    request: WorkflowActionRequest,
) -> str:
    from ...utils.concept_id_utils import canonicalise_vontology_concept_id

    workflow_id = (
        _normalise_text(inputs.get("workflow_experience_target_workflow_id"))
        or _normalise_text(parent_context.get("workflow_experience_target_workflow_id"))
        or _normalise_text(parent_context.get("selected_workflow_id"))
        or _normalise_text(inputs.get("__parent_workflow_id"))
        or _normalise_text(parent_context.get("__parent_workflow_id"))
        or "unknown_workflow"
    )
    model_ref = (
        _normalise_text(inputs.get("workflow_experience_model_ref"))
        or _normalise_text(inputs.get("requested_model"))
        or _normalise_text(parent_context.get("workflow_experience_model_ref"))
        or _normalise_text(parent_context.get("requested_model"))
        or _normalise_text(getattr(request.environment, "model", None))
        or "unknown_model"
    )
    return (
        canonicalise_vontology_concept_id(
            f"Workflow LLM experience profile: {workflow_id} / {model_ref}"
        )
        or "#V#workflow_llm_experience_profile_unknown_workflow_unknown_model"
    )


def _build_agent_test_workflow_experience_context_result(
    *,
    request: WorkflowActionRequest,
    inputs: Mapping[str, Any],
    child_workflow_id: str,
    parent_workflow_id: str,
    parent_state_id: str,
    failure_mode: str,
    invocation_chain: Sequence[str],
    invocation_count: int,
    invocation_limit: int,
) -> WorkflowActionResult:
    child_chain = [*invocation_chain, child_workflow_id]
    request.data["__workflow_subworkflow_invocation_count"] = invocation_count + 1
    profile_concept_id = _agent_test_workflow_experience_profile_concept_id(
        inputs=inputs,
        parent_context=request.data,
        request=request,
    )
    child_result_payload: Dict[str, Any] = {
        "workflow_success_guidance_history": [],
        "workflow_failure_avoidance_history": [],
        "workflow_low_imposition_exploration_history": [],
        "workflow_experience_profile_concept_id": profile_concept_id,
        "workflow_experience_effective_workflow_id": (
            _normalise_text(inputs.get("workflow_experience_target_workflow_id"))
            or _normalise_text(request.data.get("selected_workflow_id"))
            or _normalise_text(parent_workflow_id)
            or "unknown_workflow"
        ),
        "workflow_experience_effective_model_ref": (
            _normalise_text(inputs.get("workflow_experience_model_ref"))
            or _normalise_text(inputs.get("requested_model"))
            or _normalise_text(request.data.get("requested_model"))
            or _normalise_text(getattr(request.environment, "model", None))
            or "unknown_model"
        ),
        "agent_test_guidance_skip_reason": "agent_test_instance",
    }
    invocation_event: Dict[str, Any] = {
        "parent_workflow_id": parent_workflow_id or None,
        "parent_state_id": parent_state_id or None,
        "child_workflow_id": child_workflow_id,
        "failure_mode": failure_mode,
        "invocation_chain": list(child_chain),
        "invocation_count": invocation_count + 1,
        "invocation_limit": invocation_limit,
        "child_completed": True,
        "child_final_state": "agent_test_skipped",
        "child_error": None,
        "skip_reason": "agent_test_instance",
    }
    _append_parent_trace_event(
        request_trace=request.trace,
        invocation_event=invocation_event,
    )
    increment_runtime_metric(context=request.data, key="subworkflow_invocations")
    append_runtime_event(
        context=request.data,
        event={
            "status": "subworkflow_skipped",
            "child_workflow_id": child_workflow_id,
            "child_completed": True,
            "child_final_state": "agent_test_skipped",
            "invocation_count": invocation_count + 1,
            "invocation_limit": invocation_limit,
            "reason_code": "agent_test_instance",
        },
    )
    return WorkflowActionResult(
        status="success",
        outputs={
            "result": child_result_payload,
            "subworkflow_invocation": invocation_event,
            "subworkflow_result_envelope": {
                "workflow_id": child_workflow_id,
                "completed": True,
                "final_state": "agent_test_skipped",
                "agent_test_bypass": True,
            },
        },
    )


def _model_identifier_looks_local_ollama(model: Any) -> bool:
    if not isinstance(model, str):
        return False
    cleaned = model.strip().lower().replace(": ", ":")
    if not cleaned:
        return False
    if cleaned.startswith("ollama:"):
        return True
    if cleaned.startswith(("openai:", "anthropic:", "gemini:", "azure_openai:")):
        return False
    if cleaned.startswith(("gpt-", "o1-", "claude", "gemini")):
        return False
    return ":" in cleaned


def _agent_test_request_uses_local_model(request: WorkflowActionRequest) -> bool:
    return _model_identifier_looks_local_ollama(
        _normalise_text(getattr(request.environment, "model", None))
        or _normalise_text(request.data.get("requested_model"))
    )


def _agent_test_relation_prompt_context(parent_context: Mapping[str, Any]) -> bool:
    prompt_text = " ".join(
        _normalise_text(value)
        for value in (
            parent_context.get("user_prompt"),
            parent_context.get("prompt_for_requirements"),
            parent_context.get("prompt"),
        )
        if _normalise_text(value)
    ).lower()
    if any(
        marker in prompt_text
        for marker in (
            "text relation",
            "text relations",
            "represented relation",
            "represented relations",
        )
    ):
        return True
    required_tools = parent_context.get("required_prompt_tools")
    if not isinstance(required_tools, Sequence) or isinstance(
        required_tools,
        (str, bytes, bytearray),
    ):
        return False
    return "get_text_relations_summary" in {
        _normalise_text(tool_name) for tool_name in required_tools
    }


def _build_agent_test_postcondition_critic_result(
    *,
    request: WorkflowActionRequest,
    inputs: Mapping[str, Any],
    child_workflow_id: str,
    parent_workflow_id: str,
    parent_state_id: str,
    failure_mode: str,
    invocation_chain: Sequence[str],
    invocation_count: int,
    invocation_limit: int,
) -> WorkflowActionResult:
    from .turn_execution_runtime_support import run_turn_execution_critic

    child_chain = [*invocation_chain, child_workflow_id]
    request.data["__workflow_subworkflow_invocation_count"] = invocation_count + 1
    critic_result = run_turn_execution_critic(
        request,
        annotation_component="workflow_subworkflow_agent_test",
        annotation_function="_build_agent_test_postcondition_critic_result",
    )
    if critic_result.status == "failed":
        return critic_result
    child_result_payload: Dict[str, Any] = {
        str(key): value
        for key, value in critic_result.outputs.items()
        if isinstance(key, str)
    }
    child_result_payload["agent_test_critic_skip_reason"] = "agent_test_instance"
    invocation_event: Dict[str, Any] = {
        "parent_workflow_id": parent_workflow_id or None,
        "parent_state_id": parent_state_id or None,
        "child_workflow_id": child_workflow_id,
        "failure_mode": failure_mode,
        "invocation_chain": list(child_chain),
        "invocation_count": invocation_count + 1,
        "invocation_limit": invocation_limit,
        "child_completed": True,
        "child_final_state": "agent_test_deterministic_critic",
        "child_error": None,
        "skip_reason": "agent_test_instance",
    }
    _append_parent_trace_event(
        request_trace=request.trace,
        invocation_event=invocation_event,
    )
    increment_runtime_metric(context=request.data, key="subworkflow_invocations")
    append_runtime_event(
        context=request.data,
        event={
            "status": "subworkflow_skipped",
            "child_workflow_id": child_workflow_id,
            "child_completed": True,
            "child_final_state": "agent_test_deterministic_critic",
            "invocation_count": invocation_count + 1,
            "invocation_limit": invocation_limit,
            "reason_code": "agent_test_instance",
        },
    )
    return WorkflowActionResult(
        status="success",
        outputs={
            "result": child_result_payload,
            "subworkflow_invocation": invocation_event,
            "subworkflow_result_envelope": {
                "workflow_id": child_workflow_id,
                "completed": True,
                "final_state": "agent_test_deterministic_critic",
                "agent_test_bypass": True,
            },
        },
    )


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


def _compact_child_result_payload(child_data: Mapping[str, Any] | None) -> Dict[str, Any]:
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

        if (
            child_workflow_id == WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID
            and _is_agent_test_instance()
        ):
            return _build_agent_test_workflow_experience_context_result(
                request=request,
                inputs=inputs,
                child_workflow_id=child_workflow_id,
                parent_workflow_id=parent_workflow_id,
                parent_state_id=parent_state_id,
                failure_mode=failure_mode,
                invocation_chain=invocation_chain,
                invocation_count=invocation_count,
                invocation_limit=invocation_limit,
            )

        if (
            child_workflow_id == KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID
            and _is_agent_test_instance()
            and _agent_test_request_uses_local_model(request)
            and _agent_test_relation_prompt_context(request.data)
        ):
            return _build_agent_test_postcondition_critic_result(
                request=request,
                inputs=inputs,
                child_workflow_id=child_workflow_id,
                parent_workflow_id=parent_workflow_id,
                parent_state_id=parent_state_id,
                failure_mode=failure_mode,
                invocation_chain=invocation_chain,
                invocation_count=invocation_count,
                invocation_limit=invocation_limit,
            )

        definition = definition_loader(child_workflow_id)
        if definition is None:
            try:
                from ...services.namespace_service import (
                    derive_actor_context_from_namespace,
                )
                from .registry_factory import resolve_workflow_definition_from_authority

                namespace_user, namespace_org = derive_actor_context_from_namespace(
                    request.environment.user_namespace
                )
                actor_user_id = request.environment.user_concept_id or namespace_user
                actor_org_id = request.environment.org_concept_id or namespace_org
                resolution = resolve_workflow_definition_from_authority(
                    child_workflow_id,
                    registry=None,
                    use_current_shared_registry=True,
                    register_authoritative_fallback=True,
                    actor_user_id=actor_user_id,
                    actor_org_id=actor_org_id,
                )
                definition = resolution.definition
            except Exception:
                definition = None
        if definition is None:
            return WorkflowActionResult(
                status="failed",
                error=f"subworkflow_definition_not_found:{child_workflow_id}",
            )

        child_chain = [*invocation_chain, child_workflow_id]
        child_inputs = _extract_child_inputs(
            inputs=inputs,
            parent_context=request.data,
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
    )
    if overwrite:
        registry.replace(spec)
    else:
        registry.register_if_absent(spec)


__all__ = ["register_subworkflow_actions"]
