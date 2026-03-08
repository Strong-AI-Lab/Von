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


def _empty_tool_observation_summary() -> dict[str, Any]:
    return {
        "tool_history": [],
        "tool_call_count": 0,
        "tool_success_count": 0,
        "tool_failure_count": 0,
        "tool_pending_count": 0,
        "tool_call_start_count": 0,
        "tool_call_end_count": 0,
    }


def _normalise_tool_history_entry(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    tool_name = _safe_str(entry.get("tool"))
    if not tool_name:
        return None

    workflow_task = _safe_str(entry.get("workflowTask")) or _safe_str(
        entry.get("workflow_task")
    )
    phase = _safe_str(entry.get("phase"))
    result_summary = _safe_str(entry.get("resultSummary")) or _safe_str(
        entry.get("result_summary")
    )
    call_id = _safe_str(entry.get("callId")) or _safe_str(entry.get("call_id"))
    success = entry.get("success")

    return {
        "tool": tool_name,
        "workflowTask": workflow_task or "",
        "batchSize": _normalise_batch_size(
            entry.get("batchSize") if "batchSize" in entry else entry.get("batch_size")
        ),
        "phase": phase or "",
        "resultSummary": result_summary or "",
        "success": success if isinstance(success, bool) else None,
        "callId": call_id,
    }


def _finalise_tool_observation_summary(
    *,
    tool_history: list[dict[str, Any]],
    tool_call_start_count: int,
    tool_call_end_count: int,
    limit: int | None = None,
) -> dict[str, Any]:
    if isinstance(limit, int) and limit > 0:
        tool_history = tool_history[-limit:]

    tool_success_count = sum(1 for entry in tool_history if entry.get("success") is True)
    tool_failure_count = sum(
        1 for entry in tool_history if entry.get("success") is False
    )
    tool_call_count = len(tool_history)
    tool_pending_count = max(0, tool_call_count - tool_success_count - tool_failure_count)

    return {
        "tool_history": tool_history,
        "tool_call_count": tool_call_count,
        "tool_success_count": tool_success_count,
        "tool_failure_count": tool_failure_count,
        "tool_pending_count": tool_pending_count,
        "tool_call_start_count": max(0, int(tool_call_start_count)),
        "tool_call_end_count": max(0, int(tool_call_end_count)),
    }


def _initialise_tool_observation_summary(
    summary: Mapping[str, Any] | None,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    if not isinstance(summary, Mapping):
        return _empty_tool_observation_summary()

    raw_history = summary.get("tool_history")
    tool_history: list[dict[str, Any]] = []
    if isinstance(raw_history, list):
        for entry in raw_history:
            if not isinstance(entry, Mapping):
                continue
            normalised_entry = _normalise_tool_history_entry(entry)
            if normalised_entry is not None:
                tool_history.append(normalised_entry)

    tool_call_start_count = int(_safe_number(summary.get("tool_call_start_count")) or 0)
    tool_call_end_count = int(_safe_number(summary.get("tool_call_end_count")) or 0)

    return _finalise_tool_observation_summary(
        tool_history=tool_history,
        tool_call_start_count=tool_call_start_count,
        tool_call_end_count=tool_call_end_count,
        limit=limit,
    )


def update_tool_observation_summary(
    summary: Mapping[str, Any] | None,
    event: Mapping[str, Any] | None,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Incrementally apply a tool lifecycle event to a canonical summary."""

    current = _initialise_tool_observation_summary(summary, limit=limit)
    tool_history = [
        dict(entry)
        for entry in current.get("tool_history", [])
        if isinstance(entry, Mapping)
    ]
    history_indexes_by_key: dict[str, list[int]] = {}
    for index, entry in enumerate(tool_history):
        tool_name = _safe_str(entry.get("tool"))
        if not tool_name:
            continue
        batch_size = _normalise_batch_size(entry.get("batchSize"))
        call_id = _safe_str(entry.get("callId"))
        key = call_id or f"{tool_name.lower()}::{batch_size!r}"
        history_indexes_by_key.setdefault(key, []).append(index)

    tool_call_start_count = int(current.get("tool_call_start_count") or 0)
    tool_call_end_count = int(current.get("tool_call_end_count") or 0)

    if not isinstance(event, Mapping):
        return _finalise_tool_observation_summary(
            tool_history=tool_history,
            tool_call_start_count=tool_call_start_count,
            tool_call_end_count=tool_call_end_count,
            limit=limit,
        )

    tool_name = _safe_str(event.get("tool"))
    if not tool_name:
        return _finalise_tool_observation_summary(
            tool_history=tool_history,
            tool_call_start_count=tool_call_start_count,
            tool_call_end_count=tool_call_end_count,
            limit=limit,
        )

    status = (_safe_str(event.get("status")) or "").lower()
    event_kind = (_safe_str(event.get("event_kind")) or "").lower()
    is_tool_start = status in _TOOL_START_STATUSES or event_kind in _TOOL_START_EVENT_KINDS
    is_tool_terminal = (
        status in _TOOL_SUCCESS_STATUSES
        or status in _TOOL_FAILURE_STATUSES
        or event_kind in _TOOL_END_EVENT_KINDS
    )
    if not is_tool_start and not is_tool_terminal:
        return _finalise_tool_observation_summary(
            tool_history=tool_history,
            tool_call_start_count=tool_call_start_count,
            tool_call_end_count=tool_call_end_count,
            limit=limit,
        )

    if is_tool_start:
        tool_call_start_count += 1
    if is_tool_terminal:
        tool_call_end_count += 1

    batch_size = _normalise_batch_size(event.get("batch_size"))
    call_id = _safe_str(event.get("call_id"))
    phase = _safe_str(event.get("phase")) or _safe_str(event.get("stage")) or ""
    result_summary = _safe_str(event.get("result_summary")) or ""
    workflow_task = _safe_str(event.get("workflow_task")) or ""
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
    elif event_kind in _TOOL_END_EVENT_KINDS and isinstance(event.get("success"), bool):
        history_entry["success"] = bool(event.get("success"))

    return _finalise_tool_observation_summary(
        tool_history=tool_history,
        tool_call_start_count=tool_call_start_count,
        tool_call_end_count=tool_call_end_count,
        limit=limit,
    )


def derive_tool_observations_from_diagnostic_events(
    events: Sequence[Mapping[str, Any]] | None,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Derive tool history and counts from concrete tool lifecycle events only."""
    summary = _empty_tool_observation_summary()
    for raw_entry in events or ():
        if not isinstance(raw_entry, Mapping):
            continue
        summary = update_tool_observation_summary(summary, raw_entry, limit=limit)
    return summary
