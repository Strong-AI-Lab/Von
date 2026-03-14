"""Process-local workflow selection experience buffer.

JVNAUTOSCI-1424 Phase 3:  Records selection decisions (workflow chosen,
confidence, reasoning, candidates offered) in a bounded ring buffer so
that Phase 4 RL training has a warm-start dataset of experience tuples.

The buffer is process-local and thread-safe.  A snapshot function
exposes the current experience data for telemetry endpoints and offline
export.  Aggregate counters give a quick overview of selection quality
without iterating the full buffer.
"""

from __future__ import annotations

import copy
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class SelectionExperienceTuple:
    """One recorded selection decision (immutable experience tuple).

    Fields mirror the orchestrator's auxiliary LLM‐call record so the
    buffer can be reconciled against telemetry traces.
    """

    timestamp: str
    turn_id: str = ""
    query: str = ""
    candidate_workflow_ids: tuple[str, ...] = ()
    candidate_count: int = 0
    selected_workflow_id: str = ""
    verdict: str = ""
    confidence_score: float = 0.0
    reasoning: str = ""
    model_name: str = ""
    routing_duration_ms: float = 0.0
    # Outcome populated asynchronously after execution completes.
    outcome: Optional[str] = None
    outcome_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# -----------------------------------------------------------------------
# Aggregate counters
# -----------------------------------------------------------------------

@dataclass
class _SelectionAggregates:
    total: int = 0
    rag_selected: int = 0
    rag_default: int = 0
    legacy: int = 0
    fallback: int = 0
    confidence_sum: float = 0.0
    confidence_count: int = 0


# -----------------------------------------------------------------------
# Global ring-buffer singleton
# -----------------------------------------------------------------------

_DEFAULT_BUFFER_SIZE = 500

_lock = Lock()
_buffer: deque[SelectionExperienceTuple] = deque(maxlen=_DEFAULT_BUFFER_SIZE)
_aggregates = _SelectionAggregates()


def record_selection_experience(
    *,
    turn_id: str = "",
    query: str = "",
    candidate_workflow_ids: Sequence[str] = (),
    selected_workflow_id: str = "",
    verdict: str = "",
    confidence_score: float = 0.0,
    reasoning: str = "",
    model_name: str = "",
    routing_duration_ms: float = 0.0,
) -> SelectionExperienceTuple:
    """Append a selection experience tuple to the global ring buffer."""
    entry = SelectionExperienceTuple(
        timestamp=datetime.now(timezone.utc).isoformat(),
        turn_id=str(turn_id or ""),
        query=str(query or "")[:500],  # Cap query length.
        candidate_workflow_ids=tuple(
            str(cid) for cid in candidate_workflow_ids if cid
        ),
        candidate_count=len(
            [cid for cid in candidate_workflow_ids if cid]
        ),
        selected_workflow_id=str(selected_workflow_id or ""),
        verdict=str(verdict or ""),
        confidence_score=max(0.0, min(1.0, float(confidence_score))),
        reasoning=str(reasoning or "")[:500],  # Cap reasoning length.
        model_name=str(model_name or ""),
        routing_duration_ms=float(routing_duration_ms or 0.0),
    )

    with _lock:
        _buffer.append(entry)
        _aggregates.total += 1
        if verdict == "rag_selected":
            _aggregates.rag_selected += 1
        elif verdict == "rag_default":
            _aggregates.rag_default += 1
        elif verdict == "fallback":
            _aggregates.fallback += 1
        else:
            _aggregates.legacy += 1
        if confidence_score > 0.0:
            _aggregates.confidence_sum += entry.confidence_score
            _aggregates.confidence_count += 1

    return entry


def get_selection_experience_snapshot() -> Dict[str, Any]:
    """Return a deep copy of aggregates and recent experience tuples."""
    with _lock:
        entries = [e.to_dict() for e in _buffer]
        agg = copy.copy(_aggregates)

    avg_confidence = (
        round(agg.confidence_sum / agg.confidence_count, 4)
        if agg.confidence_count > 0
        else 0.0
    )
    return {
        "aggregates": {
            "total_selections": agg.total,
            "rag_selected": agg.rag_selected,
            "rag_default": agg.rag_default,
            "legacy": agg.legacy,
            "fallback": agg.fallback,
            "avg_confidence": avg_confidence,
            "confidence_sample_count": agg.confidence_count,
        },
        "recent_experiences": entries,
        "buffer_capacity": _buffer.maxlen or _DEFAULT_BUFFER_SIZE,
    }


def get_recent_experiences(
    *,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return the most recent *limit* experience tuples (newest first)."""
    with _lock:
        recent = list(_buffer)
    recent.reverse()
    return [e.to_dict() for e in recent[:limit]]


def reset_selection_experience() -> None:
    """Clear the experience buffer and aggregates (for testing)."""
    with _lock:
        _buffer.clear()
        _aggregates.total = 0
        _aggregates.rag_selected = 0
        _aggregates.rag_default = 0
        _aggregates.legacy = 0
        _aggregates.fallback = 0
        _aggregates.confidence_sum = 0.0
        _aggregates.confidence_count = 0
