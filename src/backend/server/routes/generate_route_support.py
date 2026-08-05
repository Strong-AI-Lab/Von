from __future__ import annotations

import json
import time
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Callable, Mapping, Sequence, cast

from src.backend.languagemodels.llm_interface import ModelExecutionEligibilityError
from src.backend.services.debug_payload_store import (
    compact_debug_payload_for_storage,
    default_tool_message_threshold_bytes,
)
from src.backend.services.python_decision_authority_service import (
    annotate_python_decision_event,
)

SCREEN_BACKFILL_STAGE_CONCEPT_ID = "#V#screen_backfill_stage"
SCREEN_BACKFILL_STAGE_PROMPT_IDS = ("#V#von_screen_content_prompt_for_witbrock",)


def _normalise_prompt_concept_ids(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        concept_id = value.strip()
        if not concept_id or concept_id in seen:
            continue
        seen.add(concept_id)
        ordered.append(concept_id)
    return ordered


def _compact_presenter_context_scalar(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool | int | float):
        return value
    return None


def _compact_presenter_context_scalar_list(
    value: Any,
    *,
    limit: int = 24,
) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return []
    compacted: list[Any] = []
    seen: set[str] = set()
    for item in value:
        scalar = _compact_presenter_context_scalar(item)
        if scalar is None:
            continue
        dedupe_key = str(scalar)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        compacted.append(scalar)
        if len(compacted) >= limit:
            break
    return compacted


def _compact_presenter_context_mapping(
    value: Any,
    *,
    scalar_fields: Sequence[str],
    list_fields: Sequence[str] = (),
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    compacted: dict[str, Any] = {}
    for field in scalar_fields:
        if field not in value:
            continue
        scalar = _compact_presenter_context_scalar(value.get(field))
        if scalar is not None:
            compacted[field] = scalar
    for field in list_fields:
        if field not in value:
            continue
        scalar_list = _compact_presenter_context_scalar_list(value.get(field))
        if scalar_list:
            compacted[field] = scalar_list
    return compacted


def _compact_workflow_execution_for_presenter_context(
    workflow_execution_summary: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    compacted = _compact_presenter_context_mapping(
        workflow_execution_summary,
        scalar_fields=(
            "workflow_id",
            "workflow_instance_id",
            "completed",
            "effective_completed",
            "terminal_status",
            "final_state",
            "workflow_instance_status",
            "action_started_count",
            "action_completed_count",
            "action_success_count",
            "action_failure_count",
            "action_unknown_count",
            "terminal_effect_count",
            "durable_side_effect_count",
            "runtime_event_count",
            "step_result_envelope_count",
        ),
    )
    if not compacted:
        return None
    compacted["schema_version"] = "workflow_execution_presenter_context.v1"
    return compacted


def _build_presenter_screen_backfill_context(
    *,
    prompt_concept_ids: Sequence[str],
    backfill_reason: str | None,
    user_request: str,
    model_response: str,
    tool_evidence_summary: str | None,
    existing_spoken: str | None,
    required_screen_json_fence: str | None,
    response_candidate_internal_status: bool,
    response_candidate_tool_dump: bool,
    response_candidate_duplicates_spoken: bool,
    workflow_execution_summary: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    prompt_ids = _normalise_prompt_concept_ids(prompt_concept_ids)
    if not prompt_ids:
        prompt_ids = list(SCREEN_BACKFILL_STAGE_PROMPT_IDS)
    workflow_execution_context = _compact_workflow_execution_for_presenter_context(
        workflow_execution_summary
    )
    payload = {
        "schema_version": "presenter_screen_backfill_context.v1",
        "stage_concept_id": SCREEN_BACKFILL_STAGE_CONCEPT_ID,
        "screen_prompt_concept_ids": prompt_ids,
        "backfill_reason": backfill_reason,
        "user_request": user_request,
        "model_response": model_response,
        "tool_evidence_summary": tool_evidence_summary,
        "existing_spoken": existing_spoken,
        "required_screen_json_fence": required_screen_json_fence,
        "rejected_candidate_reasons": {
            "response_candidate_internal_status": bool(
                response_candidate_internal_status
            ),
            "response_candidate_tool_dump": bool(response_candidate_tool_dump),
            "response_candidate_duplicates_spoken": bool(
                response_candidate_duplicates_spoken
            ),
        },
        "workflow_execution": workflow_execution_context,
    }
    context_messages = [
        {
            "role": "user",
            "content": json.dumps(
                payload,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
                default=str,
            ),
        }
    ]
    context_telemetry = {
        "schema_version": "presenter_screen_backfill_context_lineage.v1",
        "stage_concept_id": SCREEN_BACKFILL_STAGE_CONCEPT_ID,
        "prompt_concept_ids": prompt_ids,
        "context_payload_schema": "presenter_screen_backfill_context.v1",
        "context_source": "route_structured_support_payload",
        "workflow_execution_present": bool(workflow_execution_context),
        "workflow_execution_workflow_id": (
            workflow_execution_context.get("workflow_id")
            if isinstance(workflow_execution_context, Mapping)
            else None
        ),
    }
    return context_messages, context_telemetry


def _build_presenter_screen_backfill_authority_gate_event(
    *, prompt_concept_ids: Sequence[str], source: str
) -> dict[str, Any]:
    return annotate_python_decision_event(
        {
            "type": "presenter_screen_backfill",
            "stage": "screen_backfill",
            "stage_concept_id": SCREEN_BACKFILL_STAGE_CONCEPT_ID,
            "requested_prompt_concept_ids": _normalise_prompt_concept_ids(
                prompt_concept_ids
            ),
            "source": source,
        },
        stage="screen_backfill",
        component="presenter_routes",
        function="_presenter_screen_backfill_authority_gate",
        decision_class="presenter_authority_gate",
        decision_source="represented_prompt_resolution",
        changed_outcome=True,
        reason_code="screen_backfill_prompt_unavailable",
        possible_inappropriate_python_code_use=False,
    )


def _invoke_presenter_screen_backfill_prompt(
    *,
    llm_client: Any,
    represented_screen_prompt: str,
    context_messages: Sequence[Mapping[str, Any]],
    context_telemetry: Mapping[str, Any],
    model_name: str,
    record_stage_llm_call: Callable[..., None],
    emit_stage_progress: Callable[[Mapping[str, Any]], None],
    infer_provider: Callable[[str | None], str | None],
) -> tuple[Any, str]:
    screen_context_messages = [dict(message) for message in context_messages]

    llm_start = time.perf_counter()
    emit_stage_progress(
        {"status": "llm_call_start", "stage": "screen_backfill", "model": model_name}
    )
    try:
        synthesis_response = llm_client.generate(
            prompt=represented_screen_prompt,
            context=screen_context_messages,
            model=model_name,
        )
    except Exception as exc:
        screen_duration_ms = (time.perf_counter() - llm_start) * 1000.0
        eligibility_denied = isinstance(exc, ModelExecutionEligibilityError)
        record_stage_llm_call(
            call_type="llm.generate",
            model_name=model_name,
            duration_ms=screen_duration_ms,
            usage=None,
            note="Screen backfill represented prompt invocation.",
            stage="screen_backfill",
            workflow_stage_id=SCREEN_BACKFILL_STAGE_CONCEPT_ID,
            provider=(
                exc.provider if eligibility_denied else infer_provider(model_name)
            ),
            status="failed",
            success=False,
            error_class=type(exc).__name__,
            failure_kind=(exc.failure_kind if eligibility_denied else None),
            provider_request_sent=False if eligibility_denied else None,
        )
        raise
    screen_duration_ms = (time.perf_counter() - llm_start) * 1000.0
    emit_stage_progress(
        {
            "status": "llm_call_chunk",
            "stage": "screen_backfill",
            "model": model_name,
            "chunks": 1,
            "duration_ms": int(screen_duration_ms),
        }
    )
    prompt_ids = _normalise_prompt_concept_ids(
        context_telemetry.get("prompt_concept_ids")
    )
    record_stage_llm_call(
        call_type="llm.generate",
        model_name=model_name,
        duration_ms=screen_duration_ms,
        usage=None,
        note="Screen backfill represented prompt invocation.",
        stage="screen_backfill",
        workflow_stage_id=SCREEN_BACKFILL_STAGE_CONCEPT_ID,
        provider=infer_provider(model_name),
        prompt={
            "text": represented_screen_prompt,
            "char_count": len(represented_screen_prompt),
            "is_truncated": False,
            "prompt_concept_ids": prompt_ids,
            "context_messages": screen_context_messages,
            "context_telemetry": dict(context_telemetry),
        },
        response={
            "text": str(synthesis_response),
            "char_count": len(str(synthesis_response)),
            "is_truncated": False,
        },
        status="completed",
        success=True,
        provider_request_sent=True,
    )
    return synthesis_response, model_name


def _persist_generate_turn_messages(
    *,
    history_user_id: str | None,
    user_message_persisted_early: bool,
    prompt_text: str,
    user_concept_id: str | None,
    session_id: str,
    tool_messages: list[dict[str, Any]],
    response_text: str,
    llm_debug_info: Mapping[str, Any],
    user_namespace: str | None,
    org_concept_id: str | None,
    role_in_org: str | None,
    current_context: Sequence[Mapping[str, Any]] | None,
    truncate_large_tool_results_fn: Callable[..., list[dict[str, Any]]],
    add_chat_history_message_fn: Callable[..., Any],
    limit_context_size_fn: Callable[..., list[dict[str, Any]]],
    timing_recorder: Any | None = None,
    refresh_llm_debug_timing_fn: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    def _maybe_span(
        *, operation_name: str, attributes: Mapping[str, Any] | None = None
    ) -> AbstractContextManager[Any]:
        span_fn = getattr(timing_recorder, "span", None)
        if callable(span_fn):
            return cast(
                AbstractContextManager[Any],
                span_fn(
                    stage_id="response_finalising",
                    operation_kind="chat_history_persistence",
                    operation_name=operation_name,
                    attributes=attributes,
                ),
            )
        return nullcontext()

    with _maybe_span(
        operation_name="compact_tool_messages_for_history",
        attributes={"tool_message_count": len(tool_messages)},
    ):
        truncated_tool_messages = truncate_large_tool_results_fn(
            tool_messages, max_tool_content_chars=5000
        )
    llm_debug_payload = dict(llm_debug_info)
    request_id = (
        str(llm_debug_payload.get("request_id")).strip()
        if llm_debug_payload.get("request_id")
        else None
    )
    storage_tool_messages: list[dict[str, Any]] = []
    with _maybe_span(
        operation_name="offload_large_tool_message_debug_payloads",
        attributes={"tool_message_count": len(truncated_tool_messages)},
    ):
        for tool_msg in truncated_tool_messages:
            if not isinstance(tool_msg, Mapping):
                continue
            compacted = compact_debug_payload_for_storage(
                dict(tool_msg),
                root_kind="chat_history.tool_message",
                namespace=user_namespace,
                request_id=request_id,
                threshold_bytes=default_tool_message_threshold_bytes(),
                fail_soft=True,
            )
            storage_tool_messages.append(
                compacted.payload
                if isinstance(compacted.payload, dict)
                else dict(tool_msg)
            )
    updated_context = [
        dict(message)
        for message in (current_context or [])
        if isinstance(message, Mapping)
    ]

    if history_user_id:
        if not user_message_persisted_early:
            with _maybe_span(operation_name="persist_user_message"):
                add_chat_history_message_fn(
                    user_id=history_user_id,
                    session_id=session_id,
                    message={
                        "role": "user",
                        "content": prompt_text,
                        "author_user_id": user_concept_id,
                    },
                    namespace=user_namespace,
                    organisation_concept_id=org_concept_id,
                    role_in_org=role_in_org,
                    skip_rag_indexing=True,
                )
        with _maybe_span(
            operation_name="persist_tool_messages",
            attributes={"tool_message_count": len(storage_tool_messages)},
        ):
            for tool_msg in storage_tool_messages:
                add_chat_history_message_fn(
                    user_id=history_user_id,
                    session_id=session_id,
                    message=tool_msg,
                    namespace=user_namespace,
                    organisation_concept_id=org_concept_id,
                    role_in_org=role_in_org,
                    skip_rag_indexing=True,
                )
        if callable(refresh_llm_debug_timing_fn):
            refresh_llm_debug_timing_fn(llm_debug_payload)
        with _maybe_span(operation_name="persist_assistant_message"):
            add_chat_history_message_fn(
                user_id=history_user_id,
                session_id=session_id,
                message={"role": "assistant", "content": response_text},
                llm_debug_data=llm_debug_payload,
                namespace=user_namespace,
                organisation_concept_id=org_concept_id,
                role_in_org=role_in_org,
                skip_rag_indexing=True,
            )
    else:
        if callable(refresh_llm_debug_timing_fn):
            refresh_llm_debug_timing_fn(llm_debug_payload)
        updated_context.append({"role": "user", "content": prompt_text})
        updated_context.extend(storage_tool_messages)
        updated_context.append({"role": "assistant", "content": response_text})

    with _maybe_span(
        operation_name="limit_context_after_history_persistence",
        attributes={"context_message_count": len(updated_context)},
    ):
        return limit_context_size_fn(updated_context, max_messages=20)


def _build_generate_success_body(
    *,
    request_id: str,
    session_id: str,
    created_conversation_session_name: str | None,
    created_conversation_session: bool,
    response_text: str,
    success: bool,
    terminal_status: str,
    presenter_channels: dict[str, Any] | None,
    llm_debug_info: Mapping[str, Any],
    display_elements_contract: Mapping[str, Any] | None,
    rag_trace: Any,
    conversation_situation: Mapping[str, Any] | None = None,
    conversation_observations: Sequence[Mapping[str, Any]] | None = None,
    conversation_observation_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "terminal_status": terminal_status,
        "request_id": request_id,
        "session_id": session_id,
        "conversation_session_id": session_id,
        "conversation_session_name": created_conversation_session_name,
        "conversation_session_created": created_conversation_session,
        "response": response_text,
        "response_channels": (
            {
                "screen": presenter_channels.get("screen"),
                "spoken": presenter_channels.get("spoken"),
                "format": presenter_channels.get("format"),
            }
            if isinstance(presenter_channels, dict)
            else None
        ),
        "llm_debug": llm_debug_info,
        "turn_output_health": (
            llm_debug_info.get("turn_output_health")
            if isinstance(llm_debug_info, Mapping)
            and isinstance(llm_debug_info.get("turn_output_health"), Mapping)
            else None
        ),
        "display_elements": display_elements_contract,
        "rag_trace": rag_trace,
        "conversation_situation": (
            dict(conversation_situation)
            if isinstance(conversation_situation, Mapping)
            else None
        ),
        "conversation_observations": [
            dict(observation)
            for observation in (conversation_observations or ())
            if isinstance(observation, Mapping)
        ],
        "conversation_observation_state": (
            dict(conversation_observation_state)
            if isinstance(conversation_observation_state, Mapping)
            else None
        ),
    }


def _build_generate_error_debug_info(
    *,
    interaction_timestamp_utc: str,
    request_id: str | None,
    model_name: str | None,
    prompt_text: str | None,
    error_text: str,
    enhanced_context: Sequence[Mapping[str, Any]] | None,
    user_prompt_debug: Mapping[str, Any] | None,
    response_transformations: Mapping[str, Any] | None,
    error_tool_progress_snapshot: Mapping[str, Any] | None,
    error_workflow_discovery: Mapping[str, Any] | None,
    error_workflow_routing: Mapping[str, Any] | None,
    auxiliary_llm_calls: Sequence[Mapping[str, Any]] | None,
    llm_interaction: Mapping[str, Any] | None,
    error_elapsed_ms: float | None,
    session_id: str | None,
    namespace: str | None,
    history_user_id: str | None,
    org_concept_id: str | None,
    calculate_context_stats_fn: Callable[[list[dict[str, Any]]], dict[str, Any]],
    build_response_transformation_telemetry_payload_fn: Callable[..., dict[str, Any]],
    build_turn_execution_diagnostics_fn: Callable[..., dict[str, Any]],
    finalise_llm_debug_info_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    context_stats = None
    if enhanced_context is not None:
        try:
            normalised_context = [
                dict(message)
                for message in enhanced_context
                if isinstance(message, Mapping)
            ]
            context_stats = {
                "sent_to_llm": calculate_context_stats_fn(normalised_context)
            }
        except Exception:
            context_stats = None

    normalised_response_transformations = (
        dict(response_transformations)
        if isinstance(response_transformations, Mapping)
        else build_response_transformation_telemetry_payload_fn(request_id=request_id)
    )
    normalised_tool_progress_snapshot = (
        dict(error_tool_progress_snapshot)
        if isinstance(error_tool_progress_snapshot, Mapping)
        else None
    )
    normalised_workflow_discovery = (
        dict(error_workflow_discovery)
        if isinstance(error_workflow_discovery, Mapping)
        else None
    )
    normalised_workflow_routing = (
        dict(error_workflow_routing)
        if isinstance(error_workflow_routing, Mapping)
        else None
    )
    normalised_auxiliary_llm_calls = (
        [dict(item) for item in auxiliary_llm_calls if isinstance(item, Mapping)]
        if auxiliary_llm_calls is not None
        else None
    )
    normalised_llm_interaction = (
        dict(llm_interaction) if isinstance(llm_interaction, Mapping) else None
    )

    error_debug_info = {
        "interaction_timestamp_utc": interaction_timestamp_utc,
        "request_id": request_id,
        "model": model_name or "unknown",
        "messages": (
            [{"role": "user", "content": prompt_text}]
            if isinstance(prompt_text, str)
            else []
        ),
        "response": None,
        "error": error_text,
        "context_stats": context_stats,
        "user_prompt": user_prompt_debug,
        "tool_invocations": [],
        "response_transformations": normalised_response_transformations,
        "llm_interaction": normalised_llm_interaction,
        "turn_execution_diagnostics": build_turn_execution_diagnostics_fn(
            request_id=request_id,
            prompt_text=prompt_text,
            elapsed_ms=error_elapsed_ms,
            tool_progress_state=normalised_tool_progress_snapshot,
            workflow_discovery=normalised_workflow_discovery,
            workflow_routing=normalised_workflow_routing,
            aux_llm_calls=normalised_auxiliary_llm_calls,
        ),
    }
    return finalise_llm_debug_info_fn(
        llm_debug_info=error_debug_info,
        prompt_text=prompt_text,
        response_text=None,
        session_id=session_id,
        namespace=namespace,
        user_id=history_user_id,
        org_id=org_concept_id,
        workflow_discovery=normalised_workflow_discovery,
        workflow_routing=normalised_workflow_routing,
    )


def _build_generate_error_body(
    *,
    request_id: str | None,
    error_text: str,
    error_debug_info: Mapping[str, Any],
    rag_trace: Any = None,
    namespace_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": request_id,
        "error": error_text,
        "llm_debug": error_debug_info,
    }
    if rag_trace is not None:
        body["rag_trace"] = rag_trace
    if isinstance(namespace_report, Mapping):
        body["namespace_report"] = dict(namespace_report)
    return body
