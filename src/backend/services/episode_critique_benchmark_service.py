"""Benchmark and sampled meta-audit helpers for episode critique memories."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pymongo import ASCENDING

from .episode_critic_evidence_service import build_episode_critic_evidence_bundle
from .episode_critique_memory_service import (
    get_episode_critique_memories_collection,
    get_episode_critique_memory_state,
)

logger = logging.getLogger(__name__)

EPISODE_CRITIQUE_BENCHMARK_SCHEMA_VERSION = "episode_critique_benchmark_report.v1"

_AUDIT_BUCKET_PRIORITY = {
    "false_positive_task_proxy": 0,
    "false_negative_task_proxy": 1,
    "post_remediation_recurrence": 2,
    "inconclusive_actionable": 3,
    "high_repeat_actionable": 4,
}


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalise_strings(values: Any, *, limit: int = 60) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _json_fingerprint(value: Any, *, length: int = 16) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _rate_pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def _rate_pct_or_none(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return _rate_pct(numerator, denominator)


def _build_query(
    *,
    namespace: str,
    workflow_id: str | None,
    request_id: str | None,
    episode_id: str | None,
    verdict: str | None,
    verdicts: Sequence[str] | None,
    from_utc: str | None,
    to_utc: str | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {"namespace": namespace}

    resolved_workflow_id = _safe_str(workflow_id)
    if resolved_workflow_id:
        query["workflow_id"] = resolved_workflow_id

    resolved_request_id = _safe_str(request_id)
    if resolved_request_id:
        query["request_id"] = resolved_request_id

    resolved_episode_id = _safe_str(episode_id)
    if resolved_episode_id:
        query["episode_id"] = resolved_episode_id

    verdict_values = [
        *_normalise_strings(verdict, limit=10),
        *_normalise_strings(verdicts, limit=20),
    ]
    if verdict_values:
        deduped = list(dict.fromkeys(verdict_values))
        query["verdict"] = deduped[0] if len(deduped) == 1 else {"$in": deduped}

    created_range: dict[str, str] = {}
    resolved_from_utc = _safe_str(from_utc)
    if resolved_from_utc:
        created_range["$gte"] = resolved_from_utc
    resolved_to_utc = _safe_str(to_utc)
    if resolved_to_utc:
        created_range["$lte"] = resolved_to_utc
    if created_range:
        query["created_at_utc"] = created_range

    return query


def _normalise_projection_doc(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "memory_id": _safe_str(doc.get("memory_id")),
        "request_id": _safe_str(doc.get("request_id")),
        "episode_id": _safe_str(doc.get("episode_id")),
        "workflow_id": _safe_str(doc.get("workflow_id")),
        "session_id": _safe_str(doc.get("session_id")),
        "namespace": _safe_str(doc.get("namespace")),
        "created_at_utc": _safe_str(doc.get("created_at_utc")),
        "updated_at_utc": _safe_str(doc.get("updated_at_utc")),
        "verdict": _safe_str(doc.get("verdict")) or "inconclusive",
        "critic_summary_text": _safe_str(doc.get("critic_summary_text")),
        "confidence": _safe_float(doc.get("confidence")),
        "unresolved_check_count": _safe_int(doc.get("unresolved_check_count")),
        "implicated_workflow_ids": _normalise_strings(
            doc.get("implicated_workflow_ids"),
            limit=20,
        ),
        "implicated_tool_names": _normalise_strings(
            doc.get("implicated_tool_names"),
            limit=40,
        ),
        "implicated_concept_ids": _normalise_strings(
            doc.get("implicated_concept_ids"),
            limit=80,
        ),
        "routing_decision": _safe_str(doc.get("routing_decision")) or "memory_only",
        "routing_reason_codes": _normalise_strings(
            doc.get("routing_reason_codes"),
            limit=20,
        ),
        "routing_fingerprint": _safe_str(doc.get("routing_fingerprint")),
        "routing_repeat_count": _safe_int(doc.get("routing_repeat_count")),
        "routing_task_action": _safe_str(doc.get("routing_task_action")),
        "routing_jira_action": _safe_str(doc.get("routing_jira_action")),
        "remediation_task_ids": _normalise_strings(
            doc.get("remediation_task_ids"),
            limit=20,
        ),
        "remediation_issue_keys": _normalise_strings(
            doc.get("remediation_issue_keys"),
            limit=20,
        ),
        "recommendations": _normalise_strings(doc.get("recommendations"), limit=8),
        "improvement_suggestion_count": _safe_int(doc.get("improvement_suggestion_count")),
        "improvement_suggestion_categories": _normalise_strings(
            doc.get("improvement_suggestion_categories"),
            limit=20,
        ),
        "improvement_target_workflow_ids": _normalise_strings(
            doc.get("improvement_target_workflow_ids"),
            limit=20,
        ),
        "improvement_target_tool_names": _normalise_strings(
            doc.get("improvement_target_tool_names"),
            limit=20,
        ),
        "receipt_hash": _safe_str(doc.get("receipt_hash")),
        "dedupe_fingerprint": _safe_str(doc.get("dedupe_fingerprint")),
    }


def _fetch_projection_rows(
    *,
    query: Mapping[str, Any],
    scan_limit: int,
) -> list[dict[str, Any]]:
    coll = get_episode_critique_memories_collection()
    if coll is None:
        return []

    projection = {
        "_id": 0,
        "memory_id": 1,
        "request_id": 1,
        "episode_id": 1,
        "workflow_id": 1,
        "session_id": 1,
        "namespace": 1,
        "created_at_utc": 1,
        "updated_at_utc": 1,
        "verdict": 1,
        "critic_summary_text": 1,
        "confidence": 1,
        "unresolved_check_count": 1,
        "implicated_workflow_ids": 1,
        "implicated_tool_names": 1,
        "implicated_concept_ids": 1,
        "routing_decision": 1,
        "routing_reason_codes": 1,
        "routing_fingerprint": 1,
        "routing_repeat_count": 1,
        "routing_task_action": 1,
        "routing_jira_action": 1,
        "remediation_task_ids": 1,
        "remediation_issue_keys": 1,
        "recommendations": 1,
        "improvement_suggestion_count": 1,
        "improvement_suggestion_categories": 1,
        "improvement_target_workflow_ids": 1,
        "improvement_target_tool_names": 1,
        "receipt_hash": 1,
        "dedupe_fingerprint": 1,
    }

    cursor = coll.find(dict(query), projection)
    sort_method = getattr(cursor, "sort", None)
    if callable(sort_method):
        cursor = sort_method("created_at_utc", ASCENDING)
    limit_method = getattr(cursor, "limit", None)
    if callable(limit_method):
        cursor = limit_method(scan_limit)

    if not isinstance(cursor, Iterable):
        return []

    rows: list[dict[str, Any]] = []
    for raw in cursor:
        if not isinstance(raw, Mapping):
            continue
        rows.append(_normalise_projection_doc(raw))

    rows.sort(
        key=lambda row: (
            str(row.get("created_at_utc") or ""),
            str(row.get("memory_id") or ""),
        )
    )
    return rows[:scan_limit]


def _has_remediation(row: Mapping[str, Any]) -> bool:
    return bool(row.get("remediation_task_ids")) or bool(row.get("remediation_issue_keys"))


def _row_improvement_suggestion_count(row: Mapping[str, Any]) -> int:
    explicit_count = _safe_int(row.get("improvement_suggestion_count"))
    if explicit_count > 0:
        return explicit_count
    category_count = len(_normalise_strings(row.get("improvement_suggestion_categories")))
    workflow_target_count = len(
        _normalise_strings(row.get("improvement_target_workflow_ids"))
    )
    tool_target_count = len(_normalise_strings(row.get("improvement_target_tool_names")))
    return max(category_count, workflow_target_count, tool_target_count)


def _has_improvement_suggestions(row: Mapping[str, Any]) -> bool:
    return _row_improvement_suggestion_count(row) > 0


def _has_useful_improvement_suggestions(row: Mapping[str, Any]) -> bool:
    if not _has_improvement_suggestions(row):
        return False

    implicated_workflow_ids = set(
        _normalise_strings(row.get("implicated_workflow_ids"), limit=40)
    )
    subject_workflow_id = _safe_str(row.get("workflow_id"))
    if subject_workflow_id:
        implicated_workflow_ids.add(subject_workflow_id)

    targeted_workflow_ids = set(
        _normalise_strings(row.get("improvement_target_workflow_ids"), limit=40)
    )
    if implicated_workflow_ids.intersection(targeted_workflow_ids):
        return True

    implicated_tool_names = {
        item.lower()
        for item in _normalise_strings(row.get("implicated_tool_names"), limit=40)
    }
    targeted_tool_names = {
        item.lower()
        for item in _normalise_strings(row.get("improvement_target_tool_names"), limit=40)
    }
    return bool(implicated_tool_names.intersection(targeted_tool_names))


def _is_likely_actionable(row: Mapping[str, Any]) -> bool:
    verdict = str(row.get("verdict") or "").strip().lower()
    unresolved = _safe_int(row.get("unresolved_check_count"))
    repeat_count = _safe_int(row.get("routing_repeat_count"))
    return verdict in {"fail", "follow_up_required"} or unresolved > 0 or repeat_count > 0


def _is_strong_actionable(row: Mapping[str, Any]) -> bool:
    verdict = str(row.get("verdict") or "").strip().lower()
    unresolved = _safe_int(row.get("unresolved_check_count"))
    repeat_count = _safe_int(row.get("routing_repeat_count"))
    return verdict in {"fail", "follow_up_required"} or unresolved >= 2 or repeat_count >= 1


def _is_false_positive_task_proxy(row: Mapping[str, Any]) -> bool:
    verdict = str(row.get("verdict") or "").strip().lower()
    return (
        _has_remediation(row)
        and not _is_strong_actionable(row)
        and verdict in {"pass", "inconclusive"}
        and _safe_int(row.get("routing_repeat_count")) <= 0
    )


def _is_false_negative_task_proxy(row: Mapping[str, Any]) -> bool:
    return _is_strong_actionable(row) and not _has_remediation(row)


def _count_by_string(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = _safe_str(row.get(field)) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _analyse_recurrence(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], set[str]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    missing_fingerprint_count = 0
    for row in rows:
        fingerprint = _safe_str(row.get("routing_fingerprint"))
        if not fingerprint:
            missing_fingerprint_count += 1
            continue
        grouped.setdefault(fingerprint, []).append(row)

    repeated_fingerprint_count = 0
    remediated_fingerprint_count = 0
    post_remediation_recurrence_fingerprint_count = 0
    post_remediation_recurrence_case_count = 0
    post_remediation_memory_ids: set[str] = set()

    for group in grouped.values():
        ordered = sorted(
            group,
            key=lambda row: (
                str(row.get("created_at_utc") or ""),
                str(row.get("memory_id") or ""),
            ),
        )
        if len(ordered) > 1:
            repeated_fingerprint_count += 1
        first_remediated_index = next(
            (index for index, row in enumerate(ordered) if _has_remediation(row)),
            None,
        )
        if first_remediated_index is None:
            continue
        remediated_fingerprint_count += 1
        later_rows = ordered[first_remediated_index + 1 :]
        if not later_rows:
            continue
        post_remediation_recurrence_fingerprint_count += 1
        post_remediation_recurrence_case_count += len(later_rows)
        for row in later_rows:
            memory_id = _safe_str(row.get("memory_id"))
            if memory_id:
                post_remediation_memory_ids.add(memory_id)

    post_remediation_recurrence_rate_pct = _rate_pct_or_none(
        post_remediation_recurrence_fingerprint_count,
        remediated_fingerprint_count,
    )
    recurrence_free_after_remediation_rate_pct = _rate_pct_or_none(
        remediated_fingerprint_count - post_remediation_recurrence_fingerprint_count,
        remediated_fingerprint_count,
    )

    metrics = {
        "unique_routing_fingerprint_count": len(grouped),
        "missing_routing_fingerprint_count": missing_fingerprint_count,
        "repeated_routing_fingerprint_count": repeated_fingerprint_count,
        "remediated_fingerprint_count": remediated_fingerprint_count,
        "post_remediation_recurrence_fingerprint_count": (
            post_remediation_recurrence_fingerprint_count
        ),
        "post_remediation_recurrence_case_count": post_remediation_recurrence_case_count,
        "post_remediation_recurrence_rate_pct": post_remediation_recurrence_rate_pct,
        "recurrence_free_after_remediation_rate_pct": (
            recurrence_free_after_remediation_rate_pct
        ),
    }
    return metrics, post_remediation_memory_ids


def _build_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    audit_case_count: int = 0,
    audit_bucket_counts: Mapping[str, int] | None = None,
    audit_bundle_ready_count: int = 0,
    audit_bundle_fail_closed_count: int = 0,
) -> dict[str, Any]:
    scanned_count = len(rows)
    verdict_counts = _count_by_string(rows, "verdict")
    verdict_rates_pct = {
        verdict: _rate_pct(count, scanned_count) for verdict, count in verdict_counts.items()
    }
    workflow_counts = _count_by_string(rows, "workflow_id")
    routing_decision_counts = _count_by_string(rows, "routing_decision")

    actionable_rows = [row for row in rows if _is_likely_actionable(row)]
    strong_actionable_rows = [row for row in rows if _is_strong_actionable(row)]
    remediated_rows = [row for row in rows if _has_remediation(row)]
    task_linked_episode_count = sum(
        1 for row in rows if bool(row.get("remediation_task_ids"))
    )
    jira_linked_episode_count = sum(
        1 for row in rows if bool(row.get("remediation_issue_keys"))
    )
    false_positive_rows = [row for row in rows if _is_false_positive_task_proxy(row)]
    false_negative_rows = [row for row in rows if _is_false_negative_task_proxy(row)]
    useful_task_creation_count = max(0, len(remediated_rows) - len(false_positive_rows))
    strong_actionable_remediated_count = sum(
        1 for row in strong_actionable_rows if _has_remediation(row)
    )
    suggestion_rows = [row for row in rows if _has_improvement_suggestions(row)]
    actionable_suggestion_rows = [
        row for row in actionable_rows if _has_improvement_suggestions(row)
    ]
    strong_actionable_suggestion_rows = [
        row for row in strong_actionable_rows if _has_improvement_suggestions(row)
    ]
    strong_actionable_useful_suggestion_rows = [
        row
        for row in strong_actionable_suggestion_rows
        if _has_useful_improvement_suggestions(row)
    ]
    workflow_targeted_suggestion_rows = [
        row
        for row in suggestion_rows
        if bool(_normalise_strings(row.get("improvement_target_workflow_ids"), limit=40))
    ]
    tool_targeted_suggestion_rows = [
        row
        for row in suggestion_rows
        if bool(_normalise_strings(row.get("improvement_target_tool_names"), limit=40))
    ]

    recurrence_metrics, _ = _analyse_recurrence(rows)

    return {
        "scanned_count": scanned_count,
        "verdict_counts": verdict_counts,
        "verdict_rates_pct": verdict_rates_pct,
        "workflow_counts": workflow_counts,
        "routing_decision_counts": routing_decision_counts,
        "actionability_metrics": {
            "actionable_episode_count": len(actionable_rows),
            "actionable_episode_rate_pct": _rate_pct(len(actionable_rows), scanned_count),
            "strong_actionable_episode_count": len(strong_actionable_rows),
            "strong_actionable_episode_rate_pct": _rate_pct(
                len(strong_actionable_rows),
                scanned_count,
            ),
        },
        "remediation_metrics": {
            "remediated_episode_count": len(remediated_rows),
            "remediated_episode_rate_pct": _rate_pct(len(remediated_rows), scanned_count),
            "task_linked_episode_count": task_linked_episode_count,
            "jira_linked_episode_count": jira_linked_episode_count,
            "false_positive_task_proxy_count": len(false_positive_rows),
            "false_positive_task_proxy_rate_pct": _rate_pct_or_none(
                len(false_positive_rows),
                len(remediated_rows),
            ),
            "false_negative_task_proxy_count": len(false_negative_rows),
            "false_negative_task_proxy_rate_pct": _rate_pct_or_none(
                len(false_negative_rows),
                len(strong_actionable_rows),
            ),
            "task_creation_usefulness_proxy_pct": _rate_pct_or_none(
                useful_task_creation_count,
                len(remediated_rows),
            ),
            "task_creation_precision_proxy_pct": _rate_pct_or_none(
                useful_task_creation_count,
                len(remediated_rows),
            ),
            "task_creation_recall_proxy_pct": _rate_pct_or_none(
                strong_actionable_remediated_count,
                len(strong_actionable_rows),
            ),
        },
        "improvement_suggestion_metrics": {
            "episode_with_suggestions_count": len(suggestion_rows),
            "episode_with_suggestions_rate_pct": _rate_pct(
                len(suggestion_rows),
                scanned_count,
            ),
            "actionable_episode_with_suggestions_count": len(actionable_suggestion_rows),
            "actionable_episode_with_suggestions_rate_pct": _rate_pct_or_none(
                len(actionable_suggestion_rows),
                len(actionable_rows),
            ),
            "strong_actionable_episode_with_suggestions_count": len(
                strong_actionable_suggestion_rows
            ),
            "strong_actionable_episode_with_suggestions_rate_pct": _rate_pct_or_none(
                len(strong_actionable_suggestion_rows),
                len(strong_actionable_rows),
            ),
            "strong_actionable_episode_missing_suggestions_count": max(
                0,
                len(strong_actionable_rows) - len(strong_actionable_suggestion_rows),
            ),
            "strong_actionable_episode_missing_suggestions_rate_pct": _rate_pct_or_none(
                max(
                    0,
                    len(strong_actionable_rows) - len(strong_actionable_suggestion_rows),
                ),
                len(strong_actionable_rows),
            ),
            "strong_actionable_episode_with_useful_suggestions_count": len(
                strong_actionable_useful_suggestion_rows
            ),
            "strong_actionable_episode_with_useful_suggestions_rate_pct": _rate_pct_or_none(
                len(strong_actionable_useful_suggestion_rows),
                len(strong_actionable_rows),
            ),
            "suggestion_usefulness_proxy_pct": _rate_pct_or_none(
                len(strong_actionable_useful_suggestion_rows),
                len(strong_actionable_suggestion_rows),
            ),
            "workflow_targeted_suggestion_episode_count": len(
                workflow_targeted_suggestion_rows
            ),
            "tool_targeted_suggestion_episode_count": len(tool_targeted_suggestion_rows),
        },
        "recurrence_metrics": recurrence_metrics,
        "audit_metrics": {
            "sampled_case_count": audit_case_count,
            "sampled_bucket_counts": dict(audit_bucket_counts or {}),
            "sampled_bundle_ready_count": audit_bundle_ready_count,
            "sampled_bundle_fail_closed_count": audit_bundle_fail_closed_count,
        },
    }


def _build_capability_gaps(
    rows: Sequence[Mapping[str, Any]],
    *,
    metrics: Mapping[str, Any],
    sampled_bundle_fail_closed_count: int,
    sampled_bundle_failure_memory_ids: Sequence[str],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    if not rows:
        return [
            {
                "gap_id": "no_episode_critique_memories",
                "title": "No episode critique memories found",
                "evidence_count": 0,
                "severity": "high",
                "description": (
                    "The actor/critic benchmark could not run because no episode_critique_memories "
                    "matched the selected namespace and filters."
                ),
            }
        ]

    missing_request_id_count = sum(
        1
        for row in rows
        if not _safe_str(row.get("request_id")) and not _safe_str(row.get("episode_id"))
    )
    missing_routing_fingerprint_count = sum(
        1 for row in rows if not _safe_str(row.get("routing_fingerprint"))
    )
    if missing_request_id_count > 0:
        gaps.append(
            {
                "gap_id": "missing_request_id",
                "title": "Episode critique memories missing request identity",
                "evidence_count": missing_request_id_count,
                "severity": "medium",
                "description": (
                    "Some critique memories cannot be cleanly replayed into fresh evidence bundles "
                    "because request_id or episode identity is absent."
                ),
            }
        )
    if missing_routing_fingerprint_count > 0:
        gaps.append(
            {
                "gap_id": "missing_routing_fingerprint",
                "title": "Episode critique memories missing routing fingerprint",
                "evidence_count": missing_routing_fingerprint_count,
                "severity": "medium",
                "description": (
                    "Routing fingerprints are required for recurrence and remediation-usefulness metrics."
                ),
            }
        )
    if sampled_bundle_fail_closed_count > 0:
        gaps.append(
            {
                "gap_id": "sampled_meta_audit_bundle_fail_closed",
                "title": "Sampled meta-audit cases could not rebuild full evidence",
                "evidence_count": sampled_bundle_fail_closed_count,
                "severity": "high",
                "description": (
                    "At least one sampled critique case failed closed when rebuilding a fresh "
                    "episode evidence bundle for audit."
                ),
                "memory_ids": list(sampled_bundle_failure_memory_ids)[:20],
            }
        )
    improvement_metrics = _mapping_or_empty(metrics.get("improvement_suggestion_metrics"))
    missing_suggestions_count = _safe_int(
        improvement_metrics.get("strong_actionable_episode_missing_suggestions_count")
    )
    if missing_suggestions_count > 0:
        gaps.append(
            {
                "gap_id": "missing_improvement_suggestions_for_actionable_episodes",
                "title": "Strong actionable critique episodes are missing improvement suggestions",
                "evidence_count": missing_suggestions_count,
                "severity": "medium",
                "description": (
                    "At least one strong actionable critique episode did not produce bounded workflow/tool "
                    "improvement suggestions, which weakens the remediation handoff from episode evaluation."
                ),
            }
        )
    strong_with_suggestions_count = _safe_int(
        improvement_metrics.get("strong_actionable_episode_with_suggestions_count")
    )
    strong_with_useful_suggestions_count = _safe_int(
        improvement_metrics.get("strong_actionable_episode_with_useful_suggestions_count")
    )
    if strong_with_suggestions_count > strong_with_useful_suggestions_count:
        gaps.append(
            {
                "gap_id": "improvement_suggestions_not_targeted_to_actionable_surface",
                "title": "Some improvement suggestions are not clearly targeted to the actionable workflow/tool surface",
                "evidence_count": (
                    strong_with_suggestions_count - strong_with_useful_suggestions_count
                ),
                "severity": "medium",
                "description": (
                    "The benchmark found strong actionable episodes with suggestions present, but at least one "
                    "suggestion was not aligned to the implicated workflow or tool surface."
                ),
            }
        )
    return gaps


def _build_recommendations(
    *,
    rows: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    capability_gaps: Sequence[Mapping[str, Any]],
) -> list[str]:
    if not rows:
        return [
            "Run or backfill episode evaluation first so actor/critic benchmark data exists.",
        ]

    remediation_metrics = _mapping_or_empty(metrics.get("remediation_metrics"))
    recurrence_metrics = _mapping_or_empty(metrics.get("recurrence_metrics"))
    audit_metrics = _mapping_or_empty(metrics.get("audit_metrics"))
    improvement_metrics = _mapping_or_empty(metrics.get("improvement_suggestion_metrics"))

    recommendations: list[str] = []
    if _safe_int(remediation_metrics.get("false_negative_task_proxy_count")) > 0:
        recommendations.append(
            "Review strong actionable critique episodes that did not create remediation work; task-routing recall looks weak."
        )
    if _safe_int(remediation_metrics.get("false_positive_task_proxy_count")) > 0:
        recommendations.append(
            "Tighten remediation creation thresholds for one-off low-signal findings to reduce likely false positives."
        )
    if _safe_int(recurrence_metrics.get("post_remediation_recurrence_case_count")) > 0:
        recommendations.append(
            "Inspect recurring routing fingerprints after remediation; the created tasks or Jira issues are not yet reducing recurrence."
        )
    if _safe_int(audit_metrics.get("sampled_bundle_fail_closed_count")) > 0:
        recommendations.append(
            "Improve episode evidence bundle completeness before relying on sampled meta-audit verdicts."
        )
    if (
        _safe_int(improvement_metrics.get("strong_actionable_episode_missing_suggestions_count"))
        > 0
    ):
        recommendations.append(
            "Ensure strong actionable critique episodes produce bounded workflow/tool improvement suggestions, not only verdict text."
        )
    strong_with_suggestions_count = _safe_int(
        improvement_metrics.get("strong_actionable_episode_with_suggestions_count")
    )
    strong_with_useful_suggestions_count = _safe_int(
        improvement_metrics.get("strong_actionable_episode_with_useful_suggestions_count")
    )
    if strong_with_suggestions_count > strong_with_useful_suggestions_count:
        recommendations.append(
            "Tighten improvement-suggestion targeting so suggested workflow/tool changes align with the implicated actionable surface."
        )
    if not recommendations and capability_gaps:
        recommendations.append(
            "Address the reported capability gaps before interpreting benchmark results as stable."
        )
    if not recommendations:
        recommendations.append(
            "Actor/critic metrics are within bounded expectations for the selected window; keep tracking regression deltas over time."
        )

    deduped: list[str] = []
    seen: set[str] = set()
    for item in recommendations:
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(item)
    return deduped[:8]


def _build_regression_assessment(
    *,
    metrics: Mapping[str, Any],
    baseline_precision_proxy_pct: Any,
    baseline_recall_proxy_pct: Any,
    baseline_post_remediation_recurrence_rate_pct: Any,
    regression_tolerance_pct: Any,
) -> dict[str, Any]:
    tolerance = _safe_float(regression_tolerance_pct)
    if tolerance is None or tolerance < 0:
        tolerance = 0.0

    remediation_metrics = _mapping_or_empty(metrics.get("remediation_metrics"))
    recurrence_metrics = _mapping_or_empty(metrics.get("recurrence_metrics"))

    comparisons: list[dict[str, Any]] = []
    regression_detected = False

    def _add_comparison(
        *,
        metric_name: str,
        current_value: Any,
        baseline_value_raw: Any,
        higher_is_better: bool,
    ) -> None:
        nonlocal regression_detected
        current = _safe_float(current_value)
        baseline = _safe_float(baseline_value_raw)
        if current is None or baseline is None:
            return
        delta_pct = round(current - baseline, 2)
        regressed = (
            current < (baseline - tolerance)
            if higher_is_better
            else current > (baseline + tolerance)
        )
        if regressed:
            regression_detected = True
        comparisons.append(
            {
                "metric": metric_name,
                "baseline_pct": round(baseline, 2),
                "current_pct": round(current, 2),
                "delta_pct": delta_pct,
                "regressed": regressed,
                "higher_is_better": higher_is_better,
            }
        )

    _add_comparison(
        metric_name="task_creation_precision_proxy_pct",
        current_value=remediation_metrics.get("task_creation_precision_proxy_pct"),
        baseline_value_raw=baseline_precision_proxy_pct,
        higher_is_better=True,
    )
    _add_comparison(
        metric_name="task_creation_recall_proxy_pct",
        current_value=remediation_metrics.get("task_creation_recall_proxy_pct"),
        baseline_value_raw=baseline_recall_proxy_pct,
        higher_is_better=True,
    )
    _add_comparison(
        metric_name="post_remediation_recurrence_rate_pct",
        current_value=recurrence_metrics.get("post_remediation_recurrence_rate_pct"),
        baseline_value_raw=baseline_post_remediation_recurrence_rate_pct,
        higher_is_better=False,
    )

    return {
        "baseline_provided": len(comparisons) > 0,
        "regression_tolerance_pct": round(tolerance, 2),
        "regression_detected": regression_detected,
        "comparisons": comparisons,
    }


def _build_benchmark_signals(
    *,
    metrics: Mapping[str, Any],
    regression_assessment: Mapping[str, Any],
    max_audit_cases: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    def _signal_status(passed: bool | None) -> str:
        if passed is True:
            return "pass"
        if passed is False:
            return "fail"
        return "not_evaluated"

    def _add_signal(
        *,
        signal_id: str,
        dimension: str,
        title: str,
        passed: bool | None,
        details: Mapping[str, Any],
    ) -> None:
        signals.append(
            {
                "signal_id": signal_id,
                "dimension": dimension,
                "title": title,
                "status": _signal_status(passed),
                "passed": passed,
                "details": dict(details),
            }
        )

    remediation_metrics = _mapping_or_empty(metrics.get("remediation_metrics"))
    recurrence_metrics = _mapping_or_empty(metrics.get("recurrence_metrics"))
    audit_metrics = _mapping_or_empty(metrics.get("audit_metrics"))
    improvement_metrics = _mapping_or_empty(metrics.get("improvement_suggestion_metrics"))

    signals: list[dict[str, Any]] = []
    false_positive_count = _safe_int(remediation_metrics.get("false_positive_task_proxy_count"))
    false_negative_count = _safe_int(remediation_metrics.get("false_negative_task_proxy_count"))
    sampled_case_count = _safe_int(audit_metrics.get("sampled_case_count"))
    sampled_fail_closed_count = _safe_int(
        audit_metrics.get("sampled_bundle_fail_closed_count")
    )
    sampled_bundle_ready_count = _safe_int(audit_metrics.get("sampled_bundle_ready_count"))

    _add_signal(
        signal_id="low_signal_remediation_suppressed",
        dimension="task_quality",
        title="Low-signal critique episodes do not create likely false-positive remediation",
        passed=false_positive_count == 0,
        details={"false_positive_task_proxy_count": false_positive_count},
    )
    _add_signal(
        signal_id="strong_actionable_findings_receive_remediation",
        dimension="task_quality",
        title="Strong actionable critique episodes are routed into remediation work",
        passed=false_negative_count == 0,
        details={"false_negative_task_proxy_count": false_negative_count},
    )
    readiness_passed: bool | None
    if sampled_case_count <= 0:
        readiness_passed = None
    else:
        readiness_passed = sampled_fail_closed_count == 0
    _add_signal(
        signal_id="sampled_meta_audit_bundle_readiness",
        dimension="meta_audit",
        title="Sampled meta-audit cases rebuild fresh evidence bundles",
        passed=readiness_passed,
        details={
            "sampled_case_count": sampled_case_count,
            "sampled_bundle_ready_count": sampled_bundle_ready_count,
            "sampled_bundle_fail_closed_count": sampled_fail_closed_count,
        },
    )
    _add_signal(
        signal_id="sampled_meta_audit_is_bounded",
        dimension="meta_audit",
        title="Meta-audit sampling stays bounded and non-recursive",
        passed=sampled_case_count <= max_audit_cases,
        details={
            "sampled_case_count": sampled_case_count,
            "max_audit_cases": max_audit_cases,
            "mode": "on_demand_sample",
            "non_recursive": True,
        },
    )
    strong_actionable_episode_count = _safe_int(
        _mapping_or_empty(metrics.get("actionability_metrics")).get(
            "strong_actionable_episode_count"
        )
    )
    strong_actionable_with_suggestions_count = _safe_int(
        improvement_metrics.get("strong_actionable_episode_with_suggestions_count")
    )
    strong_actionable_with_useful_suggestions_count = _safe_int(
        improvement_metrics.get("strong_actionable_episode_with_useful_suggestions_count")
    )
    improvement_presence_passed: bool | None
    if strong_actionable_episode_count <= 0:
        improvement_presence_passed = None
    else:
        improvement_presence_passed = (
            strong_actionable_with_suggestions_count >= strong_actionable_episode_count
        )
    _add_signal(
        signal_id="strong_actionable_episodes_receive_improvement_suggestions",
        dimension="improvement_suggestions",
        title="Strong actionable critique episodes produce improvement suggestions",
        passed=improvement_presence_passed,
        details={
            "strong_actionable_episode_count": strong_actionable_episode_count,
            "strong_actionable_episode_with_suggestions_count": (
                strong_actionable_with_suggestions_count
            ),
        },
    )
    improvement_usefulness_passed: bool | None
    if strong_actionable_with_suggestions_count <= 0:
        improvement_usefulness_passed = None
    else:
        improvement_usefulness_passed = (
            strong_actionable_with_useful_suggestions_count
            >= strong_actionable_with_suggestions_count
        )
    _add_signal(
        signal_id="improvement_suggestions_target_actionable_surface",
        dimension="improvement_suggestions",
        title="Improvement suggestions align with the implicated actionable workflow/tool surface",
        passed=improvement_usefulness_passed,
        details={
            "strong_actionable_episode_with_suggestions_count": (
                strong_actionable_with_suggestions_count
            ),
            "strong_actionable_episode_with_useful_suggestions_count": (
                strong_actionable_with_useful_suggestions_count
            ),
            "suggestion_usefulness_proxy_pct": improvement_metrics.get(
                "suggestion_usefulness_proxy_pct"
            ),
        },
    )

    baseline_provided = bool(regression_assessment.get("baseline_provided"))
    if baseline_provided:
        _add_signal(
            signal_id="actor_critic_regression_guard",
            dimension="benchmark",
            title="Actor/critic benchmark metrics stay within baseline tolerance",
            passed=not bool(regression_assessment.get("regression_detected")),
            details={"comparisons": regression_assessment.get("comparisons") or []},
        )

    post_remediation_recurrence_rate_pct = _safe_float(
        recurrence_metrics.get("post_remediation_recurrence_rate_pct")
    )
    _add_signal(
        signal_id="post_remediation_recurrence_visible",
        dimension="recurrence",
        title="Recurrence after remediation remains measurable",
        passed=post_remediation_recurrence_rate_pct is not None,
        details={
            "post_remediation_recurrence_rate_pct": post_remediation_recurrence_rate_pct,
            "remediated_fingerprint_count": recurrence_metrics.get(
                "remediated_fingerprint_count"
            ),
        },
    )

    summary = {
        "pass_count": sum(1 for signal in signals if signal.get("status") == "pass"),
        "fail_count": sum(1 for signal in signals if signal.get("status") == "fail"),
        "not_evaluated_count": sum(
            1 for signal in signals if signal.get("status") == "not_evaluated"
        ),
        "total_count": len(signals),
    }
    return signals, summary


def _build_case_bundle_preview(bundle: Mapping[str, Any]) -> dict[str, Any]:
    locator = _mapping_or_empty(bundle.get("episode_locator"))
    source_resolution = _mapping_or_empty(bundle.get("source_resolution"))
    return {
        "success": bool(bundle.get("success")),
        "ready_for_critic": bool(bundle.get("ready_for_critic")),
        "fail_closed": bool(bundle.get("fail_closed")),
        "fail_closed_reason_codes": _normalise_strings(
            bundle.get("fail_closed_reason_codes"),
            limit=20,
        ),
        "bundle_receipt": _mapping_or_empty(bundle.get("bundle_receipt")),
        "episode_locator": {
            "request_id": _safe_str(locator.get("request_id")),
            "episode_id": _safe_str(locator.get("episode_id")),
            "instance_id": _safe_str(locator.get("instance_id")),
            "workflow_id": _safe_str(locator.get("workflow_id")),
        },
        "source_resolution": {
            "record_found": bool(source_resolution.get("record_found")),
            "episode_found": bool(source_resolution.get("episode_found")),
            "chat_history_found": bool(source_resolution.get("chat_history_found")),
            "execution_trace_found": bool(source_resolution.get("execution_trace_found")),
        },
        "capability_gap_count": len(bundle.get("capability_gaps") or []),
    }


def _build_case_state_preview(
    *,
    row: Mapping[str, Any],
    state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    state_mapping = _mapping_or_empty(state)
    critic = _mapping_or_empty(state_mapping.get("critic"))
    critic_summary = _mapping_or_empty(critic.get("summary"))
    subject_episode = _mapping_or_empty(state_mapping.get("subject_episode"))
    routing = _mapping_or_empty(state_mapping.get("routing"))
    return {
        "memory_id": _safe_str(row.get("memory_id")),
        "request_id": _safe_str(row.get("request_id")),
        "episode_id": _safe_str(row.get("episode_id")),
        "workflow_id": _safe_str(row.get("workflow_id")),
        "subject_episode": {
            "subject_kind": _safe_str(subject_episode.get("subject_kind")),
            "instance_id": _safe_str(subject_episode.get("instance_id")),
            "stable_key": _safe_str(subject_episode.get("stable_key")),
        },
        "critic": {
            "verdict": _safe_str(critic.get("verdict")) or _safe_str(row.get("verdict")),
            "confidence": (
                _safe_float(critic.get("confidence"))
                if _safe_float(critic.get("confidence")) is not None
                else _safe_float(row.get("confidence"))
            ),
            "summary": _safe_str(critic_summary.get("summary")),
            "root_cause_count": _safe_int(critic_summary.get("root_cause_count")),
        },
        "routing": {
            "decision": _safe_str(routing.get("decision")) or _safe_str(row.get("routing_decision")),
            "repeat_count": (
                _safe_int(routing.get("repeat_count"))
                if routing.get("repeat_count") is not None
                else _safe_int(row.get("routing_repeat_count"))
            ),
        },
    }


def _build_audit_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    post_remediation_memory_ids: set[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        memory_id = _safe_str(row.get("memory_id"))
        if not memory_id:
            continue
        repeat_count = _safe_int(row.get("routing_repeat_count"))
        unresolved = _safe_int(row.get("unresolved_check_count"))
        verdict = str(row.get("verdict") or "").strip().lower()

        if _is_false_positive_task_proxy(row):
            candidates.append(
                {
                    "memory_id": memory_id,
                    "bucket": "false_positive_task_proxy",
                    "priority": _AUDIT_BUCKET_PRIORITY["false_positive_task_proxy"],
                    "score": repeat_count + unresolved,
                }
            )
        if _is_false_negative_task_proxy(row):
            candidates.append(
                {
                    "memory_id": memory_id,
                    "bucket": "false_negative_task_proxy",
                    "priority": _AUDIT_BUCKET_PRIORITY["false_negative_task_proxy"],
                    "score": repeat_count + unresolved,
                }
            )
        if memory_id in post_remediation_memory_ids:
            candidates.append(
                {
                    "memory_id": memory_id,
                    "bucket": "post_remediation_recurrence",
                    "priority": _AUDIT_BUCKET_PRIORITY["post_remediation_recurrence"],
                    "score": repeat_count + unresolved,
                }
            )
        if verdict == "inconclusive" and _is_likely_actionable(row):
            candidates.append(
                {
                    "memory_id": memory_id,
                    "bucket": "inconclusive_actionable",
                    "priority": _AUDIT_BUCKET_PRIORITY["inconclusive_actionable"],
                    "score": repeat_count + unresolved,
                }
            )
        if repeat_count >= 2:
            candidates.append(
                {
                    "memory_id": memory_id,
                    "bucket": "high_repeat_actionable",
                    "priority": _AUDIT_BUCKET_PRIORITY["high_repeat_actionable"],
                    "score": repeat_count + unresolved,
                }
            )

    candidates.sort(
        key=lambda row: (
            int(row.get("priority") or 99),
            -int(row.get("score") or 0),
            str(row.get("memory_id") or ""),
        )
    )
    return candidates


def _build_sampled_meta_audit_cases(
    *,
    rows: Sequence[Mapping[str, Any]],
    namespace: str,
    max_audit_cases: int,
    neighbour_turn_count: int,
    post_remediation_memory_ids: set[str],
) -> tuple[list[dict[str, Any]], dict[str, int], int, int, list[str]]:
    by_memory_id = {
        _safe_str(row.get("memory_id")): row
        for row in rows
        if _safe_str(row.get("memory_id"))
    }
    candidates = _build_audit_candidates(
        rows,
        post_remediation_memory_ids=post_remediation_memory_ids,
    )

    selected: list[dict[str, Any]] = []
    seen_memory_ids: set[str] = set()
    for candidate in candidates:
        memory_id = _safe_str(candidate.get("memory_id"))
        if not memory_id or memory_id in seen_memory_ids:
            continue
        row = by_memory_id.get(memory_id)
        if not isinstance(row, Mapping):
            continue
        seen_memory_ids.add(memory_id)
        selected.append({"row": row, "bucket": candidate.get("bucket")})
        if len(selected) >= max_audit_cases:
            break

    cases: list[dict[str, Any]] = []
    bucket_counts: dict[str, int] = {}
    bundle_ready_count = 0
    bundle_fail_closed_count = 0
    bundle_failure_memory_ids: list[str] = []

    for index, selected_case in enumerate(selected, start=1):
        row = _mapping_or_empty(selected_case.get("row"))
        bucket = _safe_str(selected_case.get("bucket")) or "general_review"
        memory_id = _safe_str(row.get("memory_id")) or f"memory_{index}"
        state = get_episode_critique_memory_state(memory_id)
        state_mapping = _mapping_or_empty(state)
        subject_episode = _mapping_or_empty(state_mapping.get("subject_episode"))
        request_id = _safe_str(row.get("request_id")) or _safe_str(
            subject_episode.get("turn_id")
        )
        instance_id = _safe_str(subject_episode.get("instance_id"))
        bundle = build_episode_critic_evidence_bundle(
            request_id=request_id,
            instance_id=instance_id,
            namespace=namespace,
            neighbour_turn_count=neighbour_turn_count,
        )
        bundle_preview = _build_case_bundle_preview(bundle) if isinstance(bundle, Mapping) else {}
        if bundle_preview.get("ready_for_critic") is True:
            bundle_ready_count += 1
        if bundle_preview.get("fail_closed") is True:
            bundle_fail_closed_count += 1
            bundle_failure_memory_ids.append(memory_id)

        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        cases.append(
            {
                "case_id": f"episode_critique_audit_case_{index:02d}",
                "audit_bucket": bucket,
                "memory_id": memory_id,
                "request_id": request_id,
                "episode_id": _safe_str(row.get("episode_id")),
                "workflow_id": _safe_str(row.get("workflow_id")),
                "verdict": _safe_str(row.get("verdict")),
                "confidence": _safe_float(row.get("confidence")),
                "unresolved_check_count": _safe_int(row.get("unresolved_check_count")),
                "routing_decision": _safe_str(row.get("routing_decision")),
                "routing_repeat_count": _safe_int(row.get("routing_repeat_count")),
                "remediation_task_ids": _normalise_strings(
                    row.get("remediation_task_ids"),
                    limit=10,
                ),
                "remediation_issue_keys": _normalise_strings(
                    row.get("remediation_issue_keys"),
                    limit=10,
                ),
                "recommendations": _normalise_strings(row.get("recommendations"), limit=6),
                "critic_summary_text": _safe_str(row.get("critic_summary_text")),
                "improvement_suggestion_count": _row_improvement_suggestion_count(row),
                "improvement_suggestion_categories": _normalise_strings(
                    row.get("improvement_suggestion_categories"),
                    limit=10,
                ),
                "improvement_target_workflow_ids": _normalise_strings(
                    row.get("improvement_target_workflow_ids"),
                    limit=10,
                ),
                "improvement_target_tool_names": _normalise_strings(
                    row.get("improvement_target_tool_names"),
                    limit=10,
                ),
                "improvement_suggestions_useful": _has_useful_improvement_suggestions(
                    row
                ),
                "receipt_hash": _safe_str(row.get("receipt_hash")),
                "audit_inputs": {
                    "memory_id": memory_id,
                    "request_id": request_id,
                    "instance_id": instance_id,
                    "namespace": namespace,
                },
                "audit_questions": [
                    "Does the recorded critic verdict match the rebuilt evidence bundle?",
                    "Was remediation creation proportionate to the observed recurrence and severity?",
                    "Would you tighten, preserve, or relax the remediation threshold for this case?",
                ],
                "critic_memory_preview": _build_case_state_preview(row=row, state=state_mapping),
                "evidence_bundle_preview": bundle_preview,
            }
        )

    return (
        cases,
        bucket_counts,
        bundle_ready_count,
        bundle_fail_closed_count,
        bundle_failure_memory_ids,
    )


def _observation_verdict(
    *,
    metrics: Mapping[str, Any],
    benchmark_signal_summary: Mapping[str, Any],
    regression_assessment: Mapping[str, Any],
) -> str:
    scanned_count = _safe_int(metrics.get("scanned_count"))
    if scanned_count <= 0:
        return "inconclusive"
    if bool(regression_assessment.get("regression_detected")):
        return "fail"
    if _safe_int(benchmark_signal_summary.get("fail_count")) > 0:
        return "partial"
    return "pass"


def _record_benchmark_observation(
    *,
    run_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    from .experiment_run_service import record_experiment_observation

    metrics = _mapping_or_empty(payload.get("metrics"))
    sampled_meta_audit = _mapping_or_empty(payload.get("sampled_meta_audit"))
    cases = sampled_meta_audit.get("cases")
    audit_cases = cases if isinstance(cases, list) else []
    turn_execution_request_ids: list[str] = []
    for item in audit_cases:
        if not isinstance(item, Mapping):
            continue
        request_id = _safe_str(item.get("request_id"))
        if request_id:
            turn_execution_request_ids.append(request_id)
    benchmark_signal_summary = _mapping_or_empty(payload.get("benchmark_signal_summary"))
    regression_assessment = _mapping_or_empty(payload.get("regression_assessment"))
    observation_verdict = _observation_verdict(
        metrics=metrics,
        benchmark_signal_summary=benchmark_signal_summary,
        regression_assessment=regression_assessment,
    )
    observation = {
        "label": "episode_critique_benchmark",
        "verdict": observation_verdict,
        "observed_outcome": observation_verdict,
        "evidence": {
            "benchmark_fingerprint": _safe_str(payload.get("benchmark_fingerprint")),
            "sampled_case_ids": [
                _safe_str(item.get("case_id"))
                for item in audit_cases
                if isinstance(item, Mapping) and _safe_str(item.get("case_id"))
            ],
            "sampled_case_buckets": [
                _safe_str(item.get("audit_bucket"))
                for item in audit_cases
                if isinstance(item, Mapping) and _safe_str(item.get("audit_bucket"))
            ],
            "benchmark_signals": payload.get("benchmark_signals") or [],
            "regression_assessment": regression_assessment,
        },
        "metrics": metrics,
        "policy_decisions": [
            {
                "decision": "sampled_meta_audit_non_recursive",
                "mode": "on_demand_sample",
                "sampled_case_count": _safe_int(
                    _mapping_or_empty(metrics.get("audit_metrics")).get("sampled_case_count")
                ),
            }
        ],
    }
    result = record_experiment_observation(
        run_id=run_id,
        observations=[observation],
        turn_execution_request_ids=turn_execution_request_ids,
    )
    return {
        "success": bool(result.get("success")),
        "run_id": run_id,
        "observation_verdict": observation_verdict,
        "recorded_observation_count": len(result.get("recorded_observations") or []),
        "turn_execution_request_ids": turn_execution_request_ids,
        "result": result,
    }


def build_episode_critique_benchmark_report(
    *,
    namespace: str,
    workflow_id: str | None = None,
    request_id: str | None = None,
    episode_id: str | None = None,
    verdict: str | None = None,
    verdicts: Sequence[str] | None = None,
    from_utc: str | None = None,
    to_utc: str | None = None,
    scan_limit: int = 500,
    max_audit_cases: int = 5,
    neighbour_turn_count: int = 1,
    run_id: str | None = None,
    baseline_precision_proxy_pct: Any = None,
    baseline_recall_proxy_pct: Any = None,
    baseline_post_remediation_recurrence_rate_pct: Any = None,
    regression_tolerance_pct: Any = None,
) -> dict[str, Any]:
    resolved_namespace = _safe_str(namespace)
    if not resolved_namespace:
        return {"success": False, "error": "namespace_required"}

    resolved_scan_limit = _coerce_int(
        scan_limit,
        default=500,
        minimum=1,
        maximum=5000,
    )
    resolved_max_audit_cases = _coerce_int(
        max_audit_cases,
        default=5,
        minimum=1,
        maximum=25,
    )
    resolved_neighbour_turn_count = _coerce_int(
        neighbour_turn_count,
        default=1,
        minimum=0,
        maximum=5,
    )

    query = _build_query(
        namespace=resolved_namespace,
        workflow_id=workflow_id,
        request_id=request_id,
        episode_id=episode_id,
        verdict=verdict,
        verdicts=verdicts,
        from_utc=from_utc,
        to_utc=to_utc,
    )
    rows = _fetch_projection_rows(query=query, scan_limit=resolved_scan_limit)
    recurrence_metrics, post_remediation_memory_ids = _analyse_recurrence(rows)
    (
        sampled_cases,
        sampled_bucket_counts,
        sampled_bundle_ready_count,
        sampled_bundle_fail_closed_count,
        sampled_bundle_failure_memory_ids,
    ) = _build_sampled_meta_audit_cases(
        rows=rows,
        namespace=resolved_namespace,
        max_audit_cases=resolved_max_audit_cases,
        neighbour_turn_count=resolved_neighbour_turn_count,
        post_remediation_memory_ids=post_remediation_memory_ids,
    )
    metrics = _build_metrics(
        rows,
        audit_case_count=len(sampled_cases),
        audit_bucket_counts=sampled_bucket_counts,
        audit_bundle_ready_count=sampled_bundle_ready_count,
        audit_bundle_fail_closed_count=sampled_bundle_fail_closed_count,
    )
    recurrence_metrics_payload = _mapping_or_empty(metrics.get("recurrence_metrics"))
    recurrence_metrics_payload.update(recurrence_metrics)
    metrics["recurrence_metrics"] = recurrence_metrics_payload

    regression_assessment = _build_regression_assessment(
        metrics=metrics,
        baseline_precision_proxy_pct=baseline_precision_proxy_pct,
        baseline_recall_proxy_pct=baseline_recall_proxy_pct,
        baseline_post_remediation_recurrence_rate_pct=(
            baseline_post_remediation_recurrence_rate_pct
        ),
        regression_tolerance_pct=regression_tolerance_pct,
    )
    benchmark_signals, benchmark_signal_summary = _build_benchmark_signals(
        metrics=metrics,
        regression_assessment=regression_assessment,
        max_audit_cases=resolved_max_audit_cases,
    )
    capability_gaps = _build_capability_gaps(
        rows,
        metrics=metrics,
        sampled_bundle_fail_closed_count=sampled_bundle_fail_closed_count,
        sampled_bundle_failure_memory_ids=sampled_bundle_failure_memory_ids,
    )
    recommendations = _build_recommendations(
        rows=rows,
        metrics=metrics,
        capability_gaps=capability_gaps,
    )
    filters_payload = {
        "namespace": resolved_namespace,
        "workflow_id": _safe_str(workflow_id),
        "request_id": _safe_str(request_id),
        "episode_id": _safe_str(episode_id),
        "verdict": _safe_str(verdict),
        "verdicts": _normalise_strings(verdicts, limit=20),
        "from_utc": _safe_str(from_utc),
        "to_utc": _safe_str(to_utc),
        "scan_limit": resolved_scan_limit,
        "max_audit_cases": resolved_max_audit_cases,
        "neighbour_turn_count": resolved_neighbour_turn_count,
        "run_id": _safe_str(run_id),
    }
    benchmark_fingerprint = _json_fingerprint(
        {
            "filters": filters_payload,
            "sampled_cases": [
                {
                    "memory_id": case.get("memory_id"),
                    "audit_bucket": case.get("audit_bucket"),
                    "request_id": case.get("request_id"),
                }
                for case in sampled_cases
            ],
        }
    )

    payload: dict[str, Any] = {
        "success": True,
        "schema_version": EPISODE_CRITIQUE_BENCHMARK_SCHEMA_VERSION,
        "collection": "episode_critique_memories",
        "namespace": resolved_namespace,
        "filters": filters_payload,
        "metrics": metrics,
        "benchmark_fingerprint": benchmark_fingerprint,
        "sampled_meta_audit": {
            "mode": "on_demand_sample",
            "non_recursive": True,
            "max_cases": resolved_max_audit_cases,
            "case_count": len(sampled_cases),
            "bucket_counts": sampled_bucket_counts,
            "cases": sampled_cases,
        },
        "regression_assessment": regression_assessment,
        "benchmark_signals": benchmark_signals,
        "benchmark_signal_summary": benchmark_signal_summary,
        "capability_gaps": capability_gaps,
        "recommendations": recommendations,
    }

    resolved_run_id = _safe_str(run_id)
    if resolved_run_id:
        try:
            payload["experiment_recording"] = _record_benchmark_observation(
                run_id=resolved_run_id,
                payload=payload,
            )
        except Exception as exc:
            logger.warning(
                "[episode_critique_benchmark] could not record benchmark observation for %s: %s",
                resolved_run_id,
                exc,
            )
            payload["experiment_recording"] = {
                "success": False,
                "run_id": resolved_run_id,
                "error": "experiment_record_observation_failed",
                "details": str(exc),
            }

    return payload


__all__ = [
    "EPISODE_CRITIQUE_BENCHMARK_SCHEMA_VERSION",
    "build_episode_critique_benchmark_report",
]
