"""Compact timing trace support for conversation turn execution.

This module is deliberately a support surface: it records measured facts about
runtime work, represented artefacts, model calls, and prompt identities. It does
not decide routing, model choice, prompt quality, or user-facing behaviour.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

TURN_TIMING_TRACE_SCHEMA_VERSION = "turn_timing_trace.v1"
TURN_TIMING_SPAN_SCHEMA_VERSION = "turn_timing_span.v1"

MAX_TIMING_SPANS = 160
MAX_TIMING_ATTRIBUTES = 24
MAX_TIMING_STRING_CHARS = 300
MAX_TIMING_SLOWEST_SPANS = 20
MAX_TIMING_AGGREGATE_ROWS = 80

_REDACTED_VALUE = "[redacted]"
_STRUCTURAL_VALUE = "[structural]"

_SAFE_CONTENT_ID_KEYS = {
    "prompt_id",
    "prompt_ids",
    "selected_prompt_id",
    "requested_prompt_id",
    "requested_prompt_ids",
    "prompt_hash",
    "prompt_sha256",
    "prompt_char_count",
    "prompt_token_count",
    "prompt_version",
    "prompt_template_id",
    "prompt_template_sha256",
    "system_prompt_id",
    "argument_keys",
    "input_tokens",
    "output_tokens",
    "completion_tokens",
    "total_tokens",
    "workflow_id",
    "selected_workflow_id",
}

_SENSITIVE_KEY_PARTS = {
    "access_token",
    "api_key",
    "apikey",
    "argument",
    "arguments",
    "authorization",
    "body",
    "content",
    "cookie",
    "credential",
    "document",
    "email",
    "id_token",
    "message",
    "messages",
    "password",
    "payload",
    "prompt",
    "raw",
    "refresh_token",
    "response",
    "secret",
    "set_cookie",
    "token",
}


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_TIMING_STRING_CHARS:
        return cleaned[:MAX_TIMING_STRING_CHARS] + "...[truncated]"
    return cleaned


def _safe_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _safe_int_ms(value: Any) -> int | None:
    parsed = _safe_number(value)
    if parsed is None:
        return None
    return int(max(0.0, parsed))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _json_size(value: Any) -> int:
    return len(json.dumps(value, default=str, ensure_ascii=True, separators=(",", ":")))


def _parse_iso_ms(value: Any) -> int | None:
    raw = _safe_str(value)
    if not raw:
        return None
    try:
        normalised = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalised)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000.0)
    except ValueError:
        return None


def _iso_from_ms(value_ms: int | None) -> str | None:
    if value_ms is None:
        return None
    try:
        return (
            datetime.fromtimestamp(value_ms / 1000.0, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _iso_minus_ms(value: Any, duration_ms: int | None) -> str | None:
    end_ms = _parse_iso_ms(value)
    if end_ms is None or duration_ms is None:
        return None
    return _iso_from_ms(max(0, end_ms - duration_ms))


def _duration_between_iso_ms(started_at: Any, ended_at: Any) -> int | None:
    start_ms = _parse_iso_ms(started_at)
    end_ms = _parse_iso_ms(ended_at)
    if start_ms is None or end_ms is None or end_ms < start_ms:
        return None
    return int(end_ms - start_ms)


def _key_is_sensitive(key: str) -> bool:
    key_lc = key.strip().lower()
    if key_lc in _SAFE_CONTENT_ID_KEYS:
        return False
    return any(part in key_lc for part in _SENSITIVE_KEY_PARTS)


def _safe_attribute_value(key: str, value: Any) -> Any:
    if (
        key.strip().lower() in _SAFE_CONTENT_ID_KEYS
        and isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
    ):
        return [
            cleaned
            for cleaned in (_safe_str(item) for item in value[:20])
            if cleaned is not None
        ]
    if _key_is_sensitive(key):
        if isinstance(value, str) and value:
            return {
                "redacted": True,
                "char_count": len(value),
                "sha256": _hash_text(value),
            }
        if isinstance(value, Mapping):
            return {
                "redacted": True,
                "key_count": len(value),
                "keys_sample": sorted(str(item) for item in value.keys())[:12],
            }
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return {"redacted": True, "item_count": len(value)}
        return _REDACTED_VALUE

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    parsed_number = _safe_number(value)
    if parsed_number is not None:
        return parsed_number
    cleaned = _safe_str(value)
    if cleaned is not None:
        return cleaned
    if isinstance(value, Mapping):
        return {
            "kind": _STRUCTURAL_VALUE,
            "key_count": len(value),
            "keys_sample": sorted(str(item) for item in value.keys())[:12],
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {"kind": _STRUCTURAL_VALUE, "item_count": len(value)}
    return _safe_str(str(value))


def _normalise_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(attributes, Mapping):
        return {}
    normalised: dict[str, Any] = {}
    for raw_key, value in attributes.items():
        key = _safe_str(raw_key)
        if not key:
            continue
        normalised[key] = _safe_attribute_value(key, value)
        if len(normalised) >= MAX_TIMING_ATTRIBUTES:
            normalised["attributes_truncated"] = True
            break
    return normalised


def _make_span_id(source: str, index: int, seed: Any) -> str:
    digest = hashlib.sha1(  # nosec B324 - compact non-security identifier
        json.dumps([source, index, seed], default=str, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"{source}:{digest}"


def _normalise_status(value: Any, *, success: Any = None) -> str | None:
    if isinstance(success, bool):
        return "success" if success else "failure"
    raw = (_safe_str(value) or "").lower()
    if not raw:
        return None
    if raw in {"ok", "complete", "completed", "success", "succeeded"}:
        return "success"
    if raw in {"error", "failed", "failure", "cancelled", "canceled", "blocked"}:
        return "failure"
    return raw


def _normalise_span(
    span: Mapping[str, Any], *, index: int, source: str
) -> dict[str, Any] | None:
    stage = (
        _safe_str(span.get("stage_id"))
        or _safe_str(span.get("workflow_stage_id"))
        or _safe_str(span.get("stage"))
        or _safe_str(span.get("phase"))
        or "unscoped"
    )
    operation_kind = _safe_str(span.get("operation_kind")) or source
    operation_name = (
        _safe_str(span.get("operation_name"))
        or _safe_str(span.get("name"))
        or _safe_str(span.get("event_kind"))
        or _safe_str(span.get("status"))
        or operation_kind
    )
    duration_ms = _safe_int_ms(span.get("duration_ms"))
    started_at = _safe_str(span.get("started_at_utc")) or _safe_str(
        span.get("started_at")
    )
    ended_at = (
        _safe_str(span.get("ended_at_utc"))
        or _safe_str(span.get("completed_at_utc"))
        or _safe_str(span.get("at_utc"))
    )
    if duration_ms is None:
        duration_ms = _duration_between_iso_ms(started_at, ended_at)
    if started_at is None and ended_at is not None:
        started_at = _iso_minus_ms(ended_at, duration_ms)
    if ended_at is None and started_at is not None and duration_ms is not None:
        start_ms = _parse_iso_ms(started_at)
        if start_ms is not None:
            ended_at = _iso_from_ms(start_ms + duration_ms)
    if duration_ms is None:
        return None

    attributes = _normalise_attributes(
        span.get("attributes") if isinstance(span.get("attributes"), Mapping) else span
    )
    status = _normalise_status(span.get("status"), success=span.get("success"))

    payload: dict[str, Any] = {
        "schema_version": TURN_TIMING_SPAN_SCHEMA_VERSION,
        "span_id": _safe_str(span.get("span_id"))
        or _make_span_id(
            source,
            index,
            [stage, operation_kind, operation_name, started_at, duration_ms],
        ),
        "parent_span_id": _safe_str(span.get("parent_span_id")),
        "source": _safe_str(span.get("source")) or source,
        "stage_id": stage,
        "operation_kind": operation_kind,
        "operation_name": operation_name,
        "started_at_utc": started_at,
        "ended_at_utc": ended_at,
        "duration_ms": duration_ms,
        "status": status,
        "attributes": attributes,
    }
    error_class = _safe_str(span.get("error_class"))
    if error_class:
        payload["error_class"] = error_class
    return {key: value for key, value in payload.items() if value is not None}


def _entry_timestamp_ms(entry: Mapping[str, Any]) -> int | None:
    for key in ("timestamp", "timestamp_ms", "elapsed_ms"):
        value = _safe_int_ms(entry.get(key))
        if value is not None:
            return value
    for key in ("at_utc", "started_at_utc", "completed_at_utc", "ended_at_utc"):
        value = _parse_iso_ms(entry.get(key))
        if value is not None:
            return value
    return None


def _latest_timestamp_ms(
    *collections: Sequence[Mapping[str, Any]] | None
) -> int | None:
    latest: int | None = None
    for collection in collections:
        if not isinstance(collection, Sequence):
            continue
        for entry in collection:
            if not isinstance(entry, Mapping):
                continue
            timestamp = _entry_timestamp_ms(entry)
            if timestamp is None:
                continue
            latest = timestamp if latest is None else max(latest, timestamp)
    return latest


def _phase_history_spans(
    phase_history: Sequence[Mapping[str, Any]],
    *,
    latest_timestamp_ms: int | None,
) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for index, entry in enumerate(phase_history):
        if not isinstance(entry, Mapping):
            continue
        phase = _safe_str(entry.get("phase")) or _safe_str(entry.get("stage"))
        start_ms = _safe_int_ms(entry.get("timestamp"))
        if not phase or start_ms is None:
            continue
        next_ms = None
        next_at = None
        if index + 1 < len(phase_history):
            next_entry = phase_history[index + 1]
            if isinstance(next_entry, Mapping):
                next_ms = _safe_int_ms(next_entry.get("timestamp"))
                next_at = _safe_str(next_entry.get("at_utc"))
        if next_ms is None:
            next_ms = latest_timestamp_ms
        if next_ms is None or next_ms < start_ms:
            continue
        spans.append(
            {
                "span_id": _make_span_id(
                    "phase_history", index, [phase, start_ms, next_ms]
                ),
                "source": "phase_history",
                "stage_id": phase,
                "operation_kind": "phase",
                "operation_name": phase,
                "started_at_utc": _safe_str(entry.get("at_utc"))
                or _iso_from_ms(start_ms),
                "ended_at_utc": next_at or _iso_from_ms(next_ms),
                "duration_ms": int(next_ms - start_ms),
                "status": _safe_str(entry.get("status")),
                "attributes": {
                    "phase_label": _safe_str(entry.get("phase_label")),
                    "sequence_no": entry.get("sequence_no"),
                },
            }
        )
    return spans


def _prompt_metadata(entry: Mapping[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    prompt_metadata = entry.get("prompt_metadata")
    if isinstance(prompt_metadata, Mapping):
        for key in (
            "prompt_id",
            "selected_prompt_id",
            "requested_prompt_id",
            "prompt_sha256",
            "prompt_hash",
            "prompt_version",
            "prompt_template_id",
        ):
            value = _safe_str(prompt_metadata.get(key))
            if value:
                attributes[key] = value
    for key in (
        "prompt_id",
        "selected_prompt_id",
        "requested_prompt_id",
        "prompt_sha256",
        "prompt_hash",
        "prompt_version",
        "prompt_template_id",
    ):
        value = _safe_str(entry.get(key))
        if value:
            attributes.setdefault(key, value)
    for key in ("prompt_text", "prompt"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            attributes.setdefault("prompt_sha256", _hash_text(value))
            attributes.setdefault("prompt_char_count", len(value))
            break
    return attributes


def _first_output_latency_ms(entry: Mapping[str, Any]) -> int | None:
    first_output_at = _safe_str(entry.get("first_output_at_utc")) or _safe_str(
        entry.get("llm_first_output_at_utc")
    )
    if not first_output_at:
        return None
    start_at = (
        _safe_str(entry.get("llm_request_sent_at_utc"))
        or _safe_str(entry.get("request_sent_at_utc"))
        or _safe_str(entry.get("started_at_utc"))
        or _safe_str(entry.get("started_at"))
    )
    return _duration_between_iso_ms(start_at, first_output_at)


def _llm_call_spans(llm_calls: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for index, entry in enumerate(llm_calls):
        if not isinstance(entry, Mapping):
            continue
        duration_ms = _safe_int_ms(entry.get("duration_ms"))
        if duration_ms is None:
            duration_ms = _duration_between_iso_ms(
                entry.get("started_at_utc"),
                entry.get("completed_at_utc") or entry.get("ended_at_utc"),
            )
        if duration_ms is None:
            continue
        stage = (
            _safe_str(entry.get("workflow_stage_id"))
            or _safe_str(entry.get("stage"))
            or _safe_str(entry.get("phase"))
            or "unscoped"
        )
        model = _safe_str(entry.get("model")) or _safe_str(entry.get("model_name"))
        provider = _safe_str(entry.get("provider"))
        attributes = {
            "model": model,
            "provider": provider,
            "client_type": _safe_str(entry.get("client_type")),
            "workflow_id": _safe_str(entry.get("workflow_id"))
            or _safe_str(entry.get("selected_workflow_id")),
            "llm_exchange_id": _safe_str(entry.get("llm_exchange_id")),
            "input_tokens": entry.get("input_tokens"),
            "output_tokens": entry.get("output_tokens"),
            "total_tokens": entry.get("total_tokens"),
            **_prompt_metadata(entry),
        }
        first_output_latency = _first_output_latency_ms(entry)
        if first_output_latency is not None:
            attributes["first_output_latency_ms"] = first_output_latency
        spans.append(
            {
                "span_id": _safe_str(entry.get("span_id"))
                or _safe_str(entry.get("llm_exchange_id"))
                or _make_span_id(
                    "llm_call", index, [stage, model, provider, duration_ms]
                ),
                "source": "llm_calls",
                "stage_id": stage,
                "operation_kind": "llm_call",
                "operation_name": model or "llm_call",
                "started_at_utc": _safe_str(entry.get("started_at_utc")),
                "ended_at_utc": _safe_str(entry.get("completed_at_utc"))
                or _safe_str(entry.get("ended_at_utc"))
                or _safe_str(entry.get("at_utc")),
                "duration_ms": duration_ms,
                "status": _normalise_status(
                    entry.get("status"), success=entry.get("success")
                ),
                "attributes": attributes,
            }
        )
    return spans


def _tool_name(entry: Mapping[str, Any]) -> str:
    return (
        _safe_str(entry.get("tool"))
        or _safe_str(entry.get("method"))
        or _safe_str(entry.get("tool_name"))
        or "unknown_tool"
    )


def _tool_spans(
    tool_entries: Sequence[Mapping[str, Any]],
    *,
    source: str,
) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for index, entry in enumerate(tool_entries):
        if not isinstance(entry, Mapping):
            continue
        duration_ms = _safe_int_ms(entry.get("duration_ms"))
        if duration_ms is None:
            continue
        tool_name = _tool_name(entry)
        argument_keys: list[str] = []
        for arguments_key in ("effective_arguments", "arguments", "payload"):
            arguments = entry.get(arguments_key)
            if isinstance(arguments, Mapping):
                argument_keys = sorted(str(key) for key in arguments.keys())[:20]
                break
        spans.append(
            {
                "span_id": _safe_str(entry.get("span_id"))
                or _safe_str(entry.get("call_id"))
                or _make_span_id(source, index, [tool_name, duration_ms]),
                "source": source,
                "stage_id": _safe_str(entry.get("workflow_stage_id"))
                or _safe_str(entry.get("workflow_state_id"))
                or _safe_str(entry.get("stage"))
                or _safe_str(entry.get("phase"))
                or "tool_execute",
                "operation_kind": "tool_call",
                "operation_name": tool_name,
                "duration_ms": duration_ms,
                "status": _normalise_status(
                    entry.get("status"), success=entry.get("success")
                ),
                "attributes": {
                    "tool": tool_name,
                    "method": _safe_str(entry.get("method")) or tool_name,
                    "call_id": _safe_str(entry.get("call_id")),
                    "workflow_id": _safe_str(entry.get("workflow_id")),
                    "workflow_state_id": _safe_str(entry.get("workflow_state_id")),
                    "workflow_action_id": _safe_str(entry.get("workflow_action_id")),
                    "argument_keys": argument_keys,
                    "blocked": entry.get("blocked"),
                    "direct_user_call": entry.get("direct_user_call"),
                },
            }
        )
    return spans


def _response_transformation_spans(
    response_transformations: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(response_transformations, Mapping):
        return []
    raw_entries = response_transformations.get("transformations")
    if not isinstance(raw_entries, list):
        return []
    spans: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, Mapping):
            continue
        duration_ms = _safe_int_ms(entry.get("latency_ms"))
        if duration_ms is None:
            continue
        transform_name = (
            _safe_str(entry.get("transform_name")) or "response_transformation"
        )
        if transform_name == "spoken_backfill":
            stage = "narration"
        elif transform_name == "screen_backfill":
            stage = "screen_backfill"
        else:
            stage = _safe_str(entry.get("stage")) or "response_finalising"
        spans.append(
            {
                "span_id": _make_span_id(
                    "response_transformation", index, [transform_name, duration_ms]
                ),
                "source": "response_transformations",
                "stage_id": stage,
                "operation_kind": "response_transformation",
                "operation_name": transform_name,
                "duration_ms": duration_ms,
                "status": _normalise_status(entry.get("status")),
                "error_class": _safe_str(entry.get("error_class")),
                "attributes": {
                    "transform_name": transform_name,
                    "source_path": _safe_str(entry.get("source_path")),
                    "model_id": _safe_str(entry.get("model_id")),
                    "suppression_reason": _safe_str(entry.get("suppression_reason")),
                },
            }
        )
    return spans


def _diagnostic_event_spans(
    diagnostic_events: Sequence[Mapping[str, Any]],
    *,
    include_llm_events: bool,
    include_tool_events: bool,
) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for index, entry in enumerate(diagnostic_events):
        if not isinstance(entry, Mapping):
            continue
        duration_ms = _safe_int_ms(entry.get("duration_ms"))
        if duration_ms is None:
            continue
        event_kind = (_safe_str(entry.get("event_kind")) or "").lower()
        status = (_safe_str(entry.get("status")) or "").lower()
        is_llm = "llm" in event_kind or "llm" in status or entry.get("llm_exchange_id")
        is_tool = "tool" in event_kind or "tool" in status or entry.get("tool")
        if is_llm and not include_llm_events:
            continue
        if is_tool and not include_tool_events:
            continue
        operation_kind = "diagnostic_event"
        if is_llm:
            operation_kind = "llm_call"
        elif is_tool:
            operation_kind = "tool_call"
        elif "workflow_step" in event_kind or "workflow_step" in status:
            operation_kind = "workflow_step"
        spans.append(
            {
                "span_id": _safe_str(entry.get("span_id"))
                or _safe_str(entry.get("call_id"))
                or _safe_str(entry.get("llm_exchange_id"))
                or _make_span_id(
                    "diagnostic_event", index, [event_kind, status, duration_ms]
                ),
                "source": "diagnostic_events",
                "stage_id": _safe_str(entry.get("workflow_stage_id"))
                or _safe_str(entry.get("stage"))
                or _safe_str(entry.get("phase"))
                or "unscoped",
                "operation_kind": operation_kind,
                "operation_name": _safe_str(entry.get("tool"))
                or _safe_str(entry.get("workflow_task"))
                or _safe_str(entry.get("subtask"))
                or _safe_str(entry.get("event_kind"))
                or _safe_str(entry.get("status"))
                or operation_kind,
                "started_at_utc": _iso_minus_ms(entry.get("at_utc"), duration_ms),
                "ended_at_utc": _safe_str(entry.get("at_utc")),
                "duration_ms": duration_ms,
                "status": _normalise_status(
                    entry.get("status"), success=entry.get("success")
                ),
                "attributes": {
                    "event_kind": _safe_str(entry.get("event_kind")),
                    "workflow_task": _safe_str(entry.get("workflow_task")),
                    "call_id": _safe_str(entry.get("call_id")),
                    "llm_exchange_id": _safe_str(entry.get("llm_exchange_id")),
                    "model": _safe_str(entry.get("model")),
                    "provider": _safe_str(entry.get("provider")),
                    "result_summary": _safe_str(entry.get("result_summary")),
                    **_prompt_metadata(entry),
                },
            }
        )
    return spans


def _aggregate_stage_totals(spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    order: dict[str, int] = {}
    for span in spans:
        stage = _safe_str(span.get("stage_id")) or "unscoped"
        duration_ms = _safe_int_ms(span.get("duration_ms"))
        if duration_ms is None:
            continue
        bucket = buckets.get(stage)
        if bucket is None:
            bucket = {
                "stage_id": stage,
                "span_count": 0,
                "phase_elapsed_ms": 0,
                "operation_elapsed_ms": 0,
                "llm_elapsed_ms": 0,
                "tool_elapsed_ms": 0,
                "support_elapsed_ms": 0,
                "response_transformation_elapsed_ms": 0,
            }
            buckets[stage] = bucket
            order[stage] = len(order)
        operation_kind = _safe_str(span.get("operation_kind")) or "unknown"
        bucket["span_count"] = int(bucket["span_count"]) + 1
        if operation_kind == "phase":
            bucket["phase_elapsed_ms"] = int(bucket["phase_elapsed_ms"]) + duration_ms
        else:
            bucket["operation_elapsed_ms"] = (
                int(bucket["operation_elapsed_ms"]) + duration_ms
            )
        if operation_kind == "llm_call":
            bucket["llm_elapsed_ms"] = int(bucket["llm_elapsed_ms"]) + duration_ms
        elif operation_kind == "tool_call":
            bucket["tool_elapsed_ms"] = int(bucket["tool_elapsed_ms"]) + duration_ms
        elif operation_kind == "response_transformation":
            bucket["response_transformation_elapsed_ms"] = (
                int(bucket["response_transformation_elapsed_ms"]) + duration_ms
            )
        elif operation_kind not in {"phase", "llm_call", "tool_call"}:
            bucket["support_elapsed_ms"] = (
                int(bucket["support_elapsed_ms"]) + duration_ms
            )
    return sorted(
        buckets.values(),
        key=lambda item: (
            order.get(str(item.get("stage_id")), 1_000_000),
            str(item.get("stage_id")),
        ),
    )[:MAX_TIMING_AGGREGATE_ROWS]


def _aggregate_operation_totals(
    spans: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for span in spans:
        duration_ms = _safe_int_ms(span.get("duration_ms"))
        if duration_ms is None:
            continue
        operation_kind = _safe_str(span.get("operation_kind")) or "unknown"
        operation_name = _safe_str(span.get("operation_name")) or operation_kind
        key = (operation_kind, operation_name)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "operation_kind": operation_kind,
                "operation_name": operation_name,
                "span_count": 0,
                "duration_ms": 0,
            }
            buckets[key] = bucket
        bucket["span_count"] = int(bucket["span_count"]) + 1
        bucket["duration_ms"] = int(bucket["duration_ms"]) + duration_ms
    return sorted(
        buckets.values(),
        key=lambda item: (
            -int(item.get("duration_ms") or 0),
            str(item.get("operation_kind")),
        ),
    )[:MAX_TIMING_AGGREGATE_ROWS]


def _numeric_summary(values: Sequence[int]) -> dict[str, int | None]:
    if not values:
        return {"min_ms": None, "max_ms": None, "mean_ms": None}
    total = sum(values)
    return {
        "min_ms": min(values),
        "max_ms": max(values),
        "mean_ms": int(round(total / len(values))),
    }


def _model_prompt_summary(spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[
        tuple[str, str | None, str | None, str | None, str | None], dict[str, Any]
    ] = {}
    first_output_latencies: defaultdict[
        tuple[str, str | None, str | None, str | None, str | None], list[int]
    ] = defaultdict(list)
    for span in spans:
        if _safe_str(span.get("operation_kind")) != "llm_call":
            continue
        duration_ms = _safe_int_ms(span.get("duration_ms"))
        if duration_ms is None:
            continue
        attrs = (
            span.get("attributes")
            if isinstance(span.get("attributes"), Mapping)
            else {}
        )
        model = _safe_str(attrs.get("model")) or _safe_str(span.get("operation_name"))
        provider = _safe_str(attrs.get("provider"))
        prompt_id = (
            _safe_str(attrs.get("prompt_id"))
            or _safe_str(attrs.get("selected_prompt_id"))
            or _safe_str(attrs.get("requested_prompt_id"))
        )
        prompt_sha256 = _safe_str(attrs.get("prompt_sha256")) or _safe_str(
            attrs.get("prompt_hash")
        )
        stage = _safe_str(span.get("stage_id")) or "unscoped"
        key = (stage, provider, model, prompt_id, prompt_sha256)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "stage_id": stage,
                "provider": provider,
                "model": model,
                "prompt_id": prompt_id,
                "prompt_sha256": prompt_sha256,
                "call_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "duration_ms": 0,
                "max_prompt_char_count": None,
                "first_output_latency_ms": {
                    "min_ms": None,
                    "max_ms": None,
                    "mean_ms": None,
                },
            }
            buckets[key] = bucket
        bucket["call_count"] = int(bucket["call_count"]) + 1
        bucket["duration_ms"] = int(bucket["duration_ms"]) + duration_ms
        status = _safe_str(span.get("status"))
        if status == "success":
            bucket["success_count"] = int(bucket["success_count"]) + 1
        elif status == "failure":
            bucket["failure_count"] = int(bucket["failure_count"]) + 1
        prompt_char_count = _safe_int_ms(attrs.get("prompt_char_count"))
        if prompt_char_count is not None:
            existing_prompt_chars = bucket.get("max_prompt_char_count")
            bucket["max_prompt_char_count"] = max(
                prompt_char_count,
                int(existing_prompt_chars)
                if isinstance(existing_prompt_chars, int)
                else 0,
            )
        latency_ms = _safe_int_ms(attrs.get("first_output_latency_ms"))
        if latency_ms is not None:
            first_output_latencies[key].append(latency_ms)

    for key, latencies in first_output_latencies.items():
        bucket = buckets.get(key)
        if bucket is not None:
            bucket["first_output_latency_ms"] = _numeric_summary(latencies)

    return sorted(
        buckets.values(),
        key=lambda item: (
            str(item.get("stage_id")),
            -int(item.get("duration_ms") or 0),
            str(item.get("model")),
        ),
    )[:MAX_TIMING_AGGREGATE_ROWS]


def _slowest_spans(spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = sorted(
        spans,
        key=lambda item: -int(_safe_int_ms(item.get("duration_ms")) or 0),
    )[:MAX_TIMING_SLOWEST_SPANS]
    slowest: list[dict[str, Any]] = []
    for span in rows:
        attrs = (
            span.get("attributes")
            if isinstance(span.get("attributes"), Mapping)
            else {}
        )
        slowest.append(
            {
                "span_id": _safe_str(span.get("span_id")),
                "stage_id": _safe_str(span.get("stage_id")),
                "operation_kind": _safe_str(span.get("operation_kind")),
                "operation_name": _safe_str(span.get("operation_name")),
                "duration_ms": _safe_int_ms(span.get("duration_ms")),
                "status": _safe_str(span.get("status")),
                "source": _safe_str(span.get("source")),
                "model": _safe_str(attrs.get("model")),
                "provider": _safe_str(attrs.get("provider")),
                "prompt_id": _safe_str(attrs.get("prompt_id"))
                or _safe_str(attrs.get("selected_prompt_id")),
            }
        )
    return slowest


def build_turn_timing_trace(
    *,
    request_id: str | None,
    phase_history: Sequence[Mapping[str, Any]] | None = None,
    diagnostic_events: Sequence[Mapping[str, Any]] | None = None,
    llm_calls: Sequence[Mapping[str, Any]] | None = None,
    tool_history: Sequence[Mapping[str, Any]] | None = None,
    tool_invocations: Sequence[Mapping[str, Any]] | None = None,
    response_transformations: Mapping[str, Any] | None = None,
    extra_spans: Sequence[Mapping[str, Any]] | None = None,
    elapsed_ms_value: int | None = None,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    """Build a bounded span trace from live and persisted turn telemetry."""

    phases = [entry for entry in (phase_history or []) if isinstance(entry, Mapping)]
    events = [
        entry for entry in (diagnostic_events or []) if isinstance(entry, Mapping)
    ]
    llm_entries = [entry for entry in (llm_calls or []) if isinstance(entry, Mapping)]
    invocation_entries = [
        entry for entry in (tool_invocations or []) if isinstance(entry, Mapping)
    ]
    history_tool_entries = [
        entry for entry in (tool_history or []) if isinstance(entry, Mapping)
    ]
    explicit_spans = [
        entry for entry in (extra_spans or []) if isinstance(entry, Mapping)
    ]

    latest_ms = _latest_timestamp_ms(phases, events, explicit_spans)
    raw_spans: list[dict[str, Any]] = []
    raw_spans.extend(_phase_history_spans(phases, latest_timestamp_ms=latest_ms))
    raw_spans.extend(_llm_call_spans(llm_entries))
    raw_spans.extend(
        _tool_spans(
            invocation_entries if invocation_entries else history_tool_entries,
            source="tool_invocations" if invocation_entries else "tool_history",
        )
    )
    raw_spans.extend(_response_transformation_spans(response_transformations))
    raw_spans.extend(
        _diagnostic_event_spans(
            events,
            include_llm_events=not bool(llm_entries),
            include_tool_events=not bool(invocation_entries or history_tool_entries),
        )
    )
    raw_spans.extend(
        dict(span, source=span.get("source") or "explicit_span")
        for span in explicit_spans
    )

    normalised: list[dict[str, Any]] = []
    seen_span_ids: set[str] = set()
    for index, raw_span in enumerate(raw_spans):
        span = _normalise_span(
            raw_span, index=index, source=str(raw_span.get("source") or "span")
        )
        if span is None:
            continue
        span_id = str(span.get("span_id"))
        if span_id in seen_span_ids:
            continue
        seen_span_ids.add(span_id)
        normalised.append(span)

    normalised.sort(
        key=lambda item: (
            _parse_iso_ms(item.get("started_at_utc")) or 9_999_999_999_999,
            str(item.get("span_id")),
        )
    )
    span_count = len(normalised)
    stored_spans = normalised[:MAX_TIMING_SPANS]
    dropped_span_count = max(0, span_count - len(stored_spans))

    operation_total_ms = sum(
        int(span["duration_ms"])
        for span in stored_spans
        if _safe_int_ms(span.get("duration_ms")) is not None
        and _safe_str(span.get("operation_kind")) != "phase"
    )
    phase_total_ms = sum(
        int(span["duration_ms"])
        for span in stored_spans
        if _safe_int_ms(span.get("duration_ms")) is not None
        and _safe_str(span.get("operation_kind")) == "phase"
    )

    trace = {
        "schema_version": TURN_TIMING_TRACE_SCHEMA_VERSION,
        "generated_at_utc": _safe_str(generated_at_utc) or _now_utc_iso(),
        "request_id": _safe_str(request_id),
        "spans": stored_spans,
        "span_count": span_count,
        "stored_span_count": len(stored_spans),
        "dropped_span_count": dropped_span_count,
        "truncated": dropped_span_count > 0,
        "summary": {
            "elapsed_ms": elapsed_ms_value,
            "phase_elapsed_ms": phase_total_ms,
            "operation_elapsed_ms": operation_total_ms,
            "llm_elapsed_ms": sum(
                int(span["duration_ms"])
                for span in stored_spans
                if _safe_str(span.get("operation_kind")) == "llm_call"
            ),
            "tool_elapsed_ms": sum(
                int(span["duration_ms"])
                for span in stored_spans
                if _safe_str(span.get("operation_kind")) == "tool_call"
            ),
        },
        "stage_totals": _aggregate_stage_totals(stored_spans),
        "operation_totals": _aggregate_operation_totals(stored_spans),
        "slowest_spans": _slowest_spans(stored_spans),
        "model_prompt_summary": _model_prompt_summary(stored_spans),
        "size_guardrail": {
            "max_spans": MAX_TIMING_SPANS,
            "max_attributes_per_span": MAX_TIMING_ATTRIBUTES,
            "span_count": span_count,
            "stored_span_count": len(stored_spans),
            "dropped_span_count": dropped_span_count,
        },
    }
    trace["size_guardrail"]["json_size_chars"] = _json_size(trace)
    return trace


def merge_timing_spans(
    existing: Sequence[Mapping[str, Any]] | None,
    incoming: Sequence[Mapping[str, Any]] | None,
    *,
    max_spans: int = MAX_TIMING_SPANS,
) -> list[dict[str, Any]]:
    """Merge compact span updates without unbounded live-progress growth."""

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for collection in (existing or [], incoming or []):
        if not isinstance(collection, Sequence):
            continue
        for index, span in enumerate(collection):
            if not isinstance(span, Mapping):
                continue
            normalised = _normalise_span(
                span,
                index=index,
                source=_safe_str(span.get("source")) or "explicit_span",
            )
            if normalised is None:
                continue
            span_id = str(normalised.get("span_id"))
            if span_id in seen:
                continue
            seen.add(span_id)
            merged.append(normalised)
    merged.sort(
        key=lambda item: (
            _parse_iso_ms(item.get("started_at_utc")) or 9_999_999_999_999,
            str(item.get("span_id")),
        )
    )
    return merged[-max_spans:]


class TurnTimingRecorder:
    """Small in-process recorder for route-level support spans."""

    def __init__(self, *, request_id: str | None = None) -> None:
        self.request_id = request_id
        self._spans: list[dict[str, Any]] = []

    @contextmanager
    def span(
        self,
        *,
        stage_id: str,
        operation_kind: str,
        operation_name: str,
        parent_span_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[None]:
        started_perf = time.perf_counter()
        started_at = datetime.now(timezone.utc)
        span_id = f"support:{uuid.uuid4().hex[:16]}"
        error_class: str | None = None
        try:
            yield
        except Exception as exc:
            error_class = type(exc).__name__
            raise
        finally:
            ended_at = datetime.now(timezone.utc)
            duration_ms = int(max(0.0, (time.perf_counter() - started_perf) * 1000.0))
            self._spans.append(
                {
                    "schema_version": TURN_TIMING_SPAN_SCHEMA_VERSION,
                    "span_id": span_id,
                    "parent_span_id": parent_span_id,
                    "source": "route_support",
                    "stage_id": stage_id,
                    "operation_kind": operation_kind,
                    "operation_name": operation_name,
                    "started_at_utc": started_at.isoformat().replace("+00:00", "Z"),
                    "ended_at_utc": ended_at.isoformat().replace("+00:00", "Z"),
                    "duration_ms": duration_ms,
                    "status": "failure" if error_class else "success",
                    "error_class": error_class,
                    "attributes": _normalise_attributes(attributes),
                }
            )
            if len(self._spans) > MAX_TIMING_SPANS:
                self._spans = self._spans[-MAX_TIMING_SPANS:]

    def spans(self) -> list[dict[str, Any]]:
        return [dict(span) for span in self._spans]

    def snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return build_turn_timing_trace(
            request_id=self.request_id,
            extra_spans=self.spans(),
            **kwargs,
        )
