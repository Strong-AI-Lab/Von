"""Read-only access to persisted turn-execution diagnostics payloads."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .chat_history_service import get_chat_history_collection_service
from .conversation_scope_binding_service import (
    build_conversation_scope_binding,
    build_history_location_binding,
)
from .debug_payload_store import hydrate_debug_payload_blob_refs
from .turn_response_surface_service import build_turn_response_surface_reconciliation
from .turn_execution_record_service import (
    build_workflow_routing_diagnostics,
    get_turn_execution_records_collection,
)
from .turn_timing_telemetry_service import build_turn_timing_trace
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


def _build_tool_call_descriptor(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    purpose: str | None = None,
) -> dict[str, Any]:
    payload = {
        "tool_name": tool_name,
        "arguments": {
            key: value for key, value in arguments.items() if value is not None
        },
    }
    if isinstance(purpose, str) and purpose.strip():
        payload["purpose"] = purpose.strip()
    return payload


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


def _extract_prompt_text_from_debug(
    llm_debug_data: Mapping[str, Any] | None,
) -> str | None:
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
        hydrated_debug = hydrate_debug_payload_blob_refs(llm_debug, fail_soft=True)
        target_llm_debug = (
            dict(hydrated_debug.payload)
            if isinstance(hydrated_debug.payload, Mapping)
            else dict(llm_debug)
        )
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


def _selected_workflow_id_from_payload(
    payload: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(payload, Mapping):
        return None

    candidates: list[Any] = []
    workflow_selection = payload.get("workflow_selection")
    if isinstance(workflow_selection, Mapping):
        candidates.append(workflow_selection.get("selected_workflow_id"))
    workflow_routing = payload.get("workflow_routing")
    if isinstance(workflow_routing, Mapping):
        candidates.append(workflow_routing.get("workflow_id"))
    routing_diagnostics = payload.get("workflow_routing_diagnostics")
    if isinstance(routing_diagnostics, Mapping):
        candidates.append(routing_diagnostics.get("selected_workflow_id"))
    selected_trace = payload.get("selected_workflow_trace")
    if isinstance(selected_trace, Mapping):
        candidates.extend(
            (
                selected_trace.get("selected_workflow_id"),
                selected_trace.get("workflow_id"),
            )
        )
    turn_diagnostics = payload.get("turn_execution_diagnostics")
    if isinstance(turn_diagnostics, Mapping):
        candidates.append(_selected_workflow_id_from_payload(turn_diagnostics))
    turn_record = payload.get("turn_execution_record")
    if isinstance(turn_record, Mapping):
        candidates.append(_selected_workflow_id_from_payload(turn_record))

    for candidate in candidates:
        cleaned = _safe_str(candidate)
        if cleaned:
            return cleaned
    return None


def build_workflow_execution_trace_mcp_access_refs(
    payload: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Extract durable workflow trace access refs from debug/record payloads."""

    if not isinstance(payload, Mapping):
        return []
    selected_workflow_id = _selected_workflow_id_from_payload(payload)
    aux_llm_calls = payload.get("aux_llm_calls")

    traces: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str | None, str | None]] = set()

    def _append_ref(
        *,
        raw_entry: Mapping[str, Any],
        source: str,
        selected_hint: bool = False,
    ) -> None:
        execution_id = _safe_str(raw_entry.get("execution_id")) or _safe_str(
            raw_entry.get("execution_trace_id")
        )
        instance_id = (
            _safe_str(raw_entry.get("instance_id"))
            or _safe_str(raw_entry.get("workflow_instance_id"))
            or _safe_str(raw_entry.get("workflow_instance_concept_id"))
        )
        if not execution_id and not instance_id:
            if not selected_hint:
                return
            trace_unavailable = raw_entry.get("trace_unavailable") is True
            pre_trace_failure = raw_entry.get("selected_workflow_pre_trace_failure")
            if not trace_unavailable and not isinstance(pre_trace_failure, Mapping):
                return
            trace_workflow_id = (
                _safe_str(raw_entry.get("workflow_id"))
                or _safe_str(raw_entry.get("selected_workflow_id"))
                or _safe_str(raw_entry.get("dispatch_workflow_id"))
                or selected_workflow_id
            )
            traces.append(
                {
                    "execution_id": None,
                    "instance_id": None,
                    "workflow_id": trace_workflow_id,
                    "trace_role": "selected_workflow",
                    "trace_unavailable": True,
                    "trace_unavailable_reason": _safe_str(
                        raw_entry.get("trace_unavailable_reason")
                    )
                    or "selected_workflow_trace_ref_missing",
                    "selected_workflow_pre_trace_failure": (
                        dict(pre_trace_failure)
                        if isinstance(pre_trace_failure, Mapping)
                        else None
                    ),
                }
            )
            return
        trace_key = (execution_id, instance_id)
        if trace_key in seen_pairs:
            return
        seen_pairs.add(trace_key)
        trace_workflow_id = (
            _safe_str(raw_entry.get("workflow_id"))
            or _safe_str(raw_entry.get("selected_workflow_id"))
            or _safe_str(raw_entry.get("dispatch_workflow_id"))
        )
        trace_role = "workflow_execution"
        if selected_hint:
            trace_role = "selected_workflow"
        elif selected_workflow_id and trace_workflow_id:
            trace_role = (
                "selected_workflow"
                if trace_workflow_id.strip().lower()
                == selected_workflow_id.strip().lower()
                else "auxiliary_workflow"
            )
        traces.append(
            {
                "execution_id": execution_id,
                "instance_id": instance_id,
                "workflow_id": trace_workflow_id,
                "trace_role": trace_role,
                "mcp_access": _build_tool_call_descriptor(
                    "workflow_get_execution_trace",
                    {
                        "execution_id": execution_id,
                        "instance_id": instance_id,
                    },
                    purpose="Fetch the durable workflow execution trace referenced by this turn.",
                ),
            }
        )

    selected_trace = payload.get("selected_workflow_trace")
    if isinstance(selected_trace, Mapping):
        _append_ref(
            raw_entry=selected_trace,
            source="selected_workflow_trace",
            selected_hint=True,
        )

    execution = payload.get("execution")
    if isinstance(execution, Mapping):
        execution_selected_trace = execution.get("selected_workflow_trace")
        if isinstance(execution_selected_trace, Mapping):
            _append_ref(
                raw_entry=execution_selected_trace,
                source="execution.selected_workflow_trace",
                selected_hint=True,
            )

    turn_record = payload.get("turn_execution_record")
    if isinstance(turn_record, Mapping):
        turn_record_execution = turn_record.get("execution")
        if isinstance(turn_record_execution, Mapping):
            turn_record_selected_trace = turn_record_execution.get(
                "selected_workflow_trace"
            )
            if isinstance(turn_record_selected_trace, Mapping):
                _append_ref(
                    raw_entry=turn_record_selected_trace,
                    source="turn_execution_record.execution.selected_workflow_trace",
                    selected_hint=True,
                )

    if not isinstance(aux_llm_calls, Sequence) or isinstance(
        aux_llm_calls, (str, bytes, bytearray)
    ):
        aux_llm_calls = []

    for raw_entry in aux_llm_calls:
        if not isinstance(raw_entry, Mapping):
            continue
        entry_type = _safe_str(raw_entry.get("type"))
        if entry_type == "workflow_execution_trace":
            _append_ref(raw_entry=raw_entry, source="aux_llm_calls")
            continue
        if entry_type == "workflow_use_episode":
            _append_ref(raw_entry=raw_entry, source="workflow_use_episode")
            continue

    traces.sort(
        key=lambda entry: 0 if entry.get("trace_role") == "selected_workflow" else 1
    )
    return traces


def _extract_workflow_execution_trace_refs(
    payload: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    return build_workflow_execution_trace_mcp_access_refs(payload)


def _build_turn_diagnostics_mcp_access(
    *,
    request_id: str,
    namespace: str | None,
    session_id: str | None,
    history_index: int | None,
    history_owner_user_id: str | None,
    organisation_concept_id: str | None,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    access: dict[str, Any] = {
        "turn_execution_get_diagnostics": _build_tool_call_descriptor(
            "turn_execution_get_diagnostics",
            {
                "request_id": request_id,
                "namespace": namespace,
            },
            purpose="Fetch the persisted full turn-execution diagnostics payload.",
        ),
        "turn_execution_get": _build_tool_call_descriptor(
            "turn_execution_get",
            {
                "request_id": request_id,
                "namespace": namespace,
            },
            purpose="Fetch the projected turn-execution record for this request.",
        ),
    }
    if session_id:
        conversation_ref = build_conversation_scope_binding(
            chat_session_id=session_id,
            history_owner_user_id=history_owner_user_id,
            read_namespace=namespace,
            organisation_concept_id=organisation_concept_id,
        )
        access["conversation_telemetry_get_locator"] = _build_tool_call_descriptor(
            "conversation_telemetry_get_locator",
            {
                "conversation_ref": conversation_ref,
                "namespace": namespace,
                "organisation_concept_id": organisation_concept_id,
            },
            purpose="Fetch the compact conversation locator for the surrounding chat session.",
        )
        access["chat_history_get_segments"] = _build_tool_call_descriptor(
            "chat_history_get_segments",
            {
                "conversation_ref": conversation_ref,
                "namespace": namespace,
                "include_debug": True,
                "organisation_concept_id": organisation_concept_id,
            },
            purpose="Fetch the stored transcript segments and embedded debug payloads for this session.",
        )
    if session_id and history_index is not None:
        access["chat_history_get_debug_entry"] = _build_tool_call_descriptor(
            "chat_history_get_debug_entry",
            {
                "history_location_ref": build_history_location_binding(
                    chat_session_id=session_id,
                    history_index=history_index,
                    history_owner_user_id=history_owner_user_id,
                    read_namespace=namespace,
                    organisation_concept_id=organisation_concept_id,
                ),
                "namespace": namespace,
                "organisation_concept_id": organisation_concept_id,
            },
            purpose="Fetch the exact stored llm_debug_data entry for this assistant turn.",
        )

    workflow_traces = _extract_workflow_execution_trace_refs(payload)
    if workflow_traces:
        access["workflow_execution_traces"] = workflow_traces
        primary_trace = next(
            (
                trace
                for trace in workflow_traces
                if trace.get("trace_role") == "selected_workflow"
            ),
            workflow_traces[0],
        )
        primary_access = primary_trace.get("mcp_access")
        if isinstance(primary_access, Mapping):
            access["workflow_get_execution_trace"] = dict(primary_access)

    return access


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
        elif status in {
            "failed",
            "failure",
            "error",
            "blocked",
            "cancelled",
            "canceled",
        }:
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
            row = {
                "stage": _safe_str(entry.get("workflow_stage_id"))
                or _safe_str(entry.get("stage"))
                or _safe_str(entry.get("phase"))
                or "unscoped",
                "model": _safe_str(entry.get("model"))
                or _safe_str(entry.get("model_name"))
                or "unknown",
                "provider": _safe_str(entry.get("provider")),
                "duration_ms": duration_ms or 0,
            }
            count_raw = entry.get("historical_observation_count")
            if isinstance(count_raw, (int, float)) and not isinstance(count_raw, bool):
                row["historical_observation_count"] = int(max(0.0, float(count_raw)))
                for key in (
                    "historical_mean_duration_ms",
                    "historical_stddev_duration_ms",
                    "historical_min_duration_ms",
                    "historical_max_duration_ms",
                    "duration_deviation_from_mean_ms",
                    "duration_deviation_ratio",
                    "duration_deviation_stddevs",
                ):
                    value = entry.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        row[key] = float(value)
                for key in (
                    "historical_last_observed_at_utc",
                    "duration_deviation_classification",
                ):
                    value = _safe_str(entry.get(key))
                    if value:
                        row[key] = value
            rows.append(row)
    return rows


def _build_minimal_timing_breakdown(
    *,
    llm_debug: Mapping[str, Any] | None,
) -> dict[str, Any]:
    llm_rows = _normalise_llm_call_rows(
        llm_debug.get("llm_calls") if isinstance(llm_debug, Mapping) else None
    )
    if not llm_rows and isinstance(llm_debug, Mapping):
        interaction = llm_debug.get("llm_interaction")
        if isinstance(interaction, Mapping):
            duration_raw = interaction.get("duration_ms") or interaction.get(
                "server_elapsed_ms"
            )
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

    def _merge_duration_baseline(
        bucket: dict[str, Any], row: Mapping[str, Any]
    ) -> None:
        count = row.get("historical_observation_count")
        if not isinstance(count, int) or count <= 0:
            return
        existing_count = bucket.get("historical_observation_count")
        if isinstance(existing_count, int) and existing_count >= count:
            return
        for key in (
            "historical_observation_count",
            "historical_mean_duration_ms",
            "historical_stddev_duration_ms",
            "historical_min_duration_ms",
            "historical_max_duration_ms",
            "historical_last_observed_at_utc",
            "duration_deviation_from_mean_ms",
            "duration_deviation_ratio",
            "duration_deviation_stddevs",
            "duration_deviation_classification",
        ):
            if key in row:
                bucket[key] = row.get(key)

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
        _merge_duration_baseline(bucket, row)
        bucket["call_count"] = int(bucket["call_count"]) + 1
        bucket["duration_ms"] = int(bucket["duration_ms"]) + int(row["duration_ms"])
        if bucket.get("provider") is None and row.get("provider") is not None:
            bucket["provider"] = row.get("provider")
        total_ms += int(row["duration_ms"])

    elapsed_ms: int | None = None
    if isinstance(llm_debug, Mapping):
        interaction = llm_debug.get("llm_interaction")
        if isinstance(interaction, Mapping):
            elapsed_raw = interaction.get("server_elapsed_ms") or interaction.get(
                "duration_ms"
            )
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


def _llm_calls_from_debug(llm_debug: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(llm_debug, Mapping):
        return []
    direct_calls = llm_debug.get("llm_calls")
    if isinstance(direct_calls, list):
        return [dict(item) for item in direct_calls if isinstance(item, Mapping)]
    interaction = llm_debug.get("llm_interaction")
    if isinstance(interaction, Mapping) and isinstance(interaction.get("calls"), list):
        return [
            dict(item)
            for item in interaction.get("calls", [])
            if isinstance(item, Mapping)
        ]
    return []


def _attach_minimal_turn_timing_trace(
    payload: dict[str, Any],
    *,
    llm_debug: Mapping[str, Any] | None,
    tool_history: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    if isinstance(payload.get("turn_timing_trace"), Mapping):
        return
    trace = build_turn_timing_trace(
        request_id=_safe_str(payload.get("request_id")),
        phase_history=(
            payload.get("phase_history")
            if isinstance(payload.get("phase_history"), list)
            else []
        ),
        diagnostic_events=(
            payload.get("latest_progress", {}).get("diagnostic_events")
            if isinstance(payload.get("latest_progress"), Mapping)
            and isinstance(
                payload.get("latest_progress", {}).get("diagnostic_events"), list
            )
            else []
        ),
        llm_calls=_llm_calls_from_debug(llm_debug),
        tool_history=tool_history or [],
        elapsed_ms_value=(
            int(payload.get("elapsed_ms"))
            if isinstance(payload.get("elapsed_ms"), (int, float))
            and not isinstance(payload.get("elapsed_ms"), bool)
            else None
        ),
    )
    payload["turn_timing_trace"] = trace
    timing_breakdown = (
        dict(payload.get("timing_breakdown"))
        if isinstance(payload.get("timing_breakdown"), Mapping)
        else _build_minimal_timing_breakdown(llm_debug=llm_debug)
    )
    timing_breakdown["operation_totals"] = list(trace.get("operation_totals") or [])
    timing_breakdown["slowest_spans"] = list(trace.get("slowest_spans") or [])
    timing_breakdown["model_prompt_summary"] = list(
        trace.get("model_prompt_summary") or []
    )
    payload["timing_breakdown"] = timing_breakdown


def _string_key_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _capture_has_text(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    text = _safe_str(value.get("text"))
    if text:
        return True
    char_count = value.get("char_count")
    return isinstance(char_count, (int, float)) and char_count > 0


def _diagnostic_value_present(value: Any) -> bool:
    if value is None:
        return False
    if value == "" or value == [] or value == {}:
        return False
    return True


def _selector_core_evidence_sparse(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return True
    selector = value.get("selector")
    if not isinstance(selector, Mapping):
        return True
    return not (
        _capture_has_text(selector.get("prompt"))
        and _capture_has_text(selector.get("candidate_list"))
        and _capture_has_text(selector.get("response"))
    )


def _collect_aux_llm_entries_from_value(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [
        _string_key_mapping(item) or {} for item in value if isinstance(item, Mapping)
    ]


def _collect_routing_aux_entries(
    *,
    payload: Mapping[str, Any] | None,
    llm_debug: Mapping[str, Any] | None,
    turn_record: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def _append_from(value: Any) -> None:
        entries.extend(_collect_aux_llm_entries_from_value(value))

    for source in (payload, llm_debug, turn_record):
        if not isinstance(source, Mapping):
            continue
        _append_from(source.get("aux_llm_calls"))
        _append_from(source.get("workflow_routing_aux"))
        latest_progress = source.get("latest_progress")
        if isinstance(latest_progress, Mapping):
            _append_from(latest_progress.get("workflow_routing_aux"))
        selected_trace = source.get("selected_workflow_trace")
        if isinstance(selected_trace, Mapping):
            _append_from(selected_trace.get("aux_llm_calls"))
            _append_from(selected_trace.get("workflow_routing_aux"))

    return [entry for entry in entries if entry]


def _first_mapping_by_key(
    key: str,
    *sources: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        value = source.get(key)
        if isinstance(value, Mapping):
            return deepcopy(dict(value))
    return None


def _workflow_routing_from_sources(
    *,
    payload: Mapping[str, Any] | None,
    llm_debug: Mapping[str, Any] | None,
    turn_record: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    direct = _first_mapping_by_key("workflow_routing", payload, llm_debug, turn_record)
    if direct is not None:
        return direct

    diagnostics = _first_mapping_by_key(
        "workflow_routing_diagnostics", payload, llm_debug, turn_record
    )
    if isinstance(diagnostics, Mapping):
        selector = diagnostics.get("selector")
        return {
            "workflow_id": diagnostics.get("selected_workflow_id"),
            "verdict": diagnostics.get("selector_verdict"),
            "source": diagnostics.get("selector_source"),
            "prompt_id": (
                selector.get("prompt_id") if isinstance(selector, Mapping) else None
            ),
            "confidence_score": (
                selector.get("confidence_score")
                if isinstance(selector, Mapping)
                else None
            ),
            "reasoning": (
                selector.get("reasoning") if isinstance(selector, Mapping) else None
            ),
            "selection_rationale": diagnostics.get("selection_rationale"),
            "discovered_workflow_ids": (
                selector.get("discovered_workflow_ids")
                if isinstance(selector, Mapping)
                else None
            ),
        }

    workflow_selection = _first_mapping_by_key(
        "workflow_selection", payload, turn_record
    )
    if isinstance(workflow_selection, Mapping):
        selector = workflow_selection.get("selector")
        return {
            "workflow_id": workflow_selection.get("selected_workflow_id"),
            "verdict": workflow_selection.get("selector_verdict")
            or workflow_selection.get("verdict"),
            "source": workflow_selection.get("selector_source")
            or workflow_selection.get("source"),
            "prompt_id": workflow_selection.get("prompt_id")
            or (selector.get("prompt_id") if isinstance(selector, Mapping) else None),
            "confidence_score": workflow_selection.get("confidence_score"),
            "reasoning": workflow_selection.get("reasoning"),
            "selection_rationale": workflow_selection.get("selection_rationale"),
            "discovered_workflow_ids": workflow_selection.get(
                "discovered_workflow_ids"
            ),
        }

    return None


def _workflow_discovery_from_sources(
    *,
    payload: Mapping[str, Any] | None,
    llm_debug: Mapping[str, Any] | None,
    turn_record: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    direct = _first_mapping_by_key("workflow_discovery", payload, llm_debug)
    if direct is not None:
        return direct
    workflow_selection = _first_mapping_by_key(
        "workflow_selection", payload, turn_record
    )
    if isinstance(workflow_selection, Mapping) and isinstance(
        workflow_selection.get("workflow_discovery"), Mapping
    ):
        return deepcopy(dict(workflow_selection["workflow_discovery"]))
    return None


def _merge_section_with_repair(
    existing: Mapping[str, Any] | None,
    repair: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = deepcopy(dict(existing or {}))
    if not isinstance(repair, Mapping):
        return merged
    for key, value in repair.items():
        if key == "telemetry_completeness":
            merged[key] = deepcopy(value)
            continue
        if not _diagnostic_value_present(merged.get(key)) and _diagnostic_value_present(
            value
        ):
            merged[key] = deepcopy(value)
    return merged


def _merge_workflow_routing_diagnostics_with_repair(
    existing: Mapping[str, Any] | None,
    repair: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(existing, Mapping):
        return deepcopy(dict(repair))
    merged = deepcopy(dict(existing))
    for key, value in repair.items():
        if key in {"selector", "discovery", "dispatch"}:
            merged[key] = _merge_section_with_repair(
                merged.get(key) if isinstance(merged.get(key), Mapping) else None,
                value if isinstance(value, Mapping) else None,
            )
            continue
        if not _diagnostic_value_present(merged.get(key)) and _diagnostic_value_present(
            value
        ):
            merged[key] = deepcopy(value)
    return merged


def _repair_workflow_routing_diagnostics(
    payload: dict[str, Any],
    *,
    llm_debug: Mapping[str, Any] | None,
    turn_record: Mapping[str, Any] | None,
) -> None:
    current = payload.get("workflow_routing_diagnostics")
    aux_llm_calls = _collect_routing_aux_entries(
        payload=payload,
        llm_debug=llm_debug,
        turn_record=turn_record,
    )
    workflow_routing = _workflow_routing_from_sources(
        payload=payload,
        llm_debug=llm_debug,
        turn_record=turn_record,
    )
    workflow_discovery = _workflow_discovery_from_sources(
        payload=payload,
        llm_debug=llm_debug,
        turn_record=turn_record,
    )
    if not (
        isinstance(current, Mapping)
        or isinstance(workflow_routing, Mapping)
        or isinstance(workflow_discovery, Mapping)
        or aux_llm_calls
    ):
        return
    if isinstance(current, Mapping) and not _selector_core_evidence_sparse(current):
        selector = current.get("selector")
        if isinstance(selector, Mapping) and isinstance(
            selector.get("telemetry_completeness"), Mapping
        ):
            return

    repair = build_workflow_routing_diagnostics(
        workflow_discovery=workflow_discovery,
        workflow_routing=workflow_routing,
        turn_execution_diagnostics=payload,
        aux_llm_calls=aux_llm_calls,
    )
    payload["workflow_routing_diagnostics"] = (
        _merge_workflow_routing_diagnostics_with_repair(current, repair)
        if isinstance(current, Mapping)
        else repair
    )


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
    workflow_stage_model_snapshot = (
        execution.get("workflow_stage_model") if execution else None
    )
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

    workflow_stage_path_snapshot = (
        execution.get("workflow_stage_path") if execution else None
    )
    workflow_stage_path = (
        deepcopy(workflow_stage_path_snapshot)
        if isinstance(workflow_stage_path_snapshot, Mapping)
        else build_conversation_turn_stage_path(
            runtime_stages=(),
            workflow_id=selected_workflow_id,
            selected_workflow_id=selected_workflow_id,
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

    prompt_text = (
        history_context_mapping.get("prompt_text") if history_context_mapping else None
    )
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
            code_version_details = deepcopy(
                llm_debug_mapping.get("code_version_details")
            )

    payload = {
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
        "workflow_selection": (
            deepcopy(workflow_selection)
            if isinstance(workflow_selection, Mapping)
            else None
        ),
        "workflow_routing_diagnostics": workflow_routing_diagnostics,
        "workflow_stage_model": workflow_stage_model,
        "workflow_stage_path": workflow_stage_path,
        "stage_diagnostics": [],
        "timing_breakdown": _build_minimal_timing_breakdown(
            llm_debug=llm_debug_mapping
        ),
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
    _repair_workflow_routing_diagnostics(
        payload,
        llm_debug=llm_debug_mapping,
        turn_record=turn_record,
    )
    _attach_minimal_turn_timing_trace(
        payload,
        llm_debug=llm_debug_mapping,
        tool_history=tool_history,
    )
    return payload


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
        prompt_text = (
            history_context_mapping.get("prompt_text")
            if history_context_mapping
            else None
        )
        if isinstance(prompt_text, str):
            payload["prompt_preview"] = prompt_text[:_PROMPT_PREVIEW_LIMIT]

    if not _safe_str(payload.get("code_version")) and isinstance(
        llm_debug_mapping, Mapping
    ):
        code_version = _safe_str(llm_debug_mapping.get("code_version"))
        if code_version:
            payload["code_version"] = code_version
    if (
        not isinstance(payload.get("code_version_details"), Mapping)
        and isinstance(llm_debug_mapping, Mapping)
        and isinstance(llm_debug_mapping.get("code_version_details"), Mapping)
    ):
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

    if not isinstance(payload.get("workflow_selection"), Mapping) and isinstance(
        turn_record, Mapping
    ):
        workflow_selection = turn_record.get("workflow_selection")
        if isinstance(workflow_selection, Mapping):
            payload["workflow_selection"] = deepcopy(workflow_selection)

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
        workflow_stage_model_snapshot = (
            execution.get("workflow_stage_model") if execution else None
        )
        if isinstance(workflow_stage_model_snapshot, Mapping):
            payload["workflow_stage_model"] = deepcopy(workflow_stage_model_snapshot)
        else:
            payload["workflow_stage_model"] = (
                build_conversation_turn_stage_model_snapshot()
            )

    if not isinstance(payload.get("workflow_stage_path"), Mapping):
        workflow_stage_path_snapshot = (
            execution.get("workflow_stage_path") if execution else None
        )
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
                selected_workflow_id=selected_workflow_id,
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

    _attach_minimal_turn_timing_trace(
        payload,
        llm_debug=llm_debug_mapping,
        tool_history=tool_history,
    )

    _repair_workflow_routing_diagnostics(
        payload,
        llm_debug=llm_debug_mapping,
        turn_record=turn_record,
    )

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
        namespace=_safe_str(namespace)
        or (
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
        payload["diagnostics_source"] = (
            "chat_history.llm_debug_data.turn_execution_diagnostics"
        )
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
    history_index = (
        history_context.get("target_index")
        if isinstance(history_context, Mapping)
        else None
    )
    payload["history_location"] = (
        {
            "session_id": payload["chat_session_id"],
            "history_index": history_index,
        }
        if isinstance(history_index, int) and payload.get("chat_session_id")
        else None
    )
    raw_target_message = (
        history_context.get("target_message")
        if isinstance(history_context, Mapping)
        else None
    )
    response_surface_target_message: dict[str, Any] | None = (
        dict(raw_target_message) if isinstance(raw_target_message, Mapping) else None
    )
    if isinstance(response_surface_target_message, dict) and not isinstance(
        response_surface_target_message.get("history_location"), Mapping
    ):
        response_surface_target_message["history_location"] = payload.get(
            "history_location"
        )
    payload["response_surfaces"] = build_turn_response_surface_reconciliation(
        target_message=response_surface_target_message,
        turn_record=turn_record,
        diagnostics=payload,
    )
    payload["mcp_access"] = _build_turn_diagnostics_mcp_access(
        request_id=request_id_value,
        namespace=_safe_str(payload.get("namespace")),
        session_id=_safe_str(payload.get("chat_session_id")),
        history_index=history_index if isinstance(history_index, int) else None,
        history_owner_user_id=_safe_str(payload.get("derived_user_concept_id")),
        organisation_concept_id=_safe_str(
            payload.get("derived_organisation_concept_id")
        ),
        payload=payload,
    )
    payload["success"] = True
    return payload


def _safe_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(max(0.0, float(value)))
    return None


def _extract_text_capture(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return {
            "text": text,
            "char_count": len(text),
            "is_truncated": False,
        }

    if not isinstance(value, Mapping):
        return None

    text_value = _safe_str(value.get("text"))
    preview_value = _safe_str(value.get("preview"))
    content_value = _safe_str(value.get("content"))
    chosen_text = text_value or preview_value or content_value
    if not chosen_text:
        return None

    char_count = _safe_non_negative_int(value.get("char_count"))
    if char_count is None:
        char_count = _safe_non_negative_int(value.get("content_char_count"))
    if char_count is None:
        char_count = len(chosen_text)

    is_truncated = bool(
        value.get("is_truncated") is True
        or value.get("truncated") is True
        or value.get("preview_truncated") is True
        or (char_count > len(chosen_text))
    )

    return {
        "text": chosen_text,
        "char_count": char_count,
        "is_truncated": is_truncated,
    }


def _extract_prompt_capture_from_entry(
    entry: Mapping[str, Any],
) -> dict[str, Any] | None:
    direct_capture = _extract_text_capture(entry.get("prompt_capture"))
    if direct_capture:
        return direct_capture

    direct = _extract_text_capture(entry.get("prompt"))
    if direct:
        return direct

    preview = _extract_text_capture(entry.get("prompt_preview"))
    if preview:
        return preview

    llm_request = entry.get("llm_request")
    if isinstance(llm_request, Mapping):
        request_prompt = _extract_text_capture(llm_request.get("prompt"))
        if request_prompt:
            return request_prompt

    request_payload = entry.get("request")
    if isinstance(request_payload, Mapping):
        request_prompt = _extract_text_capture(request_payload.get("prompt"))
        if request_prompt:
            return request_prompt

    return None


def _extract_response_capture_from_entry(
    entry: Mapping[str, Any],
) -> dict[str, Any] | None:
    direct_capture = _extract_text_capture(entry.get("response_capture"))
    if direct_capture:
        return direct_capture

    direct = _extract_text_capture(entry.get("response"))
    if direct:
        return direct

    preview = _extract_text_capture(entry.get("response_preview"))
    if preview:
        return preview

    selected = entry.get("selected")
    if isinstance(selected, Mapping):
        selected_response = _extract_text_capture(selected.get("response"))
        if selected_response:
            return selected_response

    return None


def _extract_llm_exchange_timestamps(
    entry: Mapping[str, Any],
) -> dict[str, str]:
    """Pull wall-clock timestamps recorded on an LLM call entry.

    Supports the Thinking-card timestamped LLM interaction list (JVNAUTOSCI-2385).
    Reads canonical keys stamped at recording time, falling back to common
    alternatives carried by chat-history or aux entries.
    """

    timestamps: dict[str, str] = {}
    completed = (
        _safe_str(entry.get("completed_at_utc"))
        or _safe_str(entry.get("at_utc"))
        or _safe_str(entry.get("timestamp"))
        or _safe_str(entry.get("ended_at_utc"))
        or _safe_str(entry.get("llm_first_output_at_utc"))
    )
    started = (
        _safe_str(entry.get("started_at_utc"))
        or _safe_str(entry.get("requested_at_utc"))
        or _safe_str(entry.get("sent_at_utc"))
        or _safe_str(entry.get("llm_request_sent_at_utc"))
        or _safe_str(entry.get("llm_request_prepared_at_utc"))
    )
    if completed:
        timestamps["completed_at_utc"] = completed
    if started:
        timestamps["started_at_utc"] = started
    canonical = completed or started
    if canonical:
        timestamps["at_utc"] = canonical
    return timestamps


def _normalise_llm_exchange_entry(
    entry: Mapping[str, Any],
    *,
    source: str,
    source_index: int,
) -> dict[str, Any]:
    prompt_capture = _extract_prompt_capture_from_entry(entry)
    response_capture = _extract_response_capture_from_entry(entry)
    exchange_timestamps = _extract_llm_exchange_timestamps(entry)

    call_type = (
        _safe_str(entry.get("type"))
        or _safe_str(entry.get("call_type"))
        or _safe_str(entry.get("entry_type"))
    )
    stage = _safe_str(entry.get("stage")) or _safe_str(entry.get("phase"))
    workflow_stage_id = _safe_str(entry.get("workflow_stage_id"))
    model = (
        _safe_str(entry.get("model"))
        or _safe_str(entry.get("model_name"))
        or _safe_str(entry.get("selected_model"))
    )
    provider = _safe_str(entry.get("provider")) or _safe_str(
        entry.get("selected_provider")
    )
    duration_ms = _safe_non_negative_int(entry.get("duration_ms"))
    status = _safe_str(entry.get("status"))
    success_raw = entry.get("success")
    success = success_raw if isinstance(success_raw, bool) else None
    error = _safe_str(entry.get("error"))
    error_class = _safe_str(entry.get("error_class"))
    failure_kind = _safe_str(entry.get("failure_kind"))

    exchange_blob_ref = entry.get("exchange_blob_ref")
    exchange_blob_ref_payload = (
        {
            str(key): item
            for key, item in exchange_blob_ref.items()
            if isinstance(key, str)
        }
        if isinstance(exchange_blob_ref, Mapping)
        else None
    )

    prompt_recorded = prompt_capture is not None
    response_recorded = response_capture is not None

    unavailable_reason = None
    if not prompt_recorded or not response_recorded:
        if (
            status in {"failed", "cancelled", "error", "timeout"}
            and prompt_recorded
            and not response_recorded
        ):
            unavailable_reason = "failed_before_response"
        elif exchange_blob_ref_payload is not None:
            unavailable_reason = "exchange_in_blob_ref"
        elif call_type and call_type.startswith("workflow_model_policy"):
            unavailable_reason = "model_policy_call_without_exchange"
        elif call_type:
            unavailable_reason = "exchange_not_recorded"

    payload = {
        "schema_version": "turn_llm_exchange_entry.v1",
        "source": source,
        "source_index": source_index,
        "call_type": call_type,
        "stage": stage,
        "workflow_stage_id": workflow_stage_id,
        "model": model,
        "provider": provider,
        "duration_ms": duration_ms,
        "status": status,
        "success": success,
        "error": error,
        "error_class": error_class,
        "failure_kind": failure_kind,
        "at_utc": exchange_timestamps.get("at_utc"),
        "started_at_utc": exchange_timestamps.get("started_at_utc"),
        "completed_at_utc": exchange_timestamps.get("completed_at_utc"),
        "prompt": prompt_capture,
        "response": response_capture,
        "prompt_recorded": prompt_recorded,
        "response_recorded": response_recorded,
        "exchange_blob_ref": exchange_blob_ref_payload,
        "unavailable_reason": unavailable_reason,
    }
    return {key: item for key, item in payload.items() if item is not None}


def _coerce_exchange_blob_text(value: Any) -> dict[str, Any] | None:
    """Coerce a hydrated exchange prompt/response body into a text capture.

    Reuses :func:`_extract_text_capture` for strings and text-bearing mappings,
    falling back to a JSON rendering for structured request/response bodies so
    the exact exchange is preserved (JVNAUTOSCI-2385).
    """

    capture = _extract_text_capture(value)
    if capture:
        return capture
    if isinstance(value, (Mapping, list, tuple)):
        try:
            rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except Exception:
            return None
        rendered = rendered.strip()
        if not rendered or rendered in {"{}", "[]"}:
            return None
        return {
            "text": rendered,
            "char_count": len(rendered),
            "is_truncated": False,
        }
    return None


def _hydrate_llm_exchange_blob_into_entry(entry: dict[str, Any]) -> None:
    """Populate prompt/response/model/timestamps from an exchange blob ref.

    Resolves ``exchange_blob_ref`` for entries whose exact prompt or response
    was offloaded to the blob store, restoring the full exchange in the
    Thinking-card timestamped LLM interaction list (JVNAUTOSCI-2385). No-ops
    when the entry has no blob reference or already carries both bodies.
    """

    blob_ref = entry.get("exchange_blob_ref")
    if not isinstance(blob_ref, Mapping) or not blob_ref:
        return
    if entry.get("prompt") is not None and entry.get("response") is not None:
        return

    try:
        from .llm_exchange_blob_writer import read_llm_exchange_blob_ref

        body = read_llm_exchange_blob_ref(blob_ref)
    except Exception:
        body = None
    if not isinstance(body, Mapping):
        return

    blob_truncated = bool(body.get("truncated"))

    if entry.get("prompt") is None:
        request_body = body.get("request")
        prompt_value = (
            request_body.get("prompt") if isinstance(request_body, Mapping) else None
        )
        prompt_capture = _coerce_exchange_blob_text(prompt_value)
        if prompt_capture is not None:
            if blob_truncated:
                prompt_capture["is_truncated"] = True
            entry["prompt"] = prompt_capture
            entry["prompt_recorded"] = True

    if entry.get("response") is None:
        response_capture = _coerce_exchange_blob_text(body.get("response"))
        if response_capture is not None:
            if blob_truncated:
                response_capture["is_truncated"] = True
            entry["response"] = response_capture
            entry["response_recorded"] = True

    if not _safe_str(entry.get("model")):
        model = _safe_str(body.get("model"))
        if model:
            entry["model"] = model
    if not _safe_str(entry.get("provider")):
        provider = _safe_str(body.get("provider"))
        if provider:
            entry["provider"] = provider

    completed = _safe_str(body.get("first_output_at_utc")) or _safe_str(
        body.get("captured_at_utc")
    )
    started = _safe_str(body.get("sent_at_utc")) or _safe_str(
        body.get("prepared_at_utc")
    )
    if completed:
        entry.setdefault("completed_at_utc", completed)
        if not _safe_str(entry.get("at_utc")):
            entry["at_utc"] = completed
    if started:
        entry.setdefault("started_at_utc", started)

    if entry.get("prompt") is not None and entry.get("response") is not None:
        entry.pop("unavailable_reason", None)
        entry["exchange_source"] = "blob_ref_hydrated"

    extra = body.get("extra")
    if isinstance(extra, Mapping):
        status = _safe_str(extra.get("status"))
        if status and not _safe_str(entry.get("status")):
            entry["status"] = status
        success = extra.get("success")
        if isinstance(success, bool) and not isinstance(entry.get("success"), bool):
            entry["success"] = success
        failure = extra.get("failure")
        failure_mapping = failure if isinstance(failure, Mapping) else extra
        error = _safe_str(failure_mapping.get("error"))
        if error and not _safe_str(entry.get("error")):
            entry["error"] = error
        error_class = _safe_str(failure_mapping.get("error_class"))
        if error_class and not _safe_str(entry.get("error_class")):
            entry["error_class"] = error_class
        failure_kind = _safe_str(failure_mapping.get("failure_kind"))
        if failure_kind and not _safe_str(entry.get("failure_kind")):
            entry["failure_kind"] = failure_kind

    status = _safe_str(entry.get("status"))
    if (
        status in {"failed", "cancelled", "error", "timeout"}
        and entry.get("prompt") is not None
        and entry.get("response") is None
    ):
        entry["unavailable_reason"] = "failed_before_response"


def _collect_llm_exchange_entries(
    *,
    llm_debug: Mapping[str, Any] | None,
    turn_record: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def _is_llm_interaction_aux_entry(entry: Mapping[str, Any]) -> bool:
        call_type = (
            _safe_str(entry.get("type"))
            or _safe_str(entry.get("call_type"))
            or _safe_str(entry.get("entry_type"))
            or ""
        ).lower()
        if call_type.endswith("_skipped") or call_type.endswith("_skip"):
            return False
        if (
            entry.get("prompt") is not None
            or entry.get("prompt_preview") is not None
            or entry.get("prompt_capture") is not None
            or entry.get("response") is not None
            or entry.get("response_preview") is not None
            or entry.get("response_capture") is not None
            or entry.get("request") is not None
            or entry.get("selected") is not None
            or entry.get("exchange_blob_ref") is not None
        ):
            return True
        return call_type in {
            "workflow_selector_prompt",
            "workflow_selector",
            "workflow_selector_override",
            "workflow_model_policy_stage",
            "workflow_model_policy",
            "workflow_llm_call",
            "workflow_llm_exchange",
            "llm.generate",
        }

    def _append_from_sequence(
        value: Any,
        source: str,
        *,
        only_llm_interactions: bool = False,
    ) -> None:
        if not isinstance(value, Sequence) or isinstance(
            value, (str, bytes, bytearray)
        ):
            return
        for index, raw in enumerate(value):
            if not isinstance(raw, Mapping):
                continue
            call_type = (
                _safe_str(raw.get("type"))
                or _safe_str(raw.get("call_type"))
                or _safe_str(raw.get("entry_type"))
                or ""
            ).lower()
            if call_type.endswith("_skipped") or call_type.endswith("_skip"):
                continue
            if only_llm_interactions and not _is_llm_interaction_aux_entry(raw):
                continue
            rows.append(
                _normalise_llm_exchange_entry(
                    raw,
                    source=source,
                    source_index=index,
                )
            )

    def _append_stage_diagnostics(value: Any, source: str) -> None:
        if not isinstance(value, Sequence) or isinstance(
            value, (str, bytes, bytearray)
        ):
            return
        source_counter = 0
        for stage in value:
            if not isinstance(stage, Mapping):
                continue
            stage_id = _safe_str(stage.get("stage_id"))
            summaries = stage.get("llm_exchange_summaries")
            if not isinstance(summaries, Sequence) or isinstance(
                summaries, (str, bytes, bytearray)
            ):
                latest = stage.get("latest_llm_exchange")
                summaries = [latest] if isinstance(latest, Mapping) else []
            for raw in summaries:
                if not isinstance(raw, Mapping):
                    continue
                entry = dict(raw)
                if stage_id and not _safe_str(entry.get("workflow_stage_id")):
                    entry["workflow_stage_id"] = stage_id
                if stage_id and not _safe_str(entry.get("stage")):
                    entry["stage"] = stage_id
                rows.append(
                    _normalise_llm_exchange_entry(
                        entry,
                        source=source,
                        source_index=source_counter,
                    )
                )
                source_counter += 1

    if isinstance(llm_debug, Mapping):
        llm_interaction = llm_debug.get("llm_interaction")
        if isinstance(llm_interaction, Mapping):
            _append_from_sequence(
                llm_interaction.get("calls"),
                "chat_history.llm_debug_data.llm_interaction.calls",
            )
        _append_from_sequence(
            llm_debug.get("llm_calls"), "chat_history.llm_debug_data.llm_calls"
        )
        embedded_diagnostics = llm_debug.get("turn_execution_diagnostics")
        if isinstance(embedded_diagnostics, Mapping):
            _append_from_sequence(
                embedded_diagnostics.get("llm_calls"),
                "chat_history.llm_debug_data.turn_execution_diagnostics.llm_calls",
            )
            _append_from_sequence(
                embedded_diagnostics.get("aux_llm_calls"),
                "chat_history.llm_debug_data.turn_execution_diagnostics.aux_llm_calls",
                only_llm_interactions=True,
            )
            _append_stage_diagnostics(
                embedded_diagnostics.get("stage_diagnostics"),
                "chat_history.llm_debug_data.turn_execution_diagnostics.stage_diagnostics",
            )

    if isinstance(turn_record, Mapping):
        execution = turn_record.get("execution")
        if isinstance(execution, Mapping):
            _append_from_sequence(
                execution.get("llm_calls"),
                "mongo.turn_execution_records.execution.llm_calls",
            )
            _append_from_sequence(
                execution.get("aux_llm_calls"),
                "mongo.turn_execution_records.execution.aux_llm_calls",
                only_llm_interactions=True,
            )
        _append_from_sequence(
            turn_record.get("llm_calls"),
            "mongo.turn_execution_records.llm_calls",
        )
        _append_from_sequence(
            turn_record.get("aux_llm_calls"),
            "mongo.turn_execution_records.aux_llm_calls",
            only_llm_interactions=True,
        )
        turn_record_diagnostics = turn_record.get("turn_execution_diagnostics")
        if isinstance(turn_record_diagnostics, Mapping):
            _append_stage_diagnostics(
                turn_record_diagnostics.get("stage_diagnostics"),
                "mongo.turn_execution_records.turn_execution_diagnostics.stage_diagnostics",
            )

    aux_entries = _collect_routing_aux_entries(
        payload=None,
        llm_debug=llm_debug,
        turn_record=turn_record,
    )
    for index, aux_entry in enumerate(aux_entries):
        if not isinstance(aux_entry, Mapping):
            continue
        if not _is_llm_interaction_aux_entry(aux_entry):
            continue
        rows.append(
            _normalise_llm_exchange_entry(
                aux_entry,
                source="chat_history_or_projection.aux_llm_calls",
                source_index=index,
            )
        )

    deduped: list[dict[str, Any]] = []
    seen_keys: set[tuple[Any, ...]] = set()
    for row in rows:
        prompt = row.get("prompt")
        response = row.get("response")
        key = (
            row.get("call_type"),
            row.get("stage"),
            row.get("workflow_stage_id"),
            row.get("model"),
            row.get("at_utc"),
            (prompt.get("text") if isinstance(prompt, Mapping) else None),
            (response.get("text") if isinstance(response, Mapping) else None),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(row)

    def _sort_key(row: Mapping[str, Any]) -> tuple[int, str, int]:
        timestamp = _safe_str(row.get("at_utc")) or ""
        sequence = _safe_non_negative_int(row.get("source_index"))
        return (0 if timestamp else 1, timestamp, sequence or 0)

    deduped.sort(key=_sort_key)

    for index, row in enumerate(deduped):
        row["sequence_no"] = index + 1
    return deduped


def get_turn_llm_call_log_payload(
    *,
    request_id: str,
    namespace: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any] | None:
    request_id_value = _safe_str(request_id)
    if not request_id_value:
        return None

    safe_namespace = _safe_str(namespace)
    history_context = _resolve_history_context(
        request_id=request_id_value,
        namespace=safe_namespace,
    )
    turn_record = _load_turn_execution_record(
        request_id=request_id_value,
        namespace=safe_namespace
        or (
            _safe_str(history_context.get("namespace"))
            if isinstance(history_context, Mapping)
            else None
        ),
    )
    if not isinstance(history_context, Mapping) and not isinstance(
        turn_record, Mapping
    ):
        return None

    llm_debug = (
        history_context.get("target_llm_debug")
        if isinstance(history_context, Mapping)
        else None
    )
    llm_debug_mapping = (
        _safe_mapping(llm_debug) if isinstance(llm_debug, Mapping) else None
    )

    all_entries = _collect_llm_exchange_entries(
        llm_debug=llm_debug_mapping,
        turn_record=turn_record,
    )

    bounded_limit = int(max(1, min(200, int(limit))))
    bounded_offset = int(max(0, int(offset)))

    total_count = len(all_entries)
    page = all_entries[bounded_offset : bounded_offset + bounded_limit]
    next_offset = bounded_offset + len(page)
    has_more = next_offset < total_count

    # Surface the exact prompt/response for the returned page by hydrating any
    # exchange blob references (JVNAUTOSCI-2385). Blob I/O is bounded to the
    # current page only. Page entries are the same dict objects held in
    # ``all_entries`` (list slicing shares references), so the truncated and
    # timestamped counts below reflect the hydrated values.
    for entry in page:
        _hydrate_llm_exchange_blob_into_entry(entry)

    truncated_entry_count = sum(
        1
        for entry in all_entries
        if (
            isinstance(entry.get("prompt"), Mapping)
            and entry.get("prompt", {}).get("is_truncated") is True
        )
        or (
            isinstance(entry.get("response"), Mapping)
            and entry.get("response", {}).get("is_truncated") is True
        )
    )

    return {
        "schema_version": "turn_llm_call_log.v1",
        "request_id": request_id_value,
        "namespace": safe_namespace
        or (
            _safe_str(history_context.get("namespace"))
            if isinstance(history_context, Mapping)
            else None
        )
        or (
            _safe_str(turn_record.get("namespace"))
            if isinstance(turn_record, Mapping)
            else None
        ),
        "chat_session_id": (
            _safe_str(history_context.get("session_id"))
            if isinstance(history_context, Mapping)
            else None
        )
        or (
            _safe_str(turn_record.get("session_id"))
            if isinstance(turn_record, Mapping)
            else None
        ),
        "derived_user_concept_id": (
            _safe_str(history_context.get("user_id"))
            if isinstance(history_context, Mapping)
            else None
        )
        or (
            _safe_str(turn_record.get("user_id"))
            if isinstance(turn_record, Mapping)
            else None
        ),
        "offset": bounded_offset,
        "limit": bounded_limit,
        "returned_count": len(page),
        "total_count": total_count,
        "empty_reason": "no_llm_interactions_recorded" if total_count == 0 else None,
        "has_more": has_more,
        "next_offset": (next_offset if has_more else None),
        "truncated_entry_count": truncated_entry_count,
        "timestamped_entry_count": sum(
            1 for entry in all_entries if _safe_str(entry.get("at_utc"))
        ),
        "entries": page,
        "diagnostics_source": (
            "chat_history.llm_debug_data"
            if isinstance(history_context, Mapping)
            else "mongo.turn_execution_records"
        ),
    }
