"""Generic prompt-driven LLM step execution for VWL.

This module makes prompt-bearing workflow steps executable without requiring a
bespoke Python handler per reasoning step. Deterministic tool execution and
write-policy enforcement are delegated to the existing internal MCP
orchestrator machinery when a gateway is available.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    cast,
)

from ..services.buttonify_service import (
    parse_buttonify_options_json,
    sanitise_buttonify_options,
)
from ..services.agent_test_replay_mode_service import (
    use_represented_selector_llm_for_agent_test_replay,
)
from ..services.prompt_template_service import PromptTemplateService
from ..services.required_tool_obligation_service import (
    OPERATION_MUTATION_WRITE,
    build_required_tool_obligation_ledger,
    classify_required_tool_operation,
)
from ..services.required_tool_identity_service import (
    canonical_required_tool_key,
    canonical_required_tool_keys,
)
from ..services.tool_target_contract_validation import (
    target_contract_state_from_context,
)
from ..services.workflow_llm_duration_stats_service import (
    record_workflow_llm_step_duration_observation,
)
from ..languagemodels.llm_interface import infer_llm_client_provider
from ..languagemodels.structured_tool_calling import StructuredToolTransportError
from .llm_call_telemetry import stamp_llm_call_timestamps
from .prompt_metadata_resolution import resolve_model_prompt_variant
from .recovery_prompt_compaction import (
    RECOVERY_CONTEXT_FIELD_CHAR_BUDGET,
    RECOVERY_CONTEXT_TOTAL_CHAR_BUDGET,
    TOTAL_TRUNCATION_MARKER,
    compact_recovery_context_field,
    is_recovery_decision_state,
    render_compacted_recovery_field_text,
)
from .turn_expected_outcome_contract import TurnExpectedOutcomeContract
from .definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)
from .conversation_turn_llm_timeout import (
    coerce_conversation_turn_llm_timeout_sec,
    default_conversation_turn_llm_timeout_sec,
)
from .action_registry import WorkflowActionRequest, WorkflowActionResult

_SPOKEN_BLOCK_RE = re.compile(
    r"<spoken>\s*(.*?)\s*</spoken>",
    flags=re.DOTALL | re.IGNORECASE,
)
_CONVERSATION_TURN_LLM_TELEMETRY_STATES = frozenset(
    {
        "context_adjudication",
        "context_adjudication_decision",
        "expected_outcome_inference",
        "selector_decision",
        "tool_plan",
        "tool_planning",
        "recovery_decision",
    }
)
_CONVERSATION_TURN_LLM_TIMEOUT_CONTEXT_KEY = (
    "conversation_turn_llm_timeout_override_sec"
)
_DEFAULT_WORKFLOW_TOOL_INVOCATION_CAP = 4
_COMPLETION_REPORT_NARRATION_PROMPT_IDS = frozenset(
    {"#V#prompt_turn_execution_narrate_completion_report"}
)
_TURN_EXPECTED_OUTCOME_INFERENCE_PROMPT_ID = (
    "#V#prompt_turn_execution_expected_outcome_inference"
)
_AVAILABLE_INTERNAL_TOOL_IDS_CONTEXT_KEY = "available_internal_tool_ids"
_AGENT_TEST_LOCAL_PROVIDER_NAME = "ollama"
_AGENT_TEST_GENERIC_SELECTOR_WORKFLOW_IDS = frozenset(
    {
        CHAT_ASSISTANT_WORKFLOW_ID,
        CHAT_NARRATION_WORKFLOW_ID,
        TOOL_CALLING_WORKFLOW_ID,
    }
)


def _context_string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return str(value or "").strip()


def _hydrate_authored_runtime_context_fields(
    *,
    request: WorkflowActionRequest,
    llm_policy: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Hydrate runtime-owned values only when the workflow explicitly requests them."""

    raw_context_fields = llm_policy.get("context_fields")
    if not isinstance(raw_context_fields, Sequence) or isinstance(
        raw_context_fields,
        (str, bytes, bytearray),
    ):
        return None
    authored_context_keys = {
        _context_string(item.get("context_key"))
        for item in raw_context_fields
        if isinstance(item, Mapping)
    }
    if _AVAILABLE_INTERNAL_TOOL_IDS_CONTEXT_KEY not in authored_context_keys:
        return None

    gateway = request.environment.gateway
    try:
        method_catalogue = gateway.describe_methods() if gateway is not None else None
    except Exception as exc:
        request.data[_AVAILABLE_INTERNAL_TOOL_IDS_CONTEXT_KEY] = {
            "status": "unavailable",
            "tool_ids": [],
        }
        return {
            "context_key": _AVAILABLE_INTERNAL_TOOL_IDS_CONTEXT_KEY,
            "source": "workflow_environment.gateway.describe_methods",
            "status": "unavailable",
            "tool_count": 0,
            "error_class": type(exc).__name__,
        }

    tool_ids = sorted(
        {
            str(tool_name).strip()
            for tool_name in (
                method_catalogue.keys()
                if isinstance(method_catalogue, Mapping)
                else ()
            )
            if str(tool_name).strip()
        }
    )
    status = "available" if isinstance(method_catalogue, Mapping) else "unavailable"
    request.data[_AVAILABLE_INTERNAL_TOOL_IDS_CONTEXT_KEY] = {
        "status": status,
        "tool_ids": tool_ids,
    }
    return {
        "context_key": _AVAILABLE_INTERNAL_TOOL_IDS_CONTEXT_KEY,
        "source": "workflow_environment.gateway.describe_methods",
        "status": status,
        "tool_count": len(tool_ids),
    }


def _truthy_env_value(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_agent_test_instance() -> bool:
    return _truthy_env_value(os.getenv("VON_AGENT_TEST_INSTANCE"))


def _split_model_provider_prefix(model: str | None) -> tuple[str | None, str | None]:
    text = _context_string(model)
    if not text or "/" not in text:
        return None, text or None
    provider, model_name = text.split("/", 1)
    provider = provider.strip().lower() or None
    model_name = model_name.strip() or None
    return provider, model_name


def _request_uses_agent_test_local_model(request: WorkflowActionRequest) -> bool:
    if not _is_agent_test_instance():
        return False
    provider = _context_string(
        request.data.get("selected_model_provider")
        or request.data.get("requested_client_type")
        or request.data.get("model_provider")
    ).lower()
    if provider:
        return provider == _AGENT_TEST_LOCAL_PROVIDER_NAME
    requested_model = _context_string(
        request.data.get("requested_model")
        or getattr(request.environment, "model", None)
    )
    model_provider, _model_name = _split_model_provider_prefix(requested_model)
    if model_provider:
        return model_provider == _AGENT_TEST_LOCAL_PROVIDER_NAME
    inferred_provider = _context_string(
        infer_llm_client_provider(getattr(request.environment, "llm_client", None))
    ).lower()
    if inferred_provider:
        return inferred_provider == _AGENT_TEST_LOCAL_PROVIDER_NAME
    # A bare model ID is ambiguous: active hosted models such as
    # ``gpt-5.6-luna`` do not carry a provider prefix. Faithful AgentTest replay
    # must call the configured model unless locality can be resolved.
    return False


def _required_tools_from_contract_payload(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    required_tools = value.get("required_tools")
    if required_tools is None:
        fields = value.get("fields")
        if isinstance(fields, Mapping):
            required_tools = fields.get("required_tools")
    return _coerce_tool_name_list(required_tools)


def _agent_test_required_tools(request: WorkflowActionRequest) -> list[str]:
    """Return explicit AgentTest replay authority, never prompt-derived policy."""

    return _merge_required_prompt_tools(
        request.data.get("agent_test_replay_required_tools"),
        request.data.get("agent_test_required_tools"),
        request.data.get("turn_expected_required_tools"),
        request.data.get("required_prompt_tools"),
        _required_tools_from_contract_payload(
            request.data.get("turn_expected_outcome_contract")
        ),
        _required_tools_from_contract_payload(
            request.data.get("turn_expected_outcome_profile")
        ),
        _required_tools_from_contract_payload(
            request.data.get("turn_expected_outcome_contract_state")
        ),
    )


def _agent_test_selector_candidate_ids(request: WorkflowActionRequest) -> list[str]:
    raw_candidate_ids = request.data.get("selector_candidate_ids")
    if not isinstance(raw_candidate_ids, Sequence) or isinstance(
        raw_candidate_ids,
        (str, bytes, bytearray),
    ):
        return []
    return [
        str(item).strip()
        for item in raw_candidate_ids
        if isinstance(item, str) and str(item).strip()
    ]


def _agent_test_selector_authoritative_candidate_ids(
    request: WorkflowActionRequest,
) -> list[str]:
    raw_candidate_ids = request.data.get("selector_authoritative_candidate_ids")
    if isinstance(raw_candidate_ids, Sequence) and not isinstance(
        raw_candidate_ids,
        (str, bytes, bytearray),
    ):
        cleaned = [
            str(item).strip()
            for item in raw_candidate_ids
            if isinstance(item, str) and str(item).strip()
        ]
        if cleaned:
            return cleaned

    raw_discovery = request.data.get("workflow_discovery_result")
    if not isinstance(raw_discovery, Mapping):
        raw_discovery = request.data.get("workflow_discovery")
    if not isinstance(raw_discovery, Mapping):
        return []

    candidate_ids: list[str] = []
    seen: set[str] = set()
    for key in ("matches", "routing_matches", "candidates"):
        raw_entries = raw_discovery.get(key)
        if not isinstance(raw_entries, Sequence) or isinstance(
            raw_entries,
            (str, bytes, bytearray),
        ):
            continue
        for entry in raw_entries:
            if not isinstance(entry, Mapping):
                continue
            concept_id = _context_string(
                entry.get("concept_id") or entry.get("workflow_id") or entry.get("id")
            )
            if not concept_id:
                continue
            if concept_id in _AGENT_TEST_GENERIC_SELECTOR_WORKFLOW_IDS:
                continue
            if entry.get("routing_eligible") is False:
                continue
            if entry.get("is_executable") is False:
                continue
            if entry.get("is_policy_safe") is False:
                continue
            dedupe_key = concept_id.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            candidate_ids.append(concept_id)
    return candidate_ids


def _build_agent_test_expected_outcome_response(
    request: WorkflowActionRequest,
) -> str:
    required_tools = _agent_test_required_tools(request)
    user_prompt = _context_string(
        request.data.get("user_prompt") or request.data.get("prompt")
    )
    if required_tools:
        selector_guidance = (
            "Prefer the most specific represented workflow already prepared "
            "for the selector; use the generic tool-calling workflow only "
            "when no specialised represented workflow is eligible."
        )
        if "get_text_relations_summary" in required_tools:
            summary = "Answer the represented-relation request using grounded Vontology tool evidence."
            answering_guidance = "Report the text relation predicates and counts found by the relation-summary tool."
            grounding_requirement = (
                "Use authoritative Vontology text relation summary evidence."
            )
        else:
            summary = "Answer the user's request using grounded evidence from the requested external surfaces."
            answering_guidance = "Use the required tools before finalising; report unavailable evidence clearly."
            grounding_requirement = "Use the requested external evidence tools; avoid answering from unsupported memory."
    else:
        summary = "Answer the user's request accurately using available context and grounded evidence."
        selector_guidance = (
            "Choose the route most likely to answer the request with grounded evidence."
        )
        answering_guidance = "Answer directly only when sufficiently grounded; otherwise use tools or state what evidence is missing."
        grounding_requirement = "Use available conversation context and authoritative tool evidence; avoid unsupported claims."
    payload = {
        "expected_outcome_summary": summary,
        "summary": summary,
        "grounding_requirement": grounding_requirement,
        "precision_policy": "Prefer a concise answer with explicit uncertainty over unsupported specificity.",
        "selector_guidance": selector_guidance,
        "answering_guidance": answering_guidance,
        "reasoning": (
            "AgentTest explicit local replay uses deterministic expectation defaults "
            "for wrapper planning so repeated local runs spend model time on the "
            "selected workflow and tool evidence."
        ),
        "required_tools": required_tools,
    }
    if user_prompt:
        payload["user_prompt"] = user_prompt
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _build_agent_test_selector_response(request: WorkflowActionRequest) -> str:
    required_tools = _merge_required_prompt_tools(
        request.data.get("required_prompt_tools"),
        request.data.get("turn_expected_required_tools"),
        _agent_test_required_tools(request),
    )
    authoritative_candidate_ids = _agent_test_selector_authoritative_candidate_ids(
        request
    )
    candidate_ids = _agent_test_selector_candidate_ids(request)
    non_generic_candidate_id = next(
        (
            candidate_id
            for candidate_id in candidate_ids
            if candidate_id not in _AGENT_TEST_GENERIC_SELECTOR_WORKFLOW_IDS
        ),
        None,
    )
    if authoritative_candidate_ids:
        workflow_id = authoritative_candidate_ids[0]
        reasoning = (
            "AgentTest explicit local replay reused the first authoritative "
            "workflow-discovery candidate so represented workflow discovery "
            "remains authoritative over selector policy ordering."
        )
    elif non_generic_candidate_id:
        workflow_id = non_generic_candidate_id
        reasoning = (
            "AgentTest explicit local replay reused the first non-generic "
            "selector-prepared candidate."
        )
    elif required_tools:
        workflow_id = TOOL_CALLING_WORKFLOW_ID
        reasoning = (
            "AgentTest explicit local replay selected the tool-calling route "
            "because the turn requires grounded tool evidence."
        )
    else:
        workflow_id = CHAT_ASSISTANT_WORKFLOW_ID
        reasoning = "AgentTest explicit local replay selected the default chat route."
    payload = {
        "workflow_id": workflow_id,
        "confidence": 1.0,
        "reasoning": reasoning,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _build_agent_test_narration_response(request: WorkflowActionRequest) -> str:
    for key in (
        "selected_workflow_user_response",
        "response_text",
        "final_response",
        "current_response",
    ):
        value = _context_string(request.data.get(key))
        if value:
            return value
    completion_report = request.data.get("completion_report")
    if isinstance(completion_report, Mapping):
        response_preview = _context_string(completion_report.get("response_preview"))
        if response_preview:
            return response_preview
    return "The selected workflow completed without a user-visible response."


def _agent_test_conversation_turn_fast_path_result(
    *,
    request: WorkflowActionRequest,
    stage: str,
    llm_policy_map: Mapping[str, Any],
    validation_policy_map: Mapping[str, Any],
) -> WorkflowActionResult | None:
    if not _request_uses_agent_test_local_model(request):
        return None
    if _context_string(request.workflow_id) != CONVERSATION_TURN_EXECUTION_WORKFLOW_ID:
        return None

    state_id = _context_string(request.workflow_state_id).lower()
    prompt_id: str | None = None
    response_text: str | None = None
    if state_id == "expected_outcome_inference":
        prompt_id = _TURN_EXPECTED_OUTCOME_INFERENCE_PROMPT_ID
        if use_represented_selector_llm_for_agent_test_replay(
            request.data
        ) and not _agent_test_required_tools(request):
            return None
        response_text = _build_agent_test_expected_outcome_response(request)
    elif state_id == "selector_decision":
        if use_represented_selector_llm_for_agent_test_replay(request.data):
            return None
        prompt_id = _context_string(request.data.get("selector_prompt_id")) or None
        response_text = _build_agent_test_selector_response(request)
    elif state_id == "narration":
        prompt_id = next(iter(_COMPLETION_REPORT_NARRATION_PROMPT_IDS))
        response_text = _build_agent_test_narration_response(request)
    if response_text is None:
        return None

    selected_model = (
        _context_string(
            request.data.get("requested_model")
            or getattr(request.environment, "model", None)
        )
        or None
    )
    selected_candidate = {
        "provider": _AGENT_TEST_LOCAL_PROVIDER_NAME,
        "locality": "local",
    }
    llm_call = {
        "type": "llm.generate_skipped",
        "stage": stage,
        "model_name": selected_model,
        "provider": _AGENT_TEST_LOCAL_PROVIDER_NAME,
        "duration_ms": 0.0,
        "note": "agent_test_explicit_local_replay_fast_path",
        "workflow_stage_id": request.workflow_state_id,
    }
    aux_call = {
        "type": "workflow_llm_step_skip",
        "stage": stage,
        "workflow_stage_id": request.workflow_state_id,
        "status": "skipped",
        "reason_code": "agent_test_explicit_local_replay_fast_path",
    }
    prior_llm_calls = request.data.get("llm_calls")
    llm_calls = (
        [
            {str(key): value for key, value in item.items() if isinstance(key, str)}
            for item in prior_llm_calls
            if isinstance(item, Mapping)
        ]
        if isinstance(prior_llm_calls, list)
        else []
    )
    llm_calls.append(llm_call)
    prior_aux_llm_calls = request.data.get("aux_llm_calls")
    aux_llm_calls = (
        [
            {str(key): value for key, value in item.items() if isinstance(key, str)}
            for item in prior_aux_llm_calls
            if isinstance(item, Mapping)
        ]
        if isinstance(prior_aux_llm_calls, list)
        else []
    )
    aux_llm_calls.append(aux_call)
    return _build_result(
        request=request,
        response_text=response_text,
        prompt_id=prompt_id,
        prompt_source="agent_test_fast_path",
        rendered_variables={"agent_test_fast_path": True},
        llm_policy_map=llm_policy_map,
        validation_policy_map=validation_policy_map,
        selected_model=selected_model,
        selected_candidate=selected_candidate,
        tool_invocations=(),
        tool_messages=(),
        llm_calls=llm_calls,
        aux_llm_calls=aux_llm_calls,
        prompt_variant_selection={
            "source": "agent_test_fast_path",
            "reason": "agent_test_explicit_local_replay",
        },
    )


def _serialise_prompt_context_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)
    except Exception:
        return str(value)


def _coerce_prompt_id_list(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    items: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if cleaned:
            items.append(cleaned)
    return items


def _coerce_tool_name_list(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    items: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if cleaned:
            items.append(cleaned)
    return items


def _tool_call_validation_failure_context_from_data(
    data: Mapping[str, Any],
) -> dict[str, Any] | None:
    failures_by_tool: dict[str, dict[str, Any]] = {}

    def add_error(raw_error: Mapping[str, Any]) -> None:
        tool_name = (
            _context_string(raw_error.get("tool"))
            or _context_string(raw_error.get("planned_tool"))
            or _context_string(raw_error.get("method"))
            or _context_string(raw_error.get("name"))
        )
        if not tool_name:
            return
        key = tool_name.lower()
        failure = failures_by_tool.setdefault(
            key,
            {"tool": tool_name, "errors": []},
        )
        errors = failure.setdefault("errors", [])
        if isinstance(errors, list):
            errors.append(
                {
                    str(error_key): error_value
                    for error_key, error_value in raw_error.items()
                    if isinstance(error_key, str)
                }
            )

    for field_name in (
        "tool_call_validation_required_tool_errors",
        "tool_call_validation_diagnostics",
    ):
        raw_errors = data.get(field_name)
        if not isinstance(raw_errors, Sequence) or isinstance(
            raw_errors, (str, bytes, bytearray)
        ):
            continue
        for raw_error in raw_errors:
            if isinstance(raw_error, Mapping):
                add_error(raw_error)

    if not failures_by_tool:
        return None

    repair_decision = data.get("tool_call_repair_decision")
    return {
        "failures_by_tool": failures_by_tool,
        "failed_tools": _coerce_tool_name_list(
            data.get("tool_call_validation_required_failed_tools")
            or data.get("tool_call_validation_failed_tools")
            or []
        ),
        "repair_outcome": _context_string(data.get("tool_call_repair_outcome")),
        "repair_stop_reason": _context_string(data.get("tool_call_repair_stop_reason")),
        "repair_decision": (
            dict(repair_decision) if isinstance(repair_decision, Mapping) else None
        ),
    }


def _filter_tool_names_to_allowed_set(
    tool_names: Sequence[str],
    allowed_tools: Sequence[str] | None,
) -> list[str]:
    if allowed_tools is None:
        return [
            str(tool_name).strip()
            for tool_name in tool_names
            if isinstance(tool_name, str) and str(tool_name).strip()
        ]
    allowed_names = [
        str(tool_name).strip()
        for tool_name in allowed_tools
        if isinstance(tool_name, str) and str(tool_name).strip()
    ]
    known_names = [*allowed_names, *tool_names]
    allowed = canonical_required_tool_keys(
        allowed_names,
        known_tool_names=known_names,
    )
    filtered: list[str] = []
    seen: set[str] = set()
    for raw_tool_name in tool_names:
        if not isinstance(raw_tool_name, str):
            continue
        tool_name = raw_tool_name.strip()
        canonical_key = canonical_required_tool_key(
            tool_name,
            known_tool_names=known_names,
        )
        if (
            not tool_name
            or not canonical_key
            or canonical_key in seen
            or canonical_key not in allowed
        ):
            continue
        seen.add(canonical_key)
        filtered.append(tool_name)
    return filtered


def _merge_required_prompt_tools(
    *tool_sets: Any,
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for tool_set in tool_sets:
        for tool_name in _coerce_tool_name_list(tool_set):
            canonical_key = canonical_required_tool_key(tool_name)
            if not canonical_key or canonical_key in seen:
                continue
            seen.add(canonical_key)
            merged.append(tool_name)
    return merged


def _normalise_tool_argument_defaults(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    normalised: dict[str, dict[str, Any]] = {}
    for raw_tool_name, raw_defaults in value.items():
        tool_name = _context_string(raw_tool_name)
        if not tool_name or not isinstance(raw_defaults, Mapping):
            continue
        clean_defaults: dict[str, Any] = {}
        for raw_key, raw_item in raw_defaults.items():
            key = _context_string(raw_key)
            if not key:
                continue
            clean_defaults[key] = raw_item
        if clean_defaults:
            normalised[tool_name] = clean_defaults
    return normalised


def _resolve_turn_expected_outcome_contract(
    context: Mapping[str, Any],
) -> TurnExpectedOutcomeContract:
    return TurnExpectedOutcomeContract.merge_preferred(
        context.get("turn_expected_outcome_contract_state"),
        context.get("turn_expected_outcome_contract"),
        context.get("expected_outcome_contract_state"),
        context.get("expected_outcome_contract"),
    )


def _resolve_llm_step_max_tool_invocations(
    *,
    request: WorkflowActionRequest,
    llm_policy: Mapping[str, Any],
    required_prompt_tools: Sequence[str],
    required_obligation_tools: Sequence[str] | None = None,
) -> int:
    env_max_tool_invocations = request.environment.max_tool_invocations

    def _apply_environment_cap(resolved_limit: int) -> int:
        """Keep the global runtime setting as a ceiling, not authored policy."""

        if env_max_tool_invocations is None:
            return max(0, int(resolved_limit))
        try:
            environment_cap = max(0, int(env_max_tool_invocations))
        except (TypeError, ValueError):
            return max(0, int(resolved_limit))
        return min(max(0, int(resolved_limit)), environment_cap)

    raw_policy_limit = llm_policy.get("max_tool_invocations")
    if isinstance(raw_policy_limit, bool):
        policy_limit = int(raw_policy_limit)
    elif isinstance(raw_policy_limit, (int, float, str)):
        try:
            policy_limit = int(raw_policy_limit)
        except ValueError:
            policy_limit = 0
    else:
        policy_limit = 0
    if policy_limit > 0:
        return _apply_environment_cap(policy_limit)

    required_tools = _merge_required_prompt_tools(
        required_prompt_tools,
        required_obligation_tools or (),
    )
    if required_tools:
        operation_classes = [
            classify_required_tool_operation(tool_name) for tool_name in required_tools
        ]
        required_budget = len(required_tools)
        if OPERATION_MUTATION_WRITE in operation_classes:
            # KR write contracts need room for resolution/search, mutation, and
            # read-back.  The authored contract decides which tools are required;
            # this support surface only avoids starving those obligations.
            required_budget += 2
        return _apply_environment_cap(
            max(_DEFAULT_WORKFLOW_TOOL_INVOCATION_CAP, required_budget)
        )

    return _apply_environment_cap(1)


def _coerce_context_messages(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        role = _context_string(item.get("role")) or "user"
        content = _context_string(item.get("content"))
        if not content:
            continue
        rows.append({"role": role, "content": content})
    return rows


def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _context_message_chars(messages: Sequence[Mapping[str, str]]) -> int:
    total = 0
    for item in messages:
        total += len(_context_string(item.get("role")))
        total += len(_context_string(item.get("content")))
    return total


def _bounded_context_messages_for_policy(
    *,
    raw_value: Any,
    llm_policy_map: Mapping[str, Any],
    context_messages_key: str,
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    messages = _coerce_context_messages(raw_value)
    raw_chars = _context_message_chars(messages)
    max_chars = _coerce_positive_int(
        llm_policy_map.get("context_messages_max_chars")
        or llm_policy_map.get("context_messages_char_budget")
    )
    strategy = (
        _context_string(llm_policy_map.get("context_messages_truncation_strategy"))
        or "tail"
    ).lower()
    if strategy not in {"head", "tail"}:
        strategy = "tail"

    diagnostics: dict[str, Any] = {
        "schema_version": "llm_context_messages_diagnostics.v1",
        "context_messages_context_key": context_messages_key or None,
        "raw_message_count": len(messages),
        "raw_chars": raw_chars,
        "max_chars": max_chars,
        "truncation_strategy": strategy,
        "truncated": False,
        "rendered_message_count": len(messages),
        "rendered_chars": raw_chars,
        "omitted_message_count": 0,
        "omitted_chars": 0,
    }
    if not max_chars or raw_chars <= max_chars:
        return messages, diagnostics

    selected_reversed: list[dict[str, str]] = []
    remaining = max_chars
    source = list(messages if strategy == "head" else reversed(messages))
    truncated_single_message = False
    for message in source:
        role = _context_string(message.get("role")) or "user"
        content = _context_string(message.get("content"))
        message_chars = len(role) + len(content)
        if message_chars <= remaining:
            selected_reversed.append({"role": role, "content": content})
            remaining -= message_chars
            continue
        if remaining > len(role):
            content_budget = max(0, remaining - len(role))
            if content_budget:
                content = (
                    content[:content_budget]
                    if strategy == "head"
                    else content[-content_budget:]
                )
                selected_reversed.append({"role": role, "content": content})
                truncated_single_message = True
        break

    selected = (
        selected_reversed if strategy == "head" else list(reversed(selected_reversed))
    )
    rendered_chars = _context_message_chars(selected)
    diagnostics.update(
        {
            "truncated": True,
            "rendered_message_count": len(selected),
            "rendered_chars": rendered_chars,
            "omitted_message_count": max(0, len(messages) - len(selected)),
            "omitted_chars": max(0, raw_chars - rendered_chars),
            "single_message_truncated": truncated_single_message,
        }
    )
    return selected, diagnostics


def _merge_prompt_context_diagnostics(
    existing: Mapping[str, Any] | None,
    *,
    context_messages_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    merged = dict(existing) if isinstance(existing, Mapping) else {}
    if context_messages_diagnostics:
        merged["schema_version"] = "llm_prompt_context_diagnostics.v1"
        merged["context_messages"] = dict(context_messages_diagnostics)
    return merged


def _coerce_context_telemetry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _conversation_turn_llm_phase_override(
    request: WorkflowActionRequest,
) -> str | None:
    workflow_state_id = _context_string(request.workflow_state_id)
    state_alias = _conversation_turn_llm_state_alias(workflow_state_id)
    if state_alias:
        return state_alias
    return None


def _conversation_turn_llm_state_alias(value: Any) -> str | None:
    workflow_state_id = _context_string(value)
    if not workflow_state_id:
        return None
    if workflow_state_id in _CONVERSATION_TURN_LLM_TELEMETRY_STATES:
        return workflow_state_id
    for state_id in _CONVERSATION_TURN_LLM_TELEMETRY_STATES:
        if workflow_state_id.endswith(state_id):
            return state_id
    return None


def _coerce_llm_timeout_override_sec(raw_timeout: Any) -> float | None:
    return coerce_conversation_turn_llm_timeout_sec(raw_timeout)


def _conversation_turn_llm_timeout_override_sec(
    request: WorkflowActionRequest,
) -> float | None:
    explicit_override = (
        _coerce_llm_timeout_override_sec(
            request.data.get(_CONVERSATION_TURN_LLM_TIMEOUT_CONTEXT_KEY)
        )
        if isinstance(request.data, Mapping)
        else None
    )
    if explicit_override is not None:
        return explicit_override

    workflow_state_id = _context_string(request.workflow_state_id)
    if _conversation_turn_llm_state_alias(workflow_state_id) is None:
        return None

    return default_conversation_turn_llm_timeout_sec(
        os.getenv("VON_CONVERSATION_TURN_LLM_TIMEOUT_SEC")
    )


def _conversation_turn_llm_fallback_guard_timeout_sec(
    timeout_seconds: float | None,
) -> float | None:
    """Return an outer guard that leaves room for gateway fallback handling."""

    if timeout_seconds is None:
        return timeout_seconds
    if timeout_seconds < 30.0:
        return timeout_seconds + 1.0
    return timeout_seconds + max(30.0, min(90.0, timeout_seconds * 0.75))


def _conversation_turn_llm_step_guard_timeout_sec(
    timeout_seconds: float | None,
) -> float | None:
    """Return a whole-step guard that leaves setup plus one LLM call budget."""

    if timeout_seconds is None:
        return None
    if timeout_seconds < 30.0:
        return timeout_seconds + 1.0
    return timeout_seconds + max(180.0, min(240.0, timeout_seconds * 1.5))


def _missing_prompt_tools_for_completion_report_narration(
    request: WorkflowActionRequest,
    *,
    prompt_id: str | None,
) -> list[str]:
    workflow_state_id = _context_string(request.workflow_state_id)
    clean_prompt_id = _context_string(prompt_id)
    if (
        workflow_state_id != "narration"
        and clean_prompt_id not in _COMPLETION_REPORT_NARRATION_PROMPT_IDS
    ):
        return []

    missing_tools: list[str] = []
    seen: set[str] = set()
    for source in (
        request.data,
        request.data.get("completion_report"),
        request.data.get("selected_workflow_trace"),
    ):
        if not isinstance(source, Mapping):
            continue
        for tool_name in _coerce_tool_name_list(source.get("missing_prompt_tools")):
            lowered = tool_name.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            missing_tools.append(tool_name)
    return missing_tools


def _build_skipped_completion_report_narration_result(
    *,
    request: WorkflowActionRequest,
    stage: str,
    prompt_id: str | None,
    prompt_source: str | None,
    rendered_variables: Mapping[str, Any],
    llm_policy_map: Mapping[str, Any],
    validation_policy_map: Mapping[str, Any],
    missing_prompt_tools: Sequence[str],
) -> WorkflowActionResult:
    raw_aux_llm_calls = request.data.get("aux_llm_calls")
    aux_llm_calls: list[Mapping[str, Any]] = (
        [
            {str(key): value for key, value in item.items() if isinstance(key, str)}
            for item in raw_aux_llm_calls
            if isinstance(item, Mapping)
        ]
        if isinstance(raw_aux_llm_calls, list)
        else []
    )
    aux_llm_calls.append(
        {
            "type": "llm_step_skipped",
            "stage": stage,
            "workflow_state_id": _context_string(request.workflow_state_id),
            "prompt_id": _context_string(prompt_id),
            "reason": "completion_report_missing_required_prompt_tools",
            "missing_prompt_tools": list(missing_prompt_tools),
        }
    )
    raw_llm_calls = request.data.get("llm_calls")
    llm_calls: list[Mapping[str, Any]] = (
        [
            {str(key): value for key, value in item.items() if isinstance(key, str)}
            for item in raw_llm_calls
            if isinstance(item, Mapping)
        ]
        if isinstance(raw_llm_calls, list)
        else []
    )
    result = _build_result(
        request=request,
        response_text="",
        prompt_id=prompt_id,
        prompt_source=prompt_source,
        rendered_variables=rendered_variables,
        llm_policy_map=llm_policy_map,
        validation_policy_map=validation_policy_map,
        selected_model=request.environment.model,
        selected_candidate=None,
        tool_invocations=(),
        tool_messages=(),
        llm_calls=llm_calls,
        aux_llm_calls=aux_llm_calls,
    )
    envelope = result.outputs.get("llm_step_envelope")
    if isinstance(envelope, MutableMapping):
        envelope["completion_reason"] = "skipped_missing_required_prompt_tools"
        envelope["skipped"] = True
        envelope["skip_reason"] = "completion_report_missing_required_prompt_tools"
        envelope["missing_prompt_tools"] = list(missing_prompt_tools)
    return result


def _resolve_user_context_ids(
    context: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    user_concept_id = _context_string(context.get("user_concept_id")) or None
    org_concept_id = (
        _context_string(context.get("org_concept_id"))
        or _context_string(context.get("organisation_concept_id"))
        or None
    )
    return user_concept_id, org_concept_id


def _prefer_default_model_for_request(request: WorkflowActionRequest) -> bool:
    explicit_preference = request.data.get("prefer_default_model")
    if isinstance(explicit_preference, bool):
        return explicit_preference
    requested_model = _context_string(request.data.get("requested_model"))
    requested_client_type = _context_string(request.data.get("requested_client_type"))
    return bool(requested_model or requested_client_type)


def _default_model_parameters_for_request(
    request: WorkflowActionRequest,
) -> Mapping[str, Any] | None:
    requested_parameters = request.data.get("requested_model_parameters")
    if isinstance(requested_parameters, Mapping):
        return dict(requested_parameters)
    if _prefer_default_model_for_request(request):
        return None
    environment_parameters = getattr(request.environment, "model_parameters", None)
    if isinstance(environment_parameters, Mapping):
        return dict(environment_parameters)
    return None


def _model_parameters_with_timeout(
    model_parameters: Mapping[str, Any] | None,
    timeout_seconds: float | None,
) -> Mapping[str, Any] | None:
    params = dict(model_parameters) if isinstance(model_parameters, Mapping) else {}
    if timeout_seconds is not None and timeout_seconds > 0:
        params["request_timeout_seconds"] = (
            _conversation_turn_llm_fallback_guard_timeout_sec(timeout_seconds)
            or timeout_seconds
        )
    return params or None


def _resolve_prompt_from_policy_context(
    *,
    llm_policy: Mapping[str, Any],
    context: Mapping[str, Any],
    prompt_service: PromptTemplateService,
) -> tuple[str | None, str | None, Mapping[str, Any], str | None]:
    prompt_id_context_key = _context_string(llm_policy.get("prompt_id_context_key"))
    prompt_text_context_key = _context_string(llm_policy.get("prompt_text_context_key"))
    prompt_concept_id_context_key = _context_string(
        llm_policy.get("prompt_concept_id_context_key")
    )
    prompt_candidates_context_key = _context_string(
        llm_policy.get("prompt_candidates_context_key")
    )

    prompt_id = (
        _context_string(context.get(prompt_id_context_key))
        if prompt_id_context_key
        else None
    )
    prompt_text = (
        _context_string(context.get(prompt_text_context_key))
        if prompt_text_context_key
        else ""
    )
    if prompt_text:
        return prompt_id or None, prompt_text, {}, "context.prompt_text"

    prompt_candidate_ids: list[str] = []
    if prompt_concept_id_context_key:
        candidate = _context_string(context.get(prompt_concept_id_context_key))
        if candidate:
            prompt_candidate_ids.append(candidate)
    if prompt_candidates_context_key:
        prompt_candidate_ids.extend(
            _coerce_prompt_id_list(context.get(prompt_candidates_context_key))
        )
    prompt_candidate_ids = list(dict.fromkeys(prompt_candidate_ids))
    if not prompt_candidate_ids:
        return None, None, {}, None

    rendered = prompt_service.render_prompt(
        prompt_candidate_ids,
        variables=context,
        fallback=None,
        max_chars=24000,
    )
    if rendered is None:
        return None, None, {}, "context.prompt_candidates_unavailable"
    return rendered.prompt_id, rendered.text, dict(rendered.variables), "context.prompt"


def _resolve_prompt_render(
    *,
    prompt_contract: Mapping[str, Any] | None,
    llm_policy: Mapping[str, Any] | None,
    context: Mapping[str, Any],
) -> tuple[str | None, str | None, Mapping[str, Any], str | None]:
    prompt_service = PromptTemplateService(default_max_chars=24000)
    llm_policy_map = dict(llm_policy) if isinstance(llm_policy, Mapping) else {}

    policy_prompt_id, policy_prompt_text, policy_variables, policy_source = (
        _resolve_prompt_from_policy_context(
            llm_policy=llm_policy_map,
            context=context,
            prompt_service=prompt_service,
        )
    )
    if isinstance(policy_prompt_text, str) and policy_prompt_text.strip():
        return (
            policy_prompt_id,
            policy_prompt_text,
            dict(policy_variables),
            policy_source,
        )

    prompt_contract_map = (
        prompt_contract if isinstance(prompt_contract, Mapping) else {}
    )
    requested_prompt_ids = prompt_contract_map.get("requested_prompt_concept_ids") or []
    prompt_candidates = (
        list(requested_prompt_ids) if isinstance(requested_prompt_ids, list) else []
    )
    fallback_prompt = _context_string(prompt_contract_map.get("prompt_text")) or None
    if prompt_candidates or fallback_prompt:
        rendered = prompt_service.render_prompt(
            prompt_candidates,
            variables=context,
            fallback=fallback_prompt,
            max_chars=24000,
        )
        if rendered is not None:
            return (
                rendered.prompt_id,
                rendered.text,
                dict(rendered.variables),
                "prompt_contract",
            )

    static_prompt_candidates = _coerce_prompt_id_list(
        llm_policy_map.get("prompt_candidates")
    )
    static_fallback_prompt = _context_string(llm_policy_map.get("prompt_text")) or None
    if static_prompt_candidates or static_fallback_prompt:
        rendered = prompt_service.render_prompt(
            static_prompt_candidates,
            variables=context,
            fallback=static_fallback_prompt,
            max_chars=24000,
        )
        if rendered is not None:
            return (
                rendered.prompt_id,
                rendered.text,
                dict(rendered.variables),
                "llm_policy",
            )

    return None, None, {}, None


def _diagnostic_prompt_id_from_contract(
    prompt_contract: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(prompt_contract, Mapping):
        return None
    resolved_prompt_id = _context_string(
        prompt_contract.get("resolved_prompt_concept_id")
    )
    if resolved_prompt_id:
        return resolved_prompt_id
    requested_prompt_ids = prompt_contract.get("requested_prompt_concept_ids")
    if isinstance(requested_prompt_ids, Sequence) and not isinstance(
        requested_prompt_ids,
        (str, bytes, bytearray),
    ):
        for candidate in requested_prompt_ids:
            prompt_id = _context_string(candidate)
            if prompt_id:
                return prompt_id
    return None


def _append_recovery_total_truncation_marker(
    *,
    sections: list[str],
    remaining_budget: int,
    diagnostics: dict[str, Any],
) -> int:
    if remaining_budget <= 0:
        diagnostics["total_truncation_marker_rendered"] = False
        return remaining_budget
    marker = (
        TOTAL_TRUNCATION_MARKER
        if len(TOTAL_TRUNCATION_MARKER) <= remaining_budget
        else TOTAL_TRUNCATION_MARKER[:remaining_budget]
    )
    if not marker:
        diagnostics["total_truncation_marker_rendered"] = False
        return remaining_budget
    sections.append(marker)
    diagnostics["total_truncation_marker_rendered"] = True
    return remaining_budget - len(marker)


def _render_llm_context_field_sections(
    *,
    llm_policy: Mapping[str, Any],
    context: Mapping[str, Any],
    workflow_state_id: str | None = None,
) -> tuple[bool, list[str], list[str], dict[str, Any] | None]:
    """Render the authored context-field sections used by an LLM prompt.

    The first section list contains everything appended to the prompt for the
    context-field block, including a recovery total-truncation marker when one
    is required.  The second contains only the exact ordered labelled field
    sections and is therefore the stable lineage surface hashed below.
    """

    raw_context_fields = llm_policy.get("context_fields")
    if not isinstance(raw_context_fields, Sequence) or isinstance(
        raw_context_fields,
        (str, bytes, bytearray),
    ):
        return False, [], [], None

    context_field_items = list(raw_context_fields)
    prompt_sections: list[str] = []
    rendered_field_sections: list[str] = []
    # JVNAUTOSCI-2514: the recovery-decision step inlines the full turn
    # state; project each field to bounded runtime evidence and cap the
    # total so the prompt cannot balloon to tens of thousands of tokens.
    is_recovery = is_recovery_decision_state(workflow_state_id)
    remaining_recovery_budget = RECOVERY_CONTEXT_TOTAL_CHAR_BUDGET
    recovery_diagnostics: dict[str, Any] | None = None
    if is_recovery:
        recovery_diagnostics = {
            "schema_version": "recovery_context_compaction.v1",
            "enabled": True,
            "workflow_state_id": _context_string(workflow_state_id),
            "field_char_budget": RECOVERY_CONTEXT_FIELD_CHAR_BUDGET,
            "total_context_char_budget": RECOVERY_CONTEXT_TOTAL_CHAR_BUDGET,
            "candidate_context_field_count": len(context_field_items),
            "rendered_context_field_count": 0,
            "skipped_empty_context_field_count": 0,
            "omitted_context_field_count": 0,
            "rendered_context_chars": 0,
            "total_context_budget_exhausted": False,
            "total_truncation_marker_rendered": False,
            "fields": [],
        }

    for index, item in enumerate(context_field_items):
        if not isinstance(item, Mapping):
            continue
        context_key = _context_string(item.get("context_key"))
        if not context_key:
            continue
        value = context.get(context_key)
        label = _context_string(item.get("label")) or context_key.replace("_", " ")
        if is_recovery:
            assert recovery_diagnostics is not None
            if remaining_recovery_budget <= 0:
                recovery_diagnostics["total_context_budget_exhausted"] = True
                recovery_diagnostics["omitted_context_field_count"] = (
                    len(context_field_items) - index
                )
                break
            projection = compact_recovery_context_field(value)
            value_text = _serialise_prompt_context_value(projection.value)
            field_diagnostics = dict(projection.diagnostics)
            field_diagnostics.update(
                {
                    "context_key": context_key,
                    "label": label,
                    "input_value_kind": type(value).__name__,
                }
            )
            if not value_text:
                field_diagnostics["skipped_empty"] = True
                recovery_diagnostics["skipped_empty_context_field_count"] = (
                    int(
                        recovery_diagnostics.get("skipped_empty_context_field_count")
                        or 0
                    )
                    + 1
                )
                recovery_diagnostics["fields"].append(field_diagnostics)
                continue
            header = f"{label}:\n"
            body_budget = min(
                RECOVERY_CONTEXT_FIELD_CHAR_BUDGET,
                max(0, remaining_recovery_budget - len(header)),
            )
            if body_budget <= 0:
                recovery_diagnostics["total_context_budget_exhausted"] = True
                recovery_diagnostics["omitted_context_field_count"] = (
                    len(context_field_items) - index
                )
                break
            serialised_compacted_chars = len(value_text)
            value_text = render_compacted_recovery_field_text(
                value_text,
                char_budget=body_budget,
            )
            section = f"{header}{value_text}"
            remaining_recovery_budget -= len(section)
            field_diagnostics.update(
                {
                    "skipped_empty": False,
                    "serialised_compacted_chars": serialised_compacted_chars,
                    "rendered_body_chars": len(value_text),
                    "rendered_section_chars": len(section),
                    "field_char_budget_used": body_budget,
                    "body_truncated_by_char_budget": (
                        serialised_compacted_chars > body_budget
                    ),
                }
            )
            recovery_diagnostics["fields"].append(field_diagnostics)
            recovery_diagnostics["rendered_context_field_count"] = (
                int(recovery_diagnostics.get("rendered_context_field_count") or 0) + 1
            )
            recovery_diagnostics["rendered_context_chars"] = int(
                recovery_diagnostics.get("rendered_context_chars") or 0
            ) + len(section)
            prompt_sections.append(section)
            rendered_field_sections.append(section)
            continue
        value_text = _serialise_prompt_context_value(value)
        if not value_text:
            continue
        section = f"{label}:\n{value_text}"
        prompt_sections.append(section)
        rendered_field_sections.append(section)

    if recovery_diagnostics is not None:
        if recovery_diagnostics["omitted_context_field_count"]:
            remaining_recovery_budget = _append_recovery_total_truncation_marker(
                sections=prompt_sections,
                remaining_budget=remaining_recovery_budget,
                diagnostics=recovery_diagnostics,
            )
        recovery_diagnostics["context_char_budget_remaining"] = max(
            0,
            remaining_recovery_budget,
        )
    return (
        True,
        prompt_sections,
        rendered_field_sections,
        recovery_diagnostics,
    )


def _rendered_llm_context_fields_sha256(sections: Sequence[str]) -> str:
    encoded = json.dumps(
        list(sections),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_llm_context_fields_sha256(
    *,
    llm_policy: Mapping[str, Any],
    context: Mapping[str, Any],
    workflow_state_id: str | None = None,
) -> str | None:
    """Recompute exact rendered context-field lineage without an LLM call.

    ``None`` means the policy does not author a ``context_fields`` sequence.
    An authored sequence that renders no fields deliberately hashes the empty
    ordered list so consumers can distinguish it from missing lineage. Recovery
    truncation markers are part of this digest because they are appended to the
    model-visible context-field block.
    """

    authored, prompt_sections, _rendered_field_sections, _diagnostics = (
        _render_llm_context_field_sections(
            llm_policy=llm_policy,
            context=context,
            workflow_state_id=workflow_state_id,
        )
    )
    if not authored:
        return None
    return _rendered_llm_context_fields_sha256(prompt_sections)


def _compose_llm_prompt_with_diagnostics(
    *,
    base_prompt: str,
    llm_policy: Mapping[str, Any],
    context: Mapping[str, Any],
    workflow_state_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    sections: list[str] = []
    prompt_context_diagnostics: dict[str, Any] = {}

    prefix_text = _context_string(
        llm_policy.get("prompt_prefix_text") or llm_policy.get("system_preamble")
    )
    if prefix_text:
        sections.append(prefix_text)

    prompt_text = _context_string(base_prompt)
    if prompt_text:
        sections.append(prompt_text)

    response_contract_text = _context_string(llm_policy.get("response_contract_text"))
    if response_contract_text:
        sections.append(response_contract_text)

    (
        context_fields_authored,
        context_prompt_sections,
        rendered_context_field_sections,
        recovery_diagnostics,
    ) = _render_llm_context_field_sections(
        llm_policy=llm_policy,
        context=context,
        workflow_state_id=workflow_state_id,
    )
    sections.extend(context_prompt_sections)
    if context_fields_authored:
        prompt_context_diagnostics["schema_version"] = (
            "llm_prompt_context_diagnostics.v1"
        )
        prompt_context_diagnostics["llm_context_fields_sha256"] = (
            _rendered_llm_context_fields_sha256(context_prompt_sections)
        )
        # Keep only a bounded scalar count in generic diagnostics; context-field
        # labels and values remain represented prompt content, never telemetry.
        prompt_context_diagnostics["llm_context_fields_rendered_count"] = len(
            rendered_context_field_sections
        )
        if recovery_diagnostics is not None:
            prompt_context_diagnostics["workflow_state_id"] = _context_string(
                workflow_state_id
            )
            prompt_context_diagnostics["recovery_context_compaction"] = (
                recovery_diagnostics
            )

    rendered_prompt = "\n\n".join(section for section in sections if section).strip()
    if prompt_context_diagnostics:
        prompt_context_diagnostics["rendered_prompt_chars"] = len(rendered_prompt)
    return rendered_prompt, prompt_context_diagnostics


def _compose_llm_prompt(
    *,
    base_prompt: str,
    llm_policy: Mapping[str, Any],
    context: Mapping[str, Any],
    workflow_state_id: str | None = None,
) -> str:
    rendered_prompt, _diagnostics = _compose_llm_prompt_with_diagnostics(
        base_prompt=base_prompt,
        llm_policy=llm_policy,
        context=context,
        workflow_state_id=workflow_state_id,
    )
    return rendered_prompt


def _validation_output_format(validation_policy: Mapping[str, Any]) -> str:
    for key in ("output_format", "format", "validator"):
        value = _context_string(validation_policy.get(key))
        if value:
            return value.lower()
    return ""


def _json_path_parts(path: Any) -> list[str]:
    text = _context_string(path)
    if not text:
        return []
    return [part.strip() for part in text.split(".") if part.strip()]


def _json_path_get(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for part in _json_path_parts(path):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current.get(part)
    return current


def _json_path_set(payload: MutableMapping[str, Any], path: str, value: Any) -> None:
    parts = _json_path_parts(path)
    if not parts:
        return
    current: MutableMapping[str, Any] = payload
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, MutableMapping):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def _json_value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _json_field_defaults(validation_policy: Mapping[str, Any]) -> Mapping[str, Any]:
    raw_defaults = validation_policy.get("json_field_defaults")
    return raw_defaults if isinstance(raw_defaults, Mapping) else {}


def _json_required_fields(validation_policy: Mapping[str, Any]) -> list[str]:
    raw_fields = validation_policy.get("required_json_fields") or validation_policy.get(
        "json_required_fields"
    )
    if isinstance(raw_fields, str):
        return [raw_fields.strip()] if raw_fields.strip() else []
    if not isinstance(raw_fields, Sequence) or isinstance(
        raw_fields, (bytes, bytearray)
    ):
        return []
    return [field for raw in raw_fields if (field := _context_string(raw))]


def _coerce_spoken_text(text: Any) -> str | None:
    raw = _context_string(text)
    if not raw:
        return None

    match = _SPOKEN_BLOCK_RE.search(raw)
    if match:
        raw = _context_string(match.group(1))

    raw = re.sub(r"</?spoken>", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"</?screen>", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
    raw = raw.replace("`", "").strip()
    return raw or None


def _build_planning_outputs(
    *,
    response_text: str,
    request: WorkflowActionRequest,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .durable.planning_workflow import (
        _append_trace_event,
        _coerce_planning_actions,
        _coerce_str_list,
        _extract_json_payload,
    )

    parsed_payload, parse_mode = _extract_json_payload(response_text)
    if not isinstance(parsed_payload, Mapping):
        return (
            {},
            {
                "status": "failed",
                "reason": f"planning_json_parse_failed:{parse_mode}",
                "parse_mode": parse_mode,
            },
        )

    actions = _coerce_planning_actions(parsed_payload.get("actions"))
    assumptions = _coerce_str_list(parsed_payload.get("assumptions"))
    raw_planning_context = request.data.get("planning_context")
    planning_context = (
        {
            str(key): value
            for key, value in raw_planning_context.items()
            if isinstance(key, str)
        }
        if isinstance(raw_planning_context, Mapping)
        else {}
    )
    planning_goal = (
        _context_string(parsed_payload.get("goal"))
        or _context_string(planning_context.get("goal"))
        or None
    )
    trace = _append_trace_event(
        request.data,
        stage="infer",
        event="plan_inferred",
        details={
            "parse_mode": parse_mode,
            "actions_count": len(actions),
        },
    )

    raw_planning_diagnostics = request.data.get("planning_diagnostics")
    diagnostics = (
        {
            str(key): value
            for key, value in raw_planning_diagnostics.items()
            if isinstance(key, str)
        }
        if isinstance(raw_planning_diagnostics, Mapping)
        else {}
    )
    diagnostics["inference"] = {
        "parse_mode": parse_mode,
        "actions_count": len(actions),
        "assumptions_count": len(assumptions),
        "response_preview": response_text[:300],
    }

    return (
        {
            "planning_goal": planning_goal,
            "planning_assumptions": assumptions,
            "planning_actions": actions,
            "planning_raw_plan": dict(parsed_payload),
            "planning_trace": trace,
            "planning_diagnostics": diagnostics,
        },
        {
            "status": "success",
            "parse_mode": parse_mode,
            "actions_count": len(actions),
        },
    )


def _normalise_validated_json_payload_for_prompt(
    payload: Any,
    *,
    prompt_id: str | None,
    workflow_state_id: str | None = None,
) -> Any:
    """Apply schema-boundary normalisation for known JSON contracts."""

    if not isinstance(payload, Mapping):
        return payload
    clean_prompt_id = _context_string(prompt_id)
    clean_workflow_state_id = _context_string(workflow_state_id).lower()
    is_expected_outcome_step = (
        clean_prompt_id == _TURN_EXPECTED_OUTCOME_INFERENCE_PROMPT_ID
        or "expected_outcome_inference" in clean_workflow_state_id
    )
    if not is_expected_outcome_step:
        return payload

    contract = TurnExpectedOutcomeContract.from_mapping(payload)
    if contract.is_empty():
        return payload

    normalised = dict(payload)
    if contract.summary:
        normalised["expected_outcome_summary"] = contract.summary
        normalised["summary"] = contract.summary
    if contract.grounding_requirement:
        normalised["grounding_requirement"] = contract.grounding_requirement
    if contract.precision_policy:
        normalised["precision_policy"] = contract.precision_policy
    if contract.selector_guidance:
        normalised["selector_guidance"] = contract.selector_guidance
    if contract.answering_guidance:
        normalised["answering_guidance"] = contract.answering_guidance
    if contract.reasoning:
        normalised["reasoning"] = contract.reasoning
    if contract.required_tools:
        normalised["required_tools"] = list(contract.required_tools)
    if contract.conditional_required_tools:
        normalised["conditional_required_tools"] = list(
            contract.conditional_required_tools
        )
    if contract.target_concept_ids:
        normalised["target_concept_ids"] = list(contract.target_concept_ids)
    if contract.target_type_ids:
        normalised["target_type_ids"] = list(contract.target_type_ids)
    if contract.workflow_concept_ids:
        normalised["workflow_concept_ids"] = list(contract.workflow_concept_ids)
    if contract.target_contracts:
        normalised["target_contracts"] = [
            target_contract.to_state_payload()
            for target_contract in contract.target_contracts
        ]
    return normalised


def _build_validated_json_outputs(
    *,
    response_text: str,
    prompt_id: str | None = None,
    workflow_state_id: str | None = None,
    validation_policy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .durable.planning_workflow import _extract_json_payload

    parsed_payload, parse_mode = _extract_json_payload(response_text)

    validation_policy_map = (
        validation_policy if isinstance(validation_policy, Mapping) else {}
    )
    field_defaults = _json_field_defaults(validation_policy_map)
    required_fields = _json_required_fields(validation_policy_map)
    defaultable_object_contract = bool(field_defaults)

    if parsed_payload is None and not defaultable_object_contract:
        return (
            {},
            {
                "status": "failed",
                "reason": f"json_parse_failed:{parse_mode}",
                "parse_mode": parse_mode,
            },
        )

    coerced_non_object = False
    if (
        parsed_payload is None or not isinstance(parsed_payload, Mapping)
    ) and defaultable_object_contract:
        parsed_payload = {}
        coerced_non_object = True

    normalised_payload = _normalise_validated_json_payload_for_prompt(
        parsed_payload,
        prompt_id=prompt_id,
        workflow_state_id=workflow_state_id,
    )
    defaults_applied: list[str] = []
    if isinstance(normalised_payload, Mapping) and field_defaults:
        normalised_payload = dict(normalised_payload)
        for raw_key, default_value in field_defaults.items():
            key = _context_string(raw_key)
            if not key:
                continue
            existing = _json_path_get(normalised_payload, key)
            if not _json_value_present(existing):
                _json_path_set(normalised_payload, key, default_value)
                defaults_applied.append(key)

    missing_required_fields = (
        [
            field
            for field in required_fields
            if not isinstance(normalised_payload, Mapping)
            or not _json_value_present(_json_path_get(normalised_payload, field))
        ]
        if required_fields
        else []
    )
    if missing_required_fields:
        return (
            {},
            {
                "status": "failed",
                "reason": "json_required_fields_missing",
                "output_format": "json_value",
                "parse_mode": parse_mode,
                "missing_required_fields": missing_required_fields,
            },
        )

    validation_summary: dict[str, Any] = {
        "status": "success",
        "output_format": "json_value",
        "parse_mode": parse_mode,
    }
    if defaults_applied:
        validation_summary["json_defaults_applied"] = sorted(defaults_applied)
    if coerced_non_object:
        validation_summary["json_object_defaulted_from_non_object"] = True

    outputs = {
        "validated_json": normalised_payload,
        "validated_json_parse_mode": parse_mode,
        "validated_json_raw_response": response_text,
        "result": True,
    }
    if defaults_applied:
        outputs["validated_json_defaults_applied"] = sorted(defaults_applied)
    return (outputs, validation_summary)


def _apply_validation_policy(
    *,
    request: WorkflowActionRequest,
    response_text: str,
    prompt_id: str | None,
    selected_model: str | None,
    validation_policy: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(validation_policy, Mapping):
        return ({}, {"status": "skipped"})

    output_format = _validation_output_format(validation_policy)
    if not output_format:
        return ({}, {"status": "skipped"})

    if output_format == "narration_spoken_xml":
        spoken = _coerce_spoken_text(response_text) or _coerce_spoken_text(
            request.data.get("screen_text")
        )
        return (
            {
                "narration_rendered": bool(spoken),
                "narration_spoken": spoken,
                "narration_raw_response": response_text,
                "result": bool(spoken),
            },
            {
                "status": "success" if spoken else "failed",
                "output_format": output_format,
                "spoken_present": bool(spoken),
            },
        )

    if output_format == "buttonify_options_json":
        options = sanitise_buttonify_options(
            parse_buttonify_options_json(response_text)
        )
        status = "success" if options else "no_op"
        return (
            {
                "buttonify_status": status,
                "buttonify_options": options,
                "buttonify_source": "llm" if options else "none",
                "buttonify_prompt_id": _context_string(
                    request.data.get("buttonify_prompt_id")
                )
                or prompt_id,
                "buttonify_prompt_truncated": bool(
                    request.data.get("buttonify_prompt_truncated")
                ),
                "buttonify_prompt_available": bool(
                    request.data.get("buttonify_prompt_available", True)
                ),
                "buttonify_prompt_error": request.data.get("buttonify_prompt_error"),
                "buttonify_model_used": selected_model,
                "buttonify_model_attempted": True,
                "buttonify_error_class": None,
                "buttonify_suppression_reason": None if options else "no_candidates",
                "result": bool(options),
            },
            {
                "status": status,
                "output_format": output_format,
                "options_count": len(options),
            },
        )

    if output_format == "planning_plan_json":
        return _build_planning_outputs(response_text=response_text, request=request)

    if output_format == "json_value":
        return _build_validated_json_outputs(
            response_text=response_text,
            prompt_id=prompt_id,
            workflow_state_id=request.workflow_state_id,
            validation_policy=validation_policy,
        )

    return (
        {"llm_step_validation_unhandled": output_format},
        {
            "status": "skipped",
            "output_format": output_format,
            "reason": "validation_handler_unavailable",
        },
    )


def _append_llm_call(
    bucket: MutableMapping[str, Any],
    *,
    call_type: str,
    stage: str,
    model_name: str | None,
    provider: str | None = None,
    candidate: Mapping[str, Any] | None = None,
    duration_ms: float | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    llm_calls = bucket.get("llm_calls")
    if not isinstance(llm_calls, list):
        llm_calls = []
        bucket["llm_calls"] = llm_calls
    entry: dict[str, Any] = {
        "type": call_type,
        "stage": stage,
        "model_name": model_name,
    }
    if provider:
        entry["provider"] = provider
    if isinstance(candidate, Mapping):
        entry["candidate"] = dict(candidate)
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    if note:
        entry["note"] = note
    stamp_llm_call_timestamps(entry, duration_ms=duration_ms)
    llm_calls.append(entry)
    return entry


def _request_id_from_data(data: Mapping[str, Any]) -> str | None:
    for key in ("request_id", "client_request_id", "turn_request_id"):
        value = _context_string(data.get(key))
        if value:
            return value
    return None


def _positive_float_env(env_name: str, default: float) -> float:
    raw_value = os.getenv(env_name)
    try:
        parsed = float(raw_value) if raw_value is not None else default
    except (TypeError, ValueError):
        parsed = default
    if parsed <= 0:
        return default
    return parsed


def _append_workflow_llm_setup_diagnostic(
    request: WorkflowActionRequest,
    entry: Mapping[str, Any],
) -> None:
    if not isinstance(request.data, MutableMapping):
        return
    bucket = request.data.get("aux_llm_calls")
    if not isinstance(bucket, list):
        bucket = []
        request.data["aux_llm_calls"] = bucket
    bucket.append(dict(entry))


def _run_gateway_runtime_setup_step(
    request: WorkflowActionRequest,
    *,
    step_id: str,
    step_label: str,
    operation: Any,
    timeout_seconds: float | None,
    timeout_fallback: Any | None = None,
) -> Any:
    _check_request_cancellation(request)
    step_start = time.perf_counter()
    if timeout_seconds is None or timeout_seconds <= 0:
        return operation()

    result_event = threading.Event()
    result_holder: dict[str, Any] = {}

    def _worker() -> None:
        try:
            result_holder["kind"] = "result"
            result_holder["payload"] = operation()
        except Exception as exc:
            result_holder["kind"] = "exception"
            result_holder["payload"] = exc
        finally:
            result_event.set()

    thread = threading.Thread(
        target=_worker,
        name=f"workflow-llm-step-setup-{step_id}",
        daemon=True,
    )
    thread.start()
    while not result_event.wait(timeout=0.25):
        _check_request_cancellation(request)
        if time.perf_counter() - step_start < timeout_seconds:
            continue
        duration_ms = int((time.perf_counter() - step_start) * 1000)
        _append_workflow_llm_setup_diagnostic(
            request,
            {
                "type": "workflow_llm_step_setup",
                "step_id": step_id,
                "step_label": step_label,
                "stage": "workflow_llm_step_setup",
                "status": "timed_out",
                "failure_reason": "workflow_llm_step_setup_timeout",
                "timeout_seconds": timeout_seconds,
                "duration_ms": duration_ms,
            },
        )
        if timeout_fallback is not None:
            return timeout_fallback(timeout_seconds)
        raise TimeoutError(f"{step_label} timed out after {timeout_seconds:.1f}s")
    kind = result_holder.get("kind")
    payload = result_holder.get("payload")
    if kind == "exception":
        raise payload
    _check_request_cancellation(request)
    return payload


def _record_workflow_llm_duration_for_entry(
    request: WorkflowActionRequest,
    entry: MutableMapping[str, Any],
    *,
    stage: str | None,
    model_name: str | None,
    provider: str | None = None,
    duration_ms: float | None,
    workflow_stage_id: str | None = None,
) -> None:
    status = _context_string(entry.get("status")).lower()
    failure_kind = _context_string(entry.get("failure_kind")).lower()
    if failure_kind == "llm_call_timeout" or status == "timed_out":
        outcome = "timeout"
    elif entry.get("success") is True or status in {"success", "succeeded", "ok"}:
        outcome = "success"
    elif (
        entry.get("success") is False
        or entry.get("error")
        or status
        in {
            "failure",
            "failed",
            "error",
        }
    ):
        outcome = "failure"
    else:
        # This recorder is invoked after a completed call or with an explicit
        # timeout/failure entry. A completed entry without error fields is a
        # successful duration observation.
        outcome = "success"
    try:
        baseline = record_workflow_llm_step_duration_observation(
            workflow_id=request.workflow_id,
            workflow_state_id=request.workflow_state_id,
            workflow_stage_id=workflow_stage_id or stage,
            stage=stage,
            provider=provider,
            model_name=model_name,
            duration_ms=duration_ms,
            request_id=_request_id_from_data(request.data),
            outcome=outcome,
        )
    except Exception as exc:
        entry["duration_stats_error"] = exc.__class__.__name__
        return
    if isinstance(baseline, Mapping) and baseline:
        entry.update(dict(baseline))


def _record_workflow_llm_duration_for_entry_async(
    request: WorkflowActionRequest,
    entry: MutableMapping[str, Any],
    *,
    stage: str | None,
    model_name: str | None,
    provider: str | None = None,
    duration_ms: float | None,
    workflow_stage_id: str | None = None,
) -> None:
    def _worker() -> None:
        _record_workflow_llm_duration_for_entry(
            request,
            entry,
            stage=stage,
            model_name=model_name,
            provider=provider,
            duration_ms=duration_ms,
            workflow_stage_id=workflow_stage_id,
        )

    threading.Thread(
        target=_worker,
        name="workflow-llm-duration-record",
        daemon=True,
    ).start()


def _has_llm_call_timeout_entry(
    llm_calls: Iterable[Mapping[str, Any]],
    *,
    stage: str,
    prompt_id: str | None,
    workflow_state_id: str | None,
) -> bool:
    for entry in llm_calls:
        if entry.get("failure_kind") != "llm_call_timeout":
            continue
        if _context_string(entry.get("stage")) != stage:
            continue
        entry_prompt_id = _context_string(entry.get("prompt_id"))
        if prompt_id and entry_prompt_id and entry_prompt_id != prompt_id:
            continue
        entry_state_id = _context_string(entry.get("workflow_stage_id"))
        if workflow_state_id and entry_state_id and entry_state_id != workflow_state_id:
            continue
        return True
    return False


def _nonblocking_progress_callback(
    emit_progress: Callable[[Mapping[str, Any]], None] | None,
) -> Callable[[Mapping[str, Any]], None]:
    if not callable(emit_progress):
        return lambda _payload: None

    lock = threading.Lock()
    in_flight = False

    def _emit(payload: Mapping[str, Any]) -> None:
        nonlocal in_flight
        with lock:
            if in_flight:
                return
            in_flight = True
        payload_snapshot = dict(payload)

        def _worker() -> None:
            nonlocal in_flight
            try:
                emit_progress(payload_snapshot)
            except Exception:
                pass
            finally:
                with lock:
                    in_flight = False

        threading.Thread(
            target=_worker,
            name="workflow-llm-progress-forwarder",
            daemon=True,
        ).start()

    return _emit


def _run_llm_call_with_timeout(
    request: WorkflowActionRequest,
    *,
    operation: Any,
    stage: str,
    prompt_id: str | None,
    selected_model: str | None,
    selected_candidate: Mapping[str, Any] | None,
    timeout_seconds: float | None,
    llm_calls: list[dict[str, Any]],
    hard_guard_timeout_seconds: float | None = None,
    record_advisory_crossing: bool = True,
) -> Any:
    _check_request_cancellation(request)
    started_at = time.perf_counter()
    if timeout_seconds is None or timeout_seconds <= 0:
        return operation()
    advisory_timeout_seconds = timeout_seconds
    hard_guard_timeout_seconds = hard_guard_timeout_seconds or (
        _conversation_turn_llm_fallback_guard_timeout_sec(timeout_seconds)
        or timeout_seconds
    )

    emit_progress = (
        request.data.get("emit_progress")
        if callable(request.data.get("emit_progress"))
        else None
    )
    progress_emit = _nonblocking_progress_callback(emit_progress)
    telemetry_phase = _conversation_turn_llm_phase_override(request) or stage
    if callable(emit_progress):
        progress_emit(
            {
                "phase": telemetry_phase,
                "status": "thinking",
                "subtask": "LLM call",
                "result_summary": "Running LLM call for workflow step.",
                "workflow_state_id": telemetry_phase,
                "llm_advisory_budget_seconds": advisory_timeout_seconds,
                "llm_hard_guard_seconds": hard_guard_timeout_seconds,
                "prompt_id": prompt_id,
                "model_name": selected_model,
                "provider": (
                    _context_string(selected_candidate.get("provider"))
                    if isinstance(selected_candidate, Mapping)
                    else None
                ),
            }
        )

    result_event = threading.Event()
    result_holder: dict[str, Any] = {}

    def _worker() -> None:
        try:
            result_holder["kind"] = "result"
            result_holder["payload"] = operation()
        except Exception as exc:
            result_holder["kind"] = "exception"
            result_holder["payload"] = exc
        finally:
            result_event.set()

    thread = threading.Thread(
        target=_worker,
        name=f"workflow-llm-step-call-{stage}",
        daemon=True,
    )
    thread.start()
    advisory_crossing_recorded = False
    while not result_event.wait(timeout=0.25):
        _check_request_cancellation(request)
        elapsed_seconds = time.perf_counter() - started_at
        if (
            elapsed_seconds >= advisory_timeout_seconds
            and not advisory_crossing_recorded
            and record_advisory_crossing
        ):
            advisory_crossing_recorded = True
            advisory_entry = {
                "type": "workflow_llm_advisory_budget",
                "stage": stage,
                "workflow_stage_id": request.workflow_state_id,
                "model_name": selected_model,
                "status": "exceeded_continuing",
                "advisory_timeout_seconds": advisory_timeout_seconds,
                "hard_guard_timeout_seconds": hard_guard_timeout_seconds,
                "duration_ms": elapsed_seconds * 1000.0,
                "prompt_id": prompt_id,
            }
            _append_workflow_llm_setup_diagnostic(request, advisory_entry)
            progress_emit(
                {
                    "phase": telemetry_phase,
                    "status": "thinking",
                    "subtask": "LLM call exceeded advisory budget",
                    "result_summary": (
                        "The workflow LLM call is still running beyond its "
                        "advisory duration budget."
                    ),
                    "workflow_state_id": telemetry_phase,
                    "llm_advisory_budget_seconds": advisory_timeout_seconds,
                    "llm_hard_guard_seconds": hard_guard_timeout_seconds,
                    "elapsed_ms": int(elapsed_seconds * 1000.0),
                    "prompt_id": prompt_id,
                    "model_name": selected_model,
                }
            )
        if elapsed_seconds < hard_guard_timeout_seconds:
            continue
        duration_ms = (time.perf_counter() - started_at) * 1000.0
        provider = (
            _context_string(selected_candidate.get("provider"))
            if isinstance(selected_candidate, Mapping)
            else None
        )
        timeout_entry: dict[str, Any] = {
            "type": "llm.generate",
            "stage": stage,
            "workflow_stage_id": request.workflow_state_id,
            "model_name": selected_model,
            "duration_ms": duration_ms,
            "status": "timed_out",
            "success": False,
            "error": "workflow_llm_step_timeout",
            "error_class": "TimeoutError",
            "failure_kind": "llm_call_timeout",
            "timeout_seconds": hard_guard_timeout_seconds,
            "advisory_timeout_seconds": advisory_timeout_seconds,
            "hard_guard_timeout_seconds": hard_guard_timeout_seconds,
            "advisory_budget_exceeded": (elapsed_seconds >= advisory_timeout_seconds),
            "prompt_id": prompt_id,
        }
        if provider:
            timeout_entry["provider"] = provider
        if isinstance(selected_candidate, Mapping):
            timeout_entry["candidate"] = dict(selected_candidate)
        stamp_llm_call_timestamps(timeout_entry, duration_ms=duration_ms)
        if not _has_llm_call_timeout_entry(
            llm_calls,
            stage=stage,
            prompt_id=prompt_id,
            workflow_state_id=request.workflow_state_id,
        ):
            llm_calls.append(timeout_entry)
            _record_workflow_llm_duration_for_entry_async(
                request,
                timeout_entry,
                stage=stage,
                model_name=selected_model,
                provider=provider,
                duration_ms=duration_ms,
                workflow_stage_id=request.workflow_state_id,
            )
        raise TimeoutError(
            f"LLM call exceeded the {advisory_timeout_seconds:.1f}s advisory "
            f"budget and timed out at the {hard_guard_timeout_seconds:.1f}s "
            "hard guard "
            f"(stage={stage}, state={request.workflow_state_id}, model={selected_model})"
        )
    kind = result_holder.get("kind")
    payload = result_holder.get("payload")
    if kind == "exception":
        raise payload
    _check_request_cancellation(request)
    return payload


def _run_llm_step_with_timeout(
    request: WorkflowActionRequest,
    *,
    stage: str,
    llm_policy_map: Mapping[str, Any],
    timeout_seconds: float,
) -> WorkflowActionResult:
    prompt_id = _diagnostic_prompt_id_from_contract(request.prompt_contract)
    llm_calls = cast(
        list[dict[str, Any]],
        (
            request.data.get("llm_calls")
            if isinstance(request.data.get("llm_calls"), list)
            else []
        ),
    )
    if "llm_calls" not in request.data:
        request.data["llm_calls"] = llm_calls
    aux_llm_calls = cast(
        list[dict[str, Any]],
        (
            request.data.get("aux_llm_calls")
            if isinstance(request.data.get("aux_llm_calls"), list)
            else []
        ),
    )
    if "aux_llm_calls" not in request.data:
        request.data["aux_llm_calls"] = aux_llm_calls

    try:
        return cast(
            WorkflowActionResult,
            _run_llm_call_with_timeout(
                request,
                operation=lambda: _execute_llm_step_inner(request),
                stage=stage,
                prompt_id=prompt_id,
                selected_model=request.environment.model,
                selected_candidate=_selected_candidate_context_from_request(request),
                timeout_seconds=timeout_seconds,
                llm_calls=llm_calls,
                hard_guard_timeout_seconds=(
                    _conversation_turn_llm_step_guard_timeout_sec(timeout_seconds)
                    or timeout_seconds
                ),
                record_advisory_crossing=False,
            ),
        )
    except TimeoutError as exc:
        timeout_detail = _context_string(str(exc)) or "llm_step_timed_out"
        return _build_timeout_failure_result(
            request=request,
            stage=stage,
            prompt_id=prompt_id,
            prompt_source="prompt_contract",
            rendered_variables={},
            llm_policy_map=llm_policy_map,
            timeout_detail=timeout_detail,
            selected_model=request.environment.model,
            llm_calls=llm_calls,
            aux_llm_calls=aux_llm_calls,
            selected_candidate=_selected_candidate_context_from_request(request),
            timeout_seconds=timeout_seconds,
        )


def _tool_mode(llm_policy: Mapping[str, Any]) -> str:
    raw = _context_string(llm_policy.get("tool_mode")).lower()
    if raw in {"allowed", "tool_augmented", "tools"}:
        return "allowed"
    if raw in {"none", "disabled", "off"}:
        return "none"
    if "allowed_tools" in llm_policy or "required_tools" in llm_policy:
        return "allowed"
    return "none"


def _llm_stage(llm_policy: Mapping[str, Any], request: WorkflowActionRequest) -> str:
    policy_stage = _context_string(llm_policy.get("policy_stage"))
    if policy_stage:
        return policy_stage
    action_id = _context_string(request.action_id)
    if action_id:
        return action_id
    return "llm_step"


def _check_request_cancellation(request: WorkflowActionRequest) -> None:
    check_cancellation = request.data.get("check_cancellation")
    if callable(check_cancellation):
        check_cancellation()


def _build_gateway_runtime(
    request: WorkflowActionRequest,
    *,
    max_tool_invocations: int | None = None,
) -> tuple[Any, Any, Mapping[str, Any], str | None, str | None]:
    from ..integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
        _WorkflowModelPolicyState,
    )
    from ..services.model_registry_service import get_model_registry_snapshot

    gateway = request.environment.gateway
    if gateway is None:
        raise RuntimeError("workflow_llm_step_gateway_unavailable")

    _check_request_cancellation(request)
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=(
            int(max_tool_invocations)
            if max_tool_invocations is not None
            else (
                int(request.environment.max_tool_invocations)
                if request.environment.max_tool_invocations is not None
                else 1
            )
        ),
        max_tool_result_chars=request.environment.max_tool_result_chars,
        max_tool_result_field_chars=request.environment.max_tool_result_field_chars,
        tool_batch_cap=(
            max(1, int(request.inputs.get("tool_batch_cap") or 4))
            if request.inputs.get("tool_batch_cap") is not None
            else 4
        ),
        default_gmail_profile=request.environment.default_gmail_profile,
    )
    user_concept_id, org_concept_id = _resolve_user_context_ids(request.data)
    policy_state = request.data.get("policy_state")

    def _policy_state_needs_refresh(value: Any) -> bool:
        if not isinstance(value, _WorkflowModelPolicyState):
            return value is None
        if isinstance(value.policy, Mapping) and value.policy:
            return False
        return "workflow_model_policy_timeout" in {
            str(error) for error in (value.errors or ())
        }

    if _policy_state_needs_refresh(policy_state):

        def _timeout_workflow_model_policy(
            timeout_seconds: float,
        ) -> tuple[Any, Mapping[str, Any]]:
            return (
                _WorkflowModelPolicyState(
                    enabled=True,
                    policy=None,
                    policy_id=None,
                    predicate_id=None,
                    errors=("workflow_model_policy_timeout",),
                ),
                {
                    "type": "workflow_model_policy",
                    "enabled": True,
                    "policy_id": "",
                    "predicate_id": "",
                    "loaded": False,
                    "policy_source": "timeout",
                    "errors": ["workflow_model_policy_timeout"],
                    "timeout_seconds": timeout_seconds,
                },
            )

        refresh_reason = (
            "inherited_policy_timeout"
            if isinstance(policy_state, _WorkflowModelPolicyState)
            else None
        )
        policy_state, policy_telemetry = _run_gateway_runtime_setup_step(
            request,
            step_id="workflow_model_policy",
            step_label="Load workflow LLM model policy",
            operation=lambda: orchestrator._load_workflow_model_policy(None),
            timeout_seconds=_positive_float_env(
                "VON_WORKFLOW_MODEL_POLICY_TIMEOUT_SECONDS",
                8.0,
            ),
            timeout_fallback=_timeout_workflow_model_policy,
        )
        if isinstance(policy_telemetry, Mapping) and policy_telemetry:
            policy_telemetry_payload = dict(policy_telemetry)
            if refresh_reason:
                policy_telemetry_payload["refresh_reason"] = refresh_reason
            _append_workflow_llm_setup_diagnostic(request, policy_telemetry_payload)
    registry_snapshot_raw = request.data.get("registry_snapshot")
    if isinstance(registry_snapshot_raw, Mapping):
        registry_snapshot = registry_snapshot_raw
    else:

        def _timeout_registry_snapshot(timeout_seconds: float) -> Mapping[str, Any]:
            return {
                "source": "timeout",
                "models": [],
                "loaded": False,
                "errors": [
                    {
                        "failure_reason": "workflow_llm_step_setup_timeout",
                        "step_id": "model_registry_snapshot",
                        "timeout_seconds": timeout_seconds,
                    }
                ],
            }

        registry_snapshot_result = _run_gateway_runtime_setup_step(
            request,
            step_id="model_registry_snapshot",
            step_label="Load workflow LLM model registry snapshot",
            operation=get_model_registry_snapshot,
            timeout_seconds=_positive_float_env(
                "VON_MODEL_REGISTRY_SNAPSHOT_TIMEOUT_SECONDS",
                12.0,
            ),
            timeout_fallback=_timeout_registry_snapshot,
        )
        registry_snapshot = (
            registry_snapshot_result
            if isinstance(registry_snapshot_result, Mapping)
            else {}
        )
    # JVNAUTOSCI-2373: propagate the parent turn's dead-candidate cache into
    # this nested orchestrator so workflow LLM steps skip the same dead
    # provider/model combinations rather than re-discovering them stage by
    # stage. The parent stashes the cache in ``request.data`` under
    # ``turn_model_failures``; we install it on the nested orchestrator's
    # thread-local so its internal ``_run_llm_with_fallbacks`` callers find
    # it without extra plumbing.
    parent_turn_model_failures = request.data.get("turn_model_failures")
    if isinstance(parent_turn_model_failures, dict):
        orchestrator._turn_model_failures_local.cache = parent_turn_model_failures
    _check_request_cancellation(request)
    return (
        orchestrator,
        policy_state,
        registry_snapshot,
        user_concept_id,
        org_concept_id,
    )


def _selected_candidate_context_from_request(
    request: WorkflowActionRequest,
) -> dict[str, Any] | None:
    candidate: dict[str, Any] = {}
    requested_model = _context_string(
        request.data.get("requested_model")
        or getattr(request.environment, "model", None)
    )
    model_provider, _model_name = _split_model_provider_prefix(requested_model)
    provider = (
        _context_string(
            request.data.get("requested_client_type")
            or request.data.get("selected_model_provider")
            or request.data.get("model_provider")
        )
        or model_provider
        or _context_string(
            infer_llm_client_provider(getattr(request.environment, "llm_client", None))
        )
    )
    if provider:
        candidate["provider"] = provider
        if provider.lower() == "ollama":
            candidate.setdefault("locality", "local")
    return candidate or None


def _select_model_context_for_prompt_variant(
    *,
    request: WorkflowActionRequest,
    stage: str,
) -> tuple[
    str | None, Mapping[str, Any] | None, Mapping[str, Any] | None, dict[str, Any]
]:
    diagnostics: dict[str, Any] = {
        "source": "environment_default",
        "stage": stage,
    }
    selected_model = request.environment.model
    selected_candidate = _selected_candidate_context_from_request(request)
    registry_snapshot: Mapping[str, Any] | None = None

    if not request.environment.gateway:
        return selected_model, selected_candidate, registry_snapshot, diagnostics
    if _prefer_default_model_for_request(request):
        requested_model = _context_string(request.data.get("requested_model"))
        if requested_model:
            selected_model = requested_model
        diagnostics["source"] = "explicit_request_model"
        return selected_model, selected_candidate, registry_snapshot, diagnostics

    try:
        (
            orchestrator,
            policy_state,
            registry_snapshot,
            user_concept_id,
            org_concept_id,
        ) = _build_gateway_runtime(request)
        selector = getattr(orchestrator, "_select_model_for_stage", None)
        if callable(selector):
            selector_model = selector(
                stage=stage,
                default_model=request.environment.model,
                policy_state=policy_state,
                registry_snapshot=registry_snapshot,
                workflow_id=request.workflow_id,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
                prefer_default_model=_prefer_default_model_for_request(request),
            )
            selected_model = _context_string(selector_model) or selected_model
            diagnostics["source"] = "workflow_model_selector"
    except Exception as exc:
        diagnostics["source"] = "environment_default_after_selector_error"
        diagnostics["selector_error"] = str(exc)

    return selected_model, selected_candidate, registry_snapshot, diagnostics


def _build_result(
    *,
    request: WorkflowActionRequest,
    response_text: str,
    prompt_id: str | None,
    prompt_source: str | None,
    rendered_variables: Mapping[str, Any],
    llm_policy_map: Mapping[str, Any],
    validation_policy_map: Mapping[str, Any],
    selected_model: str | None,
    selected_candidate: Mapping[str, Any] | None,
    tool_invocations: Sequence[Mapping[str, Any]],
    tool_messages: Sequence[Mapping[str, Any]],
    llm_calls: Sequence[Mapping[str, Any]],
    aux_llm_calls: Sequence[Mapping[str, Any]],
    required_prompt_tools: Sequence[str] | None = None,
    required_tool_obligation_ledger: Mapping[str, Any] | None = None,
    max_tool_invocations: int | None = None,
    prompt_variant_selection: Mapping[str, Any] | None = None,
    prompt_context_diagnostics: Mapping[str, Any] | None = None,
) -> WorkflowActionResult:
    validated_outputs, validation_summary = _apply_validation_policy(
        request=request,
        response_text=response_text,
        prompt_id=prompt_id,
        selected_model=selected_model,
        validation_policy=validation_policy_map,
    )

    llm_step_envelope = {
        "execution_mode": "llm",
        "action_id": request.action_id,
        "workflow_id": request.workflow_id,
        "workflow_state_id": request.workflow_state_id,
        "policy_stage": _context_string(llm_policy_map.get("policy_stage")) or None,
        "base_prompt_id": (
            prompt_variant_selection.get("base_prompt_concept_id")
            if isinstance(prompt_variant_selection, Mapping)
            else prompt_id
        ),
        "selected_prompt_id": prompt_id,
        "selected_prompt_source": prompt_source,
        "selected_model": selected_model,
        "selected_model_candidate": (
            dict(selected_candidate)
            if isinstance(selected_candidate, Mapping)
            else None
        ),
        "tool_invocations": list(tool_invocations),
        "tool_messages": list(tool_messages),
        "rendered_prompt_variables": dict(rendered_variables),
        "selection_policy": _context_string(llm_policy_map.get("selection_policy"))
        or "adaptive",
        "allowed_tools": list(llm_policy_map.get("allowed_tools") or []),
        "llm_calls": list(llm_calls),
        "aux_llm_calls": list(aux_llm_calls),
        "validation": validation_summary,
        "completion_reason": (
            "tool_augmented_response" if tool_invocations else "direct_llm_response"
        ),
    }
    if required_prompt_tools:
        llm_step_envelope["required_prompt_tools"] = list(required_prompt_tools)
    if isinstance(required_tool_obligation_ledger, Mapping):
        llm_step_envelope["required_tool_obligation_ledger"] = dict(
            required_tool_obligation_ledger
        )
        llm_step_envelope["required_tool_obligation_blockers"] = list(
            required_tool_obligation_ledger.get("blocking_failure_codes") or []
        )
    if isinstance(validated_outputs.get("validated_json"), Mapping):
        llm_step_envelope["validated_json"] = dict(validated_outputs["validated_json"])
        llm_step_envelope["validated_json_parse_mode"] = validated_outputs.get(
            "validated_json_parse_mode"
        )
    if max_tool_invocations is not None:
        llm_step_envelope["max_tool_invocations"] = int(max_tool_invocations)
    if isinstance(prompt_variant_selection, Mapping) and prompt_variant_selection:
        llm_step_envelope["prompt_variant_selection"] = dict(prompt_variant_selection)
    if isinstance(prompt_context_diagnostics, Mapping) and prompt_context_diagnostics:
        llm_step_envelope["prompt_context_diagnostics"] = dict(
            prompt_context_diagnostics
        )

    project_raw_response = bool(
        llm_policy_map.get("project_raw_response_to_final_response", True)
    )
    llm_step_envelope["raw_response_projected_to_final_response"] = project_raw_response

    outputs = {
        "llm_step_response": response_text,
        "llm_step_envelope": llm_step_envelope,
        "tool_invocations": list(tool_invocations),
        "tool_messages": list(tool_messages),
        "llm_calls": list(llm_calls),
        "aux_llm_calls": list(aux_llm_calls),
    }
    if project_raw_response:
        outputs["final_response"] = response_text
    if required_prompt_tools:
        outputs["required_prompt_tools"] = list(required_prompt_tools)
    if isinstance(required_tool_obligation_ledger, Mapping):
        outputs["required_tool_obligation_ledger"] = dict(
            required_tool_obligation_ledger
        )
        outputs["required_tool_obligation_blockers"] = list(
            required_tool_obligation_ledger.get("blocking_failure_codes") or []
        )
        unsatisfied_tools = required_tool_obligation_ledger.get(
            "unsatisfied_required_tools"
        )
        if isinstance(unsatisfied_tools, list):
            outputs["missing_prompt_tools"] = list(unsatisfied_tools)
    if max_tool_invocations is not None:
        outputs["max_tool_invocations"] = int(max_tool_invocations)
    if isinstance(prompt_variant_selection, Mapping) and prompt_variant_selection:
        outputs["prompt_variant_selection"] = dict(prompt_variant_selection)
    if isinstance(prompt_context_diagnostics, Mapping) and prompt_context_diagnostics:
        outputs["prompt_context_diagnostics"] = dict(prompt_context_diagnostics)
    outputs.update(validated_outputs)
    validation_status = _context_string(validation_summary.get("status")).lower()
    if validation_status == "failed":
        return WorkflowActionResult(
            status="failed",
            outputs=outputs,
            error=_context_string(validation_summary.get("reason"))
            or "workflow_llm_step_validation_failed",
        )
    return WorkflowActionResult(outputs=outputs)


def _build_timeout_failure_result(
    *,
    request: WorkflowActionRequest,
    stage: str,
    prompt_id: str | None,
    prompt_source: str | None,
    rendered_variables: Mapping[str, Any],
    llm_policy_map: Mapping[str, Any],
    timeout_detail: str,
    selected_model: str | None,
    llm_calls: Sequence[Mapping[str, Any]],
    aux_llm_calls: Sequence[Mapping[str, Any]],
    selected_candidate: Mapping[str, Any] | None = None,
    timeout_seconds: float | None = None,
    prompt_variant_selection: Mapping[str, Any] | None = None,
    prompt_context_diagnostics: Mapping[str, Any] | None = None,
) -> WorkflowActionResult:
    timeout_call = next(
        (
            entry
            for entry in reversed(list(llm_calls))
            if isinstance(entry, Mapping)
            and entry.get("failure_kind") == "llm_call_timeout"
        ),
        None,
    )
    hard_guard_timeout_seconds = (
        timeout_call.get("hard_guard_timeout_seconds")
        if isinstance(timeout_call, Mapping)
        else None
    )
    llm_step_envelope = {
        "execution_mode": "llm",
        "action_id": request.action_id,
        "workflow_id": request.workflow_id,
        "workflow_state_id": request.workflow_state_id,
        "policy_stage": stage,
        "selected_prompt_id": prompt_id,
        "selected_prompt_source": prompt_source,
        "selected_model": selected_model,
        "selected_model_candidate": (
            dict(selected_candidate)
            if isinstance(selected_candidate, Mapping)
            else None
        ),
        "tool_invocations": [],
        "tool_messages": [],
        "rendered_prompt_variables": dict(rendered_variables),
        "selection_policy": _context_string(llm_policy_map.get("selection_policy"))
        or "adaptive",
        "allowed_tools": list(llm_policy_map.get("allowed_tools") or []),
        "llm_calls": list(llm_calls),
        "aux_llm_calls": list(aux_llm_calls),
        "validation": {
            "status": "skipped",
            "reason": "llm_step_timeout",
        },
        "completion_reason": "timeout",
        "timeout_stage": stage,
        "timeout_detail": timeout_detail,
        "timeout_seconds": hard_guard_timeout_seconds or timeout_seconds,
        "advisory_timeout_seconds": timeout_seconds,
        "hard_guard_timeout_seconds": hard_guard_timeout_seconds,
        "timeout_failure_kind": "llm_call_timeout",
        "fail_closed": True,
        "fallback_used": False,
        "fallback_policy": "none",
    }
    if isinstance(prompt_variant_selection, Mapping) and prompt_variant_selection:
        llm_step_envelope["base_prompt_id"] = prompt_variant_selection.get(
            "base_prompt_concept_id"
        )
        llm_step_envelope["prompt_variant_selection"] = dict(prompt_variant_selection)
    if isinstance(prompt_context_diagnostics, Mapping) and prompt_context_diagnostics:
        llm_step_envelope["prompt_context_diagnostics"] = dict(
            prompt_context_diagnostics
        )
    outputs: dict[str, Any] = {
        "llm_step_envelope": llm_step_envelope,
        "llm_calls": list(llm_calls),
        "aux_llm_calls": list(aux_llm_calls),
    }
    if isinstance(prompt_context_diagnostics, Mapping) and prompt_context_diagnostics:
        outputs["prompt_context_diagnostics"] = dict(prompt_context_diagnostics)
    return WorkflowActionResult(
        status="failed",
        outputs=outputs,
        error=f"workflow_llm_step_timeout:{timeout_detail}",
    )


def _build_structured_tool_transport_failure_result(
    *,
    request: WorkflowActionRequest,
    stage: str,
    prompt_id: str | None,
    prompt_source: str | None,
    rendered_variables: Mapping[str, Any],
    llm_policy_map: Mapping[str, Any],
    error: StructuredToolTransportError,
    selected_model: str | None,
    llm_calls: Sequence[Mapping[str, Any]],
    aux_llm_calls: Sequence[Mapping[str, Any]],
    tool_invocations: Sequence[Mapping[str, Any]] = (),
    tool_messages: Sequence[Mapping[str, Any]] = (),
    selected_candidate: Mapping[str, Any] | None = None,
    prompt_variant_selection: Mapping[str, Any] | None = None,
    prompt_context_diagnostics: Mapping[str, Any] | None = None,
) -> WorkflowActionResult:
    """Project a typed transport blocker into the durable LLM-step record."""

    decision = dict(getattr(error, "decision", {}) or {})
    failure_kind = _context_string(
        getattr(error, "failure_kind", "structured_tool_transport_error")
    )
    retryable = bool(getattr(error, "retryable", False))
    advertised_alternatives = [
        item
        for item in decision.get("advertised_alternatives") or []
        if isinstance(item, str) and item.strip()
    ]
    llm_step_envelope = {
        "execution_mode": "llm",
        "action_id": request.action_id,
        "workflow_id": request.workflow_id,
        "workflow_state_id": request.workflow_state_id,
        "policy_stage": stage,
        "selected_prompt_id": prompt_id,
        "selected_prompt_source": prompt_source,
        "selected_model": selected_model,
        "selected_model_candidate": (
            dict(selected_candidate)
            if isinstance(selected_candidate, Mapping)
            else None
        ),
        "tool_invocations": [
            dict(item) for item in tool_invocations if isinstance(item, Mapping)
        ],
        "tool_messages": [
            dict(item) for item in tool_messages if isinstance(item, Mapping)
        ],
        "rendered_prompt_variables": dict(rendered_variables),
        "selection_policy": _context_string(llm_policy_map.get("selection_policy"))
        or "adaptive",
        "allowed_tools": list(llm_policy_map.get("allowed_tools") or []),
        "llm_calls": list(llm_calls),
        "aux_llm_calls": list(aux_llm_calls),
        "validation": {
            "status": "skipped",
            "reason": "structured_tool_transport_blocked",
        },
        "completion_reason": "structured_tool_transport_blocked",
        "failure_kind": failure_kind,
        "retryable": retryable,
        "transport_decision": decision,
        "recovery_affordances": {
            "advertised_api_surface_alternatives": advertised_alternatives,
            "profile_concept_id": decision.get("profile_concept_id"),
            "capability_source": decision.get("capability_source"),
        },
        "fail_closed": True,
        "fallback_used": False,
        "fallback_policy": "represented_surface_only",
    }
    if isinstance(prompt_variant_selection, Mapping) and prompt_variant_selection:
        llm_step_envelope["base_prompt_id"] = prompt_variant_selection.get(
            "base_prompt_concept_id"
        )
        llm_step_envelope["prompt_variant_selection"] = dict(prompt_variant_selection)
    if isinstance(prompt_context_diagnostics, Mapping) and prompt_context_diagnostics:
        llm_step_envelope["prompt_context_diagnostics"] = dict(
            prompt_context_diagnostics
        )
    outputs: dict[str, Any] = {
        "llm_step_envelope": llm_step_envelope,
        "llm_calls": list(llm_calls),
        "aux_llm_calls": list(aux_llm_calls),
        "structured_tool_transport_blocker": decision,
        "tool_invocations": list(llm_step_envelope["tool_invocations"]),
        "tool_messages": list(llm_step_envelope["tool_messages"]),
    }
    if isinstance(prompt_context_diagnostics, Mapping) and prompt_context_diagnostics:
        outputs["prompt_context_diagnostics"] = dict(prompt_context_diagnostics)
    return WorkflowActionResult(
        status="failed",
        outputs=outputs,
        error=f"workflow_structured_tool_transport_blocked:{failure_kind}",
    )


def _run_direct_llm_step(
    *,
    request: WorkflowActionRequest,
    stage: str,
    prompt_id: str | None,
    prompt_source: str | None,
    rendered_prompt: str,
    rendered_variables: Mapping[str, Any],
    llm_policy_map: Mapping[str, Any],
    validation_policy_map: Mapping[str, Any],
    prompt_variant_selection: Mapping[str, Any] | None = None,
    prompt_context_diagnostics: Mapping[str, Any] | None = None,
) -> WorkflowActionResult:
    llm_client = request.environment.llm_client
    if llm_client is None or not hasattr(llm_client, "generate"):
        return WorkflowActionResult(
            status="failed",
            error="workflow_llm_step_client_unavailable",
        )

    context_messages_key = _context_string(
        llm_policy_map.get("context_messages_context_key")
    )
    context_messages, context_messages_diagnostics = (
        _bounded_context_messages_for_policy(
            raw_value=request.data.get(context_messages_key),
            llm_policy_map=llm_policy_map,
            context_messages_key=context_messages_key,
        )
    )
    prompt_context_diagnostics = _merge_prompt_context_diagnostics(
        prompt_context_diagnostics,
        context_messages_diagnostics=context_messages_diagnostics,
    )
    model_name = request.environment.model
    timeout_override_sec = _conversation_turn_llm_timeout_override_sec(request)
    llm_calls = cast(
        list[dict[str, Any]],
        (
            request.data.get("llm_calls")
            if isinstance(request.data.get("llm_calls"), list)
            else []
        ),
    )
    if "llm_calls" not in request.data:
        request.data["llm_calls"] = llm_calls
    selected_candidate = _selected_candidate_context_from_request(request)
    start = time.perf_counter()
    try:
        response = _run_llm_call_with_timeout(
            request,
            operation=lambda: llm_client.generate(
                rendered_prompt,
                context=context_messages or None,
                model=model_name,
                llm_params=_model_parameters_with_timeout(
                    _default_model_parameters_for_request(request),
                    timeout_override_sec,
                ),
            ),
            stage=stage,
            prompt_id=prompt_id,
            selected_model=model_name,
            selected_candidate=selected_candidate,
            timeout_seconds=timeout_override_sec,
            llm_calls=llm_calls,
        )
    except TimeoutError as exc:
        timeout_detail = _context_string(str(exc)) or "llm_call_timed_out"
        return _build_timeout_failure_result(
            request=request,
            stage=stage,
            prompt_id=prompt_id,
            prompt_source=prompt_source,
            rendered_variables=rendered_variables,
            llm_policy_map=llm_policy_map,
            timeout_detail=timeout_detail,
            selected_model=model_name,
            llm_calls=llm_calls,
            aux_llm_calls=request.data.get("aux_llm_calls") or [],
            selected_candidate=selected_candidate,
            timeout_seconds=timeout_override_sec,
            prompt_variant_selection=prompt_variant_selection,
            prompt_context_diagnostics=prompt_context_diagnostics,
        )
    except StructuredToolTransportError as exc:
        return _build_structured_tool_transport_failure_result(
            request=request,
            stage=stage,
            prompt_id=prompt_id,
            prompt_source=prompt_source,
            rendered_variables=rendered_variables,
            llm_policy_map=llm_policy_map,
            error=exc,
            selected_model=model_name,
            llm_calls=llm_calls,
            aux_llm_calls=request.data.get("aux_llm_calls") or [],
            selected_candidate=selected_candidate,
            prompt_variant_selection=prompt_variant_selection,
            prompt_context_diagnostics=prompt_context_diagnostics,
        )
    duration_ms = (time.perf_counter() - start) * 1000.0
    llm_call_entry = _append_llm_call(
        request.data,
        call_type="llm.generate",
        stage=stage,
        model_name=model_name,
        duration_ms=duration_ms,
        note="generic_llm_step_direct",
    )
    if isinstance(prompt_context_diagnostics, Mapping) and prompt_context_diagnostics:
        llm_call_entry["prompt_context_diagnostics"] = dict(prompt_context_diagnostics)
    _record_workflow_llm_duration_for_entry(
        request,
        llm_call_entry,
        stage=stage,
        model_name=model_name,
        duration_ms=duration_ms,
        workflow_stage_id=request.workflow_state_id,
    )

    response_text = response if isinstance(response, str) else str(response)
    return _build_result(
        request=request,
        response_text=response_text,
        prompt_id=prompt_id,
        prompt_source=prompt_source,
        rendered_variables=rendered_variables,
        llm_policy_map=llm_policy_map,
        validation_policy_map=validation_policy_map,
        selected_model=model_name,
        selected_candidate=None,
        tool_invocations=(),
        tool_messages=(),
        llm_calls=request.data.get("llm_calls") or [],
        aux_llm_calls=request.data.get("aux_llm_calls") or [],
        prompt_variant_selection=prompt_variant_selection,
        prompt_context_diagnostics=prompt_context_diagnostics,
    )


def _run_gateway_llm_step_no_tools(
    *,
    request: WorkflowActionRequest,
    stage: str,
    prompt_id: str | None,
    prompt_source: str | None,
    rendered_prompt: str,
    rendered_variables: Mapping[str, Any],
    llm_policy_map: Mapping[str, Any],
    validation_policy_map: Mapping[str, Any],
    prompt_variant_selection: Mapping[str, Any] | None = None,
    prompt_context_diagnostics: Mapping[str, Any] | None = None,
) -> WorkflowActionResult:
    orchestrator, policy_state, registry_snapshot, user_concept_id, org_concept_id = (
        _build_gateway_runtime(request)
    )
    prefer_default_model = _prefer_default_model_for_request(request)
    timeout_override_sec = _conversation_turn_llm_timeout_override_sec(request)
    llm_calls: list[dict[str, Any]] = []
    aux_llm_calls = cast(
        list[dict[str, Any]],
        (
            request.data.get("aux_llm_calls")
            if isinstance(request.data.get("aux_llm_calls"), list)
            else []
        ),
    )
    context_messages_key = _context_string(
        llm_policy_map.get("context_messages_context_key")
    )
    context_messages, context_messages_diagnostics = (
        _bounded_context_messages_for_policy(
            raw_value=request.data.get(context_messages_key),
            llm_policy_map=llm_policy_map,
            context_messages_key=context_messages_key,
        )
    )
    prompt_context_diagnostics = _merge_prompt_context_diagnostics(
        prompt_context_diagnostics,
        context_messages_diagnostics=context_messages_diagnostics,
    )
    context_lineage_key = _context_string(
        llm_policy_map.get("context_lineage_context_key")
    )
    context_telemetry = _coerce_context_telemetry(request.data.get(context_lineage_key))
    emit_progress = (
        request.data.get("emit_progress")
        if callable(request.data.get("emit_progress"))
        else None
    )
    heartbeat_progress = _nonblocking_progress_callback(emit_progress)
    check_cancellation = (
        request.data.get("check_cancellation")
        if callable(request.data.get("check_cancellation"))
        else None
    )
    last_validation_failure: dict[str, Any] | None = None
    last_validation_response_text: str | None = None
    last_validation_model: str | None = None
    last_validation_candidate: Mapping[str, Any] | None = None
    timeout_selected_model = request.environment.model
    timeout_selected_candidate = _selected_candidate_context_from_request(request)
    try:
        selector = getattr(orchestrator, "_select_model_for_stage", None)
        if callable(selector):
            timeout_selected_model = (
                _context_string(
                    selector(
                        stage=stage,
                        default_model=request.environment.model,
                        policy_state=policy_state,
                        registry_snapshot=registry_snapshot,
                        workflow_id=request.workflow_id,
                        user_concept_id=user_concept_id,
                        org_concept_id=org_concept_id,
                        prefer_default_model=prefer_default_model,
                    )
                )
                or timeout_selected_model
            )
    except Exception:
        pass

    def _validate_candidate_response(
        response_text: str,
        model_name: str | None,
        candidate: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        nonlocal last_validation_failure
        nonlocal last_validation_response_text
        nonlocal last_validation_model
        nonlocal last_validation_candidate

        _validated_outputs, validation_summary = _apply_validation_policy(
            request=request,
            response_text=response_text,
            prompt_id=prompt_id,
            selected_model=model_name,
            validation_policy=validation_policy_map,
        )
        validation_status = _context_string(validation_summary.get("status")).lower()
        if validation_status != "failed":
            return None

        validation_failure = {
            "reason": _context_string(validation_summary.get("reason"))
            or "workflow_llm_step_validation_failed",
            "error_class": "WorkflowLLMStepValidationError",
            "failure_kind": "response_validation_failed",
            "validation": dict(validation_summary),
        }
        last_validation_failure = dict(validation_failure)
        last_validation_response_text = response_text
        last_validation_model = model_name
        last_validation_candidate = dict(candidate)
        return validation_failure

    def _record_llm_call(
        *,
        call_type: str,
        model_name: str | None,
        duration_ms: float | None,
        usage: Mapping[str, Any] | None = None,
        note: str | None = None,
        stage: str | None = None,
        provider: str | None = None,
        candidate: Mapping[str, Any] | None = None,
        workflow_stage_id: str | None = None,
        call_id: str | None = None,
        exchange_blob_ref: Mapping[str, Any] | None = None,
        status: str | None = None,
        success: bool | None = None,
        error: str | None = None,
        error_class: str | None = None,
        failure_kind: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "type": call_type,
            "model_name": model_name,
            "duration_ms": duration_ms,
            "stage": stage,
        }
        if isinstance(call_id, str) and call_id.strip():
            entry["call_id"] = call_id.strip()
        if usage:
            entry["usage"] = dict(usage)
        if note:
            entry["note"] = note
        if provider:
            entry["provider"] = provider
        if isinstance(candidate, Mapping):
            entry["candidate"] = dict(candidate)
        if isinstance(workflow_stage_id, str) and workflow_stage_id.strip():
            entry["workflow_stage_id"] = workflow_stage_id.strip()
        if isinstance(call_id, str) and call_id.strip():
            entry["call_id"] = call_id.strip()
        if isinstance(exchange_blob_ref, Mapping) and exchange_blob_ref:
            entry["exchange_blob_ref"] = dict(exchange_blob_ref)
        if isinstance(status, str) and status.strip():
            entry["status"] = status.strip()
        if isinstance(success, bool):
            entry["success"] = success
        if isinstance(error, str) and error.strip():
            entry["error"] = error.strip()
        if isinstance(error_class, str) and error_class.strip():
            entry["error_class"] = error_class.strip()
        if isinstance(failure_kind, str) and failure_kind.strip():
            entry["failure_kind"] = failure_kind.strip()
        if (
            isinstance(prompt_context_diagnostics, Mapping)
            and prompt_context_diagnostics
        ):
            entry["prompt_context_diagnostics"] = dict(prompt_context_diagnostics)
        stamp_llm_call_timestamps(entry, duration_ms=duration_ms)
        llm_calls.append(entry)
        _record_workflow_llm_duration_for_entry(
            request,
            entry,
            stage=stage,
            model_name=model_name,
            provider=provider,
            duration_ms=duration_ms,
            workflow_stage_id=workflow_stage_id,
        )

    try:
        response_text, selected_model, selected_candidate = _run_llm_call_with_timeout(
            request,
            stage=stage,
            prompt_id=prompt_id,
            selected_model=timeout_selected_model,
            selected_candidate=timeout_selected_candidate,
            timeout_seconds=_conversation_turn_llm_fallback_guard_timeout_sec(
                timeout_override_sec
            ),
            llm_calls=llm_calls,
            operation=lambda: orchestrator._run_llm_with_fallbacks(
                stage=stage,
                prompt=rendered_prompt,
                context=context_messages,
                default_client=request.environment.llm_client,
                default_model=request.environment.model,
                default_model_parameters=_default_model_parameters_for_request(request),
                policy_state=policy_state,
                registry_snapshot=registry_snapshot,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
                llm_calls_log=llm_calls,
                aux_log=aux_llm_calls,
                record_llm_call=_record_llm_call,
                emit_progress=heartbeat_progress,
                context_telemetry=context_telemetry,
                prefer_default_model=prefer_default_model,
                timeout_override_sec=timeout_override_sec,
                check_cancellation=check_cancellation,
                response_validator=_validate_candidate_response,
            ),
        )
    except TimeoutError as exc:
        timeout_detail = _context_string(str(exc)) or "llm_call_timed_out"
        return _build_timeout_failure_result(
            request=request,
            stage=stage,
            prompt_id=prompt_id,
            prompt_source=prompt_source,
            rendered_variables=rendered_variables,
            llm_policy_map=llm_policy_map,
            timeout_detail=timeout_detail,
            selected_model=timeout_selected_model,
            llm_calls=llm_calls,
            aux_llm_calls=aux_llm_calls,
            selected_candidate=timeout_selected_candidate,
            timeout_seconds=timeout_override_sec,
            prompt_variant_selection=prompt_variant_selection,
            prompt_context_diagnostics=prompt_context_diagnostics,
        )
    except RuntimeError as exc:
        exc_text = str(exc)
        all_candidates_failed_prefix = f"all_model_candidates_failed:stage={stage}:"
        failure_entries = (
            [
                entry
                for entry in exc_text[len(all_candidates_failed_prefix) :].split(",")
                if entry
            ]
            if exc_text.startswith(all_candidates_failed_prefix)
            else []
        )
        if (
            failure_entries
            and all(
                entry.endswith("=response_validation_failed")
                for entry in failure_entries
            )
            and last_validation_failure is not None
            and last_validation_response_text is not None
        ):
            result = _build_result(
                request=request,
                response_text=last_validation_response_text,
                prompt_id=prompt_id,
                prompt_source=prompt_source,
                rendered_variables=rendered_variables,
                llm_policy_map=llm_policy_map,
                validation_policy_map=validation_policy_map,
                selected_model=last_validation_model,
                selected_candidate=last_validation_candidate,
                tool_invocations=(),
                tool_messages=(),
                llm_calls=llm_calls,
                aux_llm_calls=aux_llm_calls,
                prompt_variant_selection=prompt_variant_selection,
                prompt_context_diagnostics=prompt_context_diagnostics,
            )
            envelope = result.outputs.get("llm_step_envelope")
            if isinstance(envelope, MutableMapping):
                envelope["validation_fallback_exhausted"] = True
                envelope["validation_fallback_error"] = exc_text
                envelope["last_validation_failure"] = dict(last_validation_failure)
            return result
        raise

    return _build_result(
        request=request,
        response_text=response_text,
        prompt_id=prompt_id,
        prompt_source=prompt_source,
        rendered_variables=rendered_variables,
        llm_policy_map=llm_policy_map,
        validation_policy_map=validation_policy_map,
        selected_model=selected_model,
        selected_candidate=selected_candidate,
        tool_invocations=(),
        tool_messages=(),
        llm_calls=llm_calls,
        aux_llm_calls=aux_llm_calls,
        prompt_variant_selection=prompt_variant_selection,
        prompt_context_diagnostics=prompt_context_diagnostics,
    )


def _execute_llm_step_inner(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Execute a generic LLM workflow step."""

    llm_policy_map = (
        dict(request.llm_policy) if isinstance(request.llm_policy, Mapping) else {}
    )
    validation_policy_map = (
        dict(request.validation_policy)
        if isinstance(request.validation_policy, Mapping)
        else {}
    )

    stage = _llm_stage(llm_policy_map, request)
    agent_test_fast_path_result = _agent_test_conversation_turn_fast_path_result(
        request=request,
        stage=stage,
        llm_policy_map=llm_policy_map,
        validation_policy_map=validation_policy_map,
    )
    if agent_test_fast_path_result is not None:
        return agent_test_fast_path_result

    runtime_context_hydration = _hydrate_authored_runtime_context_fields(
        request=request,
        llm_policy=llm_policy_map,
    )

    prompt_id, base_prompt_text, rendered_variables, prompt_source = (
        _resolve_prompt_render(
            prompt_contract=request.prompt_contract,
            llm_policy=llm_policy_map,
            context=request.data,
        )
    )
    if not prompt_id:
        prompt_id = _diagnostic_prompt_id_from_contract(request.prompt_contract)
    if not isinstance(base_prompt_text, str) or not base_prompt_text.strip():
        return WorkflowActionResult(
            status="failed",
            error="workflow_llm_step_prompt_render_failed",
        )

    (
        prompt_variant_model,
        prompt_variant_candidate,
        prompt_variant_registry,
        prompt_variant_model_selection,
    ) = _select_model_context_for_prompt_variant(request=request, stage=stage)
    prompt_variant_resolution = resolve_model_prompt_variant(
        base_prompt_concept_id=prompt_id,
        base_prompt_text=base_prompt_text,
        variables=rendered_variables,
        selected_model=prompt_variant_model,
        selected_candidate=prompt_variant_candidate,
        registry_snapshot=prompt_variant_registry,
    )
    prompt_variant_selection = dict(prompt_variant_resolution.diagnostics)
    prompt_variant_selection["model_selection"] = dict(prompt_variant_model_selection)
    if (
        isinstance(prompt_variant_resolution.prompt_text, str)
        and prompt_variant_resolution.prompt_text.strip()
    ):
        base_prompt_text = prompt_variant_resolution.prompt_text
        rendered_variables = dict(prompt_variant_resolution.rendered_variables)
        selected_variant_prompt_id = _context_string(
            prompt_variant_resolution.selected_prompt_concept_id
        )
        if selected_variant_prompt_id:
            prompt_id = selected_variant_prompt_id
        if prompt_variant_resolution.match_reason != "base_prompt":
            prompt_source = f"{prompt_source or 'prompt'}:model_variant"

    rendered_prompt, prompt_context_diagnostics = _compose_llm_prompt_with_diagnostics(
        base_prompt=base_prompt_text,
        llm_policy=llm_policy_map,
        context=request.data,
        workflow_state_id=request.workflow_state_id,
    )
    prompt_context_diagnostics = dict(prompt_context_diagnostics or {})
    if runtime_context_hydration is not None:
        prompt_context_diagnostics["runtime_context_hydration"] = (
            runtime_context_hydration
        )
    prompt_context_diagnostics["prompt_content_sha256"] = hashlib.sha256(
        base_prompt_text.encode("utf-8")
    ).hexdigest()
    if prompt_id:
        prompt_context_diagnostics["resolved_prompt_concept_id"] = prompt_id
    if not rendered_prompt:
        return WorkflowActionResult(
            status="failed",
            error="workflow_llm_step_prompt_render_failed",
        )

    missing_narration_tools = _missing_prompt_tools_for_completion_report_narration(
        request,
        prompt_id=prompt_id,
    )
    if missing_narration_tools:
        return _build_skipped_completion_report_narration_result(
            request=request,
            stage=stage,
            prompt_id=prompt_id,
            prompt_source=prompt_source,
            rendered_variables=rendered_variables,
            llm_policy_map=llm_policy_map,
            validation_policy_map=validation_policy_map,
            missing_prompt_tools=missing_narration_tools,
        )

    emit_phase_transition = (
        request.data.get("emit_phase_transition")
        if callable(request.data.get("emit_phase_transition"))
        else None
    )
    telemetry_phase = _conversation_turn_llm_phase_override(request)
    if callable(emit_phase_transition) and telemetry_phase:
        emit_phase_transition(
            telemetry_phase,
            extra={
                "result_summary": (
                    "Running the authoritative LLM reasoning step for this turn stage"
                ),
                "workflow_state_id": telemetry_phase,
            },
        )
    if not request.environment.gateway:
        return _run_direct_llm_step(
            request=request,
            stage=stage,
            prompt_id=prompt_id,
            prompt_source=prompt_source,
            rendered_prompt=rendered_prompt,
            rendered_variables=rendered_variables,
            llm_policy_map=llm_policy_map,
            validation_policy_map=validation_policy_map,
            prompt_variant_selection=prompt_variant_selection,
            prompt_context_diagnostics=prompt_context_diagnostics,
        )

    if _tool_mode(llm_policy_map) != "allowed":
        return _run_gateway_llm_step_no_tools(
            request=request,
            stage=stage,
            prompt_id=prompt_id,
            prompt_source=prompt_source,
            rendered_prompt=rendered_prompt,
            rendered_variables=rendered_variables,
            llm_policy_map=llm_policy_map,
            validation_policy_map=validation_policy_map,
            prompt_variant_selection=prompt_variant_selection,
            prompt_context_diagnostics=prompt_context_diagnostics,
        )

    allowed_tools_raw = llm_policy_map.get("allowed_tools")
    allowed_tools = (
        [
            str(tool_name).strip()
            for tool_name in allowed_tools_raw
            if str(tool_name).strip()
        ]
        if isinstance(allowed_tools_raw, Sequence)
        and not isinstance(allowed_tools_raw, (str, bytes, bytearray))
        else None
    )
    turn_expected_outcome_contract = _resolve_turn_expected_outcome_contract(
        request.data
    )
    method_catalogue = request.environment.gateway.describe_methods()
    initial_invocations_for_contract = (
        request.data.get("invocations")
        if isinstance(request.data.get("invocations"), list)
        else ()
    )

    from ..integrations.internal_mcp.orchestrator import InternalMCPChatOrchestrator

    infer_turn_contract_required_tools = getattr(
        InternalMCPChatOrchestrator,
        "_infer_turn_contract_required_tools",
        None,
    )
    contract_required_tools = (
        infer_turn_contract_required_tools(
            turn_expected_outcome_contract=turn_expected_outcome_contract,
            method_catalogue=(
                method_catalogue if isinstance(method_catalogue, Mapping) else None
            ),
            allowed_tools=allowed_tools,
            tool_invocations=initial_invocations_for_contract,
        )
        if callable(infer_turn_contract_required_tools)
        else turn_expected_outcome_contract.required_tools
    )
    required_tool_sources = {
        "request_required_prompt_tools": request.data.get("required_prompt_tools"),
        "llm_policy_required_tools": llm_policy_map.get("required_tools"),
        "turn_expected_outcome_contract": (
            contract_required_tools or turn_expected_outcome_contract.required_tools
        ),
    }
    required_obligation_tools = _merge_required_prompt_tools(
        request.data.get("required_prompt_tools"),
        llm_policy_map.get("required_tools"),
        contract_required_tools or turn_expected_outcome_contract.required_tools,
    )
    required_prompt_tools = _filter_tool_names_to_allowed_set(
        required_obligation_tools,
        allowed_tools,
    )
    max_tool_invocations = _resolve_llm_step_max_tool_invocations(
        request=request,
        llm_policy=llm_policy_map,
        required_prompt_tools=required_prompt_tools,
        required_obligation_tools=required_obligation_tools,
    )

    orchestrator, policy_state, registry_snapshot, user_concept_id, org_concept_id = (
        _build_gateway_runtime(request, max_tool_invocations=max_tool_invocations)
    )

    llm_calls = cast(
        list[dict[str, Any]],
        (
            request.data.get("llm_calls")
            if isinstance(request.data.get("llm_calls"), list)
            else []
        ),
    )
    aux_llm_calls = cast(
        list[dict[str, Any]],
        (
            request.data.get("aux_llm_calls")
            if isinstance(request.data.get("aux_llm_calls"), list)
            else []
        ),
    )
    invocations = cast(
        list[dict[str, Any]],
        (
            request.data.get("invocations")
            if isinstance(request.data.get("invocations"), list)
            else []
        ),
    )
    initial_required_tool_obligation_ledger = build_required_tool_obligation_ledger(
        required_tools_by_source=required_tool_sources,
        invocations=invocations,
        tool_call_validation_failure_context=(
            _tool_call_validation_failure_context_from_data(request.data)
        ),
        allowed_tools=allowed_tools,
        method_catalogue=(
            method_catalogue if isinstance(method_catalogue, Mapping) else None
        ),
        max_tool_invocations=max_tool_invocations,
        target_contract_state=target_contract_state_from_context(request.data),
        existing_ledger=(
            request.data.get("required_tool_obligation_ledger")
            if isinstance(request.data.get("required_tool_obligation_ledger"), Mapping)
            else None
        ),
    )
    tool_messages = cast(
        list[dict[str, Any]],
        (
            request.data.get("tool_messages")
            if isinstance(request.data.get("tool_messages"), list)
            else []
        ),
    )
    prefer_default_model = _prefer_default_model_for_request(request)
    selected_models: dict[str, str | None] = {}

    def _model_for_stage(inner_stage: str) -> Optional[str]:
        model_name = orchestrator._select_model_for_stage(
            stage=inner_stage,
            default_model=request.environment.model,
            policy_state=policy_state,
            registry_snapshot=registry_snapshot,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
            prefer_default_model=prefer_default_model,
        )
        selected_models[inner_stage] = model_name
        return model_name

    timeout_override_sec = _conversation_turn_llm_timeout_override_sec(request)
    timeout_selected_model = request.environment.model
    timeout_selected_candidate = _selected_candidate_context_from_request(request)
    try:
        timeout_selected_model = (
            _context_string(_model_for_stage(stage)) or timeout_selected_model
        )
    except Exception:
        pass

    def _record_llm_call(
        *,
        call_type: str,
        model_name: str | None,
        duration_ms: float | None,
        usage: Mapping[str, Any] | None = None,
        note: str | None = None,
        stage: str | None = None,
        provider: str | None = None,
        candidate: Mapping[str, Any] | None = None,
        workflow_stage_id: str | None = None,
        call_id: str | None = None,
        exchange_blob_ref: Mapping[str, Any] | None = None,
        status: str | None = None,
        success: bool | None = None,
        error: str | None = None,
        error_class: str | None = None,
        failure_kind: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "type": call_type,
            "model_name": model_name,
            "duration_ms": duration_ms,
            "stage": stage,
        }
        if isinstance(call_id, str) and call_id.strip():
            entry["call_id"] = call_id.strip()
        if usage:
            entry["usage"] = dict(usage)
        if note:
            entry["note"] = note
        if provider:
            entry["provider"] = provider
        if isinstance(candidate, Mapping):
            entry["candidate"] = dict(candidate)
        if isinstance(workflow_stage_id, str) and workflow_stage_id.strip():
            entry["workflow_stage_id"] = workflow_stage_id.strip()
        if isinstance(call_id, str) and call_id.strip():
            entry["call_id"] = call_id.strip()
        if isinstance(exchange_blob_ref, Mapping) and exchange_blob_ref:
            entry["exchange_blob_ref"] = dict(exchange_blob_ref)
        if isinstance(status, str) and status.strip():
            entry["status"] = status.strip()
        if isinstance(success, bool):
            entry["success"] = success
        if isinstance(error, str) and error.strip():
            entry["error"] = error.strip()
        if isinstance(error_class, str) and error_class.strip():
            entry["error_class"] = error_class.strip()
        if isinstance(failure_kind, str) and failure_kind.strip():
            entry["failure_kind"] = failure_kind.strip()
        if (
            isinstance(prompt_context_diagnostics, Mapping)
            and prompt_context_diagnostics
        ):
            entry["prompt_context_diagnostics"] = dict(prompt_context_diagnostics)
        stamp_llm_call_timestamps(entry, duration_ms=duration_ms)
        llm_calls.append(entry)
        _record_workflow_llm_duration_for_entry(
            request,
            entry,
            stage=stage,
            model_name=model_name,
            provider=provider,
            duration_ms=duration_ms,
            workflow_stage_id=workflow_stage_id,
        )

    def _build_simple_error_result(
        kind: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "response_text": _context_string(payload.get("message"))
            or _context_string(payload.get("error"))
            or kind,
            "details": dict(payload),
        }

    shared_data: dict[str, Any] = {
        str(key): value for key, value in request.data.items() if isinstance(key, str)
    }
    workflow_step_output_format = _validation_output_format(validation_policy_map)
    workflow_step_output_contract: dict[str, Any] | None = None
    if workflow_step_output_format:
        workflow_step_output_contract = {
            "schema_version": "workflow_step_output_contract.v1",
            "output_format": workflow_step_output_format,
            "prompt_concept_id": prompt_id,
            "workflow_id": request.workflow_id,
            "workflow_state_id": request.workflow_state_id,
            "response_contract_text": _context_string(
                llm_policy_map.get("response_contract_text")
            )
            or None,
            "required_json_fields": list(_json_required_fields(validation_policy_map)),
            "json_field_defaults": dict(_json_field_defaults(validation_policy_map)),
        }
    shared_data.update(
        {
            "prompt": rendered_prompt,
            "prompt_for_requirements": request.data.get("prompt_for_requirements")
            or rendered_prompt,
            "prompt_requirement_url_policy": request.data.get(
                "prompt_requirement_url_policy"
            ),
            "required_prompt_tools": required_prompt_tools,
            "required_tool_obligation_tools": list(required_obligation_tools),
            "required_tool_obligation_sources": required_tool_sources,
            "required_tool_obligation_ledger": initial_required_tool_obligation_ledger,
            "required_tool_obligation_blockers": list(
                initial_required_tool_obligation_ledger.get("blocking_failure_codes")
                or []
            ),
            "tool_argument_defaults": _normalise_tool_argument_defaults(
                llm_policy_map.get("tool_argument_defaults")
            ),
            "required_prompt_url_extraction_tool": request.data.get(
                "required_prompt_url_extraction_tool"
            ),
            "required_prompt_url_extraction_url": request.data.get(
                "required_prompt_url_extraction_url"
            ),
            "required_prompt_fetch_concept_ids": (
                request.data.get("required_prompt_fetch_concept_ids") or []
            ),
            "required_prompt_read_file_copy_ids": (
                request.data.get("required_prompt_read_file_copy_ids") or []
            ),
            "required_prompt_scholarly_representation_for_file_copy_ids": (
                request.data.get(
                    "required_prompt_scholarly_representation_for_file_copy_ids"
                )
                or []
            ),
            "required_prompt_create_type_name": request.data.get(
                "required_prompt_create_type_name"
            ),
            "missing_prompt_tools": request.data.get("missing_prompt_tools") or [],
            "missing_prompt_fetch_concept_ids": (
                request.data.get("missing_prompt_fetch_concept_ids") or []
            ),
            "missing_prompt_read_file_copy_ids": (
                request.data.get("missing_prompt_read_file_copy_ids") or []
            ),
            "missing_prompt_scholarly_representation_for_file_copy_ids": (
                request.data.get(
                    "missing_prompt_scholarly_representation_for_file_copy_ids"
                )
                or []
            ),
            "missing_tool_call_retry_reason_override": request.data.get(
                "missing_tool_call_retry_reason_override"
            ),
            "response": request.data.get("response")
            or request.data.get("current_response")
            or "",
            "current_response": request.data.get("current_response") or "",
            "augmented_context": _coerce_context_messages(
                request.data.get("augmented_context")
            ),
            "policy_state": policy_state,
            "registry_snapshot": registry_snapshot,
            "user_concept_id": user_concept_id,
            "org_concept_id": org_concept_id,
            "conversation_session_id": request.data.get("conversation_session_id"),
            "turn_id": request.data.get("turn_id"),
            "recent_user_prompts": request.data.get("recent_user_prompts") or [],
            "gmail_profile": request.data.get("gmail_profile")
            or request.environment.default_gmail_profile,
            "workflow_episode_source": request.data.get("workflow_episode_source")
            or "workflow_step",
            "workflow_episode_stage": request.data.get("workflow_episode_stage")
            or stage,
            "model_for_stage": _model_for_stage,
            "prefer_default_model": prefer_default_model,
            "record_llm_call": _record_llm_call,
            "emit_progress": (
                request.data.get("emit_progress")
                if callable(request.data.get("emit_progress"))
                else None
            ),
            "emit_phase_transition": (
                request.data.get("emit_phase_transition")
                if callable(request.data.get("emit_phase_transition"))
                else None
            ),
            "check_cancellation": (
                request.data.get("check_cancellation")
                if callable(request.data.get("check_cancellation"))
                else None
            ),
            "build_parse_error_result": lambda error: _build_simple_error_result(
                "tool_call_parse_error",
                {
                    "error": getattr(error, "message", None) or str(error),
                    "raw_response": getattr(error, "raw_response", None),
                },
            ),
            "build_validation_error_result": lambda errors, warnings, unavailable, **kwargs: (
                _build_simple_error_result(
                    "tool_call_validation_error",
                    {
                        "message": "; ".join(
                            [*list(errors or []), *list(warnings or [])]
                        )
                        or "tool validation failed",
                        "errors": list(errors or []),
                        "warnings": list(warnings or []),
                        "unavailable": list(unavailable or []),
                        **kwargs,
                    },
                )
            ),
            "aux_llm_calls": aux_llm_calls,
            "llm_calls": llm_calls,
            "invocations": invocations,
            "tool_messages": tool_messages,
            "iteration_count": 0,
            "allowed_write_tools": set(),
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": int(
                getattr(orchestrator, "_max_missing_tool_call_retries_per_turn", 1)
            ),
            "tool_call_repair_attempts": 0,
            "tool_call_repair_budget": 1,
            "llm_allowed_tools": allowed_tools,
            "method_catalogue": method_catalogue,
            "max_tool_invocations": max_tool_invocations,
        }
    )
    if workflow_step_output_contract is not None:
        shared_data["workflow_step_output_contract"] = workflow_step_output_contract

    plan_request = WorkflowActionRequest(
        action_id=request.action_id,
        inputs={},
        environment=request.environment,
        data=shared_data,
        trace=request.trace,
        action_target_id=request.action_target_id,
        contract_concept_id=request.contract_concept_id,
    )
    try:
        plan_result = _run_llm_call_with_timeout(
            request,
            operation=lambda: orchestrator._action_tool_calling_plan(plan_request),
            stage=stage,
            prompt_id=prompt_id,
            selected_model=timeout_selected_model,
            selected_candidate=timeout_selected_candidate,
            timeout_seconds=_conversation_turn_llm_fallback_guard_timeout_sec(
                timeout_override_sec
            ),
            llm_calls=llm_calls,
        )
        shared_data.update(plan_result.outputs)
        if plan_result.status == "failed":
            return plan_result

        while bool(shared_data.get("tool_calls_present")):
            validate_result = orchestrator._action_tool_calling_validate(plan_request)
            shared_data.update(validate_result.outputs)
            if validate_result.status == "failed":
                return validate_result
            if bool(shared_data.get("tool_call_repair_required")):
                repair_result = orchestrator._action_tool_calling_repair(plan_request)
                shared_data.update(repair_result.outputs)
                if repair_result.status == "failed":
                    return repair_result
                continue
            if not shared_data.get("tool_calls_validated"):
                break

            execute_result = orchestrator._action_tool_calling_execute(plan_request)
            shared_data.update(execute_result.outputs)
            if execute_result.status == "failed":
                return execute_result

            backfill_result = orchestrator._action_tool_calling_backfill(plan_request)
            shared_data.update(backfill_result.outputs)
            if backfill_result.status == "failed":
                return backfill_result
            if not shared_data.get("more_tool_calls"):
                break
    except TimeoutError as exc:
        timeout_detail = _context_string(str(exc)) or "llm_call_timed_out"
        return _build_timeout_failure_result(
            request=request,
            stage=stage,
            prompt_id=prompt_id,
            prompt_source=prompt_source,
            rendered_variables=rendered_variables,
            llm_policy_map=llm_policy_map,
            timeout_detail=timeout_detail,
            selected_model=timeout_selected_model,
            llm_calls=llm_calls,
            aux_llm_calls=aux_llm_calls,
            selected_candidate=timeout_selected_candidate,
            timeout_seconds=timeout_override_sec,
            prompt_variant_selection=prompt_variant_selection,
            prompt_context_diagnostics=prompt_context_diagnostics,
        )
    except StructuredToolTransportError as exc:
        return _build_structured_tool_transport_failure_result(
            request=request,
            stage=stage,
            prompt_id=prompt_id,
            prompt_source=prompt_source,
            rendered_variables=rendered_variables,
            llm_policy_map=llm_policy_map,
            error=exc,
            selected_model=timeout_selected_model,
            llm_calls=llm_calls,
            aux_llm_calls=aux_llm_calls,
            tool_invocations=[
                item
                for item in (shared_data.get("invocations") or [])
                if isinstance(item, Mapping)
            ],
            tool_messages=[
                item
                for item in (shared_data.get("tool_messages") or [])
                if isinstance(item, Mapping)
            ],
            selected_candidate=timeout_selected_candidate,
            prompt_variant_selection=prompt_variant_selection,
            prompt_context_diagnostics=prompt_context_diagnostics,
        )

    orchestrator_result = shared_data.get("orchestrator_result")
    if isinstance(orchestrator_result, Mapping):
        response_text = _context_string(orchestrator_result.get("response_text"))
    else:
        response_text = _context_string(
            shared_data.get("final_response")
            or shared_data.get("current_response")
            or shared_data.get("response")
        )

    selected_model = (
        selected_models.get("tool_call")
        or selected_models.get("summariser")
        or request.environment.model
    )
    selected_candidate = None
    for entry in reversed(llm_calls):
        if not isinstance(entry, Mapping):
            continue
        candidate = entry.get("candidate")
        if isinstance(candidate, Mapping):
            selected_candidate = candidate
            break

    final_contract_required_tools = (
        infer_turn_contract_required_tools(
            turn_expected_outcome_contract=turn_expected_outcome_contract,
            method_catalogue=(
                method_catalogue if isinstance(method_catalogue, Mapping) else None
            ),
            allowed_tools=allowed_tools,
            tool_invocations=invocations,
        )
        if callable(infer_turn_contract_required_tools)
        else turn_expected_outcome_contract.required_tools
    )
    final_required_tool_sources = {
        **required_tool_sources,
        "turn_expected_outcome_contract": (
            final_contract_required_tools
            or turn_expected_outcome_contract.required_tools
        ),
    }
    final_required_obligation_tools = _merge_required_prompt_tools(
        request.data.get("required_prompt_tools"),
        llm_policy_map.get("required_tools"),
        final_contract_required_tools or turn_expected_outcome_contract.required_tools,
    )
    planned_tool_calls = shared_data.get("tool_calls")
    final_required_tool_obligation_ledger = build_required_tool_obligation_ledger(
        required_tools_by_source=final_required_tool_sources,
        invocations=invocations,
        planned_tool_calls=(
            planned_tool_calls if isinstance(planned_tool_calls, list) else None
        ),
        tool_call_validation_failure_context=(
            _tool_call_validation_failure_context_from_data(shared_data)
        ),
        allowed_tools=allowed_tools,
        method_catalogue=(
            method_catalogue if isinstance(method_catalogue, Mapping) else None
        ),
        max_tool_invocations=max_tool_invocations,
        target_contract_state=target_contract_state_from_context(shared_data),
        existing_ledger=(
            shared_data.get("required_tool_obligation_ledger")
            if isinstance(shared_data.get("required_tool_obligation_ledger"), Mapping)
            else None
        ),
    )
    shared_data["required_tool_obligation_ledger"] = (
        final_required_tool_obligation_ledger
    )
    shared_data["required_tool_obligation_blockers"] = list(
        final_required_tool_obligation_ledger.get("blocking_failure_codes") or []
    )

    return _build_result(
        request=request,
        response_text=response_text,
        prompt_id=prompt_id,
        prompt_source=prompt_source,
        rendered_variables=rendered_variables,
        llm_policy_map=llm_policy_map,
        validation_policy_map=validation_policy_map,
        selected_model=selected_model,
        selected_candidate=selected_candidate,
        tool_invocations=invocations,
        tool_messages=tool_messages,
        llm_calls=llm_calls,
        aux_llm_calls=aux_llm_calls,
        required_prompt_tools=final_required_obligation_tools,
        required_tool_obligation_ledger=final_required_tool_obligation_ledger,
        max_tool_invocations=max_tool_invocations,
        prompt_variant_selection=prompt_variant_selection,
        prompt_context_diagnostics=prompt_context_diagnostics,
    )


def execute_llm_step(request: WorkflowActionRequest) -> WorkflowActionResult:
    llm_policy_map = (
        dict(request.llm_policy) if isinstance(request.llm_policy, Mapping) else {}
    )
    stage = _llm_stage(llm_policy_map, request)
    timeout_seconds = _conversation_turn_llm_timeout_override_sec(request)
    if timeout_seconds is None:
        return _execute_llm_step_inner(request)
    return _run_llm_step_with_timeout(
        request,
        stage=stage,
        llm_policy_map=llm_policy_map,
        timeout_seconds=timeout_seconds,
    )


__all__ = ["compute_llm_context_fields_sha256", "execute_llm_step"]
