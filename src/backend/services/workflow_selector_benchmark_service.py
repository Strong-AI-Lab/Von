"""Selector-routing benchmark corpus loading and replay evaluation.

This benchmark stays intentionally close to the existing Phase 1 evaluation
surfaces:

- it uses the selector's own response-resolution logic;
- it emits the shared execution-correctness outcome labels introduced in
  JVNAUTOSCI-965;
- it returns replay-style case rows that downstream dashboards and operating
  protocols can consume without inventing a separate benchmark vocabulary.

The current corpus is stored as a repo-side seed bundle so the cases remain
reviewable and deterministic while the KB-native benchmark representation is
still being established.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..workflows import WorkflowRegistry
from ..workflows.workflow_selector import WorkflowSelectionPrompt, WorkflowSelector
from .prompt_template_service import PromptTemplateService
from .turn_execution_record_service import TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION

SELECTOR_ROUTING_BENCHMARK_SCHEMA_VERSION = "selector_routing_benchmark.v1"
SELECTOR_ROUTING_BENCHMARK_SEED_SCHEMA_VERSION = (
    "selector_routing_benchmark.seed_bundle.v1"
)
DEFAULT_SELECTOR_ROUTING_CASE_SET = "phase1_seed"
DEFAULT_SELECTOR_ROUTING_BENCHMARK_BUNDLE_PATH = (
    Path(__file__).resolve().parent.parent
    / "workflows"
    / "repo_seed_bundles"
    / "selector_routing_benchmark_seed_bundle.json"
)

SELECTOR_BENCHMARK_OUTCOME_LABELS: tuple[str, ...] = (
    "successful_completion",
    "false_success",
    "unresolved_follow_up_needed",
    "tool_or_workflow_misrouting",
    "abstain_escalate_no_safe_route",
)
_EXPECTED_ROUTING_OUTCOMES = {
    "workflow_selected",
    "abstain_escalate_no_safe_route",
}


@dataclass(frozen=True)
class SelectorBenchmarkCase:
    case_id: str
    turn_text: str
    candidate_workflows: tuple[dict[str, Any], ...]
    allowed_workflow_ids: tuple[str, ...]
    expected_routing_outcome: str
    baseline_workflow_id: str
    selector_response_text: str
    prompt_failure_reason: str | None = None
    prompt_failure_detail: str | None = None
    notes: str = ""
    jira_issue_keys: tuple[str, ...] = ()
    source_request_id: str | None = None


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


def _hash_payload(value: Any, *, length: int = 16) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _coerce_max_cases(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    return max(1, min(parsed, 500))


def _normalise_candidate_workflows(raw_items: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        return ()

    rows: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        workflow_id = _safe_str(
            raw.get("concept_id") or raw.get("workflow_id") or raw.get("id")
        )
        if not workflow_id:
            continue
        rows.append(
            {
                "concept_id": workflow_id,
                "name": _safe_str(raw.get("name")) or workflow_id,
                "description": _safe_str(raw.get("description")),
            }
        )
    return tuple(rows)


def _normalise_string_sequence(raw_items: Any) -> tuple[str, ...]:
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        return ()
    values: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        text = _safe_str(raw)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        values.append(text)
    return tuple(values)


def _serialise_selector_response(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True)
    except Exception:
        return str(value)


def _normalise_benchmark_case(
    raw_case: Mapping[str, Any],
    *,
    index: int,
) -> SelectorBenchmarkCase | None:
    candidate_workflows = _normalise_candidate_workflows(raw_case.get("candidate_workflows"))
    if not candidate_workflows:
        return None

    allowed_workflow_ids = _normalise_string_sequence(raw_case.get("allowed_workflow_ids"))
    if not allowed_workflow_ids:
        expected_workflow_id = _safe_str(raw_case.get("expected_workflow_id"))
        if expected_workflow_id:
            allowed_workflow_ids = (expected_workflow_id,)
    if not allowed_workflow_ids:
        return None

    expected_routing_outcome = _safe_str(
        raw_case.get("expected_routing_outcome")
    ).lower() or "workflow_selected"
    if expected_routing_outcome not in _EXPECTED_ROUTING_OUTCOMES:
        expected_routing_outcome = "workflow_selected"

    baseline_workflow_id = _safe_str(raw_case.get("baseline_workflow_id"))
    if not baseline_workflow_id:
        baseline_workflow_id = _safe_str(candidate_workflows[0].get("concept_id"))

    selector_response_text = _serialise_selector_response(raw_case.get("selector_response"))
    prompt_failure_reason = _safe_str(raw_case.get("prompt_failure_reason")) or None
    prompt_failure_detail = _safe_str(raw_case.get("prompt_failure_detail")) or None
    if not selector_response_text and prompt_failure_reason is None:
        return None

    case_id = _safe_str(raw_case.get("case_id")) or f"selector_case_{index:03d}"
    return SelectorBenchmarkCase(
        case_id=case_id,
        turn_text=_safe_str(raw_case.get("turn_text"), limit=4000),
        candidate_workflows=candidate_workflows,
        allowed_workflow_ids=allowed_workflow_ids,
        expected_routing_outcome=expected_routing_outcome,
        baseline_workflow_id=baseline_workflow_id,
        selector_response_text=selector_response_text,
        prompt_failure_reason=prompt_failure_reason,
        prompt_failure_detail=prompt_failure_detail,
        notes=_safe_str(raw_case.get("notes"), limit=1000),
        jira_issue_keys=_normalise_string_sequence(raw_case.get("jira_issue_keys")),
        source_request_id=_safe_str(raw_case.get("source_request_id")) or None,
    )


def _normalise_cases(raw_cases: Any) -> list[SelectorBenchmarkCase]:
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)):
        return []

    normalised: list[SelectorBenchmarkCase] = []
    for index, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, Mapping):
            continue
        case = _normalise_benchmark_case(raw_case, index=index)
        if case is not None:
            normalised.append(case)
    return normalised


def load_selector_routing_benchmark_cases(
    *,
    case_set: str | None = None,
    bundle_path: Path | str | None = None,
) -> dict[str, Any]:
    resolved_path = Path(bundle_path) if bundle_path is not None else DEFAULT_SELECTOR_ROUTING_BENCHMARK_BUNDLE_PATH
    bundle_text = resolved_path.read_text(encoding="utf-8")
    bundle_data = json.loads(bundle_text)
    if not isinstance(bundle_data, Mapping):
        raise ValueError("selector_routing_benchmark_seed_bundle_invalid")

    requested_case_set = _safe_str(case_set) or _safe_str(
        bundle_data.get("default_case_set")
    ) or DEFAULT_SELECTOR_ROUTING_CASE_SET
    case_sets = bundle_data.get("case_sets")
    if isinstance(case_sets, Mapping):
        raw_cases = case_sets.get(requested_case_set)
        if raw_cases is None:
            raise ValueError("selector_routing_benchmark_case_set_not_found")
    else:
        raw_cases = bundle_data.get("cases")

    cases = _normalise_cases(raw_cases)
    return {
        "cases": cases,
        "case_set": requested_case_set,
        "bundle_path": str(resolved_path),
        "bundle_sha256": hashlib.sha256(bundle_text.encode("utf-8")).hexdigest(),
        "seed_schema_version": _safe_str(bundle_data.get("schema_version"))
        or SELECTOR_ROUTING_BENCHMARK_SEED_SCHEMA_VERSION,
        "source": "seed_bundle",
    }


def _build_selector_instance() -> WorkflowSelector:
    return WorkflowSelector(
        registry=WorkflowRegistry(),
        prompt_service=PromptTemplateService(default_max_chars=6000),
    )


def _evaluate_selector_case(
    *,
    selector: WorkflowSelector,
    case: SelectorBenchmarkCase,
) -> dict[str, Any]:
    candidate_ids = tuple(
        _safe_str(item.get("concept_id"))
        for item in case.candidate_workflows
        if _safe_str(item.get("concept_id"))
    )
    if case.prompt_failure_reason:
        selection_prompt = WorkflowSelectionPrompt(
            prompt_id=None,
            prompt_text=None,
            discovered_workflow_ids=candidate_ids,
            candidate_entries=case.candidate_workflows,
            candidate_list_text=None,
            requested_prompt_ids=(),
            prompt_failure_reason=case.prompt_failure_reason,
            prompt_failure_detail=case.prompt_failure_detail,
        )
        selection = selector.resolve_prompt_unavailable_selection(
            selection_prompt=selection_prompt
        )
    else:
        selection = selector.resolve_selection(
            raw_response=case.selector_response_text,
            prompt_id=None,
            prompt_used=None,
            discovered_workflow_ids=candidate_ids,
            candidate_entries=case.candidate_workflows,
        )

    matched_expected_route = selection.workflow_id in case.allowed_workflow_ids
    baseline_hit = case.baseline_workflow_id in case.allowed_workflow_ids

    if matched_expected_route:
        overall_outcome = (
            "abstain_escalate_no_safe_route"
            if case.expected_routing_outcome == "abstain_escalate_no_safe_route"
            else "successful_completion"
        )
    else:
        overall_outcome = "tool_or_workflow_misrouting"

    metric_labels = {
        label_name: label_name == overall_outcome
        for label_name in SELECTOR_BENCHMARK_OUTCOME_LABELS
    }
    primary_expected_workflow_id = (
        case.allowed_workflow_ids[0] if case.allowed_workflow_ids else None
    )
    return {
        "case_id": case.case_id,
        "turn_text": case.turn_text,
        "expected_workflow_id": primary_expected_workflow_id,
        "expected_workflow_ids": list(case.allowed_workflow_ids),
        "expected_routing_outcome": case.expected_routing_outcome,
        "baseline_workflow_id": case.baseline_workflow_id,
        "candidate_workflows": [dict(item) for item in case.candidate_workflows],
        "selected_workflow_id": selection.workflow_id,
        "verdict": selection.verdict,
        "selection_source": selection.selection_source,
        "confidence_score": round(float(selection.confidence_score), 6),
        "reasoning": selection.reasoning,
        "matched_expected_route": matched_expected_route,
        "baseline_hit": baseline_hit,
        "overall_outcome": overall_outcome,
        "metric_labels": metric_labels,
        "raw_response": case.selector_response_text,
        "prompt_failure_reason": case.prompt_failure_reason,
        "notes": case.notes,
        "evidence": {
            "jira_issue_keys": list(case.jira_issue_keys),
            "source_request_id": case.source_request_id,
        },
    }


def _format_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((float(numerator) * 100.0) / float(denominator), 2)


def _build_selector_benchmark_signals(
    *,
    case_count: int,
    matched_case_count: int,
    baseline_hit_count: int,
    abstain_case_count: int,
    abstain_matched_count: int,
    misrouting_count: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    signals: list[dict[str, Any]] = []

    def _add_signal(
        *,
        signal_id: str,
        title: str,
        status: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        signals.append(
            {
                "signal_id": signal_id,
                "dimension": "selector_routing",
                "title": title,
                "status": status,
                "details": dict(details or {}),
            }
        )

    if case_count <= 0:
        _add_signal(
            signal_id="selector_benchmark_corpus_present",
            title="Selector benchmark corpus contains evaluable cases",
            status="fail",
            details={"case_count": case_count},
        )
    else:
        _add_signal(
            signal_id="selector_benchmark_corpus_present",
            title="Selector benchmark corpus contains evaluable cases",
            status="pass",
            details={"case_count": case_count},
        )

    selector_accuracy = _format_rate(matched_case_count, case_count)
    baseline_accuracy = _format_rate(baseline_hit_count, case_count)
    _add_signal(
        signal_id="selector_accuracy_not_worse_than_baseline",
        title="Selector routing is not worse than baseline case ordering",
        status="pass" if selector_accuracy >= baseline_accuracy else "fail",
        details={
            "selector_accuracy_pct": selector_accuracy,
            "baseline_accuracy_pct": baseline_accuracy,
        },
    )

    if abstain_case_count <= 0:
        _add_signal(
            signal_id="abstain_cases_routed_safely",
            title="Abstain or no-safe-route cases are represented and routed safely",
            status="not_evaluated",
            details={"abstain_case_count": 0},
        )
    else:
        _add_signal(
            signal_id="abstain_cases_routed_safely",
            title="Abstain or no-safe-route cases are represented and routed safely",
            status="pass" if abstain_matched_count == abstain_case_count else "fail",
            details={
                "abstain_case_count": abstain_case_count,
                "abstain_matched_count": abstain_matched_count,
            },
        )

    _add_signal(
        signal_id="selector_misrouting_examples_detected",
        title="Selector corpus includes misrouting or failure examples",
        status="pass" if misrouting_count > 0 else "not_evaluated",
        details={"misrouting_count": misrouting_count},
    )

    summary = {
        "pass_count": sum(1 for signal in signals if signal["status"] == "pass"),
        "fail_count": sum(1 for signal in signals if signal["status"] == "fail"),
        "not_evaluated_count": sum(
            1 for signal in signals if signal["status"] == "not_evaluated"
        ),
        "total_count": len(signals),
    }
    return signals, summary


def build_selector_routing_benchmark_report(
    *,
    cases: Sequence[Mapping[str, Any]] | None = None,
    case_set: str | None = None,
    max_cases: int | None = None,
    bundle_path: Path | str | None = None,
) -> dict[str, Any]:
    if cases is not None:
        normalised_cases = _normalise_cases(cases)
        source_info = {
            "case_set": _safe_str(case_set) or "inline",
            "bundle_path": None,
            "bundle_sha256": _hash_payload(cases),
            "seed_schema_version": None,
            "source": "inline",
        }
    else:
        source_info = load_selector_routing_benchmark_cases(
            case_set=case_set,
            bundle_path=bundle_path,
        )
        normalised_cases = list(source_info.get("cases") or [])

    bounded_max_cases = _coerce_max_cases(max_cases)
    if bounded_max_cases is not None:
        normalised_cases = normalised_cases[:bounded_max_cases]

    if not normalised_cases:
        filters_payload = {
            "case_set": source_info.get("case_set"),
            "max_cases": bounded_max_cases,
            "case_source": source_info.get("source"),
        }
        return {
            "collection": "selector_routing_benchmark_cases",
            "benchmark_generated_at_utc": _utc_now_iso(),
            "filters": filters_payload,
            "metrics": {
                "scanned_count": 0,
                "matched_case_count": 0,
                "selector_accuracy_pct": 0.0,
                "baseline_accuracy_pct": 0.0,
                "accuracy_improvement_pct": 0.0,
                "outcome_label_counts": {
                    label_name: 0 for label_name in SELECTOR_BENCHMARK_OUTCOME_LABELS
                },
                "metric_schema": {
                    "benchmark_schema_version": SELECTOR_ROUTING_BENCHMARK_SCHEMA_VERSION,
                    "summary_schema_version": TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION,
                    "outcome_labels": list(SELECTOR_BENCHMARK_OUTCOME_LABELS),
                },
            },
            "benchmark_fingerprint": _hash_payload(filters_payload),
            "seeded_cases": [],
            "replay_cases": [],
            "benchmark_signals": [
                {
                    "signal_id": "selector_benchmark_corpus_present",
                    "dimension": "selector_routing",
                    "title": "Selector benchmark corpus contains evaluable cases",
                    "status": "fail",
                    "details": {"case_count": 0},
                }
            ],
            "benchmark_signal_summary": {
                "pass_count": 0,
                "fail_count": 1,
                "not_evaluated_count": 0,
                "total_count": 1,
            },
            "capability_gaps": [
                {
                    "gap_id": "no_selector_benchmark_cases",
                    "title": "No selector benchmark corpus cases are available",
                    "severity": "high",
                    "details": {
                        "case_set": source_info.get("case_set"),
                        "source": source_info.get("source"),
                    },
                }
            ],
            "recommendations": [
                {
                    "recommendation_id": "author_selector_benchmark_cases",
                    "priority": "high",
                    "summary": "Add selector-routing benchmark cases before using the selector benchmark as a regression gate.",
                }
            ],
            "corpus": {
                "case_set": source_info.get("case_set"),
                "source": source_info.get("source"),
                "bundle_path": source_info.get("bundle_path"),
                "bundle_sha256": source_info.get("bundle_sha256"),
                "seed_schema_version": source_info.get("seed_schema_version"),
            },
            "success": True,
        }

    selector = _build_selector_instance()
    replay_cases = [
        _evaluate_selector_case(selector=selector, case=case)
        for case in normalised_cases
    ]

    case_count = len(replay_cases)
    matched_case_count = sum(
        1 for case in replay_cases if bool(case.get("matched_expected_route"))
    )
    baseline_hit_count = sum(1 for case in replay_cases if bool(case.get("baseline_hit")))
    abstain_case_count = sum(
        1
        for case in replay_cases
        if case.get("expected_routing_outcome") == "abstain_escalate_no_safe_route"
    )
    abstain_matched_count = sum(
        1
        for case in replay_cases
        if case.get("expected_routing_outcome") == "abstain_escalate_no_safe_route"
        and bool(case.get("matched_expected_route"))
    )
    outcome_label_counts = {
        label_name: 0 for label_name in SELECTOR_BENCHMARK_OUTCOME_LABELS
    }
    verdict_counts: dict[str, int] = {}
    selection_source_counts: dict[str, int] = {}
    overall_outcome_counts: dict[str, int] = {}

    for case in replay_cases:
        metric_labels = case.get("metric_labels")
        if isinstance(metric_labels, Mapping):
            for label_name in outcome_label_counts:
                if bool(metric_labels.get(label_name)):
                    outcome_label_counts[label_name] += 1

        verdict = _safe_str(case.get("verdict")) or "unknown"
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        selection_source = _safe_str(case.get("selection_source")) or "unknown"
        selection_source_counts[selection_source] = (
            selection_source_counts.get(selection_source, 0) + 1
        )
        overall_outcome = _safe_str(case.get("overall_outcome")) or "unknown"
        overall_outcome_counts[overall_outcome] = (
            overall_outcome_counts.get(overall_outcome, 0) + 1
        )

    misrouting_count = outcome_label_counts["tool_or_workflow_misrouting"]
    benchmark_signals, benchmark_signal_summary = _build_selector_benchmark_signals(
        case_count=case_count,
        matched_case_count=matched_case_count,
        baseline_hit_count=baseline_hit_count,
        abstain_case_count=abstain_case_count,
        abstain_matched_count=abstain_matched_count,
        misrouting_count=misrouting_count,
    )

    metrics_payload = {
        "scanned_count": case_count,
        "matched_case_count": matched_case_count,
        "selector_accuracy_pct": _format_rate(matched_case_count, case_count),
        "baseline_accuracy_pct": _format_rate(baseline_hit_count, case_count),
        "accuracy_improvement_pct": round(
            _format_rate(matched_case_count, case_count)
            - _format_rate(baseline_hit_count, case_count),
            2,
        ),
        "abstain_case_count": abstain_case_count,
        "abstain_matched_count": abstain_matched_count,
        "misrouting_count": misrouting_count,
        "verdict_counts": verdict_counts,
        "selection_source_counts": selection_source_counts,
        "outcome_label_counts": outcome_label_counts,
        "outcome_label_rates_pct": {
            f"{label_name}_rate_pct": _format_rate(count, case_count)
            for label_name, count in outcome_label_counts.items()
        },
        "overall_outcome_counts": overall_outcome_counts,
        "metric_schema": {
            "benchmark_schema_version": SELECTOR_ROUTING_BENCHMARK_SCHEMA_VERSION,
            "summary_schema_version": TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION,
            "outcome_labels": list(SELECTOR_BENCHMARK_OUTCOME_LABELS),
        },
    }
    filters_payload = {
        "case_set": source_info.get("case_set"),
        "max_cases": bounded_max_cases,
        "case_source": source_info.get("source"),
    }
    recommendations: list[dict[str, Any]] = []
    if misrouting_count > 0:
        recommendations.append(
            {
                "recommendation_id": "inspect_selector_misrouting_cases",
                "priority": "high",
                "summary": "Inspect the selector benchmark replay cases marked as tool_or_workflow_misrouting and convert the highest-value ones into regression gates for JVNAUTOSCI-966 / 967.",
            }
        )
    if abstain_case_count > abstain_matched_count:
        recommendations.append(
            {
                "recommendation_id": "preserve_no_safe_route_paths",
                "priority": "high",
                "summary": "Add or repair selector guidance so abstain or no-safe-route cases keep routing to the intended safe workflow.",
            }
        )
    return {
        "collection": "selector_routing_benchmark_cases",
        "benchmark_generated_at_utc": _utc_now_iso(),
        "filters": filters_payload,
        "metrics": metrics_payload,
        "benchmark_fingerprint": _hash_payload(
            {"filters": filters_payload, "replay_cases": replay_cases}
        ),
        "seeded_cases": replay_cases,
        "replay_cases": replay_cases,
        "benchmark_signals": benchmark_signals,
        "benchmark_signal_summary": benchmark_signal_summary,
        "capability_gaps": [],
        "recommendations": recommendations,
        "corpus": {
            "case_set": source_info.get("case_set"),
            "source": source_info.get("source"),
            "bundle_path": source_info.get("bundle_path"),
            "bundle_sha256": source_info.get("bundle_sha256"),
            "seed_schema_version": source_info.get("seed_schema_version"),
        },
        "success": True,
    }


__all__ = [
    "DEFAULT_SELECTOR_ROUTING_BENCHMARK_BUNDLE_PATH",
    "DEFAULT_SELECTOR_ROUTING_CASE_SET",
    "SELECTOR_ROUTING_BENCHMARK_SCHEMA_VERSION",
    "SELECTOR_BENCHMARK_OUTCOME_LABELS",
    "build_selector_routing_benchmark_report",
    "load_selector_routing_benchmark_cases",
]
