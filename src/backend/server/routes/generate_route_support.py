from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Callable, Mapping, Sequence, cast

from src.backend.services.debug_payload_store import (
    compact_debug_payload_for_storage,
    default_tool_message_threshold_bytes,
)
from src.backend.services.python_decision_authority_service import (
    annotate_python_decision_event,
)

from ...workflows import CONVERSATION_TURN_EXECUTION_WORKFLOW_ID

SCREEN_BACKFILL_STAGE_CONCEPT_ID = "#V#screen_backfill_stage"
SCREEN_BACKFILL_STAGE_PROMPT_IDS = ("#V#von_screen_content_prompt_for_witbrock",)


def _is_agent_test_instance() -> bool:
    value = os.getenv("VON_AGENT_TEST_INSTANCE", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class _GenerateConversationTurnInstanceState:
    manager: Any | None = None
    instance_id: str | None = None
    created_new: bool | None = None
    finalised: bool = False


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


def _compact_presenter_unresolved_preconditions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return []
    compacted: list[dict[str, Any]] = []
    for item in value:
        entry = _compact_presenter_context_mapping(
            item,
            scalar_fields=(
                "effect_id",
                "effect_type",
                "status",
                "status_reason",
                "failure_code",
                "workflow_id",
                "workflow_instance_id",
                "tool_name",
                "predicate",
                "concept_id",
                "contract_id",
            ),
            list_fields=("failure_codes", "required_tools", "evidence_keys"),
        )
        if entry:
            compacted.append(entry)
        if len(compacted) >= 12:
            break
    return compacted


def _compact_completion_gate_for_presenter_context(
    completion_gate: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    compacted = _compact_presenter_context_mapping(
        completion_gate,
        scalar_fields=(
            "decision",
            "decision_reason",
            "requires_follow_up",
            "safe_to_claim_completion",
            "workflow_id",
            "workflow_instance_id",
        ),
        list_fields=("blocking_effect_ids", "blocking_failure_codes"),
    )
    if isinstance(completion_gate, Mapping):
        unresolved = _compact_presenter_unresolved_preconditions(
            completion_gate.get("unresolved_preconditions")
        )
        if unresolved:
            compacted["unresolved_preconditions"] = unresolved
    if not compacted:
        return None
    compacted["schema_version"] = "turn_completion_gate_presenter_context.v1"
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
    completion_gate: Mapping[str, Any] | None = None,
    workflow_execution_summary: Mapping[str, Any] | None = None,
    response_candidate_blocked_by_completion_gate: bool = False,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    prompt_ids = _normalise_prompt_concept_ids(prompt_concept_ids)
    if not prompt_ids:
        prompt_ids = list(SCREEN_BACKFILL_STAGE_PROMPT_IDS)
    completion_gate_context = _compact_completion_gate_for_presenter_context(
        completion_gate
    )
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
            "response_candidate_blocked_by_completion_gate": bool(
                response_candidate_blocked_by_completion_gate
            ),
        },
        "completion_gate": completion_gate_context,
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
        "completion_gate_present": bool(completion_gate_context),
        "completion_gate_requires_follow_up": bool(
            completion_gate_context
            and completion_gate_context.get("requires_follow_up") is True
        ),
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
    orchestrator: Any,
    llm_client: Any,
    represented_screen_prompt: str,
    context_messages: Sequence[Mapping[str, Any]],
    context_telemetry: Mapping[str, Any],
    model_name: str,
    request_language: str,
    user_concept_id: str | None,
    org_concept_id: str | None,
    llm_calls_log: list[dict[str, Any]],
    auxiliary_llm_calls: list[dict[str, Any]],
    record_stage_llm_call: Callable[..., None],
    emit_stage_progress: Callable[[Mapping[str, Any]], None],
    infer_provider: Callable[[str | None], str | None],
) -> tuple[Any, str]:
    screen_model_used = model_name
    screen_context_messages = [dict(message) for message in context_messages]

    if orchestrator is not None and hasattr(orchestrator, "_run_llm_with_fallbacks"):
        try:
            policy_state, registry_snapshot = orchestrator._load_workflow_model_policy(
                request_language
            )
            synthesis_response, screen_model_used, _ = orchestrator._run_llm_with_fallbacks(
                stage="screen_backfill",
                prompt=represented_screen_prompt,
                context=screen_context_messages,
                default_client=llm_client,
                default_model=model_name,
                policy_state=policy_state,
                registry_snapshot=(
                    registry_snapshot if isinstance(registry_snapshot, Mapping) else None
                ),
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
                llm_calls_log=llm_calls_log,
                aux_log=auxiliary_llm_calls,
                record_llm_call=record_stage_llm_call,
                emit_progress=emit_stage_progress,
                context_telemetry=context_telemetry,
                workflow_stage_id=SCREEN_BACKFILL_STAGE_CONCEPT_ID,
            )
            return synthesis_response, screen_model_used
        except Exception:
            pass

    llm_start = time.perf_counter()
    emit_stage_progress(
        {"status": "llm_call_start", "stage": "screen_backfill", "model": model_name}
    )
    synthesis_response = llm_client.generate(
        prompt=represented_screen_prompt,
        context=screen_context_messages,
        model=model_name,
    )
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
    emit_stage_progress(
        {
            "status": "llm_call_end",
            "stage": "screen_backfill",
            "model": model_name,
            "duration_ms": int(screen_duration_ms),
            "success": True,
            "error": None,
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
    )
    return synthesis_response, model_name


def _append_generate_conversation_turn_instance_event(
    *,
    auxiliary_llm_calls: list[dict[str, Any]],
    state: _GenerateConversationTurnInstanceState,
    session_id: str,
    request_id: str,
    user_namespace: str | None,
    user_concept_id: str | None,
    org_concept_id: str | None,
    status: str,
    reason_code: str | None = None,
    error: str | None = None,
    submission_payload: Mapping[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "type": "conversation_turn_instance",
        "path": "/von/generate",
        "status": str(status or "").strip() or "unknown",
        "workflow_id": CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        "workflow_instance_id": state.instance_id,
        "workflow_instance_created_new": state.created_new,
        "session_id": session_id,
        "turn_id": request_id,
        "namespace": user_namespace,
        "user_concept_id": user_concept_id,
        "org_concept_id": org_concept_id,
    }
    if isinstance(reason_code, str) and reason_code.strip():
        payload["reason_code"] = reason_code.strip()
    if isinstance(error, str) and error.strip():
        payload["error"] = error.strip()
    if isinstance(submission_payload, Mapping):
        payload["submission"] = dict(submission_payload)
    auxiliary_llm_calls.append(payload)


def _build_generate_conversation_turn_instance_inputs(
    *,
    session_id: str,
    request_id: str,
    namespace_source: str,
    presenter_mode_requested: bool,
    request_gmail_profile: str | None,
    request_language: str,
    requested_model: str | None,
    requested_model_parameters: Mapping[str, Any] | None,
    requested_client_type: str | None,
    prompt_text: str,
    workflow_discovery_result: Mapping[str, Any] | None,
    workflow_continuation_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "conversation_session_id": session_id,
        "turn_id": request_id,
        "namespace_source": namespace_source,
        "presenter_mode_requested": presenter_mode_requested,
        "gmail_profile": request_gmail_profile,
        "preferred_language": request_language,
        "requested_model": requested_model,
        "requested_model_parameters": (
            dict(requested_model_parameters)
            if isinstance(requested_model_parameters, Mapping)
            else None
        ),
        "requested_client_type": requested_client_type,
        "prompt": prompt_text if isinstance(prompt_text, str) else "",
        "user_prompt": prompt_text if isinstance(prompt_text, str) else "",
        "prompt_preview": prompt_text[:1000] if isinstance(prompt_text, str) else None,
        "workflow_discovery_result": (
            dict(workflow_discovery_result)
            if isinstance(workflow_discovery_result, Mapping)
            else None
        ),
        "workflow_discovery": (
            dict(workflow_discovery_result)
            if isinstance(workflow_discovery_result, Mapping)
            else None
        ),
        "workflow_continuation_context": (
            dict(workflow_continuation_context)
            if isinstance(workflow_continuation_context, Mapping)
            else None
        ),
        "continuation_context": (
            dict(workflow_continuation_context)
            if isinstance(workflow_continuation_context, Mapping)
            else None
        ),
    }


def _build_generate_conversation_turn_instance_outputs(
    *,
    request_id: str,
    session_id: str,
    presenter_mode_requested: bool,
    completed: bool,
    final_state: str,
    debug_payload: Mapping[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    outputs: dict[str, Any] = {
        "request_id": request_id,
        "session_id": session_id,
        "completed": bool(completed),
        "final_state": final_state,
        "presenter_mode_requested": presenter_mode_requested,
    }
    if isinstance(error, str) and error.strip():
        outputs["error"] = error.strip()
    if not isinstance(debug_payload, Mapping):
        return outputs

    response_value = debug_payload.get("response")
    if isinstance(response_value, str) and response_value.strip():
        outputs["response"] = response_value
        outputs["response_preview"] = response_value[:1000]

    tool_invocation_payload = debug_payload.get("tool_invocations")
    if isinstance(tool_invocation_payload, list):
        outputs["tool_invocation_count"] = len(tool_invocation_payload)

    for key in (
        "workflow_discovery",
        "workflow_routing",
        "turn_execution_record",
        "turn_execution_diagnostics",
        "response_transformations",
        "display_elements",
    ):
        value = debug_payload.get(key)
        if isinstance(value, Mapping):
            outputs[key] = dict(value)

    workflow_use_episodes = debug_payload.get("workflow_use_episodes")
    if isinstance(workflow_use_episodes, list):
        outputs["workflow_use_episodes"] = [
            dict(item) for item in workflow_use_episodes if isinstance(item, Mapping)
        ]

    llm_interaction_payload = debug_payload.get("llm_interaction")
    if isinstance(llm_interaction_payload, Mapping):
        outputs["llm_interaction"] = {
            "requested_model": llm_interaction_payload.get("requested_model"),
            "requested_model_parameters": llm_interaction_payload.get(
                "requested_model_parameters"
            ),
            "orchestrator_used": llm_interaction_payload.get("orchestrator_used"),
            "duration_ms": llm_interaction_payload.get("duration_ms"),
            "orchestrator_duration_ms": llm_interaction_payload.get(
                "orchestrator_duration_ms"
            ),
            "server_elapsed_ms": llm_interaction_payload.get("server_elapsed_ms"),
        }

    return outputs


def _submit_generate_conversation_turn_instance(
    *,
    state: _GenerateConversationTurnInstanceState,
    auxiliary_llm_calls: list[dict[str, Any]],
    session_id: str,
    request_id: str,
    user_namespace: str | None,
    user_concept_id: str | None,
    org_concept_id: str | None,
    namespace_source: str,
    presenter_mode_requested: bool,
    request_gmail_profile: str | None,
    request_language: str,
    requested_model: str | None,
    requested_model_parameters: Mapping[str, Any] | None,
    requested_client_type: str | None,
    prompt_text: str,
    workflow_discovery_result: Mapping[str, Any] | None,
    workflow_continuation_context: Mapping[str, Any] | None,
    get_instance_manager_fn: Callable[[], Any],
    submit_verified_workflow_instance_fn: Callable[..., Any],
    background_task_id: str | None = None,
    logger: logging.Logger | None = None,
) -> None:
    if not isinstance(user_namespace, str) or not user_namespace.strip():
        _append_generate_conversation_turn_instance_event(
            auxiliary_llm_calls=auxiliary_llm_calls,
            state=state,
            session_id=session_id,
            request_id=request_id,
            user_namespace=user_namespace,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
            status="submission_skipped",
            reason_code="missing_namespace",
        )
        return

    from ...services.namespace_service import derive_actor_context_from_namespace

    namespace_user_id, namespace_org_id = derive_actor_context_from_namespace(
        user_namespace
    )
    effective_user_id = (
        user_concept_id
        if isinstance(user_concept_id, str) and user_concept_id.strip()
        else namespace_user_id
    )
    effective_org_id = (
        org_concept_id
        if isinstance(org_concept_id, str) and org_concept_id.strip()
        else namespace_org_id
    )
    if not isinstance(effective_user_id, str) or not effective_user_id.strip():
        _append_generate_conversation_turn_instance_event(
            auxiliary_llm_calls=auxiliary_llm_calls,
            state=state,
            session_id=session_id,
            request_id=request_id,
            user_namespace=user_namespace,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
            status="submission_skipped",
            reason_code="missing_user_context",
        )
        return

    if _is_agent_test_instance():
        _append_generate_conversation_turn_instance_event(
            auxiliary_llm_calls=auxiliary_llm_calls,
            state=state,
            session_id=session_id,
            request_id=request_id,
            user_namespace=user_namespace,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
            status="submission_skipped",
            reason_code="agent_test_instance",
        )
        return

    if isinstance(background_task_id, str) and background_task_id.strip():
        _append_generate_conversation_turn_instance_event(
            auxiliary_llm_calls=auxiliary_llm_calls,
            state=state,
            session_id=session_id,
            request_id=request_id,
            user_namespace=user_namespace,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
            status="submission_skipped",
            reason_code="background_task_reentry",
        )
        return

    route_logger = logger or logging.getLogger(__name__)
    submission = None
    try:
        state.manager = get_instance_manager_fn()
        submission = submit_verified_workflow_instance_fn(
            manager=state.manager,
            workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
            user_id=effective_user_id,
            org_id=effective_org_id,
            namespace=user_namespace,
            inputs=_build_generate_conversation_turn_instance_inputs(
                session_id=session_id,
                request_id=request_id,
                namespace_source=namespace_source,
                presenter_mode_requested=presenter_mode_requested,
                request_gmail_profile=request_gmail_profile,
                request_language=request_language,
                requested_model=requested_model,
                requested_model_parameters=requested_model_parameters,
                requested_client_type=requested_client_type,
                prompt_text=prompt_text,
                workflow_discovery_result=workflow_discovery_result,
                workflow_continuation_context=workflow_continuation_context,
            ),
            max_retries=0,
            source_event_type="conversation_turn",
            source_event_id=request_id,
            event_idempotency_key=(
                f"von.generate:conversation_turn:{session_id}:{request_id}"
            ),
            # The supervised generate path executes the turn in-process and
            # finalises this instance itself; worker auto-claim would
            # re-execute the same turn (JVNAUTOSCI-2503).
            auto_claim_enabled=False,
        )
        submission_payload = submission.to_dict()
        if not submission.success or not submission.instance_id:
            _append_generate_conversation_turn_instance_event(
                auxiliary_llm_calls=auxiliary_llm_calls,
                state=state,
                session_id=session_id,
                request_id=request_id,
                user_namespace=user_namespace,
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
                status="submission_failed",
                reason_code=submission.error_code or "submission_failed",
                error=submission.error,
                submission_payload=submission_payload,
            )
            return
        state.instance_id = submission.instance_id
        state.created_new = submission.created_new
        _append_generate_conversation_turn_instance_event(
            auxiliary_llm_calls=auxiliary_llm_calls,
            state=state,
            session_id=session_id,
            request_id=request_id,
            user_namespace=user_namespace,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
            status="submitted",
            reason_code=(
                "durable_instance_reused"
                if submission.created_new is False
                else "durable_instance_created"
            ),
            submission_payload=submission_payload,
        )
    except Exception as exc:
        _append_generate_conversation_turn_instance_event(
            auxiliary_llm_calls=auxiliary_llm_calls,
            state=state,
            session_id=session_id,
            request_id=request_id,
            user_namespace=user_namespace,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
            status="submission_failed",
            reason_code=type(exc).__name__,
            error=str(exc),
            submission_payload=(
                submission.to_dict()
                if submission is not None
                and callable(getattr(submission, "to_dict", None))
                else None
            ),
        )
        route_logger.warning(
            "[workflow_instance_telemetry] Failed to create conversation "
            "turn durable instance for request %s: %s",
            request_id,
            exc,
        )
        state.manager = None
        state.instance_id = None
        state.created_new = None


def _finalise_generate_conversation_turn_instance(
    *,
    state: _GenerateConversationTurnInstanceState,
    auxiliary_llm_calls: list[dict[str, Any]],
    session_id: str,
    request_id: str,
    user_namespace: str | None,
    user_concept_id: str | None,
    org_concept_id: str | None,
    presenter_mode_requested: bool,
    completed: bool,
    final_state: str,
    debug_payload: Mapping[str, Any] | None,
    error: str | None = None,
    logger: logging.Logger | None = None,
) -> None:
    if (
        state.finalised
        or state.manager is None
        or not isinstance(state.instance_id, str)
        or not state.instance_id.strip()
    ):
        return

    runtime_payload: dict[str, Any] = {
        "schema_version": "conversation_turn_runtime.v1",
        "request_id": request_id,
        "session_id": session_id,
        "completed": bool(completed),
        "final_state": final_state,
        "presenter_mode_requested": presenter_mode_requested,
        "namespace": user_namespace,
    }
    if isinstance(debug_payload, Mapping):
        for key in (
            "workflow_routing",
            "workflow_discovery",
            "turn_execution_record",
            "turn_execution_diagnostics",
        ):
            value = debug_payload.get(key)
            if isinstance(value, Mapping):
                runtime_payload[key] = dict(value)

    route_logger = logger or logging.getLogger(__name__)
    try:
        state.manager.checkpoint(
            state.instance_id,
            current_state=final_state,
            workflow_data={"conversation_turn_runtime": runtime_payload},
            progress_message="conversation_turn_runtime_persisted",
        )
        if completed:
            state.manager.mark_completed(
                state.instance_id,
                outputs=_build_generate_conversation_turn_instance_outputs(
                    request_id=request_id,
                    session_id=session_id,
                    presenter_mode_requested=presenter_mode_requested,
                    completed=True,
                    final_state=final_state,
                    debug_payload=debug_payload,
                ),
                final_state=final_state,
            )
        else:
            state.manager.mark_failed(
                state.instance_id,
                error=(
                    str(error).strip()
                    if isinstance(error, str) and error.strip()
                    else "conversation_turn_failed"
                ),
                error_step=final_state,
                increment_retry=False,
            )
        state.finalised = True
    except Exception as exc:
        route_logger.warning(
            "[workflow_instance_telemetry] Failed to finalise conversation "
            "turn durable instance %s for request %s: %s",
            state.instance_id,
            request_id,
            exc,
        )
        _append_generate_conversation_turn_instance_event(
            auxiliary_llm_calls=auxiliary_llm_calls,
            state=state,
            session_id=session_id,
            request_id=request_id,
            user_namespace=user_namespace,
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
            status="finalisation_failed",
            reason_code=type(exc).__name__,
            error=str(exc),
        )


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
    presenter_channels: dict[str, Any] | None,
    llm_debug_info: Mapping[str, Any],
    display_elements_contract: Mapping[str, Any] | None,
    rag_trace: Any,
) -> dict[str, Any]:
    return {
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
