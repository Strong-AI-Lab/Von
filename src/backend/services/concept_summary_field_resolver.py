"""Vontology-backed summary field predicate resolution.

This service moves concept-summary field-to-predicate configuration out of
``concept_summary_renderer_service`` and into Vontology-backed field concepts.

Each summary field is represented by a concept whose ID follows the pattern
``#V#summary_field_<field_key>``. The field concept may define either of these
singleton text relations, each containing a JSON list of predicate IDs:

- ``#V#has_summary_text_predicates_json``
- ``#V#has_summary_relationship_predicates_json``

When Vontology metadata is absent, the resolver falls back to the historical
in-code defaults so the renderer remains deterministic during rollout.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .task_ontology_service import TASK_SOURCE_RELATIONSHIP_PREDICATES
from .text_value_service import get_texts_for_concept

_logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS: float = 300.0

_TEXT_PREDICATE_CONFIG = "#V#has_summary_text_predicates_json"
_RELATIONSHIP_PREDICATE_CONFIG = "#V#has_summary_relationship_predicates_json"

_DEFAULT_TEXT_FIELD_PREDICATES: dict[str, tuple[str, ...]] = {
    "description": ("hasDescription", "#V#hasDescription"),
    "content": ("hasContent", "#V#hasContent"),
    "email": ("#V#has_email", "has_email"),
    "publication_date": ("#V#has_publication_date", "has_publication_date"),
    "task_status": ("#V#hasTaskStatus", "hasTaskStatus"),
    "priority": ("#V#hasPriority", "hasPriority"),
    "due_date": (
        "#V#hasDueDate",
        "#V#has_due_date",
        "#V#has_due_time",
        "#V#has_due",
        "hasDueDate",
        "has_due_date",
    ),
    "event_date": (
        "#V#date_of_event",
        "#V#has_start_time",
        "date_of_event",
        "has_start_time",
    ),
    "capacity": ("#V#has_capacity", "has_capacity", "#V#capacity", "capacity"),
    "url": ("#V#has_url", "has_url"),
}

_DEFAULT_RELATIONSHIP_FIELD_PREDICATES: dict[str, tuple[str, ...]] = {
    "affiliation": (
        "#V#has_affiliation",
        "#V#member_of_organisation",
        "#V#member_of_faculty",
        "#V#homeresearchorganisation",
    ),
    "author": ("#V#has_author", "#V#has_first_author", "#V#authored_by"),
    "meeting_participant": ("#V#meeting_participant", "#V#performed_by"),
    "meeting_location": ("#V#meeting_location", "#V#has_location"),
    "meeting_host": ("#V#meeting_host_organisation",),
    "task_source": TASK_SOURCE_RELATIONSHIP_PREDICATES,
    "authored_work": ("#V#author_of",),
}


def _dedupe_preserve_order(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return tuple(ordered)


def _field_concept_id(field_key: str) -> str:
    return f"#V#summary_field_{field_key}"


def _normalise_predicate_list(raw_text: Any) -> tuple[str, ...]:
    if isinstance(raw_text, list):
        return _dedupe_preserve_order([str(item or "").strip() for item in raw_text])
    if not isinstance(raw_text, str):
        return ()

    text = raw_text.strip()
    if not text:
        return ()

    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None

    if isinstance(parsed, list):
        return _dedupe_preserve_order([str(item or "").strip() for item in parsed])

    if "," in text:
        return _dedupe_preserve_order([part.strip() for part in text.split(",")])

    return _dedupe_preserve_order([line.strip() for line in text.splitlines()])


class ConceptSummaryFieldResolver:
    """Resolve summary field predicates from Vontology-backed metadata."""

    def __init__(self, *, cache_ttl_seconds: float = _CACHE_TTL_SECONDS) -> None:
        self._cache_ttl_seconds = float(cache_ttl_seconds)
        self._cache_loaded_at = 0.0
        self._text_predicates_by_field: dict[str, tuple[str, ...]] = {}
        self._relationship_predicates_by_field: dict[str, tuple[str, ...]] = {}

    def invalidate_cache(self) -> None:
        self._cache_loaded_at = 0.0
        self._text_predicates_by_field = {}
        self._relationship_predicates_by_field = {}

    def get_text_predicates_for_field(self, field_key: str) -> tuple[str, ...]:
        self._ensure_cache()
        return self._text_predicates_by_field.get(field_key, ())

    def get_relationship_predicates_for_field(self, field_key: str) -> tuple[str, ...]:
        self._ensure_cache()
        return self._relationship_predicates_by_field.get(field_key, ())

    def _ensure_cache(self) -> None:
        if self._cache_loaded_at and (time.time() - self._cache_loaded_at) <= self._cache_ttl_seconds:
            return
        self._load_cache()

    def _load_cache(self) -> None:
        text_predicates_by_field = dict(_DEFAULT_TEXT_FIELD_PREDICATES)
        relationship_predicates_by_field = dict(_DEFAULT_RELATIONSHIP_FIELD_PREDICATES)

        loaded_fields = 0
        for field_key in sorted(
            set(text_predicates_by_field.keys()) | set(relationship_predicates_by_field.keys())
        ):
            concept_id = _field_concept_id(field_key)
            try:
                text_rows = get_texts_for_concept(
                    concept_id,
                    predicate=_TEXT_PREDICATE_CONFIG,
                    limit=5,
                )
                relationship_rows = get_texts_for_concept(
                    concept_id,
                    predicate=_RELATIONSHIP_PREDICATE_CONFIG,
                    limit=5,
                )
            except Exception as exc:
                _logger.debug(
                    "[summary_field_resolver] Failed to load field metadata for %s: %s",
                    concept_id,
                    exc,
                )
                continue

            text_predicates = self._first_config_value(text_rows)
            relationship_predicates = self._first_config_value(relationship_rows)

            if text_predicates:
                text_predicates_by_field[field_key] = text_predicates
                loaded_fields += 1
            if relationship_predicates:
                relationship_predicates_by_field[field_key] = relationship_predicates
                loaded_fields += 1

        self._text_predicates_by_field = text_predicates_by_field
        self._relationship_predicates_by_field = relationship_predicates_by_field
        self._cache_loaded_at = time.time()

        if loaded_fields == 0:
            _logger.debug(
                "[summary_field_resolver] No Vontology-backed summary field metadata found; using defaults."
            )

    @staticmethod
    def _first_config_value(rows: list[dict[str, Any]]) -> tuple[str, ...]:
        for row in rows:
            values = _normalise_predicate_list(row.get("text"))
            if values:
                return values
        return ()


_resolver: ConceptSummaryFieldResolver | None = None


def get_concept_summary_field_resolver() -> ConceptSummaryFieldResolver:
    global _resolver
    if _resolver is None:
        _resolver = ConceptSummaryFieldResolver()
    return _resolver
