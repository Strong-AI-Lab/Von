from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

WORKFLOW_DESCRIPTION_QUALITY_SCHEMA_VERSION = "workflow_description_quality.v1"

_QUALITY_FACET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("domain", re.compile(r"\bdomain\s*:", re.IGNORECASE)),
    ("input_types", re.compile(r"\binput types?\s*:", re.IGNORECASE)),
    ("output_types", re.compile(r"\boutput types?\s*:", re.IGNORECASE)),
    (
        "prerequisite_capabilities",
        re.compile(r"\b(prerequisite|required) capabilities\s*:", re.IGNORECASE),
    ),
    ("cost_class", re.compile(r"\bcost class\s*:", re.IGNORECASE)),
    ("maturity", re.compile(r"\bmaturity\s*:", re.IGNORECASE)),
    (
        "success_likelihood",
        re.compile(r"\bestimated success likelihood\s*:", re.IGNORECASE),
    ),
)


def _normalise_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _quality_label(score: int) -> str:
    if score <= 1:
        return "stub"
    if score <= 3:
        return "minimal"
    if score <= 5:
        return "developing"
    return "retrieval_ready"


def assess_workflow_description_quality(
    *,
    description: str | None,
    description_source: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic quality assessment for workflow descriptions."""

    text = _normalise_text(description)
    source = _normalise_text(description_source) or "none"

    signals: dict[str, bool] = {
        "summary_sentence": len(text) >= 48 and any(ch in text for ch in ".:"),
    }
    for facet_name, pattern in _QUALITY_FACET_PATTERNS:
        signals[facet_name] = bool(text and pattern.search(text))

    score = sum(1 for matched in signals.values() if matched)
    quality_label = _quality_label(score)

    missing_facets = [
        facet_name for facet_name, matched in signals.items() if not bool(matched)
    ]
    issue_codes: list[str] = []
    if not text:
        issue_codes.append("missing_description")
    elif len(text) < 80:
        issue_codes.append("short_description")
    if source in {"registration.purpose", "definition.purpose", "none"}:
        issue_codes.append("fallback_source")
    if quality_label in {"stub", "minimal"}:
        issue_codes.append("under_specified_for_routing")

    return {
        "schema_version": WORKFLOW_DESCRIPTION_QUALITY_SCHEMA_VERSION,
        "score": score,
        "max_score": len(signals),
        "quality_label": quality_label,
        "signals": signals,
        "missing_facets": missing_facets,
        "issue_codes": issue_codes,
        "description_length": len(text),
        "description_source": source,
    }


def summarise_workflow_description_quality(
    records: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Aggregate workflow-description quality diagnostics for inventory surfaces."""

    counts_by_label = {
        "stub": 0,
        "minimal": 0,
        "developing": 0,
        "retrieval_ready": 0,
    }
    counts_by_source: dict[str, int] = {}
    under_specified_workflow_ids: list[str] = []
    missing_workflow_ids: list[str] = []

    total = 0
    for record in records or ():
        if not isinstance(record, Mapping):
            continue
        workflow_id = _normalise_text(record.get("workflow_id"))
        description = _normalise_text(record.get("description"))
        description_source = _normalise_text(record.get("description_source")) or "none"
        quality = assess_workflow_description_quality(
            description=description,
            description_source=description_source,
        )
        label = str(quality.get("quality_label") or "stub")
        counts_by_label[label] = counts_by_label.get(label, 0) + 1
        counts_by_source[description_source] = counts_by_source.get(description_source, 0) + 1
        total += 1
        if "missing_description" in quality.get("issue_codes", []):
            if workflow_id:
                missing_workflow_ids.append(workflow_id)
            continue
        if "under_specified_for_routing" in quality.get("issue_codes", []):
            if workflow_id:
                under_specified_workflow_ids.append(workflow_id)

    return {
        "schema_version": WORKFLOW_DESCRIPTION_QUALITY_SCHEMA_VERSION,
        "counts": {
            "total": total,
            "stub": counts_by_label.get("stub", 0),
            "minimal": counts_by_label.get("minimal", 0),
            "developing": counts_by_label.get("developing", 0),
            "retrieval_ready": counts_by_label.get("retrieval_ready", 0),
            "under_specified": len(under_specified_workflow_ids),
            "missing": len(missing_workflow_ids),
        },
        "counts_by_label": counts_by_label,
        "counts_by_source": counts_by_source,
        "under_specified_workflow_ids": under_specified_workflow_ids,
        "missing_workflow_ids": missing_workflow_ids,
    }


__all__ = [
    "WORKFLOW_DESCRIPTION_QUALITY_SCHEMA_VERSION",
    "assess_workflow_description_quality",
    "summarise_workflow_description_quality",
]
