"""Selector-routing benchmark corpus loading and replay evaluation.

This benchmark stays intentionally close to the existing Phase 1 evaluation
surfaces:

- it uses the selector's own response-resolution logic;
- it emits the shared execution-correctness outcome labels introduced in
  JVNAUTOSCI-965;
- it returns replay-style case rows that downstream dashboards and operating
  protocols can consume without inventing a separate benchmark vocabulary.

The default corpus and rubric are loaded from a represented Vontology benchmark
suite. Repo-side seed bundles remain as explicit import fixtures only.
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
from .benchmark_suite_vontology_service import (
    BenchmarkSuiteAuthorityMissingError,
    SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID,
    load_benchmark_suite_case_set,
)

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

DEFAULT_SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID = (
    SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
)


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
    replay_family_id: str | None = None
    source_session_id: str | None = None
    source_request_id: str | None = None
    source_request_ids: tuple[str, ...] = ()
    case_tags: tuple[str, ...] = ()


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
        row = {str(key): value for key, value in raw.items() if isinstance(key, str)}
        row["concept_id"] = workflow_id
        row["name"] = _safe_str(raw.get("name")) or workflow_id
        row["description"] = _safe_str(raw.get("description"))
        rows.append(row)
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


def _signal_definition_by_id(rubric: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    raw_definitions = rubric.get("signal_definitions")
    if not isinstance(raw_definitions, Sequence) or isinstance(
        raw_definitions,
        (str, bytes, bytearray),
    ):
        return {}
    definitions: dict[str, dict[str, str]] = {}
    for raw in raw_definitions:
        if not isinstance(raw, Mapping):
            continue
        signal_id = _safe_str(raw.get("signal_id"))
        if not signal_id:
            continue
        definitions[signal_id] = {
            "dimension": _safe_str(raw.get("dimension")) or "selector_routing",
            "title": _safe_str(raw.get("title")) or signal_id,
        }
    return definitions


def _required_selector_rubric_values(rubric: Mapping[str, Any]) -> dict[str, Any]:
    outcome_labels = _normalise_string_sequence(rubric.get("outcome_labels"))
    expected_routing_outcomes = _normalise_string_sequence(
        rubric.get("expected_routing_outcomes")
    )
    abstain_expected_routing_outcomes = _normalise_string_sequence(
        rubric.get("abstain_expected_routing_outcomes")
    )
    raw_expected_outcome_labels = rubric.get("expected_routing_outcome_labels")
    expected_outcome_labels: dict[str, str] = {}
    if isinstance(raw_expected_outcome_labels, Mapping):
        for raw_outcome, raw_label in raw_expected_outcome_labels.items():
            outcome = _safe_str(raw_outcome).lower()
            label = _safe_str(raw_label)
            if outcome and label:
                expected_outcome_labels[outcome] = label
    required_keys = {
        "default_expected_routing_outcome": _safe_str(
            rubric.get("default_expected_routing_outcome")
        ).lower(),
        "misrouting_outcome_label": _safe_str(
            rubric.get("misrouting_outcome_label")
        ),
    }
    missing = [
        key for key, value in required_keys.items() if not value
    ]
    if not outcome_labels:
        missing.append("outcome_labels")
    if not expected_routing_outcomes:
        missing.append("expected_routing_outcomes")
    if required_keys["default_expected_routing_outcome"] and (
        required_keys["default_expected_routing_outcome"]
        not in expected_routing_outcomes
    ):
        missing.append("default_expected_routing_outcome_not_in_expected_outcomes")
    if not expected_outcome_labels:
        missing.append("expected_routing_outcome_labels")
    for outcome in expected_routing_outcomes:
        label = expected_outcome_labels.get(outcome)
        if not label:
            missing.append(f"missing_label_for_expected_outcome:{outcome}")
        elif label not in outcome_labels:
            missing.append(f"expected_outcome_label_not_in_outcome_labels:{outcome}")
    for outcome in abstain_expected_routing_outcomes:
        if outcome not in expected_routing_outcomes:
            missing.append(f"abstain_outcome_not_in_expected_outcomes:{outcome}")
    misrouting_label = required_keys["misrouting_outcome_label"]
    if misrouting_label and misrouting_label not in outcome_labels:
        missing.append("misrouting_outcome_label_not_in_outcome_labels")
    if missing:
        raise ValueError(
            "selector_benchmark_rubric_invalid:" + ",".join(sorted(missing))
        )
    return {
        **required_keys,
        "outcome_labels": outcome_labels,
        "expected_routing_outcomes": expected_routing_outcomes,
        "abstain_expected_routing_outcomes": abstain_expected_routing_outcomes,
        "expected_routing_outcome_labels": expected_outcome_labels,
        "signal_definitions": _signal_definition_by_id(rubric),
    }


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
    rubric_values: Mapping[str, Any],
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

    expected_routing_outcomes = tuple(
        str(item) for item in rubric_values.get("expected_routing_outcomes") or ()
    )
    expected_routing_outcome = _safe_str(raw_case.get("expected_routing_outcome")).lower()
    if not expected_routing_outcome:
        expected_routing_outcome = str(
            rubric_values.get("default_expected_routing_outcome")
            or expected_routing_outcomes[0]
        )
    if expected_routing_outcome not in expected_routing_outcomes:
        expected_routing_outcome = str(
            rubric_values.get("default_expected_routing_outcome")
            or expected_routing_outcomes[0]
        )

    baseline_workflow_id = _safe_str(raw_case.get("baseline_workflow_id"))
    if not baseline_workflow_id:
        baseline_workflow_id = _safe_str(candidate_workflows[0].get("concept_id"))

    selector_response_text = _serialise_selector_response(raw_case.get("selector_response"))
    prompt_failure_reason = _safe_str(raw_case.get("prompt_failure_reason")) or None
    prompt_failure_detail = _safe_str(raw_case.get("prompt_failure_detail")) or None
    if not selector_response_text and prompt_failure_reason is None:
        return None

    source_request_ids = _normalise_string_sequence(raw_case.get("source_request_ids"))
    source_request_id = _safe_str(raw_case.get("source_request_id")) or None
    if not source_request_ids and source_request_id:
        source_request_ids = (source_request_id,)
    if source_request_id is None and source_request_ids:
        source_request_id = source_request_ids[0]

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
        replay_family_id=_safe_str(raw_case.get("replay_family_id")) or None,
        source_session_id=_safe_str(raw_case.get("source_session_id")) or None,
        source_request_id=source_request_id,
        source_request_ids=source_request_ids,
        case_tags=_normalise_string_sequence(raw_case.get("case_tags")),
    )


def _normalise_cases(
    raw_cases: Any,
    *,
    rubric_values: Mapping[str, Any],
) -> list[SelectorBenchmarkCase]:
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)):
        return []

    normalised: list[SelectorBenchmarkCase] = []
    for index, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, Mapping):
            continue
        case = _normalise_benchmark_case(
            raw_case,
            index=index,
            rubric_values=rubric_values,
        )
        if case is not None:
            normalised.append(case)
    return normalised


def load_selector_routing_benchmark_cases(
    *,
    case_set: str | None = None,
    bundle_path: Path | str | None = None,
    suite_concept_id: str | None = None,
) -> dict[str, Any]:
    source_info = load_benchmark_suite_case_set(
        suite_concept_id=(
            _safe_str(suite_concept_id)
            or DEFAULT_SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
        ),
        case_set=case_set,
        fixture_path=bundle_path,
    )
    rubric_values = _required_selector_rubric_values(source_info.get("rubric") or {})
    cases = _normalise_cases(
        source_info.get("cases"),
        rubric_values=rubric_values,
    )
    return {
        "cases": cases,
        "case_set": source_info.get("case_set"),
        "bundle_path": source_info.get("source_path"),
        "bundle_sha256": source_info.get("fixture_sha256"),
        "definition_sha256": source_info.get("definition_sha256"),
        "seed_schema_version": source_info.get("seed_schema_version"),
        "suite_schema_version": source_info.get("suite_schema_version"),
        "definition_schema_version": source_info.get("definition_schema_version"),
        "suite_concept_id": source_info.get("suite_concept_id"),
        "suite_id": source_info.get("suite_id"),
        "source": source_info.get("source"),
        "source_predicate": source_info.get("source_predicate"),
        "authority_diagnostics": source_info.get("authority_diagnostics"),
        "rubric": rubric_values,
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
    rubric_values: Mapping[str, Any],
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
        expected_outcome_labels = rubric_values.get("expected_routing_outcome_labels")
        expected_outcome_label_map = (
            expected_outcome_labels
            if isinstance(expected_outcome_labels, Mapping)
            else {}
        )
        overall_outcome = (
            _safe_str(expected_outcome_label_map.get(case.expected_routing_outcome))
            or case.expected_routing_outcome
        )
    else:
        overall_outcome = str(rubric_values.get("misrouting_outcome_label"))

    outcome_labels = tuple(str(item) for item in rubric_values.get("outcome_labels") or ())
    metric_labels = {
        label_name: label_name == overall_outcome
        for label_name in outcome_labels
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
        "selection_metadata": dict(selection.selection_metadata),
        "confidence_score": round(float(selection.confidence_score), 6),
        "reasoning": selection.reasoning,
        "matched_expected_route": matched_expected_route,
        "baseline_hit": baseline_hit,
        "overall_outcome": overall_outcome,
        "metric_labels": metric_labels,
        "raw_response": case.selector_response_text,
        "prompt_failure_reason": case.prompt_failure_reason,
        "notes": case.notes,
        "replay_family_id": case.replay_family_id,
        "source_session_id": case.source_session_id,
        "source_request_id": case.source_request_id,
        "source_request_ids": list(case.source_request_ids),
        "case_tags": list(case.case_tags),
        "evidence": {
            "jira_issue_keys": list(case.jira_issue_keys),
            "replay_family_id": case.replay_family_id,
            "source_session_id": case.source_session_id,
            "source_request_id": case.source_request_id,
            "source_request_ids": list(case.source_request_ids),
            "case_tags": list(case.case_tags),
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
    signal_definitions: Mapping[str, Mapping[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    signals: list[dict[str, Any]] = []

    def _add_signal(
        *,
        signal_id: str,
        status: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        definition = signal_definitions.get(signal_id, {})
        signals.append(
            {
                "signal_id": signal_id,
                "dimension": definition.get("dimension") or "selector_routing",
                "title": definition.get("title") or signal_id,
                "status": status,
                "details": dict(details or {}),
            }
        )

    if case_count <= 0:
        _add_signal(
            signal_id="selector_benchmark_corpus_present",
            status="fail",
            details={"case_count": case_count},
        )
    else:
        _add_signal(
            signal_id="selector_benchmark_corpus_present",
            status="pass",
            details={"case_count": case_count},
        )

    selector_accuracy = _format_rate(matched_case_count, case_count)
    baseline_accuracy = _format_rate(baseline_hit_count, case_count)
    _add_signal(
        signal_id="selector_accuracy_not_worse_than_baseline",
        status="pass" if selector_accuracy >= baseline_accuracy else "fail",
        details={
            "selector_accuracy_pct": selector_accuracy,
            "baseline_accuracy_pct": baseline_accuracy,
        },
    )

    if abstain_case_count <= 0:
        _add_signal(
            signal_id="abstain_cases_routed_safely",
            status="not_evaluated",
            details={"abstain_case_count": 0},
        )
    else:
        _add_signal(
            signal_id="abstain_cases_routed_safely",
            status="pass" if abstain_matched_count == abstain_case_count else "fail",
            details={
                "abstain_case_count": abstain_case_count,
                "abstain_matched_count": abstain_matched_count,
            },
        )

    _add_signal(
        signal_id="selector_misrouting_examples_detected",
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


def _build_selector_benchmark_missing_authority_report(
    *,
    case_set: str | None,
    max_cases: int | None,
    suite_concept_id: str,
    diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    filters_payload = {
        "case_set": _safe_str(case_set) or None,
        "max_cases": _coerce_max_cases(max_cases),
        "case_source": "vontology",
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
            "outcome_label_counts": {},
            "metric_schema": {
                "benchmark_schema_version": SELECTOR_ROUTING_BENCHMARK_SCHEMA_VERSION,
                "summary_schema_version": TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION,
                "outcome_labels": [],
            },
        },
        "benchmark_fingerprint": _hash_payload(
            {
                "filters": filters_payload,
                "suite_concept_id": suite_concept_id,
                "authority_diagnostics": dict(diagnostics),
            }
        ),
        "seeded_cases": [],
        "replay_cases": [],
        "benchmark_signals": [
            {
                "signal_id": "selector_benchmark_suite_authority_present",
                "dimension": "selector_routing",
                "title": "Selector benchmark suite authority is represented in Vontology",
                "status": "fail",
                "details": dict(diagnostics),
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
                "gap_id": "benchmark_suite_authority_missing",
                "title": "Represented selector benchmark suite authority is missing",
                "severity": "high",
                "details": dict(diagnostics),
            }
        ],
        "recommendations": [
            {
                "recommendation_id": "materialise_selector_benchmark_suite",
                "priority": "high",
                "summary": (
                    "Materialise the selector benchmark suite definition in "
                    "Vontology before using this benchmark as a regression gate."
                ),
            }
        ],
        "corpus": {
            "case_set": _safe_str(case_set) or None,
            "source": "vontology",
            "suite_concept_id": suite_concept_id,
            "authority_diagnostics": dict(diagnostics),
        },
        "error_code": "benchmark_suite_authority_missing",
        "success": False,
    }


def build_selector_routing_benchmark_report(
    *,
    cases: Sequence[Mapping[str, Any]] | None = None,
    case_set: str | None = None,
    max_cases: int | None = None,
    bundle_path: Path | str | None = None,
    suite_concept_id: str | None = None,
) -> dict[str, Any]:
    if cases is not None:
        try:
            if bundle_path is not None:
                fixture_source = load_selector_routing_benchmark_cases(
                    case_set=case_set,
                    bundle_path=bundle_path,
                    suite_concept_id=suite_concept_id,
                )
                rubric_values = fixture_source["rubric"]
            else:
                source_for_rubric = load_selector_routing_benchmark_cases(
                    case_set=None,
                    suite_concept_id=suite_concept_id,
                )
                rubric_values = source_for_rubric["rubric"]
        except BenchmarkSuiteAuthorityMissingError as exc:
            return _build_selector_benchmark_missing_authority_report(
                case_set=case_set,
                max_cases=max_cases,
                suite_concept_id=(
                    suite_concept_id
                    or DEFAULT_SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
                ),
                diagnostics=exc.diagnostics,
            )
        normalised_cases = _normalise_cases(cases, rubric_values=rubric_values)
        source_info = {
            "case_set": _safe_str(case_set) or "inline",
            "bundle_path": None,
            "bundle_sha256": _hash_payload(cases),
            "seed_schema_version": None,
            "suite_schema_version": None,
            "definition_schema_version": None,
            "suite_concept_id": suite_concept_id
            or DEFAULT_SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID,
            "suite_id": None,
            "source": "inline",
            "source_predicate": None,
            "authority_diagnostics": None,
            "rubric": rubric_values,
        }
    else:
        try:
            source_info = load_selector_routing_benchmark_cases(
                case_set=case_set,
                bundle_path=bundle_path,
                suite_concept_id=suite_concept_id,
            )
        except BenchmarkSuiteAuthorityMissingError as exc:
            return _build_selector_benchmark_missing_authority_report(
                case_set=case_set,
                max_cases=max_cases,
                suite_concept_id=(
                    suite_concept_id
                    or DEFAULT_SELECTOR_ROUTING_BENCHMARK_SUITE_CONCEPT_ID
                ),
                diagnostics=exc.diagnostics,
            )
        rubric_values = source_info["rubric"]
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
                    label_name: 0
                    for label_name in rubric_values.get("outcome_labels", ())
                },
                "metric_schema": {
                    "benchmark_schema_version": SELECTOR_ROUTING_BENCHMARK_SCHEMA_VERSION,
                    "summary_schema_version": TURN_EXECUTION_CORRECTNESS_SCHEMA_VERSION,
                    "outcome_labels": list(rubric_values.get("outcome_labels", ())),
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
                "suite_schema_version": source_info.get("suite_schema_version"),
                "definition_schema_version": source_info.get("definition_schema_version"),
                "suite_concept_id": source_info.get("suite_concept_id"),
                "suite_id": source_info.get("suite_id"),
                "definition_sha256": source_info.get("definition_sha256"),
                "source_predicate": source_info.get("source_predicate"),
                "authority_diagnostics": source_info.get("authority_diagnostics"),
            },
            "success": True,
        }

    selector = _build_selector_instance()
    replay_cases = [
        _evaluate_selector_case(
            selector=selector,
            case=case,
            rubric_values=rubric_values,
        )
        for case in normalised_cases
    ]

    case_count = len(replay_cases)
    matched_case_count = sum(
        1 for case in replay_cases if bool(case.get("matched_expected_route"))
    )
    baseline_hit_count = sum(1 for case in replay_cases if bool(case.get("baseline_hit")))
    abstain_expected_outcomes = set(
        str(item) for item in rubric_values.get("abstain_expected_routing_outcomes") or ()
    )
    abstain_case_count = sum(
        1
        for case in replay_cases
        if str(case.get("expected_routing_outcome")) in abstain_expected_outcomes
    )
    abstain_matched_count = sum(
        1
        for case in replay_cases
        if str(case.get("expected_routing_outcome")) in abstain_expected_outcomes
        and bool(case.get("matched_expected_route"))
    )
    outcome_labels = tuple(str(item) for item in rubric_values.get("outcome_labels") or ())
    outcome_label_counts = {label_name: 0 for label_name in outcome_labels}
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

    misrouting_label = str(rubric_values.get("misrouting_outcome_label"))
    misrouting_count = outcome_label_counts.get(misrouting_label, 0)
    benchmark_signals, benchmark_signal_summary = _build_selector_benchmark_signals(
        case_count=case_count,
        matched_case_count=matched_case_count,
        baseline_hit_count=baseline_hit_count,
        abstain_case_count=abstain_case_count,
        abstain_matched_count=abstain_matched_count,
        misrouting_count=misrouting_count,
        signal_definitions=rubric_values.get("signal_definitions") or {},
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
            "outcome_labels": list(outcome_labels),
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
            "suite_schema_version": source_info.get("suite_schema_version"),
            "definition_schema_version": source_info.get("definition_schema_version"),
            "suite_concept_id": source_info.get("suite_concept_id"),
            "suite_id": source_info.get("suite_id"),
            "definition_sha256": source_info.get("definition_sha256"),
            "source_predicate": source_info.get("source_predicate"),
            "authority_diagnostics": source_info.get("authority_diagnostics"),
        },
        "success": True,
    }


__all__ = [
    "DEFAULT_SELECTOR_ROUTING_BENCHMARK_BUNDLE_PATH",
    "DEFAULT_SELECTOR_ROUTING_CASE_SET",
    "SELECTOR_ROUTING_BENCHMARK_SCHEMA_VERSION",
    "build_selector_routing_benchmark_report",
    "load_selector_routing_benchmark_cases",
]
