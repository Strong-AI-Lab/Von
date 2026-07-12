"""Read-only access to live turn-progress snapshots stored for active requests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .namespace_service import derive_actor_context_from_namespace
from ..workflows.terminal_outcome_receipts import (
    build_terminal_outcome_receipt_projection,
    terminal_outcome_receipt_projection_from_record,
)

_DEFAULT_SECTION_LIMIT = 20
_MAX_SECTION_LIMIT = 200
_DEFAULT_RECENT_EVENT_LIMIT = 5

_LIVE_PROGRESS_DETAIL_SECTIONS = frozenset(
    {
        "activity_history",
        "diagnostic_events",
        "llm_request",
        "progress_events",
        "selected_workflow_execution",
        "stage_diagnostics",
        "terminal_outcome_receipt_projection",
        "thinking_interpretability",
        "timing_spans",
        "turn_timing_trace",
        "workflow_routing_diagnostics",
        "workflow_stage_path",
    }
)


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _json_size(value: Any) -> int:
    return len(json.dumps(value, default=str, ensure_ascii=True))


def _tail_list(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    if limit <= 0:
        return []
    return list(value[-limit:])


def _summarise_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    summary: dict[str, Any] = {}
    for key in (
        "schema_version",
        "stage",
        "stage_id",
        "stage_label",
        "phase",
        "status",
        "state",
        "result_summary",
        "latest_result_summary",
        "event_count",
        "workflow_id",
        "selected_workflow_id",
        "selected_workflow_name",
        "has_unmapped_runtime_stages",
    ):
        item = value.get(key)
        if isinstance(item, str | int | float | bool) or item is None:
            summary[key] = item
    if not summary:
        return None
    summary["json_size_chars"] = _json_size(value)
    return summary


def _section_counts(payload: Mapping[str, Any]) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    for section in sorted(_LIVE_PROGRESS_DETAIL_SECTIONS):
        value = payload.get(section)
        if isinstance(value, list):
            counts[section] = len(value)
        elif value is None:
            counts[section] = None
        else:
            counts[section] = 1
    return counts


def _build_live_terminal_outcome_receipt_projection(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = payload.get("terminal_outcome_receipt")
    validation = payload.get("terminal_outcome_receipt_validation")
    source = "live_progress.terminal_outcome_receipt"

    if not isinstance(receipt, Mapping):
        completion_gate = payload.get("completion_gate")
        if not isinstance(completion_gate, Mapping):
            completion_gate = payload.get("completion_gate_verdict")
        if isinstance(completion_gate, Mapping):
            evidence_payload = completion_gate.get("evidence_payload")
            if isinstance(evidence_payload, Mapping):
                receipt = evidence_payload.get("terminal_outcome_receipt")
                validation = evidence_payload.get(
                    "terminal_outcome_receipt_validation"
                )
                source = "live_progress.completion_gate.evidence_payload"

    if not isinstance(receipt, Mapping):
        stage_diagnostics = payload.get("stage_diagnostics")
        if isinstance(stage_diagnostics, list):
            for entry in reversed(stage_diagnostics):
                if not isinstance(entry, Mapping):
                    continue
                critic_verdict = entry.get("critic_verdict")
                if not isinstance(critic_verdict, Mapping):
                    continue
                candidate = critic_verdict.get("terminal_outcome_receipt")
                if isinstance(candidate, Mapping):
                    receipt = candidate
                    validation = critic_verdict.get(
                        "terminal_outcome_receipt_validation"
                    )
                    source = "live_progress.stage_diagnostics.critic_verdict"
                    break

    return build_terminal_outcome_receipt_projection(
        receipt if isinstance(receipt, Mapping) else None,
        validation=validation if isinstance(validation, Mapping) else None,
        source=source,
    )


def _build_section_payload(
    *,
    payload: Mapping[str, Any],
    section: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    value = payload.get(section)
    section_payload: dict[str, Any] = {
        "schema_version": "turn_live_progress_section.v1",
        "projection": "section",
        "section": section,
        "offset": offset,
        "limit": limit,
        "available_sections": sorted(_LIVE_PROGRESS_DETAIL_SECTIONS),
        "section_counts": _section_counts(payload),
    }
    if isinstance(value, list):
        total = len(value)
        start = min(max(offset, 0), total)
        end = min(total, start + limit)
        section_payload.update(
            {
                "items": value[start:end],
                "total": total,
                "returned": max(0, end - start),
                "next_offset": end if end < total else None,
            }
        )
    else:
        section_payload.update(
            {
                "value": value,
                "total": 1 if value is not None else 0,
                "returned": 1 if value is not None else 0,
                "next_offset": None,
            }
        )
    section_payload["section_json_size_chars"] = _json_size(value)
    return section_payload


def _build_bounded_live_progress_payload(
    *,
    payload: dict[str, Any],
    request_id: str,
    resolved_scope_key: str | None,
) -> dict[str, Any]:
    from ..server.routes.von_routes import _build_tool_progress_compact_summary

    compact_summary = _build_tool_progress_compact_summary(payload) or {}
    latest_stage_diagnostic = None
    stage_diagnostics = payload.get("stage_diagnostics")
    if isinstance(stage_diagnostics, list) and stage_diagnostics:
        latest_stage_diagnostic = _summarise_mapping(stage_diagnostics[-1])

    selected_workflow_execution = payload.get("selected_workflow_execution")
    selected_workflow_summary = _summarise_mapping(selected_workflow_execution)
    timing_summary = payload.get("timing_summary")
    if not isinstance(timing_summary, Mapping):
        timing_trace = payload.get("turn_timing_trace")
        if isinstance(timing_trace, Mapping):
            timing_summary = {
                "schema_version": "turn_timing_summary.v1",
                "span_count": timing_trace.get("span_count"),
                "stored_span_count": timing_trace.get("stored_span_count"),
                "dropped_span_count": timing_trace.get("dropped_span_count"),
                "summary": timing_trace.get("summary"),
                "slowest_spans": list(timing_trace.get("slowest_spans") or [])[:5],
                "model_prompt_summary": list(
                    timing_trace.get("model_prompt_summary") or []
                )[:5],
            }
        else:
            timing_summary = None

    return {
        "schema_version": "turn_live_progress_snapshot.v1",
        "success": True,
        "projection": "bounded_snapshot",
        "request_id": request_id,
        "resolved_scope_key": resolved_scope_key,
        "progress_source": "tool_progress_state",
        "compact_summary": compact_summary,
        "status": compact_summary.get("status"),
        "stage": compact_summary.get("stage"),
        "goal_label": compact_summary.get("goal_label"),
        "subtask": compact_summary.get("subtask"),
        "elapsed_ms": compact_summary.get("elapsed_ms"),
        "liveness_state": compact_summary.get("liveness_state"),
        "liveness_reason": compact_summary.get("liveness_reason"),
        "stall_detected": compact_summary.get("stall_detected"),
        "progress_events": _tail_list(
            payload.get("progress_events"),
            _DEFAULT_RECENT_EVENT_LIMIT,
        ),
        "activity_history": _tail_list(
            payload.get("activity_history"),
            _DEFAULT_RECENT_EVENT_LIMIT,
        ),
        "latest_stage_diagnostic": latest_stage_diagnostic,
        "timing_summary": timing_summary,
        "workflow_stage_path_summary": _summarise_mapping(
            payload.get("workflow_stage_path")
        ),
        "selected_workflow_execution_summary": selected_workflow_summary,
        "terminal_outcome_receipt_projection": payload.get(
            "terminal_outcome_receipt_projection"
        ),
        "available_sections": sorted(_LIVE_PROGRESS_DETAIL_SECTIONS),
        "section_counts": _section_counts(payload),
        "detail_access": {
            "tool_name": "turn_execution_get_live_progress",
            "arguments": {
                "request_id": request_id,
                "section": "<one of available_sections>",
                "limit": _DEFAULT_SECTION_LIMIT,
                "offset": 0,
            },
            "purpose": (
                "Fetch an explicit paginated live-progress detail section when "
                "the bounded snapshot is not enough."
            ),
        },
        "source_json_size_chars": _json_size(payload),
    }


def get_turn_execution_live_progress_payload(
    *,
    request_id: str,
    namespace: str | None = None,
    user_concept_id: str | None = None,
    window_session_id: str | None = None,
    anonymous_session_id: str | None = None,
    scope_key: str | None = None,
    section: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any] | None:
    """Return a bounded live progress snapshot or one explicit detail section."""

    request_id_value = _safe_str(request_id)
    if not request_id_value:
        return None

    from ..server.routes.von_routes import (
        _resolve_tool_progress_state_from_scope_candidates,
        _serialise_tool_progress_state,
    )

    resolved_user = _safe_str(user_concept_id)
    if not resolved_user:
        derived_user, _derived_org = derive_actor_context_from_namespace(namespace)
        resolved_user = _safe_str(derived_user)

    explicit_scope_key = _safe_str(scope_key)
    normalised_window_session_id = _safe_str(window_session_id)
    normalised_anonymous_session_id = _safe_str(anonymous_session_id)
    state, resolved_scope_key = _resolve_tool_progress_state_from_scope_candidates(
        request_id=request_id_value,
        explicit_scope_key=explicit_scope_key,
        user_concept_id=resolved_user,
        window_session_id=normalised_window_session_id,
        anonymous_session_id=normalised_anonymous_session_id,
    )
    if not isinstance(state, dict):
        return None

    payload = _serialise_tool_progress_state(state)
    receipt_projection = _build_live_terminal_outcome_receipt_projection(payload)
    if (
        not receipt_projection.get("available")
        and _safe_str(payload.get("status"))
        in {"completed", "failed", "cancelled", "error"}
    ):
        # Terminal progress may have been emitted before the bounded progress
        # payload copied the final receipt.  Read back the canonical persisted
        # record rather than leaving the live-progress surface contradictory.
        # This is neutral projection only; the represented critic remains the
        # outcome authority.
        try:
            from .turn_execution_record_service import (
                get_turn_execution_record_projection,
            )

            persisted_record = get_turn_execution_record_projection(
                request_id=request_id_value,
                namespace=_safe_str(namespace),
            )
            persisted_projection = terminal_outcome_receipt_projection_from_record(
                persisted_record,
                source="live_progress.persisted_turn_execution_record",
            )
            if persisted_projection.get("available"):
                receipt_projection = persisted_projection
        except Exception:
            # Live progress remains available even if terminal read-back is
            # temporarily unavailable; the missing projection stays explicit.
            pass
    if receipt_projection.get("available"):
        payload["terminal_outcome_receipt_projection"] = receipt_projection
    section_name = _safe_str(section)
    if section_name:
        if section_name not in _LIVE_PROGRESS_DETAIL_SECTIONS:
            return {
                "success": False,
                "error_code": "unknown_live_progress_section",
                "message": f"Unknown live progress detail section: {section_name}",
                "requested_section": section_name,
                "available_sections": sorted(_LIVE_PROGRESS_DETAIL_SECTIONS),
            }
        payload = _build_section_payload(
            payload=payload,
            section=section_name,
            offset=_safe_int(offset, default=0, minimum=0, maximum=100000),
            limit=_safe_int(
                limit,
                default=_DEFAULT_SECTION_LIMIT,
                minimum=1,
                maximum=_MAX_SECTION_LIMIT,
            ),
        )
    else:
        payload = _build_bounded_live_progress_payload(
            payload=payload,
            request_id=request_id_value,
            resolved_scope_key=resolved_scope_key,
        )

    payload["request_id"] = request_id_value
    payload["resolved_scope_key"] = resolved_scope_key
    payload["progress_source"] = "tool_progress_state"
    payload["mcp_access"] = {
        "turn_execution_get_live_progress": {
            "tool_name": "turn_execution_get_live_progress",
            "arguments": {
                "request_id": request_id_value,
                "namespace": _safe_str(namespace),
                "user_concept_id": resolved_user,
                "window_session_id": normalised_window_session_id,
                "anonymous_session_id": normalised_anonymous_session_id,
                "scope_key": explicit_scope_key,
                "section": section_name,
            },
            "purpose": (
                "Fetch the bounded live progress snapshot for this in-flight turn; "
                "provide section, limit, and offset for explicit detail hydration."
            ),
        }
    }
    return payload
