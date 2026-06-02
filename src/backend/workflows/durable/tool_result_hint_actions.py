"""Reusable durable workflow action: tool-result signal extraction.

JVNAUTOSCI-2118 (Phase 1 vertical slice of Epic JVNAUTOSCI-2112). This action
exposes the existing Python primitive
``tool_result_hints.extract_signals_from_tool_result`` as a generic VWL action
so workflow definitions can chain "fetch via tool X" -> "extract structured
signals from X's output" without any tool-specific Python.

The action is fully generic: the prompt body that drives extraction is read
from a Vontology text relation authored against the source tool concept (by
default ``#V#output_item_signal_extraction_hint``). Callers supply only:

* ``source_tool_concept`` -- the tool concept id whose authored hint to use
* ``tool_payload``        -- the raw output of that tool
* (optional) ``hint_predicate_id`` -- override the hint predicate
* (optional) ``lang``     -- language for the hint resolution

Anti-drift: this module names no specific external integration. All semantic
behaviour lives in Vontology hint bodies.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from ...services.output_hint_contracts import (
    OUTPUT_FOLLOWUP_HINT_PREDICATE_ID,
    OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
)
from ...services.tool_result_hints import (
    LLMGenerationError,
    LLMGenerationResult,
    extract_signals_from_tool_result,
    resolve_hint_body,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..llm_call_telemetry import stamp_llm_call_timestamps


logger = logging.getLogger(__name__)


EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID = "extract_signals_from_tool_result"
"""Canonical action id used by VWL workflow definitions."""

RESOLVE_TOOL_OUTPUT_FOLLOWUP_HINT_ACTION_ID = "resolve_tool_output_followup_hint"
"""Resolve a Vontology-authored follow-up hint for a tool concept."""


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _resolve_tool_payload(inputs: Mapping[str, Any], data: Mapping[str, Any]) -> Any:
    """Pick up the tool payload from inputs first, then fall back to shared data.

    VWL ``hasInputMap`` will normally bind ``tool_payload`` directly via an
    output-field-to-context-key edge, but allowing a ``data`` fallback keeps
    the action robust to authoring patterns that stash the previous step's
    output under a known context key.
    """

    if "tool_payload" in inputs:
        return inputs["tool_payload"]
    return data.get("tool_payload")


def _copy_mapping_sequence(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        {str(key): item for key, item in entry.items() if isinstance(key, str)}
        for entry in value
        if isinstance(entry, Mapping)
    )


def _resolve_model_policy_stage(
    request: WorkflowActionRequest,
    inputs: Mapping[str, Any],
) -> str:
    llm_policy = request.llm_policy if isinstance(request.llm_policy, Mapping) else {}
    return (
        _safe_str(inputs.get("policy_stage"))
        or _safe_str(inputs.get("model_policy_stage"))
        or _safe_str(llm_policy.get("policy_stage"))
        or _safe_str(request.action_id)
        or EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID
    )


def _build_model_policy_generate(
    request: WorkflowActionRequest,
    *,
    policy_stage: str,
):
    if request.environment.gateway is None:
        return None

    def _generate(
        *,
        prompt: str,
        context: Any | None = None,
        model: str | None = None,
    ) -> LLMGenerationResult:
        from ..llm_step_executor import (
            _build_gateway_runtime,
            _prefer_default_model_for_request,
        )

        llm_calls: list[dict[str, Any]] = []
        aux_llm_calls: list[Mapping[str, Any]] = []
        (
            orchestrator,
            policy_state,
            registry_snapshot,
            user_concept_id,
            org_concept_id,
        ) = _build_gateway_runtime(request)
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
            workflow_stage_id: str | None = None,
            exchange_blob_ref: Mapping[str, Any] | None = None,
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
            if isinstance(workflow_stage_id, str) and workflow_stage_id.strip():
                entry["workflow_stage_id"] = workflow_stage_id.strip()
            if isinstance(exchange_blob_ref, Mapping) and exchange_blob_ref:
                entry["exchange_blob_ref"] = dict(exchange_blob_ref)
            stamp_llm_call_timestamps(entry, duration_ms=duration_ms)
            llm_calls.append(entry)

        stage = (
            _safe_str(request.action_id)
            or EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID
        )
        workflow_stage_id = _safe_str(request.workflow_state_id) or None
        try:
            response, selected_model, selected_candidate = (
                orchestrator._run_llm_with_fallbacks(
                    stage=stage,
                    policy_stage=policy_stage,
                    prompt=prompt,
                    context=context,
                    default_client=request.environment.llm_client,
                    default_model=model or request.environment.model,
                    policy_state=policy_state,
                    registry_snapshot=registry_snapshot,
                    user_concept_id=user_concept_id,
                    org_concept_id=org_concept_id,
                    llm_calls_log=llm_calls,
                    aux_log=aux_llm_calls,
                    record_llm_call=_record_llm_call,
                    emit_progress=emit_progress,
                    prefer_default_model=_prefer_default_model_for_request(request),
                    workflow_stage_id=workflow_stage_id,
                    workflow_id=request.workflow_id,
                )
            )
        except Exception as exc:
            raise LLMGenerationError(
                str(exc) or type(exc).__name__,
                error_class=type(exc).__name__,
                llm_calls=tuple(llm_calls),
                aux_llm_calls=tuple(aux_llm_calls),
            ) from exc

        return LLMGenerationResult(
            response=response,
            selected_model=selected_model,
            selected_candidate=(
                dict(selected_candidate)
                if isinstance(selected_candidate, Mapping)
                else None
            ),
            llm_calls=tuple(llm_calls),
            aux_llm_calls=tuple(aux_llm_calls),
        )

    return _generate


def _handle_extract_signals_from_tool_result(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    data = request.data if isinstance(request.data, dict) else {}

    source_tool_concept = (
        _safe_str(inputs.get("source_tool_concept"))
        or _safe_str(inputs.get("source_tool_concept_id"))
        or _safe_str(inputs.get("tool_concept_id"))
    )
    if not source_tool_concept:
        return WorkflowActionResult(
            status="failed",
            error=(
                "extract_signals_from_tool_result requires 'source_tool_concept' "
                "(the Vontology concept id of the tool whose authored signal-"
                "extraction hint should drive extraction)."
            ),
        )

    tool_payload = _resolve_tool_payload(inputs, data)
    if tool_payload is None:
        return WorkflowActionResult(
            status="failed",
            error=(
                "extract_signals_from_tool_result requires 'tool_payload' "
                "(the raw output of the source tool)."
            ),
        )

    hint_predicate_id = (
        _safe_str(inputs.get("hint_predicate_id"))
        or OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID
    )
    lang = _safe_str(inputs.get("lang")) or "en-NZ"
    model = _safe_str(inputs.get("model")) or None
    policy_stage = _resolve_model_policy_stage(request, inputs)
    llm_generate = _build_model_policy_generate(
        request,
        policy_stage=policy_stage,
    )

    llm_client = request.environment.llm_client
    if llm_client is None and llm_generate is None:
        return WorkflowActionResult(
            status="failed",
            error=(
                "extract_signals_from_tool_result requires an LLM client on the "
                "workflow environment."
            ),
        )

    result = extract_signals_from_tool_result(
        source_tool_concept,
        tool_payload,
        turn_context=data,
        llm_client=llm_client,
        model=model,
        llm_generate=llm_generate,
        hint_predicate_id=hint_predicate_id,
        lang=lang,
    )

    llm_calls = _copy_mapping_sequence(result.llm_calls)
    aux_llm_calls = _copy_mapping_sequence(result.aux_llm_calls)
    outputs: dict[str, Any] = {
        "signals": dict(result.signals) if isinstance(result.signals, Mapping) else result.signals,
        "hint_resolved": bool(result.hint_resolved),
        "hint_body_present": bool(result.hint_body),
        "warnings": list(result.warnings),
        "raw_response": result.raw_response,
        "source_tool_concept": source_tool_concept,
        "hint_predicate_id": hint_predicate_id,
        "policy_stage": policy_stage,
        "selected_model": result.selected_model,
        "selected_model_candidate": (
            dict(result.selected_candidate)
            if isinstance(result.selected_candidate, Mapping)
            else None
        ),
        "llm_calls": list(llm_calls),
        "aux_llm_calls": list(aux_llm_calls),
    }

    # Treat "hint not authored" or any LLM-side failure as a workflow failure
    # so on_failure transitions can react. Empty signals with no warnings is a
    # legitimate "nothing to extract" success.
    failure_warnings = {"hint_not_authored", "no_llm_client", "empty_response"}
    if any(
        w in failure_warnings or w.startswith("llm_error:")
        for w in result.warnings
    ):
        return WorkflowActionResult(
            status="failed",
            outputs=outputs,
            error=", ".join(result.warnings) or "extract_signals_from_tool_result failed",
        )

    return WorkflowActionResult(status="success", outputs=outputs)


def _parse_followup_hint_body(hint_body: str) -> tuple[Mapping[str, Any], str | None]:
    if not hint_body.strip():
        return {}, "followup_hint_empty"
    try:
        parsed = json.loads(hint_body)
    except json.JSONDecodeError:
        return {}, "followup_hint_json_parse_failed"
    if not isinstance(parsed, Mapping):
        return {}, "followup_hint_not_object"
    return parsed, None


def _normalise_entries(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _select_followup_entry(
    entries: list[Mapping[str, Any]],
    *,
    required_action_kind: str | None = None,
) -> Mapping[str, Any] | None:
    action_kind = _safe_str(required_action_kind)
    if action_kind:
        for entry in entries:
            if _safe_str(entry.get("action_kind")) == action_kind:
                return entry
        return None
    return entries[0] if entries else None


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _handle_resolve_tool_output_followup_hint(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    source_tool_concept = (
        _safe_str(inputs.get("source_tool_concept"))
        or _safe_str(inputs.get("source_tool_concept_id"))
        or _safe_str(inputs.get("tool_concept_id"))
    )
    if not source_tool_concept:
        return WorkflowActionResult(
            status="failed",
            error=(
                "resolve_tool_output_followup_hint requires 'source_tool_concept' "
                "(the Vontology concept id of the tool whose authored follow-up "
                "hint should be resolved)."
            ),
        )

    hint_predicate_id = (
        _safe_str(inputs.get("hint_predicate_id")) or OUTPUT_FOLLOWUP_HINT_PREDICATE_ID
    )
    lang = _safe_str(inputs.get("lang")) or "en-NZ"
    required_action_kind = _safe_str(inputs.get("required_action_kind")) or None

    hint_body = resolve_hint_body(source_tool_concept, hint_predicate_id, lang=lang)
    if not hint_body:
        return WorkflowActionResult(
            status="failed",
            outputs={
                "hint": {},
                "hint_resolved": False,
                "hint_body_present": False,
                "warnings": ["hint_not_authored"],
                "source_tool_concept": source_tool_concept,
                "hint_predicate_id": hint_predicate_id,
            },
            error="hint_not_authored",
        )

    hint, parse_error = _parse_followup_hint_body(hint_body)
    if parse_error:
        return WorkflowActionResult(
            status="failed",
            outputs={
                "hint": {},
                "hint_resolved": True,
                "hint_body_present": True,
                "warnings": [parse_error],
                "source_tool_concept": source_tool_concept,
                "hint_predicate_id": hint_predicate_id,
                "raw_hint_body": hint_body,
            },
            error=parse_error,
        )

    entries = _normalise_entries(hint.get("entries"))
    selected_entry = _select_followup_entry(
        entries,
        required_action_kind=required_action_kind,
    )
    if selected_entry is None:
        reason = (
            f"followup_hint_required_action_kind_missing:{required_action_kind}"
            if required_action_kind
            else "followup_hint_entries_missing"
        )
        return WorkflowActionResult(
            status="failed",
            outputs={
                "hint": dict(hint),
                "hint_resolved": True,
                "hint_body_present": True,
                "warnings": [reason],
                "entries": [dict(entry) for entry in entries],
                "source_tool_concept": source_tool_concept,
                "hint_predicate_id": hint_predicate_id,
                "required_action_kind": required_action_kind,
            },
            error=reason,
        )

    selected_action = _mapping_or_empty(selected_entry.get("action"))
    selected_tool_arguments = _mapping_or_empty(
        selected_entry.get("tool_arguments")
    ) or _mapping_or_empty(selected_action.get("tool_arguments"))
    selected_upstream_filter = _mapping_or_empty(selected_entry.get("upstream_filter"))

    return WorkflowActionResult(
        status="success",
        outputs={
            "hint": dict(hint),
            "hint_resolved": True,
            "hint_body_present": True,
            "warnings": [],
            "entries": [dict(entry) for entry in entries],
            "selected_entry": dict(selected_entry),
            "selected_action": dict(selected_action),
            "selected_tool_arguments": dict(selected_tool_arguments),
            "selected_upstream_filter": dict(selected_upstream_filter),
            "source_tool_concept": source_tool_concept,
            "hint_predicate_id": hint_predicate_id,
            "required_action_kind": required_action_kind,
        },
    )


def register_tool_result_hint_actions(registry: ActionRegistry) -> None:
    """Register the extract_signals_from_tool_result action.

    Idempotent: safe to call multiple times against the same registry.
    """

    registry.register_if_absent(
        ActionSpec(
            action_id=EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID,
            handler=_handle_extract_signals_from_tool_result,
            description=(
                "Generic durable action: read the Vontology-authored signal-"
                "extraction hint for the named source tool concept, run the "
                "shared LLM-backed extractor over the supplied tool payload, "
                "and surface the structured signals plus diagnostic warnings."
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=RESOLVE_TOOL_OUTPUT_FOLLOWUP_HINT_ACTION_ID,
            handler=_handle_resolve_tool_output_followup_hint,
            description=(
                "Generic durable action: read a Vontology-authored follow-up "
                "hint for the named source tool concept, parse its JSON body, "
                "and expose the selected follow-up entry for declarative VWL "
                "steps to consume."
            ),
        )
    )


__all__ = [
    "EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID",
    "RESOLVE_TOOL_OUTPUT_FOLLOWUP_HINT_ACTION_ID",
    "register_tool_result_hint_actions",
]
