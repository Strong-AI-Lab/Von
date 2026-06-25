"""Build redacted replay-latency reports from live prompt sampler artefacts.

The report is intentionally structural: it reads sampler JSON files and emits
phase/stage timing, LLM request-preparation gaps, and terminal status metadata.
It does not print prompt bodies, tool payloads, message contents, or database
values.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

REPORT_SCHEMA_VERSION = "replay_latency_report.v1"


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _parse_timestamp(value: Any) -> datetime | None:
    text = _safe_text(value)
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _milliseconds_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _coerce_elapsed_ms(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _iter_json_files(paths: Sequence[Path]) -> list[Path]:
    discovered: list[Path] = []
    for path in paths:
        if path.is_dir():
            discovered.extend(
                sorted(item for item in path.rglob("*.json") if item.is_file())
            )
        elif path.is_file():
            discovered.append(path)
    return sorted(dict.fromkeys(discovered))


def _walk_mappings(node: Any) -> Iterable[Mapping[str, Any]]:
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            yield current
            stack.extend(current.values())
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            stack.extend(current)


def _find_progress_history(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    best: list[dict[str, Any]] = []
    for mapping in _walk_mappings(payload):
        history = mapping.get("progress_history")
        if not isinstance(history, Sequence) or isinstance(
            history, (str, bytes, bytearray)
        ):
            continue
        rows = [dict(item) for item in history if isinstance(item, Mapping)]
        if len(rows) > len(best):
            best = rows
    return best


def _find_first_text(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    wanted = set(keys)
    for mapping in _walk_mappings(payload):
        for key in wanted:
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


@dataclass(frozen=True)
class ProgressEvent:
    index: int
    phase: str
    status: str
    subtask: str
    request_preparation_step: str
    prompt_id: str
    model_name: str
    provider: str
    llm_exchange_id: str
    timestamp: datetime | None
    total_elapsed_ms: int | None
    duration_ms: int | None


def _phase_for_event(raw_event: Mapping[str, Any]) -> str:
    phase = _safe_text(raw_event.get("phase"))
    if phase:
        return phase
    workflow_state = _safe_text(raw_event.get("workflow_state_id"))
    if workflow_state:
        return workflow_state
    state_id = _safe_text(raw_event.get("state_id"))
    if state_id:
        return state_id
    status = _safe_text(raw_event.get("status"))
    return status or "unknown"


def _normalise_events(
    progress_history: Sequence[Mapping[str, Any]],
) -> list[ProgressEvent]:
    events: list[ProgressEvent] = []
    for index, raw_event in enumerate(progress_history):
        events.append(
            ProgressEvent(
                index=index,
                phase=_phase_for_event(raw_event),
                status=_safe_text(raw_event.get("status")),
                subtask=_safe_text(raw_event.get("subtask")),
                request_preparation_step=_safe_text(
                    raw_event.get("request_preparation_step")
                    or raw_event.get("preparation_step")
                ),
                prompt_id=_safe_text(raw_event.get("prompt_id")),
                model_name=_safe_text(raw_event.get("model_name")),
                provider=_safe_text(raw_event.get("provider")),
                llm_exchange_id=_safe_text(raw_event.get("llm_exchange_id")),
                timestamp=_parse_timestamp(raw_event.get("recorded_at")),
                total_elapsed_ms=_coerce_elapsed_ms(raw_event.get("total_elapsed_ms")),
                duration_ms=_coerce_elapsed_ms(raw_event.get("duration_ms")),
            )
        )
    return events


def _duration_between_events(start: ProgressEvent, end: ProgressEvent) -> int | None:
    by_time = _milliseconds_between(start.timestamp, end.timestamp)
    if by_time is not None:
        return by_time
    if start.total_elapsed_ms is not None and end.total_elapsed_ms is not None:
        return max(0, end.total_elapsed_ms - start.total_elapsed_ms)
    return None


def _summarise_phase_spans(events: Sequence[ProgressEvent]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    if not events:
        return spans

    start = events[0]
    previous = events[0]
    for event in events[1:]:
        if event.phase != start.phase:
            spans.append(_build_phase_span(start, previous))
            start = event
        previous = event
    spans.append(_build_phase_span(start, previous))

    by_phase: dict[str, dict[str, Any]] = {}
    for span in spans:
        phase = _safe_text(span.get("phase")) or "unknown"
        aggregate = by_phase.setdefault(
            phase,
            {
                "phase": phase,
                "span_count": 0,
                "total_duration_ms": 0,
                "max_duration_ms": 0,
                "first_status": span.get("first_status") or "",
                "last_status": "",
                "prompt_ids": set(),
                "models": set(),
                "providers": set(),
            },
        )
        duration = int(span.get("duration_ms") or 0)
        aggregate["span_count"] += 1
        aggregate["total_duration_ms"] += duration
        aggregate["max_duration_ms"] = max(int(aggregate["max_duration_ms"]), duration)
        aggregate["last_status"] = span.get("last_status") or ""
        if span.get("prompt_id"):
            aggregate["prompt_ids"].add(span["prompt_id"])
        if span.get("model_name"):
            aggregate["models"].add(span["model_name"])
        if span.get("provider"):
            aggregate["providers"].add(span["provider"])

    result: list[dict[str, Any]] = []
    for aggregate in by_phase.values():
        result.append(
            {
                **aggregate,
                "prompt_ids": sorted(aggregate["prompt_ids"]),
                "models": sorted(aggregate["models"]),
                "providers": sorted(aggregate["providers"]),
            }
        )
    return sorted(
        result, key=lambda row: int(row.get("total_duration_ms") or 0), reverse=True
    )


def _build_phase_span(start: ProgressEvent, end: ProgressEvent) -> dict[str, Any]:
    duration_ms = _duration_between_events(start, end)
    return {
        "phase": start.phase,
        "first_status": start.status,
        "last_status": end.status,
        "duration_ms": duration_ms or 0,
        "event_count": end.index - start.index + 1,
        "prompt_id": start.prompt_id or end.prompt_id,
        "model_name": start.model_name or end.model_name,
        "provider": start.provider or end.provider,
    }


def _summarise_llm_gaps(events: Sequence[ProgressEvent]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    active_by_phase: dict[str, ProgressEvent] = {}
    phase_start_by_phase: dict[str, ProgressEvent] = {}
    previous: ProgressEvent | None = None

    for event in events:
        if previous is not None and event.phase != previous.phase:
            start = active_by_phase.pop(previous.phase, None)
            if start is not None:
                gaps.append(
                    _build_llm_gap(
                        start,
                        event,
                        status="missing_llm_request_prepared",
                    )
                )
        if event.status == "phase_transition":
            phase_start_by_phase[event.phase] = event
        if event.subtask == "bounded LLM call":
            active_by_phase.setdefault(event.phase, event)
            previous = event
            continue
        if event.status == "llm_request_prepared":
            start = active_by_phase.pop(event.phase, None)
            status = "prepared"
            if start is None:
                start = phase_start_by_phase.get(event.phase)
                status = "prepared_from_phase_start"
            if start is not None:
                gaps.append(_build_llm_gap(start, event, status=status))
        previous = event

    last_by_phase: dict[str, ProgressEvent] = {}
    for event in events:
        last_by_phase[event.phase] = event
    for phase, start in active_by_phase.items():
        terminal = last_by_phase.get(phase, start)
        gaps.append(
            _build_llm_gap(start, terminal, status="missing_llm_request_prepared")
        )

    return sorted(gaps, key=lambda row: int(row.get("duration_ms") or 0), reverse=True)


def _summarise_llm_preparation_steps(
    events: Sequence[ProgressEvent],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for event in events:
        if event.status != "llm_request_preparation_step":
            continue
        if not event.request_preparation_step:
            continue
        steps.append(
            {
                "phase": event.phase,
                "step": event.request_preparation_step,
                "duration_ms": event.duration_ms or 0,
                "prompt_id": event.prompt_id,
                "model_name": event.model_name,
                "provider": event.provider,
            }
        )
    return sorted(
        steps,
        key=lambda row: int(row.get("duration_ms") or 0),
        reverse=True,
    )


def _build_llm_gap(
    start: ProgressEvent, end: ProgressEvent, *, status: str
) -> dict[str, Any]:
    return {
        "phase": start.phase,
        "status": status,
        "duration_ms": _duration_between_events(start, end) or 0,
        "prompt_id": start.prompt_id or end.prompt_id,
        "model_name": start.model_name or end.model_name,
        "provider": start.provider or end.provider,
        "llm_exchange_id": end.llm_exchange_id,
    }


def analyse_replay_artifact(path: Path) -> dict[str, Any]:
    payload = _as_mapping(json.loads(path.read_text(encoding="utf-8")))
    progress_history = _find_progress_history(payload)
    events = _normalise_events(progress_history)
    phase_spans = _summarise_phase_spans(events)
    llm_gaps = _summarise_llm_gaps(events)
    llm_preparation_steps = _summarise_llm_preparation_steps(events)

    return {
        "path": str(path),
        "status": _safe_text(payload.get("status")),
        "request_id": _find_first_text(payload, ("request_id", "background_task_id")),
        "prompt_id": _find_first_text(
            payload, ("prompt_id", "replay_case_id", "case_id")
        ),
        "selected_workflow_id": _find_first_text(payload, ("selected_workflow_id",)),
        "progress_event_count": len(events),
        "phase_spans": phase_spans,
        "llm_request_preparation_gaps": llm_gaps,
        "llm_request_preparation_steps": llm_preparation_steps,
        "max_phase_duration_ms": max(
            (int(row.get("total_duration_ms") or 0) for row in phase_spans),
            default=0,
        ),
        "max_llm_request_preparation_gap_ms": max(
            (int(row.get("duration_ms") or 0) for row in llm_gaps),
            default=0,
        ),
        "max_llm_request_preparation_step_ms": max(
            (int(row.get("duration_ms") or 0) for row in llm_preparation_steps),
            default=0,
        ),
    }


def build_replay_latency_report(paths: Sequence[Path]) -> dict[str, Any]:
    files = _iter_json_files(paths)
    artefacts: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in files:
        try:
            artefact = analyse_replay_artifact(path)
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append({"path": str(path), "reason": type(exc).__name__})
            continue
        if int(artefact.get("progress_event_count") or 0) == 0:
            skipped.append({"path": str(path), "reason": "no_progress_history"})
            continue
        artefacts.append(artefact)

    phase_totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "phase": "",
            "artifact_count": 0,
            "total_duration_ms": 0,
            "max_duration_ms": 0,
            "prompt_ids": set(),
            "models": set(),
            "providers": set(),
        }
    )
    gap_totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "phase": "",
            "status": "",
            "artifact_count": 0,
            "total_duration_ms": 0,
            "max_duration_ms": 0,
            "prompt_ids": set(),
            "models": set(),
            "providers": set(),
        }
    )
    step_totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "phase": "",
            "step": "",
            "artifact_count": 0,
            "total_duration_ms": 0,
            "max_duration_ms": 0,
            "prompt_ids": set(),
            "models": set(),
            "providers": set(),
        }
    )

    for artefact in artefacts:
        for row in _as_list(artefact.get("phase_spans")):
            if not isinstance(row, Mapping):
                continue
            phase = _safe_text(row.get("phase")) or "unknown"
            total = phase_totals[phase]
            total["phase"] = phase
            total["artifact_count"] += 1
            duration = int(row.get("total_duration_ms") or 0)
            total["total_duration_ms"] += duration
            total["max_duration_ms"] = max(int(total["max_duration_ms"]), duration)
            for value in _as_list(row.get("prompt_ids")):
                if _safe_text(value):
                    total["prompt_ids"].add(_safe_text(value))
            for value in _as_list(row.get("models")):
                if _safe_text(value):
                    total["models"].add(_safe_text(value))
            for value in _as_list(row.get("providers")):
                if _safe_text(value):
                    total["providers"].add(_safe_text(value))
        for row in _as_list(artefact.get("llm_request_preparation_gaps")):
            if not isinstance(row, Mapping):
                continue
            key = f"{_safe_text(row.get('phase'))}|{_safe_text(row.get('status'))}"
            total = gap_totals[key]
            total["phase"] = _safe_text(row.get("phase")) or "unknown"
            total["status"] = _safe_text(row.get("status")) or "unknown"
            total["artifact_count"] += 1
            duration = int(row.get("duration_ms") or 0)
            total["total_duration_ms"] += duration
            total["max_duration_ms"] = max(int(total["max_duration_ms"]), duration)
            for field, target in (
                ("prompt_id", "prompt_ids"),
                ("model_name", "models"),
                ("provider", "providers"),
            ):
                value = _safe_text(row.get(field))
                if value:
                    total[target].add(value)
        for row in _as_list(artefact.get("llm_request_preparation_steps")):
            if not isinstance(row, Mapping):
                continue
            key = f"{_safe_text(row.get('phase'))}|{_safe_text(row.get('step'))}"
            total = step_totals[key]
            total["phase"] = _safe_text(row.get("phase")) or "unknown"
            total["step"] = _safe_text(row.get("step")) or "unknown"
            total["artifact_count"] += 1
            duration = int(row.get("duration_ms") or 0)
            total["total_duration_ms"] += duration
            total["max_duration_ms"] = max(int(total["max_duration_ms"]), duration)
            for field, target in (
                ("prompt_id", "prompt_ids"),
                ("model_name", "models"),
                ("provider", "providers"),
            ):
                value = _safe_text(row.get(field))
                if value:
                    total[target].add(value)

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_count": len(files),
        "artifact_count": len(artefacts),
        "skipped": skipped,
        "phase_totals": _normalise_aggregate_rows(phase_totals.values()),
        "llm_request_preparation_gap_totals": _normalise_aggregate_rows(
            gap_totals.values()
        ),
        "llm_request_preparation_step_totals": _normalise_aggregate_rows(
            step_totals.values()
        ),
        "artifacts": artefacts,
    }


def _normalise_aggregate_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    for row in rows:
        normalised.append(
            {
                "phase": row.get("phase") or "",
                **({"status": row.get("status")} if row.get("status") else {}),
                **({"step": row.get("step")} if row.get("step") else {}),
                "artifact_count": int(row.get("artifact_count") or 0),
                "total_duration_ms": int(row.get("total_duration_ms") or 0),
                "max_duration_ms": int(row.get("max_duration_ms") or 0),
                "prompt_ids": sorted(row.get("prompt_ids") or []),
                "models": sorted(row.get("models") or []),
                "providers": sorted(row.get("providers") or []),
            }
        )
    return sorted(
        normalised,
        key=lambda item: (
            int(item.get("total_duration_ms") or 0),
            int(item.get("max_duration_ms") or 0),
        ),
        reverse=True,
    )


def render_markdown(report: Mapping[str, Any], *, limit: int = 20) -> str:
    lines = [
        "# Replay Latency Report",
        "",
        f"- Schema: `{_safe_text(report.get('schema_version'))}`",
        f"- Artefacts analysed: {int(report.get('artifact_count') or 0)}",
        f"- Inputs skipped: {len(_as_list(report.get('skipped')))}",
        "",
        "## Slow Phases",
        "",
        "| Phase | Artefacts | Total ms | Max ms | Prompts | Models |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for row in _as_list(report.get("phase_totals"))[:limit]:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| {phase} | {artifact_count} | {total_duration_ms} | {max_duration_ms} | {prompts} | {models} |".format(
                phase=_safe_text(row.get("phase")) or "unknown",
                artifact_count=int(row.get("artifact_count") or 0),
                total_duration_ms=int(row.get("total_duration_ms") or 0),
                max_duration_ms=int(row.get("max_duration_ms") or 0),
                prompts=", ".join(_as_list(row.get("prompt_ids"))) or "-",
                models=", ".join(_as_list(row.get("models"))) or "-",
            )
        )
    lines.extend(
        [
            "",
            "## LLM Request Preparation Gaps",
            "",
            "| Phase | Status | Artefacts | Total ms | Max ms | Prompts | Models |",
            "| --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in _as_list(report.get("llm_request_preparation_gap_totals"))[:limit]:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| {phase} | {status} | {artifact_count} | {total_duration_ms} | {max_duration_ms} | {prompts} | {models} |".format(
                phase=_safe_text(row.get("phase")) or "unknown",
                status=_safe_text(row.get("status")) or "unknown",
                artifact_count=int(row.get("artifact_count") or 0),
                total_duration_ms=int(row.get("total_duration_ms") or 0),
                max_duration_ms=int(row.get("max_duration_ms") or 0),
                prompts=", ".join(_as_list(row.get("prompt_ids"))) or "-",
                models=", ".join(_as_list(row.get("models"))) or "-",
            )
        )
    lines.extend(
        [
            "",
            "## LLM Request Preparation Steps",
            "",
            "| Phase | Step | Artefacts | Total ms | Max ms | Prompts | Models |",
            "| --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in _as_list(report.get("llm_request_preparation_step_totals"))[:limit]:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| {phase} | {step} | {artifact_count} | {total_duration_ms} | {max_duration_ms} | {prompts} | {models} |".format(
                phase=_safe_text(row.get("phase")) or "unknown",
                step=_safe_text(row.get("step")) or "unknown",
                artifact_count=int(row.get("artifact_count") or 0),
                total_duration_ms=int(row.get("total_duration_ms") or 0),
                max_duration_ms=int(row.get("max_duration_ms") or 0),
                prompts=", ".join(_as_list(row.get("prompt_ids"))) or "-",
                models=", ".join(_as_list(row.get("models"))) or "-",
            )
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Sampler JSON artefact files or directories to scan recursively.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of Markdown."
    )
    parser.add_argument(
        "--limit", type=int, default=20, help="Maximum rows per Markdown section."
    )
    args = parser.parse_args(argv)

    report = build_replay_latency_report(args.paths)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render_markdown(report, limit=max(1, args.limit)), end="")
    return 0 if report.get("artifact_count") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
