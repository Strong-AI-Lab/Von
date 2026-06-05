"""Operational review loop for Atlas Query Shape Insights reports.

This module compares already-redacted Atlas diagnostics reports, packages
review evidence, and optionally delegates Jira creation/update to the shared
deduplicated Jira gateway. It never creates, drops, or modifies Mongo indexes.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.backend.services.atlas_query_insights_service import (
    AtlasQueryInsightsConfig,
    AtlasQueryInsightsFilters,
    AtlasTypedBlocker,
    assert_report_has_no_known_secrets,
    build_atlas_query_insights_report,
    load_atlas_query_insights_config_from_env,
)
from src.backend.utils.runtime_env import clean_env_value, get_project_root, truthy

_JIRA_KEY = "JVNAUTOSCI-2429"
_PARENT_TELEMETRY_KEY = "JVNAUTOSCI-2427"
_ATLAS_DIAGNOSTICS_KEY = "JVNAUTOSCI-2428"
_REPORT_SCHEMA = "atlas_query_insights_review.v1"
_SEARCH_LABEL = "mongo-query-tuning"
_DEFAULT_PROJECT_KEY = "JVNAUTOSCI"
_DEFAULT_ISSUE_TYPE = "Task"
_MAX_DESCRIPTION_CHARS = 32000
_SENSITIVE_TOKENS = (
    "authorization",
    "access_token",
    "api_token",
    "private_key",
    "client_secret",
    "password",
    "secret-token",
    "client-secret",
)


@dataclass(frozen=True)
class AtlasQueryReviewThresholds:
    high_total_execution_time_ms: float = 60_000.0
    high_examined_returned_ratio: float = 1_000.0
    persistent_min_total_execution_time_ms: float = 10_000.0
    persistent_min_execution_count: int = 100
    high_frequency_execution_count: int = 10_000
    low_latency_ms: float = 20.0
    regression_factor: float = 1.5
    improvement_factor: float = 0.65
    candidate_limit: int = 20


@dataclass(frozen=True)
class AtlasReviewOptions:
    project_key: str = _DEFAULT_PROJECT_KEY
    issue_type: str = _DEFAULT_ISSUE_TYPE
    parent_issue_key: str = _PARENT_TELEMETRY_KEY
    dry_run: bool = True
    approved: bool = False
    assignee_account_id: str | None = None
    request_id: str | None = None
    thresholds: AtlasQueryReviewThresholds = AtlasQueryReviewThresholds()


@dataclass(frozen=True)
class AtlasReviewTypedBlocker(Exception):
    blocker_type: str
    message: str
    details: Mapping[str, Any] | None = None

    def to_report(self) -> dict[str, Any]:
        blocker: dict[str, Any] = {
            "type": self.blocker_type,
            "message": self.message,
        }
        if self.details:
            blocker["details"] = dict(self.details)
        return {
            "schema_version": _REPORT_SCHEMA,
            "status": "blocked",
            "attribution_jira": _JIRA_KEY,
            "parent_jira": _PARENT_TELEMETRY_KEY,
            "atlas_diagnostics_jira": _ATLAS_DIAGNOSTICS_KEY,
            "blocker": blocker,
        }


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalise_text(value: Any, *, max_len: int = 512) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:max_len]


def _normalise_label(value: str) -> str:
    allowed = []
    for char in value.strip().lower().replace("_", "-"):
        if char.isalnum() or char in {"-", "."}:
            allowed.append(char)
        elif char.isspace():
            allowed.append("-")
    label = "".join(allowed).strip("-")
    return label[:255] or "uncategorised"


def _shape_key(row: Mapping[str, Any]) -> str:
    query_hash = _normalise_text(row.get("query_shape_hash"), max_len=160)
    if query_hash:
        return f"hash:{query_hash}"
    basis = "|".join(
        [
            _normalise_text(row.get("namespace"), max_len=256),
            _normalise_text(row.get("command_name"), max_len=96),
            _normalise_text(row.get("query_shape_text_redacted"), max_len=2000),
        ]
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return f"derived:{digest}"


def _rows_by_key(report: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(report, Mapping):
        return {}
    rows = report.get("rows")
    if not isinstance(rows, list):
        return {}
    keyed: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        key = _shape_key(row)
        row["review_shape_key"] = key
        keyed[key] = row
    return keyed


def _impact(row: Mapping[str, Any]) -> float:
    total_ms = float(row.get("total_execution_time_ms") or 0.0)
    execution_count = float(row.get("execution_count") or 0.0)
    p99 = float(row.get("p99_execution_time_ms") or 0.0)
    p90 = float(row.get("p90_execution_time_ms") or 0.0)
    ratio = max(
        float(row.get("docs_examined_per_returned") or 0.0),
        float(row.get("keys_examined_per_returned") or 0.0),
    )
    return round(total_ms + (ratio * max(execution_count, 1.0)) + max(p99, p90), 3)


def _ratio_change(
    current: Mapping[str, Any], previous: Mapping[str, Any]
) -> float | None:
    current_impact = _impact(current)
    previous_impact = _impact(previous)
    if previous_impact <= 0:
        return None
    return round(current_impact / previous_impact, 3)


def diff_atlas_query_reports(
    previous_report: Mapping[str, Any] | None,
    current_report: Mapping[str, Any],
    *,
    thresholds: AtlasQueryReviewThresholds | None = None,
) -> dict[str, Any]:
    """Return a concise new/persistent/resolved/regressed/improved diff."""

    active_thresholds = thresholds or AtlasQueryReviewThresholds()
    previous_rows = _rows_by_key(previous_report)
    current_rows = _rows_by_key(current_report)
    previous_keys = set(previous_rows)
    current_keys = set(current_rows)

    new_keys = current_keys - previous_keys
    resolved_keys = previous_keys - current_keys
    shared_keys = current_keys & previous_keys

    new_rows = [_review_row(current_rows[key], state="new") for key in sorted(new_keys)]
    resolved_rows = [
        _review_row(previous_rows[key], state="resolved")
        for key in sorted(resolved_keys)
    ]
    persistent_rows: list[dict[str, Any]] = []
    regressed_rows: list[dict[str, Any]] = []
    improved_rows: list[dict[str, Any]] = []
    for key in sorted(shared_keys):
        current = current_rows[key]
        previous = previous_rows[key]
        change = _ratio_change(current, previous)
        row = _review_row(current, state="persistent", previous_row=previous)
        row["impact_change_ratio"] = change
        persistent_rows.append(row)
        if change is not None and change >= active_thresholds.regression_factor:
            regressed_rows.append({**row, "state": "regressed"})
        if change is not None and change <= active_thresholds.improvement_factor:
            improved_rows.append({**row, "state": "improved"})

    return {
        "schema_version": "atlas_query_insights_diff.v1",
        "attribution_jira": _JIRA_KEY,
        "parent_jira": _PARENT_TELEMETRY_KEY,
        "counts": {
            "previous": len(previous_rows),
            "current": len(current_rows),
            "new": len(new_rows),
            "persistent": len(persistent_rows),
            "resolved": len(resolved_rows),
            "regressed": len(regressed_rows),
            "improved": len(improved_rows),
        },
        "new": _rank_review_rows(new_rows, limit=active_thresholds.candidate_limit),
        "persistent": _rank_review_rows(
            persistent_rows, limit=active_thresholds.candidate_limit
        ),
        "resolved": _rank_review_rows(
            resolved_rows, limit=active_thresholds.candidate_limit
        ),
        "regressed": _rank_review_rows(
            regressed_rows, limit=active_thresholds.candidate_limit
        ),
        "improved": _rank_review_rows(
            improved_rows, limit=active_thresholds.candidate_limit
        ),
    }


def _rank_review_rows(
    rows: Sequence[Mapping[str, Any]], *, limit: int
) -> list[dict[str, Any]]:
    ranked = [dict(row) for row in rows]
    ranked.sort(key=lambda item: float(item.get("impact_score") or 0.0), reverse=True)
    return ranked[: max(1, int(limit or 1))]


def _review_row(
    row: Mapping[str, Any],
    *,
    state: str,
    previous_row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "review_shape_key": _normalise_text(row.get("review_shape_key"), max_len=220),
        "query_shape_hash": _normalise_text(row.get("query_shape_hash"), max_len=160),
        "namespace": _normalise_text(row.get("namespace"), max_len=256),
        "command_name": _normalise_text(row.get("command_name"), max_len=96),
        "total_execution_time_ms": _safe_float(row.get("total_execution_time_ms")),
        "average_execution_time_ms": _safe_float(row.get("average_execution_time_ms")),
        "execution_count": _safe_int(row.get("execution_count")),
        "docs_examined": _safe_float(row.get("docs_examined")),
        "keys_examined": _safe_float(row.get("keys_examined")),
        "docs_returned": _safe_float(row.get("docs_returned")),
        "docs_examined_per_returned": _safe_float(
            row.get("docs_examined_per_returned")
        ),
        "keys_examined_per_returned": _safe_float(
            row.get("keys_examined_per_returned")
        ),
        "p90_execution_time_ms": _safe_float(row.get("p90_execution_time_ms")),
        "p99_execution_time_ms": _safe_float(row.get("p99_execution_time_ms")),
        "query_shape_text_redacted": _normalise_text(
            row.get("query_shape_text_redacted"), max_len=4000
        ),
        "repo_index_comparison": _normalise_text(
            row.get("repo_index_comparison"), max_len=200
        ),
        "repo_index_evidence_files": [
            _normalise_text(item, max_len=256)
            for item in row.get("repo_index_evidence_files") or []
            if _normalise_text(item)
        ][:12],
        "atlas_suggested_indexes": _summarise_suggested_indexes(
            row.get("atlas_suggested_indexes")
        ),
        "impact_score": _impact(row),
        "previous_impact_score": _impact(previous_row or {}) if previous_row else None,
    }


def _summarise_suggested_indexes(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "suggested_index_count": _safe_int(value.get("suggested_index_count")) or 0,
        "index_names": [
            _normalise_text(item, max_len=160)
            for item in value.get("index_names") or []
            if _normalise_text(item)
        ][:12],
    }


def classify_review_candidate(
    row: Mapping[str, Any],
    *,
    thresholds: AtlasQueryReviewThresholds | None = None,
) -> list[str]:
    """Return evidence categories for operator review."""

    active_thresholds = thresholds or AtlasQueryReviewThresholds()
    categories: list[str] = []
    suggested = row.get("atlas_suggested_indexes")
    suggested_count = 0
    if isinstance(suggested, Mapping):
        suggested_count = int(suggested.get("suggested_index_count") or 0)
    repo_index_status = _normalise_text(row.get("repo_index_comparison"))
    docs_ratio = float(row.get("docs_examined_per_returned") or 0.0)
    keys_ratio = float(row.get("keys_examined_per_returned") or 0.0)
    execution_count = int(row.get("execution_count") or 0)
    avg_ms = float(row.get("average_execution_time_ms") or 0.0)
    total_ms = float(row.get("total_execution_time_ms") or 0.0)
    command = _normalise_text(row.get("command_name")).lower()
    shape_text = _normalise_text(row.get("query_shape_text_redacted"), max_len=4000)

    if (
        suggested_count > 0
        and repo_index_status != "candidate_collection_has_repo_index_definitions"
    ):
        categories.append("missing_index")
    if max(docs_ratio, keys_ratio) >= active_thresholds.high_examined_returned_ratio:
        if repo_index_status == "candidate_collection_has_repo_index_definitions":
            categories.append("likely_query_shape_index_mismatch")
        else:
            categories.append("possible_data_model_issue")
    if "$regex" in shape_text or "$text" in shape_text:
        categories.append("expensive_regex_text_scan")
    if (
        execution_count >= active_thresholds.high_frequency_execution_count
        and 0 < avg_ms <= active_thresholds.low_latency_ms
        and total_ms >= active_thresholds.persistent_min_total_execution_time_ms
    ):
        categories.append("high_frequency_low_latency_hot_path")
    if command == "aggregate" and (
        total_ms >= active_thresholds.persistent_min_total_execution_time_ms
        or max(docs_ratio, keys_ratio) >= active_thresholds.high_examined_returned_ratio
    ):
        categories.append("aggregate_recomputation")
    if not categories:
        categories.append("general_query_tuning_review")
    return categories


def is_high_impact_candidate(
    row: Mapping[str, Any],
    *,
    thresholds: AtlasQueryReviewThresholds | None = None,
) -> bool:
    active_thresholds = thresholds or AtlasQueryReviewThresholds()
    total_ms = float(row.get("total_execution_time_ms") or 0.0)
    execution_count = int(row.get("execution_count") or 0)
    ratio = max(
        float(row.get("docs_examined_per_returned") or 0.0),
        float(row.get("keys_examined_per_returned") or 0.0),
    )
    return (
        total_ms >= active_thresholds.high_total_execution_time_ms
        or ratio >= active_thresholds.high_examined_returned_ratio
        or (
            total_ms >= active_thresholds.persistent_min_total_execution_time_ms
            and execution_count >= active_thresholds.persistent_min_execution_count
        )
    )


def _candidate_source_rows(
    diff_report: Mapping[str, Any],
    *,
    thresholds: AtlasQueryReviewThresholds,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for state in ("regressed", "persistent", "new"):
        for raw in diff_report.get(state) or []:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            row["state"] = state
            if state in {"persistent", "regressed"} and is_high_impact_candidate(
                row, thresholds=thresholds
            ):
                rows.append(row)
            elif state == "new" and is_high_impact_candidate(
                row, thresholds=thresholds
            ):
                rows.append(row)
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        deduped.setdefault(str(row.get("review_shape_key") or _shape_key(row)), row)
    return _rank_review_rows(list(deduped.values()), limit=thresholds.candidate_limit)


def build_jira_task_payloads(
    *,
    diff_report: Mapping[str, Any],
    current_report: Mapping[str, Any],
    options: AtlasReviewOptions | None = None,
) -> list[dict[str, Any]]:
    active_options = options or AtlasReviewOptions()
    rows = _candidate_source_rows(
        diff_report,
        thresholds=active_options.thresholds,
    )
    payloads: list[dict[str, Any]] = []
    for row in rows:
        categories = classify_review_candidate(
            row, thresholds=active_options.thresholds
        )
        fingerprint = _fingerprint(row)
        description = build_jira_description(
            row=row,
            categories=categories,
            current_report=current_report,
            options=active_options,
            fingerprint=fingerprint,
        )
        payloads.append(
            {
                "project_key": active_options.project_key,
                "issue_type": active_options.issue_type,
                "summary": _summary_for_row(row, categories),
                "description": description,
                "fingerprint": fingerprint,
                "search_label": _SEARCH_LABEL,
                "labels": [
                    _SEARCH_LABEL,
                    "atlas-query-insights",
                    "mongo-performance",
                    *[_normalise_label(category) for category in categories],
                ],
                "parent_issue_key": active_options.parent_issue_key,
                "dry_run": active_options.dry_run,
                "approved": active_options.approved,
                "assignee_account_id": active_options.assignee_account_id,
                "existing_comment": _existing_comment_for_row(row, fingerprint),
                "request_id": active_options.request_id
                or f"atlas-query-review:{fingerprint}",
            }
        )
    return payloads


def _summary_for_row(row: Mapping[str, Any], categories: Sequence[str]) -> str:
    namespace = (
        _normalise_text(row.get("namespace"), max_len=120) or "unknown namespace"
    )
    command = _normalise_text(row.get("command_name"), max_len=32) or "query"
    primary = categories[0].replace("_", " ")
    return f"Review Mongo query shape: {namespace} {command} ({primary})"[:255]


def _fingerprint(row: Mapping[str, Any]) -> str:
    basis = "|".join(
        [
            _normalise_text(row.get("review_shape_key"), max_len=220),
            _normalise_text(row.get("namespace"), max_len=256),
            _normalise_text(row.get("command_name"), max_len=96),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _existing_comment_for_row(row: Mapping[str, Any], fingerprint: str) -> str:
    return (
        "Atlas Query Insights review reran and found the same Mongo tuning "
        f"candidate fingerprint `{fingerprint}`.\n\n"
        f"Latest state: `{row.get('state')}`; impact score: `{row.get('impact_score')}`."
    )


def build_jira_description(
    *,
    row: Mapping[str, Any],
    categories: Sequence[str],
    current_report: Mapping[str, Any],
    options: AtlasReviewOptions,
    fingerprint: str,
) -> str:
    filters = (
        current_report.get("filters") if isinstance(current_report, Mapping) else {}
    )
    runtime_config = (
        current_report.get("runtime_config")
        if isinstance(current_report, Mapping)
        else {}
    )
    suggested = row.get("atlas_suggested_indexes")
    repo_files = row.get("repo_index_evidence_files") or []
    shape_text = _normalise_text(row.get("query_shape_text_redacted"), max_len=4000)
    lines = [
        "## Why",
        "",
        "Atlas Query Insights found a persistent or high-impact Mongo query shape that needs operator review. This task is generated from redacted diagnostics only and must not be treated as approval to create/drop indexes directly.",
        "",
        "## Review fingerprint",
        "",
        f"`{fingerprint}`",
        "",
        "## Evidence",
        "",
        f"- Parent telemetry gap: `{options.parent_issue_key}`.",
        f"- Atlas diagnostics surface: `{_ATLAS_DIAGNOSTICS_KEY}`.",
        f"- State in latest comparison: `{row.get('state')}`.",
        f"- Categories: {', '.join(f'`{category}`' for category in categories)}.",
        f"- Cluster: `{_normalise_text(runtime_config.get('cluster_name') if isinstance(runtime_config, Mapping) else '') or 'not recorded'}`.",
        f"- Time window since/until: `{_normalise_text(filters.get('since') if isinstance(filters, Mapping) else '') or 'not recorded'}` / `{_normalise_text(filters.get('until') if isinstance(filters, Mapping) else '') or 'not recorded'}`.",
        f"- Namespace: `{row.get('namespace') or 'unknown'}`.",
        f"- Command: `{row.get('command_name') or 'unknown'}`.",
        f"- Query shape hash: `{row.get('query_shape_hash') or 'not available'}`.",
        f"- Total execution time ms: `{row.get('total_execution_time_ms')}`.",
        f"- Average execution time ms: `{row.get('average_execution_time_ms')}`.",
        f"- Execution count: `{row.get('execution_count')}`.",
        f"- Docs examined / keys examined / docs returned: `{row.get('docs_examined')}` / `{row.get('keys_examined')}` / `{row.get('docs_returned')}`.",
        f"- Docs examined per returned: `{row.get('docs_examined_per_returned')}`.",
        f"- Keys examined per returned: `{row.get('keys_examined_per_returned')}`.",
        f"- P90/P99 execution time ms: `{row.get('p90_execution_time_ms')}` / `{row.get('p99_execution_time_ms')}`.",
        f"- Impact score: `{row.get('impact_score')}`.",
        "",
        "## Index evidence",
        "",
        f"- Atlas suggested indexes: `{_suggested_index_summary(suggested)}`.",
        f"- Repo index comparison: `{row.get('repo_index_comparison') or 'not_evaluated'}`.",
        f"- Repo index evidence files: `{', '.join(repo_files) if repo_files else 'none recorded'}`.",
    ]
    if shape_text:
        lines.extend(
            [
                "",
                "## Redacted query shape",
                "",
                "```json",
                shape_text,
                "```",
            ]
        )
    lines.extend(
        [
            "",
            "## Recommended next diagnostic step",
            "",
            _recommended_next_step(row, categories),
            "",
            "## Safety",
            "",
            "- Do not create or drop indexes from this task without a separate reviewed implementation change.",
            "- Re-run the Atlas review after any index or query change, especially after `JVNAUTOSCI-2426`-style index materialisation, to verify what remains problematic.",
        ]
    )
    description = "\n".join(lines)
    return description[:_MAX_DESCRIPTION_CHARS]


def _suggested_index_summary(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "none recorded"
    count = int(value.get("suggested_index_count") or 0)
    names = [
        _normalise_text(item, max_len=120)
        for item in value.get("index_names") or []
        if _normalise_text(item)
    ]
    if names:
        return f"{count}: {', '.join(names[:6])}"
    return str(count)


def _recommended_next_step(row: Mapping[str, Any], categories: Sequence[str]) -> str:
    if "missing_index" in categories:
        return "Compare the Atlas suggested index with repo-owned index setup, then add a reviewed code/migration change if the index is still justified."
    if "likely_query_shape_index_mismatch" in categories:
        return 'Run a bounded `explain("executionStats")` for the redacted shape and inspect whether the existing repo-owned index order matches the filter/sort shape.'
    if "expensive_regex_text_scan" in categories:
        return "Trace the code path that builds the regex/text query and review whether a search-specific index or query rewrite is appropriate."
    if "high_frequency_low_latency_hot_path" in categories:
        return "Trace the hot path and evaluate batching, caching, or query-count reduction before adding indexes."
    if "aggregate_recomputation" in categories:
        return "Trace the aggregate pipeline and consider precomputation, narrower match stages, or a reviewed compound index."
    if "possible_data_model_issue" in categories:
        return "Review whether the access pattern should be represented differently before treating this as only an index gap."
    return "Run a bounded explain, identify the call path, and compare with repo-owned index definitions."


def _redact_report(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(token in key_text.lower() for token in _SENSITIVE_TOKENS):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = _redact_report(item)
        return redacted
    if isinstance(value, list):
        return [_redact_report(item) for item in value]
    if isinstance(value, str) and any(
        token in value.lower() for token in _SENSITIVE_TOKENS
    ):
        return "<redacted>"
    return value


def assert_review_report_has_no_known_secrets(report: Mapping[str, Any]) -> None:
    rendered = json.dumps(report, sort_keys=True, default=str)
    lowered = rendered.lower()
    if any(token in lowered for token in _SENSITIVE_TOKENS):
        raise RuntimeError("Atlas review report contains a sensitive token marker")


def build_atlas_query_review(
    *,
    previous_report: Mapping[str, Any] | None,
    current_report: Mapping[str, Any],
    options: AtlasReviewOptions | None = None,
) -> dict[str, Any]:
    active_options = options or AtlasReviewOptions()
    redacted_current = _redact_report(current_report)
    redacted_previous = _redact_report(previous_report) if previous_report else None
    diff = diff_atlas_query_reports(
        redacted_previous,
        redacted_current,
        thresholds=active_options.thresholds,
    )
    jira_payloads = build_jira_task_payloads(
        diff_report=diff,
        current_report=redacted_current,
        options=active_options,
    )
    review = {
        "schema_version": _REPORT_SCHEMA,
        "status": "ok",
        "attribution_jira": _JIRA_KEY,
        "parent_jira": active_options.parent_issue_key,
        "atlas_diagnostics_jira": _ATLAS_DIAGNOSTICS_KEY,
        "dry_run": active_options.dry_run,
        "approved": active_options.approved,
        "diff": diff,
        "candidate_count": len(jira_payloads),
        "jira_task_payloads": jira_payloads,
        "jira_results": [],
        "safety": {
            "read_only_diagnostics": True,
            "index_mutation_performed": False,
            "credentials_redacted": True,
        },
    }
    assert_review_report_has_no_known_secrets(review)
    return review


def apply_jira_task_payloads(
    payloads: Sequence[Mapping[str, Any]],
    *,
    options: AtlasReviewOptions | None = None,
) -> list[dict[str, Any]]:
    active_options = options or AtlasReviewOptions()
    if active_options.dry_run or not active_options.approved:
        return [
            {
                "success": True,
                "mode": "dry_run",
                "fingerprint": payload.get("fingerprint"),
                "summary": payload.get("summary"),
            }
            for payload in payloads
        ]
    from src.backend.services.jira_deduplicated_issue_service import (
        upsert_deduplicated_jira_issue,
    )

    results: list[dict[str, Any]] = []
    for payload in payloads:
        result = upsert_deduplicated_jira_issue(
            project_key=str(payload.get("project_key") or active_options.project_key),
            issue_type=str(payload.get("issue_type") or active_options.issue_type),
            summary=str(payload.get("summary") or ""),
            description=str(payload.get("description") or ""),
            fingerprint=str(payload.get("fingerprint") or ""),
            search_label=str(payload.get("search_label") or _SEARCH_LABEL),
            labels=[
                str(item)
                for item in payload.get("labels") or []
                if isinstance(item, str) and item.strip()
            ],
            request_id=str(payload.get("request_id") or ""),
            existing_comment=str(payload.get("existing_comment") or ""),
            assignee_account_id=active_options.assignee_account_id,
            link_issue_key=str(
                payload.get("parent_issue_key") or active_options.parent_issue_key
            ),
            link_type="Relates",
        )
        results.append(dict(result))
    return results


def run_atlas_query_review(
    *,
    previous_report: Mapping[str, Any] | None,
    current_report: Mapping[str, Any],
    options: AtlasReviewOptions | None = None,
    apply_jira: bool = False,
) -> dict[str, Any]:
    review = build_atlas_query_review(
        previous_report=previous_report,
        current_report=current_report,
        options=options,
    )
    if apply_jira:
        review["jira_results"] = apply_jira_task_payloads(
            review.get("jira_task_payloads") or [],
            options=options,
        )
    return review


def read_report_file(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    report_path = Path(path)
    if not report_path.exists():
        return None
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AtlasReviewTypedBlocker(
            "report_file_unreadable",
            f"Could not read report file: {report_path}",
            {"error": type(exc).__name__},
        ) from exc
    if not isinstance(data, Mapping):
        raise AtlasReviewTypedBlocker(
            "report_file_invalid",
            f"Report file did not contain a JSON object: {report_path}",
        )
    return dict(data)


def write_report_file(path: str | Path, report: Mapping[str, Any]) -> None:
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(_redact_report(report), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def default_review_store_dir() -> Path:
    raw = clean_env_value(os.getenv("VON_ATLAS_QUERY_REVIEW_STORE_DIR"))
    if raw:
        return Path(raw)
    return get_project_root() / ".von" / "atlas_query_reviews"


def scheduled_review_enabled() -> bool:
    return truthy(os.getenv("VON_ATLAS_QUERY_REVIEW_SCHEDULE_ENABLED"))


def load_or_fetch_current_report(
    *,
    current_report_path: str | Path | None = None,
    config: AtlasQueryInsightsConfig | None = None,
    filters: AtlasQueryInsightsFilters | None = None,
) -> dict[str, Any]:
    file_report = read_report_file(current_report_path)
    if file_report is not None:
        return file_report
    active_config = config or load_atlas_query_insights_config_from_env()
    active_filters = filters or AtlasQueryInsightsFilters(
        include_query_shapes=True,
        include_shape_text=True,
    )
    report = build_atlas_query_insights_report(active_config, active_filters)
    assert_report_has_no_known_secrets(report)
    return report


def format_review_summary(review: Mapping[str, Any]) -> str:
    if review.get("status") == "blocked":
        blocker = (
            review.get("blocker") if isinstance(review.get("blocker"), Mapping) else {}
        )
        return f"blocked\t{blocker.get('type')}\t{blocker.get('message')}"
    diff = review.get("diff") if isinstance(review.get("diff"), Mapping) else {}
    counts = diff.get("counts") if isinstance(diff.get("counts"), Mapping) else {}
    lines = [
        "status\tnew\tpersistent\tresolved\tregressed\timproved\tcandidates",
        "\t".join(
            [
                str(review.get("status") or ""),
                str(counts.get("new") or 0),
                str(counts.get("persistent") or 0),
                str(counts.get("resolved") or 0),
                str(counts.get("regressed") or 0),
                str(counts.get("improved") or 0),
                str(review.get("candidate_count") or 0),
            ]
        ),
    ]
    return "\n".join(lines)


__all__ = [
    "AtlasQueryReviewThresholds",
    "AtlasReviewOptions",
    "AtlasReviewTypedBlocker",
    "apply_jira_task_payloads",
    "build_atlas_query_review",
    "build_jira_description",
    "build_jira_task_payloads",
    "classify_review_candidate",
    "default_review_store_dir",
    "diff_atlas_query_reports",
    "format_review_summary",
    "is_high_impact_candidate",
    "load_or_fetch_current_report",
    "read_report_file",
    "run_atlas_query_review",
    "scheduled_review_enabled",
    "write_report_file",
]
