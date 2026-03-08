"""Helpers for deriving consistent turn-execution telemetry from event streams."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

_TOOL_START_STATUSES = frozenset({"tool_call_start"})
_TOOL_SUCCESS_STATUSES = frozenset({"tool_invoked"})
_TOOL_FAILURE_STATUSES = frozenset({"tool_failed", "tool_blocked", "error"})
_TOOL_START_EVENT_KINDS = frozenset({"tool_call_start"})
_TOOL_END_EVENT_KINDS = frozenset({"tool_call_end"})


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _normalise_batch_size(value: Any) -> int | float | None:
    raw_value = _safe_number(value)
    if raw_value is None:
        return None
    if float(raw_value).is_integer():
        return int(raw_value)
    return float(raw_value)


def derive_tool_observations_from_diagnostic_events(
    events: Sequence[Mapping[str, Any]] | None,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Derive tool history and counts from concrete tool lifecycle events only."""

    tool_history: list[dict[str, Any]] = []
    history_indexes_by_key: dict[str, list[int]] = {}
    tool_call_start_count = 0
    tool_call_end_count = 0

    for raw_entry in events or ():
        if not isinstance(raw_entry, Mapping):
            continue

        tool_name = _safe_str(raw_entry.get("tool"))
        if not tool_name:
            continue

        status = (_safe_str(raw_entry.get("status")) or "").lower()
        event_kind = (_safe_str(raw_entry.get("event_kind")) or "").lower()
        is_tool_start = status in _TOOL_START_STATUSES or event_kind in _TOOL_START_EVENT_KINDS
        is_tool_terminal = (
            status in _TOOL_SUCCESS_STATUSES
            or status in _TOOL_FAILURE_STATUSES
            or event_kind in _TOOL_END_EVENT_KINDS
        )
        if not is_tool_start and not is_tool_terminal:
            continue

        if is_tool_start:
            tool_call_start_count += 1
        if is_tool_terminal:
            tool_call_end_count += 1

        batch_size = _normalise_batch_size(raw_entry.get("batch_size"))
        call_id = _safe_str(raw_entry.get("call_id"))
        phase = _safe_str(raw_entry.get("phase")) or _safe_str(raw_entry.get("stage")) or ""
        result_summary = _safe_str(raw_entry.get("result_summary")) or ""
        workflow_task = _safe_str(raw_entry.get("workflow_task")) or ""
        key = call_id or f"{tool_name.lower()}::{batch_size!r}"

        history_index: int | None = None
        for candidate_index in reversed(history_indexes_by_key.get(key, [])):
            candidate_entry = tool_history[candidate_index]
            if candidate_entry.get("success") is None:
                history_index = candidate_index
                break

        if history_index is None:
            tool_history.append(
                {
                    "tool": tool_name,
                    "workflowTask": workflow_task,
                    "batchSize": batch_size,
                    "phase": phase,
                    "resultSummary": result_summary,
                    "success": None,
                    "callId": call_id,
                }
            )
            history_index = len(tool_history) - 1
            history_indexes_by_key.setdefault(key, []).append(history_index)

        history_entry = tool_history[history_index]
        if workflow_task and not history_entry.get("workflowTask"):
            history_entry["workflowTask"] = workflow_task
        if phase and not history_entry.get("phase"):
            history_entry["phase"] = phase
        if result_summary:
            history_entry["resultSummary"] = result_summary
        if call_id and not history_entry.get("callId"):
            history_entry["callId"] = call_id

        if status in _TOOL_SUCCESS_STATUSES:
            history_entry["success"] = True
        elif status in _TOOL_FAILURE_STATUSES:
            history_entry["success"] = False
        elif event_kind in _TOOL_END_EVENT_KINDS and isinstance(raw_entry.get("success"), bool):
            history_entry["success"] = bool(raw_entry.get("success"))

    if isinstance(limit, int) and limit > 0:
        tool_history = tool_history[-limit:]

    tool_success_count = sum(1 for entry in tool_history if entry.get("success") is True)
    tool_failure_count = sum(1 for entry in tool_history if entry.get("success") is False)
    tool_call_count = len(tool_history)
    tool_pending_count = max(0, tool_call_count - tool_success_count - tool_failure_count)

    return {
        "tool_history": tool_history,
        "tool_call_count": tool_call_count,
        "tool_success_count": tool_success_count,
        "tool_failure_count": tool_failure_count,
        "tool_pending_count": tool_pending_count,
        "tool_call_start_count": tool_call_start_count,
        "tool_call_end_count": tool_call_end_count,
    }
