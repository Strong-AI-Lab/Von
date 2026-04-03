from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from ..workflows import list_recent_workflow_execution_traces

WORKFLOW_PREDICTION_ENVELOPE_SCHEMA_VERSION = "workflow_prediction_envelope.v1"
_PREDICTION_KIND = "observed_history_envelope"
_MINIMUM_RECOMMENDED_RUN_COUNT = 5


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _normalise_provider(value: Any) -> str | None:
    token = _safe_str(value).lower()
    return token or None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compute_percentile(values: Sequence[int], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(int(value) for value in values)
    if len(ordered) == 1:
        return float(ordered[0])
    bounded = min(100.0, max(0.0, float(percentile)))
    position = (bounded / 100.0) * float(len(ordered) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - float(lower_index)
    lower = float(ordered[lower_index])
    upper = float(ordered[upper_index])
    return lower + ((upper - lower) * fraction)


def _round_or_none(value: float | int | None, *, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        token = value.strip()
        if not token:
            return None
        try:
            return int(float(token))
        except ValueError:
            return None
    return None


def _build_numeric_summary(values: Sequence[int]) -> dict[str, Any] | None:
    if not values:
        return None
    series = [int(value) for value in values]
    return {
        "sample_count": len(series),
        "min": min(series),
        "p50": _round_or_none(_compute_percentile(series, 50.0)),
        "p90": _round_or_none(_compute_percentile(series, 90.0)),
        "mean": _round_or_none(mean(series)),
        "max": max(series),
        "total": sum(series),
    }


def _coerce_usage_map(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    payload: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        raw = value.get(key)
        coerced = _coerce_int(raw)
        if coerced is not None:
            payload[key] = coerced
    return payload or None


def _mapping_to_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    payload: dict[str, Any] = {}
    for key, item in value.items():
        payload[_safe_str(key)] = item
    return payload


def _merge_usage_totals(values: Sequence[Mapping[str, int] | None]) -> dict[str, int] | None:
    totals: dict[str, int] = {}
    any_usage = False
    for value in values:
        if not isinstance(value, Mapping):
            continue
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            raw = value.get(key)
            if isinstance(raw, int):
                totals[key] = totals.get(key, 0) + int(raw)
                any_usage = True
    return totals if any_usage else None


def _build_usage_summary(values: Sequence[Mapping[str, int] | None]) -> dict[str, Any] | None:
    usage_maps = [value for value in values if isinstance(value, Mapping)]
    if not usage_maps:
        return None
    prompt_values = [
        int(value["prompt_tokens"])
        for value in usage_maps
        if isinstance(value.get("prompt_tokens"), int)
    ]
    completion_values = [
        int(value["completion_tokens"])
        for value in usage_maps
        if isinstance(value.get("completion_tokens"), int)
    ]
    total_values = [
        int(value["total_tokens"])
        for value in usage_maps
        if isinstance(value.get("total_tokens"), int)
    ]
    return {
        "sample_count": len(usage_maps),
        "prompt_tokens": _build_numeric_summary(prompt_values),
        "completion_tokens": _build_numeric_summary(completion_values),
        "total_tokens": _build_numeric_summary(total_values),
        "aggregate_totals": _merge_usage_totals(usage_maps),
        "cost_signal": {
            "pricing_available": False,
            "kind": "token_usage_only",
            "reason": (
                "Workflow traces currently expose token usage but do not carry "
                "authoritative per-model pricing."
            ),
        },
    }


def _build_duration_ms(start_time: Any, end_time: Any) -> int | None:
    start = _parse_datetime(start_time)
    end = _parse_datetime(end_time)
    if start is None or end is None:
        return None
    delta_ms = int(round((end - start).total_seconds() * 1000.0))
    if delta_ms < 0:
        return None
    return delta_ms


def _normalise_model_entry(
    *,
    model: Any,
    provider: Any,
    usage: Any = None,
    source: str,
) -> dict[str, Any] | None:
    model_name = _safe_str(model) or None
    provider_name = _normalise_provider(provider)
    usage_map = _coerce_usage_map(usage)
    if model_name is None and provider_name is None and usage_map is None:
        return None
    return {
        "model": model_name,
        "provider": provider_name,
        "usage": usage_map,
        "source": source,
    }


def _iter_action_output_model_entries(outputs: Any) -> list[dict[str, Any]]:
    if not isinstance(outputs, Mapping):
        return []

    entries: list[dict[str, Any]] = []
    raw_llm_calls = outputs.get("llm_calls")
    if isinstance(raw_llm_calls, Sequence) and not isinstance(raw_llm_calls, (str, bytes)):
        for raw in raw_llm_calls:
            if not isinstance(raw, Mapping):
                continue
            entry = _normalise_model_entry(
                model=raw.get("model") or raw.get("model_name"),
                provider=raw.get("provider"),
                usage=raw.get("usage"),
                source="action.llm_calls",
            )
            if entry is not None:
                entries.append(entry)

    if entries:
        return entries

    llm_step_envelope = outputs.get("llm_step_envelope")
    if not isinstance(llm_step_envelope, Mapping):
        return entries
    selected_candidate = _mapping_to_dict(
        llm_step_envelope.get("selected_model_candidate")
    )
    fallback_entry = _normalise_model_entry(
        model=llm_step_envelope.get("selected_model"),
        provider=selected_candidate.get("provider"),
        usage=None,
        source="action.llm_step_envelope",
    )
    if fallback_entry is not None:
        entries.append(fallback_entry)
    return entries


def _extract_trace_model_entries(trace_doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = trace_doc.get("metadata")
    metadata_map = _mapping_to_dict(metadata)
    entries: list[dict[str, Any]] = []

    raw_llm_calls = metadata_map.get("llm_calls")
    if isinstance(raw_llm_calls, Sequence) and not isinstance(raw_llm_calls, (str, bytes)):
        for raw in raw_llm_calls:
            if not isinstance(raw, Mapping):
                continue
            entry = _normalise_model_entry(
                model=raw.get("model") or raw.get("model_name"),
                provider=raw.get("provider"),
                usage=raw.get("usage"),
                source="trace.metadata.llm_calls",
            )
            if entry is not None:
                entries.append(entry)

    if entries:
        return entries

    raw_actions = trace_doc.get("actions")
    if isinstance(raw_actions, Sequence) and not isinstance(raw_actions, (str, bytes)):
        for action in raw_actions:
            if not isinstance(action, Mapping):
                continue
            entries.extend(_iter_action_output_model_entries(action.get("outputs")))

    if entries:
        return entries

    fallback_entry = _normalise_model_entry(
        model=metadata_map.get("default_model"),
        provider=metadata_map.get("default_provider"),
        usage=metadata_map.get("llm_usage"),
        source="trace.metadata.default_model",
    )
    if fallback_entry is not None:
        entries.append(fallback_entry)
    return entries


def _normalise_model_filter_token(model: str | None, provider: str | None) -> tuple[str | None, str | None]:
    model_token = _safe_str(model).lower() or None
    provider_token = _normalise_provider(provider)
    return model_token, provider_token


def _model_entry_matches(
    entry: Mapping[str, Any],
    *,
    model_filter: str | None,
    provider_filter: str | None,
) -> bool:
    entry_model = _safe_str(entry.get("model")).lower() or None
    entry_provider = _normalise_provider(entry.get("provider"))
    if provider_filter and entry_provider != provider_filter:
        return False
    if model_filter is None:
        return True
    if entry_model == model_filter:
        return True
    if entry_model and provider_filter and entry_model == f"{provider_filter}:{model_filter}":
        return True
    if (
        entry_model
        and ":" in entry_model
        and entry_model.split(":", 1)[1] == model_filter
    ):
        if provider_filter is None or entry_provider == provider_filter:
            return True
    return False


def _build_quality_proxy(status_counts: Mapping[str, int]) -> dict[str, Any]:
    total = sum(int(value) for value in status_counts.values())
    completed = int(status_counts.get("completed", 0))
    rate = (completed / total) if total else 0.0
    return {
        "metric": "completed_status_rate",
        "value": round(rate, 4),
        "note": (
            "This is a coarse quality proxy derived from observed terminal workflow "
            "status, not a task-level correctness or benchmark verdict."
        ),
    }


def _build_trace_observation(trace_doc: Mapping[str, Any]) -> dict[str, Any]:
    metadata = trace_doc.get("metadata")
    metadata_map = _mapping_to_dict(metadata)
    status = _safe_str(trace_doc.get("status")).lower() or "unknown"
    duration_ms = _build_duration_ms(
        trace_doc.get("start_time"),
        trace_doc.get("end_time"),
    )
    llm_usage = _coerce_usage_map(metadata_map.get("llm_usage"))
    model_entries = _extract_trace_model_entries(trace_doc)

    if llm_usage is None:
        llm_usage = _merge_usage_totals(
            [_coerce_usage_map(entry.get("usage")) for entry in model_entries]
        )

    action_durations: list[dict[str, Any]] = []
    raw_actions = trace_doc.get("actions")
    if isinstance(raw_actions, Sequence) and not isinstance(raw_actions, (str, bytes)):
        for action in raw_actions:
            if not isinstance(action, Mapping):
                continue
            raw_duration = _coerce_int(action.get("duration_ms"))
            if raw_duration is not None and raw_duration >= 0:
                action_durations.append(
                    {
                        "action_id": _safe_str(action.get("action_id")) or None,
                        "duration_ms": raw_duration,
                    }
                )

    step_durations: list[dict[str, Any]] = []
    raw_steps = trace_doc.get("steps")
    if isinstance(raw_steps, Sequence) and not isinstance(raw_steps, (str, bytes)):
        for step in raw_steps:
            if not isinstance(step, Mapping):
                continue
            step_duration = _build_duration_ms(
                step.get("start_time"),
                step.get("end_time"),
            )
            if step_duration is None:
                continue
            step_durations.append(
                {
                    "step_id": _safe_str(step.get("step_id")) or None,
                    "duration_ms": step_duration,
                }
            )

    dispatch_prepare_steps: list[dict[str, Any]] = []
    raw_dispatch_steps = metadata_map.get("workflow_dispatch_prepare_steps")
    if isinstance(raw_dispatch_steps, Sequence) and not isinstance(
        raw_dispatch_steps, (str, bytes)
    ):
        for step in raw_dispatch_steps:
            if not isinstance(step, Mapping):
                continue
            raw_duration = _coerce_int(step.get("duration_ms"))
            if raw_duration is None or raw_duration < 0:
                continue
            dispatch_prepare_steps.append(
                {
                    "step_id": _safe_str(step.get("step_id")) or None,
                    "step_label": _safe_str(step.get("step_label")) or None,
                    "duration_ms": raw_duration,
                }
            )

    return {
        "execution_id": _safe_str(trace_doc.get("execution_id")) or None,
        "status": status,
        "start_time": _parse_datetime(trace_doc.get("start_time")),
        "end_time": _parse_datetime(trace_doc.get("end_time")),
        "duration_ms": duration_ms,
        "llm_usage": llm_usage,
        "model_entries": model_entries,
        "action_durations": action_durations,
        "step_durations": step_durations,
        "dispatch_prepare_steps": dispatch_prepare_steps,
    }


def _build_grouped_duration_summaries(
    grouped_values: Mapping[tuple[str | None, str | None], list[int]],
    *,
    id_key: str,
    include_label: bool = False,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for (item_id, label), values in grouped_values.items():
        summary = _build_numeric_summary(values)
        if summary is None:
            continue
        payload: dict[str, Any] = {
            id_key: item_id,
            "duration_ms": summary,
        }
        if include_label:
            payload["label"] = label
        items.append(payload)
    items.sort(
        key=lambda item: (
            -(item.get("duration_ms", {}).get("sample_count") or 0),
            _safe_str(item.get(id_key)),
        )
    )
    return items


def build_workflow_prediction_envelope(
    *,
    workflow_id: str,
    namespace: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    workflow_token = _safe_str(workflow_id)
    if not workflow_token:
        raise ValueError("workflow_id is required")

    bounded_limit = max(1, min(int(limit), 200))
    namespace_token = _safe_str(namespace) or None
    model_filter, provider_filter = _normalise_model_filter_token(model, provider)

    traces = list_recent_workflow_execution_traces(
        limit=bounded_limit,
        namespace=namespace_token,
        workflow_id=workflow_token,
    )
    observations = [
        _build_trace_observation(trace_doc)
        for trace_doc in traces
        if isinstance(trace_doc, Mapping)
    ]

    matched_observations = [
        observation
        for observation in observations
        if (
            model_filter is None
            and provider_filter is None
        )
        or any(
            _model_entry_matches(
                entry,
                model_filter=model_filter,
                provider_filter=provider_filter,
            )
            for entry in observation.get("model_entries") or []
        )
    ]

    status_counts = Counter(
        _safe_str(observation.get("status")).lower() or "unknown"
        for observation in matched_observations
    )
    durations = [
        int(observation["duration_ms"])
        for observation in matched_observations
        if isinstance(observation.get("duration_ms"), int)
    ]
    usage_values = [
        _coerce_usage_map(observation.get("llm_usage"))
        for observation in matched_observations
    ]

    action_duration_groups: dict[tuple[str | None, str | None], list[int]] = defaultdict(list)
    step_duration_groups: dict[tuple[str | None, str | None], list[int]] = defaultdict(list)
    dispatch_duration_groups: dict[tuple[str | None, str | None], list[int]] = defaultdict(list)

    model_groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for observation in matched_observations:
        for action in observation.get("action_durations") or []:
            action_duration_groups[(action.get("action_id"), None)].append(
                int(action.get("duration_ms") or 0)
            )
        for step in observation.get("step_durations") or []:
            step_duration_groups[(step.get("step_id"), None)].append(
                int(step.get("duration_ms") or 0)
            )
        for step in observation.get("dispatch_prepare_steps") or []:
            dispatch_duration_groups[
                (step.get("step_id"), step.get("step_label"))
            ].append(int(step.get("duration_ms") or 0))

        per_trace_model_usage: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        for entry in observation.get("model_entries") or []:
            key = (
                _normalise_provider(entry.get("provider")),
                _safe_str(entry.get("model")) or None,
            )
            if key == (None, None):
                continue
            bucket = per_trace_model_usage.setdefault(
                key,
                {
                    "provider": key[0],
                    "model": key[1],
                    "call_count": 0,
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    "has_usage": False,
                },
            )
            bucket["call_count"] += 1
            usage = entry.get("usage")
            if isinstance(usage, Mapping):
                for usage_key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    raw = usage.get(usage_key)
                    if isinstance(raw, int):
                        bucket["usage"][usage_key] += int(raw)
                        bucket["has_usage"] = True

        for key, trace_bucket in per_trace_model_usage.items():
            group = model_groups.setdefault(
                key,
                {
                    "provider": trace_bucket["provider"],
                    "model": trace_bucket["model"],
                    "run_count": 0,
                    "call_count": 0,
                    "status_counts": Counter(),
                    "durations": [],
                    "usage_values": [],
                },
            )
            group["run_count"] += 1
            group["call_count"] += int(trace_bucket["call_count"])
            group["status_counts"][observation.get("status") or "unknown"] += 1
            if isinstance(observation.get("duration_ms"), int):
                group["durations"].append(int(observation["duration_ms"]))
            if bool(trace_bucket.get("has_usage")):
                usage_payload = _coerce_usage_map(trace_bucket.get("usage"))
                if usage_payload is not None:
                    group["usage_values"].append(usage_payload)

    model_tradeoffs: list[dict[str, Any]] = []
    for group in model_groups.values():
        group_status_counts = {
            key: int(value) for key, value in group["status_counts"].items()
        }
        completion_rate = 0.0
        total_group_runs = sum(group_status_counts.values())
        if total_group_runs:
            completion_rate = int(group_status_counts.get("completed", 0)) / float(
                total_group_runs
            )
        duration_summary = _build_numeric_summary(group["durations"])
        usage_summary = _build_usage_summary(group["usage_values"])
        total_token_summary = (
            _mapping_to_dict(usage_summary.get("total_tokens"))
            if isinstance(usage_summary, Mapping)
            and isinstance(usage_summary.get("total_tokens"), Mapping)
            else {}
        )
        payload = {
            "provider": group["provider"],
            "model": group["model"],
            "run_count": int(group["run_count"]),
            "call_count": int(group["call_count"]),
            "status_counts": group_status_counts,
            "completion_rate": round(completion_rate, 4),
            "duration_ms": duration_summary,
            "llm_usage": usage_summary,
            "tradeoff_summary": {
                "completion_rate": round(completion_rate, 4),
                "p50_duration_ms": (
                    duration_summary.get("p50")
                    if isinstance(duration_summary, Mapping)
                    else None
                ),
                "p50_total_tokens": total_token_summary.get("p50"),
                "note": (
                    "Ranking is based on observed completion rate first, then run count, "
                    "then lower median duration and token usage."
                ),
            },
        }
        model_tradeoffs.append(payload)

    model_tradeoffs.sort(
        key=lambda item: (
            -(item.get("completion_rate") or 0.0),
            -(item.get("run_count") or 0),
            item.get("tradeoff_summary", {}).get("p50_duration_ms")
            if isinstance(item.get("tradeoff_summary"), Mapping)
            and isinstance(
                item.get("tradeoff_summary", {}).get("p50_duration_ms"), (int, float)
            )
            else float("inf"),
            item.get("tradeoff_summary", {}).get("p50_total_tokens")
            if isinstance(item.get("tradeoff_summary"), Mapping)
            and isinstance(
                item.get("tradeoff_summary", {}).get("p50_total_tokens"), (int, float)
            )
            else float("inf"),
            _safe_str(item.get("provider")),
            _safe_str(item.get("model")),
        )
    )
    for index, item in enumerate(model_tradeoffs, start=1):
        item["rank"] = index

    matched_run_count = len(matched_observations)
    duration_coverage = (len(durations) / matched_run_count) if matched_run_count else 0.0
    usage_coverage = (
        sum(1 for value in usage_values if isinstance(value, Mapping)) / matched_run_count
        if matched_run_count
        else 0.0
    )
    if matched_run_count == 0 or duration_coverage == 0.0:
        sufficiency_status = "insufficient"
    elif matched_run_count < 3:
        sufficiency_status = "limited"
    elif matched_run_count < _MINIMUM_RECOMMENDED_RUN_COUNT:
        sufficiency_status = "emerging"
    else:
        sufficiency_status = "usable"

    start_times = [
        observation["start_time"]
        for observation in matched_observations
        if isinstance(observation.get("start_time"), datetime)
    ]
    end_times = [
        observation["end_time"]
        for observation in matched_observations
        if isinstance(observation.get("end_time"), datetime)
    ]

    return {
        "success": True,
        "schema_version": WORKFLOW_PREDICTION_ENVELOPE_SCHEMA_VERSION,
        "workflow_id": workflow_token,
        "filters": {
            "namespace": namespace_token,
            "model": _safe_str(model) or None,
            "provider": provider_filter,
            "limit": bounded_limit,
        },
        "sample_window": {
            "candidate_trace_count": len(observations),
            "matched_trace_count": matched_run_count,
            "earliest_start_time": (
                min(start_times).isoformat() if start_times else None
            ),
            "latest_end_time": (max(end_times).isoformat() if end_times else None),
        },
        "prediction_envelope": {
            "prediction_kind": _PREDICTION_KIND,
            "note": (
                "This envelope is derived from recent observed workflow traces. "
                "It is intended as a credible substrate for ETA and model-policy "
                "work, not as a trained forecast model."
            ),
            "data_sufficiency": {
                "status": sufficiency_status,
                "observed_run_count": matched_run_count,
                "minimum_recommended_run_count": _MINIMUM_RECOMMENDED_RUN_COUNT,
                "duration_coverage_rate": round(duration_coverage, 4),
                "llm_usage_coverage_rate": round(usage_coverage, 4),
            },
            "status_counts": {key: int(value) for key, value in status_counts.items()},
            "completion_rate": round(
                (int(status_counts.get("completed", 0)) / matched_run_count)
                if matched_run_count
                else 0.0,
                4,
            ),
            "failure_rate": round(
                (int(status_counts.get("failed", 0)) / matched_run_count)
                if matched_run_count
                else 0.0,
                4,
            ),
            "quality_proxy": _build_quality_proxy(status_counts),
            "duration_ms": _build_numeric_summary(durations),
            "llm_usage": _build_usage_summary(usage_values),
            "trace_step_duration_ms": _build_grouped_duration_summaries(
                step_duration_groups,
                id_key="step_id",
            ),
            "action_duration_ms": _build_grouped_duration_summaries(
                action_duration_groups,
                id_key="action_id",
            ),
            "dispatch_prepare_step_duration_ms": _build_grouped_duration_summaries(
                dispatch_duration_groups,
                id_key="step_id",
                include_label=True,
            ),
            "model_tradeoffs": model_tradeoffs,
        },
    }


__all__ = [
    "WORKFLOW_PREDICTION_ENVELOPE_SCHEMA_VERSION",
    "build_workflow_prediction_envelope",
]
