from flask import (
    Blueprint,
    request,
    jsonify,
    render_template,
    current_app,
    session,
    send_file,
)
import os
import re
import time
import threading
import secrets
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, cast
from src.workflows.onboarding_workflow import run_onboarding_workflow
from ...languagemodels.llm_interface import get_llm_client, get_active_model_name
from .settings_routes import get_all_settings_data
from ...integrations.internal_mcp import ProgressTracker, ToolCallParsingError
from ...services import chat_history_service
from ...services.window_session_context_service import (
    get_or_create_window_context,
    set_window_organisation,
    clear_window_organisation,
    get_effective_context,
)
from ...services.settings_service import (
    get_internal_mcp_max_tool_invocations,
    get_internal_mcp_tool_batch_cap,
    get_show_tool_use_during_thinking,
    get_buttonify_model_enabled,
    get_buttonify_heuristic_preflight_enabled,
)
from ...services.feature_flags import (
    get_display_elements_screen_fence_compat_enabled,
)
from ...services.chat_concept_reference_service import (
    build_context_concept_reference_metadata,
)
from ...services.buttonify_service import (
    BUTTONIFY_PROMPT_IDS,
    BUTTONIFY_PROMPT_TEMPLATE,
    dedupe_buttonify_options,
    extract_buttonify_options_heuristic as _extract_buttonify_options_heuristic,
    parse_buttonify_options_json,
)
from ...services.display_elements_service import (
    build_canonical_table_payload_from_records,
    build_turn_display_elements,
)
from ...services.response_transformation_telemetry import (
    build_response_transformation_event,
    build_response_transformation_telemetry_payload,
    record_response_transformation_event,
)
from ...services.turn_execution_record_service import build_turn_execution_record
from ...workflows import (
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    WorkflowExecutionTrace,
    insert_workflow_execution_trace,
)
from ...workflows.conversation_turn_stage_model import (
    build_conversation_turn_stage_model_snapshot,
    build_conversation_turn_stage_path,
)

# NOTE: Previous relative template_folder path ('../../frontend/...') was incorrect.
# From this file (src/backend/server/routes/von_routes.py) we need to traverse up THREE levels
# to reach the 'src' directory, then descend into frontend/web/von_interface/templates
# would raise TemplateNotFound for 'von_interface.html'.
_TEMPLATE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "../../../frontend/web/von_interface/templates"
    )
)
von_bp = Blueprint("von", __name__, template_folder=_TEMPLATE_DIR)


# ----------------- Tool progress (JVNAUTOSCI-942) -----------------

_TOOL_PROGRESS_TTL_SEC = 10 * 60
_TOOL_PROGRESS_LOCK = threading.Lock()
_TOOL_PROGRESS: dict[tuple[str, str], dict[str, Any]] = {}
_TOOL_PROGRESS_TERMINAL_STATUSES = {"completed", "error", "cancelled"}
_TOOL_PROGRESS_DIAGNOSTIC_EVENT_LIMIT = 80
_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT = 40
_TURN_EXECUTION_DIAGNOSTICS_PROMPT_PREVIEW_LIMIT = 1000


def _env_int(name: str, default: int, *, min_value: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = int(default)
    return max(min_value, value)


_TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC = _env_int(
    "VON_TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC", 5, min_value=1
)
_TOOL_PROGRESS_WAITING_THRESHOLD_SEC = _env_int(
    "VON_TOOL_PROGRESS_WAITING_THRESHOLD_SEC", 12, min_value=2
)
_TOOL_PROGRESS_STALL_THRESHOLD_SEC = _env_int(
    "VON_TOOL_PROGRESS_STALL_THRESHOLD_SEC", 45, min_value=5
)
if _TOOL_PROGRESS_WAITING_THRESHOLD_SEC >= _TOOL_PROGRESS_STALL_THRESHOLD_SEC:
    _TOOL_PROGRESS_STALL_THRESHOLD_SEC = (
        _TOOL_PROGRESS_WAITING_THRESHOLD_SEC + _TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC
    )


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _progress_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _progress_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _default_stage_label(stage: str) -> str:
    mapping = {
        "context_build": "Building context",
        "workflow_discovery": "Searching for workflows",
        "workflow_discovery_complete": "Found workflows",
        "tool_plan": "Planning tool calls",
        "tool_execute": "Executing tools",
        "screen_backfill": "Generating response",
        "narration": "Generating narration",
        "buttonify": "Generating quick replies",
        "tool_recovery": "Recovering tool call",
        "orchestrator_start": "Starting orchestrator",
        "orchestrator_end": "Finishing orchestrator",
        "completed": "Complete",
        "error": "Error",
    }
    if stage in mapping:
        return mapping[stage]
    return stage.replace("_", " ").strip().title() or "Thinking"


def _derive_progress_stage(update: dict[str, Any], existing: dict[str, Any]) -> str:
    explicit_stage = _progress_str(update.get("stage"))
    if explicit_stage:
        return explicit_stage

    phase = _progress_str(update.get("phase"))
    if phase:
        return phase

    status = _progress_str(update.get("status")) or ""
    status_stage_map = {
        "orchestrator_start": "orchestrator_start",
        "orchestrator_end": "orchestrator_end",
        "llm_call_start": "llm_call",
        "llm_call_chunk": "llm_call",
        "llm_call_end": "llm_call",
        "tool_call_start": "tool_execute",
        "tool_invoked": "tool_execute",
        "tool_failed": "tool_execute",
        "tool_blocked": "tool_execute",
        "retry_start": "tool_recovery",
        "retry_end": "tool_recovery",
        "completed": "completed",
        "error": "error",
    }
    derived = status_stage_map.get(status)
    if derived:
        return derived

    existing_stage = _progress_str(existing.get("stage"))
    if existing_stage:
        return existing_stage

    existing_phase = _progress_str(existing.get("phase"))
    if existing_phase:
        return existing_phase

    return "context_build"


def _derive_progress_event_kind(update: dict[str, Any]) -> str:
    explicit_kind = _progress_str(update.get("event_kind"))
    if explicit_kind:
        return explicit_kind

    status = _progress_str(update.get("status")) or ""
    status_kind_map = {
        "phase_transition": "stage_event",
        "heartbeat": "heartbeat",
        "orchestrator_start": "orchestrator_start",
        "orchestrator_end": "orchestrator_end",
        "llm_call_start": "llm_call_start",
        "llm_call_chunk": "llm_call_chunk",
        "llm_call_end": "llm_call_end",
        "tool_call_start": "tool_call_start",
        "tool_invoked": "tool_call_end",
        "tool_failed": "tool_call_end",
        "tool_blocked": "tool_call_end",
        "retry_start": "retry_start",
        "retry_end": "retry_end",
        "completed": "completed",
        "error": "error",
    }
    return status_kind_map.get(status, status or "status_update")


def _classify_progress_cause(stage: str, status: str) -> str:
    stage_lower = stage.lower()
    status_lower = status.lower()

    if "tool" in stage_lower or status_lower.startswith("tool_"):
        return "tool_timeout"
    if "llm" in stage_lower or status_lower.startswith("llm_"):
        return "model_timeout"
    if "retry" in stage_lower or status_lower.startswith("retry_"):
        return "model_timeout"
    if "orchestrator" in stage_lower:
        return "worker_unavailable"
    return "network_silence"


def _derive_progress_liveness(
    state: dict[str, Any], *, now_epoch: float | None = None
) -> dict[str, Any]:
    now = float(now_epoch if now_epoch is not None else time.time())

    status = (_progress_str(state.get("status")) or "thinking").lower()
    stage = _progress_str(state.get("stage")) or _progress_str(state.get("phase")) or ""

    last_activity_epoch = _progress_number(
        state.get("last_activity_epoch")
    ) or _progress_number(state.get("updated_at_epoch"))
    if last_activity_epoch is None:
        last_activity_epoch = now

    last_stage_activity_epoch = _progress_number(
        state.get("last_stage_activity_epoch")
    ) or last_activity_epoch

    activity_idle_ms = int(max(0.0, (now - float(last_activity_epoch)) * 1000.0))
    stage_idle_ms = int(max(0.0, (now - float(last_stage_activity_epoch)) * 1000.0))

    if status in _TOOL_PROGRESS_TERMINAL_STATUSES:
        liveness_state = "active"
        liveness_reason = status
        stall_detected = False
    elif activity_idle_ms >= int(_TOOL_PROGRESS_STALL_THRESHOLD_SEC * 1000):
        liveness_state = "stalled"
        liveness_reason = _classify_progress_cause(stage, status)
        stall_detected = True
    elif stage_idle_ms >= int(_TOOL_PROGRESS_WAITING_THRESHOLD_SEC * 1000):
        liveness_state = "waiting"
        liveness_reason = _classify_progress_cause(stage, status)
        stall_detected = False
    else:
        liveness_state = "active"
        liveness_reason = "recent_activity"
        stall_detected = False

    return {
        "liveness_state": liveness_state,
        "liveness_reason": liveness_reason,
        "stall_detected": stall_detected,
        "waiting_threshold_sec": int(_TOOL_PROGRESS_WAITING_THRESHOLD_SEC),
        "stall_threshold_sec": int(_TOOL_PROGRESS_STALL_THRESHOLD_SEC),
        "activity_idle_ms": activity_idle_ms,
        "stage_idle_ms": stage_idle_ms,
    }


def _serialise_tool_progress_state(
    state: dict[str, Any], *, now_epoch: float | None = None
) -> dict[str, Any]:
    payload = dict(state)
    payload.update(_derive_progress_liveness(payload, now_epoch=now_epoch))

    payload.pop("updated_at_epoch", None)
    payload.pop("request_started_epoch", None)
    payload.pop("last_activity_epoch", None)
    payload.pop("last_stage_activity_epoch", None)

    events = payload.get("diagnostic_events")
    if isinstance(events, list):
        payload["diagnostic_events"] = list(
            events[-_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT :]
        )

    return payload


def _build_tool_progress_compact_summary(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None

    summary = state.get("diagnostic_summary")
    summary = summary if isinstance(summary, dict) else {}
    counters = state.get("counters")
    counters = counters if isinstance(counters, dict) else {}

    return {
        "request_id": state.get("request_id"),
        "sequence_no": state.get("sequence_no"),
        "status": state.get("status"),
        "stage": state.get("stage"),
        "subtask": state.get("subtask"),
        "elapsed_ms": state.get("elapsed_ms"),
        "idle_ms": state.get("activity_idle_ms", state.get("idle_ms")),
        "stage_idle_ms": state.get("stage_idle_ms"),
        "liveness_state": state.get("liveness_state"),
        "liveness_reason": state.get("liveness_reason"),
        "stall_detected": state.get("stall_detected"),
        "last_activity_at_utc": state.get("last_activity_at_utc"),
        "event_count": summary.get("event_count"),
        "counters": {
            "tokens_streamed": counters.get("tokens_streamed", 0),
            "tools_started": counters.get("tools_started", 0),
            "tools_completed": counters.get("tools_completed", 0),
        },
        "waiting_threshold_sec": state.get("waiting_threshold_sec"),
        "stall_threshold_sec": state.get("stall_threshold_sec"),
    }


def _iso_utc_to_epoch_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000.0)


def _normalise_progress_events_from_diagnostic_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    progress_events: list[dict[str, Any]] = []
    for entry in events:
        stage = _progress_str(entry.get("stage")) or _progress_str(entry.get("phase"))
        subtask = _progress_str(entry.get("subtask")) or _progress_str(
            entry.get("tool")
        ) or _progress_str(entry.get("workflow_task"))

        sequence_no_raw = _progress_number(entry.get("sequence_no"))
        sequence_no = int(sequence_no_raw) if sequence_no_raw is not None else None

        idle_raw = _progress_number(entry.get("idle_ms"))
        if idle_raw is None:
            idle_raw = _progress_number(entry.get("activity_idle_ms"))
        idle_ms = int(idle_raw) if idle_raw is not None else None
        duration_raw = _progress_number(entry.get("duration_ms"))
        duration_ms = int(max(0.0, duration_raw)) if duration_raw is not None else None
        model = _progress_str(entry.get("model"))
        success = entry.get("success")
        if not isinstance(success, bool):
            success = None

        progress_events.append(
            {
                "at_utc": _progress_str(entry.get("at_utc")),
                "status": _progress_str(entry.get("status")),
                "stage": stage,
                "sequence_no": sequence_no,
                "liveness_state": _progress_str(entry.get("liveness_state")),
                "idle_ms": idle_ms,
                "subtask": subtask,
                "duration_ms": duration_ms,
                "model": model,
                "success": success,
            }
        )
    return progress_events


def _derive_phase_history_from_diagnostic_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    phase_history: list[dict[str, Any]] = []
    last_phase: str | None = None

    for entry in events:
        status = (_progress_str(entry.get("status")) or "").lower()
        if status != "phase_transition":
            continue

        phase = _progress_str(entry.get("phase")) or _progress_str(entry.get("stage"))
        if not phase:
            continue
        if last_phase == phase:
            continue

        phase_history.append(
            {
                "phase": phase,
                "phaseLabel": _progress_str(entry.get("phase_label"))
                or _progress_str(entry.get("stage_label")),
                "timestamp": _iso_utc_to_epoch_ms(entry.get("at_utc")),
            }
        )
        last_phase = phase

    return phase_history[-_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT :]


def _derive_tool_history_from_diagnostic_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tool_history: list[dict[str, Any]] = []

    for entry in events:
        tool = _progress_str(entry.get("tool")) or ""
        workflow_task = _progress_str(entry.get("workflow_task")) or ""
        if not tool and not workflow_task:
            continue

        phase = _progress_str(entry.get("phase")) or _progress_str(entry.get("stage")) or ""
        result_summary = _progress_str(entry.get("result_summary")) or ""
        status = (_progress_str(entry.get("status")) or "").lower()

        batch_size_raw = _progress_number(entry.get("batch_size"))
        batch_size: int | float | None
        if batch_size_raw is None:
            batch_size = None
        elif float(batch_size_raw).is_integer():
            batch_size = int(batch_size_raw)
        else:
            batch_size = float(batch_size_raw)

        last = tool_history[-1] if tool_history else None
        if (
            isinstance(last, dict)
            and last.get("tool") == tool
            and last.get("workflowTask") == workflow_task
            and last.get("batchSize") == batch_size
        ):
            if result_summary:
                last["resultSummary"] = result_summary
            if status == "tool_invoked":
                last["success"] = True
            elif status in {"tool_failed", "tool_blocked", "error"}:
                last["success"] = False
            continue

        success: bool | None = None
        if status == "tool_invoked":
            success = True
        elif status in {"tool_failed", "tool_blocked", "error"}:
            success = False

        tool_history.append(
            {
                "tool": tool,
                "workflowTask": workflow_task,
                "batchSize": batch_size,
                "phase": phase,
                "resultSummary": result_summary,
                "success": success,
            }
        )

    return tool_history[-_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT :]


def _latest_event_timestamp_ms(events: list[dict[str, Any]]) -> int | None:
    for entry in reversed(events):
        timestamp = _iso_utc_to_epoch_ms(entry.get("at_utc"))
        if timestamp is not None:
            return timestamp
    return None


def _normalise_llm_stage_calls(
    *,
    llm_calls: list[dict[str, Any]],
    diagnostic_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if llm_calls:
        source_entries: list[dict[str, Any]] = llm_calls
    else:
        source_entries = []
        for event in diagnostic_events:
            status = (_progress_str(event.get("status")) or "").lower()
            if status != "llm_call_end":
                continue
            source_entries.append(event)

    normalised: list[dict[str, Any]] = []
    for entry in source_entries:
        if not isinstance(entry, dict):
            continue
        duration_raw = _progress_number(entry.get("duration_ms"))
        if duration_raw is None:
            continue
        duration_ms = int(max(0.0, float(duration_raw)))
        stage = (
            _progress_str(entry.get("stage"))
            or _progress_str(entry.get("phase"))
            or "unscoped"
        )
        model = _progress_str(entry.get("model")) or "unknown"
        normalised.append(
            {
                "stage": stage,
                "model": model,
                "provider": _progress_str(entry.get("provider")),
                "duration_ms": duration_ms,
            }
        )
    return normalised


def _build_timing_breakdown(
    *,
    diagnostic_events: list[dict[str, Any]],
    phase_history: list[dict[str, Any]],
    llm_calls: list[dict[str, Any]],
    elapsed_ms_value: int | None,
) -> dict[str, Any]:
    stage_totals: dict[str, dict[str, Any]] = {}
    stage_order: dict[str, int] = {}
    order_counter = 0

    def _touch_stage(stage: str) -> dict[str, Any]:
        nonlocal order_counter
        row = stage_totals.get(stage)
        if row is None:
            row = {
                "stage": stage,
                "elapsed_ms": None,
                "llm_elapsed_ms": 0,
                "llm_call_count": 0,
                "non_llm_elapsed_ms": None,
            }
            stage_totals[stage] = row
        if stage not in stage_order:
            stage_order[stage] = order_counter
            order_counter += 1
        return row

    latest_timestamp = _latest_event_timestamp_ms(diagnostic_events)
    first_phase_timestamp: int | None = None

    for index, phase_entry in enumerate(phase_history):
        stage = _progress_str(phase_entry.get("phase"))
        start_raw = _progress_number(phase_entry.get("timestamp"))
        if not stage or start_raw is None:
            continue
        start_ts = int(start_raw)
        if first_phase_timestamp is None:
            first_phase_timestamp = start_ts

        next_ts: int | None = None
        if index + 1 < len(phase_history):
            next_raw = _progress_number(phase_history[index + 1].get("timestamp"))
            if next_raw is not None:
                next_ts = int(next_raw)
        if next_ts is None:
            next_ts = latest_timestamp
        if next_ts is None:
            continue

        duration_ms = int(max(0, next_ts - start_ts))
        stage_row = _touch_stage(stage)
        existing_elapsed = stage_row.get("elapsed_ms")
        if isinstance(existing_elapsed, int):
            stage_row["elapsed_ms"] = existing_elapsed + duration_ms
        else:
            stage_row["elapsed_ms"] = duration_ms

    llm_stage_calls = _normalise_llm_stage_calls(
        llm_calls=llm_calls,
        diagnostic_events=diagnostic_events,
    )
    llm_by_stage_model: dict[tuple[str, str], dict[str, Any]] = {}
    llm_total_ms = 0

    for entry in llm_stage_calls:
        stage = cast(str, entry["stage"])
        model = cast(str, entry["model"])
        duration_ms = int(entry["duration_ms"])
        provider = _progress_str(entry.get("provider"))

        stage_row = _touch_stage(stage)
        stage_row["llm_elapsed_ms"] = int(stage_row["llm_elapsed_ms"]) + duration_ms
        stage_row["llm_call_count"] = int(stage_row["llm_call_count"]) + 1

        key = (stage, model)
        bucket = llm_by_stage_model.get(key)
        if bucket is None:
            bucket = {
                "stage": stage,
                "model": model,
                "provider": provider,
                "call_count": 0,
                "duration_ms": 0,
            }
            llm_by_stage_model[key] = bucket
        bucket["call_count"] = int(bucket["call_count"]) + 1
        bucket["duration_ms"] = int(bucket["duration_ms"]) + duration_ms
        if bucket.get("provider") is None and provider is not None:
            bucket["provider"] = provider

        llm_total_ms += duration_ms

    for row in stage_totals.values():
        elapsed = row.get("elapsed_ms")
        llm_elapsed = int(row.get("llm_elapsed_ms") or 0)
        if isinstance(elapsed, int):
            row["non_llm_elapsed_ms"] = max(0, elapsed - llm_elapsed)

    stage_rows = sorted(
        stage_totals.values(),
        key=lambda item: (stage_order.get(cast(str, item.get("stage")), 1_000_000), cast(str, item.get("stage"))),
    )

    llm_rows = sorted(
        llm_by_stage_model.values(),
        key=lambda item: (
            stage_order.get(cast(str, item.get("stage")), 1_000_000),
            -int(item.get("duration_ms") or 0),
            cast(str, item.get("model")),
        ),
    )

    phase_elapsed_total = sum(
        int(row["elapsed_ms"])
        for row in stage_rows
        if isinstance(row.get("elapsed_ms"), int)
    )
    observed_timeline_ms: int | None = None
    if (
        first_phase_timestamp is not None
        and latest_timestamp is not None
        and latest_timestamp >= first_phase_timestamp
    ):
        observed_timeline_ms = int(latest_timestamp - first_phase_timestamp)

    return {
        "schema_version": "conversation_turn_timing_breakdown.v1",
        "stages": stage_rows,
        "llm_calls_by_stage_model": llm_rows,
        "totals": {
            "elapsed_ms": elapsed_ms_value,
            "observed_timeline_ms": observed_timeline_ms,
            "phase_elapsed_ms": phase_elapsed_total,
            "llm_elapsed_ms": llm_total_ms,
            "llm_call_count": len(llm_stage_calls),
        },
    }


def _build_turn_execution_diagnostics(
    *,
    request_id: str | None,
    prompt_text: str | None,
    elapsed_ms: float | int | None = None,
    tool_progress_state: dict[str, Any] | None = None,
    workflow_discovery: dict[str, Any] | None = None,
    generated_at_utc: str | None = None,
    llm_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prompt_preview = (
        prompt_text[:_TURN_EXECUTION_DIAGNOSTICS_PROMPT_PREVIEW_LIMIT]
        if isinstance(prompt_text, str)
        else None
    )

    latest_progress = dict(tool_progress_state) if isinstance(tool_progress_state, dict) else None

    diagnostic_events: list[dict[str, Any]] = []
    if isinstance(latest_progress, dict):
        raw_events = latest_progress.get("diagnostic_events")
        if isinstance(raw_events, list):
            diagnostic_events = [
                cast(dict[str, Any], entry)
                for entry in raw_events
                if isinstance(entry, dict)
            ][-_TURN_EXECUTION_DIAGNOSTICS_EVENT_LIMIT :]

    effective_elapsed = _progress_number(elapsed_ms)
    if effective_elapsed is None and isinstance(latest_progress, dict):
        effective_elapsed = _progress_number(latest_progress.get("elapsed_ms"))
    elapsed_ms_value = int(max(0.0, effective_elapsed)) if effective_elapsed is not None else None

    workflow_payload = workflow_discovery
    if workflow_payload is None and isinstance(latest_progress, dict):
        progress_workflow = latest_progress.get("workflow_discovery")
        if isinstance(progress_workflow, dict):
            workflow_payload = dict(progress_workflow)

    effective_request_id = _progress_str(request_id)
    if effective_request_id is None and isinstance(latest_progress, dict):
        effective_request_id = _progress_str(latest_progress.get("request_id"))

    phase_history = _derive_phase_history_from_diagnostic_events(diagnostic_events)
    runtime_stages = [entry.get("phase") for entry in phase_history]
    workflow_stage_path = build_conversation_turn_stage_path(
        runtime_stages=runtime_stages,
        workflow_id=None,
    )
    llm_call_entries = [
        cast(dict[str, Any], entry)
        for entry in (llm_calls or [])
        if isinstance(entry, dict)
    ]
    timing_breakdown = _build_timing_breakdown(
        diagnostic_events=diagnostic_events,
        phase_history=phase_history,
        llm_calls=llm_call_entries,
        elapsed_ms_value=elapsed_ms_value,
    )

    return {
        "generated_at_utc": _progress_str(generated_at_utc) or _now_utc_iso(),
        "request_id": effective_request_id,
        "elapsed_ms": elapsed_ms_value,
        "prompt_preview": prompt_preview,
        "latest_progress": latest_progress,
        "progress_events": _normalise_progress_events_from_diagnostic_events(
            diagnostic_events
        ),
        "phase_history": phase_history,
        "tool_history": _derive_tool_history_from_diagnostic_events(diagnostic_events),
        "workflow_discovery": workflow_payload,
        "workflow_stage_model": build_conversation_turn_stage_model_snapshot(),
        "workflow_stage_path": workflow_stage_path,
        "timing_breakdown": timing_breakdown,
    }


def _start_tool_progress_heartbeat(
    scope_key: str, request_id: str
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_event.wait(float(_TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC)):
            try:
                current = _get_tool_progress(scope_key, request_id)
                if not isinstance(current, dict):
                    continue
                status = (_progress_str(current.get("status")) or "").lower()
                if status in _TOOL_PROGRESS_TERMINAL_STATUSES:
                    break
                _set_tool_progress(
                    scope_key,
                    request_id,
                    {
                        "status": "heartbeat",
                        "request_id": request_id,
                        "heartbeat_interval_sec": int(
                            _TOOL_PROGRESS_HEARTBEAT_INTERVAL_SEC
                        ),
                    },
                )
            except Exception:
                # Heartbeat is best-effort and must never break user requests.
                continue

    thread = threading.Thread(
        target=_heartbeat_loop,
        name=f"tool-progress-heartbeat-{request_id[:8]}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _stop_tool_progress_heartbeat(
    stop_event: threading.Event | None, thread: threading.Thread | None
) -> None:
    try:
        if stop_event is not None:
            stop_event.set()
    except Exception:
        pass
    try:
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.3)
    except Exception:
        pass


def _slugify_concept_id_for_key(concept_id: str) -> str:
    cleaned = (concept_id or "").strip()
    if cleaned.startswith("#V#"):
        cleaned = cleaned[3:]
    cleaned = cleaned.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    return cleaned or "unknown"


def _get_tool_progress_scope_key() -> str:
    """Return a stable scope key for tool-progress lookup.

    - Authenticated: scope is user concept id
    - Unauthenticated: scope is a session-scoped random token
    """

    user_concept_id = None
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if isinstance(user_concept_id, str) and user_concept_id.strip():
        return f"user:{user_concept_id.strip()}"

    if "tool_progress_scope" not in session:
        session["tool_progress_scope"] = secrets.token_urlsafe(16)

    return f"anon:{session.get('tool_progress_scope')}"


@von_bp.route("/api/files/upload", methods=["POST"])
def upload_file_to_blob_store_and_vontology():
    """Upload a user-provided file into the configured blob store and register it in Vontology.

    Security: user identity is derived server-side via get_effective_user_concept_id().

    Multipart form-data:
      - file: the uploaded file

    Returns JSON:
      - success
      - uploaded: { concept_id, type_concept_id, sha256, size_bytes, content_type, original_filename }
      - storage: { backend, key, uri, content_type, size_bytes, metadata }
    """

    from werkzeug.utils import secure_filename

    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return (
            jsonify(
                {
                    "success": False,
                    "error": "missing_user_context",
                    "message": "Missing user context: establish an authenticated session first.",
                }
            ),
            401,
        )

    # Ensure the upload is associated with a stable chat session so it becomes part
    # of the same persisted history that /von/generate uses.
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    session_id = session["session_id"]

    if "file" not in request.files:
        return jsonify({"success": False, "error": "missing_file"}), 400

    uploaded = request.files.get("file")
    if not uploaded or not getattr(uploaded, "filename", None):
        return jsonify({"success": False, "error": "empty_upload"}), 400

    original_filename = str(uploaded.filename)
    safe_filename = secure_filename(original_filename) or "uploaded_file"
    content_type = getattr(uploaded, "mimetype", None) or None

    try:
        data = uploaded.read()
    except Exception as exc:
        current_app.logger.warning(f"[files/upload] Failed to read upload: {exc}")
        return jsonify({"success": False, "error": "read_failed"}), 400

    if not isinstance(data, (bytes, bytearray)) or not data:
        return jsonify({"success": False, "error": "empty_bytes"}), 400

    data_bytes = bytes(data)

    import hashlib

    sha256 = hashlib.sha256(data_bytes).hexdigest()
    size_bytes = len(data_bytes)

    from ...services.blob_uploads import BlobUploadError, put_bytes_durable

    user_slug = _slugify_concept_id_for_key(user_concept_id)
    blob_key = f"uploads/{user_slug}/{sha256}/{safe_filename}"
    uploaded_at = _now_utc_iso()

    try:
        stored = put_bytes_durable(
            key=blob_key,
            data=data_bytes,
            content_type=content_type,
            sha256=sha256,
            size_bytes=size_bytes,
            metadata={
                "original_filename": original_filename,
                "user_concept_id": user_concept_id.strip(),
                "uploaded_at": uploaded_at,
            },
        )
    except BlobUploadError as exc:
        current_app.logger.error(
            "[files/upload] Blob store upload failed: %s",
            exc,
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "blob_store_upload_failed",
                    "message": str(exc),
                }
            ),
            502,
        )

    blob_ref = stored.ref

    # --- Ensure KR infrastructure exists ---
    type_concept_id = "#V#computer_file_copy"

    try:
        from ...db.repositories.concepts_repository import ConceptsRepository
        from ...vontology.utils_vontology import (
            THING_PRIMARY_ID,
            create_vontology_concept,
            ensure_thing_exists_and_link_orphans,
        )

        if not ConceptsRepository.find_one({"concept_id": type_concept_id}):
            # Prefer a store-of-information parent if present; otherwise fall back to Thing.
            parent_id = (
                "#V#store_of_information"
                if ConceptsRepository.find_one(
                    {"concept_id": "#V#store_of_information"}
                )
                else THING_PRIMARY_ID
            )
            if parent_id == THING_PRIMARY_ID:
                # Best-effort: ensure Thing exists.
                ensure_thing_exists_and_link_orphans()

            created = create_vontology_concept(
                parent_id=parent_id,
                new_concept_name="Computer File Copy",
                create_as_instance=False,
                description=(
                    "A computer file copy is an information-bearing artefact representing a specific stored byte sequence "
                    "(for example an uploaded file stored in Von's blob store)."
                ),
                notes=(
                    "Created on-demand by Von's chat file upload flow. Instances typically have blob store metadata "
                    "(URI, key, content type, size, and hash) recorded as text relations."
                ),
            )
            if not created.get("success"):
                current_app.logger.warning(
                    "[files/upload] Failed to create Computer File Copy type: %s",
                    created.get("message"),
                )
    except Exception as exc:
        current_app.logger.warning(
            f"[files/upload] KR type ensure failed (continuing): {exc}"
        )

    # --- Create the file-copy instance concept ---
    instance_concept_id = f"#V#uploaded_file_copy_{uuid.uuid4().hex}"

    try:
        from ...services import concept_service
        from ...db.repositories.concepts_repository import ConceptsRepository
        from ...services.text_value_service import upsert_text_for_concept

        instance = concept_service.create_concept(
            name=original_filename,
            concept_id=instance_concept_id,
            parent_concept_ids=[type_concept_id],
            create_as_instance=True,
            system_tags=["uploaded", "file", "blob_store"],
            attributes={
                "sha256": sha256,
                "size_bytes": size_bytes,
                "content_type": content_type,
                "blob_backend": blob_ref.backend,
                "blob_key": blob_ref.key,
                "blob_uri": blob_ref.uri,
            },
        )

        # Scope visibility to the current user.
        ConceptsRepository.update_one(
            {"concept_id": instance_concept_id},
            {"$set": {"relationships.specific_to_user": [user_concept_id.strip()]}},
        )

        # Attach blob + metadata as text relations (authoritative)
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_original_filename",
            text=original_filename,
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_sha256",
            text=sha256,
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_size_bytes",
            text=str(size_bytes),
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_upload_timestamp",
            text=str(uploaded_at),
            lang="en-NZ",
        )
        if content_type:
            upsert_text_for_concept(
                subject_concept_id=instance_concept_id,
                predicate="#V#has_mime_type",
                text=content_type,
                lang="en-NZ",
            )

        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_blob_backend",
            text=str(blob_ref.backend),
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_blob_key",
            text=str(blob_ref.key),
            lang="en-NZ",
        )
        upsert_text_for_concept(
            subject_concept_id=instance_concept_id,
            predicate="#V#has_blob_uri",
            text=str(blob_ref.uri),
            lang="en-NZ",
        )
    except Exception as exc:
        current_app.logger.error(
            f"[files/upload] Failed to register uploaded file in Vontology: {exc}",
            exc_info=True,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": "vontology_register_failed",
                    "detail": str(exc),
                }
            ),
            500,
        )

    chat_history_recorded = _record_file_upload_in_chat_history(
        user_concept_id=user_concept_id.strip(),
        session_id=session_id,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
        blob_backend=str(blob_ref.backend),
        blob_key=str(blob_ref.key),
        blob_uri=str(blob_ref.uri),
        file_copy_concept_id=instance_concept_id,
    )

    return (
        jsonify(
            {
                "success": True,
                "uploaded": {
                    "concept_id": instance_concept_id,
                    "type_concept_id": type_concept_id,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "content_type": content_type,
                    "original_filename": original_filename,
                },
                "storage": {
                    "backend": blob_ref.backend,
                    "key": blob_ref.key,
                    "uri": blob_ref.uri,
                    "content_type": blob_ref.content_type,
                    "size_bytes": blob_ref.size_bytes,
                    "metadata": blob_ref.metadata,
                },
                "chat_history_recorded": bool(chat_history_recorded),
            }
        ),
        200,
    )


def _record_file_upload_in_chat_history(
    *,
    user_concept_id: str,
    session_id: str,
    original_filename: str,
    content_type: str | None,
    size_bytes: int,
    sha256: str,
    blob_backend: str,
    blob_key: str,
    blob_uri: str,
    file_copy_concept_id: str,
) -> bool:
    """Persist a durable upload record in chat history.

    We store human-readable text plus a compact JSON payload. This ensures the
    attachment can be rediscovered later (including the blob key/URI), assuming
    the user is authorised.
    """

    try:
        import json

        upload_summary = f"[UPLOAD] {original_filename} ({size_bytes} bytes)"
        assistant_lines: list[str] = [
            f"Attachment uploaded: {original_filename}",
            f"File copy concept: {file_copy_concept_id}",
            f"Blob URI: {blob_uri}",
            "(Blob access is subject to authorisation.)",
        ]

        payload = {
            "kind": "file_upload",
            "original_filename": original_filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "file_copy_concept_id": file_copy_concept_id,
            "blob": {
                "backend": blob_backend,
                "key": blob_key,
                "uri": blob_uri,
            },
        }

        assistant_text = (
            "\n".join(assistant_lines)
            + "\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )

        chat_history_service.add_message_to_history(
            user_concept_id,
            session_id,
            {"role": "user", "content": upload_summary},
        )
        chat_history_service.add_message_to_history(
            user_concept_id,
            session_id,
            {"role": "assistant", "content": assistant_text},
        )
        return True
    except Exception as exc:
        current_app.logger.warning(
            "[files/upload] Failed to record upload in chat history: %s", exc
        )
        return False


@von_bp.route("/api/files/<path:file_copy_concept_id>/download", methods=["GET"])
def download_file_copy(file_copy_concept_id: str):
    """Download an uploaded file-copy by its Vontology concept id.

    Security: user identity is derived server-side via get_effective_user_concept_id().
    Access is restricted using relationships.specific_to_user on the file-copy concept.

    Path params:
      - file_copy_concept_id: URL-encoded concept id (e.g. %23V%23uploaded_file_copy_...)
    Query params:
      - concept_id: optional override (for callers that prefer query param)
    """

    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return (
            jsonify(
                {
                    "success": False,
                    "error": "missing_user_context",
                    "message": "Missing user context: establish an authenticated session first.",
                }
            ),
            401,
        )

    # Allow query-parameter override so callers don't need to place the full id in the path.
    concept_id = request.args.get("concept_id") or file_copy_concept_id
    concept_id = str(concept_id or "").strip()
    if not concept_id:
        return jsonify({"success": False, "error": "missing_concept_id"}), 400

    try:
        from ...db.repositories.concepts_repository import ConceptsRepository

        concept_doc = ConceptsRepository.find_one({"concept_id": concept_id})
    except Exception as exc:
        current_app.logger.warning("[files/download] Concept lookup failed: %s", exc)
        concept_doc = None

    if not isinstance(concept_doc, dict):
        # Avoid leaking which concept IDs exist.
        return jsonify({"success": False, "error": "not_found"}), 404

    relationships = (
        concept_doc.get("relationships") if isinstance(concept_doc, dict) else None
    )
    # Check both legacy and predicate-style specific_to_user fields
    from ...security.access_control import _get_specific_to_user_values

    specific = (
        _get_specific_to_user_values(relationships)
        if isinstance(relationships, dict)
        else []
    )
    if specific and user_concept_id.strip() not in {
        str(x).strip() for x in specific if x is not None
    }:
        # Avoid leaking which concept IDs exist.
        return jsonify({"success": False, "error": "not_found"}), 404

    from ...services.computer_file_copy_service import fetch_file_copy_bytes

    result = fetch_file_copy_bytes(
        file_copy_concept_id=concept_id,
        allow_large=True,
        logger=current_app.logger,
    )
    if not isinstance(result, dict) or result.get("success") is not True:
        error = result.get("error") if isinstance(result, dict) else "not_found"
        if error == "not_found":
            return jsonify({"success": False, "error": "not_found"}), 404
        if error == "blob_fetch_failed":
            return jsonify({"success": False, "error": "blob_fetch_failed"}), 404
        if error == "file_too_large":
            return jsonify({"success": False, "error": "file_too_large"}), 413
        return jsonify({"success": False, "error": "not_found"}), 404

    info = result.get("info")
    data_bytes = result.get("data")
    if data_bytes is None:
        return jsonify({"success": False, "error": "blob_fetch_failed"}), 404

    import io

    download_name = "download"
    if info is not None and getattr(info, "original_filename", None):
        download_name = str(info.original_filename)
    else:
        name = concept_doc.get("name") if isinstance(concept_doc, dict) else None
        if isinstance(name, str) and name.strip():
            download_name = name.strip()

    mimetype = "application/octet-stream"
    if info is not None and getattr(info, "content_type", None):
        mimetype = str(info.content_type)
    resp = send_file(
        io.BytesIO(data_bytes),
        mimetype=mimetype,
        as_attachment=True,
        download_name=download_name,
        max_age=0,
    )
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _prune_tool_progress() -> None:
    cutoff = time.time() - _TOOL_PROGRESS_TTL_SEC
    with _TOOL_PROGRESS_LOCK:
        stale_keys = [
            key
            for key, value in _TOOL_PROGRESS.items()
            if isinstance(value, dict)
            and isinstance(value.get("updated_at_epoch"), (int, float))
            and float(value["updated_at_epoch"]) < cutoff
        ]
        for key in stale_keys:
            _TOOL_PROGRESS.pop(key, None)


def _set_tool_progress(scope_key: str, request_id: str, update: dict[str, Any]) -> None:
    _prune_tool_progress()
    now_epoch = time.time()
    now_utc = _now_utc_iso()
    with _TOOL_PROGRESS_LOCK:
        key = (scope_key, request_id)
        existing = _TOOL_PROGRESS.get(key)
        if not isinstance(existing, dict):
            existing = {}

        safe_update = dict(update or {})
        status = _progress_str(safe_update.get("status")) or _progress_str(
            existing.get("status")
        )
        if not status:
            status = "thinking"

        stage = _derive_progress_stage(safe_update, existing)
        event_kind = _derive_progress_event_kind(safe_update)

        request_started_epoch = _progress_number(
            existing.get("request_started_epoch")
        ) or _progress_number(existing.get("updated_at_epoch"))
        if request_started_epoch is None:
            request_started_epoch = now_epoch

        last_activity_epoch = _progress_number(
            existing.get("last_activity_epoch")
        ) or _progress_number(existing.get("updated_at_epoch"))
        if last_activity_epoch is None:
            last_activity_epoch = request_started_epoch

        last_stage_activity_epoch = _progress_number(
            existing.get("last_stage_activity_epoch")
        )
        if last_stage_activity_epoch is None:
            last_stage_activity_epoch = last_activity_epoch
        if event_kind != "heartbeat":
            last_stage_activity_epoch = now_epoch

        elapsed_ms = int(max(0.0, (now_epoch - request_started_epoch) * 1000.0))
        idle_ms = int(max(0.0, (now_epoch - last_activity_epoch) * 1000.0))
        stage_idle_ms = int(max(0.0, (now_epoch - last_stage_activity_epoch) * 1000.0))

        sequence_no = int(_progress_number(existing.get("sequence_no")) or 0) + 1

        existing_counters_raw = existing.get("counters")
        existing_counters: dict[str, Any]
        if isinstance(existing_counters_raw, dict):
            existing_counters = dict(cast(dict[str, Any], existing_counters_raw))
        else:
            existing_counters = {}
        explicit_tokens_streamed = _progress_number(safe_update.get("tokens_streamed"))
        existing_tokens_streamed = _progress_number(
            existing_counters.get("tokens_streamed", 0)
        )
        tokens_streamed = int(
            explicit_tokens_streamed
            if explicit_tokens_streamed is not None
            else (existing_tokens_streamed or 0)
        )

        existing_tools_started = _progress_number(existing_counters.get("tools_started"))
        existing_tools_completed = _progress_number(
            existing_counters.get("tools_completed")
        )
        tools_started = int(existing_tools_started or 0)
        tools_completed = int(existing_tools_completed or 0)
        if event_kind == "tool_call_start":
            tools_started += 1
        if event_kind == "tool_call_end":
            tools_completed += 1
        explicit_done = _progress_number(safe_update.get("tool_calls_done"))
        if explicit_done is not None:
            tools_started = max(tools_started, int(explicit_done))
            tools_completed = max(tools_completed, int(explicit_done))

        merged = {**existing, **safe_update}
        merged["request_id"] = request_id
        merged["status"] = status
        merged["stage"] = stage
        merged["event_kind"] = event_kind
        merged["sequence_no"] = sequence_no
        merged["elapsed_ms"] = elapsed_ms
        merged["idle_ms"] = idle_ms
        merged["stage_idle_ms"] = stage_idle_ms
        merged["request_started_epoch"] = request_started_epoch
        merged["last_activity_epoch"] = now_epoch
        merged["last_activity_at_utc"] = now_utc
        merged["last_stage_activity_epoch"] = last_stage_activity_epoch
        if not _progress_str(merged.get("phase")):
            merged["phase"] = stage
        if not _progress_str(merged.get("phase_label")):
            merged["phase_label"] = _default_stage_label(stage)
        merged["stage_label"] = _default_stage_label(stage)

        subtask = _progress_str(merged.get("subtask"))
        if not subtask:
            subtask = _progress_str(merged.get("tool")) or _progress_str(
                merged.get("workflow_task")
            )
        merged["subtask"] = subtask

        merged["counters"] = {
            "tokens_streamed": max(0, tokens_streamed),
            "tools_started": max(0, tools_started),
            "tools_completed": max(0, tools_completed),
        }
        liveness = _derive_progress_liveness(merged, now_epoch=now_epoch)

        existing_events = merged.get("diagnostic_events")
        if not isinstance(existing_events, list):
            existing_events = []
        event_entry = {
            "sequence_no": sequence_no,
            "at_utc": now_utc,
            "status": status,
            "event_kind": event_kind,
            "stage": stage,
            "phase": _progress_str(merged.get("phase")) or stage,
            "phase_label": _progress_str(merged.get("phase_label")),
            "stage_label": _progress_str(merged.get("stage_label")),
            "subtask": subtask,
            "tool": _progress_str(merged.get("tool")),
            "workflow_task": _progress_str(merged.get("workflow_task")),
            "batch_size": _progress_number(merged.get("batch_size")),
            "result_summary": _progress_str(merged.get("result_summary")),
            "idle_ms": idle_ms,
            "elapsed_ms": elapsed_ms,
            "liveness_state": liveness.get("liveness_state"),
            "liveness_reason": liveness.get("liveness_reason"),
            "stall_detected": liveness.get("stall_detected"),
        }
        duration_ms = _progress_number(safe_update.get("duration_ms"))
        if duration_ms is not None:
            event_entry["duration_ms"] = int(max(0.0, duration_ms))
        model_name = _progress_str(safe_update.get("model"))
        if model_name:
            event_entry["model"] = model_name
        provider_name = _progress_str(safe_update.get("provider"))
        if provider_name:
            event_entry["provider"] = provider_name
        success_flag = safe_update.get("success")
        if isinstance(success_flag, bool):
            event_entry["success"] = success_flag
        if _progress_str(merged.get("error")):
            event_entry["error"] = str(merged.get("error"))
        trimmed_events = [
            *existing_events[-(_TOOL_PROGRESS_DIAGNOSTIC_EVENT_LIMIT - 1) :],
            event_entry,
        ]
        merged["diagnostic_events"] = trimmed_events
        merged["diagnostic_summary"] = {
            "request_id": request_id,
            "event_count": len(trimmed_events),
            "latest_sequence_no": sequence_no,
            "latest_stage": stage,
            "latest_status": status,
            "elapsed_ms": elapsed_ms,
            "idle_ms": idle_ms,
            "last_activity_at_utc": now_utc,
            "counters": dict(merged["counters"]),
        }

        merged.update(liveness)
        merged["updated_at"] = now_utc
        merged["updated_at_epoch"] = now_epoch
        _TOOL_PROGRESS[key] = merged


def _get_tool_progress(scope_key: str, request_id: str) -> dict[str, Any] | None:
    _prune_tool_progress()
    with _TOOL_PROGRESS_LOCK:
        value = _TOOL_PROGRESS.get((scope_key, request_id))
        return dict(value) if isinstance(value, dict) else None


def _snapshot_tool_progress_for_request(
    scope_key: str | None, request_id: str | None
) -> dict[str, Any] | None:
    scope = _progress_str(scope_key)
    req = _progress_str(request_id)
    if not scope or not req:
        return None

    raw_state = _get_tool_progress(scope, req)
    if not isinstance(raw_state, dict):
        return None
    return _serialise_tool_progress_state(raw_state)


def _clear_tool_progress(scope_key: str, request_id: str) -> None:
    with _TOOL_PROGRESS_LOCK:
        _TOOL_PROGRESS.pop((scope_key, request_id), None)


@von_bp.route("/progress/<request_id>", methods=["GET"])
def get_generation_progress(request_id: str):
    """Return the latest tool-execution progress for an in-flight generate() call."""

    if (
        not isinstance(request_id, str)
        or not request_id.strip()
        or len(request_id) > 200
    ):
        return jsonify({"error": "Invalid request_id"}), 400

    scope_key = _get_tool_progress_scope_key()
    state = _get_tool_progress(scope_key, request_id.strip())
    if not state:
        try:
            show_tool_use_progress = bool(get_show_tool_use_during_thinking())
        except Exception:
            show_tool_use_progress = False

        if not show_tool_use_progress:
            return jsonify({"status": "disabled"}), 200

        return jsonify({"status": "pending"}), 202

    return jsonify(_serialise_tool_progress_state(state)), 200


# ----------------- Background Tasks (JVNAUTOSCI-1038) -----------------

from ...services.background_task_service import background_task_registry


@von_bp.route("/api/task/status/<task_id>", methods=["GET"])
def get_task_status(task_id: str):
    """Get the status of a background task.

    Returns:
        200: Task status dict
        400: Invalid task_id
        404: Task not found
    """
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200:
        return jsonify({"error": "Invalid task_id"}), 400

    status = background_task_registry.get_task_status(task_id.strip())
    if status is None:
        return jsonify({"error": "Task not found", "task_id": task_id}), 404

    return jsonify(status.to_dict()), 200


@von_bp.route("/api/task/result/<task_id>", methods=["GET"])
def get_task_result(task_id: str):
    """Get the result of a completed background task.

    Returns:
        200: Task result (serialised OrchestratorResult or error)
        400: Invalid task_id
        404: Task not found
        409: Task not yet completed or failed
    """
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200:
        return jsonify({"error": "Invalid task_id"}), 400

    status = background_task_registry.get_task_status(task_id.strip())
    if status is None:
        return jsonify({"error": "Task not found", "task_id": task_id}), 404

    if status.status == "failed":
        return (
            jsonify(
                {
                    "error": "Task failed",
                    "task_id": task_id,
                    "detail": status.error,
                }
            ),
            409,
        )

    if status.status not in ("completed",):
        return (
            jsonify(
                {
                    "error": "Task not completed",
                    "task_id": task_id,
                    "status": status.status,
                }
            ),
            409,
        )

    # Serialise OrchestratorResult if that's what we have
    result = status.result
    if hasattr(result, "response_text"):
        # It's an OrchestratorResult
        serialised = {
            "response_text": result.response_text,
            "extra_messages": (
                list(result.extra_messages) if result.extra_messages else []
            ),
            "tool_invocations": (
                list(result.tool_invocations) if result.tool_invocations else []
            ),
        }
        if hasattr(result, "llm_calls"):
            serialised["llm_calls"] = list(result.llm_calls)
        if hasattr(result, "llm_usage"):
            serialised["llm_usage"] = result.llm_usage
        if hasattr(result, "render_plan") and isinstance(result.render_plan, dict):
            serialised["render_plan"] = dict(result.render_plan)
        return jsonify({"task_id": task_id, "result": serialised}), 200

    # Generic result
    return jsonify({"task_id": task_id, "result": result}), 200


@von_bp.route("/api/task/cancel/<task_id>", methods=["POST"])
def cancel_task(task_id: str):
    """Request cancellation of a running background task.

    Note: Actual cancellation depends on the task implementation cooperating.

    Returns:
        200: Cancellation requested
        400: Invalid task_id
        404: Task not found or already completed
    """
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200:
        return jsonify({"error": "Invalid task_id"}), 400

    success = background_task_registry.request_cancellation(task_id.strip())
    if not success:
        return (
            jsonify(
                {
                    "error": "Cannot cancel task",
                    "task_id": task_id,
                    "detail": "Task not found or already completed",
                }
            ),
            404,
        )

    return (
        jsonify(
            {"success": True, "task_id": task_id, "message": "Cancellation requested"}
        ),
        200,
    )


@von_bp.route("/api/tasks", methods=["GET"])
def list_tasks():
    """List background tasks.

    Query params:
        status: Filter by status (pending, running, completed, failed, cancelled)
        session_id: Filter by session ID (Phase 4)
        user_id: Filter by user ID (Phase 4)

    Returns:
        200: List of task status dicts
    """
    status_filter = request.args.get("status")
    if status_filter and status_filter not in (
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
    ):
        return jsonify({"error": "Invalid status filter"}), 400

    session_id = request.args.get("session_id")
    user_id = request.args.get("user_id")

    tasks = background_task_registry.list_tasks(
        status_filter=status_filter,
        session_id=session_id,
        user_id=user_id,
    )
    return jsonify({"tasks": tasks}), 200


# ----------------- End Background Tasks -----------------


def _truncate_large_tool_results(
    messages: list[dict], max_tool_content_chars: int = 5000
) -> list[dict]:
    """
    Truncate large tool result content to prevent context explosion.

    Tool results from MCP can be very large (e.g., search results with hierarchies).
    This function limits the size of 'tool' role messages to prevent exponential
    token growth in the conversation context.

    Args:
        messages: List of message dictionaries
        max_tool_content_chars: Maximum characters to keep in tool message content

    Returns:
        New list with truncated tool messages
    """
    result = []
    for msg in messages:
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > max_tool_content_chars:
                # Truncate and add indicator
                truncated_content = (
                    content[:max_tool_content_chars]
                    + f"\n... [truncated {len(content) - max_tool_content_chars} chars]"
                )
                result.append({**msg, "content": truncated_content})
            else:
                result.append(msg)
        else:
            result.append(msg)
    return result


def _truncate_debug_payload(raw: str | None, max_chars: int = 4000) -> str | None:
    """Limit rejected tool payloads before surfacing them in LLM debug info."""
    if raw is None:
        return None

    payload = raw if isinstance(raw, str) else str(raw)
    if len(payload) <= max_chars:
        return payload

    return payload[:max_chars] + f"\n... [truncated {len(payload) - max_chars} chars]"


def _limit_context_size(context: list[dict], max_messages: int = 20) -> list[dict]:
    """
    Keep only the most recent messages in context to prevent unbounded growth.

    Always preserves the system message (if present at index 0) and keeps the
    most recent user/assistant exchanges.

    Args:
        context: List of message dictionaries
        max_messages: Maximum number of messages to keep (excluding system message)

    Returns:
        Trimmed context list
    """
    if len(context) <= max_messages:
        return context

    # Check if first message is system message
    if context and context[0].get("role") == "system":
        system_msg = [context[0]]
        recent_msgs = context[-(max_messages - 1) :]  # Keep room for system message
        return system_msg + recent_msgs
    else:
        return context[-max_messages:]


def _calculate_context_stats(messages: list[dict]) -> dict:
    """
    Calculate statistics about message context for debugging.

    Args:
        messages: List of message dictionaries

    Returns:
        Dictionary with context statistics
    """
    stats = {
        "total_messages": len(messages),
        "by_role": {},
        "total_chars": 0,
        "largest_message": {"role": None, "chars": 0},
    }

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        content_len = len(str(content))

        # Count by role
        stats["by_role"][role] = stats["by_role"].get(role, 0) + 1

        # Total characters
        stats["total_chars"] += content_len

        # Track largest message
        if content_len > stats["largest_message"]["chars"]:
            stats["largest_message"] = {"role": role, "chars": content_len}

    return stats


def _calculate_tool_stats(tool_messages: list[dict]) -> dict:
    """
    Calculate statistics about MCP tool results.

    Args:
        tool_messages: List of tool message dictionaries

    Returns:
        Dictionary with tool result statistics
    """
    stats = {
        "tool_count": len(tool_messages),
        "total_chars": 0,
        "truncated_count": 0,
        "tools": [],
    }

    for msg in tool_messages:
        if msg.get("role") != "tool":
            continue

        content = msg.get("content", "")
        content_str = str(content)
        content_len = len(content_str)

        stats["total_chars"] += content_len

        # Check if truncated
        was_truncated = "[truncated" in content_str
        if was_truncated:
            stats["truncated_count"] += 1

        # Try to parse tool name from content
        tool_name = "unknown"
        try:
            import json

            parsed = json.loads(
                content_str.split("[truncated")[0] if was_truncated else content_str
            )
            if isinstance(parsed, dict):
                tool_name = parsed.get("tool", "unknown")
        except:
            pass

        stats["tools"].append(
            {"name": tool_name, "chars": content_len, "truncated": was_truncated}
        )

    return stats


def _derive_llm_debug_warnings(debug_info: dict) -> list[str]:
    """
    Derive warnings from LLM debug information.

    Mirrors the frontend deriveLlmDebugWarnings logic to ensure backend
    warnings are persisted in the JSON structure.

    Args:
        debug_info: The llm_debug_info dictionary

    Returns:
        List of warning strings
    """
    warnings = []

    if not debug_info or not isinstance(debug_info, dict):
        return warnings

    # Check for backend errors
    if isinstance(debug_info.get("error"), str) and debug_info.get("error", "").strip():
        warnings.append(f"Backend error: {debug_info['error'].strip()}")

    # Check auxiliary LLM calls for warnings
    aux_calls = debug_info.get("aux_llm_calls", [])
    if isinstance(aux_calls, list):
        for call in aux_calls:
            if not isinstance(call, dict):
                continue

            call_type = call.get("type", "")

            # Check missing tool-call classifier warnings
            if call_type == "missing_tool_call_classifier":
                injection_mode = call.get("prompt_injection_mode", "")
                if injection_mode == "append":
                    warnings.append(
                        "Missing tool-call detector prompt did not include `{response}` placeholder; response was appended."
                    )

                verdict = str(call.get("response_preview", "")).strip().lower()
                if verdict and not verdict.startswith(("yes", "no")):
                    warnings.append(
                        "Missing tool-call classifier returned an unexpected verdict (not yes/no)."
                    )

                model_raw = call.get("model_raw", "")
                model_resolved = call.get("model_resolved", "")
                if (
                    isinstance(model_raw, str)
                    and model_raw.startswith("#V#")
                    and not model_resolved
                ):
                    warnings.append(
                        "Missing tool-call classifier model could not be resolved from ontology ID."
                    )

            # Check for call-level errors
            if isinstance(call.get("error"), str) and call.get("error", "").strip():
                warnings.append(call["error"].strip())

    # Tool invocation failures / parse errors
    tool_invocations = debug_info.get("tool_invocations", [])
    if isinstance(tool_invocations, list):
        for inv in tool_invocations:
            if not isinstance(inv, dict):
                continue
            method = inv.get("method")
            if not isinstance(method, str):
                method = (
                    inv.get("tool") if isinstance(inv.get("tool"), str) else "unknown"
                )
            error = inv.get("error")
            error_text = error.strip() if isinstance(error, str) else ""
            if not error_text:
                continue

            # Surface parse errors directly so it is obvious tools were not executed.
            if method == "__tool_call_parse_error__":
                warnings.append(error_text)
            else:
                warnings.append(f"Tool {method} failed: {error_text}")

    # Max tool invocation cap reached (LLM still wants tools)
    try:
        internal_mcp = debug_info.get("internal_mcp")
        caps = (
            internal_mcp.get("execution_caps")
            if isinstance(internal_mcp, dict)
            else None
        )
        max_invocations = (
            caps.get("max_tool_invocations") if isinstance(caps, dict) else None
        )
        max_invocations = int(max_invocations) if max_invocations is not None else None
    except Exception:
        max_invocations = None

    response_text = debug_info.get("response")
    if (
        isinstance(response_text, str)
        and isinstance(max_invocations, int)
        and max_invocations > 0
        and isinstance(tool_invocations, list)
        and len(tool_invocations) >= max_invocations
    ):
        trimmed = response_text.strip()
        looks_like_tool_call = (
            (trimmed.startswith("{") or trimmed.startswith("["))
            and '"call_tool"' in trimmed
            and '"tool"' in trimmed
        )
        if looks_like_tool_call:
            warnings.append(
                f"Reached max tool invocation limit ({max_invocations}); additional tool calls were not executed."
            )

    # Check presenter channel health (screen/spoken routes)
    presenter_channels = debug_info.get("presenter_channels")
    if isinstance(presenter_channels, dict):
        screen_value = presenter_channels.get("screen")
        spoken_value = presenter_channels.get("spoken")
        screen_ok = isinstance(screen_value, str) and bool(screen_value.strip())
        spoken_ok = isinstance(spoken_value, str) and bool(spoken_value.strip())

        # If one channel is missing, flag it so the other route output acts as a
        # diagnostic cue (without mutating the actual output text).
        if screen_ok and not spoken_ok:
            warnings.append(
                "Presenter output missing spoken channel; text-to-speech will fall back to screen text."
            )
        elif spoken_ok and not screen_ok:
            warnings.append(
                "Presenter output missing screen channel; display will fall back to spoken text."
            )
        elif not screen_ok and not spoken_ok:
            warnings.append(
                "Presenter output present but both screen and spoken channels are empty."
            )

    spoken_backfill_attempted = bool(
        debug_info.get("spoken_backfill_second_pass_attempted")
    )
    spoken_backfill_reason = debug_info.get("spoken_backfill_second_pass_reason")
    if spoken_backfill_attempted:
        # Flag only if spoken is still missing after backfill attempt.
        spoken_still_missing = True
        if isinstance(presenter_channels, dict):
            spoken_value = presenter_channels.get("spoken")
            spoken_still_missing = not (
                isinstance(spoken_value, str) and bool(spoken_value.strip())
            )

        if spoken_still_missing:
            reason_text = (
                str(spoken_backfill_reason).strip()
                if isinstance(spoken_backfill_reason, str)
                and spoken_backfill_reason.strip()
                else "unknown_reason"
            )
            warnings.append(
                f"Spoken backfill attempted but spoken channel is still missing ({reason_text})."
            )

    display_elements = debug_info.get("display_elements")
    if isinstance(display_elements, dict):
        validation = display_elements.get("validation")
        if isinstance(validation, dict) and validation.get("valid") is False:
            warnings.append("Display element contract validation failed.")
            errors = validation.get("errors")
            if isinstance(errors, list):
                for error in errors:
                    if isinstance(error, str) and error.strip():
                        warnings.append(f"Display element validation error: {error.strip()}")

    # Remove duplicates while preserving order
    seen = set()
    unique_warnings = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            unique_warnings.append(w)

    return unique_warnings


def _normalise_workflow_routing_payload(
    workflow_routing: Any,
) -> dict[str, Any] | None:
    if workflow_routing is None:
        return None
    if isinstance(workflow_routing, Mapping):
        return {
            str(key): value
            for key, value in workflow_routing.items()
            if isinstance(key, str)
        }
    if is_dataclass(workflow_routing) and not isinstance(workflow_routing, type):
        try:
            payload = asdict(workflow_routing)
            if isinstance(payload, dict):
                return {
                    str(key): value
                    for key, value in payload.items()
                    if isinstance(key, str)
                }
        except Exception:
            return None
    raw_dict = getattr(workflow_routing, "__dict__", None)
    if isinstance(raw_dict, dict):
        return {
            str(key): value
            for key, value in raw_dict.items()
            if isinstance(key, str)
        }
    return None


def _latest_turn_completion_gate(aux_calls: Any) -> dict[str, Any] | None:
    if not isinstance(aux_calls, list):
        return None

    for entry in reversed(aux_calls):
        if not isinstance(entry, Mapping):
            continue
        call_type = entry.get("type")
        if not isinstance(call_type, str) or call_type.strip() != "turn_completion_gate":
            continue

        decision = _progress_str(entry.get("decision"))
        decision_reason = _progress_str(entry.get("decision_reason"))
        requires_follow_up = bool(entry.get("requires_follow_up", False))
        safe_to_claim_completion = bool(
            entry.get("safe_to_claim_completion", not requires_follow_up)
        )
        blocking_effect_ids_raw = entry.get("blocking_effect_ids")
        blocking_effect_ids: list[str] = []
        if isinstance(blocking_effect_ids_raw, list):
            for item in blocking_effect_ids_raw:
                effect_id = _progress_str(item)
                if effect_id:
                    blocking_effect_ids.append(effect_id)

        return {
            "decision": decision,
            "decision_reason": decision_reason,
            "requires_follow_up": requires_follow_up,
            "safe_to_claim_completion": safe_to_claim_completion,
            "blocking_effect_ids": blocking_effect_ids,
        }

    return None


def _build_terminal_tool_progress_payload(
    *, request_id: str, aux_calls: Any
) -> dict[str, Any]:
    completion_gate = _latest_turn_completion_gate(aux_calls)
    requires_follow_up = bool(
        completion_gate.get("requires_follow_up", False)
        if isinstance(completion_gate, dict)
        else False
    )
    safe_to_claim_completion = bool(
        completion_gate.get("safe_to_claim_completion", not requires_follow_up)
        if isinstance(completion_gate, dict)
        else True
    )
    progress_success = bool(safe_to_claim_completion and not requires_follow_up)

    payload: dict[str, Any] = {
        "status": "completed",
        "stage": "completed",
        "phase_label": "Complete",
        "request_id": request_id,
        "success": progress_success,
        "completion_gate_requires_follow_up": requires_follow_up,
        "completion_gate_safe_to_claim_completion": safe_to_claim_completion,
        "orchestrator_status": (
            "completed" if progress_success else "follow_up_required"
        ),
    }
    if isinstance(completion_gate, dict):
        payload["completion_gate_decision"] = completion_gate.get("decision")
        payload["completion_gate_decision_reason"] = completion_gate.get("decision_reason")
        blocking_effect_ids = completion_gate.get("blocking_effect_ids")
        if isinstance(blocking_effect_ids, list):
            payload["completion_gate_blocking_effect_ids"] = list(blocking_effect_ids)
    return payload


def _finalise_llm_debug_info(
    *,
    llm_debug_info: dict[str, Any],
    prompt_text: str | None,
    response_text: str | None,
    session_id: str | None,
    namespace: str | None,
    actor_concept_id: str | None = None,
    user_id: str | None,
    org_id: str | None,
    workflow_discovery: dict[str, Any] | None = None,
    workflow_routing: Any = None,
) -> dict[str, Any]:
    if not isinstance(llm_debug_info, dict):
        return llm_debug_info

    workflow_discovery_payload = workflow_discovery
    if workflow_discovery_payload is None:
        raw_discovery = llm_debug_info.get("workflow_discovery")
        if isinstance(raw_discovery, dict):
            workflow_discovery_payload = dict(raw_discovery)

    workflow_routing_payload = _normalise_workflow_routing_payload(workflow_routing)
    if workflow_routing_payload is None:
        raw_routing = llm_debug_info.get("workflow_routing")
        workflow_routing_payload = _normalise_workflow_routing_payload(raw_routing)

    resolved_actor_concept_id = actor_concept_id
    if not isinstance(resolved_actor_concept_id, str) or not resolved_actor_concept_id.strip():
        raw_actor_concept_id = llm_debug_info.get("actor_concept_id")
        if isinstance(raw_actor_concept_id, str) and raw_actor_concept_id.strip():
            resolved_actor_concept_id = raw_actor_concept_id.strip()
        elif isinstance(namespace, str) and namespace.strip():
            resolved_actor_concept_id = namespace.strip()
        else:
            resolved_actor_concept_id = None
    if isinstance(resolved_actor_concept_id, str) and resolved_actor_concept_id.strip():
        llm_debug_info["actor_concept_id"] = resolved_actor_concept_id

    try:
        turn_execution_record = build_turn_execution_record(
            request_id=llm_debug_info.get("request_id"),
            session_id=session_id,
            namespace=namespace,
            actor_concept_id=resolved_actor_concept_id,
            user_id=user_id,
            org_id=org_id,
            prompt_text=prompt_text,
            response_text=(
                response_text
                if isinstance(response_text, str)
                else llm_debug_info.get("response")
            ),
            interaction_timestamp_utc=llm_debug_info.get("interaction_timestamp_utc"),
            workflow_discovery=workflow_discovery_payload,
            workflow_routing=workflow_routing_payload,
            tool_invocations=(
                llm_debug_info.get("tool_invocations")
                if isinstance(llm_debug_info.get("tool_invocations"), list)
                else []
            ),
            turn_execution_diagnostics=(
                llm_debug_info.get("turn_execution_diagnostics")
                if isinstance(llm_debug_info.get("turn_execution_diagnostics"), dict)
                else None
            ),
            aux_llm_calls=(
                llm_debug_info.get("aux_llm_calls")
                if isinstance(llm_debug_info.get("aux_llm_calls"), list)
                else []
            ),
        )
        llm_debug_info["turn_execution_record"] = turn_execution_record
    except Exception as exc:
        try:
            current_app.logger.warning(
                "[turn_execution_record] Failed to build turn execution record: %s",
                exc,
            )
        except Exception:
            pass

    llm_debug_info["warnings"] = _derive_llm_debug_warnings(llm_debug_info)
    return llm_debug_info


_FENCED_CODE_BLOCK_PATTERN = re.compile(
    r"```[\w+\-]*\n.*?(?:```|$)",
    flags=re.DOTALL,
)


def _strip_fenced_code_blocks(text: str) -> str:
    if not isinstance(text, str) or not text:
        return ""
    return _FENCED_CODE_BLOCK_PATTERN.sub("", text)


def _mask_fenced_code_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Replace fenced code blocks with placeholders for safe tag matching.

    This allows presenter-tag extraction to ignore tags *inside* fenced code
    while still restoring fenced content that belongs inside a valid <screen>
    block.
    """

    if not isinstance(text, str) or not text:
        return "", {}

    replacements: dict[str, str] = {}
    counter = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal counter
        token = f"__VON_FENCED_BLOCK_{counter}__"
        replacements[token] = match.group(0)
        counter += 1
        return token

    masked = _FENCED_CODE_BLOCK_PATTERN.sub(_replace, text)
    return masked, replacements


def _restore_masked_fenced_code_blocks(
    text: str,
    replacements: dict[str, str],
) -> str:
    if not isinstance(text, str) or not text:
        return ""
    restored = text
    for token, block in replacements.items():
        restored = restored.replace(token, block)
    return restored


def _extract_tagged_block(text: str, tag: str) -> str | None:
    """Extract a presenter tag value while preserving fenced code in content."""

    if not isinstance(text, str) or not text:
        return None
    if not isinstance(tag, str) or not tag:
        return None

    searchable, replacements = _mask_fenced_code_blocks(text)
    pattern = rf"<{re.escape(tag)}>\s*(.*?)\s*</{re.escape(tag)}>"
    match = re.search(pattern, searchable, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    value = match.group(1)
    if not isinstance(value, str):
        return None
    value = _restore_masked_fenced_code_blocks(value, replacements).strip()
    return value if value else None


def _extract_presenter_channels(text: str) -> dict[str, object] | None:
    """Extract presenter-style output blocks.

    Expected format (v1):
      <spoken>...talk track...</spoken>
      <screen>...what to display...</screen>

    Returns None when no tags are present.
    """

    if not isinstance(text, str) or not text:
        return None

    spoken = _extract_tagged_block(text, "spoken")
    screen = _extract_tagged_block(text, "screen")

    if spoken is None and screen is None:
        return None

    # Defaults:
    # - If only <spoken> is provided, display it on-screen too (otherwise we'd render the raw tags).
    # - If only <screen> is provided, do NOT fabricate spoken from screen; let the client fall back.
    screen_text = screen if screen is not None else spoken
    spoken_text = spoken

    if screen_text is None:
        screen_text = text.strip()

    return {
        "format": "tagged_blocks_v1",
        "extracted": True,
        "screen": screen_text,
        "spoken": spoken_text,
    }


def _presenter_tag_present(text: str, tag: str) -> bool:
    return _extract_tagged_block(text, tag) is not None


def _build_presenter_screen_from_tool_messages(
    tool_messages: list[dict],
) -> str | None:
    if not tool_messages:
        return None

    import json

    entries: list[str] = []
    index = 0
    for msg in tool_messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        index += 1
        parsed = None
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = None

        if isinstance(parsed, dict):
            tool_name = parsed.get("tool") or parsed.get("method") or "tool"
            status = parsed.get("status")
            duration_ms = parsed.get("duration_ms")
            error = parsed.get("error")
            payload = parsed.get("payload")

            lines = [f"{index}. {tool_name}"]
            if status:
                lines.append(f"Status: {status}")
            if duration_ms is not None:
                lines.append(f"Duration: {duration_ms} ms")
            if error:
                lines.append(f"Error: {error}")
            if payload is not None:
                try:
                    payload_text = json.dumps(
                        payload,
                        indent=2,
                        sort_keys=True,
                        ensure_ascii=True,
                    )
                except Exception:
                    payload_text = str(payload)
                lines.append("Payload:")
                lines.append("```json")
                lines.append(payload_text)
                lines.append("```")
            entries.append("\n".join(lines))
        else:
            raw = content.strip()
            entries.append(f"{index}. Tool result\n```\n{raw}\n```")

    if not entries:
        return None

    return "Tool results:\n\n" + "\n\n".join(entries)


def _extract_required_screen_json_fence(prompt_text: str | None) -> str | None:
    """Extract a required JSON fence requested by the user prompt.

    Handles both well-formed fenced blocks and partially rendered prompts where
    the closing code fence may have been stripped by transport/presenter layers.
    """

    if not isinstance(prompt_text, str) or not prompt_text.strip():
        return None

    text = prompt_text
    lowered = text.lower()
    if "fenced block" not in lowered and "```json" not in lowered:
        return None

    fenced_match = re.search(
        r"```json\s*\n(?P<body>[\s\S]*?)\n```",
        text,
        flags=re.IGNORECASE,
    )
    if fenced_match:
        body = str(fenced_match.group("body") or "").strip()
        if body:
            return f"```json\n{body}\n```"

    sentinel_json_match = re.search(
        r"\{\s*\"sentinel\"\s*:\s*\"[^\"]+\"\s*,\s*\"check\"\s*:\s*\"[^\"]+\"\s*\}",
        text,
        flags=re.IGNORECASE,
    )
    if sentinel_json_match:
        body = str(sentinel_json_match.group(0) or "").strip()
        if body:
            return f"```json\n{body}\n```"

    return None


def _extract_screen_table_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract table specs from renderer plan diagnostics.

    Supports both prebuilt payloads and record-set definitions that are
    converted through the canonical table payload builder.
    """
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("table", True)
    ):
        return []

    table_elements: list[dict[str, Any]] = []

    def _append_table_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    table_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            table_elements.append(dict(raw_value))

    _append_table_specs(render_plan.get("screen_table_elements"))
    _append_table_specs(render_plan.get("screen_table_payloads"))

    record_sets_raw = render_plan.get("screen_table_record_sets")
    record_sets: list[dict[str, Any]] = []
    if isinstance(record_sets_raw, list):
        for item in record_sets_raw:
            if isinstance(item, dict):
                record_sets.append(item)
    elif isinstance(record_sets_raw, dict):
        record_sets.append(record_sets_raw)

    single_record_set = render_plan.get("screen_table_record_set")
    if isinstance(single_record_set, dict):
        record_sets.append(single_record_set)

    for index, record_set in enumerate(record_sets, start=1):
        records = record_set.get("records")
        columns = record_set.get("columns")
        if not isinstance(records, list) or not isinstance(columns, list):
            continue

        page_size_raw = record_set.get("page_size")
        page_size = (
            int(page_size_raw)
            if isinstance(page_size_raw, int) and page_size_raw > 0
            else None
        )
        record_set_title = (
            str(record_set.get("title"))
            if isinstance(record_set.get("title"), str)
            else (
                str(record_set.get("label"))
                if isinstance(record_set.get("label"), str)
                else None
            )
        )

        filters = record_set.get("filters")
        payload = build_canonical_table_payload_from_records(
            records=records,
            columns=columns,
            row_id_field=(
                str(record_set.get("row_id_field"))
                if isinstance(record_set.get("row_id_field"), str)
                else None
            ),
            row_provenance_field=(
                str(record_set.get("row_provenance_field"))
                if isinstance(record_set.get("row_provenance_field"), str)
                else None
            ),
            default_sort_column_id=(
                str(record_set.get("default_sort_column_id"))
                if isinstance(record_set.get("default_sort_column_id"), str)
                else None
            ),
            default_sort_direction=(
                str(record_set.get("default_sort_direction"))
                if isinstance(record_set.get("default_sort_direction"), str)
                else "asc"
            ),
            filters=filters if isinstance(filters, list) else None,
            pagination_enabled=bool(record_set.get("pagination_enabled", True)),
            page_size=page_size,
            title=record_set_title,
        )

        spec: dict[str, Any] = {"payload": payload}
        if isinstance(record_set.get("element_id"), str):
            spec["element_id"] = str(record_set["element_id"])
        if isinstance(record_set.get("order"), int):
            spec["order"] = int(record_set["order"])
        if isinstance(record_set.get("intent"), str):
            spec["intent"] = str(record_set["intent"])
        if isinstance(record_set.get("constraints"), dict):
            spec["constraints"] = dict(record_set["constraints"])
        provenance = record_set.get("provenance")
        if isinstance(provenance, dict):
            spec["provenance"] = dict(provenance)
        else:
            spec["provenance"] = {
                "source": "render_plan_table_record_set",
                "record_set_index": index,
            }
        table_elements.append(spec)

    return table_elements


def _extract_screen_workflow_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract workflow specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("workflow_view", True)
    ):
        return []

    workflow_elements: list[dict[str, Any]] = []

    def _append_workflow_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    workflow_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            workflow_elements.append(dict(raw_value))

    _append_workflow_specs(render_plan.get("screen_workflow_elements"))
    _append_workflow_specs(render_plan.get("screen_workflow_payloads"))

    single_workflow = render_plan.get("screen_workflow_element")
    if isinstance(single_workflow, dict):
        workflow_elements.append(dict(single_workflow))

    return workflow_elements


def _extract_screen_task_view_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract task_view specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("task_view", True)
    ):
        return []

    task_view_elements: list[dict[str, Any]] = []

    def _append_task_view_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    task_view_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            task_view_elements.append(dict(raw_value))

    _append_task_view_specs(render_plan.get("screen_task_view_elements"))
    _append_task_view_specs(render_plan.get("screen_task_view_payloads"))

    single_task_view = render_plan.get("screen_task_view_element")
    if isinstance(single_task_view, dict):
        task_view_elements.append(dict(single_task_view))

    return task_view_elements


def _extract_screen_calendar_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract calendar specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("calendar_view", True)
    ):
        return []

    calendar_elements: list[dict[str, Any]] = []

    def _append_calendar_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    calendar_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            calendar_elements.append(dict(raw_value))

    _append_calendar_specs(render_plan.get("screen_calendar_elements"))
    _append_calendar_specs(render_plan.get("screen_calendar_payloads"))

    single_calendar = render_plan.get("screen_calendar_element")
    if isinstance(single_calendar, dict):
        calendar_elements.append(dict(single_calendar))

    return calendar_elements


def _extract_screen_document_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract document_view specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("document_view", True)
    ):
        return []

    document_elements: list[dict[str, Any]] = []

    def _append_document_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    document_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            document_elements.append(dict(raw_value))

    _append_document_specs(render_plan.get("screen_document_elements"))
    _append_document_specs(render_plan.get("screen_document_payloads"))

    single_document = render_plan.get("screen_document_element")
    if isinstance(single_document, dict):
        document_elements.append(dict(single_document))

    return document_elements


def _extract_screen_kanban_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract kanban_view specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("kanban_view", True)
    ):
        return []

    kanban_elements: list[dict[str, Any]] = []

    def _append_kanban_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    kanban_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            kanban_elements.append(dict(raw_value))

    _append_kanban_specs(render_plan.get("screen_kanban_elements"))
    _append_kanban_specs(render_plan.get("screen_kanban_payloads"))

    single_kanban = render_plan.get("screen_kanban_element")
    if isinstance(single_kanban, dict):
        kanban_elements.append(dict(single_kanban))

    return kanban_elements


def _extract_screen_timeline_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract timeline specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("timeline", True)
    ):
        return []

    timeline_elements: list[dict[str, Any]] = []

    def _append_timeline_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    timeline_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            timeline_elements.append(dict(raw_value))

    _append_timeline_specs(render_plan.get("screen_timeline_elements"))
    _append_timeline_specs(render_plan.get("screen_timeline_payloads"))

    single_timeline = render_plan.get("screen_timeline_element")
    if isinstance(single_timeline, dict):
        timeline_elements.append(dict(single_timeline))

    return timeline_elements


def _extract_screen_relation_graph_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract relation graph specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("relation_graph_view", True)
    ):
        return []

    relation_graph_elements: list[dict[str, Any]] = []

    def _append_relation_graph_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    relation_graph_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            relation_graph_elements.append(dict(raw_value))

    _append_relation_graph_specs(render_plan.get("screen_relation_graph_elements"))
    _append_relation_graph_specs(render_plan.get("screen_relation_graph_payloads"))

    single_relation_graph = render_plan.get("screen_relation_graph_element")
    if isinstance(single_relation_graph, dict):
        relation_graph_elements.append(dict(single_relation_graph))

    return relation_graph_elements


def _extract_screen_relation_truth_state_elements_from_render_plan(
    render_plan: dict[str, Any] | None,
    *,
    screen_element_targets: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Extract display-contract relation truth-state specs from renderer plan diagnostics."""
    if not isinstance(render_plan, dict):
        return []
    if isinstance(screen_element_targets, dict) and not bool(
        screen_element_targets.get("relation_truth_state", True)
    ):
        return []

    relation_truth_state_elements: list[dict[str, Any]] = []

    def _append_relation_truth_state_specs(raw_value: Any) -> None:
        if isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    relation_truth_state_elements.append(dict(item))
        elif isinstance(raw_value, dict):
            relation_truth_state_elements.append(dict(raw_value))

    _append_relation_truth_state_specs(
        render_plan.get("screen_relation_truth_state_elements")
    )
    _append_relation_truth_state_specs(
        render_plan.get("screen_relation_truth_state_payloads")
    )

    single_relation_truth_state = render_plan.get("screen_relation_truth_state_element")
    if isinstance(single_relation_truth_state, dict):
        relation_truth_state_elements.append(dict(single_relation_truth_state))

    return relation_truth_state_elements


def _extract_screen_element_targets_from_render_plan(
    render_plan: dict[str, Any] | None,
) -> dict[str, bool]:
    """Resolve render-plan screen element targets with legacy-compatible defaults."""
    default_targets: dict[str, bool] = {
        "table": True,
        "workflow_view": True,
        "task_view": True,
        "calendar_view": True,
        "document_view": True,
        "kanban_view": True,
        "timeline": True,
        "relation_graph_view": True,
        "relation_truth_state": True,
    }
    if not isinstance(render_plan, dict):
        return default_targets

    raw_targets = render_plan.get("screen_element_targets")
    if not isinstance(raw_targets, dict):
        return default_targets

    return {
        "table": bool(raw_targets.get("table", True)),
        "workflow_view": bool(raw_targets.get("workflow_view", True)),
        "task_view": bool(raw_targets.get("task_view", True)),
        "calendar_view": bool(raw_targets.get("calendar_view", True)),
        "document_view": bool(raw_targets.get("document_view", True)),
        "kanban_view": bool(raw_targets.get("kanban_view", True)),
        "timeline": bool(raw_targets.get("timeline", True)),
        "relation_graph_view": bool(
            raw_targets.get("relation_graph_view", True)
        ),
        "relation_truth_state": bool(
            raw_targets.get("relation_truth_state", True)
        ),
    }


def _extract_screen_element_reason_codes_from_render_plan(
    render_plan: dict[str, Any] | None,
) -> list[str]:
    """Return renderer mapping diagnostics for display element contract reason codes."""
    if not isinstance(render_plan, dict):
        return []

    raw_reason_codes = render_plan.get("screen_element_reason_codes")
    if not isinstance(raw_reason_codes, list):
        return []

    reason_codes: list[str] = []
    for raw_reason_code in raw_reason_codes:
        if not isinstance(raw_reason_code, str):
            continue
        reason_code = raw_reason_code.strip()
        if not reason_code or reason_code in reason_codes:
            continue
        reason_codes.append(reason_code)
    return reason_codes


def _ensure_required_screen_json_fence(
    screen_text: str | None,
    required_fence: str | None,
) -> str | None:
    """Append a required JSON fence when the screen output is missing it."""

    if not isinstance(required_fence, str) or not required_fence.strip():
        return screen_text

    required_value = required_fence.strip()
    base = screen_text.strip() if isinstance(screen_text, str) else ""
    if required_value in base:
        return base
    if not base:
        return required_value
    return f"{base}\n\n{required_value}"


def _extract_screen_only(text: str) -> str | None:
    return _extract_tagged_block(text, "screen")


def _build_presenter_screen_summary_from_tool_messages(
    tool_messages: list[dict],
) -> str | None:
    """Deterministic, user-facing summary of tool activity.

    This deliberately avoids embedding raw JSON payloads so it can safely appear
    in the main chat transcript.
    """

    if not tool_messages:
        return None

    import json

    lines: list[str] = []
    lines.append("Tools ran to answer this request:")

    description_write_seen = False
    relationship_write_seen = False
    names_write_seen = False
    concept_create_seen = False

    index = 0
    for msg in tool_messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        parsed = None
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = None

        if not isinstance(parsed, dict):
            index += 1
            lines.append(f"{index}. Tool result (unstructured)")
            continue

        tool_name = parsed.get("tool") or parsed.get("method") or "tool"
        status = parsed.get("status")
        error = parsed.get("error")
        payload = parsed.get("payload")

        tool_name_text = tool_name.strip() if isinstance(tool_name, str) else ""
        tool_name_lower = tool_name_text.lower()
        payload_dict = payload if isinstance(payload, dict) else {}

        if "description" in tool_name_lower:
            description_write_seen = True
        predicate = payload_dict.get("predicate")
        if isinstance(predicate, str) and predicate.strip() in {
            "hasDescription",
            "has_description",
            "#V#hasDescription",
        }:
            description_write_seen = True
        description_value = payload_dict.get("description")
        if isinstance(description_value, str) and description_value.strip():
            description_write_seen = True

        if tool_name_lower in {"add_relationship", "remove_relationship"}:
            relationship_write_seen = True
        if tool_name_lower in {"add_names", "add_name"}:
            names_write_seen = True
        if tool_name_lower in {"create_concepts", "create_concept"}:
            concept_create_seen = True

        index += 1
        entry = f"{index}. {tool_name}"
        if status:
            entry += f" — {status}"
        lines.append(entry)
        if error:
            lines.append(f"   Error: {error}")

        # Pull out a few common, safe identifiers to help the user.
        if isinstance(payload, dict):
            for key in ("concept_id", "identifier", "id", "url", "arxiv_id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    lines.append(f"   {key}: {value.strip()}")

    if index == 0:
        return None

    # Always include an explicit writes ledger to make negative facts visible.
    lines.append("")
    lines.append("Writes ledger (authoritative):")
    if description_write_seen:
        lines.append("- Description updated: YES (evidence present in tool results)")
    else:
        lines.append("- Description updated: NO (no description write tool ran)")

    did_not_lines: list[str] = []
    if not relationship_write_seen:
        did_not_lines.append("- No relationship writes detected")
    if not names_write_seen:
        did_not_lines.append("- No name writes detected")
    if not concept_create_seen:
        did_not_lines.append("- No concept creation detected")

    if did_not_lines:
        lines.append("")
        lines.append("Writes not detected:")
        lines.extend(did_not_lines)

    return "\n".join(lines).strip() or None


def _build_tool_messages_prompt_blob(
    tool_messages: list[dict], *, max_chars: int = 12000
) -> str:
    """Build a compact plain-text representation of tool results for LLM backfill."""

    if not tool_messages:
        return "(no tool messages)"

    import json

    def _normalise_tool_name(parsed: dict) -> str:
        tool_name = (
            parsed.get("tool") or parsed.get("method") or parsed.get("name") or ""
        )
        return tool_name.strip() if isinstance(tool_name, str) else ""

    def _iter_parsed_tool_results(messages: list[dict]) -> list[dict]:
        parsed_results: list[dict] = []
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            try:
                parsed = json.loads(content)
            except Exception:
                continue
            if isinstance(parsed, dict):
                parsed_results.append(parsed)
        return parsed_results

    parsed_results = _iter_parsed_tool_results(tool_messages)

    executed_lines: list[str] = []
    writes_lines: list[str] = []

    # Track a small set of write categories we care about for UI truthfulness.
    description_write_seen = False
    relationship_write_seen = False
    names_write_seen = False
    concept_create_seen = False

    def _mark_description_write(tool_name: str, payload: dict) -> None:
        nonlocal description_write_seen
        if description_write_seen:
            return
        tool_name_lower = (tool_name or "").lower()
        if "description" in tool_name_lower:
            description_write_seen = True
            return
        predicate = payload.get("predicate")
        if isinstance(predicate, str) and predicate.strip() in {
            "hasDescription",
            "has_description",
            "#V#hasDescription",
        }:
            description_write_seen = True
            return
        description_value = payload.get("description")
        if isinstance(description_value, str) and description_value.strip():
            description_write_seen = True

    def _summarise_relationship_write(tool_name: str, payload: dict) -> str | None:
        nonlocal relationship_write_seen
        name_lower = (tool_name or "").lower()
        if name_lower not in {"add_relationship", "remove_relationship"}:
            return None

        source_id = payload.get("source_id")
        predicate = payload.get("predicate")
        target = payload.get("target")
        added = payload.get("added")
        removed = payload.get("removed")

        if not (
            isinstance(source_id, str)
            and isinstance(predicate, str)
            and isinstance(target, str)
        ):
            relationship_write_seen = True
            return f"- Relationship update via {tool_name} (details unavailable)"

        relationship_write_seen = True

        verb = "changed"
        if name_lower == "add_relationship":
            verb = "added" if added is not False else "attempted"
        elif name_lower == "remove_relationship":
            verb = "removed" if removed is not False else "attempted"

        return f"- Relationship {verb}: `{source_id}` — `{predicate}` → `{target}`"

    def _summarise_name_or_concept_write(tool_name: str, payload: dict) -> str | None:
        nonlocal names_write_seen, concept_create_seen
        name_lower = (tool_name or "").lower()
        if name_lower in {"add_names", "add_name"}:
            names_write_seen = True
            concept_id = payload.get("concept_id")
            if isinstance(concept_id, str) and concept_id.strip():
                return f"- Names added for `{concept_id}`"
            return "- Names added"

        if name_lower in {"create_concepts", "create_concept"}:
            concept_create_seen = True
            total = payload.get("total")
            if isinstance(total, int):
                return f"- Concepts created: {total}"
            return "- Concept creation attempted"

        return None

    for parsed in parsed_results:
        tool_name = _normalise_tool_name(parsed) or "tool"
        status = parsed.get("status")
        status_text = (
            status.strip() if isinstance(status, str) and status.strip() else None
        )
        executed_lines.append(
            f"- {tool_name}" + (f" ({status_text})" if status_text else "")
        )

        payload = parsed.get("payload")
        payload = payload if isinstance(payload, dict) else {}

        # Categorise writes.
        rel_summary = _summarise_relationship_write(tool_name, payload)
        if rel_summary:
            writes_lines.append(rel_summary)

        name_or_concept_summary = _summarise_name_or_concept_write(tool_name, payload)
        if name_or_concept_summary:
            writes_lines.append(name_or_concept_summary)

        _mark_description_write(tool_name, payload)

    # Always include an explicit description verdict because it is a common source of confusion.
    if description_write_seen:
        writes_lines.append(
            "- Description updated: YES (evidence present in tool results)"
        )
    else:
        writes_lines.append("- Description updated: NO (no description write tool ran)")

    # Provide a small "did not happen" block to make negative facts explicit.
    did_not_lines: list[str] = []
    if not relationship_write_seen:
        did_not_lines.append("- No relationship writes detected")
    if not names_write_seen:
        did_not_lines.append("- No name writes detected")
    if not concept_create_seen:
        did_not_lines.append("- No concept creation detected")

    blob_lines: list[str] = []
    blob_lines.append("TOOL EXECUTION (authoritative):")
    blob_lines.extend(executed_lines or ["- (no parsed tool results)"])
    blob_lines.append("")
    blob_lines.append("TOOL WRITES LEDGER (authoritative):")
    blob_lines.extend(writes_lines or ["- No writes detected"])
    if did_not_lines:
        blob_lines.append("")
        blob_lines.append("WRITES NOT DETECTED:")
        blob_lines.extend(did_not_lines)

    blob = "\n".join(blob_lines).strip() or "(tool results unavailable)"

    blob = str(blob)
    if len(blob) > max_chars:
        blob = blob[:max_chars].rstrip() + "\n... [truncated]"
    return blob


def _is_prompt_introspection_question(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    triggers = (
        "what is my user prompt",
        "what's my user prompt",
        "what is my system prompt",
        "what's my system prompt",
        "what prompt is active",
        "which prompt is active",
        "tell me what my user prompt is",
        "tell me what my system prompt is",
    )
    return any(t in lowered for t in triggers)


def _is_tool_introspection_question(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    triggers = (
        "what tools do you have",
        "what tools can you",
        "what tools can i",
        "list tools",
        "show tools",
        "available tools",
        "tool list",
        "mcp tools",
        "what can you do",
        "what capabilities do you have",
    )
    return any(t in lowered for t in triggers)


def _is_rag_status_question(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    triggers = (
        "rag status",
        "what is my rag status",
        "is rag enabled",
        "is rag on",
        "rag enabled",
        "rag on",
        "rag working",
        "rag isolation",
    )
    return any(t in lowered for t in triggers)


def _format_tool_inventory(methods: dict) -> str:
    # Deterministic plain-text listing; keep stable ordering.
    if not isinstance(methods, dict) or not methods:
        return "No internal MCP tools are registered."

    # Group by category
    buckets: dict[str, list[tuple[str, str]]] = {}
    for name, meta in methods.items():
        if not isinstance(name, str):
            continue
        meta_dict = meta if isinstance(meta, dict) else {}

        category: str = "other"
        category_value = meta_dict.get("category")
        if isinstance(category_value, str) and category_value.strip():
            category = category_value.strip()

        desc: str = ""
        desc_value = meta_dict.get("description")
        if isinstance(desc_value, str):
            desc = desc_value.strip()

        buckets.setdefault(category, []).append((name, desc))

    lines: list[str] = []
    lines.append("Internal MCP tools currently registered:")
    for category in sorted(buckets.keys()):
        lines.append("")
        lines.append(f"- {category}:")
        for name, desc in sorted(buckets[category], key=lambda x: x[0]):
            if desc:
                lines.append(f"  - {name}: {desc}")
            else:
                lines.append(f"  - {name}")
    lines.append("")
    lines.append(
        "Note: Some tools (especially RAG) require a user namespace to avoid cross-user data leakage."
    )
    return "\n".join(lines)


def _deterministic_introspection_enabled() -> bool:
    try:
        flag_value = os.getenv("VON_DETERMINISTIC_INTROSPECTION", "0")
        return str(flag_value).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def _maybe_handle_prompt_introspection_fastpath(
    *,
    prompt_text: str,
    user_concept_id: str,
    history_user_id: str | None,
    session_id: str,
    auxiliary_system_prompt: str | None,
    user_prompt_debug: dict,
    context: list[dict],
    interaction_timestamp_utc: str,
    model_name: str,
    request_start_perf: float,
    request_id: str | None = None,
    progress_scope_key: str | None = None,
):
    gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
    import json as _json

    tool_messages: list[dict] = []
    tool_invocations: list[dict] = []

    response_text = None
    used_tool = False

    if gateway is not None and getattr(gateway, "enabled", False):
        try:
            tool_result = gateway.invoke(
                "chat_get_prompt_context",
                {
                    "namespace": user_concept_id,
                    "include_content": True,
                    "max_chars": 5000,
                },
            )
            payload = tool_result.payload
            duration_ms = getattr(tool_result, "duration_ms", None)
            used_tool = True
            tool_messages = [
                {
                    "role": "tool",
                    "content": _json.dumps(
                        {
                            "tool": "chat_get_prompt_context",
                            "status": "ok",
                            "duration_ms": duration_ms,
                            "payload": payload,
                        },
                        default=str,
                    ),
                }
            ]
            tool_invocations = [
                {
                    "tool": "chat_get_prompt_context",
                    "payload": {
                        "namespace": user_concept_id,
                        "include_content": True,
                        "max_chars": 5000,
                    },
                    "duration_ms": duration_ms,
                    "direct_user_call": False,
                }
            ]

            if isinstance(payload, dict) and payload.get("success"):
                prompt_ids = payload.get("prompt_concept_ids") or []
                prompt_text_value = payload.get("prompt_text") or ""
                if not isinstance(prompt_text_value, str):
                    prompt_text_value = str(prompt_text_value)
                prompt_text_value = prompt_text_value.strip()
                if not prompt_text_value:
                    response_text = (
                        "No user-specific system prompt text is currently available for your account. "
                        "(The prompt linkage exists but no content was returned.)"
                    )
                else:
                    response_text = (
                        "Here is your current user-specific system prompt (from Vontology).\n\n"
                        f"Prompt concept IDs: {prompt_ids}\n\n"
                        f"{prompt_text_value}"
                    )
            else:
                response_text = (
                    "I could not retrieve your user-specific prompt context via internal tools. "
                    f"Result: {payload}"
                )
        except Exception as exc:
            current_app.logger.warning(
                "[mcp_orchestrator] Prompt introspection tool failed: %s", exc
            )

    # Fallback: use already-loaded prompt fragments (no tool required)
    if response_text is None:
        prompt_ids = (
            user_prompt_debug.get("prompt_concept_ids")
            if isinstance(user_prompt_debug, dict)
            else []
        )
        if not isinstance(prompt_ids, list):
            prompt_ids = []
        prompt_text_value = auxiliary_system_prompt or ""
        prompt_text_value = (
            prompt_text_value.strip()
            if isinstance(prompt_text_value, str)
            else str(prompt_text_value)
        )
        if not prompt_text_value:
            response_text = "No user-specific system prompt is currently active (no prompt content was loaded from Vontology)."
        else:
            response_text = (
                "Here is your current user-specific system prompt (from Vontology).\n\n"
                f"Prompt concept IDs: {prompt_ids}\n\n"
                f"{prompt_text_value}"
            )

    # Store messages in history/context, matching the direct-tool-call pattern.
    if history_user_id:
        chat_history_service.add_message_to_history(
            history_user_id,
            session_id,
            {
                "role": "user",
                "content": prompt_text,
                "author_user_id": user_concept_id,
            },
        )
    for tool_msg in _truncate_large_tool_results(
        tool_messages, max_tool_content_chars=5000
    ):
        if history_user_id:
            chat_history_service.add_message_to_history(
                history_user_id, session_id, tool_msg
            )

    current_app.config["CONTEXT"] = _limit_context_size(
        current_app.config.get("CONTEXT", []), max_messages=20
    )

    current_turn_messages = [{"role": "user", "content": prompt_text}] + tool_messages
    context_stats = _calculate_context_stats(context)
    current_context_stats = _calculate_context_stats(current_app.config["CONTEXT"])
    tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None
    tool_progress_snapshot = _snapshot_tool_progress_for_request(
        progress_scope_key, request_id
    )
    turn_execution_diagnostics = _build_turn_execution_diagnostics(
        request_id=request_id,
        prompt_text=prompt_text,
        elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
        tool_progress_state=tool_progress_snapshot,
    )

    llm_debug_info = {
        "interaction_timestamp_utc": interaction_timestamp_utc,
        "request_id": request_id,
        "model": model_name,
        "llm_interaction": {
            "requested_model": model_name,
            "orchestrator_used": False,
            "duration_ms": None,
            "usage": None,
            "calls": [],
            "server_elapsed_ms": (time.perf_counter() - request_start_perf) * 1000.0,
        },
        "messages": current_turn_messages,
        "response": response_text,
        "user_prompt": user_prompt_debug,
        "context_stats": {
            "sent_to_llm": context_stats,
            "stored_context": current_context_stats,
        },
        "tool_stats": tool_stats,
        "tool_invocations": tool_invocations,
        "prompt_introspection_fastpath": {"used_tool": used_tool, "enabled": True},
        "fastpath": {
            "name": "prompt_introspection",
            "bypassed_llm": True,
            "used_tool": used_tool,
            "enabled": True,
        },
        "turn_execution_diagnostics": turn_execution_diagnostics,
    }
    llm_debug_info = _finalise_llm_debug_info(
        llm_debug_info=llm_debug_info,
        prompt_text=prompt_text,
        response_text=response_text,
        session_id=session_id,
        namespace=user_concept_id,
        user_id=history_user_id or user_concept_id,
        org_id=None,
    )

    if history_user_id:
        chat_history_service.add_message_to_history(
            history_user_id,
            session_id,
            {"role": "assistant", "content": response_text},
            llm_debug_data=llm_debug_info,
        )

    return jsonify(
        {
            "response": response_text,
            "fastpath": {
                "name": "prompt_introspection",
                "bypassed_llm": True,
                "used_tool": used_tool,
            },
            "llm_debug": llm_debug_info,
        }
    )


def _maybe_handle_tool_inventory_fastpath(
    *,
    prompt_text: str,
    user_concept_id: str | None,
    history_user_id: str | None,
    session_id: str,
    context: list[dict],
    interaction_timestamp_utc: str,
    model_name: str,
    request_start_perf: float,
    user_prompt_debug: dict,
    request_id: str | None = None,
    progress_scope_key: str | None = None,
):
    gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")

    response_text = None
    used_tool = False
    methods_snapshot = None

    if gateway is not None and getattr(gateway, "enabled", False):
        try:
            methods_snapshot = gateway.describe_methods()
            used_tool = True
            response_text = _format_tool_inventory(methods_snapshot)
        except Exception as exc:
            response_text = (
                f"Could not retrieve tool inventory from the internal gateway: {exc}"
            )
    else:
        response_text = ()

    current_turn_messages = [{"role": "user", "content": prompt_text}]
    context_stats = _calculate_context_stats(context)
    current_context_stats = _calculate_context_stats(
        current_app.config.get("CONTEXT", [])
    )
    tool_progress_snapshot = _snapshot_tool_progress_for_request(
        progress_scope_key, request_id
    )
    turn_execution_diagnostics = _build_turn_execution_diagnostics(
        request_id=request_id,
        prompt_text=prompt_text,
        elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
        tool_progress_state=tool_progress_snapshot,
    )

    llm_debug_info = {
        "interaction_timestamp_utc": interaction_timestamp_utc,
        "request_id": request_id,
        "model": model_name,
        "llm_interaction": {
            "requested_model": model_name,
            "orchestrator_used": False,
            "duration_ms": None,
            "usage": None,
            "calls": [],
            "server_elapsed_ms": (time.perf_counter() - request_start_perf) * 1000.0,
        },
        "messages": current_turn_messages,
        "response": response_text,
        "user_prompt": user_prompt_debug,
        "context_stats": {
            "sent_to_llm": context_stats,
            "stored_context": current_context_stats,
        },
        "tool_stats": None,
        "tool_invocations": (
            [
                {
                    "tool": "gateway.describe_methods",
                    "payload": {},
                    "duration_ms": None,
                    "direct_user_call": False,
                    "ok": bool(methods_snapshot is not None),
                }
            ]
            if used_tool
            else []
        ),
        "fastpath": {
            "name": "tool_inventory",
            "bypassed_llm": True,
            "used_tool": used_tool,
            "enabled": True,
        },
        "turn_execution_diagnostics": turn_execution_diagnostics,
    }
    llm_debug_info = _finalise_llm_debug_info(
        llm_debug_info=llm_debug_info,
        prompt_text=prompt_text,
        response_text=(
            response_text if isinstance(response_text, str) else str(response_text)
        ),
        session_id=session_id,
        namespace=user_concept_id,
        user_id=history_user_id or user_concept_id,
        org_id=None,
    )

    if history_user_id:
        chat_history_service.add_message_to_history(
            history_user_id,
            session_id,
            {
                "role": "user",
                "content": prompt_text,
                "author_user_id": user_concept_id,
            },
        )
        chat_history_service.add_message_to_history(
            history_user_id,
            session_id,
            {"role": "assistant", "content": response_text},
            llm_debug_data=llm_debug_info,
        )
    else:
        stored_context = current_app.config.get("CONTEXT", [])
        stored_context.append({"role": "user", "content": prompt_text})
        stored_context.append({"role": "assistant", "content": response_text})
        current_app.config["CONTEXT"] = _limit_context_size(
            stored_context, max_messages=20
        )

    current_app.config["CONTEXT"] = _limit_context_size(
        current_app.config.get("CONTEXT", []), max_messages=20
    )

    return jsonify(
        {
            "response": response_text,
            "fastpath": {
                "name": "tool_inventory",
                "bypassed_llm": True,
                "used_tool": used_tool,
            },
            "llm_debug": llm_debug_info,
        }
    )


def _maybe_handle_rag_status_fastpath(
    *,
    prompt_text: str,
    user_concept_id: str,
    history_user_id: str | None,
    session_id: str,
    context: list[dict],
    interaction_timestamp_utc: str,
    model_name: str,
    request_start_perf: float,
    user_prompt_debug: dict,
    request_id: str | None = None,
    progress_scope_key: str | None = None,
):
    gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
    import json as _json

    tool_messages: list[dict] = []
    tool_invocations: list[dict] = []
    used_tool = False
    response_text = None

    if gateway is not None and getattr(gateway, "enabled", False):
        try:
            tool_result = gateway.invoke(
                "rag_get_status",
                {"namespace": user_concept_id},
            )
            payload = tool_result.payload
            duration_ms = getattr(tool_result, "duration_ms", None)
            used_tool = True

            tool_messages = [
                {
                    "role": "tool",
                    "content": _json.dumps(
                        {
                            "tool": "rag_get_status",
                            "status": "ok",
                            "duration_ms": duration_ms,
                            "payload": payload,
                        },
                        default=str,
                    ),
                }
            ]
            tool_invocations = [
                {
                    "tool": "rag_get_status",
                    "payload": {"namespace": user_concept_id},
                    "duration_ms": duration_ms,
                    "direct_user_call": False,
                }
            ]

            response_text = (
                "Here is your current RAG status (server-truth):\n\n"
                + _json.dumps(payload, indent=2, default=str)
            )
        except Exception as exc:
            response_text = f"I could not retrieve RAG status via internal tools: {exc}"
    else:
        response_text = "Internal MCP gateway is disabled; RAG status is unavailable."

    # Persist messages in history/context.
    if history_user_id:
        chat_history_service.add_message_to_history(
            history_user_id,
            session_id,
            {
                "role": "user",
                "content": prompt_text,
                "author_user_id": user_concept_id,
            },
        )
    for tool_msg in _truncate_large_tool_results(
        tool_messages, max_tool_content_chars=5000
    ):
        if history_user_id:
            chat_history_service.add_message_to_history(
                history_user_id, session_id, tool_msg
            )

    current_app.config["CONTEXT"] = _limit_context_size(
        current_app.config.get("CONTEXT", []), max_messages=20
    )

    current_turn_messages = [{"role": "user", "content": prompt_text}] + tool_messages
    context_stats = _calculate_context_stats(context)
    current_context_stats = _calculate_context_stats(current_app.config["CONTEXT"])
    tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None
    tool_progress_snapshot = _snapshot_tool_progress_for_request(
        progress_scope_key, request_id
    )
    turn_execution_diagnostics = _build_turn_execution_diagnostics(
        request_id=request_id,
        prompt_text=prompt_text,
        elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
        tool_progress_state=tool_progress_snapshot,
    )

    llm_debug_info = {
        "interaction_timestamp_utc": interaction_timestamp_utc,
        "request_id": request_id,
        "model": model_name,
        "llm_interaction": {
            "requested_model": model_name,
            "orchestrator_used": False,
            "duration_ms": None,
            "usage": None,
            "calls": [],
            "server_elapsed_ms": (time.perf_counter() - request_start_perf) * 1000.0,
        },
        "messages": current_turn_messages,
        "response": response_text,
        "user_prompt": user_prompt_debug,
        "context_stats": {
            "sent_to_llm": context_stats,
            "stored_context": current_context_stats,
        },
        "tool_stats": tool_stats,
        "tool_invocations": tool_invocations,
        "fastpath": {
            "name": "rag_status",
            "bypassed_llm": True,
            "used_tool": used_tool,
            "enabled": True,
        },
        "turn_execution_diagnostics": turn_execution_diagnostics,
    }
    llm_debug_info = _finalise_llm_debug_info(
        llm_debug_info=llm_debug_info,
        prompt_text=prompt_text,
        response_text=response_text,
        session_id=session_id,
        namespace=user_concept_id,
        user_id=history_user_id or user_concept_id,
        org_id=None,
    )

    if history_user_id:
        chat_history_service.add_message_to_history(
            history_user_id,
            session_id,
            {"role": "assistant", "content": response_text},
            llm_debug_data=llm_debug_info,
        )

    return jsonify(
        {
            "response": response_text,
            "fastpath": {
                "name": "rag_status",
                "bypassed_llm": True,
                "used_tool": used_tool,
            },
            "llm_debug": llm_debug_info,
        }
    )


@von_bp.route("/onboard_new_member", methods=["POST"])
def onboard_new_member():
    """Onboards a new lab member."""
    data = request.get_json()
    member_name = data.get("member_name")

    if not member_name:
        return jsonify({"error": "No member name provided."}), 400

    try:
        run_onboarding_workflow(member_name)
        return (
            jsonify({"message": f"Onboarding workflow started for {member_name}."}),
            200,
        )
    except Exception as e:
        print(f"Error during onboarding: {e}")  # Add server-side logging
        return jsonify({"error": f"Error during onboarding: {str(e)}"}), 500


@von_bp.route("/update_model", methods=["POST"])
def update_model():
    data = request.get_json()
    new_model = data.get("model")

    if new_model:
        current_app.config["MODEL"] = new_model
        print(f"Model updated to: {new_model}")  # Add server-side logging
        return jsonify({"message": f"Model updated to {new_model}"}), 200
    return jsonify({"error": "No model provided."}), 400


@von_bp.route("/")
def serve_page():
    """Serve the main chat interface (HTML file)."""
    try:
        # Ensure template changes (e.g., recent fixes) are picked up even in production mode
        jenv = getattr(current_app, "jinja_env", None)
        cache = getattr(jenv, "cache", None)
        clear_fn = getattr(cache, "clear", None)
        if callable(clear_fn):
            clear_fn()
    except Exception:
        pass  # Defensive: don't block page serving if cache clear fails
    return render_template("von_interface.html")


@von_bp.route("/generate", methods=["POST"])
def generate():  # pyright: ignore[reportGeneralTypeIssues]
    """Handle text generation requests."""
    data = request.get_json()
    prompt_text = data.get("prompt", "")

    client_request_id = data.get("client_request_id")
    if (
        isinstance(client_request_id, str)
        and client_request_id.strip()
        and len(client_request_id) <= 200
    ):
        request_id = client_request_id.strip()
    else:
        request_id = str(uuid.uuid4())

    # JVNAUTOSCI-1038: Background execution mode
    background_mode = bool(data.get("background", False))

    presenter_mode_requested = bool(data.get("presenter_mode"))

    request_start_perf = time.perf_counter()
    progress_heartbeat_stop_event: threading.Event | None = None
    progress_heartbeat_thread: threading.Thread | None = None

    interaction_timestamp_utc = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    # Get user/org context from request body (sent by frontend from localStorage)
    request_user_id = data.get("user_id")
    request_org_id = data.get("org_id")
    request_language = data.get("language", "en-NZ")
    request_gmail_profile_raw = data.get("gmail_profile")
    request_gmail_profile = None
    if isinstance(request_gmail_profile_raw, str):
        request_gmail_profile = request_gmail_profile_raw.strip() or None
    elif request_gmail_profile_raw is not None:
        return (
            jsonify(
                {
                    "error": "invalid_gmail_profile",
                    "detail": "gmail_profile must be a string.",
                }
            ),
            400,
        )

    if request_gmail_profile:
        from ...integrations.google.gmail_service import list_profile_ids_from_env

        available_profiles = list_profile_ids_from_env()
        if not available_profiles:
            return (
                jsonify(
                    {
                        "error": "gmail_profiles_not_configured",
                        "detail": "No Gmail profiles are configured on the server.",
                    }
                ),
                400,
            )
        if request_gmail_profile not in available_profiles:
            return (
                jsonify(
                    {
                        "error": "unknown_gmail_profile",
                        "detail": "gmail_profile is not configured on the server.",
                        "available_profiles": available_profiles,
                    }
                ),
                400,
            )

    user_concept_id = session.get("user_concept_id")
    context = current_app.config.get("CONTEXT", [])

    # REFACTORING_NOTE: Use the new factory to get the correct client and model
    # Get user and org context for per-user/org LLM settings
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()

        # SECURITY: Do NOT trust client-provided user_id - require proper authentication
        # User must be authenticated via:
        # 1. Server-side session (populated during login flow)
        # 2. Validated headers (X-User-Concept-ID with concept validation)
        # If no authenticated user, user_concept_id will be None and RAG tools will be unavailable
        if not user_concept_id:
            current_app.logger.info(
                "[AUTH] No authenticated user for this request. RAG and user-scoped tools will be unavailable. "
                "Client-provided user_id '%s' is ignored for security.",
                request_user_id or "(none)",
            )

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        org_concept_id = effective.get("organisation_id")

        # Store user_concept_id in session for history tracking
        if user_concept_id:
            session["user_concept_id"] = user_concept_id
    except Exception:
        user_concept_id = None
        org_concept_id = None
        effective = {}

    # JVNAUTOSCI-1011: Get session_id from window context (or flask session fallback).
    # This ensures each browser window uses its own active chat session.
    session_id = effective.get("chat_session_id")
    if not session_id:
        # Fallback to flask session for legacy clients
        if "session_id" not in session:
            session["session_id"] = str(uuid.uuid4())
        session_id = session["session_id"]

    history_owner_user_id, shared_invite = _resolve_shared_conversation_owner(
        user_concept_id=user_concept_id, session_id=session_id
    )
    history_user_id = history_owner_user_id or user_concept_id

    if history_user_id:
        if (
            shared_invite
            and history_owner_user_id
            and user_concept_id
            and history_owner_user_id != user_concept_id
        ):
            owner_history = chat_history_service.get_chat_history(
                history_owner_user_id, session_id
            )
            invitee_history = chat_history_service.get_chat_history(
                user_concept_id, session_id
            )
            owner_history = _apply_default_author(owner_history, history_owner_user_id)
            invitee_history = _apply_default_author(invitee_history, user_concept_id)
            context = _merge_shared_histories(owner_history, invitee_history)
        else:
            context = chat_history_service.get_chat_history(history_user_id, session_id)

    progress_scope_key = _get_tool_progress_scope_key()
    show_tool_use_progress = False
    try:
        show_tool_use_progress = bool(get_show_tool_use_during_thinking())
    except Exception:
        show_tool_use_progress = False

    if show_tool_use_progress:
        try:
            max_calls = int(get_internal_mcp_max_tool_invocations())
        except Exception:
            max_calls = 0
        try:
            batch_cap = int(get_internal_mcp_tool_batch_cap())
        except Exception:
            batch_cap = 4
        _set_tool_progress(
            progress_scope_key,
            request_id,
            {
                "status": "thinking",
                "phase": "context_build",
                "phase_label": "Building context",
                "request_id": request_id,
                "tool": None,
                "batch_size": None,
                "tool_calls_done": 0,
                "tool_calls_cap": max_calls,
                "tool_calls_remaining": max(0, max_calls),
                "tool_batch_cap": batch_cap,
            },
        )

    try:
        llm_client = get_llm_client(
            user_concept_id=user_concept_id, org_concept_id=org_concept_id
        )
        model_name = get_active_model_name()
    except Exception as e:
        return jsonify({"error": f"Could not get LLM client: {e}"}), 500

    if not prompt_text:
        return jsonify({"error": "No prompt provided."}), 400

    if show_tool_use_progress and not background_mode:
        progress_heartbeat_stop_event, progress_heartbeat_thread = (
            _start_tool_progress_heartbeat(progress_scope_key, request_id)
        )

    try:
        # Build system message with user and organization context from request
        system_message_parts = []

        # ---------------------------------------------------------
        # JVNAUTOSCI-797: user-specific system prompt from Vontology
        # ---------------------------------------------------------
        auxiliary_system_prompt = None
        narration_prompt_text = None
        narration_prompt_fragments = []
        screen_prompt_text = None
        screen_prompt_fragments = []
        user_prompt_debug = {
            "effective_user_concept_id": user_concept_id,
            "loaded": False,
            "chars": 0,
            "prompt_concept_ids": [],
            "behaviour_prompt_concept_ids": [],
            "narration_prompt_concept_ids": [],
            "screen_prompt_concept_ids": [],
        }
        if user_concept_id:
            try:
                from ...services.chat_auxiliary_prompt_service import (
                    get_user_specific_prompt_fragments,
                )

                behaviour_prompt_fragments = get_user_specific_prompt_fragments(
                    user_concept_id,
                    prompt_types=(
                        "#V#von_chat_behaviour_prompt",
                        "#V#von_chat_behavior_prompt",
                        "#V#von_llm_prompt",
                    ),
                )
                narration_prompt_fragments = get_user_specific_prompt_fragments(
                    user_concept_id,
                    prompt_types=("#V#von_chat_narration_prompt",),
                )
                screen_prompt_fragments = get_user_specific_prompt_fragments(
                    user_concept_id,
                    prompt_types=("#V#von_chat_screen_content_prompt",),
                )

                user_prompt_debug["behaviour_prompt_concept_ids"] = [
                    frag.get("concept_id")
                    for frag in behaviour_prompt_fragments
                    if isinstance(frag, dict)
                    and isinstance(frag.get("concept_id"), str)
                ]
                user_prompt_debug["narration_prompt_concept_ids"] = [
                    frag.get("concept_id")
                    for frag in narration_prompt_fragments
                    if isinstance(frag, dict)
                    and isinstance(frag.get("concept_id"), str)
                ]
                user_prompt_debug["screen_prompt_concept_ids"] = [
                    frag.get("concept_id")
                    for frag in screen_prompt_fragments
                    if isinstance(frag, dict)
                    and isinstance(frag.get("concept_id"), str)
                ]
                # Backwards-compatible field name used by the UI/debug tools.
                user_prompt_debug["prompt_concept_ids"] = list(
                    user_prompt_debug["behaviour_prompt_concept_ids"]
                )

                prompt_texts = []
                for frag in behaviour_prompt_fragments:
                    if not isinstance(frag, dict):
                        continue
                    content = frag.get("content")
                    if not isinstance(content, str):
                        continue
                    if not content.strip():
                        continue
                    prompt_texts.append(content)
                auxiliary_system_prompt = "\n\n".join(
                    text.strip() for text in prompt_texts if text and text.strip()
                )
                auxiliary_system_prompt = (
                    auxiliary_system_prompt.strip() if auxiliary_system_prompt else None
                )

                narration_texts = []
                for frag in narration_prompt_fragments:
                    if not isinstance(frag, dict):
                        continue
                    content = frag.get("content")
                    if not isinstance(content, str):
                        continue
                    if not content.strip():
                        continue
                    narration_texts.append(content)
                narration_prompt_text = "\n\n".join(
                    text.strip() for text in narration_texts if text and text.strip()
                )
                narration_prompt_text = (
                    narration_prompt_text.strip() if narration_prompt_text else None
                )

                screen_texts = []
                for frag in screen_prompt_fragments:
                    if not isinstance(frag, dict):
                        continue
                    content = frag.get("content")
                    if not isinstance(content, str):
                        continue
                    if not content.strip():
                        continue
                    screen_texts.append(content)
                screen_prompt_text = "\n\n".join(
                    text.strip() for text in screen_texts if text and text.strip()
                )
                screen_prompt_text = (
                    screen_prompt_text.strip() if screen_prompt_text else None
                )

                if auxiliary_system_prompt:
                    user_prompt_debug["loaded"] = True
                    user_prompt_debug["chars"] = len(auxiliary_system_prompt)
                if auxiliary_system_prompt:
                    current_app.logger.info(
                        "[CHAT_PROMPT] Loaded %d chars of user-specific prompt for %s",
                        len(auxiliary_system_prompt),
                        user_concept_id,
                    )
            except Exception as e:
                current_app.logger.warning(
                    "[CHAT_PROMPT] Failed loading user-specific prompt for %s: %s",
                    user_concept_id,
                    e,
                )
                user_prompt_debug["error"] = str(e)

        deterministic_introspection_enabled = _deterministic_introspection_enabled()

        if deterministic_introspection_enabled and user_concept_id:
            if _is_prompt_introspection_question(prompt_text):
                return _maybe_handle_prompt_introspection_fastpath(
                    prompt_text=prompt_text,
                    user_concept_id=user_concept_id,
                    history_user_id=history_user_id,
                    session_id=session_id,
                    auxiliary_system_prompt=auxiliary_system_prompt,
                    user_prompt_debug=user_prompt_debug,
                    context=context,
                    interaction_timestamp_utc=interaction_timestamp_utc,
                    model_name=model_name or "unknown",
                    request_start_perf=request_start_perf,
                    request_id=request_id,
                    progress_scope_key=progress_scope_key,
                )

            if _is_rag_status_question(prompt_text):
                return _maybe_handle_rag_status_fastpath(
                    prompt_text=prompt_text,
                    user_concept_id=user_concept_id,
                    history_user_id=history_user_id,
                    session_id=session_id,
                    context=context,
                    interaction_timestamp_utc=interaction_timestamp_utc,
                    model_name=model_name or "unknown",
                    request_start_perf=request_start_perf,
                    user_prompt_debug=user_prompt_debug,
                    request_id=request_id,
                    progress_scope_key=progress_scope_key,
                )

        if deterministic_introspection_enabled and _is_tool_introspection_question(
            prompt_text
        ):
            return _maybe_handle_tool_inventory_fastpath(
                prompt_text=prompt_text,
                user_concept_id=user_concept_id,
                history_user_id=history_user_id,
                session_id=session_id,
                context=context,
                interaction_timestamp_utc=interaction_timestamp_utc,
                model_name=model_name or "unknown",
                request_start_perf=request_start_perf,
                user_prompt_debug=user_prompt_debug,
                request_id=request_id,
                progress_scope_key=progress_scope_key,
            )

        # Try to get user name from concept if user_id provided
        if request_user_id:
            try:
                from ...services.concept_service import get_concept_by_concept_id

                user_concept = get_concept_by_concept_id(request_user_id)
                if user_concept:
                    user_name = user_concept.get("name") or request_user_id
                    system_message_parts.append(
                        f"Current user: {user_name} ({request_user_id})"
                    )
                    current_app.logger.info(
                        f"User context: {user_name} ({request_user_id})"
                    )
                else:
                    system_message_parts.append(f"Current user ID: {request_user_id}")
            except Exception as e:
                current_app.logger.warning(
                    f"Could not fetch user concept {request_user_id}: {e}"
                )
                system_message_parts.append(f"Current user ID: {request_user_id}")

        # Try to get organization name from concept if org_id provided
        if request_org_id:
            try:
                from ...services.concept_service import get_concept_by_concept_id

                org_concept = get_concept_by_concept_id(request_org_id)
                if org_concept:
                    org_name = org_concept.get("name") or request_org_id
                    system_message_parts.append(
                        f"Organization: {org_name} ({request_org_id})"
                    )
                    current_app.logger.info(
                        f"Organization context: {org_name} ({request_org_id})"
                    )
                else:
                    system_message_parts.append(f"Organization ID: {request_org_id}")
            except Exception as e:
                current_app.logger.warning(
                    f"Could not fetch org concept {request_org_id}: {e}"
                )
                system_message_parts.append(f"Organization ID: {request_org_id}")

        # Add language preference if provided
        if request_language and request_language != "en-NZ":
            system_message_parts.append(f"Language preference: {request_language}")

        # Create enhanced context with system message if we have user/org info
        enhanced_context = context.copy()
        if system_message_parts:
            # Create system message
            system_message = "You are Von, an AI assistant. " + " | ".join(
                system_message_parts
            )

            # Insert system message at the beginning if not already present
            if not enhanced_context or enhanced_context[0].get("role") != "system":
                enhanced_context.insert(
                    0, {"role": "system", "content": system_message}
                )
            else:
                # Update existing system message to include user/org context
                existing_system = enhanced_context[0]["content"]
                if not any(part in existing_system for part in system_message_parts):
                    enhanced_context[0][
                        "content"
                    ] = f"{existing_system} | {' | '.join(system_message_parts)}"

        # Ensure user-specific system prompt is included even when the orchestrator
        # is disabled/unavailable.
        if auxiliary_system_prompt:
            user_prompt_message = {
                "role": "system",
                "content": "USER-SPECIFIC SYSTEM PROMPT (from Vontology):\n"
                + auxiliary_system_prompt,
            }
            if not enhanced_context or enhanced_context[0].get("role") != "system":
                enhanced_context.insert(0, user_prompt_message)
            else:
                enhanced_context.insert(1, user_prompt_message)

        # ---------------------------------------------------------
        # JVNAUTOSCI-894: Presenter-mode response protocol
        # ---------------------------------------------------------
        if presenter_mode_requested:
            # Include a small client-reported timing hint for narration generation.
            # (Non-authoritative; used only for guidance.)
            timing_hint = None
            try:
                from ...services.client_capabilities_service import (
                    get_client_capabilities_snapshot,
                )

                snapshot = get_client_capabilities_snapshot()
                speech = (
                    snapshot.get("speech_synthesis")
                    if isinstance(snapshot, dict)
                    else None
                )
                speech = speech if isinstance(speech, dict) else {}
                raw_settings = speech.get("settings")
                settings = raw_settings if isinstance(raw_settings, dict) else {}

                preferred = settings.get("preferred_speaking_seconds")
                maximum = settings.get("max_speaking_seconds")

                try:
                    preferred_int = int(preferred) if preferred is not None else None
                except Exception:
                    preferred_int = None

                try:
                    maximum_int = int(maximum) if maximum is not None else None
                except Exception:
                    maximum_int = None

                if preferred_int is not None:
                    preferred_int = max(1, min(preferred_int, 600))
                if maximum_int is not None:
                    maximum_int = max(1, min(maximum_int, 600))

                effective_preferred = preferred_int
                if preferred_int is not None and maximum_int is not None:
                    effective_preferred = min(preferred_int, maximum_int)

                if effective_preferred is not None or maximum_int is not None:
                    timing_hint = (
                        "Speech timing hint (client-reported, non-authoritative): "
                        f"preferred_speaking_seconds={effective_preferred!r}, "
                        f"max_speaking_seconds={maximum_int!r}. "
                        "Aim for about preferred_speaking_seconds seconds and do not exceed max_speaking_seconds."
                    )
            except Exception:
                timing_hint = None

            presenter_protocol_message = {
                "role": "system",
                "content": (
                    "PRESENTER MODE PROTOCOL:\n"
                    "- Output EXACTLY TWO tagged blocks and nothing else:\n"
                    "  <spoken>...brief talk track...</spoken>\n"
                    "  <screen>...full on-screen content...</screen>\n"
                    "- <spoken> is what will be read aloud (TTS). Keep it short (1–4 sentences), conversational, and focused on the user's intent and what you did / what to do next. Do not read long lists, code blocks, or raw markdown.\n"
                    "- <screen> is what will be shown. It may include structured markdown, code blocks, and full details.\n"
                    "- Do not include <spoken>/<screen> tags inside code blocks.\n"
                    "- Use New Zealand English spelling."
                    + ("\n\n" + timing_hint if timing_hint else "")
                    + (
                        "\n\nVON CHAT NARRATION PROMPT (from Vontology):\n"
                        "(Applies ONLY to the <spoken> block; do not apply it to <screen>.)\n"
                        + narration_prompt_text
                        if narration_prompt_text
                        else ""
                    )
                    + (
                        "\n\nVON CHAT SCREEN CONTENT PROMPT (from Vontology):\n"
                        "(Applies ONLY to the <screen> block; it must not override tool-grounded facts.)\n"
                        + screen_prompt_text
                        if screen_prompt_text
                        else ""
                    )
                ),
            }
            enhanced_context.insert(0, presenter_protocol_message)

        # Log the enhanced context being sent to the model for debugging
        current_app.logger.info(
            f"Enhanced context being sent to model: {len(enhanced_context)} messages"
        )
        for i, msg in enumerate(enhanced_context):
            current_app.logger.info(
                f"Message {i}: role={msg.get('role')}, content_preview={msg.get('content', '')[:100]}..."
            )

        orchestrator = current_app.config.get("INTERNAL_MCP_ORCHESTRATOR")
        gateway = current_app.config.get("INTERNAL_MCP_GATEWAY")
        tool_messages: list[dict[str, str]] = []

        # Derive user namespace for MCP tool isolation (JVNAUTOSCI-760)
        user_namespace = None
        namespace_source = "missing"
        namespace_report: dict[str, object] = {
            "authenticated": bool(user_concept_id),
            "user_concept_id": user_concept_id,
            "session_namespace": session.get("namespace"),
            "namespace": None,
            "namespace_source": None,
        }
        if user_concept_id:
            session_namespace = session.get("namespace")
            if (
                isinstance(session_namespace, str)
                and session_namespace.strip()
                and session_namespace.startswith("#V#")
            ):
                user_namespace = session_namespace.strip()
                namespace_source = "session.namespace"
                current_app.logger.info(
                    "[NAMESPACE] Using session namespace=%s for user_concept_id=%s",
                    user_namespace,
                    user_concept_id,
                )
            else:
                # Convert concept ID to namespace format (#V#michael_witbrock)
                # Handle both full concept ID and person ID formats
                if user_concept_id.startswith("#V#"):
                    user_namespace = user_concept_id
                    namespace_source = "user_concept_id"
                else:
                    # Normalize to namespace format
                    user_id_normalized = (
                        user_concept_id.lower()
                        .replace(" ", "_")
                        .replace("#v#", "")
                        .replace("#", "")
                    )
                    user_namespace = f"#V#{user_id_normalized}"
                    namespace_source = "derived_from_user_concept_id"
                current_app.logger.info(
                    "[NAMESPACE] Derived user_namespace=%s from user_concept_id=%s",
                    user_namespace,
                    user_concept_id,
                )
        else:
            current_app.logger.warning(
                "[NAMESPACE] No user_concept_id - user_namespace=None (RAG unavailable)"
            )

        namespace_report["namespace"] = user_namespace
        namespace_report["namespace_source"] = namespace_source

        rag_trace: dict[str, object] = {
            "authenticated": bool(user_concept_id),
            "namespace": user_namespace,
            "namespace_source": namespace_source,
            "retrieval_attempted": False,
            "retrieval_attempt_reason": (
                None if user_concept_id else "not_authenticated"
            ),
            "tools_invoked": [],
            "tool_results_included_in_prompt": False,
        }

        # ---------------------------------------------------------
        # JVNAUTOSCI-1076: Workflow discovery during conversation turn
        # ---------------------------------------------------------
        # Search for applicable workflows based on user input.
        # Results are surfaced in llm_debug for the Thinking context display.
        workflow_discovery_result: dict[str, Any] | None = None
        workflow_discovery_enabled = os.getenv(
            "VON_WORKFLOW_DISCOVERY_ENABLE", "1"
        ).lower() in {"1", "true"}

        if workflow_discovery_enabled and prompt_text and len(prompt_text.strip()) >= 5:
            try:
                if show_tool_use_progress:
                    _set_tool_progress(
                        progress_scope_key,
                        request_id,
                        {
                            "status": "thinking",
                            "phase": "workflow_discovery",
                            "phase_label": "Searching for workflows",
                            "request_id": request_id,
                        },
                    )
                from ...services.workflow_discovery_service import (
                    discover_workflows_for_turn,
                )

                workflow_discovery_result = discover_workflows_for_turn(
                    prompt_text,
                    namespace=user_namespace,
                )
                if workflow_discovery_result:
                    current_app.logger.info(
                        "[WORKFLOW_DISCOVERY] Found %d relevant workflows for prompt",
                        workflow_discovery_result.get("match_count", 0),
                    )
                    # Emit workflow discovery results in tool progress for frontend
                    if show_tool_use_progress:
                        _set_tool_progress(
                            progress_scope_key,
                            request_id,
                            {
                                "status": "thinking",
                                "phase": "workflow_discovery_complete",
                                "phase_label": "Found workflows",
                                "request_id": request_id,
                                "workflow_discovery": workflow_discovery_result,
                            },
                        )
            except Exception as e:
                current_app.logger.warning("[WORKFLOW_DISCOVERY] Search failed: %s", e)
                workflow_discovery_result = None

        # ---------------------------------------------------------
        # Tool-backed RAG counts (avoid KA vs chat-history confusion)
        # ---------------------------------------------------------
        # We bypass the LLM for simple factual questions about indexed sessions,
        # because the assistant can otherwise confuse:
        # - interaction sessions (KA sessions) vs
        # - chat history sessions.
        def _is_rag_counts_question(text: str) -> bool:
            lowered = (text or "").strip().lower()
            if not lowered:
                return False

            # Keep this intentionally narrow to avoid hijacking normal chat.
            count_triggers = (
                "how many",
                "count",
                "number of",
            )
            domain_triggers = (
                "indexed",
                "rag",
            )
            chat_triggers = (
                "chat session",
                "chat sessions",
                "chat history",
            )
            ka_triggers = (
                "ka",
                "interaction session",
                "interaction sessions",
            )

            has_count = any(t in lowered for t in count_triggers)
            has_domain = any(t in lowered for t in domain_triggers)
            refers_chat = any(t in lowered for t in chat_triggers)
            refers_ka = any(t in lowered for t in ka_triggers)

            # Examples:
            # - "How many indexed chat sessions can you see?"
            # - "How many chat history sessions are indexed?"
            # - "How many KA sessions are indexed?"
            return has_count and has_domain and (refers_chat or refers_ka)

        if (
            user_concept_id
            and user_namespace
            and gateway is not None
            and getattr(gateway, "enabled", False)
            and _is_rag_counts_question(prompt_text)
        ):
            import json as _json

            user_id_for_history: str | None = history_user_id or user_concept_id

            tool_invocations = []
            try:
                tool_result = gateway.invoke(
                    "rag_get_status",
                    {"namespace": user_namespace, "detail": 1},
                )
                payload = tool_result.payload
                duration_ms = getattr(tool_result, "duration_ms", None)

                tool_payload = _json.dumps(
                    {
                        "tool": "rag_get_status",
                        "status": "ok",
                        "duration_ms": duration_ms,
                        "payload": payload,
                    },
                    default=str,
                )
                tool_messages = [{"role": "tool", "content": tool_payload}]
                tool_invocations = [
                    {
                        "tool": "rag_get_status",
                        "payload": {"namespace": user_namespace, "detail": 1},
                        "duration_ms": duration_ms,
                        "direct_user_call": False,
                    }
                ]

                # Summarise counts in a way that makes the KA vs chat distinction explicit.
                rs = payload if isinstance(payload, dict) else {}
                ka_indexed = rs.get("indexed")
                ch_sessions = rs.get("chat_history_sessions_in_namespace")
                if ch_sessions is None:
                    ch_sessions = rs.get("chat_history_sessions")

                ch_details = rs.get("chat_history_session_details")
                fully_indexed = None
                any_indexed = None
                if isinstance(ch_details, list) and ch_details:

                    def _as_int(value):
                        try:
                            return int(value)
                        except Exception:
                            return 0

                    fully_indexed = 0
                    any_indexed = 0
                    for row in ch_details:
                        if not isinstance(row, dict):
                            continue
                        missing = _as_int(row.get("messages_missing_index"))
                        ok = _as_int(row.get("rag_indexed_success"))
                        fail = _as_int(row.get("rag_indexed_failed"))
                        indexed_total = row.get("messages_indexed_total")
                        indexed_total_int = (
                            _as_int(indexed_total)
                            if indexed_total is not None
                            else (ok + fail)
                        )

                        if indexed_total_int > 0:
                            any_indexed += 1
                        if missing <= 0:
                            fully_indexed += 1

                parts = []
                parts.append(
                    f"Chat history sessions (in namespace): {ch_sessions if ch_sessions is not None else '—'}"
                )
                if any_indexed is not None:
                    parts.append(
                        f"Chat history sessions with any indexed messages: {any_indexed}"
                    )
                if fully_indexed is not None:
                    parts.append(
                        f"Chat history sessions fully indexed (missing=0): {fully_indexed}"
                    )
                parts.append(
                    f"KA interaction sessions indexed: {ka_indexed if ka_indexed is not None else '—'}"
                )

                response_text = (
                    "Here are the server-truth counts (RAG status), keeping chat history separate from KA interaction sessions:\n\n"
                    + "\n".join(f"- {p}" for p in parts)
                )
            except Exception as exc:
                response_text = (
                    f"I could not retrieve RAG status via internal tools: {exc}"
                )
                tool_messages = []

            # Persist messages in history/context.
            if user_id_for_history:
                chat_history_service.add_message_to_history(
                    user_id_for_history,
                    session_id,
                    {
                        "role": "user",
                        "content": prompt_text,
                        "author_user_id": user_concept_id,
                    },
                )
            for tool_msg in _truncate_large_tool_results(
                tool_messages, max_tool_content_chars=5000
            ):
                if user_id_for_history:
                    chat_history_service.add_message_to_history(
                        user_id_for_history, session_id, tool_msg
                    )
            current_app.config["CONTEXT"] = _limit_context_size(
                current_app.config["CONTEXT"], max_messages=20
            )

            context_stats = _calculate_context_stats(context)
            current_context_stats = _calculate_context_stats(
                current_app.config["CONTEXT"]
            )
            tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None
            tool_progress_snapshot = _snapshot_tool_progress_for_request(
                progress_scope_key, request_id
            )
            turn_execution_diagnostics = _build_turn_execution_diagnostics(
                request_id=request_id,
                prompt_text=prompt_text,
                elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
                tool_progress_state=tool_progress_snapshot,
            )

            llm_debug_info = {
                "interaction_timestamp_utc": interaction_timestamp_utc,
                "request_id": request_id,
                "model": model_name,
                "llm_interaction": {
                    "requested_model": model_name,
                    "orchestrator_used": False,
                    "duration_ms": None,
                    "usage": None,
                    "calls": [],
                    "server_elapsed_ms": (time.perf_counter() - request_start_perf)
                    * 1000.0,
                },
                "messages": (
                    [{"role": "user", "content": prompt_text}] + tool_messages
                ),
                "response": response_text,
                "user_prompt": user_prompt_debug,
                "context_stats": {
                    "sent_to_llm": context_stats,
                    "stored_context": current_context_stats,
                },
                "tool_stats": tool_stats,
                "tool_invocations": tool_invocations,
                "fastpath": {
                    "name": "rag_counts",
                    "bypassed_llm": True,
                    "used_tool": bool(tool_messages),
                    "enabled": True,
                },
                "turn_execution_diagnostics": turn_execution_diagnostics,
            }
            llm_debug_info = _finalise_llm_debug_info(
                llm_debug_info=llm_debug_info,
                prompt_text=prompt_text,
                response_text=response_text,
                session_id=session_id,
                namespace=user_namespace,
                user_id=user_id_for_history or user_concept_id,
                org_id=org_concept_id,
            )

            if user_id_for_history:
                chat_history_service.add_message_to_history(
                    user_id_for_history,
                    session_id,
                    {"role": "assistant", "content": response_text},
                    llm_debug_data=llm_debug_info,
                )

            return jsonify(
                {
                    "response": response_text,
                    "fastpath": {
                        "name": "rag_counts",
                        "bypassed_llm": True,
                        "used_tool": bool(tool_messages),
                    },
                    "llm_debug": llm_debug_info,
                }
            )

        # ---------------------------------------------------------
        # Optional debug mode: allow user-issued tool calls
        # ---------------------------------------------------------
        # This is disabled by default because it bypasses the LLM's behavioural
        # guardrails. When enabled, only read-category tools are permitted.
        allow_user_tool_calls = os.getenv(
            "VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "0"
        ).lower() in {"1", "true"}
        if allow_user_tool_calls and orchestrator is not None and gateway is not None:
            try:
                direct_request = orchestrator._extract_json_blob(prompt_text)  # type: ignore[attr-defined]
            except ToolCallParsingError:
                direct_request = None

            if direct_request and isinstance(direct_request, dict):
                action = direct_request.get("action")
                tool_name = direct_request.get("tool")
                payload = direct_request.get("payload") or {}

                if (
                    action == "call_tool"
                    and isinstance(tool_name, str)
                    and isinstance(payload, dict)
                ):
                    try:
                        meta = gateway.describe_methods().get(tool_name)  # type: ignore[union-attr]
                        category = (
                            meta.get("category") if isinstance(meta, dict) else None
                        )
                    except Exception:
                        category = None

                    if category != "read":
                        response_text = (
                            "Direct tool calls are restricted to read-only tools. "
                            "Ask Von normally if you need write actions."
                        )
                        tool_invocations = []
                    else:
                        # Direct tool-call mode should not mutate payloads except for
                        # Gmail profile convenience (namespace injection can break
                        # strict schemas like Jira tools).
                        if tool_name.startswith("gmail_"):
                            if request_gmail_profile and not payload.get("profile"):
                                payload["profile"] = request_gmail_profile
                            payload.pop("namespace", None)

                        try:
                            result = gateway.invoke(tool_name, payload)  # type: ignore[union-attr]
                            tool_payload = orchestrator._format_tool_result(tool_name, result.payload, result.duration_ms, "ok")  # type: ignore[attr-defined]
                            response_text = tool_payload
                            tool_messages = [{"role": "tool", "content": tool_payload}]
                            tool_invocations = [
                                {
                                    "tool": tool_name,
                                    "payload": dict(payload),
                                    "direct_user_call": True,
                                }
                            ]
                        except Exception as exc:
                            tool_payload = orchestrator._format_tool_result(tool_name, None, None, "error", str(exc))  # type: ignore[attr-defined]
                            response_text = tool_payload
                            tool_messages = [{"role": "tool", "content": tool_payload}]
                            tool_invocations = [
                                {
                                    "tool": tool_name,
                                    "payload": dict(payload),
                                    "error": str(exc),
                                    "direct_user_call": True,
                                }
                            ]

                    # Skip LLM generation for direct tool calls
                    if history_user_id:
                        chat_history_service.add_message_to_history(
                            history_user_id,
                            session_id,
                            {
                                "role": "user",
                                "content": prompt_text,
                                "author_user_id": user_concept_id,
                            },
                        )
                        for tool_msg in _truncate_large_tool_results(
                            tool_messages, max_tool_content_chars=5000
                        ):
                            chat_history_service.add_message_to_history(
                                history_user_id, session_id, tool_msg
                            )
                    else:
                        current_app.config["CONTEXT"].append(
                            {"role": "user", "content": prompt_text}
                        )
                        for tool_msg in _truncate_large_tool_results(
                            tool_messages, max_tool_content_chars=5000
                        ):
                            current_app.config["CONTEXT"].append(tool_msg)
                        current_app.config["CONTEXT"].append(
                            {"role": "assistant", "content": response_text}
                        )

                    current_app.config["CONTEXT"] = _limit_context_size(
                        current_app.config["CONTEXT"], max_messages=20
                    )

                    # Return immediately with debug info
                    current_turn_messages = [
                        {"role": "user", "content": prompt_text}
                    ] + tool_messages
                    context_stats = _calculate_context_stats(enhanced_context)
                    current_context_stats = _calculate_context_stats(
                        current_app.config["CONTEXT"]
                    )
                    tool_stats = (
                        _calculate_tool_stats(tool_messages) if tool_messages else None
                    )
                    tool_progress_snapshot = _snapshot_tool_progress_for_request(
                        progress_scope_key, request_id
                    )
                    turn_execution_diagnostics = _build_turn_execution_diagnostics(
                        request_id=request_id,
                        prompt_text=prompt_text,
                        elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
                        tool_progress_state=tool_progress_snapshot,
                    )

                    llm_debug_info = {
                        "interaction_timestamp_utc": interaction_timestamp_utc,
                        "request_id": request_id,
                        "model": model_name,
                        "llm_interaction": {
                            "requested_model": model_name,
                            "orchestrator_used": False,
                            "duration_ms": None,
                            "usage": None,
                            "calls": [],
                            "server_elapsed_ms": (
                                time.perf_counter() - request_start_perf
                            )
                            * 1000.0,
                        },
                        "messages": current_turn_messages,
                        "response": response_text,
                        "user_prompt": user_prompt_debug,
                        "namespace_report": namespace_report,
                        "context_stats": {
                            "sent_to_llm": context_stats,
                            "stored_context": current_context_stats,
                        },
                        "tool_stats": tool_stats,
                        "tool_invocations": tool_invocations,
                        "aux_llm_calls": [],
                        "response_transformations": (
                            build_response_transformation_telemetry_payload(
                                request_id=request_id
                            )
                        ),
                        "turn_execution_diagnostics": turn_execution_diagnostics,
                    }
                    llm_debug_info = _finalise_llm_debug_info(
                        llm_debug_info=llm_debug_info,
                        prompt_text=prompt_text,
                        response_text=response_text,
                        session_id=session_id,
                        namespace=user_namespace,
                        user_id=history_user_id or user_concept_id,
                        org_id=org_concept_id,
                    )

                    if history_user_id:
                        chat_history_service.add_message_to_history(
                            history_user_id,
                            session_id,
                            {"role": "assistant", "content": response_text},
                            llm_debug_data=llm_debug_info,
                        )

                    rag_trace["tools_invoked"] = [
                        inv.get("tool")
                        for inv in tool_invocations
                        if isinstance(inv, dict) and isinstance(inv.get("tool"), str)
                    ]
                    rag_trace["retrieval_attempted"] = False
                    rag_trace["retrieval_attempt_reason"] = "direct_tool_call"

                    return jsonify(
                        {
                            "response": response_text,
                            "llm_debug": llm_debug_info,
                            "rag_trace": rag_trace,
                        }
                    )

        auxiliary_llm_calls: list[dict] = []
        llm_interaction: dict = {
            "requested_model": model_name,
            "orchestrator_used": orchestrator is not None,
            "duration_ms": None,
            "usage": None,
            "calls": [],
        }
        render_plan_debug: dict[str, Any] | None = None
        workflow_routing_info: dict[str, Any] | None = None

        def _infer_provider(model_id: str | None) -> str | None:
            if not isinstance(model_id, str):
                return None
            lowered = model_id.strip().lower()
            if not lowered:
                return None
            if lowered.startswith("openai:"):
                return "openai"
            if lowered.startswith(
                ("gpt-", "o1-", "text-", "davinci", "curie", "babbage", "ada")
            ):
                return "openai"
            if lowered.startswith("gemini"):
                return "gemini"
            if lowered.startswith("ollama:"):
                return "ollama"
            if ":" in lowered and not lowered.startswith("ft:"):
                return "ollama"
            return None

        def _record_stage_llm_call(
            *,
            call_type: str,
            model_name: str | None,
            duration_ms: float | None,
            usage: dict | None = None,
            note: str | None = None,
            stage: str | None = None,
            provider: str | None = None,
            candidate: Mapping[str, Any] | None = None,
        ) -> None:
            payload = {
                "type": call_type,
                "model": model_name,
                "provider": provider or _infer_provider(model_name),
                "duration_ms": duration_ms,
                "usage": usage,
                "workflow": "von_generate",
            }
            if stage:
                payload["stage"] = stage
            if note:
                payload["note"] = note
            if isinstance(candidate, Mapping):
                payload["candidate"] = dict(candidate)
            llm_interaction["calls"].append(payload)

        if orchestrator is None:
            llm_start_perf = time.perf_counter()
            response_text = llm_client.generate(
                prompt_text, context=enhanced_context, model=model_name
            )
            llm_interaction["duration_ms"] = (
                time.perf_counter() - llm_start_perf
            ) * 1000.0
            llm_interaction["calls"] = [
                {
                    "type": "llm.generate",
                    "model": model_name,
                    "provider": _infer_provider(model_name),
                    "duration_ms": llm_interaction["duration_ms"],
                    "usage": None,
                    "workflow": "von_generate",
                }
            ]
            tool_invocations = []
        else:
            try:
                current_app.logger.info(
                    "[NAMESPACE] Calling orchestrator.run() with user_namespace=%s",
                    user_namespace,
                )
                try:
                    orchestrator.configure_execution_caps(
                        max_tool_invocations=get_internal_mcp_max_tool_invocations(),
                        tool_batch_cap=get_internal_mcp_tool_batch_cap(),
                    )
                except Exception:
                    # Defensive: never fail the request due to settings refresh.
                    pass

                # JVNAUTOSCI-1038: Create request-scoped progress tracker
                progress_tracker = None
                if show_tool_use_progress:

                    def _progress_update(info: dict[str, Any]) -> None:
                        payload = (
                            dict(info)
                            if isinstance(info, dict)
                            else {"status": "unknown"}
                        )
                        payload.setdefault("request_id", request_id)
                        _set_tool_progress(progress_scope_key, request_id, payload)

                    progress_tracker = ProgressTracker(callback=_progress_update)

                # JVNAUTOSCI-1038: Background execution mode
                if background_mode:
                    # Capture app context for background thread
                    app = current_app._get_current_object()

                    # Create progress tracker with cancellation support
                    def _cancellation_checker() -> bool:
                        return background_task_registry.is_cancellation_requested(
                            request_id
                        )

                    # Override progress_tracker with cancellation-aware version
                    progress_tracker = ProgressTracker(
                        callback=_progress_update if show_tool_use_progress else None,
                        cancellation_checker=_cancellation_checker,
                        task_id=request_id,
                    )

                    # JVNAUTOSCI-1038 Phase 4: Capture history context for persistence
                    bg_history_user_id = history_user_id
                    bg_session_id = session_id
                    bg_user_concept_id = user_concept_id

                    def _run_in_background() -> Any:
                        """Execute orchestrator.run() in background with app context.

                        Phase 4: Also persists results to chat history on completion.
                        """
                        with app.app_context():
                            result = orchestrator.run(
                                prompt=prompt_text,
                                context=enhanced_context,
                                llm_client=llm_client,
                                model=model_name,
                                user_namespace=user_namespace,
                                gmail_profile=request_gmail_profile,
                                auxiliary_system_prompt=auxiliary_system_prompt,
                                preferred_language=request_language,
                                progress_tracker=progress_tracker,
                                conversation_session_id=bg_session_id,
                                turn_id=request_id,
                                workflow_discovery_result=workflow_discovery_result,
                            )

                            # Phase 4: Persist to chat history
                            if bg_history_user_id:
                                try:
                                    # Store user message
                                    chat_history_service.add_message_to_history(
                                        bg_history_user_id,
                                        bg_session_id,
                                        {
                                            "role": "user",
                                            "content": prompt_text,
                                            "author_user_id": bg_user_concept_id,
                                            "background_task_id": request_id,
                                        },
                                    )
                                    # Store tool messages
                                    tool_messages = [
                                        dict(msg) for msg in result.extra_messages
                                    ]
                                    for tool_msg in _truncate_large_tool_results(
                                        tool_messages, max_tool_content_chars=5000
                                    ):
                                        chat_history_service.add_message_to_history(
                                            bg_history_user_id,
                                            bg_session_id,
                                            tool_msg,
                                        )
                                    # Store assistant response
                                    chat_history_service.add_message_to_history(
                                        bg_history_user_id,
                                        bg_session_id,
                                        {
                                            "role": "assistant",
                                            "content": result.response_text,
                                            "background_task_id": request_id,
                                        },
                                    )
                                except Exception as hist_exc:
                                    _logger.warning(
                                        "[background] Failed to persist history: %s",
                                        hist_exc,
                                    )

                            return result

                    # Submit to background registry with session context (Phase 4)
                    task_status = background_task_registry.submit_task(
                        task_id=request_id,
                        callable=_run_in_background,
                        progress_callback=(
                            _progress_update if show_tool_use_progress else None
                        ),
                        session_id=bg_session_id,
                        user_id=bg_history_user_id,
                    )

                    return (
                        jsonify(
                            {
                                "background": True,
                                "task_id": request_id,
                                "status": task_status.status,
                                "message": "Task submitted for background execution",
                                "status_url": f"/von/api/task/status/{request_id}",
                                "result_url": f"/von/api/task/result/{request_id}",
                            }
                        ),
                        202,
                    )

                if show_tool_use_progress:
                    _set_tool_progress(
                        progress_scope_key,
                        request_id,
                        {
                            "status": "orchestrator_start",
                            "stage": "orchestrator_start",
                            "phase_label": "Starting orchestrator",
                            "request_id": request_id,
                        },
                    )

                orchestrator_start_perf = time.perf_counter()
                orchestrator_result = orchestrator.run(
                    prompt=prompt_text,
                    context=enhanced_context,
                    llm_client=llm_client,
                    model=model_name,
                    user_namespace=user_namespace,
                    gmail_profile=request_gmail_profile,
                    auxiliary_system_prompt=auxiliary_system_prompt,
                    preferred_language=request_language,
                    progress_tracker=progress_tracker,
                    conversation_session_id=session_id,
                    turn_id=request_id,
                    workflow_discovery_result=workflow_discovery_result,
                )
                llm_interaction["duration_ms"] = (
                    time.perf_counter() - orchestrator_start_perf
                ) * 1000.0
                llm_interaction["calls"] = list(
                    getattr(orchestrator_result, "llm_calls", [])
                )
                llm_interaction["usage"] = getattr(
                    orchestrator_result, "llm_usage", None
                )
                llm_interaction["orchestrator_duration_ms"] = getattr(
                    orchestrator_result, "orchestrator_duration_ms", None
                )
                response_text = orchestrator_result.response_text
                tool_messages = [
                    dict(msg) for msg in orchestrator_result.extra_messages
                ]
                tool_invocations = list(orchestrator_result.tool_invocations)
                auxiliary_llm_calls = list(
                    getattr(orchestrator_result, "aux_llm_calls", [])
                )
                raw_render_plan = getattr(orchestrator_result, "render_plan", None)
                if isinstance(raw_render_plan, dict):
                    render_plan_debug = dict(raw_render_plan)
                workflow_routing_raw = getattr(
                    orchestrator_result, "workflow_routing", None
                )
                workflow_routing_info = _normalise_workflow_routing_payload(
                    workflow_routing_raw
                )

                invoked_tools = []
                for inv in tool_invocations:
                    if not isinstance(inv, dict):
                        continue
                    name = inv.get("tool") or inv.get("method")
                    if isinstance(name, str) and name:
                        invoked_tools.append(name)

                rag_trace["tools_invoked"] = invoked_tools
                rag_trace["retrieval_attempted"] = (
                    "search_knowledge_base" in invoked_tools
                )
                if rag_trace["retrieval_attempted"]:
                    rag_trace["retrieval_attempt_reason"] = "tool_invoked"
                else:
                    rag_trace["retrieval_attempt_reason"] = (
                        "no_rag_retrieval_tool_invoked"
                        if user_concept_id
                        else "not_authenticated"
                    )
                rag_trace["tool_results_included_in_prompt"] = bool(tool_messages)

                if show_tool_use_progress:
                    _set_tool_progress(
                        progress_scope_key,
                        request_id,
                        {
                            "status": "orchestrator_end",
                            "stage": "orchestrator_end",
                            "phase_label": "Finishing orchestrator",
                            "request_id": request_id,
                        },
                    )
            except ToolCallParsingError as exc:
                current_app.logger.warning(
                    "[mcp_orchestrator] Invalid tool request payload: %s", exc
                )
                response_text = (
                    "Tool call was not executed due to an MCP serialisation error. "
                    f"({exc})\n\n"
                    "Please try again. If this keeps happening, copy the LLM debug output so we can reproduce it."
                )
                rejected_tool_call = _truncate_debug_payload(
                    getattr(exc, "raw_response", None)
                )
                tool_messages = []
                tool_invocations = [
                    {
                        "tool": "__tool_call_parse_error__",
                        "payload": (
                            {"raw_tool_call": rejected_tool_call}
                            if rejected_tool_call is not None
                            else {}
                        ),
                        "error": str(exc),
                    }
                ]

                if show_tool_use_progress:
                    _set_tool_progress(
                        progress_scope_key,
                        request_id,
                        {
                            "status": "error",
                            "phase": "error",
                            "phase_label": "Error",
                            "request_id": request_id,
                            "error": str(exc),
                        },
                    )

        presenter_channels = _extract_presenter_channels(response_text)
        presenter_channels_missing = (
            not isinstance(presenter_channels, dict) or not presenter_channels
        )
        has_tool_messages = bool(tool_messages)
        response_transformations = build_response_transformation_telemetry_payload(
            request_id=request_id
        )

        screen_backfill_started_perf = time.perf_counter()
        screen_backfill_second_pass_attempted = False
        screen_backfill_second_pass_reason = None
        screen_backfill_source = None
        screen_backfill_model_id = None
        screen_backfill_error_class = None
        screen_backfill_applied = False
        screen_backfill_screen_tag_present: bool | None = None
        needs_screen_backfill = False
        required_screen_json_fence = None
        screen_fence_compat_enabled = (
            get_display_elements_screen_fence_compat_enabled(default=True)
        )

        if presenter_mode_requested:
            screen_tag_present = _presenter_tag_present(response_text, "screen")
            screen_backfill_screen_tag_present = screen_tag_present
            required_screen_json_fence = _extract_required_screen_json_fence(prompt_text)
            screen_text = None
            spoken_text = None
            if isinstance(presenter_channels, dict):
                screen_value = presenter_channels.get("screen")
                if isinstance(screen_value, str) and screen_value.strip():
                    screen_text = screen_value.strip()
                spoken_value = presenter_channels.get("spoken")
                if isinstance(spoken_value, str) and spoken_value.strip():
                    spoken_text = spoken_value.strip()

            def _screen_looks_like_tool_dump(value: str) -> bool:
                lowered = (value or "").strip().lower()
                if not lowered:
                    return True
                if lowered.startswith("tool results:"):
                    return True
                if "```json" in lowered or '"payload"' in lowered:
                    return True
                return False

            def _screen_too_similar_to_spoken(
                screen_value: str | None, spoken_value: str | None
            ) -> bool:
                if not screen_value or not spoken_value:
                    return False
                a = screen_value.strip()
                b = spoken_value.strip()
                if not a or not b:
                    return False
                # Heuristic: if the screen is exactly the talk track, it usually means the
                # model emitted only <spoken> and we defaulted screen=spoken.
                return a == b

            needs_screen_backfill = (
                (not screen_tag_present)
                or (not isinstance(screen_text, str) or not screen_text.strip())
                or (
                    isinstance(screen_text, str)
                    and _screen_looks_like_tool_dump(screen_text)
                )
                or (
                    screen_fence_compat_enabled
                    and isinstance(required_screen_json_fence, str)
                    and required_screen_json_fence.strip()
                    and (
                        not isinstance(screen_text, str)
                        or required_screen_json_fence.strip() not in screen_text
                    )
                )
                or _screen_too_similar_to_spoken(screen_text, spoken_text)
            )

            def _missing_required_screen_fence() -> bool:
                return bool(
                    screen_fence_compat_enabled
                    and isinstance(required_screen_json_fence, str)
                    and required_screen_json_fence.strip()
                    and (
                        not isinstance(screen_text, str)
                        or required_screen_json_fence.strip() not in screen_text
                    )
                )

            def _strip_presenter_tags(text: str) -> str | None:
                if not isinstance(text, str) or not text:
                    return None
                import re

                cleaned = re.sub(r"</?spoken>", "", text, flags=re.IGNORECASE)
                cleaned = re.sub(r"</?screen>", "", cleaned, flags=re.IGNORECASE)
                cleaned = cleaned.strip()
                return cleaned or None

            if needs_screen_backfill:
                screen_backfill_second_pass_attempted = True
                if show_tool_use_progress:
                    _set_tool_progress(
                        progress_scope_key,
                        request_id,
                        {
                            "status": "phase_transition",
                            "phase": "screen_backfill",
                            "phase_label": "Generating response",
                            "request_id": request_id,
                            "workflow_task": "screen_backfill",
                        },
                    )
                if not screen_tag_present or not screen_text:
                    screen_backfill_second_pass_reason = "missing_screen"
                elif _missing_required_screen_fence():
                    screen_backfill_second_pass_reason = "missing_screen_fence"
                elif _screen_looks_like_tool_dump(screen_text or ""):
                    screen_backfill_second_pass_reason = "tool_payload_screen"
                else:
                    screen_backfill_second_pass_reason = "screen_matches_spoken"

                tool_blob = (
                    _build_tool_messages_prompt_blob(tool_messages)
                    if has_tool_messages
                    else ""
                )

                def _tool_messages_include_description_write(
                    messages: list[dict],
                ) -> bool:
                    """Best-effort detection of a description write tool action.

                    We keep this conservative: if we cannot identify a description
                    write confidently, return False.
                    """

                    import json

                    for message in messages:
                        if message.get("role") != "tool":
                            continue
                        content = message.get("content")
                        if not isinstance(content, str) or not content.strip():
                            continue

                        try:
                            parsed = json.loads(content)
                        except Exception:
                            continue

                        if not isinstance(parsed, dict):
                            continue

                        tool_name = (
                            parsed.get("tool")
                            or parsed.get("method")
                            or parsed.get("name")
                            or ""
                        )
                        tool_name = tool_name if isinstance(tool_name, str) else ""
                        tool_name_lower = tool_name.lower()

                        payload = parsed.get("payload")
                        payload = payload if isinstance(payload, dict) else {}

                        if "description" in tool_name_lower:
                            return True

                        predicate = payload.get("predicate")
                        if isinstance(predicate, str) and predicate.strip() in {
                            "hasDescription",
                            "has_description",
                            "#V#hasDescription",
                        }:
                            return True

                        if (
                            isinstance(payload.get("description"), str)
                            and payload.get("description").strip()
                        ):
                            return True

                    return False

                description_write_seen = (
                    _tool_messages_include_description_write(tool_messages)
                    if has_tool_messages
                    else False
                )

                screen_candidate = None
                screen_backfill_source = None
                response_candidate = _strip_presenter_tags(response_text)
                if response_candidate and not _screen_looks_like_tool_dump(
                    response_candidate
                ):
                    screen_candidate = response_candidate
                    screen_backfill_source = "response_text"
                allow_llm_screen_synthesis = os.getenv(
                    "VON_PRESENTER_SCREEN_BACKFILL_USE_LLM", "1"
                ).lower() in {"1", "true"}

                if screen_candidate is None and allow_llm_screen_synthesis:
                    try:
                        if has_tool_messages:
                            synthesis_system = (
                                "You are Von. Create the on-screen response for the chat UI. "
                                "Return ONLY one block: <screen>...</screen>. "
                                "Do not include <spoken>. Do not include JSON. "
                                "Use New Zealand English spelling. "
                                "CRITICAL: Only state facts that are explicitly present in the tool results summary. "
                                "Do not infer, guess, or add any claims beyond tool outputs."
                            )
                            synthesis_user = (
                                "User request:\n"
                                f"{prompt_text}\n\n"
                                "Model response (may be incomplete; NOT authoritative for tool-backed changes):\n"
                                f"{response_text}\n\n"
                                + (
                                    "VON CHAT SCREEN CONTENT PROMPT (from Vontology):\n"
                                    "(Applies ONLY to <screen> formatting; it must not override tool-grounded facts.)\n"
                                    + str(screen_prompt_text).strip()
                                    + "\n\n"
                                    if isinstance(screen_prompt_text, str)
                                    and screen_prompt_text.strip()
                                    else ""
                                )
                                + "Tool results summary (authoritative):\n"
                                f"{tool_blob}\n"
                                "\nNon-negotiable rule:\n"
                                "- If the tool results summary does not explicitly show a description update, you MUST NOT claim the description was added/updated. "
                                "  You may say it is still empty/vacuous or that no tool updated it.\n"
                                "- You MUST include a short section titled 'Writes ledger (authoritative)' that reflects the tool writes ledger without contradiction.\n"
                            )
                        else:
                            synthesis_system = (
                                "You are Von. Create the on-screen response for the chat UI. "
                                "Return ONLY one block: <screen>...</screen>. "
                                "Do not include <spoken>. Do not include JSON. "
                                "Use New Zealand English spelling. "
                                "Use only the user request and the model response as sources. "
                                "Do not invent facts beyond what is stated there."
                            )
                            synthesis_user = (
                                "User request:\n"
                                f"{prompt_text}\n\n"
                                "Model response (may be incomplete; use it as content to display):\n"
                                f"{response_text}\n\n"
                                + (
                                    "VON CHAT SCREEN CONTENT PROMPT (from Vontology):\n"
                                    "(Applies ONLY to <screen> formatting.)\n"
                                    + str(screen_prompt_text).strip()
                                    + "\n\n"
                                    if isinstance(screen_prompt_text, str)
                                    and screen_prompt_text.strip()
                                    else ""
                                )
                            )

                        synthesis_response = None
                        if orchestrator is not None and hasattr(
                            orchestrator, "_run_llm_with_fallbacks"
                        ):
                            try:
                                policy_state, _ = (
                                    orchestrator._load_workflow_model_policy(
                                        request_language
                                    )
                                )
                                synthesis_response, screen_model_used, _ = (
                                    orchestrator._run_llm_with_fallbacks(
                                        stage="screen_backfill",
                                        prompt="Generate <screen> display content",
                                        context=[
                                            {
                                                "role": "system",
                                                "content": synthesis_system,
                                            },
                                            {"role": "user", "content": synthesis_user},
                                        ],
                                        default_client=llm_client,
                                        default_model=model_name,
                                        policy_state=policy_state,
                                        user_concept_id=user_concept_id,
                                        org_concept_id=org_concept_id,
                                        llm_calls_log=llm_interaction["calls"],
                                        aux_log=auxiliary_llm_calls,
                                        record_llm_call=_record_stage_llm_call,
                                    )
                                )
                                screen_backfill_model_id = screen_model_used
                            except Exception:
                                synthesis_response = None
                        if synthesis_response is None:
                            llm_start = time.perf_counter()
                            screen_model_used = model_name
                            screen_backfill_model_id = screen_model_used
                            synthesis_response = llm_client.generate(
                                prompt="Generate <screen> display content",
                                context=[
                                    {"role": "system", "content": synthesis_system},
                                    {"role": "user", "content": synthesis_user},
                                ],
                                model=screen_model_used,
                            )
                            _record_stage_llm_call(
                                call_type="llm.generate",
                                model_name=screen_model_used,
                                duration_ms=(time.perf_counter() - llm_start) * 1000.0,
                                usage=None,
                                note="Screen backfill synthesis (legacy).",
                                stage="screen_backfill",
                                provider=_infer_provider(screen_model_used),
                            )

                        screen_candidate = _extract_screen_only(str(synthesis_response))
                        if not screen_candidate:
                            raw = str(synthesis_response).strip()
                            if raw:
                                screen_candidate = raw
                        if screen_candidate:
                            screen_backfill_source = "llm_synthesis"
                    except Exception as exc:
                        screen_backfill_error_class = type(exc).__name__
                        screen_candidate = None

                # If the LLM tries to claim a description write without evidence,
                # discard it and fall back to the deterministic tool summary.
                if (
                    screen_candidate
                    and not description_write_seen
                    and isinstance(screen_candidate, str)
                ):
                    import re

                    lowered = screen_candidate.lower()
                    description_claim_patterns = (
                        r"\bdescription\s+(?:has\s+been\s+)?(?:added|updated|set|filled)\b",
                        r"\badded\s+(?:a\s+)?description\b",
                        r"\bupdated\s+(?:the\s+)?description\b",
                    )
                    if any(
                        re.search(p, lowered, flags=re.IGNORECASE)
                        for p in description_claim_patterns
                    ):
                        screen_candidate = None

                if not screen_candidate:
                    screen_candidate = (
                        _build_presenter_screen_summary_from_tool_messages(
                            tool_messages
                        )
                    )
                    if screen_candidate:
                        screen_backfill_source = "tool_summary"

                if screen_candidate:
                    if screen_fence_compat_enabled:
                        screen_candidate = _ensure_required_screen_json_fence(
                            str(screen_candidate).strip(),
                            required_screen_json_fence,
                        )
                    base_channels = (
                        dict(presenter_channels)
                        if isinstance(presenter_channels, dict)
                        else {}
                    )
                    base_channels["screen"] = str(screen_candidate).strip()
                    if screen_backfill_source == "response_text":
                        base_channels["format"] = "screen_backfill_from_response_v1"
                    else:
                        base_channels["format"] = "screen_backfill_from_tools_v1"
                    presenter_channels = base_channels
                    screen_backfill_applied = True

                    auxiliary_llm_calls.append(
                        {
                            "type": "workflow_stage",
                            "stage": "screen_backfill",
                            "source": screen_backfill_source,
                            "reason": screen_backfill_second_pass_reason,
                            "format": base_channels.get("format"),
                        }
                    )

        screen_backfill_latency_ms = (
            time.perf_counter() - screen_backfill_started_perf
        ) * 1000.0
        if not presenter_mode_requested:
            screen_backfill_status = "skipped"
            screen_backfill_suppression_reason = "presenter_mode_disabled"
        elif not needs_screen_backfill:
            screen_backfill_status = "skipped"
            screen_backfill_suppression_reason = "not_required"
        elif screen_backfill_applied:
            screen_backfill_status = (
                "success"
                if screen_backfill_source == "llm_synthesis"
                else "fallback_success"
            )
            screen_backfill_suppression_reason = None
        elif screen_backfill_error_class:
            screen_backfill_status = "failure"
            screen_backfill_suppression_reason = "model_error"
        else:
            screen_backfill_status = "no_op"
            screen_backfill_suppression_reason = (
                screen_backfill_second_pass_reason or "no_candidates"
            )

        record_response_transformation_event(
            response_transformations,
            event=build_response_transformation_event(
                transform_name="screen_backfill",
                transform_version="v1",
                status=screen_backfill_status,
                input_summary={
                    "presenter_mode_requested": presenter_mode_requested,
                    "screen_tag_present": screen_backfill_screen_tag_present,
                    "needs_backfill": needs_screen_backfill,
                    "tool_message_count": len(tool_messages),
                    "required_screen_json_fence": bool(
                        isinstance(required_screen_json_fence, str)
                        and required_screen_json_fence.strip()
                    ),
                },
                output_summary={
                    "applied": screen_backfill_applied,
                    "presenter_format": (
                        presenter_channels.get("format")
                        if isinstance(presenter_channels, dict)
                        else None
                    ),
                    "reason": screen_backfill_second_pass_reason,
                },
                options_emitted_count=0,
                source_path=screen_backfill_source,
                latency_ms=screen_backfill_latency_ms,
                model_id=screen_backfill_model_id,
                suppression_reason=screen_backfill_suppression_reason,
                error_class=screen_backfill_error_class,
            ),
        )

        def _extract_spoken_only(text: str) -> str | None:
            return _extract_tagged_block(text, "spoken")

        def _coerce_spoken_text(text: object) -> str | None:
            """Best-effort normalisation for narration responses.

            The narration second-pass *should* return <spoken>...</spoken>, but in
            practice models sometimes return plain text. Accept either format.
            """

            if text is None:
                return None

            raw = str(text).strip()
            if not raw:
                return None

            tagged = _extract_spoken_only(raw)
            if tagged:
                return tagged

            import re

            # Strip any accidental tagged blocks and code fences.
            raw = re.sub(r"</?spoken>", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"</?screen>", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
            raw = raw.replace("`", "")
            raw = raw.strip()

            if not raw:
                return None

            # Keep it short for TTS.
            try:
                max_chars = int(os.getenv("VON_NARRATION_SPOKEN_MAX_CHARS", "4000"))
            except ValueError:
                max_chars = 4000
            max_chars = max(200, min(max_chars, 50000))
            if len(raw) > max_chars:
                clipped = raw[:max_chars].rstrip()
                # Prefer clipping at a sentence boundary.
                for sep in (". ", "! ", "? "):
                    cut = clipped.rfind(sep)
                    if cut > 200:
                        clipped = clipped[: cut + 1]
                        break
                raw = clipped.strip()

            return raw or None

        spoken_backfill_started_perf = time.perf_counter()
        spoken_backfill_second_pass_attempted = False
        spoken_backfill_second_pass_reason = None
        spoken_backfill_source = None
        spoken_backfill_model_id = None
        spoken_backfill_error_class = None
        spoken_backfill_applied = False

        # If presenter mode was requested but the model didn't produce a usable
        # <spoken> channel, generate it in a second pass for reliability.
        #
        # Note: The model may emit only <screen>...</screen>. In that case,
        # we still generate <spoken> using the narration prompt rather than
        # falling back to reading the screen/markdown verbatim.
        needs_spoken_backfill = False
        spoken_from_screen_text = False
        if presenter_mode_requested:
            if presenter_channels_missing:
                needs_spoken_backfill = True
                if has_tool_messages:
                    spoken_backfill_second_pass_reason = "missing_spoken"
                else:
                    spoken_backfill_second_pass_reason = "missing_presenter_channels"
            elif isinstance(presenter_channels, dict):
                spoken_value = presenter_channels.get("spoken")
                if not isinstance(spoken_value, str) or not spoken_value.strip():
                    needs_spoken_backfill = True
                    spoken_backfill_second_pass_reason = "missing_spoken"
            else:
                needs_spoken_backfill = True
                spoken_backfill_second_pass_reason = "missing_presenter_channels"

        if needs_spoken_backfill:
            try:
                spoken_backfill_second_pass_attempted = True
                if show_tool_use_progress:
                    _set_tool_progress(
                        progress_scope_key,
                        request_id,
                        {
                            "status": "phase_transition",
                            "phase": "narration",
                            "phase_label": "Generating narration",
                            "request_id": request_id,
                            "workflow_task": "narration_planning",
                        },
                    )
                if isinstance(presenter_channels, dict) and isinstance(
                    presenter_channels.get("screen"), str
                ):
                    screen_text = str(presenter_channels.get("screen") or "").strip()
                else:
                    screen_text = (
                        response_text.strip()
                        if isinstance(response_text, str)
                        else str(response_text)
                    )

                narration_data = {
                    "presenter_mode_requested": presenter_mode_requested,
                    "screen_text": screen_text,
                    "user_prompt": prompt_text,
                    "narration_prompt_text": narration_prompt_text,
                    "narration_prompt_ids": [
                        frag.get("concept_id")
                        for frag in (narration_prompt_fragments or [])
                        if isinstance(frag, dict)
                    ],
                    "presenter_channels": (
                        dict(presenter_channels)
                        if isinstance(presenter_channels, dict)
                        else {}
                    ),
                }

                narration_trace = None
                narration_trace_store = None
                narration_trace_enabled = os.getenv(
                    "VON_WORKFLOWS_TRACE_ENABLED", "0"
                ).lower() in {"1", "true"}
                if narration_trace_enabled:
                    try:
                        narration_trace = WorkflowExecutionTrace(
                            workflow_id=CHAT_NARRATION_WORKFLOW_ID
                        )
                        narration_trace.user_namespace = user_namespace
                        narration_trace_store = insert_workflow_execution_trace
                    except Exception:
                        narration_trace = None
                        narration_trace_store = None
                        narration_trace_enabled = False

                workflow_result = None
                if orchestrator is not None:
                    workflow_result = orchestrator.execute_workflow(
                        CHAT_NARRATION_WORKFLOW_ID,
                        data=narration_data,
                        llm_client=llm_client,
                        model=model_name,
                        user_namespace=user_namespace,
                        auxiliary_system_prompt=auxiliary_system_prompt,
                        trace=narration_trace,
                    )

                if workflow_result is not None:
                    spoken_backfill_source = "workflow"
                    channels = workflow_result.data.get(
                        "presenter_channels", presenter_channels
                    )
                    if isinstance(channels, dict) and channels:
                        presenter_channels = channels
                        workflow_spoken = channels.get("spoken")
                        if (
                            isinstance(workflow_spoken, str)
                            and workflow_spoken.strip()
                        ):
                            spoken_backfill_applied = True
                    if narration_trace_enabled and narration_trace is not None:
                        try:
                            if workflow_result.completed:
                                narration_trace.finish_completed()
                            elif workflow_result.error:
                                narration_trace.finish_failed(workflow_result.error)
                        except Exception:
                            pass
                        if narration_trace_store is not None:
                            try:
                                stored_exec = narration_trace_store(
                                    narration_trace.to_storage_document()
                                )
                                auxiliary_llm_calls.append(
                                    {
                                        "type": "workflow_execution_trace",
                                        "path": "narration",
                                        "workflow_id": CHAT_NARRATION_WORKFLOW_ID,
                                        "execution_id": narration_trace.execution_id,
                                        "stored": bool(stored_exec),
                                        "status": narration_trace.status,
                                    }
                                )
                            except Exception:
                                pass
                else:
                    # Include a small client-reported timing hint for narration generation.
                    # (Non-authoritative; used only for guidance.)
                    timing_hint = None
                    try:
                        from ...services.client_capabilities_service import (
                            get_client_capabilities_snapshot,
                        )

                        snapshot = get_client_capabilities_snapshot()
                        speech = (
                            snapshot.get("speech_synthesis")
                            if isinstance(snapshot, dict)
                            else None
                        )
                        speech = speech if isinstance(speech, dict) else {}
                        raw_settings = speech.get("settings")
                        settings = (
                            raw_settings if isinstance(raw_settings, dict) else {}
                        )

                        preferred = settings.get("preferred_speaking_seconds")
                        maximum = settings.get("max_speaking_seconds")

                        try:
                            preferred_int = (
                                int(preferred) if preferred is not None else None
                            )
                        except Exception:
                            preferred_int = None

                        try:
                            maximum_int = int(maximum) if maximum is not None else None
                        except Exception:
                            maximum_int = None

                        if preferred_int is not None:
                            preferred_int = max(1, min(preferred_int, 600))
                        if maximum_int is not None:
                            maximum_int = max(1, min(maximum_int, 600))

                        effective_preferred = preferred_int
                        if preferred_int is not None and maximum_int is not None:
                            effective_preferred = min(preferred_int, maximum_int)

                        if effective_preferred is not None or maximum_int is not None:
                            timing_hint = (
                                "Speech timing hint (client-reported, non-authoritative): "
                                f"preferred_speaking_seconds={effective_preferred!r}, "
                                f"max_speaking_seconds={maximum_int!r}. "
                                "Aim for about preferred_speaking_seconds seconds and do not exceed max_speaking_seconds."
                            )
                    except Exception:
                        timing_hint = None

                    narration_system = (
                        "You are Von. Produce a short talk track for text-to-speech. "
                        "Return ONLY one block: <spoken>...</spoken>. "
                        "Do not include <screen>. Do not include code blocks. "
                        "Use New Zealand English spelling."
                        + ("\n\n" + timing_hint if timing_hint else "")
                        + (
                            "\n\nVON CHAT NARRATION PROMPT (from Vontology):\n"
                            + narration_prompt_text
                            if narration_prompt_text
                            else ""
                        )
                    )

                    narration_user = (
                        "User message:\n"
                        f"{prompt_text}\n\n"
                        "On-screen content (do not read verbatim if long; summarise):\n"
                        f"{screen_text}\n"
                    )

                    narration_response = llm_client.generate(
                        prompt="Generate <spoken> talk track",
                        context=[
                            {"role": "system", "content": narration_system},
                            {"role": "user", "content": narration_user},
                        ],
                        model=model_name,
                    )
                    spoken_backfill_source = "llm_synthesis"
                    spoken_backfill_model_id = model_name

                    spoken_fallback = _coerce_spoken_text(narration_response)
                    if not spoken_fallback:
                        spoken_fallback = _coerce_spoken_text(screen_text)
                        spoken_from_screen_text = bool(spoken_fallback)
                        if spoken_from_screen_text:
                            spoken_backfill_source = "screen_text_fallback"

                    if spoken_fallback:
                        base_channels = (
                            dict(presenter_channels)
                            if isinstance(presenter_channels, dict)
                            else {}
                        )
                        base_channels["screen"] = screen_text
                        base_channels["spoken"] = spoken_fallback
                        existing_format = base_channels.get("format")
                        # Preserve formats that describe *how the screen* was produced.
                        # For normal tagged responses, surface that narration was added.
                        if existing_format == "tool_results_fallback_v1":
                            base_channels["format"] = existing_format
                        elif (
                            has_tool_messages
                            and isinstance(existing_format, str)
                            and existing_format.startswith("screen_backfill_")
                        ):
                            base_channels["format"] = existing_format
                        elif (
                            not has_tool_messages
                            and isinstance(existing_format, str)
                            and existing_format.startswith("screen_backfill_")
                        ):
                            base_channels["format"] = "narration_fallback_v1"
                        elif (
                            spoken_backfill_second_pass_reason
                            == "missing_presenter_channels"
                        ):
                            base_channels["format"] = "narration_fallback_v1"
                        elif (
                            spoken_from_screen_text
                            and isinstance(existing_format, str)
                            and existing_format.startswith("screen_backfill_")
                        ):
                            base_channels["format"] = existing_format
                        else:
                            base_channels["format"] = "narration_fallback_v1"
                        presenter_channels = base_channels
                        spoken_backfill_applied = True
            except Exception as exc:
                # Defensive: never fail the request just because narration generation failed.
                spoken_backfill_error_class = type(exc).__name__
                presenter_channels = presenter_channels

        spoken_backfill_latency_ms = (
            time.perf_counter() - spoken_backfill_started_perf
        ) * 1000.0
        if not presenter_mode_requested:
            spoken_backfill_status = "skipped"
            spoken_backfill_suppression_reason = "presenter_mode_disabled"
        elif not needs_spoken_backfill:
            spoken_backfill_status = "skipped"
            spoken_backfill_suppression_reason = "not_required"
        elif spoken_backfill_applied:
            spoken_backfill_status = (
                "fallback_success"
                if spoken_backfill_source == "screen_text_fallback"
                else "success"
            )
            spoken_backfill_suppression_reason = None
        elif spoken_backfill_error_class:
            spoken_backfill_status = "failure"
            spoken_backfill_suppression_reason = "model_error"
        else:
            spoken_backfill_status = "no_op"
            spoken_backfill_suppression_reason = (
                spoken_backfill_second_pass_reason or "no_spoken_generated"
            )

        record_response_transformation_event(
            response_transformations,
            event=build_response_transformation_event(
                transform_name="spoken_backfill",
                transform_version="v1",
                status=spoken_backfill_status,
                input_summary={
                    "presenter_mode_requested": presenter_mode_requested,
                    "needs_backfill": needs_spoken_backfill,
                    "reason": spoken_backfill_second_pass_reason,
                    "tool_message_count": len(tool_messages),
                },
                output_summary={
                    "applied": spoken_backfill_applied,
                    "source": spoken_backfill_source,
                    "spoken_from_screen_text": spoken_from_screen_text,
                    "presenter_format": (
                        presenter_channels.get("format")
                        if isinstance(presenter_channels, dict)
                        else None
                    ),
                },
                options_emitted_count=0,
                source_path=spoken_backfill_source,
                latency_ms=spoken_backfill_latency_ms,
                model_id=spoken_backfill_model_id,
                suppression_reason=spoken_backfill_suppression_reason,
                error_class=spoken_backfill_error_class,
            ),
        )

        if presenter_channels is not None:
            # Screen channel becomes the stored/displayed response.
            # Exception: tool-results fallback is debug-only; do not show it as the main chat bubble.
            presenter_format = presenter_channels.get("format")
            screen_text_value = presenter_channels.get("screen")

            if presenter_format == "tool_results_fallback_v1":
                # Keep the model's user-facing response when available.
                # Only strip <spoken> tags if present, or fall back to spoken when the
                # response is empty.
                extracted = _extract_spoken_only(response_text)
                if extracted:
                    response_text = extracted
                elif isinstance(response_text, str) and response_text.strip():
                    pass
                else:
                    spoken_value = presenter_channels.get("spoken")
                    if isinstance(spoken_value, str) and spoken_value.strip():
                        response_text = spoken_value.strip()
            else:
                if isinstance(screen_text_value, str) and screen_text_value.strip():
                    response_text = screen_text_value.strip()

        if tool_invocations:
            current_app.logger.info(
                "[mcp_orchestrator] Tool invocations: %s", tool_invocations
            )

        render_plan_for_display = (
            render_plan_debug if isinstance(render_plan_debug, dict) else None
        )
        screen_element_targets = _extract_screen_element_targets_from_render_plan(
            render_plan_for_display
        )
        screen_element_reason_codes = _extract_screen_element_reason_codes_from_render_plan(
            render_plan_for_display
        )
        screen_table_elements = _extract_screen_table_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_workflow_elements = _extract_screen_workflow_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_task_view_elements = _extract_screen_task_view_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_calendar_elements = _extract_screen_calendar_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_document_elements = _extract_screen_document_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_kanban_elements = _extract_screen_kanban_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_timeline_elements = _extract_screen_timeline_elements_from_render_plan(
            render_plan_for_display,
            screen_element_targets=screen_element_targets,
        )
        screen_relation_graph_elements = (
            _extract_screen_relation_graph_elements_from_render_plan(
                render_plan_for_display,
                screen_element_targets=screen_element_targets,
            )
        )
        screen_relation_truth_state_elements = (
            _extract_screen_relation_truth_state_elements_from_render_plan(
                render_plan_for_display,
                screen_element_targets=screen_element_targets,
            )
        )
        display_elements_contract = build_turn_display_elements(
            response_text=response_text,
            presenter_channels=(
                presenter_channels if isinstance(presenter_channels, dict) else None
            ),
            screen_table_elements=screen_table_elements,
            screen_workflow_elements=screen_workflow_elements,
            screen_task_view_elements=screen_task_view_elements,
            screen_calendar_elements=screen_calendar_elements,
            screen_document_elements=screen_document_elements,
            screen_kanban_elements=screen_kanban_elements,
            screen_timeline_elements=screen_timeline_elements,
            screen_relation_graph_elements=screen_relation_graph_elements,
            screen_relation_truth_state_elements=screen_relation_truth_state_elements,
            supplemental_reason_codes=screen_element_reason_codes,
            required_screen_json_fence=required_screen_json_fence,
            screen_backfill_second_pass_attempted=screen_backfill_second_pass_attempted,
            screen_backfill_second_pass_reason=screen_backfill_second_pass_reason,
            spoken_backfill_second_pass_attempted=spoken_backfill_second_pass_attempted,
            spoken_backfill_second_pass_reason=spoken_backfill_second_pass_reason,
        )

        # Build LLM debug information FIRST (before saving to history)
        # so we can persist it alongside the assistant message
        # NOTE: Only include the NEW messages for this turn to avoid exponential token growth
        # as the full context would include all previous turns' debug data
        current_turn_messages = [{"role": "user", "content": prompt_text}]
        if tool_messages:
            current_turn_messages.extend(tool_messages)

        # Calculate context statistics for visibility.
        # If the internal orchestrator is enabled, it augments and trims the context
        # before sending it to the LLM, so report stats for the *actual* sent context.
        sent_context_for_stats = enhanced_context
        if orchestrator is not None:
            build_augmented = getattr(orchestrator, "_build_augmented_context", None)
            if callable(build_augmented):
                try:
                    sent_context_for_stats = build_augmented(
                        enhanced_context,
                        user_namespace=user_namespace,
                        auxiliary_system_prompt=auxiliary_system_prompt,
                    )
                except Exception:
                    sent_context_for_stats = enhanced_context

        sent_context_stats_messages: list[dict] = enhanced_context
        if isinstance(sent_context_for_stats, list):
            normalised_messages: list[dict] = []
            for msg in sent_context_for_stats:
                if isinstance(msg, dict):
                    normalised_messages.append(msg)
                    continue
                try:
                    normalised_messages.append(dict(msg))
                except Exception:
                    continue
            if normalised_messages:
                sent_context_stats_messages = normalised_messages

        buttonify_started_perf = time.perf_counter()
        buttonify_options: list[str] = []
        buttonify_meta: dict[str, Any] | None = None
        buttonify_workflow_contract: dict[str, Any] | None = None
        buttonify_enabled = get_buttonify_model_enabled()
        buttonify_allowed = (
            not current_app.testing
            and not os.getenv("PYTEST_CURRENT_TEST")
            and (
                orchestrator is None or hasattr(orchestrator, "_run_llm_with_fallbacks")
            )
        )
        buttonify_workflow_available = bool(
            orchestrator is not None
            and hasattr(orchestrator, "execute_workflow")
            and hasattr(orchestrator, "_run_llm_with_fallbacks")
        )
        buttonify_preflight_enabled = False
        buttonify_model_used = model_name
        buttonify_source = "none"
        buttonify_prompt_id = None
        buttonify_prompt_truncated = False
        buttonify_status = "skipped"
        buttonify_error_class = None
        buttonify_suppression_reason = None
        buttonify_model_attempted = False
        buttonify_workflow_used = False

        if not buttonify_enabled:
            buttonify_suppression_reason = "buttonify_disabled"
        elif not buttonify_allowed:
            buttonify_suppression_reason = "buttonify_not_allowed"
        elif not isinstance(response_text, str) or not response_text.strip():
            buttonify_suppression_reason = "empty_response"
        else:
            buttonify_status = "no_op"
            buttonify_preflight_enabled = get_buttonify_heuristic_preflight_enabled()
            if show_tool_use_progress:
                _set_tool_progress(
                    progress_scope_key,
                    request_id,
                    {
                        "status": "workflow",
                        "request_id": request_id,
                        "workflow_task": "buttonify",
                    },
                )

            if buttonify_workflow_available:
                buttonify_workflow_used = True
                buttonify_workflow_result = None
                policy_state = None
                try:
                    policy_state, _ = orchestrator._load_workflow_model_policy(
                        request_language
                    )
                except Exception:
                    policy_state = None

                try:
                    buttonify_workflow_result = orchestrator.execute_workflow(
                        CHAT_BUTTONIFY_WORKFLOW_ID,
                        data={
                            "screen_text": response_text,
                            "user_prompt": prompt_text,
                            "buttonify_enabled": buttonify_enabled,
                            "buttonify_allowed": buttonify_allowed,
                            "buttonify_preflight_enabled": buttonify_preflight_enabled,
                            "buttonify_prompt_ids": list(BUTTONIFY_PROMPT_IDS),
                            "buttonify_prompt_template": BUTTONIFY_PROMPT_TEMPLATE,
                            "default_model": model_name,
                            "policy_state": policy_state,
                            "record_llm_call": _record_stage_llm_call,
                            "llm_calls_log": llm_interaction["calls"],
                            "aux_llm_calls": auxiliary_llm_calls,
                            "user_concept_id": user_concept_id,
                            "org_concept_id": org_concept_id,
                            "workflow_episode_stage": "buttonify",
                        },
                        llm_client=llm_client,
                        model=model_name,
                        user_namespace=user_namespace,
                        auxiliary_system_prompt=auxiliary_system_prompt,
                        trace=None,
                        conversation_session_id=session_id,
                        turn_id=request_id,
                        episode_source="chat_turn_workflow",
                    )
                except Exception as exc:
                    buttonify_error_class = type(exc).__name__
                    buttonify_workflow_result = None

                workflow_payload: Mapping[str, Any] = {}
                if buttonify_workflow_result is not None and isinstance(
                    buttonify_workflow_result.data, Mapping
                ):
                    workflow_payload = buttonify_workflow_result.data

                raw_options = workflow_payload.get("buttonify_options")
                if isinstance(raw_options, list):
                    buttonify_options = dedupe_buttonify_options(raw_options)

                if isinstance(workflow_payload.get("buttonify_source"), str):
                    buttonify_source = workflow_payload.get("buttonify_source", "none")
                if isinstance(workflow_payload.get("buttonify_prompt_id"), str):
                    buttonify_prompt_id = workflow_payload.get("buttonify_prompt_id")
                buttonify_prompt_truncated = bool(
                    workflow_payload.get("buttonify_prompt_truncated")
                )
                if isinstance(workflow_payload.get("buttonify_status"), str):
                    buttonify_status = workflow_payload.get("buttonify_status", "no_op")
                if isinstance(workflow_payload.get("buttonify_error_class"), str):
                    buttonify_error_class = workflow_payload.get("buttonify_error_class")
                if isinstance(workflow_payload.get("buttonify_model_used"), str):
                    buttonify_model_used = workflow_payload.get("buttonify_model_used")
                buttonify_model_attempted = bool(
                    workflow_payload.get("buttonify_model_attempted")
                )
                if isinstance(workflow_payload.get("buttonify_suppression_reason"), str):
                    buttonify_suppression_reason = workflow_payload.get(
                        "buttonify_suppression_reason"
                    )
                contract_payload = workflow_payload.get("output_transformation_contract")
                if isinstance(contract_payload, Mapping):
                    buttonify_workflow_contract = dict(contract_payload)

                # Deterministic guardrail: if workflow output is empty/invalid,
                # always attempt heuristic fallback before surfacing no-op.
                if not buttonify_options:
                    fallback_options = _extract_buttonify_options_heuristic(response_text)
                    if fallback_options:
                        buttonify_options = fallback_options
                        buttonify_source = "heuristic_fallback"
                        buttonify_status = "fallback_success"
                        buttonify_suppression_reason = "fallback_heuristic_used"
                        buttonify_workflow_contract = None
            else:
                # Keep deterministic behaviour when workflow execution is not available.
                if buttonify_preflight_enabled:
                    preflight_options = _extract_buttonify_options_heuristic(response_text)
                    if preflight_options:
                        buttonify_options = preflight_options
                        buttonify_source = "heuristic_preflight"

                if not buttonify_options:
                    buttonify_model_attempted = True
                    try:
                        buttonify_prompt = BUTTONIFY_PROMPT_TEMPLATE.format(
                            user_message=prompt_text,
                            assistant_response=response_text,
                        )
                    except Exception:
                        buttonify_prompt = str(BUTTONIFY_PROMPT_TEMPLATE)

                    llm_start = time.perf_counter()
                    buttonify_response = None
                    try:
                        buttonify_response = llm_client.generate(
                            prompt=buttonify_prompt,
                            context=[],
                            model=buttonify_model_used,
                        )
                        _record_stage_llm_call(
                            call_type="llm.generate",
                            model_name=buttonify_model_used,
                            duration_ms=(time.perf_counter() - llm_start) * 1000.0,
                            usage=None,
                            note="Buttonify quick-reply extraction (workflow unavailable).",
                            stage="buttonify",
                        )
                    except Exception as exc:
                        buttonify_error_class = type(exc).__name__
                        buttonify_response = None

                    buttonify_options = parse_buttonify_options_json(buttonify_response)
                    buttonify_source = "llm" if buttonify_options else "none"
                    if not buttonify_options:
                        fallback_options = _extract_buttonify_options_heuristic(
                            response_text
                        )
                        if fallback_options:
                            buttonify_options = fallback_options
                            buttonify_source = "heuristic_fallback"

                if not buttonify_workflow_available and not buttonify_options:
                    buttonify_suppression_reason = (
                        buttonify_suppression_reason or "workflow_unavailable"
                    )

            if buttonify_source in {"heuristic_preflight", "llm"}:
                buttonify_status = "success"
            elif buttonify_source == "heuristic_fallback":
                buttonify_status = "fallback_success"
                buttonify_suppression_reason = "fallback_heuristic_used"
            else:
                buttonify_status = "no_op"
                if buttonify_error_class:
                    buttonify_suppression_reason = "model_error"
                elif not buttonify_suppression_reason:
                    buttonify_suppression_reason = "no_candidates"

            buttonify_meta = {
                "enabled": True,
                "model": buttonify_model_used,
                "options": buttonify_options,
                "source": buttonify_source,
                "prompt_id": buttonify_prompt_id,
                "prompt_truncated": buttonify_prompt_truncated,
                "heuristic_preflight_enabled": buttonify_preflight_enabled,
                "workflow_used": buttonify_workflow_used,
                "workflow_available": buttonify_workflow_available,
            }
            if buttonify_workflow_contract is not None:
                buttonify_meta["workflow_contract"] = buttonify_workflow_contract

        buttonify_latency_ms = (time.perf_counter() - buttonify_started_perf) * 1000.0
        record_response_transformation_event(
            response_transformations,
            event=build_response_transformation_event(
                transform_name="buttonify",
                transform_version="v1",
                status=buttonify_status,
                input_summary={
                    "buttonify_enabled": buttonify_enabled,
                    "buttonify_allowed": buttonify_allowed,
                    "heuristic_preflight_enabled": buttonify_preflight_enabled,
                    "response_text_chars": (
                        len(response_text) if isinstance(response_text, str) else 0
                    ),
                },
                output_summary={
                    "options": list(buttonify_options),
                    "source": buttonify_source,
                    "prompt_id": buttonify_prompt_id,
                    "prompt_truncated": buttonify_prompt_truncated,
                },
                options_emitted_count=len(buttonify_options),
                source_path=buttonify_source,
                latency_ms=buttonify_latency_ms,
                model_id=buttonify_model_used if buttonify_model_attempted else None,
                suppression_reason=buttonify_suppression_reason,
                error_class=buttonify_error_class,
            ),
        )

        context_stats = _calculate_context_stats(sent_context_stats_messages)

        # stored_context should reflect the persisted user/session history when authenticated,
        # not the unauthenticated in-memory CONTEXT list.
        if user_concept_id:
            try:
                persisted_history = chat_history_service.get_chat_history(
                    user_concept_id, session_id
                )
            except Exception:
                persisted_history = []
            current_context_stats = _calculate_context_stats(persisted_history)
        else:
            current_context_stats = _calculate_context_stats(
                current_app.config["CONTEXT"]
            )

        # Calculate tool statistics if tools were used
        tool_stats = _calculate_tool_stats(tool_messages) if tool_messages else None

        # JVNAUTOSCI-958: cartouche-style concept metadata for references that appear
        # in the context actually sent to the model (prior user/assistant turns).
        # This makes reference status inspectable in the ensuing turn's debug payload.
        try:
            context_concept_references = build_context_concept_reference_metadata(
                sent_context_stats_messages,
                source="sent_context_user_assistant",
            )
        except Exception as exc:
            current_app.logger.debug(
                "Failed to build context concept-reference metadata: %s",
                exc,
                exc_info=True,
            )
            context_concept_references = {
                "source": "sent_context_user_assistant",
                "metadata_version": 1,
                "message_roles": ["user", "assistant"],
                "messages_scanned": 0,
                "concept_count": 0,
                "concept_count_capped": False,
                "max_concepts": None,
                "include_direct_supertypes": False,
                "max_direct_supertypes": 0,
                "concepts": [],
                "error": str(exc),
            }

        # Capture a compact summary of the tool catalogue that the agent was shown.
        # This improves trace transparency without storing the full system prompt.
        tool_catalogue_summary = None
        if gateway is not None:
            try:
                methods_snapshot = gateway.describe_methods()
                if isinstance(methods_snapshot, dict):
                    method_names = sorted(
                        [
                            name
                            for name in methods_snapshot.keys()
                            if isinstance(name, str)
                        ]
                    )
                    try:
                        import hashlib
                        import json

                        digest = hashlib.sha256(
                            json.dumps(
                                method_names,
                                separators=(",", ":"),
                                ensure_ascii=True,
                            ).encode("utf-8")
                        ).hexdigest()
                    except Exception:
                        digest = None

                    sample_cap = 25
                    tool_catalogue_summary = {
                        "method_count": len(method_names),
                        "sha256": digest,
                        "sample": method_names[:sample_cap],
                        "sample_truncated": len(method_names) > sample_cap,
                    }
            except Exception:
                tool_catalogue_summary = None

        try:
            applied_max_tool_invocations = int(get_internal_mcp_max_tool_invocations())
        except Exception:
            applied_max_tool_invocations = None

        try:
            applied_tool_batch_cap = int(get_internal_mcp_tool_batch_cap())
        except Exception:
            applied_tool_batch_cap = None

        if show_tool_use_progress:
            final_progress_payload = _build_terminal_tool_progress_payload(
                request_id=request_id,
                aux_calls=auxiliary_llm_calls,
            )
            _stop_tool_progress_heartbeat(
                progress_heartbeat_stop_event, progress_heartbeat_thread
            )
            _set_tool_progress(
                progress_scope_key,
                request_id,
                final_progress_payload,
            )

        tool_progress_snapshot = _snapshot_tool_progress_for_request(
            progress_scope_key, request_id
        )
        turn_execution_diagnostics = _build_turn_execution_diagnostics(
            request_id=request_id,
            prompt_text=prompt_text,
            elapsed_ms=(time.perf_counter() - request_start_perf) * 1000.0,
            tool_progress_state=tool_progress_snapshot,
            workflow_discovery=workflow_discovery_result,
            llm_calls=llm_interaction["calls"],
        )

        workflow_use_episodes = [
            entry
            for entry in auxiliary_llm_calls
            if isinstance(entry, dict)
            and str(entry.get("type", "")).strip() == "workflow_use_episode"
        ]

        llm_debug_info = {
            "interaction_timestamp_utc": interaction_timestamp_utc,
            "request_id": request_id,
            "model": model_name,
            "llm_interaction": {
                **llm_interaction,
                "server_elapsed_ms": (time.perf_counter() - request_start_perf)
                * 1000.0,
            },
            "messages": current_turn_messages,
            "response": response_text,
            "presenter_channels": presenter_channels,
            "screen_backfill_second_pass_attempted": screen_backfill_second_pass_attempted,
            "screen_backfill_second_pass_reason": screen_backfill_second_pass_reason,
            "display_elements_screen_fence_compat_enabled": (
                screen_fence_compat_enabled if presenter_mode_requested else None
            ),
            "spoken_backfill_second_pass_attempted": spoken_backfill_second_pass_attempted,
            "spoken_backfill_second_pass_reason": spoken_backfill_second_pass_reason,
            "user_prompt": user_prompt_debug,
            "namespace_report": namespace_report,
            "internal_mcp": {
                "gateway_present": gateway is not None,
                "gateway_enabled": (
                    bool(getattr(gateway, "enabled", False))
                    if gateway is not None
                    else False
                ),
                "orchestrator_present": orchestrator is not None,
                "execution_caps": {
                    "max_tool_invocations": applied_max_tool_invocations,
                    "tool_batch_cap": applied_tool_batch_cap,
                },
                "tool_use_progress": {
                    "enabled": show_tool_use_progress,
                    "request_id": request_id,
                    "diagnostic_summary": _build_tool_progress_compact_summary(
                        tool_progress_snapshot
                    ),
                },
                "tool_catalogue": tool_catalogue_summary,
            },
            "context_stats": {
                "sent_to_llm": context_stats,  # What was actually sent this turn
                "stored_context": current_context_stats,  # Current state after this turn
            },
            "context_concept_references": context_concept_references,
            "tool_stats": tool_stats,  # MCP tool result statistics
            "tool_invocations": (
                [
                    {
                        "method": inv.get("tool") or inv.get("method", "unknown"),
                        "arguments": inv.get("payload") or inv.get("arguments", {}),
                        "error": inv.get("error"),
                    }
                    for inv in tool_invocations
                ]
                if tool_invocations
                else []
            ),
            "aux_llm_calls": auxiliary_llm_calls,
            "workflow_use_episodes": workflow_use_episodes,
            "buttonify": buttonify_meta,
            "response_transformations": response_transformations,
            # JVNAUTOSCI-1076: Workflow discovery results for Thinking context
            "workflow_discovery": workflow_discovery_result,
            "workflow_routing": workflow_routing_info,
            "display_elements": display_elements_contract,
            "turn_execution_diagnostics": turn_execution_diagnostics,
        }
        if isinstance(render_plan_debug, dict):
            llm_debug_info["render_plan"] = dict(render_plan_debug)

        llm_debug_info = _finalise_llm_debug_info(
            llm_debug_info=llm_debug_info,
            prompt_text=prompt_text,
            response_text=response_text,
            session_id=session_id,
            namespace=user_namespace,
            user_id=history_user_id or user_concept_id,
            org_id=org_concept_id,
            workflow_discovery=workflow_discovery_result,
            workflow_routing=workflow_routing_info,
        )

        # Now save messages to history/context with debug info
        # Truncate large tool results to prevent context explosion
        truncated_tool_messages = _truncate_large_tool_results(
            tool_messages, max_tool_content_chars=5000
        )

        if history_user_id:
            chat_history_service.add_message_to_history(
                history_user_id,
                session_id,
                {
                    "role": "user",
                    "content": prompt_text,
                    "author_user_id": user_concept_id,
                },
            )
            for tool_msg in truncated_tool_messages:
                chat_history_service.add_message_to_history(
                    history_user_id, session_id, tool_msg
                )
            # Save assistant message WITH debug data
            chat_history_service.add_message_to_history(
                history_user_id,
                session_id,
                {"role": "assistant", "content": response_text},
                llm_debug_data=llm_debug_info,
            )
        else:
            current_app.config["CONTEXT"].append(
                {"role": "user", "content": prompt_text}
            )
            for tool_msg in truncated_tool_messages:
                current_app.config["CONTEXT"].append(tool_msg)
            current_app.config["CONTEXT"].append(
                {"role": "assistant", "content": response_text}
            )

        # Limit overall context size to prevent unbounded growth
        current_app.config["CONTEXT"] = _limit_context_size(
            current_app.config["CONTEXT"], max_messages=20
        )

        return jsonify(
            {
                "request_id": request_id,
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
                "display_elements": display_elements_contract,
                "rag_trace": rag_trace,
            }
        )
    except Exception as e:
        print(f"Error during generation: {e}")  # Log error server-side
        # Return error with debug info showing the current turn only (not full context)
        # to avoid exponential token growth in debug data

        # Calculate context stats if available
        context_stats = None
        if "enhanced_context" in locals():
            try:
                context_stats = {
                    "sent_to_llm": _calculate_context_stats(enhanced_context)
                }
            except:
                pass

        if "show_tool_use_progress" in locals() and show_tool_use_progress:
            try:
                _stop_tool_progress_heartbeat(
                    progress_heartbeat_stop_event, progress_heartbeat_thread
                )
                _set_tool_progress(
                    _get_tool_progress_scope_key(),
                    request_id if "request_id" in locals() else "unknown",
                    {
                        "status": "error",
                        "request_id": (
                            request_id if "request_id" in locals() else "unknown"
                        ),
                        "error": str(e),
                    },
                )
            except Exception:
                pass

        error_tool_progress_snapshot = _snapshot_tool_progress_for_request(
            progress_scope_key if "progress_scope_key" in locals() else None,
            request_id if "request_id" in locals() else None,
        )
        error_workflow_discovery = (
            workflow_discovery_result
            if (
                "workflow_discovery_result" in locals()
                and isinstance(workflow_discovery_result, dict)
            )
            else None
        )
        error_elapsed_ms = (
            (time.perf_counter() - request_start_perf) * 1000.0
            if "request_start_perf" in locals()
            else None
        )
        error_workflow_routing = (
            workflow_routing_info
            if (
                "workflow_routing_info" in locals()
                and isinstance(workflow_routing_info, dict)
            )
            else None
        )

        error_debug_info = {
            "interaction_timestamp_utc": interaction_timestamp_utc,
            "request_id": request_id,
            "model": model_name if "model_name" in locals() else "unknown",
            "messages": [{"role": "user", "content": prompt_text}],
            "response": None,
            "error": str(e),
            "context_stats": context_stats,
            "user_prompt": (
                user_prompt_debug if "user_prompt_debug" in locals() else None
            ),
            "tool_invocations": [],
            "response_transformations": (
                response_transformations
                if (
                    "response_transformations" in locals()
                    and isinstance(response_transformations, dict)
                )
                else build_response_transformation_telemetry_payload(
                    request_id=request_id if "request_id" in locals() else None
                )
            ),
            "turn_execution_diagnostics": _build_turn_execution_diagnostics(
                request_id=request_id if "request_id" in locals() else None,
                prompt_text=prompt_text if "prompt_text" in locals() else None,
                elapsed_ms=error_elapsed_ms,
                tool_progress_state=error_tool_progress_snapshot,
                workflow_discovery=error_workflow_discovery,
            ),
        }
        error_debug_info = _finalise_llm_debug_info(
            llm_debug_info=error_debug_info,
            prompt_text=prompt_text if "prompt_text" in locals() else None,
            response_text=None,
            session_id=session_id if "session_id" in locals() else None,
            namespace=user_namespace if "user_namespace" in locals() else None,
            user_id=history_user_id if "history_user_id" in locals() else None,
            org_id=org_concept_id if "org_concept_id" in locals() else None,
            workflow_discovery=error_workflow_discovery,
            workflow_routing=error_workflow_routing,
        )
        body = {
            "request_id": request_id,
            "error": str(e),
            "llm_debug": error_debug_info,
        }
        if "rag_trace" in locals():
            body["rag_trace"] = rag_trace
        if "namespace_report" in locals():
            body["namespace_report"] = namespace_report
        return jsonify(body), 500


@von_bp.route("/history", methods=["GET"])
def history():
    """Retrieve chat history segments for the current user."""
    # SECURITY: derive user id server-side
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    requested_session_id = request.args.get("session_id")
    if isinstance(requested_session_id, str) and requested_session_id.strip():
        session_id = requested_session_id.strip()
    else:
        # Ensure session_id exists (create if needed for the current session context)
        if "session_id" not in session:
            session["session_id"] = str(uuid.uuid4())
        session_id = session["session_id"]

    if not user_concept_id:
        return jsonify(
            {
                "history": [],
                "segments_returned": 0,
                "total_segments": 0,
                "has_more_history": False,
            }
        )

    requested_segments = request.args.get("segments", default=1, type=int)
    segment_count = max(1, requested_segments)
    segment_size = request.args.get("segment_size", default=None, type=int)
    if not segment_size or segment_size <= 0:
        segment_size = None
    tail_limit = request.args.get("tail_limit", default=None, type=int)
    if tail_limit is not None and tail_limit <= 0:
        tail_limit = None
    include_debug_raw = request.args.get("include_debug")
    include_debug = True
    if isinstance(include_debug_raw, str):
        parsed = include_debug_raw.strip().lower()
        if parsed in ("0", "false", "no", "n", "off"):
            include_debug = False
        elif parsed in ("1", "true", "yes", "y", "on"):
            include_debug = True
    history_tail_limit = None
    if isinstance(tail_limit, int) and tail_limit > 0:
        history_tail_limit = tail_limit
    elif isinstance(segment_size, int) and segment_size > 0:
        history_tail_limit = segment_size * max(segment_count, 1)

    try:
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        owner_user_id = user_concept_id
        shared_invite = None

        owner_user_id, shared_invite = _resolve_shared_conversation_owner(
            user_concept_id=user_concept_id, session_id=session_id
        )
        if shared_invite:
            if not owner_user_id:
                return jsonify({"error": "Not authorised for conversation"}), 403
        elif not chat_history_service.has_chat_history_session(
            user_concept_id, session_id, namespace=namespace
        ):
            return jsonify({"error": "Not authorised for conversation"}), 403

        if not isinstance(owner_user_id, str) or not owner_user_id:
            return jsonify({"error": "Not authorised for conversation"}), 403

        if shared_invite and user_concept_id and owner_user_id != user_concept_id:
            owner_history = chat_history_service.get_chat_history(
                owner_user_id, session_id
            )
            invitee_history = chat_history_service.get_chat_history(
                user_concept_id, session_id
            )
            owner_history = _apply_default_author(owner_history, owner_user_id)
            invitee_history = _apply_default_author(invitee_history, user_concept_id)
            merged_history = _merge_shared_histories(owner_history, invitee_history)

            history_truncated = False
            if history_tail_limit and len(merged_history) > history_tail_limit:
                merged_history = merged_history[-history_tail_limit:]
                history_truncated = True

            segments = chat_history_service._split_history_into_segments_with_locations(
                merged_history,
                session_id=session_id,
                include_debug=include_debug,
                owner_user_id=owner_user_id,
            )
            segments = chat_history_service._chunk_history_segments(
                segments, segment_size
            )
            meta = {"history_truncated": history_truncated}
        else:
            if shared_invite:
                owner_namespace = (
                    _derive_namespace_for_user_org(
                        owner_user_id,
                        shared_invite.get("organisation_concept_id"),
                    )
                    or namespace
                )
            else:
                # JVNAUTOSCI-1011: Use window-context namespace, not flask session
                owner_namespace = namespace
            # Fallback if namespace is None (e.g. legacy sessions without org)
            if not owner_namespace:
                owner_namespace = chat_history_service.resolve_chat_history_namespace(
                    owner_user_id
                )
            segments_result = chat_history_service.get_chat_history_segments(
                owner_user_id,
                session_id,
                include_locations=True,
                namespace=owner_namespace,
                segment_size=segment_size,
                include_debug=include_debug,
                history_tail_limit=history_tail_limit,
                return_meta=True,
            )
            if isinstance(segments_result, tuple):
                segments, meta = segments_result
            else:
                segments = segments_result
                meta = {"history_truncated": False}
        total_segments = len(segments)

        if total_segments == 0:
            return jsonify(
                {
                    "history": [],
                    "segments_returned": 0,
                    "total_segments": 0,
                    "has_more_history": False,
                }
            )

        segment_count = min(segment_count, total_segments)
        selected_segments = segments[-segment_count:]
        flattened_history = [msg for segment in selected_segments for msg in segment]
        segments_returned = len(selected_segments)
        history_truncated = bool(meta.get("history_truncated"))
        has_more = history_truncated or segments_returned < total_segments
        if history_truncated and total_segments <= segments_returned:
            total_segments = segments_returned + 1

        # Shared sessions are read from the conversation owner's history so all
        # participants see the same canonical thread.

        return jsonify(
            {
                "history": flattened_history,
                "segments_returned": segments_returned,
                "total_segments": total_segments,
                "has_more_history": has_more,
            }
        )
    except Exception as e:
        print(f"Error retrieving history: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/history/debug", methods=["GET"])
def history_debug():
    """Retrieve stored LLM debug data for a specific history entry."""
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return jsonify({"error": "Not authenticated"}), 401

    session_id = request.args.get("session_id") or session.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return jsonify({"error": "session_id required"}), 400

    history_index = request.args.get("history_index", default=None, type=int)
    if history_index is None or history_index < 0:
        return jsonify({"error": "history_index required"}), 400
    view = str(request.args.get("view") or "").strip().lower()

    try:
        session_id = session_id.strip()
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        owner_user_id = user_concept_id
        shared_invite = None

        has_session = chat_history_service.has_chat_history_session(
            user_concept_id, session_id, namespace=namespace
        )
        print(
            f"[history/debug] user={user_concept_id}, session={session_id}, namespace={namespace}, has_session={has_session}, history_index={history_index}"
        )
        if not has_session:
            owner_user_id, shared_invite = _resolve_shared_conversation_owner(
                user_concept_id=user_concept_id, session_id=session_id
            )
            if not owner_user_id:
                return jsonify({"error": "Not authorised for conversation"}), 403

        owner_namespace = _derive_namespace_for_user_org(
            owner_user_id,
            shared_invite.get("organisation_concept_id") if shared_invite else None,
        ) or chat_history_service.resolve_chat_history_namespace(owner_user_id)
        print(
            f"[history/debug] owner_user_id={owner_user_id}, owner_namespace={owner_namespace}"
        )
        debug_data = chat_history_service.get_chat_history_debug_entry(
            user_id=owner_user_id,
            session_id=session_id,
            history_index=history_index,
            namespace=owner_namespace,
        )
        # Fallback: if namespace mismatch (e.g. org session accessed without org context),
        # retry without namespace restriction since we've already verified access above.
        if not debug_data:
            print(f"[history/debug] Retrying without namespace restriction")
            debug_data = chat_history_service.get_chat_history_debug_entry(
                user_id=owner_user_id,
                session_id=session_id,
                history_index=history_index,
                namespace=None,
            )
        print(f"[history/debug] debug_data={bool(debug_data)}")
        if not debug_data:
            return jsonify(
                {
                    "success": False,
                    "error": "debug_not_available",
                    "history_location": {
                        "session_id": session_id,
                        "history_index": history_index,
                    },
                }
            )
        if view in {"transformations", "response_transformations"}:
            transformation_payload = None
            if isinstance(debug_data, dict):
                candidate = debug_data.get("response_transformations")
                if isinstance(candidate, dict):
                    transformation_payload = candidate
            if transformation_payload is None:
                transformation_payload = build_response_transformation_telemetry_payload(
                    request_id=(
                        debug_data.get("request_id")
                        if isinstance(debug_data, dict)
                        else None
                    )
                )
            transformations = transformation_payload.get("transformations")
            return jsonify(
                {
                    "success": True,
                    "history_location": {
                        "session_id": session_id,
                        "history_index": history_index,
                    },
                    "response_transformations": transformation_payload,
                    "transformations_count": (
                        len(transformations) if isinstance(transformations, list) else 0
                    ),
                }
            )
        return jsonify(
            {
                "success": True,
                "history_location": {
                    "session_id": session_id,
                    "history_index": history_index,
                },
                "llm_debug_data": debug_data,
            }
        )
    except Exception as e:
        print(f"Error retrieving history debug data: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/history/backfill_spoken", methods=["POST"])
def history_backfill_spoken():
    """Generate and persist missing <spoken> talk track for a stored assistant turn.

    This is intended for legacy history turns where presenter channels were not
    generated or persisted at the time. The backfill:
    - requires authentication
    - does not touch session `updated_at` (so it won't reorder session recency)
    """

    from ...security.access_control import get_effective_user_concept_id

    user_concept_id = get_effective_user_concept_id()
    if not user_concept_id:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    history_location = data.get("history_location")
    if not isinstance(history_location, dict):
        history_location = {}

    target_session_id = history_location.get("session_id") or data.get("session_id")
    history_index = history_location.get("history_index")
    if history_index is None:
        history_index = data.get("history_index")

    if not isinstance(target_session_id, str) or not target_session_id.strip():
        return jsonify({"error": "session_id is required"}), 400
    if not isinstance(history_index, int) or history_index < 0:
        return jsonify({"error": "history_index must be a non-negative integer"}), 400

    force = bool(data.get("force"))

    target_session_id = target_session_id.strip()
    owner_user_id = user_concept_id
    try:
        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        if not chat_history_service.has_chat_history_session(
            user_concept_id, target_session_id, namespace=namespace
        ):
            owner_user_id, shared_invite = _resolve_shared_conversation_owner(
                user_concept_id=user_concept_id, session_id=target_session_id
            )
            if not owner_user_id:
                return jsonify({"error": "Not authorised for conversation"}), 403
    except Exception:
        owner_user_id = user_concept_id

    # Fetch the stored assistant message and associated previous user prompt.
    try:
        chat_history_coll = chat_history_service.get_chat_history_collection_service()
        if chat_history_coll is None:
            return (
                jsonify({"error": "Could not connect to chat history collection."}),
                500,
            )

        doc = chat_history_coll.find_one(
            {"user_id": owner_user_id, "session_id": target_session_id},
            {"history": 1},
        )
        history = (doc or {}).get("history") or []
        if not isinstance(history, list) or history_index >= len(history):
            return jsonify({"error": "History entry not found"}), 404

        entry = history[history_index]
        if not isinstance(entry, dict) or entry.get("role") != "assistant":
            return (
                jsonify({"error": "Target history entry is not an assistant message"}),
                400,
            )

        screen_text = entry.get("content")
        if not isinstance(screen_text, str) or not screen_text.strip():
            return jsonify({"error": "Assistant message has no content"}), 400
        screen_text = screen_text.strip()

        existing_debug = entry.get("llm_debug_data")
        existing_channels = None
        if isinstance(existing_debug, dict):
            existing_channels = existing_debug.get("presenter_channels")
        if not force and isinstance(existing_channels, dict):
            spoken_existing = existing_channels.get("spoken")
            if isinstance(spoken_existing, str) and spoken_existing.strip():
                existing_display_elements = (
                    existing_debug.get("display_elements")
                    if isinstance(existing_debug, dict)
                    else None
                )
                if not isinstance(existing_display_elements, dict):
                    existing_display_elements = build_turn_display_elements(
                        response_text=screen_text,
                        presenter_channels=existing_channels,
                    )
                return jsonify(
                    {
                        "status": "already_present",
                        "presenter_channels": existing_channels,
                        "display_elements": existing_display_elements,
                        "updated": False,
                    }
                )

        prompt_text = ""
        for i in range(history_index - 1, -1, -1):
            msg = history[i]
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "system" and msg.get("content") == "__RESET__":
                # Stop at reset boundary.
                break
            if msg.get("role") == "user":
                candidate = msg.get("content")
                if isinstance(candidate, str) and candidate.strip():
                    prompt_text = candidate.strip()
                break

    except Exception as e:
        return jsonify({"error": f"Failed reading history: {e}"}), 500

    # Load narration prompt fragments (best-effort).
    narration_prompt_text = None
    try:
        from ...services.chat_auxiliary_prompt_service import (
            get_user_specific_prompt_fragments,
        )

        narration_prompt_fragments = get_user_specific_prompt_fragments(
            user_concept_id,
            prompt_types=("#V#von_chat_narration_prompt",),
        )
        narration_texts = []
        for frag in narration_prompt_fragments:
            if not isinstance(frag, dict):
                continue
            content = frag.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            narration_texts.append(content)
        narration_prompt_text = "\n\n".join(
            text.strip() for text in narration_texts if text and text.strip()
        )
        narration_prompt_text = (
            narration_prompt_text.strip() if narration_prompt_text else None
        )
    except Exception:
        narration_prompt_text = None

    # Generate spoken talk track.
    try:
        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        org_concept_id = effective.get("organisation_id")
        llm_client = get_llm_client(
            user_concept_id=user_concept_id, org_concept_id=org_concept_id
        )
        model_name = get_active_model_name()

        def _extract_spoken_only(text: str) -> str | None:
            return _extract_tagged_block(text, "spoken")

        # Include a small client-reported timing hint for narration generation.
        # (Non-authoritative; used only for guidance.)
        timing_hint = None
        try:
            from ...services.client_capabilities_service import (
                get_client_capabilities_snapshot,
            )

            snapshot = get_client_capabilities_snapshot()
            speech = (
                snapshot.get("speech_synthesis") if isinstance(snapshot, dict) else None
            )
            speech = speech if isinstance(speech, dict) else {}
            raw_settings = speech.get("settings")
            settings = raw_settings if isinstance(raw_settings, dict) else {}

            preferred = settings.get("preferred_speaking_seconds")
            maximum = settings.get("max_speaking_seconds")

            try:
                preferred_int = int(preferred) if preferred is not None else None
            except Exception:
                preferred_int = None

            try:
                maximum_int = int(maximum) if maximum is not None else None
            except Exception:
                maximum_int = None

            if preferred_int is not None:
                preferred_int = max(1, min(preferred_int, 600))
            if maximum_int is not None:
                maximum_int = max(1, min(maximum_int, 600))

            effective_preferred = preferred_int
            if preferred_int is not None and maximum_int is not None:
                effective_preferred = min(preferred_int, maximum_int)

            if effective_preferred is not None or maximum_int is not None:
                timing_hint = (
                    "Speech timing hint (client-reported, non-authoritative): "
                    f"preferred_speaking_seconds={effective_preferred!r}, "
                    f"max_speaking_seconds={maximum_int!r}. "
                    "Aim for about preferred_speaking_seconds seconds and do not exceed max_speaking_seconds."
                )
        except Exception:
            timing_hint = None

        narration_system = (
            "You are Von. Produce a short talk track for text-to-speech. "
            "Return ONLY one block: <spoken>...</spoken>. "
            "Do not include <screen>. Do not include code blocks. "
            "Use New Zealand English spelling."
            + ("\n\n" + timing_hint if timing_hint else "")
            + (
                "\n\nVON CHAT NARRATION PROMPT (from Vontology):\n"
                + narration_prompt_text
                if narration_prompt_text
                else ""
            )
        )

        narration_user = (
            "User message:\n"
            f"{prompt_text}\n\n"
            "On-screen content (do not read verbatim if long; summarise):\n"
            f"{screen_text}\n"
        )

        narration_response = llm_client.generate(
            prompt="Generate <spoken> talk track",
            context=[
                {"role": "system", "content": narration_system},
                {"role": "user", "content": narration_user},
            ],
            model=model_name,
        )

        spoken = _extract_spoken_only(str(narration_response))
        if not spoken:
            return jsonify({"status": "no_spoken_generated", "updated": False}), 200

        presenter_channels = {
            "screen": screen_text,
            "spoken": spoken,
            "format": "narration_fallback_v1",
        }
        spoken_backfill_reason = (
            "missing_spoken"
            if isinstance(existing_channels, dict)
            else "missing_presenter_channels"
        )
        display_elements_contract = build_turn_display_elements(
            response_text=screen_text,
            presenter_channels=presenter_channels,
            spoken_backfill_second_pass_attempted=True,
            spoken_backfill_second_pass_reason=spoken_backfill_reason,
        )

        update_result = (
            chat_history_service.upsert_presenter_channels_for_history_message(
                user_id=user_concept_id,
                session_id=target_session_id,
                history_index=history_index,
                presenter_channels=presenter_channels,
                display_elements=display_elements_contract,
                generated_at=datetime.now(timezone.utc),
                force=force,
            )
        )

        return jsonify(
            {
                "status": "ok",
                "presenter_channels": presenter_channels,
                "display_elements": display_elements_contract,
                "updated": bool(update_result.get("updated")),
                "matched": bool(update_result.get("matched")),
            }
        )
    except Exception as e:
        return jsonify({"error": f"Failed generating spoken talk track: {e}"}), 500


@von_bp.route("/history/length", methods=["GET"])
def history_length():
    """Retrieve the chat history length for the current user."""
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")

    if not user_concept_id:
        return jsonify({"history_length": 0, "authenticated": False})

    try:
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        length = chat_history_service.get_chat_history_length(
            user_concept_id, namespace=namespace
        )
        session_count = chat_history_service.get_chat_history_session_count(
            user_concept_id, namespace=namespace
        )
        return jsonify(
            {
                "history_length": length,
                "session_count": session_count,
                "authenticated": True,
            }
        )
    except Exception as e:
        print(f"Error retrieving history length: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/history/sessions", methods=["GET"])
def history_sessions():
    """Return per-session chat history counts for the current user."""
    try:
        from ...security.access_control import get_effective_user_concept_id

        user_concept_id = get_effective_user_concept_id()
    except Exception:
        user_concept_id = session.get("user_concept_id")
    if not user_concept_id:
        return jsonify({"authenticated": False, "sessions": []})

    limit = request.args.get("limit", default=50, type=int)
    summary_mode = request.args.get("summary", default="full")
    if not isinstance(summary_mode, str) or not summary_mode.strip():
        summary_mode = "full"
    try:
        from ...services.shared_conversation_service import (
            list_accepted_invites_for_user,
            list_outgoing_accepted_invites_for_user,
            resolve_conversation_owner,
        )

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        organisation_concept_id = _normalise_concept_id(
            effective.get("organisation_id")
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)

        # Fallback: extract org from namespace if not in session (JVNAUTOSCI-1004)
        # Namespace format: #V#user@org or user@org
        if (
            not organisation_concept_id
            and isinstance(namespace, str)
            and "@" in namespace
        ):
            ns_org_part = namespace.split("@", 1)[-1]
            organisation_concept_id = _normalise_concept_id(ns_org_part)

        # JVNAUTOSCI-1015: Legacy conversations without namespace are excluded.
        # Run utilities/backfill_chat_history_namespace.py to migrate any old data.
        include_legacy = False
        sessions = chat_history_service.get_chat_history_session_summaries(
            user_concept_id,
            limit=limit,
            namespace=namespace,
            include_legacy=include_legacy,
            summary_mode=summary_mode,
        )

        shared_invites = list_accepted_invites_for_user(user_concept_id=user_concept_id)
        # JVNAUTOSCI-1004: Filter shared invites by current organisation
        if organisation_concept_id:
            shared_invites = [
                invite
                for invite in shared_invites
                if (
                    _normalise_concept_id(invite.get("organisation_concept_id"))
                    in (None, organisation_concept_id)
                )
            ]
        invite_by_session: dict[str, dict] = {}
        for invite in shared_invites:
            if not isinstance(invite, dict):
                continue
            invite_session_id = invite.get("session_id")
            if isinstance(invite_session_id, str) and invite_session_id:
                invite_by_session.setdefault(invite_session_id, invite)

        if invite_by_session:
            for summary in sessions:
                if not isinstance(summary, dict):
                    continue
                session_id = summary.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    continue
                invite = invite_by_session.get(session_id)
                if not isinstance(invite, dict):
                    continue

                inviter_id = _normalise_concept_id(invite.get("inviter_user_id"))
                owner_id = _normalise_concept_id(
                    invite.get("conversation_owner_user_id")
                    or invite.get("inviter_user_id")
                )
                if not invite.get("conversation_owner_user_id"):
                    resolved_owner = resolve_conversation_owner(session_id=session_id)
                    resolved_owner_id = _normalise_concept_id(resolved_owner)
                    if resolved_owner_id:
                        owner_id = resolved_owner_id

                summary["shared_with_me"] = True
                if inviter_id:
                    summary["shared_from_user_id"] = inviter_id
                if owner_id:
                    summary["shared_owner_user_id"] = owner_id
                if invite.get("invite_id"):
                    summary["invite_id"] = invite.get("invite_id")
                shared_timestamp = (
                    invite.get("accepted_at")
                    or invite.get("updated_at")
                    or invite.get("created_at")
                )
                summary["shared_accepted_at"] = shared_timestamp

        # Flag owner's sessions that have accepted participants (JVNAUTOSCI-1002)
        # This enables the owner to subscribe to SSE updates from participants
        outgoing_accepted = list_outgoing_accepted_invites_for_user(
            user_concept_id=user_concept_id
        )
        if organisation_concept_id:
            outgoing_accepted = [
                invite
                for invite in outgoing_accepted
                if (
                    _normalise_concept_id(invite.get("organisation_concept_id"))
                    in (None, organisation_concept_id)
                )
            ]
        outgoing_by_session: dict[str, list] = {}
        for invite in outgoing_accepted:
            if not isinstance(invite, dict):
                continue
            out_session_id = invite.get("session_id")
            if isinstance(out_session_id, str) and out_session_id:
                outgoing_by_session.setdefault(out_session_id, []).append(invite)
        if outgoing_by_session:
            for summary in sessions:
                if not isinstance(summary, dict):
                    continue
                session_id = summary.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    continue
                if session_id in outgoing_by_session:
                    summary["has_shared_participants"] = True
        existing_session_ids = {
            s.get("session_id") for s in sessions if isinstance(s, dict)
        }
        shared_sessions = []
        for invite in shared_invites:
            if not isinstance(invite, dict):
                continue
            inviter_id = _normalise_concept_id(invite.get("inviter_user_id"))
            owner_id = _normalise_concept_id(
                invite.get("conversation_owner_user_id")
                or invite.get("inviter_user_id")
            )
            session_id = invite.get("session_id")
            if not owner_id or not isinstance(session_id, str) or not session_id:
                continue
            if not invite.get("conversation_owner_user_id"):
                resolved_owner = resolve_conversation_owner(session_id=session_id)
                resolved_owner_id = _normalise_concept_id(resolved_owner)
                if resolved_owner_id:
                    owner_id = resolved_owner_id
            if session_id in existing_session_ids:
                continue
            owner_namespace = _derive_namespace_for_user_org(
                owner_id, invite.get("organisation_concept_id")
            ) or chat_history_service.resolve_chat_history_namespace(owner_id)
            summary = chat_history_service.get_chat_history_session_summary(
                owner_id,
                session_id,
                namespace=owner_namespace,
                summary_mode=summary_mode,
            )
            if not isinstance(summary, dict) and owner_id:
                summary = chat_history_service.get_chat_history_session_summary(
                    owner_id,
                    session_id,
                    namespace=None,
                    summary_mode=summary_mode,
                )
            if not isinstance(summary, dict):
                continue
            shared_timestamp = (
                invite.get("accepted_at")
                or invite.get("updated_at")
                or invite.get("created_at")
            )
            existing_last = summary.get("last_message_at")
            if not existing_last:
                shared_dt = chat_history_service._coerce_datetime(shared_timestamp)
                if shared_dt:
                    summary["last_message_at"] = shared_dt.isoformat().replace(
                        "+00:00", "Z"
                    )
            summary["shared_with_me"] = True
            if inviter_id:
                summary["shared_from_user_id"] = inviter_id
            summary["shared_owner_user_id"] = owner_id
            summary["invite_id"] = invite.get("invite_id")
            summary["shared_accepted_at"] = shared_timestamp
            shared_sessions.append(summary)

        combined = sessions + shared_sessions
        combined.sort(
            key=lambda s: chat_history_service._coerce_datetime(
                s.get("last_message_at") if isinstance(s, dict) else None
            )
            or datetime(1970, 1, 1, tzinfo=timezone.utc),
            reverse=True,
        )

        return jsonify(
            {
                "authenticated": True,
                "sessions": combined,
                "active_session_id": session.get("session_id"),
            }
        )
    except Exception as e:
        print(f"Error retrieving history sessions: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/models", methods=["GET"])
def get_models():
    """API endpoint to fetch the list of available models."""
    # Assuming list_models_func is stored in app config or accessible globally
    list_models_func = current_app.config.get("LIST_MODELS_FUNC")
    if not list_models_func:
        return jsonify({"error": "Model listing function not configured."}), 500
    try:
        models = list_models_func()
        return jsonify(models), 200
    except Exception as e:
        print(f"Error fetching models: {e}")  # Log error server-side
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/search", methods=["GET"])
def search_concepts_endpoint():
    """API endpoint to search for concepts by name.

    Query parameters:
    - q: Search query string (required)
    - limit: Maximum results to return (default: 8)
    """
    try:
        from ...services.concept_search_service import search_concepts

        query = request.args.get("q", "").strip()
        limit = request.args.get("limit", default=8, type=int)

        if not query:
            return jsonify({"results": [], "total_count": 0}), 200

        result = search_concepts(
            query=query, match_type="substring", limit=limit, include_description=False
        )

        # Format results for autocomplete
        formatted_results = [
            {
                "id": item.get("concept_id"),  # Frontend expects 'id' field
                "concept_id": item.get("concept_id"),
                "name": item.get("name") or item.get("concept_id"),
                "kind": item.get("kind", "unknown"),
            }
            for item in result.get("results", [])
        ]

        return (
            jsonify(
                {
                    "results": formatted_results,
                    "total_count": result.get("total_count", 0),
                }
            ),
            200,
        )
    except Exception as e:
        current_app.logger.error(f"Concept search error: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/render_markdown", methods=["POST"])
def render_markdown_endpoint():
    """Render markdown to sanitised HTML.

    Request JSON:
    - text: markdown string

    Response JSON:
    - html: sanitised HTML string
    """

    try:
        from ...services.markdown_render_service import render_markdown_to_safe_html

        data = request.get_json(silent=True) or {}
        text = data.get("text", "")
        if not isinstance(text, str):
            return jsonify({"error": "Field 'text' must be a string"}), 400

        if len(text) > 200_000:
            return jsonify({"error": "Markdown payload too large"}), 413

        html = render_markdown_to_safe_html(text)
        return jsonify({"html": html}), 200
    except Exception as e:
        current_app.logger.error(f"render_markdown error: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/reset", methods=["POST"])
def reset_context():
    """Reset the conversation context."""
    try:
        user_concept_id = session.get("user_concept_id")
        session_id = session.get("session_id")

        if user_concept_id and session_id:
            chat_history_service.add_reset_marker_to_history(
                user_concept_id, session_id
            )

        # Clear the conversation context
        current_app.config["CONTEXT"] = []
        print("Context reset successfully")  # Log server-side
        return (
            jsonify({"status": "reset", "message": "Context reset successfully"}),
            200,
        )
    except Exception as e:
        print(f"Error resetting context: {e}")  # Log error server-side
        return jsonify({"error": f"Failed to reset context: {str(e)}"}), 500


# Phase 2: Organisation and Role Selection Endpoints
@von_bp.route("/api/session/set_user_concept", methods=["POST"])
def set_user_concept():
    """Set the current user concept in the session.

    This aligns the authenticated session identity with the user selected in Settings.

    Request body: {user_concept_id: str}
    Returns: {user_id, organisation_id, role, namespace, status: 'updated'}
    """
    try:
        from ...services.namespace_service import derive_namespace

        authenticated_id = (
            session.get("user_id")
            or session.get("user_concept_id")
            or session.get("user_email")
        )
        if not authenticated_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        user_concept_id = data.get("user_concept_id")
        if not isinstance(user_concept_id, str) or not user_concept_id.strip():
            return jsonify({"error": "user_concept_id required"}), 400

        user_concept_id = user_concept_id.strip()
        if not user_concept_id.startswith("#V#"):
            user_concept_id = f"#V#{user_concept_id}"

        user_slug = user_concept_id[3:]
        user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")

        existing_user_concept_id = session.get("user_concept_id")
        if isinstance(existing_user_concept_id, str):
            existing_user_concept_id = existing_user_concept_id.strip()
            if existing_user_concept_id and not existing_user_concept_id.startswith(
                "#V#"
            ):
                existing_user_concept_id = f"#V#{existing_user_concept_id}"
        else:
            existing_user_concept_id = None

        organisation_concept_id = session.get("organisation_concept_id")
        role_in_org = session.get("role_in_org")
        org_slug = None
        if isinstance(organisation_concept_id, str) and organisation_concept_id.strip():
            org_slug_raw = organisation_concept_id.strip()
            if org_slug_raw.startswith("#V#"):
                org_slug_raw = org_slug_raw[3:]
            if "@" in org_slug_raw:
                org_slug_raw = org_slug_raw.split("@", 1)[0]
            if "+" in org_slug_raw:
                org_slug_raw = org_slug_raw.split("+", 1)[0]
            org_slug = re.sub(r"[^a-z0-9]+", "_", org_slug_raw.strip().lower()).strip(
                "_"
            )

        namespace = derive_namespace(user_slug, org_slug, role_in_org)

        # Store the concept id for authoritative identity.
        session["user_concept_id"] = user_concept_id
        # Keep backward compatibility with code that still reads session['user_id'].
        session["user_id"] = user_concept_id

        # Clear org-scoped context only when switching between different user concepts.
        # If we are simply backfilling user_concept_id for an already-authenticated session,
        # keep any existing org selection and recompute namespace accordingly.
        if existing_user_concept_id and existing_user_concept_id != user_concept_id:
            organisation_concept_id = None
            role_in_org = None
            session.pop("organisation_concept_id", None)
            session.pop("role_in_org", None)
        session["namespace"] = namespace
        session.modified = True

        organisation_id_response = None
        if isinstance(organisation_concept_id, str) and organisation_concept_id.strip():
            organisation_id_response = organisation_concept_id.strip()
            if not organisation_id_response.startswith("#V#"):
                organisation_id_response = f"#V#{organisation_id_response}"

        return (
            jsonify(
                {
                    "status": (
                        "updated"
                        if existing_user_concept_id != user_concept_id
                        else "unchanged"
                    ),
                    "user_id": user_concept_id,
                    "organisation_id": organisation_id_response,
                    "role": role_in_org,
                    "namespace": namespace,
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error setting user concept: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/set_organisation", methods=["POST"])
def set_organisation():
    """
    Set the current organisation context in the session.

    Request body: {organisation_concept_id: str} or empty dict to clear
    - organisation_concept_id can be a concept ID with or without #V# prefix
    - If organisation_concept_id is present but empty/null, treat as clear request

    Supports window-scoped sessions via X-Von-Window-Session header (JVNAUTOSCI-1011).
    If the header is present, org context is stored in per-window memory store
    instead of the browser-wide Flask session.

    Returns: {user_id, organisation_id, role, namespace, status: 'updated'}
    """
    try:
        from ...services.namespace_service import derive_namespace
        from ...security.role_resolver import get_user_role

        user_id = (
            session.get("user_concept_id")
            or session.get("user_id")
            or session.get("user_email")
        )
        if not user_id:
            return jsonify({"error": "Not authenticated"}), 401

        # JVNAUTOSCI-1011: Check for window session header
        window_session_id = request.headers.get("X-Von-Window-Session")

        data = request.get_json() or {}
        org_id = data.get("organisation_concept_id")

        # Normalise user id to a safe slug for namespace/role lookup
        user_slug = str(user_id)
        if user_slug.startswith("#V#"):
            user_slug = user_slug[3:]
        if "@" in user_slug:
            user_slug = user_slug.split("@", 1)[0]
        if "+" in user_slug:
            user_slug = user_slug.split("+", 1)[0]
        user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")

        # Check if this is a clear request (empty dict or explicit null/empty string)
        is_clear_request = "organisation_concept_id" in data and not org_id

        # Handle clearing (personal context / no org)
        if is_clear_request:
            namespace = derive_namespace(user_slug)

            # JVNAUTOSCI-1011: Use window session if header present
            if window_session_id:
                clear_window_organisation(window_session_id, namespace, user_id)
            else:
                # Fallback: update Flask session
                session.pop("organisation_concept_id", None)
                session.pop("role_in_org", None)
                # JVNAUTOSCI-1004: Clear chat session_id when org changes
                session.pop("session_id", None)
                session["namespace"] = namespace
                session.modified = True

            return (
                jsonify(
                    {
                        "status": "updated",
                        "user_id": user_id,
                        "organisation_id": None,
                        "role": None,
                        "namespace": namespace,
                        "window_session_id": window_session_id,
                    }
                ),
                200,
            )

        # If org_id not provided and not an explicit clear, that's an error
        if not org_id:
            return jsonify({"error": "organisation_concept_id required"}), 400

        # TODO: Validate user is member of org (once membership model exists)
        # For now, allow any org switch

        # org_id may arrive as a concept id (e.g., "#V#university_of_auckland_strong_ai_lab")
        org_slug = str(org_id)
        if org_slug.startswith("#V#"):
            org_slug = org_slug[3:]
        org_slug = org_slug.strip().lower().replace(" ", "_")

        # Get role for this user in this org (stub resolver expects slugs)
        try:
            role_in_org = get_user_role(user_slug, org_slug)
        except Exception:
            role_in_org = "member"  # Default fallback

        # Derive composite namespace using slug values
        namespace = derive_namespace(user_slug, org_slug)

        # JVNAUTOSCI-1011: Use window session if header present
        if window_session_id:
            set_window_organisation(
                window_session_id=window_session_id,
                organisation_concept_id=org_slug,
                role_in_org=role_in_org,
                namespace=namespace,
                user_id=user_id,
            )
        else:
            # Fallback: update Flask session (for clients without window session support)
            session["organisation_concept_id"] = org_slug
            session["role_in_org"] = role_in_org
            session["namespace"] = namespace
            # JVNAUTOSCI-1004: Clear chat session_id when org changes to avoid
            # showing conversation from previous org context
            session.pop("session_id", None)
            session.modified = True

        # Return concept ID form in API response (with #V# prefix)
        concept_id_response = (
            f"#V#{org_slug}" if not str(org_id).startswith("#V#") else org_id
        )

        return (
            jsonify(
                {
                    "status": "updated",
                    "user_id": user_id,
                    "organisation_id": concept_id_response,
                    "role": role_in_org,
                    "namespace": namespace,
                    "window_session_id": window_session_id,
                }
            ),
            200,
        )

    except Exception as e:
        print(f"Error setting organisation: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/context", methods=["GET"])
def get_session_context():
    """
    Get current session context (user, organisation, role, namespace).

    Supports window-scoped sessions via X-Von-Window-Session header (JVNAUTOSCI-1011).
    If the header is present, org context is read from per-window memory store
    instead of the browser-wide Flask session, enabling different org contexts
    in different browser windows.

    Returns: {user_id, organisation_id, role, namespace, authenticated, window_session_id?}
    """
    try:
        from ...services.namespace_service import derive_namespace
        from ...security.role_resolver import get_user_role

        user_id = (
            session.get("user_concept_id")
            or session.get("user_id")
            or session.get("user_email")
        )
        if not user_id:
            return (
                jsonify(
                    {
                        "authenticated": False,
                        "user_id": None,
                        "organisation_id": None,
                        "role": None,
                        "namespace": None,
                    }
                ),
                200,
            )

        # JVNAUTOSCI-1011: Check for window session header
        window_session_id = request.headers.get("X-Von-Window-Session")

        # Get effective context from window session or Flask session
        effective = get_effective_context(window_session_id, dict(session), user_id)

        org_id = effective.get("organisation_id")
        role_in_org = effective.get("role")
        namespace = effective.get("namespace")

        # If no namespace resolved, derive it
        if not namespace:
            user_slug = str(user_id)
            if user_slug.startswith("#V#"):
                user_slug = user_slug[3:]
            if "@" in user_slug:
                user_slug = user_slug.split("@", 1)[0]
            if "+" in user_slug:
                user_slug = user_slug.split("+", 1)[0]
            user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")
            if org_id:
                # Get role if not in session
                if not role_in_org:
                    try:
                        role_in_org = get_user_role(user_slug, org_id)
                    except Exception:
                        role_in_org = "member"
                namespace = derive_namespace(user_slug, org_id)
            else:
                namespace = derive_namespace(user_slug)

        organisation_id_response = None
        if isinstance(org_id, str) and org_id.strip():
            organisation_id_response = org_id.strip()
            if not organisation_id_response.startswith("#V#"):
                organisation_id_response = f"#V#{organisation_id_response}"

        response_data = {
            "authenticated": True,
            "user_id": user_id,
            "organisation_id": organisation_id_response,
            "role": role_in_org,
            "namespace": namespace,
        }

        # Include window_session_id in response so frontend can confirm which session is active
        if window_session_id:
            response_data["window_session_id"] = window_session_id
            response_data["context_source"] = effective.get("source", "window_session")

        return jsonify(response_data), 200

    except Exception as e:
        print(f"Error getting session context: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/set_chat_session", methods=["POST"])
def set_chat_session():
    """Set the active chat session_id for the current authenticated user.

    Request body: {session_id: str, include_history?: bool}
    Returns: {status, session_id, session_name, history}

    This enables the frontend to switch to a prior session and continue it.
    """
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()
        include_history = data.get("include_history", True)
        if isinstance(include_history, bool):
            pass
        elif isinstance(include_history, str):
            parsed = include_history.strip().lower()
            if parsed in ("0", "false", "no", "n", "off"):
                include_history = False
            elif parsed in ("1", "true", "yes", "y", "on"):
                include_history = True
            else:
                include_history = True
        elif isinstance(include_history, (int, float)):
            include_history = include_history != 0
        else:
            include_history = True

        # Verify the session belongs to this user.
        coll = chat_history_service.get_chat_history_collection_service()
        if coll is None:
            return jsonify({"error": "Chat history unavailable"}), 503

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        query = chat_history_service.build_chat_history_query(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=namespace,
        )
        projection = {"session_name": 1}
        if include_history:
            projection["history"] = 1
        user_doc = coll.find_one(query, projection)
        owner_user_id, invite = _resolve_shared_conversation_owner(
            user_concept_id=user_concept_id, session_id=session_id
        )
        owner_doc = None
        owner_namespace = None
        if owner_user_id and invite:
            org_id = invite.get("organisation_concept_id") if invite else None
            owner_namespace = _derive_namespace_for_user_org(
                owner_user_id, org_id
            ) or chat_history_service.resolve_chat_history_namespace(owner_user_id)
            shared_query = chat_history_service.build_chat_history_query(
                user_id=owner_user_id,
                session_id=session_id,
                namespace=owner_namespace,
            )
            owner_doc = coll.find_one(shared_query, projection)
            if not owner_doc:
                shared_query = chat_history_service.build_chat_history_query(
                    user_id=owner_user_id,
                    session_id=session_id,
                    namespace=None,
                )
                owner_doc = coll.find_one(shared_query, projection)

        doc = owner_doc or user_doc
        if not doc:
            return jsonify({"error": "Session not found"}), 404

        session_name = doc.get("session_name")

        def _normalise_timestamp(value):
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value.isoformat().replace("+00:00", "Z")
            return value

        normalised_history = []
        if include_history:
            history = doc.get("history") or []
            if not isinstance(history, list):
                history = []
            if invite and owner_doc is not None:
                owner_history = owner_doc.get("history") or []
                if not isinstance(owner_history, list):
                    owner_history = []
                user_history = user_doc.get("history") if user_doc else []
                if not isinstance(user_history, list):
                    user_history = []
                owner_history = _apply_default_author(owner_history, owner_user_id)
                user_history = _apply_default_author(user_history, user_concept_id)
                history = _merge_shared_histories(owner_history, user_history)
                if owner_user_id and len(history) > len(owner_history):
                    try:
                        chat_history_service.set_chat_history_for_session(
                            user_id=owner_user_id,
                            session_id=session_id,
                            history=history,
                            namespace=owner_namespace,
                            set_updated_at=False,
                            extra_set_fields={
                                "shared_history_migrated_at": datetime.now(
                                    timezone.utc
                                ),
                                "shared_history_migrated_from": user_concept_id,
                            },
                        )
                    except Exception:
                        pass

            for msg in history:
                if not isinstance(msg, dict):
                    continue
                out = dict(msg)
                if "timestamp" in out:
                    out["timestamp"] = _normalise_timestamp(out.get("timestamp"))
                normalised_history.append(out)

        # Switch active session (window-scoped if window session is present).
        # JVNAUTOSCI-1011: Store in window session context to avoid cross-window leaks.
        if window_session_id:
            from ...services.window_session_context_service import (
                set_window_chat_session,
            )

            set_window_chat_session(window_session_id, session_id, user_concept_id)
        else:
            # Fallback for legacy clients without window session support
            session["session_id"] = session_id
            session.modified = True

        # Clear any non-persistent context cache.
        try:
            current_app.config["CONTEXT"] = []
        except Exception:
            pass

        return (
            jsonify(
                {
                    "status": "updated",
                    "session_id": session_id,
                    "session_name": session_name,
                    "history": normalised_history,
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error setting chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/assign_chat_session_org", methods=["POST"])
def assign_chat_session_org():
    """Assign a chat session to the current organisation namespace if missing."""
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        organisation_concept_id = _normalise_concept_id(
            effective.get("organisation_id")
        )
        if not organisation_concept_id:
            return jsonify({"error": "No organisation context"}), 400

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        coll = chat_history_service.get_chat_history_collection_service()
        if coll is None:
            return jsonify({"error": "Chat history unavailable"}), 503

        target_namespace = _derive_namespace_for_user_org(
            user_concept_id, organisation_concept_id
        )
        if not target_namespace:
            return jsonify({"error": "Unable to derive namespace"}), 500

        query = chat_history_service.build_chat_history_query(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=None,
            include_legacy=True,
        )
        doc = coll.find_one(query, {"namespace": 1, "organisation_concept_id": 1})
        if not isinstance(doc, dict):
            return jsonify({"error": "Session not found"}), 404

        existing_namespace = doc.get("namespace")
        if isinstance(existing_namespace, str) and existing_namespace.strip():
            if existing_namespace.strip() != target_namespace:
                return (
                    jsonify(
                        {
                            "error": "Session already assigned to a different organisation",
                            "namespace": existing_namespace,
                        }
                    ),
                    409,
                )
            return jsonify({"status": "ok", "namespace": existing_namespace}), 200

        existing_org = _normalise_concept_id(doc.get("organisation_concept_id"))
        if existing_org and existing_org != organisation_concept_id:
            return (
                jsonify(
                    {
                        "error": "Session already assigned to a different organisation",
                        "organisation_concept_id": existing_org,
                    }
                ),
                409,
            )

        result = coll.update_one(
            {"_id": doc.get("_id")},
            {
                "$set": {
                    "namespace": target_namespace,
                    "organisation_concept_id": organisation_concept_id,
                    "role_in_org": session.get("role_in_org"),
                }
            },
        )

        return (
            jsonify(
                {
                    "status": "updated",
                    "session_id": session_id,
                    "namespace": target_namespace,
                    "organisation_concept_id": organisation_concept_id,
                    "matched": bool(getattr(result, "matched_count", 0) > 0),
                    "updated": bool(getattr(result, "modified_count", 0) > 0),
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error assigning chat session org: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/move_chat_session_org", methods=["POST"])
def move_chat_session_org():
    """Move a conversation the user owns to a different organisation.

    JVNAUTOSCI-1039: Allows users who are members of multiple organisations
    to move a conversation from one organisation context to another.

    Request body:
        session_id: The conversation to move
        target_organisation_id: The organisation to move to

    Security:
        - User must be authenticated
        - User must own the conversation
        - User must be a member of both the current and target organisations
        - Shared invites for users not in target org are revoked
    """
    try:
        from ...services.organisation_membership_service import (
            get_organisation_members,
            get_user_memberships,
        )
        from ...services.shared_conversation_service import (
            list_active_invites_for_session,
            revoke_invites_for_session,
        )
        from ...services.episode_logging_service import log_episode

        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        target_org_id = _normalise_concept_id(
            data.get("target_organisation_id") or data.get("organisation_id")
        )

        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        if not target_org_id:
            return jsonify({"error": "target_organisation_id required"}), 400

        # Verify user is a member of the target organisation
        memberships = get_user_memberships(user_concept_id)
        user_org_ids = {
            m.get("organisation_concept_id")
            for m in memberships.get("memberships", [])
            if m.get("organisation_concept_id")
        }
        if target_org_id not in user_org_ids:
            return (
                jsonify({"error": "Not a member of target organisation"}),
                403,
            )

        coll = chat_history_service.get_chat_history_collection_service()
        if coll is None:
            return jsonify({"error": "Chat history unavailable"}), 503

        # Find the conversation - must belong to this user
        query = chat_history_service.build_chat_history_query(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=None,
            include_legacy=True,
        )
        doc = coll.find_one(
            query,
            {
                "namespace": 1,
                "organisation_concept_id": 1,
                "user_id": 1,
                "session_id": 1,
            },
        )
        if not isinstance(doc, dict):
            return jsonify({"error": "Conversation not found"}), 404

        # Verify ownership
        doc_user_id = _normalise_concept_id(doc.get("user_id"))
        if doc_user_id != user_concept_id:
            return jsonify({"error": "Not the owner of this conversation"}), 403

        # Check current organisation
        current_org_id = _normalise_concept_id(doc.get("organisation_concept_id"))
        current_namespace = doc.get("namespace")

        if current_org_id == target_org_id:
            return (
                jsonify(
                    {
                        "status": "ok",
                        "message": "Conversation already in target organisation",
                        "namespace": current_namespace,
                    }
                ),
                200,
            )

        # Derive new namespace for target organisation
        target_namespace = _derive_namespace_for_user_org(
            user_concept_id, target_org_id
        )
        if not target_namespace:
            return jsonify({"error": "Unable to derive namespace for target org"}), 500

        # Handle shared conversation invites
        # Get members of the target org to determine which invites to keep
        target_org_members = get_organisation_members(target_org_id)
        target_member_ids = {
            _normalise_concept_id(m.get("user_concept_id"))
            for m in target_org_members.get("members", [])
            if m.get("user_concept_id")
        }

        # Check existing invites
        active_invites = list_active_invites_for_session(session_id=session_id)
        invites_to_revoke = [
            inv
            for inv in active_invites
            if _normalise_concept_id(inv.get("invitee_user_id"))
            not in target_member_ids
        ]

        revoke_result = {"revoked_count": 0, "invitee_ids": []}
        if invites_to_revoke:
            # Filter out None values for type safety
            valid_member_ids = [m for m in target_member_ids if m is not None]
            revoke_result = revoke_invites_for_session(
                session_id=session_id,
                exclude_user_ids=valid_member_ids,
                reason=f"conversation_moved_to_{target_org_id}",
            )

        # Update the conversation
        update_fields = {
            "namespace": target_namespace,
            "organisation_concept_id": target_org_id,
            "previous_namespace": current_namespace,
            "previous_organisation_concept_id": current_org_id,
            "moved_at": datetime.now(timezone.utc),
            "moved_by": user_concept_id,
        }

        # Clear RAG indexed status so conversation will be re-indexed for new namespace
        unset_fields = {
            "rag_indexed_success": "",
            "rag_indexed_failed": "",
            "rag_indexed_at": "",
        }

        result = coll.update_one(
            {"_id": doc.get("_id")},
            {"$set": update_fields, "$unset": unset_fields},
        )

        # Log the move operation
        log_episode(
            episode_type="conversation_moved",
            actor_user_id=user_concept_id,
            organisation_concept_id=target_org_id,
            session_id=session_id,
            payload={
                "from_organisation": current_org_id,
                "to_organisation": target_org_id,
                "from_namespace": current_namespace,
                "to_namespace": target_namespace,
                "invites_revoked": revoke_result.get("revoked_count", 0),
                "revoked_invitee_ids": revoke_result.get("invitee_ids", []),
            },
            status="moved",
        )

        return (
            jsonify(
                {
                    "status": "moved",
                    "session_id": session_id,
                    "namespace": target_namespace,
                    "organisation_concept_id": target_org_id,
                    "previous_namespace": current_namespace,
                    "previous_organisation_concept_id": current_org_id,
                    "matched": bool(getattr(result, "matched_count", 0) > 0),
                    "updated": bool(getattr(result, "modified_count", 0) > 0),
                    "invites_revoked": revoke_result.get("revoked_count", 0),
                    "revoked_invitee_ids": revoke_result.get("invitee_ids", []),
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error moving chat session org: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/create_chat_session", methods=["POST"])
def create_chat_session():
    """Create and switch to a new named chat session for the current user."""
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_name = (
            data.get("session_name") or data.get("name") or data.get("chat_name")
        )

        session_id = str(uuid.uuid4())

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )

        result = chat_history_service.create_chat_session(
            user_id=user_concept_id,
            session_id=session_id,
            session_name=session_name,
            namespace=effective.get("namespace"),
            organisation_concept_id=effective.get("organisation_id"),
            role_in_org=effective.get("role"),
        )

        session["session_id"] = session_id
        session.modified = True

        try:
            current_app.config["CONTEXT"] = []
        except Exception:
            pass

        return (
            jsonify(
                {
                    "status": "created",
                    "session_id": session_id,
                    "session_name": result.get("session_name"),
                    "history": [],
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error creating chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/rename_chat_session", methods=["POST"])
def rename_chat_session():
    """Rename an existing chat session for the current user."""
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()
        include_history = data.get("include_history", True)
        if isinstance(include_history, bool):
            pass
        elif isinstance(include_history, str):
            parsed = include_history.strip().lower()
            if parsed in ("0", "false", "no", "n", "off"):
                include_history = False
            elif parsed in ("1", "true", "yes", "y", "on"):
                include_history = True
            else:
                include_history = True
        elif isinstance(include_history, (int, float)):
            include_history = include_history != 0
        else:
            include_history = True

        session_name = data.get("session_name") or data.get("name")
        if not isinstance(session_name, str) or not session_name.strip():
            return jsonify({"error": "session_name required"}), 400

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        result = chat_history_service.rename_chat_session(
            user_id=user_concept_id,
            session_id=session_id,
            session_name=session_name,
            namespace=namespace,
        )

        if not result.get("matched"):
            return jsonify({"error": "Session not found"}), 404

        return (
            jsonify(
                {
                    "status": "updated",
                    "session_id": session_id,
                    "session_name": result.get("session_name"),
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error renaming chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/delete_chat_session", methods=["POST"])
def delete_chat_session():
    """Delete a chat session for the current user.

    JVNAUTOSCI-1014: Only allows deletion of sessions with fewer than a threshold
    number of turns (currently 4) to prevent accidental deletion of substantial
    conversations.
    """
    MAX_DELETABLE_TURNS = 4
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)

        # Verify session exists and check message count
        session_doc = chat_history_service.get_chat_history_session_summary(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=namespace,
            summary_mode="light",
        )
        if not session_doc:
            return jsonify({"error": "Session not found"}), 404

        message_count = session_doc.get("message_count", 0)
        turn_count = max(0, (message_count + 1) // 2)
        if turn_count >= MAX_DELETABLE_TURNS:
            return (
                jsonify(
                    {
                        "error": f"Cannot delete conversations with {MAX_DELETABLE_TURNS} or more turns",
                        "turn_count": turn_count,
                    }
                ),
                403,
            )

        chat_history_service.delete_chat_history(user_concept_id, session_id)

        return (
            jsonify(
                {
                    "status": "deleted",
                    "session_id": session_id,
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error deleting chat session: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/session/chat_session_links", methods=["GET", "POST"])
def chat_session_links():
    """Get or set concept links for a chat session.

    Stored on the chat_history session document as `session_links`.
    Links are many-to-many lists of concept_ids:
      - programmes
      - projects
      - activities
      - modalities
    """
    try:
        user_concept_id = session.get("user_concept_id")
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        requested_session_id = (
            request.args.get("session_id")
            if request.method == "GET"
            else (request.get_json(silent=True) or {}).get("session_id")
        )
        session_id = None
        if isinstance(requested_session_id, str) and requested_session_id.strip():
            session_id = requested_session_id.strip()
        else:
            session_id = session.get("session_id")

        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)

        # Best-effort: ensure modality concepts exist so the UI can attach them.
        try:
            from ...vontology.utils_vontology import (
                ensure_conversation_modality_concepts,
            )

            ensure_conversation_modality_concepts()
        except Exception:
            pass

        if request.method == "GET":
            links = chat_history_service.get_chat_session_links(
                user_id=user_concept_id,
                session_id=session_id,
                namespace=namespace,
            )
            return (
                jsonify(
                    {
                        "status": "ok",
                        "session_id": session_id,
                        "session_links": links,
                    }
                ),
                200,
            )

        data = request.get_json(silent=True) or {}
        raw_links = data.get("session_links")
        if not isinstance(raw_links, dict):
            # Allow top-level key alternatives for callers.
            raw_links = {
                "programmes": data.get("programmes") or data.get("programme_ids"),
                "projects": data.get("projects") or data.get("project_ids"),
                "activities": data.get("activities") or data.get("activity_ids"),
                "modalities": data.get("modalities") or data.get("modality_ids"),
            }

        # Filter out unknown concept IDs (best-effort) so we don't persist stale references.
        try:
            from ...db.repositories.concepts_repository import ConceptsRepository

            def _flatten(values):
                if values is None:
                    return []
                if isinstance(values, str):
                    return [values]
                if isinstance(values, list):
                    return values
                return []

            all_ids = []
            for key in ("programmes", "projects", "activities", "modalities"):
                all_ids.extend(_flatten(raw_links.get(key)))

            all_ids = [
                v.strip()
                for v in all_ids
                if isinstance(v, str) and v.strip().startswith("#V#")
            ]
            if all_ids:
                found = set(
                    doc.get("concept_id")
                    for doc in ConceptsRepository.find(
                        {"concept_id": {"$in": list(set(all_ids))}},
                        {"concept_id": 1},
                    )
                    if isinstance(doc, dict) and isinstance(doc.get("concept_id"), str)
                )
            else:
                found = set()

            missing = sorted({cid for cid in set(all_ids) if cid not in found})
            if found:
                for key in ("programmes", "projects", "activities", "modalities"):
                    raw_links[key] = [
                        v.strip()
                        for v in _flatten(raw_links.get(key))
                        if isinstance(v, str)
                        and v.strip().startswith("#V#")
                        and v.strip() in found
                    ]
        except Exception:
            missing = []

        result = chat_history_service.set_chat_session_links(
            user_id=user_concept_id,
            session_id=session_id,
            session_links=raw_links,
            namespace=namespace,
        )

        if not result.get("matched"):
            return jsonify({"error": "Session not found"}), 404

        body = {
            "status": "updated" if result.get("updated") else "ok",
            "session_id": session_id,
            "session_links": result.get("session_links") or {},
        }
        if missing:
            body["missing_concepts"] = missing
        return jsonify(body), 200
    except Exception as e:
        print(f"Error updating chat session links: {e}")
        return jsonify({"error": str(e)}), 500


def _normalise_concept_id(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if not value.startswith("#V#"):
        value = f"#V#{value}"
    return value


def _normalise_history_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str) and value.strip():
        return chat_history_service._coerce_datetime(value)
    return None


def _history_merge_key(message: dict) -> tuple | None:
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    content = message.get("content")
    author = message.get("author_user_id")
    ts = message.get("timestamp")
    if isinstance(ts, datetime):
        ts_key = ts.isoformat()
    elif isinstance(ts, str):
        ts_key = ts
    else:
        ts_key = None
    return (role, content, author, ts_key)


def _merge_shared_histories(
    owner_history: list[dict], invitee_history: list[dict]
) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple] = set()

    combined = (owner_history or []) + (invitee_history or [])
    for msg in combined:
        if not isinstance(msg, dict):
            continue
        key = _history_merge_key(msg)
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        merged.append(msg)

    indexed = []
    for idx, msg in enumerate(merged):
        ts = _normalise_history_timestamp(msg.get("timestamp"))
        if ts is None:
            ts = datetime(1970, 1, 1, tzinfo=timezone.utc)
        indexed.append((ts, idx, msg))

    indexed.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in indexed]


def _apply_default_author(
    history: list[dict], author_user_id: str | None
) -> list[dict]:
    if not author_user_id:
        return history
    updated: list[dict] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user" and not msg.get("author_user_id"):
            patched = dict(msg)
            patched["author_user_id"] = author_user_id
            updated.append(patched)
        else:
            updated.append(msg)
    return updated


def _derive_namespace_for_user_org(
    user_concept_id: str | None, org_concept_id: str | None
) -> str | None:
    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return None
    try:
        from ...services.namespace_service import derive_namespace

        user_slug = user_concept_id.strip()
        if user_slug.startswith("#V#"):
            user_slug = user_slug[3:]
        if "@" in user_slug:
            user_slug = user_slug.split("@", 1)[0]
        if "+" in user_slug:
            user_slug = user_slug.split("+", 1)[0]
        user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")

        org_slug = None
        if isinstance(org_concept_id, str) and org_concept_id.strip():
            org_slug = org_concept_id.strip()
            if org_slug.startswith("#V#"):
                org_slug = org_slug[3:]
            if "@" in org_slug:
                org_slug = org_slug.split("@", 1)[0]
            if "+" in org_slug:
                org_slug = org_slug.split("+", 1)[0]
            org_slug = re.sub(r"[^a-z0-9]+", "_", org_slug.strip().lower()).strip("_")

        return (
            derive_namespace(user_slug, org_slug)
            if org_slug
            else derive_namespace(user_slug)
        )
    except Exception:
        return None


def _resolve_shared_conversation_owner(
    *, user_concept_id: str | None, session_id: str | None
) -> tuple[str | None, dict | None]:
    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return None, None
    if not isinstance(session_id, str) or not session_id.strip():
        return user_concept_id.strip(), None
    try:
        from ...services.shared_conversation_service import (
            get_accepted_invite_for_user_session,
            resolve_conversation_owner,
        )

        invite = get_accepted_invite_for_user_session(
            user_concept_id=user_concept_id.strip(),
            session_id=session_id.strip(),
        )
        if not isinstance(invite, dict):
            return user_concept_id.strip(), None
        owner = invite.get("conversation_owner_user_id") or invite.get(
            "inviter_user_id"
        )
        owner_id = _normalise_concept_id(owner)
        if not invite.get("conversation_owner_user_id"):
            try:
                resolved_owner = resolve_conversation_owner(
                    session_id=session_id.strip()
                )
                resolved_owner_id = _normalise_concept_id(resolved_owner)
                if resolved_owner_id:
                    owner_id = resolved_owner_id
            except Exception:
                pass
        return owner_id or user_concept_id.strip(), invite
    except Exception:
        return user_concept_id.strip(), None


def _collect_relationship_concept_ids(concept: dict) -> set[str]:
    relationships = concept.get("relationships") if isinstance(concept, dict) else None
    if not isinstance(relationships, dict):
        return set()
    found: set[str] = set()
    for value in relationships.values():
        if isinstance(value, str):
            cid = _normalise_concept_id(value)
            if cid:
                found.add(cid)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    cid = _normalise_concept_id(item)
                    if cid:
                        found.add(cid)
    return found


def _build_invitee_entry(concept: dict, *, role: str, match_details: dict) -> dict:
    from ...services.concept_service import enrich_concept_with_text_relations
    from ...vontology.utils_vontology import (
        get_concept_display_name_with_names_fallback,
    )

    enriched = enrich_concept_with_text_relations(concept)
    display_name = get_concept_display_name_with_names_fallback(enriched)
    return {
        "concept_id": concept.get("concept_id"),
        "name": display_name or concept.get("name") or "Unknown",
        "role": role,
        "match": match_details,
    }


@von_bp.route("/api/shared_conversations/invitees", methods=["GET"])
def get_shared_conversation_invitees():
    """List eligible invitees for a shared conversation.

    Returns organisation-scoped users, ordered by relevance to session links.
    """
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services import chat_history_service
        from ...services.organisation_membership_service import (
            get_organisation_members,
            get_user_memberships,
        )
        from ...services.concept_service import get_concept_by_concept_id
        from ...services.shared_conversation_service import get_invite_status_map

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        session_id = request.args.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        organisation_concept_id = _normalise_concept_id(
            effective.get("organisation_id")
        )
        if not organisation_concept_id:
            return jsonify({"error": "No organisation context"}), 400

        memberships = get_user_memberships(user_concept_id)
        if not any(
            m.get("organisation_concept_id") == organisation_concept_id
            for m in memberships.get("memberships", [])
        ):
            return jsonify({"error": "Not authorised for organisation"}), 403

        namespace = effective.get(
            "namespace"
        ) or chat_history_service.resolve_chat_history_namespace(user_concept_id)
        session_links = chat_history_service.get_chat_session_links(
            user_id=user_concept_id,
            session_id=session_id,
            namespace=namespace,
        )

        link_sets = {
            key: set(session_links.get(key) or [])
            for key in ("programmes", "projects", "activities", "modalities")
        }

        org_members = get_organisation_members(organisation_concept_id)
        candidates = []
        invitee_ids: list[str] = []
        for member in org_members.get("members", []):
            candidate_id = member.get("user_concept_id")
            candidate_id = _normalise_concept_id(candidate_id)
            if not candidate_id or candidate_id == user_concept_id:
                continue
            concept = get_concept_by_concept_id(concept_id=candidate_id)
            if not isinstance(concept, dict):
                continue

            related_ids = _collect_relationship_concept_ids(concept)
            match_details: dict[str, Any] = {
                key: sorted(link_sets[key] & related_ids) for key in link_sets
            }
            match_score = sum(len(vals) for vals in match_details.values())
            match_details["score"] = match_score

            entry = _build_invitee_entry(
                concept,
                role=member.get("role") or "member",
                match_details=match_details,
            )
            candidates.append(entry)
            invitee_ids.append(candidate_id)

        debug_payload = None
        if request.args.get("debug") == "1" and session.get("role_in_org") == "admin":
            debug_payload = {
                "user_concept_id": user_concept_id,
                "organisation_concept_id": organisation_concept_id,
                "membership_org_ids": [
                    m.get("organisation_concept_id")
                    for m in memberships.get("memberships", [])
                    if m.get("organisation_concept_id")
                ],
                "org_member_count": len(org_members.get("members", [])),
                "candidate_count": len(candidates),
                "excluded_self": user_concept_id,
                "session_links": session_links,
            }

        status_map = get_invite_status_map(
            session_id=session_id, invitee_ids=invitee_ids
        )
        for entry in candidates:
            invitee_id = entry.get("concept_id")
            if invitee_id and invitee_id in status_map:
                entry["invite_status"] = status_map[invitee_id]

        candidates.sort(
            key=lambda item: (
                -(item.get("match", {}).get("score") or 0),
                (item.get("name") or "").lower(),
            )
        )

        return (
            jsonify(
                {
                    "invitees": candidates,
                    "total_count": len(candidates),
                    "session_links": session_links,
                    "ordered_by": "conversation_links",
                    "debug": debug_payload,
                }
            ),
            200,
        )
    except Exception as e:
        print(f"Error listing invitees: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/shared_conversations/invite", methods=["POST"])
def invite_to_shared_conversation():
    """Create a shared conversation invite for a session."""
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services.organisation_membership_service import (
            get_organisation_members,
            get_user_memberships,
        )
        from ...services.shared_conversation_service import create_invite
        from ...services.episode_logging_service import log_episode

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        session_id = data.get("session_id")
        invitee_concept_id = _normalise_concept_id(
            data.get("invitee_concept_id") or data.get("invitee_user_id")
        )

        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        if not invitee_concept_id:
            return jsonify({"error": "invitee_concept_id required"}), 400

        # JVNAUTOSCI-1011: Use window session context if available
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        organisation_concept_id = _normalise_concept_id(
            effective.get("organisation_id")
        )
        if not organisation_concept_id:
            return jsonify({"error": "No organisation context"}), 400

        memberships = get_user_memberships(user_concept_id)
        if not any(
            m.get("organisation_concept_id") == organisation_concept_id
            for m in memberships.get("memberships", [])
        ):
            return jsonify({"error": "Not authorised for organisation"}), 403

        org_members = get_organisation_members(organisation_concept_id)
        valid_ids = {
            _normalise_concept_id(m.get("user_concept_id"))
            for m in org_members.get("members", [])
        }
        if invitee_concept_id not in valid_ids:
            return jsonify({"error": "Invitee not in organisation"}), 403

        result = create_invite(
            session_id=session_id,
            inviter_user_id=user_concept_id,
            invitee_user_id=invitee_concept_id,
            organisation_concept_id=organisation_concept_id,
        )

        log_episode(
            episode_type="shared_conversation_invite_created",
            actor_user_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
            session_id=session_id,
            related_invite_id=result.get("invite", {}).get("invite_id"),
            payload={
                "invitee_user_id": invitee_concept_id,
                "created": bool(result.get("created")),
            },
            status="created" if result.get("created") else "exists",
        )

        return jsonify({"status": "ok", **result}), 200
    except Exception as e:
        print(f"Error creating invite: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/shared_conversations/invites", methods=["GET"])
def list_shared_conversation_invites():
    """List incoming invites for the authenticated user."""
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services.shared_conversation_service import list_invites_for_user

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        status = request.args.get("status") or "pending"
        session_id = request.args.get("session_id")

        invites = list_invites_for_user(
            user_concept_id=user_concept_id,
            status=status,
            direction="incoming",
            session_id=session_id,
        )

        # JVNAUTOSCI-1004/1011: Filter invites by current organisation using window session
        window_session_id = request.headers.get("X-Von-Window-Session")
        effective = get_effective_context(
            window_session_id, dict(session), user_concept_id
        )
        organisation_concept_id = _normalise_concept_id(
            effective.get("organisation_id")
        )
        if organisation_concept_id:
            invites = [
                invite
                for invite in invites
                if (
                    _normalise_concept_id(invite.get("organisation_concept_id"))
                    in (None, organisation_concept_id)
                )
            ]

        return jsonify({"invites": invites, "total_count": len(invites)}), 200
    except Exception as e:
        print(f"Error listing invites: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/shared_conversations/invites/respond", methods=["POST"])
def respond_shared_conversation_invite():
    """Accept or decline a shared conversation invite."""
    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services.shared_conversation_service import respond_to_invite
        from ...services.episode_logging_service import log_episode

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.get_json(silent=True) or {}
        invite_id = data.get("invite_id")
        action = data.get("action")
        if not isinstance(invite_id, str) or not invite_id.strip():
            return jsonify({"error": "invite_id required"}), 400

        updated = respond_to_invite(
            invite_id=invite_id.strip(),
            user_concept_id=user_concept_id,
            action=action or "",
        )
        if not updated:
            return jsonify({"error": "Invite not found"}), 404

        log_episode(
            episode_type="shared_conversation_invite_responded",
            actor_user_id=user_concept_id,
            organisation_concept_id=updated.get("organisation_concept_id"),
            session_id=updated.get("session_id"),
            related_invite_id=invite_id.strip(),
            payload={"action": action},
            status=updated.get("status"),
        )

        return jsonify({"status": "ok", "invite": updated}), 200
    except Exception as e:
        print(f"Error responding to invite: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/shared_conversations/stream", methods=["GET"])
def stream_shared_conversation():
    """SSE endpoint for real-time shared conversation turn updates.

    Query params:
        session_id: The shared conversation session to subscribe to

    Requires:
        - Authenticated user
        - Accepted invite for the session (or ownership)

    Returns:
        SSE stream with events:
            - user_turn: A user message was added
            - assistant_turn: An assistant response was added
            - keepalive: Periodic ping to keep connection alive
    """
    from flask import Response, stream_with_context

    try:
        from ...security.access_control import get_effective_user_concept_id
        from ...services.shared_conversation_service import (
            get_accepted_invite_for_user_session,
            resolve_conversation_owner,
        )
        from ...services.shared_conversation_stream_service import get_stream_service

        user_concept_id = get_effective_user_concept_id()
        if not user_concept_id:
            return jsonify({"error": "Not authenticated"}), 401

        session_id = request.args.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "session_id required"}), 400
        session_id = session_id.strip()

        # Verify access: must be owner or have accepted invite
        is_owner = False
        has_invite = False

        owner_id = resolve_conversation_owner(session_id=session_id)
        if owner_id == user_concept_id:
            is_owner = True
        else:
            invite = get_accepted_invite_for_user_session(
                user_concept_id=user_concept_id,
                session_id=session_id,
            )
            if isinstance(invite, dict):
                has_invite = True

        if not is_owner and not has_invite:
            return jsonify({"error": "Not authorized for this conversation"}), 403

        stream_service = get_stream_service()
        subscriber = stream_service.subscribe(
            session_id=session_id,
            user_concept_id=user_concept_id,
        )

        def generate():
            try:
                for event_data in stream_service.generate_events(subscriber):
                    yield event_data
            finally:
                stream_service.unsubscribe(subscriber)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )
    except Exception as e:
        print(f"Error in shared conversation stream: {e}")
        return jsonify({"error": str(e)}), 500


@von_bp.route("/api/organisations/my_organisations", methods=["GET"])
def get_my_organisations():
    """
    Get list of organisations the user is member of.

    Returns: {organisations: [{concept_id, name, role}, ...], total_count}
    """
    try:
        from ...security.role_resolver import get_all_user_organisations, get_user_role
        from ...services.concept_service import get_concept_by_concept_id

        user_id = (
            session.get("user_id")
            or session.get("user_concept_id")
            or session.get("user_email")
        )
        if not user_id:
            return jsonify({"error": "Not authenticated"}), 401

        requested_user_concept_id = request.args.get("user_concept_id")
        user_concept_id = requested_user_concept_id or session.get("user_concept_id")
        user_email = session.get("user_email")

        def _normalise_relationships(rel):
            if not isinstance(rel, (dict, list)):
                return {}
            if isinstance(rel, list):
                out = {}
                for item in rel:
                    if not isinstance(item, dict):
                        continue
                    pred = item.get("predicate")
                    tgt = item.get("target")
                    if not pred or not tgt:
                        continue
                    out.setdefault(pred, [])
                    if isinstance(tgt, list):
                        out[pred].extend(tgt)
                    else:
                        out[pred].append(tgt)
                return out
            return rel or {}

        def _prettify_concept_id(concept_id: str) -> str:
            return concept_id.replace("#V#", "").replace("_", " ").title()

        # Derive a slug for stub role resolution.
        # Prefer identifiers that are stable/meaningful (concept ID or email) over
        # opaque auth subjects.
        slug_source = user_concept_id or user_email or user_id

        user_slug = str(slug_source)
        if user_slug.startswith("#V#"):
            user_slug = user_slug[3:]
        if "@" in user_slug:
            user_slug = user_slug.split("@", 1)[0]
        if "+" in user_slug:
            user_slug = user_slug.split("+", 1)[0]

        import re

        user_slug = re.sub(r"[^a-z0-9]+", "_", user_slug.strip().lower()).strip("_")

        USER_PREF_ORG_PREDICATE = "#V#member_of_organisation"

        organisations = []

        # Prefer memberships stored on the selected/authenticated user concept.
        if isinstance(user_concept_id, str) and user_concept_id.strip():
            try:
                user_concept = get_concept_by_concept_id(concept_id=user_concept_id)
            except Exception:
                user_concept = None
            if isinstance(user_concept, dict):
                rel = _normalise_relationships(user_concept.get("relationships", {}))
                org_raw = rel.get(USER_PREF_ORG_PREDICATE)

                org_targets: list[str] = []
                if isinstance(org_raw, str) and org_raw:
                    org_targets = [org_raw]
                elif isinstance(org_raw, list):
                    org_targets = [t for t in org_raw if isinstance(t, str) and t]

                for org_cid in org_targets:
                    org_cid = org_cid if org_cid.startswith("#V#") else f"#V#{org_cid}"
                    org_slug = org_cid[3:] if org_cid.startswith("#V#") else org_cid
                    org_slug = org_slug.strip().lower().replace(" ", "_")
                    try:
                        role = get_user_role(user_slug, org_slug)
                    except Exception:
                        role = "member"

                    organisations.append(
                        {
                            "concept_id": org_cid,
                            "name": _prettify_concept_id(org_cid),
                            "role": role,
                        }
                    )

        # Fallback: stub role resolver mappings (Phase 1 hardcoded)
        if not organisations:
            org_roles = get_all_user_organisations(user_slug)
            for org_id, role in org_roles.items():
                concept_id = org_id if org_id.startswith("#V#") else f"#V#{org_id}"
                organisations.append(
                    {
                        "concept_id": concept_id,
                        "name": _prettify_concept_id(concept_id),
                        "role": role,
                    }
                )

        return (
            jsonify(
                {"organisations": organisations, "total_count": len(organisations)}
            ),
            200,
        )

    except Exception as e:
        print(f"Error getting organisations: {e}")
        return jsonify({"error": str(e)}), 500
