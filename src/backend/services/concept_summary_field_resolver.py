"""Vontology-backed summary field predicate and type-binding resolution.

Each summary field is represented by a ``#V#summary_field_*`` concept. Field
concepts carry ordered predicate lists as text relations, and type concepts
link to field concepts through ``#V#has_summary_fields``. Python only resolves
and caches that represented metadata; field policy lives in Vontology.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping, Sequence

from .concept_service import get_concept_by_concept_id
from .text_value_service import get_texts_for_concept

_logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS: float = 300.0

SUMMARY_TEXT_PREDICATE_CONFIG = "#V#has_summary_text_predicates_json"
SUMMARY_RELATIONSHIP_PREDICATE_CONFIG = (
    "#V#has_summary_relationship_predicates_json"
)
SUMMARY_FIELD_DISPLAY_ORDER_PREDICATE = "#V#summary_field_display_order"
SUMMARY_TYPE_FIELD_RELATIONSHIP = "#V#has_summary_fields"

_TEXT_PREDICATE_CONFIG_ALIASES: tuple[str, ...] = (
    SUMMARY_TEXT_PREDICATE_CONFIG,
    "#V#hasSummaryTextPredicate",
)
_RELATIONSHIP_PREDICATE_CONFIG_ALIASES: tuple[str, ...] = (
    SUMMARY_RELATIONSHIP_PREDICATE_CONFIG,
    "#V#hasSummaryRelationshipPredicate",
)
_TYPE_FIELD_RELATIONSHIP_ALIASES: tuple[str, ...] = (
    SUMMARY_TYPE_FIELD_RELATIONSHIP,
    "#V#hasSummaryFields",
)


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


def _field_key_from_concept_id(concept_id: str) -> str | None:
    prefix = "#V#summary_field_"
    if not isinstance(concept_id, str) or not concept_id.startswith(prefix):
        return None
    key = concept_id.removeprefix(prefix).strip()
    return key or None


def _normalise_strings(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return _dedupe_preserve_order([str(item or "").strip() for item in raw])


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
        self._summary_fields_by_type: dict[str, tuple[str, ...]] = {}

    def invalidate_cache(self) -> None:
        self._cache_loaded_at = 0.0
        self._text_predicates_by_field = {}
        self._relationship_predicates_by_field = {}
        self._summary_fields_by_type = {}

    def get_text_predicates_for_field(self, field_key: str) -> tuple[str, ...]:
        self._ensure_cache()
        normalised = str(field_key or "").strip()
        if normalised not in self._text_predicates_by_field:
            self._load_field_metadata(normalised)
        return self._text_predicates_by_field.get(normalised, ())

    def get_relationship_predicates_for_field(self, field_key: str) -> tuple[str, ...]:
        self._ensure_cache()
        normalised = str(field_key or "").strip()
        if normalised not in self._relationship_predicates_by_field:
            self._load_field_metadata(normalised)
        return self._relationship_predicates_by_field.get(normalised, ())

    def get_summary_fields_for_type(self, type_id: str) -> tuple[str, ...]:
        self._ensure_cache()
        normalised = str(type_id or "").strip()
        if not normalised:
            return ()
        if normalised not in self._summary_fields_by_type:
            self._load_type_binding(normalised)
        return self._summary_fields_by_type.get(normalised, ())

    def get_summary_fields_for_types(self, type_ids: Sequence[str]) -> tuple[str, ...]:
        self._ensure_cache()
        for type_id in _normalise_strings(type_ids):
            fields = self.get_summary_fields_for_type(type_id)
            if fields:
                return fields
        return ()

    def _ensure_cache(self) -> None:
        if self._cache_loaded_at and (time.time() - self._cache_loaded_at) <= self._cache_ttl_seconds:
            return
        self._load_cache()

    def _load_cache(self) -> None:
        self._text_predicates_by_field = {}
        self._relationship_predicates_by_field = {}
        self._summary_fields_by_type = {}
        self._cache_loaded_at = time.time()

    def _load_field_metadata(self, field_key: str) -> None:
        if not field_key:
            return
        concept_id = _field_concept_id(field_key)
        text_predicates = self._load_first_predicate_config(
            concept_id=concept_id,
            predicates=_TEXT_PREDICATE_CONFIG_ALIASES,
        )
        relationship_predicates = self._load_first_predicate_config(
            concept_id=concept_id,
            predicates=_RELATIONSHIP_PREDICATE_CONFIG_ALIASES,
        )
        self._text_predicates_by_field[field_key] = text_predicates
        self._relationship_predicates_by_field[field_key] = relationship_predicates

    def _load_type_binding(self, type_id: str) -> None:
        try:
            concept = get_concept_by_concept_id(type_id)
        except Exception as exc:
            _logger.debug(
                "[summary_field_resolver] Failed to load type binding for %s: %s",
                type_id,
                exc,
            )
            self._summary_fields_by_type[type_id] = ()
            return
        if not isinstance(concept, Mapping):
            self._summary_fields_by_type[type_id] = ()
            return
        relationships = concept.get("relationships")
        if not isinstance(relationships, Mapping):
            self._summary_fields_by_type[type_id] = ()
            return

        field_keys: list[str] = []
        for predicate in _TYPE_FIELD_RELATIONSHIP_ALIASES:
            for field_concept_id in _normalise_strings(relationships.get(predicate)):
                field_key = _field_key_from_concept_id(field_concept_id)
                if field_key:
                    field_keys.append(field_key)
            if field_keys:
                break
        self._summary_fields_by_type[type_id] = _dedupe_preserve_order(field_keys)

    @staticmethod
    def _load_first_predicate_config(
        *,
        concept_id: str,
        predicates: Sequence[str],
    ) -> tuple[str, ...]:
        for predicate in predicates:
            try:
                rows = get_texts_for_concept(
                    concept_id,
                    predicate=predicate,
                    limit=5,
                )
            except Exception as exc:
                _logger.debug(
                    "[summary_field_resolver] Failed to load field metadata for %s/%s: %s",
                    concept_id,
                    predicate,
                    exc,
                )
                continue
            values = ConceptSummaryFieldResolver._first_config_value(rows)
            if values:
                return values
        return ()

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


__all__ = [
    "ConceptSummaryFieldResolver",
    "SUMMARY_FIELD_DISPLAY_ORDER_PREDICATE",
    "SUMMARY_RELATIONSHIP_PREDICATE_CONFIG",
    "SUMMARY_TEXT_PREDICATE_CONFIG",
    "SUMMARY_TYPE_FIELD_RELATIONSHIP",
    "get_concept_summary_field_resolver",
]
