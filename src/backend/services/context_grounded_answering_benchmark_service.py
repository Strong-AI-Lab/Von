"""Deterministic benchmark coverage for context-grounded answering reliability.

The benchmark is intentionally reviewable rather than opaque. Suite, case, and
rubric authority lives in Vontology; repo-side seed bundles are import fixtures
for initially materialising canonical represented suites.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .benchmark_suite_vontology_service import (
    BenchmarkSuiteAuthorityMissingError,
    CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SUITE_CONCEPT_ID,
    load_benchmark_suite_case_set,
)

CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SCHEMA_VERSION = (
    "context_grounded_answering_benchmark.v1"
)
CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SEED_SCHEMA_VERSION = (
    "context_grounded_answering_benchmark.seed_bundle.v1"
)
DEFAULT_CONTEXT_GROUNDED_ANSWERING_BENCHMARK_CASE_SET = "phase1_seed"
DEFAULT_CONTEXT_GROUNDED_ANSWERING_BENCHMARK_BUNDLE_PATH = (
    Path(__file__).resolve().parent.parent
    / "workflows"
    / "repo_seed_bundles"
    / "context_grounded_answering_benchmark_seed_bundle.json"
)
DEFAULT_CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SUITE_CONCEPT_ID = (
    CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SUITE_CONCEPT_ID
)


@dataclass(frozen=True)
class ContextGroundedAnsweringBenchmarkCase:
    case_id: str
    prompt_text: str
    expected_authoritative_source: str
    expected_execution_mode: str
    expected_workflow_id: str | None
    coverage_tags: tuple[str, ...]
    answer_properties: tuple[dict[str, Any], ...]
    telemetry_checks: tuple[dict[str, Any], ...]
    validation_surfaces: tuple[dict[str, Any], ...]
    jira_issue_keys: tuple[str, ...] = ()
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
            "dimension": _safe_str(raw.get("dimension"))
            or "context_grounded_answering",
            "title": _safe_str(raw.get("title")) or signal_id,
        }
    return definitions


def _required_context_grounded_answering_rubric_values(
    rubric: Mapping[str, Any],
) -> dict[str, Any]:
    required_coverage_tags = _normalise_strings(
        rubric.get("required_coverage_tags")
    )
    required_authoritative_sources = _normalise_strings(
        rubric.get("required_authoritative_sources")
    )
    required_execution_modes = _normalise_strings(
        rubric.get("required_execution_modes")
    )
    required_answer_property_ids = _normalise_strings(
        rubric.get("required_answer_property_ids")
    )
    required_validation_surface_kind = _safe_str(
        rubric.get("required_validation_surface_kind")
    )
    missing: list[str] = []
    if not required_coverage_tags:
        missing.append("required_coverage_tags")
    if not required_authoritative_sources:
        missing.append("required_authoritative_sources")
    if not required_execution_modes:
        missing.append("required_execution_modes")
    if not required_answer_property_ids:
        missing.append("required_answer_property_ids")
    if not required_validation_surface_kind:
        missing.append("required_validation_surface_kind")
    if missing:
        raise ValueError(
            "context_grounded_answering_benchmark_rubric_invalid:"
            + ",".join(sorted(missing))
        )
    return {
        "required_coverage_tags": required_coverage_tags,
        "required_authoritative_sources": required_authoritative_sources,
        "required_execution_modes": required_execution_modes,
        "required_answer_property_ids": required_answer_property_ids,
        "required_validation_surface_kind": required_validation_surface_kind,
        "signal_definitions": _signal_definition_by_id(rubric),
    }


def _normalise_named_mapping_sequence(
    values: Any,
    *,
    id_key: str,
    default_description_key: str = "description",
) -> tuple[dict[str, Any], ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping):
            continue
        item_id = _safe_str(item.get(id_key))
        if not item_id:
            continue
        lowered = item_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalised: dict[str, Any] = {id_key: item_id}
        description = _safe_str(item.get(default_description_key), limit=1000)
        if description:
            normalised[default_description_key] = description
        for key in ("field_path", "expected_value", "surface_kind", "reference"):
            value = item.get(key)
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                normalised[key] = value
        output.append(normalised)
    return tuple(output)


def _normalise_case(
    raw_case: Mapping[str, Any],
    *,
    index: int,
) -> ContextGroundedAnsweringBenchmarkCase | None:
    prompt_text = _safe_str(raw_case.get("prompt_text"), limit=4000)
    if not prompt_text:
        return None

    expected_authoritative_source = _safe_str(
        raw_case.get("expected_authoritative_source")
    )
    if not expected_authoritative_source:
        return None

    expected_execution_mode = _safe_str(raw_case.get("expected_execution_mode")).lower()
    if not expected_execution_mode:
        return None

    coverage_tags = _normalise_strings(raw_case.get("coverage_tags"))
    if not coverage_tags:
        return None

    answer_properties = _normalise_named_mapping_sequence(
        raw_case.get("answer_properties"),
        id_key="property_id",
    )
    if not answer_properties:
        return None

    telemetry_checks = _normalise_named_mapping_sequence(
        raw_case.get("telemetry_checks"),
        id_key="check_id",
    )
    if not telemetry_checks:
        return None

    validation_surface_payloads = _normalise_named_mapping_sequence(
        raw_case.get("validation_surfaces"),
        id_key="reference",
        default_description_key="surface_kind",
    )
    validation_surfaces = tuple(
        {
            "reference": _safe_str(item.get("reference")),
            "surface_kind": _safe_str(item.get("surface_kind")) or "unknown",
        }
        for item in validation_surface_payloads
        if _safe_str(item.get("reference"))
    )
    if not validation_surfaces:
        return None

    case_id = _safe_str(raw_case.get("case_id")) or f"context_grounded_case_{index:03d}"
    return ContextGroundedAnsweringBenchmarkCase(
        case_id=case_id,
        prompt_text=prompt_text,
        expected_authoritative_source=expected_authoritative_source,
        expected_execution_mode=expected_execution_mode,
        expected_workflow_id=_safe_str(raw_case.get("expected_workflow_id")) or None,
        coverage_tags=coverage_tags,
        answer_properties=answer_properties,
        telemetry_checks=telemetry_checks,
        validation_surfaces=validation_surfaces,
        jira_issue_keys=_normalise_strings(raw_case.get("jira_issue_keys")),
        notes=_safe_str(raw_case.get("notes"), limit=1000),
    )


def load_context_grounded_answering_benchmark_cases(
    *,
    case_set: str | None = None,
    bundle_path: Path | str | None = None,
    suite_concept_id: str | None = None,
) -> dict[str, Any]:
    source_info = load_benchmark_suite_case_set(
        suite_concept_id=(
            _safe_str(suite_concept_id)
            or DEFAULT_CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SUITE_CONCEPT_ID
        ),
        case_set=case_set,
        fixture_path=bundle_path,
    )
    rubric_values = _required_context_grounded_answering_rubric_values(
        source_info.get("rubric") or {}
    )
    cases: list[ContextGroundedAnsweringBenchmarkCase] = []
    for index, raw_case in enumerate(source_info.get("cases") or (), start=1):
        if not isinstance(raw_case, Mapping):
            continue
        normalised = _normalise_case(raw_case, index=index)
        if normalised is not None:
            cases.append(normalised)

    return {
        "cases": cases,
        "case_set": source_info.get("case_set"),
        "bundle_path": source_info.get("source_path"),
        "bundle_sha256": source_info.get("fixture_sha256"),
        "definition_sha256": source_info.get("definition_sha256"),
        "seed_schema_version": source_info.get("seed_schema_version")
        or CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SEED_SCHEMA_VERSION,
        "suite_schema_version": source_info.get("suite_schema_version"),
        "definition_schema_version": source_info.get("definition_schema_version"),
        "suite_concept_id": source_info.get("suite_concept_id"),
        "suite_id": source_info.get("suite_id"),
        "source": source_info.get("source"),
        "source_predicate": source_info.get("source_predicate"),
        "authority_diagnostics": source_info.get("authority_diagnostics"),
        "rubric": rubric_values,
    }


def _build_context_grounded_answering_missing_authority_report(
    *,
    case_set: str | None,
    max_cases: int | None,
    suite_concept_id: str,
    diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    bounded_max_cases = (
        max(1, min(int(max_cases), 500)) if max_cases is not None else None
    )
    filters_payload = {
        "case_set": _safe_str(case_set) or None,
        "max_cases": bounded_max_cases,
        "case_source": "vontology",
    }
    return {
        "collection": "context_grounded_answering_benchmark_cases",
        "benchmark_generated_at_utc": _utc_now_iso(),
        "filters": filters_payload,
        "metrics": {
            "scanned_count": 0,
            "exact_path_case_count": 0,
            "telemetry_check_count": 0,
            "validation_surface_count": 0,
            "coverage_tag_counts": {},
            "authoritative_source_counts": {},
            "execution_mode_counts": {},
            "answer_property_counts": {},
            "validation_surface_kind_counts": {},
            "metric_schema": {
                "benchmark_schema_version": (
                    CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SCHEMA_VERSION
                ),
                "required_coverage_tags": [],
                "required_authoritative_sources": [],
                "required_execution_modes": [],
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
                "signal_id": "context_grounded_benchmark_suite_authority_present",
                "dimension": "context_grounded_answering",
                "title": (
                    "Context-grounded answering benchmark suite authority is "
                    "represented in Vontology"
                ),
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
                "title": (
                    "Represented context-grounded answering benchmark suite "
                    "authority is missing"
                ),
                "severity": "high",
                "details": dict(diagnostics),
            }
        ],
        "recommendations": [
            {
                "recommendation_id": (
                    "materialise_context_grounded_answering_benchmark_suite"
                ),
                "priority": "high",
                "summary": (
                    "Materialise the context-grounded answering benchmark suite "
                    "definition in Vontology before using this benchmark as a "
                    "regression gate."
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


def build_context_grounded_answering_benchmark_report(
    *,
    case_set: str | None = None,
    max_cases: int | None = None,
    bundle_path: Path | str | None = None,
    cases: Sequence[Mapping[str, Any]] | None = None,
    suite_concept_id: str | None = None,
) -> dict[str, Any]:
    if cases is None:
        try:
            source_info = load_context_grounded_answering_benchmark_cases(
                case_set=case_set,
                bundle_path=bundle_path,
                suite_concept_id=suite_concept_id,
            )
        except BenchmarkSuiteAuthorityMissingError as exc:
            return _build_context_grounded_answering_missing_authority_report(
                case_set=case_set,
                max_cases=max_cases,
                suite_concept_id=(
                    suite_concept_id
                    or DEFAULT_CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SUITE_CONCEPT_ID
                ),
                diagnostics=exc.diagnostics,
            )
        rubric_values = source_info["rubric"]
        normalised_cases = list(source_info.get("cases") or [])
    else:
        try:
            source_for_rubric = load_context_grounded_answering_benchmark_cases(
                case_set=None,
                bundle_path=bundle_path,
                suite_concept_id=suite_concept_id,
            )
        except BenchmarkSuiteAuthorityMissingError as exc:
            return _build_context_grounded_answering_missing_authority_report(
                case_set=case_set,
                max_cases=max_cases,
                suite_concept_id=(
                    suite_concept_id
                    or DEFAULT_CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SUITE_CONCEPT_ID
                ),
                diagnostics=exc.diagnostics,
            )
        rubric_values = source_for_rubric["rubric"]
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
            "suite_schema_version": None,
            "definition_schema_version": None,
            "suite_concept_id": suite_concept_id
            or DEFAULT_CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SUITE_CONCEPT_ID,
            "suite_id": None,
            "definition_sha256": source_for_rubric.get("definition_sha256"),
            "source_predicate": source_for_rubric.get("source_predicate"),
            "authority_diagnostics": source_for_rubric.get("authority_diagnostics"),
            "source": "inline",
            "rubric": rubric_values,
        }

    bounded_max_cases = max(1, min(int(max_cases), 500)) if max_cases is not None else None
    if bounded_max_cases is not None:
        normalised_cases = normalised_cases[:bounded_max_cases]

    required_coverage_tags = tuple(
        str(item) for item in rubric_values.get("required_coverage_tags") or ()
    )
    required_authoritative_sources = tuple(
        str(item)
        for item in rubric_values.get("required_authoritative_sources") or ()
    )
    required_execution_modes = tuple(
        str(item) for item in rubric_values.get("required_execution_modes") or ()
    )
    required_answer_property_ids = tuple(
        str(item) for item in rubric_values.get("required_answer_property_ids") or ()
    )
    required_validation_surface_kind = str(
        rubric_values.get("required_validation_surface_kind") or ""
    )
    signal_definitions_raw = rubric_values.get("signal_definitions")
    signal_definitions = (
        signal_definitions_raw if isinstance(signal_definitions_raw, Mapping) else {}
    )
    coverage_tag_counts = {tag: 0 for tag in required_coverage_tags}
    authoritative_source_counts: dict[str, int] = {}
    execution_mode_counts: dict[str, int] = {}
    answer_property_counts: dict[str, int] = {}
    validation_surface_kind_counts: dict[str, int] = {}
    validation_surface_count = 0
    telemetry_check_count = 0
    exact_path_case_count = 0
    replay_cases: list[dict[str, Any]] = []

    for case in normalised_cases:
        authoritative_source_counts[case.expected_authoritative_source] = (
            authoritative_source_counts.get(case.expected_authoritative_source, 0) + 1
        )
        execution_mode_counts[case.expected_execution_mode] = (
            execution_mode_counts.get(case.expected_execution_mode, 0) + 1
        )
        for coverage_tag in case.coverage_tags:
            if coverage_tag in coverage_tag_counts:
                coverage_tag_counts[coverage_tag] += 1
        for answer_property in case.answer_properties:
            property_id = _safe_str(answer_property.get("property_id")) or "unknown"
            answer_property_counts[property_id] = (
                answer_property_counts.get(property_id, 0) + 1
            )
        for telemetry_check in case.telemetry_checks:
            if _safe_str(telemetry_check.get("check_id")):
                telemetry_check_count += 1

        case_has_exact_path_surface = False
        serialised_surfaces: list[dict[str, Any]] = []
        for surface in case.validation_surfaces:
            reference = _safe_str(surface.get("reference"))
            if not reference:
                continue
            surface_kind = _safe_str(surface.get("surface_kind")) or "unknown"
            validation_surface_count += 1
            validation_surface_kind_counts[surface_kind] = (
                validation_surface_kind_counts.get(surface_kind, 0) + 1
            )
            if surface_kind == required_validation_surface_kind:
                case_has_exact_path_surface = True
            serialised_surfaces.append(
                {
                    "reference": reference,
                    "surface_kind": surface_kind,
                }
            )
        if case_has_exact_path_surface:
            exact_path_case_count += 1

        replay_cases.append(
            {
                "case_id": case.case_id,
                "prompt_text": case.prompt_text,
                "expected_authoritative_source": case.expected_authoritative_source,
                "expected_execution_mode": case.expected_execution_mode,
                "expected_workflow_id": case.expected_workflow_id,
                "coverage_tags": list(case.coverage_tags),
                "answer_properties": [dict(item) for item in case.answer_properties],
                "telemetry_checks": [dict(item) for item in case.telemetry_checks],
                "validation_surfaces": serialised_surfaces,
                "jira_issue_keys": list(case.jira_issue_keys),
                "notes": case.notes,
            }
        )

    case_count = len(replay_cases)
    missing_coverage_tags = [
        tag for tag, count in coverage_tag_counts.items() if count <= 0
    ]
    missing_authoritative_sources = [
        source
        for source in required_authoritative_sources
        if authoritative_source_counts.get(source, 0) <= 0
    ]
    missing_execution_modes = [
        mode
        for mode in required_execution_modes
        if execution_mode_counts.get(mode, 0) <= 0
    ]
    required_answer_property_counts = {
        property_id: answer_property_counts.get(property_id, 0)
        for property_id in required_answer_property_ids
    }

    def _signal(
        signal_id: str,
        *,
        status: str,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        definition = signal_definitions.get(signal_id, {})
        return {
            "signal_id": signal_id,
            "dimension": definition.get("dimension")
            or "context_grounded_answering",
            "title": definition.get("title") or signal_id,
            "status": status,
            "details": dict(details),
        }

    benchmark_signals = [
        _signal(
            "context_grounded_benchmark_corpus_present",
            status="pass" if case_count > 0 else "fail",
            details={"case_count": case_count},
        ),
        _signal(
            "required_context_grounding_classes_present",
            status="pass" if not missing_coverage_tags else "fail",
            details={"missing_coverage_tags": missing_coverage_tags},
        ),
        _signal(
            "authoritative_sources_represented",
            status="pass" if not missing_authoritative_sources else "fail",
            details={"missing_authoritative_sources": missing_authoritative_sources},
        ),
        _signal(
            "execution_modes_cover_direct_tool_and_workflow_paths",
            status="pass" if not missing_execution_modes else "fail",
            details={"missing_execution_modes": missing_execution_modes},
        ),
        _signal(
            "answer_properties_and_telemetry_checks_declared",
            status=(
                "pass"
                if case_count > 0
                and all(count > 0 for count in required_answer_property_counts.values())
                and telemetry_check_count >= case_count
                else "fail"
            ),
            details={
                "required_answer_property_counts": required_answer_property_counts,
                "telemetry_check_count": telemetry_check_count,
            },
        ),
        _signal(
            "benchmark_backed_by_exact_path_validation",
            status=(
                "pass"
                if exact_path_case_count == case_count and case_count > 0
                else "fail"
            ),
            details={
                "required_validation_surface_kind": required_validation_surface_kind,
                "matching_validation_surface_case_count": exact_path_case_count,
                "case_count": case_count,
            },
        ),
    ]
    benchmark_signal_summary = {
        "pass_count": sum(1 for signal in benchmark_signals if signal["status"] == "pass"),
        "fail_count": sum(1 for signal in benchmark_signals if signal["status"] == "fail"),
        "not_evaluated_count": sum(
            1 for signal in benchmark_signals if signal["status"] == "not_evaluated"
        ),
        "total_count": len(benchmark_signals),
    }

    capability_gaps: list[dict[str, Any]] = []
    if missing_coverage_tags:
        capability_gaps.append(
            {
                "gap_id": "missing_context_grounding_classes",
                "title": "Required context-grounded answering classes are missing",
                "severity": "high",
                "details": {"missing_coverage_tags": missing_coverage_tags},
            }
        )
    if missing_authoritative_sources:
        capability_gaps.append(
            {
                "gap_id": "missing_authoritative_sources",
                "title": "Required authoritative sources are not represented",
                "severity": "high",
                "details": {"missing_authoritative_sources": missing_authoritative_sources},
            }
        )
    if exact_path_case_count != case_count:
        capability_gaps.append(
            {
                "gap_id": "benchmark_missing_exact_path_validation",
                "title": "Some benchmark cases are not backed by exact-path validation references",
                "severity": "high",
                "details": {
                    "exact_path_case_count": exact_path_case_count,
                    "required_validation_surface_kind": required_validation_surface_kind,
                    "case_count": case_count,
                },
            }
        )

    recommendations: list[dict[str, Any]] = []
    if missing_coverage_tags or missing_authoritative_sources:
        recommendations.append(
            {
                "recommendation_id": "author_missing_context_grounding_cases",
                "priority": "high",
                "summary": "Add benchmark cases for the missing context-grounded answering classes or authoritative sources before relying on the corpus as a regression gate.",
            }
        )
    if exact_path_case_count != case_count:
        recommendations.append(
            {
                "recommendation_id": "add_exact_path_validation_refs",
                "priority": "high",
                "summary": "Back every benchmark case with an exact-path regression or acceptance test reference so the corpus stays executable rather than descriptive only.",
            }
        )

    filters_payload = {
        "case_set": source_info.get("case_set"),
        "max_cases": bounded_max_cases,
        "case_source": source_info.get("source"),
    }
    metrics_payload = {
        "scanned_count": case_count,
        "exact_path_case_count": exact_path_case_count,
        "telemetry_check_count": telemetry_check_count,
        "validation_surface_count": validation_surface_count,
        "coverage_tag_counts": coverage_tag_counts,
        "authoritative_source_counts": authoritative_source_counts,
        "execution_mode_counts": execution_mode_counts,
        "answer_property_counts": answer_property_counts,
        "validation_surface_kind_counts": validation_surface_kind_counts,
        "metric_schema": {
            "benchmark_schema_version": CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SCHEMA_VERSION,
            "required_coverage_tags": list(required_coverage_tags),
            "required_authoritative_sources": list(required_authoritative_sources),
            "required_execution_modes": list(required_execution_modes),
            "required_answer_property_ids": list(required_answer_property_ids),
            "required_validation_surface_kind": required_validation_surface_kind,
        },
    }
    return {
        "collection": "context_grounded_answering_benchmark_cases",
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
        "capability_gaps": capability_gaps,
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
    "CONTEXT_GROUNDED_ANSWERING_BENCHMARK_SCHEMA_VERSION",
    "DEFAULT_CONTEXT_GROUNDED_ANSWERING_BENCHMARK_BUNDLE_PATH",
    "DEFAULT_CONTEXT_GROUNDED_ANSWERING_BENCHMARK_CASE_SET",
    "build_context_grounded_answering_benchmark_report",
    "load_context_grounded_answering_benchmark_cases",
]
