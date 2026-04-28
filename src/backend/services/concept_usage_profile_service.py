"""Reusable concept-usage profile support.

This module deliberately reports grounded counters rather than deciding what
counts as "important" in a durable policy sense. Workflows and Vontology
profiles can supply thresholds and selection policy on top of these metrics.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..db.repositories.concepts_repository import ConceptsRepository
from ..vontology.utils_vontology import get_concept_display_name_with_names_fallback
from .concept_relation_service import find_relations_with_argument
from .text_value_service import get_texts_for_concept

_HIERARCHY_PREDICATES = {
    "is_a_type_of",
    "#V#is_a_type_of",
    "#v#is_a_type_of",
    "is_an_instance_of",
    "#V#is_an_instance_of",
    "#v#is_an_instance_of",
    "has_subtype",
    "#V#has_subtype",
    "#v#has_subtype",
    "has_instance",
    "#V#has_instance",
    "#v#has_instance",
}


def _normalise_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _coerce_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _target_count(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip() else 0
    if isinstance(value, list):
        return sum(_target_count(item) for item in value)
    return 0


def _predicate_is_hierarchy(predicate: Any) -> bool:
    text = _normalise_text(predicate)
    return text in _HIERARCHY_PREDICATES or text.lower() in _HIERARCHY_PREDICATES


def _normalise_predicate_name(predicate: Any) -> str:
    text = _normalise_text(predicate)
    if text.lower().startswith("#v#"):
        text = text[3:]
    return text.lower()


def _count_direct_relationships(
    relationships: Mapping[str, Any],
) -> dict[str, int]:
    hierarchy = 0
    non_hierarchy = 0
    for predicate, raw_value in relationships.items():
        count = _target_count(raw_value)
        if _predicate_is_hierarchy(predicate):
            hierarchy += count
        else:
            non_hierarchy += count
    return {
        "direct_hierarchy_relation_count": hierarchy,
        "direct_non_hierarchy_relation_count": non_hierarchy,
        "direct_structural_relation_count": hierarchy + non_hierarchy,
    }


def _summarise_relation_hits(hits: list[dict[str, Any]]) -> dict[str, int]:
    outgoing = 0
    incoming = 0
    hierarchy = 0
    non_hierarchy = 0
    for hit in hits:
        if not isinstance(hit, Mapping):
            continue
        argument_indexes = hit.get("argument_indexes")
        indexes = argument_indexes if isinstance(argument_indexes, list) else []
        if 1 in indexes:
            outgoing += 1
        else:
            incoming += 1
        predicate = hit.get("predicate_concept_id")
        if _predicate_is_hierarchy(predicate):
            hierarchy += 1
        else:
            non_hierarchy += 1
    return {
        "binary_outgoing_hits_returned": outgoing,
        "binary_incoming_hits_returned": incoming,
        "binary_hierarchy_hits_returned": hierarchy,
        "binary_non_hierarchy_hits_returned": non_hierarchy,
    }


def _summarise_text_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    languages: set[str] = set()
    predicates: set[str] = set()
    description_languages: set[str] = set()
    name_languages: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        lang = _normalise_text(row.get("lang"))
        predicate = _normalise_text(row.get("predicate"))
        if lang:
            languages.add(lang)
        if predicate:
            predicates.add(predicate)
        lowered = _normalise_predicate_name(predicate)
        if lowered == "hasdescription" and lang:
            description_languages.add(lang)
        elif lowered == "hasname" and lang:
            name_languages.add(lang)
    return {
        "text_relation_count": len(rows),
        "text_relation_predicate_count": len(predicates),
        "text_relation_language_count": len(languages),
        "text_relation_languages": sorted(languages),
        "description_languages": sorted(description_languages),
        "name_languages": sorted(name_languages),
    }


def build_concept_usage_profile(
    *,
    concept_id: str,
    concept_doc: Mapping[str, Any] | None = None,
    relation_limit: int = 200,
    text_limit: int = 200,
    minimum_total_usage: int | None = None,
) -> dict[str, Any]:
    """Return grounded usage counters for one concept.

    ``minimum_total_usage`` is optional and caller-supplied. When omitted, the
    payload contains no policy decision about whether usage is "non-trivial".
    """

    resolved_concept_id = _normalise_text(concept_id)
    if not resolved_concept_id:
        raise ValueError("concept_id is required")

    doc: Mapping[str, Any] | None = concept_doc
    if doc is None:
        loaded = ConceptsRepository.find_one({"concept_id": resolved_concept_id})
        doc = loaded if isinstance(loaded, Mapping) else None
    if doc is None:
        return {
            "success": False,
            "concept_id": resolved_concept_id,
            "usage_metrics": {},
            "error": "concept_not_found",
        }

    relationships = doc.get("relationships")
    relationship_map = relationships if isinstance(relationships, Mapping) else {}
    direct_counts = _count_direct_relationships(relationship_map)

    bounded_relation_limit = _coerce_int(
        relation_limit,
        default=200,
        minimum=1,
        maximum=1000,
    )
    bounded_text_limit = _coerce_int(
        text_limit,
        default=200,
        minimum=1,
        maximum=1000,
    )

    relation_payload: dict[str, Any] = {}
    relation_error: str | None = None
    try:
        relation_payload = find_relations_with_argument(
            resolved_concept_id,
            relation_kind="binary",
            include_concept_preview=False,
            limit=bounded_relation_limit,
        )
    except Exception as exc:
        relation_error = str(exc)
        relation_payload = {"total_hits": 0, "hits": []}

    hits = [
        item
        for item in (relation_payload.get("hits") or [])
        if isinstance(item, dict)
    ]
    hit_counts = _summarise_relation_hits(hits)

    text_rows: list[dict[str, Any]] = []
    text_error: str | None = None
    try:
        text_rows = [
            item
            for item in (
                get_texts_for_concept(resolved_concept_id, limit=bounded_text_limit)
                or []
            )
            if isinstance(item, dict)
        ]
    except Exception as exc:
        text_error = str(exc)
        text_rows = []
    text_counts = _summarise_text_rows(text_rows)

    try:
        display_name = get_concept_display_name_with_names_fallback(dict(doc))
    except Exception:
        display_name = _normalise_text(doc.get("name")) or resolved_concept_id

    binary_total = int(relation_payload.get("total_hits") or 0)
    text_total = int(text_counts["text_relation_count"])
    total_usage = binary_total + text_total
    minimum_usage = (
        _coerce_int(
            minimum_total_usage,
            default=0,
            minimum=0,
            maximum=1_000_000,
        )
        if minimum_total_usage is not None
        else None
    )

    usage_metrics = {
        **direct_counts,
        "binary_relation_hits_total": binary_total,
        "binary_relation_hits_returned": len(hits),
        **hit_counts,
        **text_counts,
        "total_usage_count": total_usage,
        "relation_limit": bounded_relation_limit,
        "text_limit": bounded_text_limit,
        "relation_hits_truncated": binary_total > len(hits),
        "text_relations_truncated": text_total >= bounded_text_limit,
    }

    return {
        "success": relation_error is None and text_error is None,
        "concept_id": resolved_concept_id,
        "display_name": display_name or resolved_concept_id,
        "usage_metrics": usage_metrics,
        "minimum_total_usage": minimum_usage,
        "meets_minimum_total_usage": (
            None if minimum_usage is None else total_usage >= minimum_usage
        ),
        "diagnostics": {
            "relation_error": relation_error,
            "text_error": text_error,
        },
    }


__all__ = ["build_concept_usage_profile"]
