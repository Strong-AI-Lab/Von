"""Read-only access to persisted turn-execution diagnostics payloads."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .chat_history_service import get_chat_history_collection_service
from .turn_execution_record_service import get_turn_execution_records_collection
from ..workflows.conversation_turn_stage_model import (
    build_conversation_turn_stage_model_snapshot,
    build_conversation_turn_stage_path,
)

TURN_EXECUTION_DIAGNOSTICS_SCHEMA_VERSION = "turn_execution_diagnostics.v1"
_PROMPT_PREVIEW_LIMIT = 1000


class TurnExecutionDiagnosticsServiceError(RuntimeError):
    """Raised when diagnostics retrieval fails unexpectedly."""


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _safe_mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _query_chat_history_document(
    *,
    request_id: str,
    namespace: str | None,
) -> dict[str, Any] | None:
    coll = get_chat_history_collection_service(read_only=True)
    if coll is None:
        return None

    query: dict[str, Any] = {
        "history": {
            "$elemMatch": {
                "role": "assistant",
                "llm_debug_data.request_id": request_id,
            }
        }
    }
    if namespace:
        query["namespace"] = namespace

    projection = {
        "_id": 0,
        "user_id": 1,
        "session_id": 1,
        "namespace": 1,
        "organisation_concept_id": 1,
        "history": 1,
    }

    try:
        doc = coll.find_one(query, projection)
    except Exception as exc:  # pragma: no cover - defensive
        raise TurnExecutionDiagnosticsServiceError(
            f"Could not query chat_history for request_id={request_id}: {exc}"
        ) from exc
    return _safe_mapping(doc)


def _extract_prompt_text_from_debug(llm_debug_data: Mapping[str, Any] | None) -> str | None:
    if not isinstance(llm_debug_data, Mapping):
        return None

    direct_prompt = _safe_str(llm_debug_data.get("prompt_text"))
    if direct_prompt:
        return direct_prompt

    messages = llm_debug_data.get("messages")
    if isinstance(messages, list):
        for item in reversed(messages):
            if not isinstance(item, Mapping):
                continue
            if item.get("role") != "user":
                continue
            content = _safe_str(item.get("content"))
            if content:
                return content

    user_prompt = llm_debug_data.get("user_prompt")
    if isinstance(user_prompt, Mapping):
        for key in ("prompt_text", "content", "preview"):
            content = _safe_str(user_prompt.get(key))
            if content:
                return content

    return None


def _resolve_history_context(
    *,
    request_id: str,
    namespace: str | None,
) -> dict[str, Any] | None:
    doc = _query_chat_history_document(request_id=request_id, namespace=namespace)
    if not isinstance(doc, Mapping):
        return None

    history = doc.get("history")
    if not isinstance(history, list):
        return None

    target_index: int | None = None
    target_message: dict[str, Any] | None = None
    target_llm_debug: dict[str, Any] | None = None

    for index, raw_message in enumerate(history):
        if not isinstance(raw_message, Mapping):
            continue
        if raw_message.get("role") != "assistant":
            continue
        llm_debug = raw_message.get("llm_debug_data")
        if not isinstance(llm_debug, Mapping):
            continue
        if _safe_str(llm_debug.get("request_id")) != request_id:
            continue
        target_index = index
        target_message = dict(raw_message)
        target_llm_debug = dict(llm_debug)
        break

    if target_message is None or target_llm_debug is None:
        return None

    prompt_text = _extract_prompt_text_from_debug(target_llm_debug)
    if prompt_text is None and target_index is not None:
        for prior_index in range(target_index - 1, -1, -1):
            prior = history[prior_index]
            if not isinstance(prior, Mapping):
                continue
            if prior.get("role") != "user":
                continue
            prompt_text = _safe_str(prior.get("content"))
            if prompt_text:
                break

    return {
        "user_id": _safe_str(doc.get("user_id")),
        "session_id": _safe_str(doc.get("session_id")),
        "namespace": _safe_str(doc.get("namespace")),
        "org_id": _safe_str(doc.get("organisation_concept_id")),
        "target_index": target_index,
        "target_message": target_message,
        "target_llm_debug": target_llm_debug,
        "prompt_text": prompt_text,
    }


def _load_turn_execution_record(
    *,
    request_id: str,
    namespace: str | None,
) -> dict[str, Any] | None:
    coll = get_turn_execution_records_collection()
    if coll is None:
        return None

    query: dict[str, Any] = {"request_id": request_id}
    if namespace:
        query["namespace"] = namespace
    try:
        doc = coll.find_one(query, {"_id": 0})
    except Exception as exc:  # pragma: no cover - defensive
        raise TurnExecutionDiagnosticsServiceError(
            f"Could not query turn_execution_records for request_id={request_id}: {exc}"
        ) from exc
    return _safe_mapping(doc)


def _normalise_tool_history(tool_history: Any) -> list[dict[str, Any]]:
    if isinstance(tool_history, list):
        return [dict(entry) for entry in tool_history if isinstance(entry, Mapping)]
    return []


def _derive_tool_counts(tool_history: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    total = 0
    success = 0
    failure = 0
    ended = 0
    for entry in tool_history:
        if not isinstance(entry, Mapping):
            continue
        total += 1
        status = (_safe_str(entry.get("status")) or "").lower()
        if status in {"completed", "complete", "success", "succeeded"}:
            success += 1
            ended += 1
        elif status in {"failed", "failure", "error", "blocked", "cancelled", "canceled"}:
            failure += 1
            ended += 1
    pending = max(0, total - ended)
    return {
        "tool_call_count": total,
        "tool_success_count": success,
        "tool_failure_count": failure,
        "tool_pending_count": pending,
        "tool_call_start_count": total,
        "tool_call_end_count": ended,
    }


def _normalise_llm_call_rows(llm_calls: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(llm_calls, list):
        for entry in llm_calls:
            if not isinstance(entry, Mapping):
                continue
            duration_raw = entry.get("duration_ms")
            if isinstance(duration_raw, bool):
                continue
            duration_ms: int | None = None
            if isinstance(duration_raw, (int, float)):
                duration_ms = int(max(0.0, float(duration_raw)))
            rows.append(
                {
                    "stage": _safe_str(entry.get("stage"))
                    or _safe_str(entry.get("phase"))
                    or "unscoped",
                    "model": _safe_str(entry.get("model")) or "unknown",
                    "provider": _safe_str(entry.get("provider")),
                    "duration_ms": duration_ms or 0,
                }
            )
    return rows


def _build_minimal_timing_breakdown(
    *,
    llm_debug: Mapping[str, Any] | None,
) -> dict[str, Any]:
    llm_rows = _normalise_llm_call_rows(llm_debug.get("llm_calls") if isinstance(llm_debug, Mapping) else None)
    if not llm_rows and isinstance(llm_debug, Mapping):
        interaction = llm_debug.get("llm_interaction")
        if isinstance(interaction, Mapping):
            duration_raw = interaction.get("duration_ms") or interaction.get("server_elapsed_ms")
            if isinstance(duration_raw, (int, float)):
                llm_rows.append(
                    {
                        "stage": "unscoped",
                        "model": _safe_str(llm_debug.get("model")) or "unknown",
                        "provider": None,
                        "duration_ms": int(max(0.0, float(duration_raw))),
                    }
                )

    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    total_ms = 0
    for row in llm_rows:
        stage = str(row["stage"])
        model = str(row["model"])
        key = (stage, model)
        bucket = aggregated.get(key)
        if bucket is None:
            bucket = {
                "stage": stage,
                "model": model,
                "provider": row.get("provider"),
                "call_count": 0,
                "duration_ms": 0,
            }
            aggregated[key] = bucket
        bucket["call_count"] = int(bucket["call_count"]) + 1
        bucket["duration_ms"] = int(bucket["duration_ms"]) + int(row["duration_ms"])
        if bucket.get("provider") is None and row.get("provider") is not None:
            bucket["provider"] = row.get("provider")
        total_ms += int(row["duration_ms"])

    elapsed_ms: int | None = None
    if isinstance(llm_debug, Mapping):
        interaction = llm_debug.get("llm_interaction")
        if isinstance(interaction, Mapping):
            elapsed_raw = interaction.get("server_elapsed_ms") or interaction.get("duration_ms")
            if isinstance(elapsed_raw, (int, float)):
                elapsed_ms = int(max(0.0, float(elapsed_raw)))

    return {
        "schema_version": "conversation_turn_timing_breakdown.v1",
        "stages": [],
        "llm_calls_by_stage_model": list(aggregated.values()),
        "totals": {
            "elapsed_ms": elapsed_ms,
            "observed_timeline_ms": None,
            "phase_elapsed_ms": 0,
            "llm_elapsed_ms": total_ms,
            "llm_call_count": len(llm_rows),
        },
    }


def _build_fallback_turn_execution_diagnostics(
    *,
    request_id: str,
    history_context: Mapping[str, Any] | None,
    turn_record: Mapping[str, Any] | None,
) -> dict[str, Any]:
    history_context_mapping = _safe_mapping(history_context)
    llm_debug_mapping = (
        _safe_mapping(history_context_mapping.get("target_llm_debug"))
        if history_context_mapping
        else None
    )
    target_message_mapping = (
        _safe_mapping(history_context_mapping.get("target_message"))
        if history_context_mapping
        else None
    )

    execution = (
        _safe_mapping(turn_record.get("execution"))
        if isinstance(turn_record, Mapping)
        else None
    )
    workflow_stage_model_snapshot = execution.get("workflow_stage_model") if execution else None
    workflow_stage_model = (
        deepcopy(workflow_stage_model_snapshot)
        if isinstance(workflow_stage_model_snapshot, Mapping)
        else build_conversation_turn_stage_model_snapshot()
    )
    selected_workflow_id = None
    workflow_selection = (
        _safe_mapping(turn_record.get("workflow_selection"))
        if isinstance(turn_record, Mapping)
        else None
    )
    if workflow_selection:
        selected_workflow_id = _safe_str(workflow_selection.get("selected_workflow_id"))

    workflow_stage_path_snapshot = execution.get("workflow_stage_path") if execution else None
    workflow_stage_path = (
        deepcopy(workflow_stage_path_snapshot)
        if isinstance(workflow_stage_path_snapshot, Mapping)
        else build_conversation_turn_stage_path(
            runtime_stages=(),
            workflow_id=selected_workflow_id,
        )
    )

    tool_history = _normalise_tool_history(
        execution.get("tool_invocations") if execution else None
    )
    tool_counts = _derive_tool_counts(tool_history)

    workflow_routing_diagnostics = None
    if isinstance(llm_debug_mapping, Mapping) and isinstance(
        llm_debug_mapping.get("workflow_routing_diagnostics"), Mapping
    ):
        workflow_routing_diagnostics = deepcopy(
            llm_debug_mapping.get("workflow_routing_diagnostics")
        )
    elif isinstance(turn_record, Mapping) and isinstance(
        turn_record.get("workflow_routing_diagnostics"), Mapping
    ):
        workflow_routing_diagnostics = deepcopy(
            turn_record.get("workflow_routing_diagnostics")
        )

    workflow_discovery = None
    if isinstance(llm_debug_mapping, Mapping) and isinstance(
        llm_debug_mapping.get("workflow_discovery"), Mapping
    ):
        workflow_discovery = deepcopy(llm_debug_mapping.get("workflow_discovery"))
    elif isinstance(workflow_selection, Mapping) and isinstance(
        workflow_selection.get("workflow_discovery"), Mapping
    ):
        workflow_discovery = deepcopy(workflow_selection.get("workflow_discovery"))

    prompt_text = history_context_mapping.get("prompt_text") if history_context_mapping else None
    prompt_preview = (
        str(prompt_text)[:_PROMPT_PREVIEW_LIMIT]
        if isinstance(prompt_text, str)
        else None
    )
    if prompt_preview is None and isinstance(turn_record, Mapping):
        prompt_payload = turn_record.get("prompt")
        if isinstance(prompt_payload, Mapping):
            prompt_preview = _safe_str(prompt_payload.get("preview"))

    generated_at_utc = None
    if isinstance(llm_debug_mapping, Mapping):
        generated_at_utc = _safe_str(llm_debug_mapping.get("interaction_timestamp_utc"))
    if generated_at_utc is None and isinstance(target_message_mapping, Mapping):
        generated_at_utc = _safe_str(target_message_mapping.get("timestamp"))

    code_version = None
    code_version_details = None
    if isinstance(llm_debug_mapping, Mapping):
        code_version = _safe_str(llm_debug_mapping.get("code_version"))
        if isinstance(llm_debug_mapping.get("code_version_details"), Mapping):
            code_version_details = deepcopy(llm_debug_mapping.get("code_version_details"))

    return {
        "schema_version": TURN_EXECUTION_DIAGNOSTICS_SCHEMA_VERSION,
        "request_id": request_id,
        "generated_at_utc": generated_at_utc or _now_utc_iso(),
        "code_version": code_version,
        "code_version_details": code_version_details,
        "elapsed_ms": (
            _build_minimal_timing_breakdown(llm_debug=llm_debug_mapping)
            .get("totals", {})
            .get("elapsed_ms")
        ),
        "prompt_preview": prompt_preview,
        "latest_progress": None,
        "progress_events": [],
        "activity_history": [],
        "phase_history": [],
        "tool_history": tool_history,
        "workflow_discovery": workflow_discovery,
        "workflow_routing_diagnostics": workflow_routing_diagnostics,
        "workflow_stage_model": workflow_stage_model,
        "workflow_stage_path": workflow_stage_path,
        "stage_diagnostics": [],
        "timing_breakdown": _build_minimal_timing_breakdown(llm_debug=llm_debug_mapping),
        "reconstruction": {
            "source": (
                "chat_history.llm_debug_data"
                if isinstance(llm_debug_mapping, Mapping)
                else "mongo.turn_execution_records"
            ),
            "reason": "embedded_turn_execution_diagnostics_missing",
            "lossy": True,
        },
        **tool_counts,
    }


def _normalise_embedded_diagnostics_payload(
    *,
    request_id: str,
    embedded: Mapping[str, Any],
    history_context: Mapping[str, Any] | None,
    turn_record: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = deepcopy(dict(embedded))
    history_context_mapping = _safe_mapping(history_context)
    llm_debug_mapping = (
        _safe_mapping(history_context_mapping.get("target_llm_debug"))
        if history_context_mapping
        else None
    )
    execution = (
        _safe_mapping(turn_record.get("execution"))
        if isinstance(turn_record, Mapping)
        else None
    )

    payload.setdefault("schema_version", TURN_EXECUTION_DIAGNOSTICS_SCHEMA_VERSION)
    payload.setdefault("request_id", request_id)

    generated_at_utc = _safe_str(payload.get("generated_at_utc"))
    if generated_at_utc is None and isinstance(llm_debug_mapping, Mapping):
        generated_at_utc = _safe_str(llm_debug_mapping.get("interaction_timestamp_utc"))
    if generated_at_utc is None:
        target_message = (
            history_context_mapping.get("target_message")
            if history_context_mapping
            else None
        )
        if isinstance(target_message, Mapping):
            generated_at_utc = _safe_str(target_message.get("timestamp"))
    payload["generated_at_utc"] = generated_at_utc or _now_utc_iso()

    if not _safe_str(payload.get("prompt_preview")):
        prompt_text = history_context_mapping.get("prompt_text") if history_context_mapping else None
        if isinstance(prompt_text, str):
            payload["prompt_preview"] = prompt_text[:_PROMPT_PREVIEW_LIMIT]

    if not _safe_str(payload.get("code_version")) and isinstance(llm_debug_mapping, Mapping):
        code_version = _safe_str(llm_debug_mapping.get("code_version"))
        if code_version:
            payload["code_version"] = code_version
    if not isinstance(payload.get("code_version_details"), Mapping) and isinstance(
        llm_debug_mapping, Mapping
    ) and isinstance(llm_debug_mapping.get("code_version_details"), Mapping):
        payload["code_version_details"] = deepcopy(
            llm_debug_mapping.get("code_version_details")
        )

    if not isinstance(payload.get("workflow_discovery"), Mapping):
        workflow_discovery = None
        if isinstance(llm_debug_mapping, Mapping) and isinstance(
            llm_debug_mapping.get("workflow_discovery"), Mapping
        ):
            workflow_discovery = llm_debug_mapping.get("workflow_discovery")
        elif isinstance(turn_record, Mapping):
            workflow_selection = turn_record.get("workflow_selection")
            if isinstance(workflow_selection, Mapping) and isinstance(
                workflow_selection.get("workflow_discovery"), Mapping
            ):
                workflow_discovery = workflow_selection.get("workflow_discovery")
        if isinstance(workflow_discovery, Mapping):
            payload["workflow_discovery"] = deepcopy(workflow_discovery)

    if not isinstance(payload.get("workflow_routing_diagnostics"), Mapping):
        routing = None
        if isinstance(llm_debug_mapping, Mapping) and isinstance(
            llm_debug_mapping.get("workflow_routing_diagnostics"), Mapping
        ):
            routing = llm_debug_mapping.get("workflow_routing_diagnostics")
        elif isinstance(turn_record, Mapping) and isinstance(
            turn_record.get("workflow_routing_diagnostics"), Mapping
        ):
            routing = turn_record.get("workflow_routing_diagnostics")
        if isinstance(routing, Mapping):
            payload["workflow_routing_diagnostics"] = deepcopy(routing)

    if not isinstance(payload.get("workflow_stage_model"), Mapping):
        workflow_stage_model_snapshot = execution.get("workflow_stage_model") if execution else None
        if isinstance(workflow_stage_model_snapshot, Mapping):
            payload["workflow_stage_model"] = deepcopy(workflow_stage_model_snapshot)
        else:
            payload["workflow_stage_model"] = build_conversation_turn_stage_model_snapshot()

    if not isinstance(payload.get("workflow_stage_path"), Mapping):
        workflow_stage_path_snapshot = execution.get("workflow_stage_path") if execution else None
        if isinstance(workflow_stage_path_snapshot, Mapping):
            payload["workflow_stage_path"] = deepcopy(workflow_stage_path_snapshot)
        else:
            selected_workflow_id = None
            if isinstance(turn_record, Mapping):
                workflow_selection = turn_record.get("workflow_selection")
                if isinstance(workflow_selection, Mapping):
                    selected_workflow_id = _safe_str(
                        workflow_selection.get("selected_workflow_id")
                    )
            payload["workflow_stage_path"] = build_conversation_turn_stage_path(
                runtime_stages=(),
                workflow_id=selected_workflow_id,
            )

    payload.setdefault("latest_progress", None)
    payload.setdefault("progress_events", [])
    payload.setdefault("activity_history", [])
    payload.setdefault("phase_history", [])
    payload.setdefault("stage_diagnostics", [])
    if not isinstance(payload.get("timing_breakdown"), Mapping):
        payload["timing_breakdown"] = _build_minimal_timing_breakdown(
            llm_debug=llm_debug_mapping
        )

    tool_history = _normalise_tool_history(payload.get("tool_history"))
    tool_invocations = execution.get("tool_invocations") if execution else None
    if not tool_history and isinstance(tool_invocations, list):
        tool_history = _normalise_tool_history(tool_invocations)
        payload["tool_history"] = tool_history
    else:
        payload["tool_history"] = tool_history

    tool_counts = _derive_tool_counts(tool_history)
    for key, value in tool_counts.items():
        if not isinstance(payload.get(key), int):
            payload[key] = value

    return payload


def get_turn_execution_diagnostics_payload(
    *,
    request_id: str,
    namespace: str | None = None,
) -> dict[str, Any] | None:
    request_id_value = _safe_str(request_id)
    if not request_id_value:
        return None

    history_context = _resolve_history_context(
        request_id=request_id_value,
        namespace=_safe_str(namespace),
    )
    turn_record = _load_turn_execution_record(
        request_id=request_id_value,
        namespace=_safe_str(namespace) or (
            _safe_str(history_context.get("namespace"))
            if isinstance(history_context, Mapping)
            else None
        ),
    )

    llm_debug = (
        history_context.get("target_llm_debug")
        if isinstance(history_context, Mapping)
        else None
    )
    embedded = (
        llm_debug.get("turn_execution_diagnostics")
        if isinstance(llm_debug, Mapping)
        else None
    )

    if isinstance(embedded, Mapping):
        payload = _normalise_embedded_diagnostics_payload(
            request_id=request_id_value,
            embedded=embedded,
            history_context=history_context,
            turn_record=turn_record,
        )
        payload["diagnostics_source"] = "chat_history.llm_debug_data.turn_execution_diagnostics"
    elif isinstance(history_context, Mapping) or isinstance(turn_record, Mapping):
        payload = _build_fallback_turn_execution_diagnostics(
            request_id=request_id_value,
            history_context=history_context,
            turn_record=turn_record,
        )
        payload["diagnostics_source"] = (
            "chat_history.llm_debug_data"
            if isinstance(history_context, Mapping)
            else "mongo.turn_execution_records"
        )
    else:
        return None

    payload["chat_session_id"] = (
        _safe_str(history_context.get("session_id"))
        if isinstance(history_context, Mapping)
        else None
    ) or (
        _safe_str(turn_record.get("session_id"))
        if isinstance(turn_record, Mapping)
        else None
    )
    payload["namespace"] = (
        _safe_str(payload.get("namespace"))
        or _safe_str(namespace)
        or (
            _safe_str(history_context.get("namespace"))
            if isinstance(history_context, Mapping)
            else None
        )
        or (
            _safe_str(turn_record.get("namespace"))
            if isinstance(turn_record, Mapping)
            else None
        )
    )
    payload["namespace_source"] = (
        "argument" if _safe_str(namespace) else "chat_history_or_projection"
    )
    payload["derived_user_concept_id"] = (
        _safe_str(history_context.get("user_id"))
        if isinstance(history_context, Mapping)
        else None
    ) or (
        _safe_str(turn_record.get("user_id"))
        if isinstance(turn_record, Mapping)
        else None
    )
    payload["derived_organisation_concept_id"] = (
        _safe_str(history_context.get("org_id"))
        if isinstance(history_context, Mapping)
        else None
    ) or (
        _safe_str(turn_record.get("org_id"))
        if isinstance(turn_record, Mapping)
        else None
    )
    payload["success"] = True
    return payload
