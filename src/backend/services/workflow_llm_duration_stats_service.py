from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Mapping, MutableMapping

from ..db.mongo_client import get_db

SCHEMA_VERSION = "workflow_llm_step_duration_stats.v1"
COLLECTION_NAME = "workflow_llm_step_duration_stats"

_INDEXES_READY = False


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _duration_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    duration = float(value)
    if not math.isfinite(duration):
        return None
    return max(0.0, duration)


def _now_utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _round_metric(value: float | None) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), 3)


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and math.isfinite(value):
        return max(0, int(value))
    return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return default


def _normalise_outcome(value: Any) -> str:
    cleaned = (_clean_text(value) or "unknown").lower()
    if cleaned in {"success", "succeeded", "ok", "completed"}:
        return "success"
    if cleaned in {"timeout", "timed_out"}:
        return "timeout"
    if cleaned in {"cancelled", "canceled"}:
        return "cancelled"
    if cleaned in {"failure", "failed", "error"}:
        return "failure"
    return "unknown"


def build_duration_stats_key(
    *,
    workflow_id: Any,
    workflow_state_id: Any,
    workflow_stage_id: Any,
    stage: Any,
    provider: Any,
    model_name: Any,
) -> dict[str, str] | None:
    """Build the stable aggregate key for a workflow LLM-step/model pair."""

    clean_workflow_id = _clean_text(workflow_id)
    clean_model_name = _clean_text(model_name)
    clean_workflow_state_id = _clean_text(workflow_state_id)
    clean_stage = _clean_text(stage)
    clean_workflow_stage_id = (
        _clean_text(workflow_stage_id) or clean_workflow_state_id or clean_stage
    )
    if not clean_workflow_id or not clean_model_name or not clean_workflow_stage_id:
        return None
    return {
        "workflow_id": clean_workflow_id,
        "workflow_state_id": clean_workflow_state_id or "",
        "workflow_stage_id": clean_workflow_stage_id,
        "stage": clean_stage or clean_workflow_stage_id,
        "provider": _clean_text(provider) or "unknown",
        "model_name": clean_model_name,
    }


def _stats_key_id(stats_key: Mapping[str, str]) -> str:
    return "\u001f".join(
        [
            stats_key.get("workflow_id", ""),
            stats_key.get("workflow_state_id", ""),
            stats_key.get("workflow_stage_id", ""),
            stats_key.get("stage", ""),
            stats_key.get("provider", ""),
            stats_key.get("model_name", ""),
        ]
    )


def _stddev_from_m2(*, count: int, m2: float) -> float | None:
    if count < 2:
        return None
    variance = max(0.0, m2 / float(count - 1))
    return math.sqrt(variance)


def build_historical_duration_context(
    previous: Mapping[str, Any] | None,
    *,
    current_duration_ms: Any,
) -> dict[str, Any] | None:
    duration = _duration_float(current_duration_ms)
    if duration is None or not isinstance(previous, Mapping):
        return None

    count = _safe_int(previous.get("observed_count"))
    if count <= 0:
        return None

    mean = _safe_float(previous.get("mean_duration_ms"))
    m2 = _safe_float(previous.get("m2_duration_ms"))
    stddev = _stddev_from_m2(count=count, m2=m2)
    deviation_ms = duration - mean
    deviation_ratio = duration / mean if mean > 0 else None
    z_score = deviation_ms / stddev if stddev and stddev > 0 else None
    if count < 2 or z_score is None:
        classification = "insufficient_history"
    elif z_score >= 2.0:
        classification = "slower_than_usual"
    elif z_score <= -2.0:
        classification = "faster_than_usual"
    else:
        classification = "within_usual_range"

    return {
        "historical_observation_count": count,
        "historical_mean_duration_ms": _round_metric(mean),
        "historical_stddev_duration_ms": _round_metric(stddev),
        "historical_min_duration_ms": _round_metric(
            _safe_float(previous.get("min_duration_ms"), duration)
        ),
        "historical_max_duration_ms": _round_metric(
            _safe_float(previous.get("max_duration_ms"), duration)
        ),
        "historical_last_observed_at_utc": _clean_text(
            previous.get("last_observed_at_utc")
        ),
        "duration_deviation_from_mean_ms": _round_metric(deviation_ms),
        "duration_deviation_ratio": _round_metric(deviation_ratio),
        "duration_deviation_stddevs": _round_metric(z_score),
        "duration_deviation_classification": classification,
        "historical_successful_observation_count": _safe_int(
            previous.get("successful_observation_count")
        ),
        "historical_successful_mean_duration_ms": _round_metric(
            _safe_float(previous.get("successful_mean_duration_ms"))
        ),
        "historical_failed_observation_count": _safe_int(
            previous.get("failed_observation_count")
        ),
        "historical_failed_mean_duration_ms": _round_metric(
            _safe_float(previous.get("failed_mean_duration_ms"))
        ),
        "historical_timeout_observation_count": _safe_int(
            previous.get("timeout_observation_count")
        ),
    }


def calculate_next_duration_stats(
    previous: Mapping[str, Any] | None,
    *,
    duration_ms: Any,
    observed_at_utc: str | None = None,
    request_id: Any = None,
    outcome: Any = None,
) -> dict[str, Any] | None:
    duration = _duration_float(duration_ms)
    if duration is None:
        return None

    previous_map: Mapping[str, Any] = previous if isinstance(previous, Mapping) else {}
    previous_count = _safe_int(previous_map.get("observed_count"))
    previous_mean = _safe_float(previous_map.get("mean_duration_ms"))
    previous_m2 = _safe_float(previous_map.get("m2_duration_ms"))
    previous_total = _safe_float(previous_map.get("total_duration_ms"))
    previous_min = (
        _duration_float(previous_map.get("min_duration_ms"))
        if previous_count > 0
        else None
    )
    previous_max = (
        _duration_float(previous_map.get("max_duration_ms"))
        if previous_count > 0
        else None
    )

    count = previous_count + 1
    delta = duration - previous_mean
    mean = previous_mean + (delta / float(count))
    delta2 = duration - mean
    m2 = previous_m2 + (delta * delta2)
    total = previous_total + duration
    min_duration = duration if previous_min is None else min(previous_min, duration)
    max_duration = duration if previous_max is None else max(previous_max, duration)
    stddev = _stddev_from_m2(count=count, m2=m2)
    normalised_outcome = _normalise_outcome(outcome)

    successful_count = _safe_int(previous_map.get("successful_observation_count"))
    successful_total = _safe_float(previous_map.get("successful_total_duration_ms"))
    failed_count = _safe_int(previous_map.get("failed_observation_count"))
    failed_total = _safe_float(previous_map.get("failed_total_duration_ms"))
    timeout_count = _safe_int(previous_map.get("timeout_observation_count"))
    cancelled_count = _safe_int(previous_map.get("cancelled_observation_count"))
    unknown_count = _safe_int(previous_map.get("unknown_outcome_observation_count"))
    if normalised_outcome == "success":
        successful_count += 1
        successful_total += duration
    elif normalised_outcome in {"failure", "timeout", "cancelled"}:
        failed_count += 1
        failed_total += duration
        if normalised_outcome == "timeout":
            timeout_count += 1
        elif normalised_outcome == "cancelled":
            cancelled_count += 1
    else:
        unknown_count += 1

    return {
        "observed_count": count,
        "total_duration_ms": _round_metric(total),
        "mean_duration_ms": _round_metric(mean),
        "m2_duration_ms": _round_metric(m2),
        "variance_duration_ms": (
            _round_metric(m2 / float(count - 1)) if count >= 2 else None
        ),
        "stddev_duration_ms": _round_metric(stddev),
        "min_duration_ms": _round_metric(min_duration),
        "max_duration_ms": _round_metric(max_duration),
        "last_duration_ms": _round_metric(duration),
        "last_observed_at_utc": observed_at_utc or _now_utc_iso(),
        "last_request_id": _clean_text(request_id),
        "last_outcome": normalised_outcome,
        "successful_observation_count": successful_count,
        "successful_total_duration_ms": _round_metric(successful_total),
        "successful_mean_duration_ms": _round_metric(
            successful_total / successful_count if successful_count else None
        ),
        "failed_observation_count": failed_count,
        "failed_total_duration_ms": _round_metric(failed_total),
        "failed_mean_duration_ms": _round_metric(
            failed_total / failed_count if failed_count else None
        ),
        "timeout_observation_count": timeout_count,
        "cancelled_observation_count": cancelled_count,
        "unknown_outcome_observation_count": unknown_count,
    }


def _get_collection() -> Any | None:
    db = get_db()
    if db is None:
        return None
    return db[COLLECTION_NAME]


def _ensure_indexes(collection: Any) -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    try:
        collection.create_index("stats_key_id", unique=True)
        collection.create_index(
            [
                ("workflow_id", 1),
                ("workflow_stage_id", 1),
                ("provider", 1),
                ("model_name", 1),
            ]
        )
        _INDEXES_READY = True
    except Exception:
        # Telemetry indexing must not break the turn path.
        return


def record_workflow_llm_step_duration_observation(
    *,
    workflow_id: Any,
    workflow_state_id: Any,
    workflow_stage_id: Any,
    stage: Any,
    provider: Any,
    model_name: Any,
    duration_ms: Any,
    request_id: Any = None,
    observed_at_utc: str | None = None,
    collection: Any | None = None,
    outcome: Any = None,
) -> dict[str, Any] | None:
    """Persist one observation and return the pre-observation baseline context."""

    stats_key = build_duration_stats_key(
        workflow_id=workflow_id,
        workflow_state_id=workflow_state_id,
        workflow_stage_id=workflow_stage_id,
        stage=stage,
        provider=provider,
        model_name=model_name,
    )
    next_stats = calculate_next_duration_stats(
        None,
        duration_ms=duration_ms,
        observed_at_utc=observed_at_utc,
        request_id=request_id,
        outcome=outcome,
    )
    if stats_key is None or next_stats is None:
        return None

    coll = collection if collection is not None else _get_collection()
    if coll is None:
        return None

    _ensure_indexes(coll)
    key_id = _stats_key_id(stats_key)
    previous = coll.find_one({"stats_key_id": key_id}, {"_id": 0}) or {}
    baseline = build_historical_duration_context(
        previous,
        current_duration_ms=duration_ms,
    )
    next_stats = calculate_next_duration_stats(
        previous,
        duration_ms=duration_ms,
        observed_at_utc=observed_at_utc,
        request_id=request_id,
        outcome=outcome,
    )
    if next_stats is None:
        return baseline

    now = _now_utc_iso()
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "stats_key_id": key_id,
        "stats_key": dict(stats_key),
        **dict(stats_key),
        **next_stats,
        "updated_at_utc": now,
    }
    update: MutableMapping[str, Any] = {
        "$set": doc,
        "$setOnInsert": {"created_at_utc": now},
    }
    coll.update_one({"stats_key_id": key_id}, update, upsert=True)
    return baseline


__all__ = [
    "COLLECTION_NAME",
    "SCHEMA_VERSION",
    "build_duration_stats_key",
    "build_historical_duration_context",
    "calculate_next_duration_stats",
    "record_workflow_llm_step_duration_observation",
]
