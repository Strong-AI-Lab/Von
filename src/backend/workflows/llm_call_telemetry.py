"""Shared support-surface telemetry helpers for LLM call records.

These helpers stamp wall-clock timestamps onto the per-call LLM telemetry
entries that workflow execution accumulates. The timestamps let the
Thinking-card LLM interaction log (JVNAUTOSCI-2385) present a timestamped
list of every LLM call made during a turn.

This is telemetry capture only. It does not influence any decision policy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, MutableMapping


def stamp_llm_call_timestamps(
    entry: MutableMapping[str, Any],
    *,
    duration_ms: float | None = None,
) -> MutableMapping[str, Any]:
    """Record completion and (derived) start timestamps on an LLM call entry.

    ``completed_at_utc`` is the wall-clock time the call finished (now).
    ``started_at_utc`` is derived from ``duration_ms`` when available, else it
    mirrors the completion time. ``at_utc`` is a canonical alias consumed by the
    Thinking-card projection.
    """

    completed = datetime.now(timezone.utc)
    completed_iso = completed.isoformat()
    entry["completed_at_utc"] = completed_iso
    if (
        isinstance(duration_ms, (int, float))
        and not isinstance(duration_ms, bool)
        and duration_ms >= 0
    ):
        started_iso = (
            completed - timedelta(milliseconds=float(duration_ms))
        ).isoformat()
    else:
        started_iso = completed_iso
    entry["started_at_utc"] = started_iso
    entry.setdefault("at_utc", completed_iso)
    return entry
