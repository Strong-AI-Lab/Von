"""Generic prompt-driven LLM step execution for VWL.

This module makes prompt-bearing workflow steps executable without requiring a
bespoke Python handler per reasoning step. Deterministic tool execution and
write-policy enforcement are delegated to the existing internal MCP
orchestrator machinery when a gateway is available.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Mapping, MutableMapping, Optional, Sequence

from ..services.buttonify_service import (
    parse_buttonify_options_json,
    sanitise_buttonify_options,
)
from ..services.prompt_template_service import PromptTemplateService
from .action_registry import WorkflowActionRequest, WorkflowActionResult

_SPOKEN_BLOCK_RE = re.compile(
    r"<spoken>\s*(.*?)\s*</spoken>",
    flags=re.DOTALL | re.IGNORECASE,
)


def _context_string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return str(value or "").strip()


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
    requested_prompt_ids = (
        prompt_contract_map.get("requested_prompt_concept_ids") or []
    )
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


def _compose_llm_prompt(
    *,
    base_prompt: str,
    llm_policy: Mapping[str, Any],
    context: Mapping[str, Any],
) -> str:
    sections: list[str] = []

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

    raw_context_fields = llm_policy.get("context_fields")
    if isinstance(raw_context_fields, Sequence) and not isinstance(
        raw_context_fields, (str, bytes, bytearray)
    ):
        for item in raw_context_fields:
            if not isinstance(item, Mapping):
                continue
            context_key = _context_string(item.get("context_key"))
            if not context_key:
                continue
            value = context.get(context_key)
            value_text = _serialise_prompt_context_value(value)
            if not value_text:
                continue
            label = _context_string(item.get("label")) or context_key.replace("_", " ")
            sections.append(f"{label}:\n{value_text}")

    return "\n\n".join(section for section in sections if section).strip()


def _validation_output_format(validation_policy: Mapping[str, Any]) -> str:
    for key in ("output_format", "format", "validator"):
        value = _context_string(validation_policy.get(key))
        if value:
            return value.lower()
    return ""


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
        options = sanitise_buttonify_options(parse_buttonify_options_json(response_text))
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
) -> None:
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
    llm_calls.append(entry)


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


def _build_gateway_runtime(
    request: WorkflowActionRequest,
) -> tuple[Any, Any, Mapping[str, Any], str | None, str | None]:
    from ..integrations.internal_mcp.orchestrator import InternalMCPChatOrchestrator
    from ..services.model_registry_service import get_model_registry_snapshot

    gateway = request.environment.gateway
    if gateway is None:
        raise RuntimeError("workflow_llm_step_gateway_unavailable")

    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=(
            int(request.environment.max_tool_invocations)
            if request.environment.max_tool_invocations is not None
            else 1
        ),
        tool_batch_cap=(
            max(1, int(request.inputs.get("tool_batch_cap") or 4))
            if request.inputs.get("tool_batch_cap") is not None
            else 4
        ),
        default_gmail_profile=request.environment.default_gmail_profile,
    )
    user_concept_id, org_concept_id = _resolve_user_context_ids(request.data)
    policy_state, _policy_telemetry = orchestrator._load_workflow_model_policy(None)
    registry_snapshot = get_model_registry_snapshot()
    return orchestrator, policy_state, registry_snapshot, user_concept_id, org_concept_id


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
        "selected_prompt_id": prompt_id,
        "selected_prompt_source": prompt_source,
        "selected_model": selected_model,
        "selected_model_candidate": (
            dict(selected_candidate) if isinstance(selected_candidate, Mapping) else None
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

    outputs = {
        "final_response": response_text,
        "llm_step_response": response_text,
        "llm_step_envelope": llm_step_envelope,
        "tool_invocations": list(tool_invocations),
        "tool_messages": list(tool_messages),
        "llm_calls": list(llm_calls),
        "aux_llm_calls": list(aux_llm_calls),
    }
    outputs.update(validated_outputs)
    return WorkflowActionResult(outputs=outputs)


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
) -> WorkflowActionResult:
    llm_client = request.environment.llm_client
    if llm_client is None or not hasattr(llm_client, "generate"):
        return WorkflowActionResult(
            status="failed",
            error="workflow_llm_step_client_unavailable",
        )

    context_messages = _coerce_context_messages(
        request.data.get(_context_string(llm_policy_map.get("context_messages_context_key")))
    )
    model_name = request.environment.model
    start = time.perf_counter()
    response = llm_client.generate(
        rendered_prompt,
        context=context_messages or None,
        model=model_name,
    )
    duration_ms = (time.perf_counter() - start) * 1000.0
    _append_llm_call(
        request.data,
        call_type="llm.generate",
        stage=stage,
        model_name=model_name,
        duration_ms=duration_ms,
        note="generic_llm_step_direct",
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
) -> WorkflowActionResult:
    orchestrator, policy_state, registry_snapshot, user_concept_id, org_concept_id = (
        _build_gateway_runtime(request)
    )
    llm_calls: list[dict[str, Any]] = []
    aux_llm_calls: list[dict[str, Any]] = []
    emit_progress = (
        request.data.get("emit_progress")
        if callable(request.data.get("emit_progress"))
        else None
    )

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
    ) -> None:
        entry: dict[str, Any] = {
            "type": call_type,
            "model_name": model_name,
            "duration_ms": duration_ms,
            "stage": stage,
        }
        if usage:
            entry["usage"] = dict(usage)
        if note:
            entry["note"] = note
        if provider:
            entry["provider"] = provider
        if isinstance(candidate, Mapping):
            entry["candidate"] = dict(candidate)
        llm_calls.append(entry)

    response_text, selected_model, selected_candidate = (
        orchestrator._run_llm_with_fallbacks(
            stage=stage,
            prompt=rendered_prompt,
            context=_coerce_context_messages(
                request.data.get(
                    _context_string(llm_policy_map.get("context_messages_context_key"))
                )
            ),
            default_client=request.environment.llm_client,
            default_model=request.environment.model,
            policy_state=policy_state,
            registry_snapshot=registry_snapshot,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
            llm_calls_log=llm_calls,
            aux_log=aux_llm_calls,
            record_llm_call=_record_llm_call,
            emit_progress=emit_progress,
        )
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
        tool_invocations=(),
        tool_messages=(),
        llm_calls=llm_calls,
        aux_llm_calls=aux_llm_calls,
    )


def execute_llm_step(request: WorkflowActionRequest) -> WorkflowActionResult:
    """Execute a generic LLM workflow step."""

    llm_policy_map = (
        dict(request.llm_policy) if isinstance(request.llm_policy, Mapping) else {}
    )
    validation_policy_map = (
        dict(request.validation_policy)
        if isinstance(request.validation_policy, Mapping)
        else {}
    )

    prompt_id, base_prompt_text, rendered_variables, prompt_source = (
        _resolve_prompt_render(
            prompt_contract=request.prompt_contract,
            llm_policy=llm_policy_map,
            context=request.data,
        )
    )
    if not isinstance(base_prompt_text, str) or not base_prompt_text.strip():
        return WorkflowActionResult(
            status="failed",
            error="workflow_llm_step_prompt_render_failed",
        )

    rendered_prompt = _compose_llm_prompt(
        base_prompt=base_prompt_text,
        llm_policy=llm_policy_map,
        context=request.data,
    )
    if not rendered_prompt:
        return WorkflowActionResult(
            status="failed",
            error="workflow_llm_step_prompt_render_failed",
        )

    stage = _llm_stage(llm_policy_map, request)
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
        )

    from ..integrations.internal_mcp.orchestrator import InternalMCPChatOrchestrator
    from ..services.model_registry_service import get_model_registry_snapshot

    orchestrator = InternalMCPChatOrchestrator(
        gateway=request.environment.gateway,
        max_tool_invocations=(
            int(request.environment.max_tool_invocations)
            if request.environment.max_tool_invocations is not None
            else 1
        ),
        tool_batch_cap=(
            max(1, int(request.inputs.get("tool_batch_cap") or 4))
            if request.inputs.get("tool_batch_cap") is not None
            else 4
        ),
        default_gmail_profile=request.environment.default_gmail_profile,
    )

    user_concept_id, org_concept_id = _resolve_user_context_ids(request.data)
    policy_state, _policy_telemetry = orchestrator._load_workflow_model_policy(None)
    registry_snapshot = get_model_registry_snapshot()

    llm_calls: list[dict[str, Any]] = []
    aux_llm_calls: list[dict[str, Any]] = []
    invocations: list[dict[str, Any]] = []
    tool_messages: list[dict[str, Any]] = []
    selected_models: dict[str, str | None] = {}

    def _model_for_stage(inner_stage: str) -> Optional[str]:
        model_name = orchestrator._select_model_for_stage(
            stage=inner_stage,
            default_model=request.environment.model,
            policy_state=policy_state,
            registry_snapshot=registry_snapshot,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
        )
        selected_models[inner_stage] = model_name
        return model_name

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
    ) -> None:
        entry: dict[str, Any] = {
            "type": call_type,
            "model_name": model_name,
            "duration_ms": duration_ms,
            "stage": stage,
        }
        if usage:
            entry["usage"] = dict(usage)
        if note:
            entry["note"] = note
        if provider:
            entry["provider"] = provider
        if isinstance(candidate, Mapping):
            entry["candidate"] = dict(candidate)
        llm_calls.append(entry)

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

    method_catalogue = request.environment.gateway.describe_methods()
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

    shared_data: dict[str, Any] = {
        "prompt": rendered_prompt,
        "prompt_for_requirements": request.data.get("prompt_for_requirements")
        or rendered_prompt,
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
        "workflow_episode_stage": request.data.get("workflow_episode_stage") or stage,
        "model_for_stage": _model_for_stage,
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
        "build_validation_error_result": lambda errors, warnings, unavailable, **kwargs: _build_simple_error_result(
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
        "llm_allowed_tools": allowed_tools,
        "method_catalogue": method_catalogue,
    }

    plan_request = WorkflowActionRequest(
        action_id=request.action_id,
        inputs={},
        environment=request.environment,
        data=shared_data,
        trace=request.trace,
        action_target_id=request.action_target_id,
        contract_concept_id=request.contract_concept_id,
    )
    plan_result = orchestrator._action_tool_calling_plan(plan_request)
    shared_data.update(plan_result.outputs)
    if plan_result.status == "failed":
        return plan_result

    while bool(shared_data.get("tool_calls_present")):
        validate_result = orchestrator._action_tool_calling_validate(plan_request)
        shared_data.update(validate_result.outputs)
        if validate_result.status == "failed":
            return validate_result
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
    )


__all__ = ["execute_llm_step"]
