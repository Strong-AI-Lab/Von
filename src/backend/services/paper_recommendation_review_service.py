"""Bounded review payloads for Von-native paper recommendation inspection.

This keeps the review surface separate from the ranking core:

- ranking stays in ``paper_recommendation_ranking_service``
- this service chooses a bounded candidate pool and adds trigger metadata

The settings tab is the first authoritative review surface. External channels
can reuse this payload later, but they are not authoritative.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Iterable, Mapping, Sequence

REVIEW_SURFACE_ID = "settings.paper_recommendation_review.v1"
REVIEW_SURFACE_LABEL = "Settings tab paper recommendation review"
SCHOLARLY_ARTICLE_TYPE_ID = "#V#scholarly_article"
DEFAULT_CANDIDATE_LIMIT = 25
MAX_CANDIDATE_LIMIT = 100
_PUBLICATION_DATE_PREDICATE_ID = "#V#has_publication_date"
_CANDIDATE_SPLIT_RE = re.compile(r"[\s,]+")

_SUPPORTED_TRIGGER_POINTS = {
    "manual_review": {
        "trigger_source": "manual_review",
        "label": "Manual review",
        "description": (
            "User requests a bounded recommendation review from the settings tab."
        ),
        "active": True,
    },
    "profile_saved": {
        "trigger_source": "profile_saved",
        "label": "Profile saved",
        "description": (
            "Re-run the current recommendation review immediately after the paper "
            "recommendation profile is saved."
        ),
        "active": True,
    },
    "candidate_ingestion": {
        "trigger_source": "candidate_ingestion",
        "label": "Candidate ingestion",
        "description": (
            "When new scholarly-paper candidate IDs are known, call the same "
            "review surface with those explicit IDs to inspect the new papers."
        ),
        "active": True,
    },
}


def search_concepts(**kwargs):
    from .concept_search_service import search_concepts as _impl

    return _impl(**kwargs)


def get_texts_for_concept(
    subject_concept_id: str,
    predicate: str | None = None,
    limit: int = 50,
):
    from .text_value_service import get_texts_for_concept as _impl

    return _impl(
        subject_concept_id=subject_concept_id,
        predicate=predicate,
        limit=limit,
    )


def build_paper_recommendations(**kwargs):
    from .paper_recommendation_ranking_service import (
        build_paper_recommendations as _impl,
    )

    return _impl(**kwargs)


def _safe_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _safe_int(value: Any, *, default: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(resolved, MAX_CANDIDATE_LIMIT))


def _normalise_candidate_ids(raw_value: Sequence[str] | str | None) -> list[str]:
    values: Iterable[Any]
    if isinstance(raw_value, str):
        values = _CANDIDATE_SPLIT_RE.split(raw_value)
    elif isinstance(raw_value, Sequence):
        values = raw_value
    else:
        values = ()

    candidate_ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _safe_text(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        candidate_ids.append(cleaned)
    return candidate_ids


def _normalise_trigger_source(value: Any) -> dict[str, Any]:
    cleaned = _safe_text(value).lower()
    if cleaned in _SUPPORTED_TRIGGER_POINTS:
        return dict(_SUPPORTED_TRIGGER_POINTS[cleaned])
    return dict(_SUPPORTED_TRIGGER_POINTS["manual_review"])


def _parse_publication_date(value: Any) -> tuple[int, float]:
    cleaned = _safe_text(value)
    if not cleaned:
        return (1, 0.0)
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return (1, 0.0)
    return (0, -parsed.timestamp())


def _load_publication_date(paper_concept_id: str) -> str | None:
    rows = get_texts_for_concept(
        paper_concept_id,
        predicate=_PUBLICATION_DATE_PREDICATE_ID,
        limit=3,
    )
    if not isinstance(rows, Sequence):
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        text = _safe_text(row.get("text"))
        if text:
            return text
    return None


def _discover_candidate_papers(*, candidate_limit: int) -> list[dict[str, Any]]:
    search_result = search_concepts(
        query="",
        instance_of=SCHOLARLY_ARTICLE_TYPE_ID,
        filter_kind=["individual"],
        limit=candidate_limit,
        match_type="substring",
    )
    rows = search_result.get("results") if isinstance(search_result, Mapping) else []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, Sequence) else []:
        if not isinstance(row, Mapping):
            continue
        concept_id = _safe_text(row.get("concept_id"))
        if not concept_id or concept_id in seen:
            continue
        seen.add(concept_id)
        candidates.append(
            {
                "paper_concept_id": concept_id,
                "paper_title": _safe_text(row.get("name")) or concept_id,
                "publication_date": _load_publication_date(concept_id),
            }
        )
    candidates.sort(
        key=lambda item: (
            *_parse_publication_date(item.get("publication_date")),
            _safe_text(item.get("paper_title")).casefold(),
            _safe_text(item.get("paper_concept_id")).casefold(),
        )
    )
    return candidates


def build_paper_recommendation_review(
    *,
    user_concept_id: str,
    candidate_paper_concept_ids: Sequence[str] | str | None = None,
    candidate_limit: int | None = None,
    include_all_candidates: bool = True,
    trigger_source: str | None = None,
) -> dict[str, Any]:
    """Build a bounded paper recommendation review payload for one user."""

    target_user = _safe_text(user_concept_id)
    safe_candidate_limit = _safe_int(candidate_limit, default=DEFAULT_CANDIDATE_LIMIT)
    resolved_trigger = _normalise_trigger_source(trigger_source)
    explicit_candidate_ids = _normalise_candidate_ids(candidate_paper_concept_ids)

    discovered_candidates: list[dict[str, Any]]
    candidate_source: str
    if explicit_candidate_ids:
        discovered_candidates = [
            {"paper_concept_id": concept_id, "paper_title": concept_id}
            for concept_id in explicit_candidate_ids
        ]
        candidate_ids = explicit_candidate_ids
        candidate_source = "explicit_candidate_ids"
    else:
        discovered_candidates = _discover_candidate_papers(
            candidate_limit=safe_candidate_limit
        )
        candidate_ids = [
            str(item.get("paper_concept_id"))
            for item in discovered_candidates
            if _safe_text(item.get("paper_concept_id"))
        ]
        candidate_source = "represented_scholarly_articles"

    report = build_paper_recommendations(
        user_concept_id=target_user,
        candidate_paper_concept_ids=candidate_ids,
        max_results=safe_candidate_limit,
        include_all_candidates=bool(include_all_candidates),
    )

    return {
        "success": bool(isinstance(report, Mapping) and report.get("success")),
        "review_surface_id": REVIEW_SURFACE_ID,
        "review_surface_label": REVIEW_SURFACE_LABEL,
        "authoritative_review_surface": "settings_tab",
        "external_channels_authoritative": False,
        "user_concept_id": target_user,
        "trigger": resolved_trigger,
        "supported_trigger_points": [
            dict(item) for item in _SUPPORTED_TRIGGER_POINTS.values()
        ],
        "candidate_selection": {
            "source": candidate_source,
            "candidate_limit": safe_candidate_limit,
            "candidate_count": len(candidate_ids),
            "candidate_type_concept_id": SCHOLARLY_ARTICLE_TYPE_ID,
            "candidate_paper_concept_ids": list(candidate_ids),
            "discovered_candidates": discovered_candidates,
        },
        "recommendation_report": dict(report) if isinstance(report, Mapping) else {},
    }


__all__ = [
    "DEFAULT_CANDIDATE_LIMIT",
    "MAX_CANDIDATE_LIMIT",
    "REVIEW_SURFACE_ID",
    "REVIEW_SURFACE_LABEL",
    "SCHOLARLY_ARTICLE_TYPE_ID",
    "build_paper_recommendation_review",
]
