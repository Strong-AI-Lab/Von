"""Vontology-backed summary field predicate and type-binding resolution.

Each summary field is represented by a ``#V#summary_field_*`` concept. Field
concepts carry ordered predicate lists as text relations, and type concepts
link to field concepts through ``#V#has_summary_fields``. Python only resolves
and caches that represented metadata; field policy lives in Vontology.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from .concept_service import get_concept_by_concept_id
from .text_value_service import get_texts_for_concept, get_texts_for_concepts

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
_DISPLAY_ORDER_PREDICATE_ALIASES: tuple[str, ...] = (
    SUMMARY_FIELD_DISPLAY_ORDER_PREDICATE,
    "#V#summaryFieldDisplayOrder",
)


@dataclass(frozen=True)
class SummaryFieldDefinition:
    """Resolved presentation metadata for one represented summary field."""

    field_key: str
    field_concept_id: str
    label: str
    display_order: float | None
    text_predicates: tuple[str, ...]
    relationship_predicates: tuple[str, ...]
    origin_type_ids: tuple[str, ...]


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


def _normalise_display_order(raw_text: Any) -> float | None:
    if isinstance(raw_text, bool):
        return None
    try:
        value = float(str(raw_text).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _fallback_field_label(field_key: str) -> str:
    return " ".join(str(field_key or "").replace("_", " ").split()).title()


def _field_label(concept: Mapping[str, Any] | None, field_key: str) -> str:
    if isinstance(concept, Mapping):
        name = concept.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        names = concept.get("names")
        if isinstance(names, Sequence) and not isinstance(names, (str, bytes)):
            for item in names:
                if not isinstance(item, Mapping):
                    continue
                value = item.get("name")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return _fallback_field_label(field_key)


class ConceptSummaryFieldResolver:
    """Resolve summary field predicates from Vontology-backed metadata."""

    def __init__(self, *, cache_ttl_seconds: float = _CACHE_TTL_SECONDS) -> None:
        self._cache_ttl_seconds = float(cache_ttl_seconds)
        self._cache_loaded_at = 0.0
        self._text_predicates_by_field: dict[str, tuple[str, ...]] = {}
        self._relationship_predicates_by_field: dict[str, tuple[str, ...]] = {}
        self._display_order_by_field: dict[str, float | None] = {}
        self._field_labels_by_field: dict[str, str] = {}
        self._summary_fields_by_type: dict[str, tuple[str, ...]] = {}

    def invalidate_cache(self) -> None:
        self._cache_loaded_at = 0.0
        self._text_predicates_by_field = {}
        self._relationship_predicates_by_field = {}
        self._display_order_by_field = {}
        self._field_labels_by_field = {}
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
        return tuple(
            definition.field_key
            for definition in self.get_summary_field_definitions_for_types(type_ids)
        )

    def get_summary_field_definitions_for_types(
        self,
        type_ids: Sequence[str],
    ) -> tuple[SummaryFieldDefinition, ...]:
        """Resolve the ordered union of fields bound to every supplied type.

        ``type_ids`` is expected to contain the direct types followed by their
        ancestors.  A field inherited through more than one type is returned
        once while retaining every origin type for inspectability.
        """

        self._ensure_cache()
        ordered_type_ids = _normalise_strings(type_ids)
        self._load_type_bindings(ordered_type_ids)
        ordered_field_keys: list[str] = []
        origin_type_ids_by_field: dict[str, list[str]] = {}
        for type_id in ordered_type_ids:
            fields = self._summary_fields_by_type.get(type_id, ())
            for field_key in fields:
                if field_key not in origin_type_ids_by_field:
                    ordered_field_keys.append(field_key)
                    origin_type_ids_by_field[field_key] = []
                origin_type_ids_by_field[field_key].append(type_id)
        if not ordered_field_keys:
            return ()

        self._load_field_metadata_for_fields(ordered_field_keys)
        discovery_index = {
            field_key: index for index, field_key in enumerate(ordered_field_keys)
        }
        ordered_field_keys.sort(
            key=lambda field_key: (
                self._display_order_by_field.get(field_key) is None,
                self._display_order_by_field.get(field_key)
                if self._display_order_by_field.get(field_key) is not None
                else 0.0,
                discovery_index[field_key],
            )
        )
        return tuple(
            SummaryFieldDefinition(
                field_key=field_key,
                field_concept_id=_field_concept_id(field_key),
                label=self._field_labels_by_field.get(field_key)
                or _fallback_field_label(field_key),
                display_order=self._display_order_by_field.get(field_key),
                text_predicates=self._text_predicates_by_field.get(field_key, ()),
                relationship_predicates=(
                    self._relationship_predicates_by_field.get(field_key, ())
                ),
                origin_type_ids=tuple(origin_type_ids_by_field[field_key]),
            )
            for field_key in ordered_field_keys
        )

    def _ensure_cache(self) -> None:
        if self._cache_loaded_at and (time.time() - self._cache_loaded_at) <= self._cache_ttl_seconds:
            return
        self._load_cache()

    def _load_cache(self) -> None:
        self._text_predicates_by_field = {}
        self._relationship_predicates_by_field = {}
        self._display_order_by_field = {}
        self._field_labels_by_field = {}
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
        display_order = self._load_first_display_order(concept_id=concept_id)
        try:
            field_concept = get_concept_by_concept_id(concept_id)
        except Exception:
            field_concept = None
        self._text_predicates_by_field[field_key] = text_predicates
        self._relationship_predicates_by_field[field_key] = relationship_predicates
        self._display_order_by_field[field_key] = display_order
        self._field_labels_by_field[field_key] = _field_label(
            field_concept if isinstance(field_concept, Mapping) else None,
            field_key,
        )

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

    def _load_type_bindings(self, type_ids: Sequence[str]) -> None:
        missing = [
            type_id
            for type_id in _normalise_strings(type_ids)
            if type_id not in self._summary_fields_by_type
        ]
        if not missing:
            return
        try:
            docs = list(
                ConceptsRepository.find(
                    {"concept_id": {"$in": missing}},
                    {"concept_id": 1, "relationships": 1},
                    limit=len(missing),
                )
            )
        except Exception as exc:
            _logger.debug(
                "[summary_field_resolver] Failed to batch-load type bindings: %s",
                exc,
            )
            docs = []
        docs_by_id = {
            str(doc.get("concept_id") or "").strip(): doc
            for doc in docs
            if isinstance(doc, Mapping)
        }
        for type_id in missing:
            concept = docs_by_id.get(type_id)
            relationships = (
                concept.get("relationships")
                if isinstance(concept, Mapping)
                else None
            )
            if not isinstance(relationships, Mapping):
                self._summary_fields_by_type[type_id] = ()
                continue
            field_keys: list[str] = []
            for predicate in _TYPE_FIELD_RELATIONSHIP_ALIASES:
                for field_concept_id in _normalise_strings(
                    relationships.get(predicate)
                ):
                    field_key = _field_key_from_concept_id(field_concept_id)
                    if field_key:
                        field_keys.append(field_key)
                if field_keys:
                    break
            self._summary_fields_by_type[type_id] = _dedupe_preserve_order(
                field_keys
            )

    def _load_field_metadata_for_fields(self, field_keys: Sequence[str]) -> None:
        ordered_field_keys = _normalise_strings(field_keys)
        missing_field_keys = [
            field_key
            for field_key in ordered_field_keys
            if field_key not in self._text_predicates_by_field
            or field_key not in self._relationship_predicates_by_field
            or field_key not in self._display_order_by_field
            or field_key not in self._field_labels_by_field
        ]
        if not missing_field_keys:
            return
        concept_ids = [_field_concept_id(key) for key in missing_field_keys]
        predicates = _dedupe_preserve_order(
            [
                *_TEXT_PREDICATE_CONFIG_ALIASES,
                *_RELATIONSHIP_PREDICATE_CONFIG_ALIASES,
                *_DISPLAY_ORDER_PREDICATE_ALIASES,
            ]
        )
        try:
            rows_by_concept = get_texts_for_concepts(
                concept_ids,
                predicates=predicates,
                limit_per_concept=10,
            )
        except Exception as exc:
            _logger.debug(
                "[summary_field_resolver] Failed to batch-load field metadata: %s",
                exc,
            )
            rows_by_concept = {}

        try:
            field_docs = list(
                ConceptsRepository.find(
                    {"concept_id": {"$in": concept_ids}},
                    {"concept_id": 1, "name": 1, "names": 1},
                    limit=len(concept_ids),
                )
            )
        except Exception as exc:
            _logger.debug(
                "[summary_field_resolver] Failed to batch-load field labels: %s",
                exc,
            )
            field_docs = []
        field_docs_by_id = {
            str(doc.get("concept_id") or "").strip(): doc
            for doc in field_docs
            if isinstance(doc, Mapping)
        }

        for field_key, concept_id in zip(missing_field_keys, concept_ids):
            rows = rows_by_concept.get(concept_id, [])
            text_predicates: tuple[str, ...] = ()
            relationship_predicates: tuple[str, ...] = ()
            display_order: float | None = None
            for predicate in _TEXT_PREDICATE_CONFIG_ALIASES:
                text_predicates = self._first_config_value(
                    [row for row in rows if row.get("predicate") == predicate]
                )
                if text_predicates:
                    break
            for predicate in _RELATIONSHIP_PREDICATE_CONFIG_ALIASES:
                relationship_predicates = self._first_config_value(
                    [row for row in rows if row.get("predicate") == predicate]
                )
                if relationship_predicates:
                    break
            for predicate in _DISPLAY_ORDER_PREDICATE_ALIASES:
                display_order = self._first_display_order(
                    [row for row in rows if row.get("predicate") == predicate]
                )
                if display_order is not None:
                    break
            self._text_predicates_by_field[field_key] = text_predicates
            self._relationship_predicates_by_field[field_key] = (
                relationship_predicates
            )
            self._display_order_by_field[field_key] = display_order
            self._field_labels_by_field[field_key] = _field_label(
                field_docs_by_id.get(concept_id),
                field_key,
            )

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

    @staticmethod
    def _load_first_display_order(*, concept_id: str) -> float | None:
        for predicate in _DISPLAY_ORDER_PREDICATE_ALIASES:
            try:
                rows = get_texts_for_concept(
                    concept_id,
                    predicate=predicate,
                    limit=5,
                )
            except Exception as exc:
                _logger.debug(
                    "[summary_field_resolver] Failed to load display order for %s/%s: %s",
                    concept_id,
                    predicate,
                    exc,
                )
                continue
            value = ConceptSummaryFieldResolver._first_display_order(rows)
            if value is not None:
                return value
        return None

    @staticmethod
    def _first_display_order(rows: list[dict[str, Any]]) -> float | None:
        for row in rows:
            value = _normalise_display_order(row.get("text"))
            if value is not None:
                return value
        return None


_resolver: ConceptSummaryFieldResolver | None = None


def get_concept_summary_field_resolver() -> ConceptSummaryFieldResolver:
    global _resolver
    if _resolver is None:
        _resolver = ConceptSummaryFieldResolver()
    return _resolver


__all__ = [
    "SUMMARY_FIELD_DISPLAY_ORDER_PREDICATE",
    "SUMMARY_RELATIONSHIP_PREDICATE_CONFIG",
    "SUMMARY_TEXT_PREDICATE_CONFIG",
    "SUMMARY_TYPE_FIELD_RELATIONSHIP",
    "ConceptSummaryFieldResolver",
    "SummaryFieldDefinition",
    "get_concept_summary_field_resolver",
]
