"""Workflow selection experience capture for Phase 3 and Phase 4.

Phase 3 introduced a process-local ring buffer so selector decisions could be
inspected after routing. Phase 4 extends that record into a proper learning
signal: decisions are persisted durably, finalised with execution outcomes, and
scored with a compact reward function that the learned routing policy can reuse.
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure

from ..db.mongo_client import get_db

logger = logging.getLogger(__name__)

SELECTION_EXPERIENCES_COLLECTION = "workflow_selection_experiences"
_DEFAULT_BUFFER_SIZE = 500
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_:-]{1,31}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _safe_str(value: Any, *, limit: int | None = None) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif value is None:
        text = ""
    else:
        text = str(value).strip()
    if limit is not None:
        return text[:limit]
    return text


def _coerce_float(
    value: Any,
    *,
    default: float = 0.0,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _coerce_int(
    value: Any,
    *,
    default: int = 0,
    minimum: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _clone_mapping(value: Mapping[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): copy.deepcopy(val) for key, val in value.items()}


def _normalise_workflow_ids(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(_safe_str(value) for value in values if _safe_str(value))


def extract_selection_query_tokens(
    query: str,
    *,
    limit: int = 24,
) -> tuple[str, ...]:
    """Extract a stable, bounded token set from selector input text."""
    clean_query = _safe_str(query, limit=500).lower()
    if not clean_query:
        return ()

    tokens: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN_RE.findall(clean_query):
        if match in seen:
            continue
        seen.add(match)
        tokens.append(match)
        if len(tokens) >= limit:
            break
    return tuple(tokens)


def _normalise_outcome(outcome: Any) -> str | None:
    clean = _safe_str(outcome).lower().replace(" ", "_")
    return clean or None


def _outcome_is_success(outcome: str | None) -> bool:
    return outcome in {"completed", "success"}


def _normalise_string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned.append(text)
    return cleaned


def _derive_missing_required_tool_count(
    outcome_metadata: Mapping[str, Any],
) -> int:
    explicit_missing = _coerce_int(
        outcome_metadata.get("required_evidence_tool_missing_count"),
        default=-1,
        minimum=-1,
    )
    if explicit_missing >= 0:
        return explicit_missing

    required_tools = _normalise_string_list(
        outcome_metadata.get("workflow_required_effects_required_tools")
    )
    if not required_tools:
        return 0

    executed_tools = {
        tool.lower()
        for tool in _normalise_string_list(
            outcome_metadata.get("tool_invocation_names")
        )
    }
    if not executed_tools:
        return len(required_tools)
    return sum(1 for tool in required_tools if tool.lower() not in executed_tools)


def _outcome_metadata_has_learning_failure(
    outcome_metadata: Mapping[str, Any],
) -> bool:
    if _coerce_bool(outcome_metadata.get("selection_learning_failure")):
        return True
    if _coerce_bool(outcome_metadata.get("required_evidence_missing")):
        return True
    if _derive_missing_required_tool_count(outcome_metadata) > 0:
        return True
    declared_effect_count = _coerce_int(
        outcome_metadata.get("workflow_required_effects_declared_count"),
        minimum=0,
    )
    declared_required_tools = _coerce_int(
        outcome_metadata.get("workflow_required_effects_required_tool_count"),
        minimum=0,
    )
    tool_invocation_count = _coerce_int(
        outcome_metadata.get("tool_invocation_count"),
        minimum=0,
    )
    if (
        declared_effect_count > 0
        and declared_required_tools > 0
        and tool_invocation_count == 0
    ):
        return True
    return False


def _derive_effective_learning_outcome(
    outcome: str | None,
    outcome_metadata: Mapping[str, Any],
) -> str | None:
    clean_outcome = _normalise_outcome(outcome)
    if clean_outcome in {
        "completed",
        "success",
    } and _outcome_metadata_has_learning_failure(outcome_metadata):
        return "learning_failure"
    return clean_outcome


@dataclass(frozen=True)
class SelectionExperienceTuple:
    """One recorded workflow-selection experience tuple."""

    experience_id: str
    timestamp: str
    turn_id: str = ""
    query: str = ""
    query_tokens: tuple[str, ...] = ()
    candidate_workflow_ids: tuple[str, ...] = ()
    candidate_count: int = 0
    selected_workflow_id: str = ""
    verdict: str = ""
    selection_source: str = "selector"
    selection_metadata: Dict[str, Any] = field(default_factory=dict)
    confidence_score: float = 0.0
    reasoning: str = ""
    model_name: str = ""
    routing_duration_ms: float = 0.0
    outcome: Optional[str] = None
    outcome_metadata: Dict[str, Any] = field(default_factory=dict)
    reward: Optional[float] = None
    reward_breakdown: Dict[str, float] = field(default_factory=dict)
    completed_at: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class _SelectionAggregates:
    total: int = 0
    rag_selected: int = 0
    rag_default: int = 0
    legacy: int = 0
    fallback: int = 0
    policy_direct: int = 0
    confidence_sum: float = 0.0
    confidence_count: int = 0
    completed_count: int = 0
    successful_outcomes: int = 0
    reward_sum: float = 0.0
    reward_count: int = 0


_lock = Lock()
_buffer: deque[SelectionExperienceTuple] = deque(maxlen=_DEFAULT_BUFFER_SIZE)
_aggregates = _SelectionAggregates()
_indexes_ensured = False


def _serialise_for_storage(entry: SelectionExperienceTuple) -> Dict[str, Any]:
    payload = entry.to_dict()
    payload["query_tokens"] = list(entry.query_tokens)
    payload["candidate_workflow_ids"] = list(entry.candidate_workflow_ids)
    return payload


def _coerce_experience_tuple(
    payload: Mapping[str, Any],
) -> SelectionExperienceTuple | None:
    experience_id = _safe_str(payload.get("experience_id"))
    timestamp = _safe_str(payload.get("timestamp"))
    if not experience_id or not timestamp:
        return None

    return SelectionExperienceTuple(
        experience_id=experience_id,
        timestamp=timestamp,
        turn_id=_safe_str(payload.get("turn_id")),
        query=_safe_str(payload.get("query"), limit=500),
        query_tokens=tuple(
            _safe_str(item)
            for item in (payload.get("query_tokens") or [])
            if _safe_str(item)
        ),
        candidate_workflow_ids=tuple(
            _safe_str(item)
            for item in (payload.get("candidate_workflow_ids") or [])
            if _safe_str(item)
        ),
        candidate_count=_coerce_int(payload.get("candidate_count"), minimum=0),
        selected_workflow_id=_safe_str(payload.get("selected_workflow_id")),
        verdict=_safe_str(payload.get("verdict")),
        selection_source=_safe_str(payload.get("selection_source")) or "selector",
        selection_metadata=_clone_mapping(payload.get("selection_metadata")),
        confidence_score=_coerce_float(
            payload.get("confidence_score"),
            minimum=0.0,
            maximum=1.0,
        ),
        reasoning=_safe_str(payload.get("reasoning"), limit=500),
        model_name=_safe_str(payload.get("model_name")),
        routing_duration_ms=_coerce_float(
            payload.get("routing_duration_ms"),
            minimum=0.0,
        ),
        outcome=_normalise_outcome(payload.get("outcome")),
        outcome_metadata=_clone_mapping(payload.get("outcome_metadata")),
        reward=(
            _coerce_float(payload.get("reward"))
            if payload.get("reward") is not None
            else None
        ),
        reward_breakdown={
            _safe_str(key): _coerce_float(value)
            for key, value in _clone_mapping(payload.get("reward_breakdown")).items()
        },
        completed_at=_safe_str(payload.get("completed_at")) or None,
    )


def _ensure_indexes() -> None:
    global _indexes_ensured
    if _indexes_ensured:
        return

    db = get_db()
    if db is None:
        return

    coll = db[SELECTION_EXPERIENCES_COLLECTION]
    try:
        existing = [idx.get("name") for idx in coll.list_indexes()]
        if "experience_id_unique" not in existing:
            coll.create_index(
                [("experience_id", ASCENDING)],
                unique=True,
                name="experience_id_unique",
            )
        if "workflow_timestamp_desc" not in existing:
            coll.create_index(
                [("selected_workflow_id", ASCENDING), ("timestamp", DESCENDING)],
                name="workflow_timestamp_desc",
            )
        if "turn_timestamp_desc" not in existing:
            coll.create_index(
                [("turn_id", ASCENDING), ("timestamp", DESCENDING)],
                name="turn_timestamp_desc",
            )
        if "reward_timestamp_desc" not in existing:
            coll.create_index(
                [("reward", DESCENDING), ("timestamp", DESCENDING)],
                name="reward_timestamp_desc",
            )
    except OperationFailure as exc:
        logger.warning(
            "[workflow_selection_experience] index creation partially failed: %s",
            exc,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[workflow_selection_experience] could not create indexes: %s",
            exc,
        )
    else:
        _indexes_ensured = True


def _get_collection():
    _ensure_indexes()
    db = get_db()
    if db is None:
        return None
    return db[SELECTION_EXPERIENCES_COLLECTION]


def _persist_entry(entry: SelectionExperienceTuple) -> None:
    coll = _get_collection()
    if coll is None:
        return
    payload = _serialise_for_storage(entry)
    coll.update_one(
        {"experience_id": entry.experience_id},
        {
            "$set": payload,
            "$setOnInsert": {"created_at": entry.timestamp},
        },
        upsert=True,
    )


def _find_buffer_index_locked(experience_id: str) -> int | None:
    for index, entry in enumerate(_buffer):
        if entry.experience_id == experience_id:
            return index
    return None


def _register_entry_locked(entry: SelectionExperienceTuple) -> None:
    _aggregates.total += 1
    if entry.verdict == "rag_selected":
        _aggregates.rag_selected += 1
    elif entry.verdict == "rag_default":
        _aggregates.rag_default += 1
    elif entry.verdict == "fallback":
        _aggregates.fallback += 1
    else:
        _aggregates.legacy += 1
    if entry.selection_source == "policy_direct":
        _aggregates.policy_direct += 1
    if entry.confidence_score > 0.0:
        _aggregates.confidence_sum += entry.confidence_score
        _aggregates.confidence_count += 1
    _register_outcome_metrics_locked(entry)


def _register_outcome_metrics_locked(entry: SelectionExperienceTuple) -> None:
    if entry.outcome is not None:
        _aggregates.completed_count += 1
        if _outcome_is_success(entry.outcome):
            _aggregates.successful_outcomes += 1
    if entry.reward is not None:
        _aggregates.reward_sum += float(entry.reward)
        _aggregates.reward_count += 1


def _unregister_outcome_metrics_locked(entry: SelectionExperienceTuple) -> None:
    if entry.outcome is not None:
        _aggregates.completed_count = max(0, _aggregates.completed_count - 1)
        if _outcome_is_success(entry.outcome):
            _aggregates.successful_outcomes = max(
                0, _aggregates.successful_outcomes - 1
            )
    if entry.reward is not None:
        _aggregates.reward_sum -= float(entry.reward)
        _aggregates.reward_count = max(0, _aggregates.reward_count - 1)


def compute_selection_reward(
    *,
    outcome: str | None,
    confidence_score: float = 0.0,
    routing_duration_ms: float = 0.0,
    outcome_metadata: Mapping[str, Any] | None = None,
    selection_metadata: Mapping[str, Any] | None = None,
) -> tuple[float, Dict[str, float]]:
    """Compute a compact reward signal from execution telemetry."""

    safe_outcome_metadata = _clone_mapping(outcome_metadata)
    safe_selection_metadata = _clone_mapping(selection_metadata)
    clean_outcome = _derive_effective_learning_outcome(outcome, safe_outcome_metadata)

    duration_ms = _coerce_float(
        safe_outcome_metadata.get("orchestrator_duration_ms", routing_duration_ms),
        minimum=0.0,
    )
    retry_attempts = _coerce_int(
        safe_outcome_metadata.get("retry_attempts"),
        minimum=0,
    ) + _coerce_int(
        safe_outcome_metadata.get("completion_gate_repeat_attempts"),
        minimum=0,
    )
    total_tokens = _coerce_int(
        safe_outcome_metadata.get("total_tokens"),
        minimum=0,
    )
    confidence = _coerce_float(
        confidence_score,
        minimum=0.0,
        maximum=1.0,
    )
    follow_up_required = _coerce_bool(
        safe_outcome_metadata.get("completion_gate_requires_follow_up")
    )
    workflow_discovery_budget_exhausted = _coerce_bool(
        safe_outcome_metadata.get("workflow_discovery_budget_exhausted")
    )
    discovery_candidate_count = _coerce_int(
        safe_outcome_metadata.get("workflow_discovery_candidate_count"),
        minimum=0,
    )
    discovery_match_count = _coerce_int(
        safe_outcome_metadata.get("workflow_discovery_match_count"),
        minimum=0,
    )
    missing_required_tool_count = _derive_missing_required_tool_count(
        safe_outcome_metadata
    )
    required_evidence_missing = (
        missing_required_tool_count > 0
        or _outcome_metadata_has_learning_failure(safe_outcome_metadata)
    )
    exploration_bonus = _coerce_float(
        safe_selection_metadata.get("selected_exploration_bonus"),
        minimum=0.0,
        maximum=0.25,
    )

    if clean_outcome in {"completed", "success"}:
        completion_signal = 1.0
    elif clean_outcome in {"follow_up_required", "partial"} or follow_up_required:
        completion_signal = 0.35
    elif clean_outcome in {
        "failed",
        "terminated",
        "error",
        "selection_failed",
        "learning_failure",
    }:
        completion_signal = -0.75
    else:
        completion_signal = 0.0

    if duration_ms <= 1500.0:
        efficiency_signal = 0.2
    elif duration_ms <= 5000.0:
        efficiency_signal = 0.1
    elif duration_ms >= 15000.0:
        efficiency_signal = -0.15
    else:
        efficiency_signal = 0.0

    retry_penalty = -0.08 * min(retry_attempts, 4)
    cost_penalty = -0.2 * min(total_tokens / 8000.0, 1.0)
    discovery_timeout_penalty = 0.0
    if workflow_discovery_budget_exhausted:
        discovery_timeout_penalty = -0.35
        if discovery_candidate_count <= 0 and discovery_match_count <= 0:
            discovery_timeout_penalty -= 0.15
    required_evidence_penalty = (
        -0.4 * min(missing_required_tool_count, 3) if required_evidence_missing else 0.0
    )
    if completion_signal > 0.5:
        calibration_signal = 0.2 * confidence
    elif completion_signal < 0.0:
        calibration_signal = -0.3 * confidence
    else:
        calibration_signal = -0.1 * confidence

    reward = (
        completion_signal
        + efficiency_signal
        + retry_penalty
        + cost_penalty
        + discovery_timeout_penalty
        + required_evidence_penalty
        + calibration_signal
        + exploration_bonus
    )
    reward = round(max(-1.5, min(1.5, reward)), 4)
    breakdown = {
        "completion_signal": round(completion_signal, 4),
        "efficiency_signal": round(efficiency_signal, 4),
        "retry_penalty": round(retry_penalty, 4),
        "cost_penalty": round(cost_penalty, 4),
        "discovery_timeout_penalty": round(discovery_timeout_penalty, 4),
        "required_evidence_penalty": round(required_evidence_penalty, 4),
        "calibration_signal": round(calibration_signal, 4),
        "exploration_bonus": round(exploration_bonus, 4),
    }
    return reward, breakdown


def record_selection_experience(
    *,
    turn_id: str = "",
    query: str = "",
    candidate_workflow_ids: Sequence[str] = (),
    selected_workflow_id: str = "",
    verdict: str = "",
    selection_source: str = "selector",
    selection_metadata: Mapping[str, Any] | None = None,
    confidence_score: float = 0.0,
    reasoning: str = "",
    model_name: str = "",
    routing_duration_ms: float = 0.0,
) -> SelectionExperienceTuple:
    """Append and persist a workflow-selection experience tuple."""

    experience = SelectionExperienceTuple(
        experience_id=f"wse_{uuid.uuid4().hex[:24]}",
        timestamp=_utcnow_iso(),
        turn_id=_safe_str(turn_id),
        query=_safe_str(query, limit=500),
        query_tokens=extract_selection_query_tokens(query),
        candidate_workflow_ids=_normalise_workflow_ids(candidate_workflow_ids),
        candidate_count=len(_normalise_workflow_ids(candidate_workflow_ids)),
        selected_workflow_id=_safe_str(selected_workflow_id),
        verdict=_safe_str(verdict),
        selection_source=_safe_str(selection_source) or "selector",
        selection_metadata=_clone_mapping(selection_metadata),
        confidence_score=_coerce_float(
            confidence_score,
            minimum=0.0,
            maximum=1.0,
        ),
        reasoning=_safe_str(reasoning, limit=500),
        model_name=_safe_str(model_name),
        routing_duration_ms=_coerce_float(routing_duration_ms, minimum=0.0),
    )

    with _lock:
        _buffer.append(experience)
        _register_entry_locked(experience)

    try:
        _persist_entry(experience)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[workflow_selection_experience] could not persist %s: %s",
            experience.experience_id,
            exc,
        )
    return experience


def get_selection_experience(experience_id: str) -> SelectionExperienceTuple | None:
    clean_experience_id = _safe_str(experience_id)
    if not clean_experience_id:
        return None

    with _lock:
        for entry in _buffer:
            if entry.experience_id == clean_experience_id:
                return entry

    coll = _get_collection()
    if coll is None:
        return None
    payload = coll.find_one({"experience_id": clean_experience_id}, {"_id": 0})
    if not isinstance(payload, Mapping):
        return None
    return _coerce_experience_tuple(payload)


def finalise_selection_experience(
    *,
    experience_id: str,
    outcome: str,
    outcome_metadata: Mapping[str, Any] | None = None,
    reward: float | None = None,
    reward_breakdown: Mapping[str, float] | None = None,
    retrain_policy: bool = True,
) -> SelectionExperienceTuple | None:
    """Attach execution outcome and reward to a previously recorded decision."""

    existing = get_selection_experience(experience_id)
    if existing is None:
        return None

    safe_outcome_metadata = _clone_mapping(outcome_metadata)
    runtime_outcome = _normalise_outcome(outcome)
    effective_outcome = _derive_effective_learning_outcome(
        runtime_outcome,
        safe_outcome_metadata,
    )
    if effective_outcome != runtime_outcome:
        safe_outcome_metadata["runtime_outcome"] = runtime_outcome
        safe_outcome_metadata["selection_learning_outcome"] = effective_outcome
        safe_outcome_metadata["selection_learning_failure"] = True
    resolved_reward = reward
    resolved_breakdown = (
        {
            _safe_str(key): _coerce_float(value)
            for key, value in reward_breakdown.items()
        }
        if isinstance(reward_breakdown, Mapping)
        else None
    )
    if resolved_reward is None or resolved_breakdown is None:
        resolved_reward, resolved_breakdown = compute_selection_reward(
            outcome=effective_outcome,
            confidence_score=existing.confidence_score,
            routing_duration_ms=existing.routing_duration_ms,
            outcome_metadata=safe_outcome_metadata,
            selection_metadata=existing.selection_metadata,
        )

    updated = replace(
        existing,
        outcome=effective_outcome,
        outcome_metadata=safe_outcome_metadata,
        reward=_coerce_float(resolved_reward),
        reward_breakdown=dict(resolved_breakdown),
        completed_at=_utcnow_iso(),
    )

    with _lock:
        buffer_index = _find_buffer_index_locked(existing.experience_id)
        if buffer_index is not None:
            prior = _buffer[buffer_index]
            _unregister_outcome_metrics_locked(prior)
            _buffer[buffer_index] = updated
            _register_outcome_metrics_locked(updated)
        else:
            _register_outcome_metrics_locked(updated)

    try:
        _persist_entry(updated)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[workflow_selection_experience] could not persist finalised %s: %s",
            updated.experience_id,
            exc,
        )

    if retrain_policy:
        try:
            from .workflow_selection_policy_service import refresh_live_selection_policy

            refresh_live_selection_policy()
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning(
                "[workflow_selection_experience] could not refresh live policy: %s",
                exc,
            )

    return updated


def list_selection_experiences(
    *,
    limit: int = 100,
    completed_only: bool = False,
) -> List[SelectionExperienceTuple]:
    """List recent workflow-selection experiences from durable storage when available."""

    coll = _get_collection()
    if coll is None:
        with _lock:
            entries = list(_buffer)
        entries.reverse()
        if completed_only:
            entries = [entry for entry in entries if entry.outcome is not None]
        return entries[: max(1, min(limit, 1000))]

    query: Dict[str, Any] = {}
    if completed_only:
        query["outcome"] = {"$ne": None}

    safe_limit = max(1, min(limit, 5000))
    cursor = (
        coll.find(query, {"_id": 0}).sort("timestamp", DESCENDING).limit(safe_limit)
    )
    entries: list[SelectionExperienceTuple] = []
    for payload in cursor:
        if isinstance(payload, Mapping):
            entry = _coerce_experience_tuple(payload)
            if entry is not None:
                entries.append(entry)
    return entries


def get_selection_experience_snapshot() -> Dict[str, Any]:
    """Return a deep copy of process-local aggregates and recent experiences."""

    with _lock:
        entries = [entry.to_dict() for entry in _buffer]
        aggregates = copy.copy(_aggregates)

    avg_confidence = (
        round(aggregates.confidence_sum / aggregates.confidence_count, 4)
        if aggregates.confidence_count > 0
        else 0.0
    )
    avg_reward = (
        round(aggregates.reward_sum / aggregates.reward_count, 4)
        if aggregates.reward_count > 0
        else 0.0
    )
    success_rate = (
        round(aggregates.successful_outcomes / aggregates.completed_count, 4)
        if aggregates.completed_count > 0
        else 0.0
    )
    return {
        "aggregates": {
            "total_selections": aggregates.total,
            "rag_selected": aggregates.rag_selected,
            "rag_default": aggregates.rag_default,
            "legacy": aggregates.legacy,
            "fallback": aggregates.fallback,
            "policy_direct": aggregates.policy_direct,
            "avg_confidence": avg_confidence,
            "confidence_sample_count": aggregates.confidence_count,
            "completed_count": aggregates.completed_count,
            "successful_outcomes": aggregates.successful_outcomes,
            "success_rate": success_rate,
            "avg_reward": avg_reward,
            "reward_sample_count": aggregates.reward_count,
        },
        "recent_experiences": entries,
        "buffer_capacity": _buffer.maxlen or _DEFAULT_BUFFER_SIZE,
    }


def get_recent_experiences(
    *,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return the most recent experience tuples (newest first)."""
    with _lock:
        recent = list(_buffer)
    recent.reverse()
    return [entry.to_dict() for entry in recent[: max(1, min(limit, 200))]]


def reset_selection_experience() -> None:
    """Clear process-local experience state and best-effort durable test data."""

    with _lock:
        _buffer.clear()
        _aggregates.total = 0
        _aggregates.rag_selected = 0
        _aggregates.rag_default = 0
        _aggregates.legacy = 0
        _aggregates.fallback = 0
        _aggregates.policy_direct = 0
        _aggregates.confidence_sum = 0.0
        _aggregates.confidence_count = 0
        _aggregates.completed_count = 0
        _aggregates.successful_outcomes = 0
        _aggregates.reward_sum = 0.0
        _aggregates.reward_count = 0

    coll = _get_collection()
    if coll is not None:
        try:
            coll.delete_many({})
        except Exception:  # pragma: no cover - best effort
            pass


__all__ = [
    "SelectionExperienceTuple",
    "compute_selection_reward",
    "extract_selection_query_tokens",
    "finalise_selection_experience",
    "get_recent_experiences",
    "get_selection_experience",
    "get_selection_experience_snapshot",
    "list_selection_experiences",
    "record_selection_experience",
    "reset_selection_experience",
]
