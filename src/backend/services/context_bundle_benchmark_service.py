"""Benchmark helpers for context bundles, dossier reconstruction, and report revision.

The benchmark is intentionally deterministic and reviewable. Cases live in a
repo-side seed bundle while the KB-native evaluation substrate matures.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .context_bundle_contracts import (
    CONTEXT_BUNDLE_BENCHMARK_SCHEMA_VERSION,
    CONTEXT_BUNDLE_BENCHMARK_SEED_SCHEMA_VERSION,
    CONTEXT_BUNDLE_STRATEGIES,
    CONTEXT_BUNDLE_STRATEGY_REPORT_REVISION,
    DEFAULT_CONTEXT_BUNDLE_BENCHMARK_CASE_SET,
)

DEFAULT_CONTEXT_BUNDLE_BENCHMARK_BUNDLE_PATH = (
    Path(__file__).resolve().parent.parent
    / "workflows"
    / "repo_seed_bundles"
    / "context_bundle_benchmark_seed_bundle.json"
)


@dataclass(frozen=True)
class ContextBundleBenchmarkCase:
    case_id: str
    workflow_class: str
    task_summary: str
    expected_best_strategy: str
    strategies: dict[str, dict[str, Any]]
    retention_priority: str
    jira_issue_keys: tuple[str, ...] = ()
    source_request_id: str | None = None
    notes: str = ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
    return False


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _hash_payload(value: Any, *, length: int = 16) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _normalise_strings(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(text)
    return tuple(output)


def _normalise_strategy_payload(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    return {
        "success": _safe_bool(raw.get("success")),
        "efficiency_score": _safe_float(raw.get("efficiency_score")),
        "interaction_depth": _safe_int(raw.get("interaction_depth")),
        "compression_events": _safe_int(raw.get("compression_events")),
        "guardrail_hits": _safe_int(raw.get("guardrail_hits")),
        "failure_modes": list(_normalise_strings(raw.get("failure_modes"))),
        "notes": _safe_str(raw.get("notes"), limit=1000),
    }


def _normalise_case(raw_case: Mapping[str, Any], *, index: int) -> ContextBundleBenchmarkCase | None:
    strategies_raw = raw_case.get("strategies")
    if not isinstance(strategies_raw, Mapping):
        return None
    strategies: dict[str, dict[str, Any]] = {}
    for strategy_id in CONTEXT_BUNDLE_STRATEGIES:
        payload = _normalise_strategy_payload(strategies_raw.get(strategy_id))
        if payload is not None:
            strategies[strategy_id] = payload
    if len(strategies) != len(CONTEXT_BUNDLE_STRATEGIES):
        return None

    expected_best_strategy = _safe_str(raw_case.get("expected_best_strategy"))
    if expected_best_strategy not in CONTEXT_BUNDLE_STRATEGIES:
        expected_best_strategy = CONTEXT_BUNDLE_STRATEGY_REPORT_REVISION

    case_id = _safe_str(raw_case.get("case_id")) or f"context_case_{index:03d}"
    workflow_class = _safe_str(raw_case.get("workflow_class")) or "unknown"
    task_summary = _safe_str(raw_case.get("task_summary"), limit=2000)
    if not task_summary:
        return None

    return ContextBundleBenchmarkCase(
        case_id=case_id,
        workflow_class=workflow_class,
        task_summary=task_summary,
        expected_best_strategy=expected_best_strategy,
        strategies=strategies,
        retention_priority=_safe_str(raw_case.get("retention_priority")) or "medium",
        jira_issue_keys=_normalise_strings(raw_case.get("jira_issue_keys")),
        source_request_id=_safe_str(raw_case.get("source_request_id")) or None,
        notes=_safe_str(raw_case.get("notes"), limit=1000),
    )


def load_context_bundle_benchmark_cases(
    *,
    case_set: str | None = None,
    bundle_path: Path | str | None = None,
) -> dict[str, Any]:
    resolved_path = (
        Path(bundle_path)
        if bundle_path is not None
        else DEFAULT_CONTEXT_BUNDLE_BENCHMARK_BUNDLE_PATH
    )
    bundle_text = resolved_path.read_text(encoding="utf-8")
    bundle_data = json.loads(bundle_text)
    if not isinstance(bundle_data, Mapping):
        raise ValueError("context_bundle_benchmark_seed_bundle_invalid")

    requested_case_set = (
        _safe_str(case_set)
        or _safe_str(bundle_data.get("default_case_set"))
        or DEFAULT_CONTEXT_BUNDLE_BENCHMARK_CASE_SET
    )
    case_sets = bundle_data.get("case_sets")
    raw_cases = case_sets.get(requested_case_set) if isinstance(case_sets, Mapping) else None
    if raw_cases is None:
        raise ValueError("context_bundle_benchmark_case_set_not_found")
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes, bytearray)):
        raise ValueError("context_bundle_benchmark_case_set_invalid")

    cases: list[ContextBundleBenchmarkCase] = []
    for index, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, Mapping):
            continue
        normalised = _normalise_case(raw_case, index=index)
        if normalised is not None:
            cases.append(normalised)

    return {
        "cases": cases,
        "case_set": requested_case_set,
        "bundle_path": str(resolved_path),
        "bundle_sha256": hashlib.sha256(bundle_text.encode("utf-8")).hexdigest(),
        "seed_schema_version": _safe_str(bundle_data.get("schema_version"))
        or CONTEXT_BUNDLE_BENCHMARK_SEED_SCHEMA_VERSION,
        "source": "seed_bundle",
    }


def build_context_bundle_benchmark_report(
    *,
    case_set: str | None = None,
    max_cases: int | None = None,
    bundle_path: Path | str | None = None,
    cases: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if cases is None:
        source_info = load_context_bundle_benchmark_cases(
            case_set=case_set,
            bundle_path=bundle_path,
        )
        normalised_cases = list(source_info.get("cases") or [])
    else:
        normalised_cases = []
        for index, raw_case in enumerate(cases, start=1):
            if not isinstance(raw_case, Mapping):
                continue
            case = _normalise_case(raw_case, index=index)
            if case is not None:
                normalised_cases.append(case)
        source_info = {
            "case_set": _safe_str(case_set) or "inline",
            "bundle_path": None,
            "bundle_sha256": _hash_payload(cases),
            "seed_schema_version": None,
            "source": "inline",
        }

    bounded_max_cases = max(1, min(int(max_cases), 500)) if max_cases is not None else None
    if bounded_max_cases is not None:
        normalised_cases = normalised_cases[:bounded_max_cases]

    strategy_metrics: dict[str, dict[str, float | int]] = {
        strategy_id: {
            "case_count": 0,
            "success_count": 0,
            "efficiency_score_total": 0.0,
            "interaction_depth_total": 0,
            "compression_events_total": 0,
            "guardrail_hits_total": 0,
        }
        for strategy_id in CONTEXT_BUNDLE_STRATEGIES
    }
    replay_cases: list[dict[str, Any]] = []
    retained_cases: list[dict[str, Any]] = []
    represented_workflow_classes: set[str] = set()
    expected_best_met_count = 0
    retained_case_pattern_counts = {
        "contains_success_pattern": 0,
        "contains_failure_pattern": 0,
    }

    for case in normalised_cases:
        represented_workflow_classes.add(case.workflow_class)
        best_efficiency = max(
            case.strategies[strategy_id]["efficiency_score"]
            for strategy_id in CONTEXT_BUNDLE_STRATEGIES
        )
        case_row = {
            "case_id": case.case_id,
            "workflow_class": case.workflow_class,
            "task_summary": case.task_summary,
            "expected_best_strategy": case.expected_best_strategy,
            "retention_priority": case.retention_priority,
            "notes": case.notes,
            "jira_issue_keys": list(case.jira_issue_keys),
            "source_request_id": case.source_request_id,
            "strategy_results": {},
        }
        for strategy_id in CONTEXT_BUNDLE_STRATEGIES:
            result = dict(case.strategies[strategy_id])
            strategy_metrics[strategy_id]["case_count"] += 1
            strategy_metrics[strategy_id]["success_count"] += 1 if result["success"] else 0
            strategy_metrics[strategy_id]["efficiency_score_total"] += _safe_float(
                result.get("efficiency_score")
            )
            strategy_metrics[strategy_id]["interaction_depth_total"] += _safe_int(
                result.get("interaction_depth")
            )
            strategy_metrics[strategy_id]["compression_events_total"] += _safe_int(
                result.get("compression_events")
            )
            strategy_metrics[strategy_id]["guardrail_hits_total"] += _safe_int(
                result.get("guardrail_hits")
            )
            result["matched_expected_best"] = (
                strategy_id == case.expected_best_strategy
                and abs(_safe_float(result.get("efficiency_score")) - best_efficiency) < 1e-9
            )
            case_row["strategy_results"][strategy_id] = result
        if case_row["strategy_results"][case.expected_best_strategy]["matched_expected_best"]:
            expected_best_met_count += 1

        case_row["contains_success_pattern"] = any(
            bool(case_row["strategy_results"][strategy_id]["success"])
            for strategy_id in CONTEXT_BUNDLE_STRATEGIES
        )
        case_row["contains_failure_pattern"] = any(
            not bool(case_row["strategy_results"][strategy_id]["success"])
            for strategy_id in CONTEXT_BUNDLE_STRATEGIES
        )

        replay_cases.append(case_row)
        expected_result = case_row["strategy_results"][case.expected_best_strategy]
        retain = (
            case.retention_priority.lower() == "high"
            or not expected_result["success"]
            or bool(expected_result.get("guardrail_hits"))
            or any(
                not case_row["strategy_results"][strategy_id]["success"]
                for strategy_id in CONTEXT_BUNDLE_STRATEGIES
            )
        )
        if retain:
            retained_cases.append(case_row)
            if case_row["contains_success_pattern"]:
                retained_case_pattern_counts["contains_success_pattern"] += 1
            if case_row["contains_failure_pattern"]:
                retained_case_pattern_counts["contains_failure_pattern"] += 1

    metrics: dict[str, Any] = {
        "case_count": len(normalised_cases),
        "represented_workflow_class_count": len(represented_workflow_classes),
        "represented_workflow_classes": sorted(represented_workflow_classes),
        "expected_best_strategy_match_count": expected_best_met_count,
        "expected_best_strategy_match_rate_pct": round(
            (expected_best_met_count * 100.0 / len(normalised_cases))
            if normalised_cases
            else 0.0,
            2,
        ),
        "retained_case_count": len(retained_cases),
        "retained_case_pattern_counts": retained_case_pattern_counts,
        "strategy_metrics": {},
    }
    for strategy_id, values in strategy_metrics.items():
        case_count = values["case_count"] or 0
        metrics["strategy_metrics"][strategy_id] = {
            "case_count": case_count,
            "success_rate_pct": round(
                (values["success_count"] * 100.0 / case_count) if case_count else 0.0,
                2,
            ),
            "average_efficiency_score": round(
                (values["efficiency_score_total"] / case_count) if case_count else 0.0,
                3,
            ),
            "average_interaction_depth": round(
                (values["interaction_depth_total"] / case_count) if case_count else 0.0,
                2,
            ),
            "average_compression_events": round(
                (values["compression_events_total"] / case_count) if case_count else 0.0,
                2,
            ),
            "average_guardrail_hits": round(
                (values["guardrail_hits_total"] / case_count) if case_count else 0.0,
                2,
            ),
        }

    gap_ids: list[str] = []
    if len(represented_workflow_classes) < 4:
        gap_ids.append("missing_required_workflow_classes")
    if not retained_cases:
        gap_ids.append("no_retained_cases")
    if retained_case_pattern_counts["contains_success_pattern"] == 0:
        gap_ids.append("missing_retained_success_patterns")
    if retained_case_pattern_counts["contains_failure_pattern"] == 0:
        gap_ids.append("missing_retained_failure_patterns")

    recommendations: list[dict[str, Any]] = []
    best_strategy = max(
        CONTEXT_BUNDLE_STRATEGIES,
        key=lambda strategy_id: metrics["strategy_metrics"][strategy_id][
            "average_efficiency_score"
        ],
    ) if normalised_cases else CONTEXT_BUNDLE_STRATEGY_REPORT_REVISION
    if best_strategy != CONTEXT_BUNDLE_STRATEGY_REPORT_REVISION:
        recommendations.append(
            {
                "recommendation_id": "report_revision_not_yet_winning",
                "priority": "high",
                "summary": (
                    "The report-revision strategy is not currently the strongest "
                    "performer in the benchmark bundle; inspect retained cases "
                    "before treating reconstruction as fully beneficial."
                ),
            }
        )
    if len(retained_cases) < 4 and normalised_cases:
        recommendations.append(
            {
                "recommendation_id": "grow_retained_case_pool",
                "priority": "medium",
                "summary": (
                    "Retain more success and failure cases so later experience-"
                    "maintenance work has a broader replay set."
                ),
            }
        )

    return {
        "success": True,
        "collection": "context_bundle_benchmark_cases",
        "benchmark_generated_at_utc": _utc_now_iso(),
        "schema_version": CONTEXT_BUNDLE_BENCHMARK_SCHEMA_VERSION,
        "benchmark_fingerprint": _hash_payload(replay_cases),
        "corpus": {
            "case_set": source_info.get("case_set"),
            "source": source_info.get("source"),
            "bundle_path": source_info.get("bundle_path"),
            "bundle_sha256": source_info.get("bundle_sha256"),
            "seed_schema_version": source_info.get("seed_schema_version"),
        },
        "metrics": metrics,
        "replay_cases": replay_cases,
        "retained_cases": retained_cases,
        "capability_gaps": [
            {
                "gap_id": gap_id,
                "severity": "high" if gap_id == "missing_required_workflow_classes" else "medium",
            }
            for gap_id in gap_ids
        ],
        "recommendations": recommendations,
    }


__all__ = [
    "CONTEXT_BUNDLE_BENCHMARK_SCHEMA_VERSION",
    "CONTEXT_BUNDLE_BENCHMARK_SEED_SCHEMA_VERSION",
    "DEFAULT_CONTEXT_BUNDLE_BENCHMARK_BUNDLE_PATH",
    "build_context_bundle_benchmark_report",
    "load_context_bundle_benchmark_cases",
]
