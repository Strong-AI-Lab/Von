"""Helpers for assembling relation payloads for concept fetch requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .text_value_service import get_texts_for_concept
from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import (
    bypass_access_control,
    can_access_concept,
    should_enforce_access_control,
)
from .concept_predicate_metadata_service import get_relationship_kinds_set
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..vontology.utils_vontology import (
    build_pure_instance_query,
    get_concept_display_name_with_names_fallback,
    get_vontology_node_and_descendant_ids,
    is_predicate,
    is_type,
)

RelationValue = Dict[str, Any]

_ARG_INDEX_SUBJECT = 1  # Align with example payloads (1-based indexing)
_ARG_INDEX_FIRST_OBJECT = 2
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 500
_UNCERTAINTY_MODE_ASSERTED_ONLY = "asserted_only"
_UNCERTAINTY_MODE_UNCERTAIN_ONLY = "uncertain_only"
_UNCERTAINTY_MODE_INCLUDE_UNCERTAIN = "include_uncertain"
_PREVIEW_DOC_PROJECTION = {
    "concept_id": 1,
    "name": 1,
    "names": 1,
    "kind": 1,
    "updated_at": 1,
    "relationships.is_a_type_of": 1,
    "relationships.#V#is_a_type_of": 1,
    "relationships.is_an_instance_of": 1,
    "relationships.#V#is_an_instance_of": 1,
}

_UNCERTAINTY_RETRIEVAL_STATS: Dict[str, int] = {
    "payload_calls_total": 0,
    "find_calls_total": 0,
    "asserted_only_calls": 0,
    "uncertain_only_calls": 0,
    "include_uncertain_calls": 0,
    "uncertain_rows_returned": 0,
    "uncertainty_mode_errors": 0,
}


@dataclass
class RelationOptions:
    include_relations_arg1: bool = False
    include_relations_any_arg: bool = False
    include_text_relations_arg1: Union[bool, str] = False
    predicate_filter: Optional[Sequence[str]] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    include_concept_preview: bool = True
    include_uncertain: bool = False
    uncertainty_mode: Optional[str] = None
    uncertainty_statuses: Optional[Sequence[str]] = None


def build_concept_relations_payload(
    concept: Optional[Dict[str, Any]],
    *,
    include_relations_arg1: bool = False,
    include_relations_any_arg: bool = False,
    include_text_relations_arg1: Union[bool, str] = False,
    predicate_filter: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    include_concept_preview: bool = True,
    include_uncertain: bool = False,
    uncertainty_mode: Optional[str] = None,
    uncertainty_statuses: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Return a structured payload capturing relations connected to ``concept``.

    The payload mirrors the shape expected by JVNAUTOSCI-728 consumers:

    * ``relations_found`` – total matches before paging
    * ``relations`` – list of relation descriptors
    * ``paging`` – metadata describing applied limit/offset

    Args:
        concept: Concept document (already enriched) for the requested concept.
        include_relations_arg1: Include structural relations where the concept is
            the subject (argument-1).
        include_relations_any_arg: Include structural relations where the concept
            appears in any argument position (i.e., other concepts reference it).
        include_text_relations_arg1: Truthy to include text relations where the
            concept is the subject. The string value ``"snippets"`` enables
            snippet generation in addition to raw text.
        predicate_filter: Optional whitelist of predicate identifiers to keep.
        limit: Maximum number of relation entries to return after paging.
        offset: Offset applied before collecting results.
        include_concept_preview: Attach light-weight concept previews for related
            concepts when accessible.
    """

    concept_id = concept.get("concept_id") if concept else None
    if not concept_id:
        return _empty_payload(limit=limit, offset=offset)

    predicate_allow_list = set(predicate_filter or [])
    structural_predicates = set(get_relationship_kinds_set())
    if predicate_allow_list:
        structural_predicates.update(
            key for key in predicate_allow_list if isinstance(key, str)
        )
    include_text_relations = bool(include_text_relations_arg1)
    mode_value, include_asserted_rows, include_uncertain_rows = _resolve_uncertainty_mode(
        uncertainty_mode=uncertainty_mode,
        include_uncertain=include_uncertain,
    )
    status_filter = _normalise_uncertainty_statuses(uncertainty_statuses)
    _record_uncertainty_mode_usage(mode_value)
    snippet_mode = (
        str(include_text_relations_arg1).strip().lower()
        if isinstance(include_text_relations_arg1, str)
        else ""
    )

    resolved_limit = _coerce_limit(limit)
    resolved_offset = max(int(offset or 0), 0)

    results: List[RelationValue] = []
    total_matches = 0
    preview_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def _predicate_allowed(candidate: Optional[str]) -> bool:
        if not predicate_allow_list:
            return True
        if candidate is None:
            return False
        return candidate in predicate_allow_list

    def _record(entry: RelationValue) -> None:
        nonlocal total_matches
        total_matches += 1
        if total_matches <= resolved_offset:
            return
        if resolved_limit is not None and len(results) >= resolved_limit:
            return
        results.append(entry)

    if include_asserted_rows and include_relations_arg1:
        relationships = (concept.get("relationships") or {}) if concept else {}
        _collect_structural_relations_for_subject(
            concept_id=concept_id,
            relationships=relationships,
            predicate_allowed=_predicate_allowed,
            include_preview=include_concept_preview,
            preview_cache=preview_cache,
            consumer=_record,
        )

    if include_asserted_rows and include_relations_any_arg:
        _collect_structural_relations_for_targets(
            concept_id=concept_id,
            predicate_allowed=_predicate_allowed,
            structural_predicates=structural_predicates,
            include_preview=include_concept_preview,
            preview_cache=preview_cache,
            consumer=_record,
        )

    if include_asserted_rows and include_text_relations:
        _collect_text_relations_for_subject(
            concept_id=concept_id,
            predicate_allowed=_predicate_allowed,
            include_preview=include_concept_preview,
            preview_cache=preview_cache,
            snippet_mode=snippet_mode,
            consumer=_record,
        )

    if include_uncertain_rows:
        uncertain_total = _collect_uncertain_relations_for_subject(
            concept_id=concept_id,
            predicate_allowed=_predicate_allowed,
            include_preview=include_concept_preview,
            preview_cache=preview_cache,
            consumer=_record,
            status_filter=status_filter,
        )
        if include_relations_any_arg:
            uncertain_total += _collect_uncertain_relations_for_targets(
                concept_id=concept_id,
                predicate_allowed=_predicate_allowed,
                include_preview=include_concept_preview,
                preview_cache=preview_cache,
                consumer=_record,
                status_filter=status_filter,
            )
        _UNCERTAINTY_RETRIEVAL_STATS["uncertain_rows_returned"] += max(
            0, int(uncertain_total)
        )

    paging = {
        "limit": resolved_limit,
        "offset": resolved_offset,
        "returned": len(results),
        "total_available": total_matches,
    }
    return {
        "concept_id": concept_id,
        "relations_found": total_matches,
        "relations": results,
        "paging": paging,
        "uncertainty_diagnostics": {
            "mode": mode_value,
            "include_uncertain": include_uncertain_rows,
            "statuses": status_filter or None,
        },
    }


def find_relations_with_argument(
    concept_id: str,
    *,
    argument_index: Union[int, str, None] = None,
    predicate_filter: Optional[Sequence[str]] = None,
    relation_kind: Optional[str] = None,
    scope: Optional[str] = None,  # Accepted for caller consistency; currently unused.
    include_text_snippets: bool = False,
    include_concept_preview: bool = True,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    sort_by: Optional[str] = None,
    include_uncertain: bool = False,
    uncertainty_mode: Optional[str] = None,
    uncertainty_statuses: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Find relation hits where ``concept_id`` appears in any argument position.

    Structural relations use exact argument matching.
    Text relations include:
    - arg1 matches (subject concept equals ``concept_id``)
    - arg2 full-text matches where text contains ``concept_id``.
    """

    resolved_concept_id = str(concept_id or "").strip()
    if not resolved_concept_id:
        raise ValueError("concept_id is required")

    _ = scope  # Scope is enforced by repository-level access control.
    resolved_limit = _coerce_limit(limit)
    resolved_limit_value = (
        resolved_limit if resolved_limit is not None else _DEFAULT_LIMIT
    )
    resolved_offset = max(int(offset or 0), 0)
    argument_filter = _coerce_argument_index(argument_index)
    relation_filter = _normalise_relation_kind_filter(relation_kind)
    predicate_terms = _normalise_predicate_terms(predicate_filter)

    include_structural = relation_filter in {"any", "binary"}
    include_text = relation_filter in {"any", "text"}
    mode_value, include_asserted_rows, include_uncertain_rows = _resolve_uncertainty_mode(
        uncertainty_mode=uncertainty_mode,
        include_uncertain=include_uncertain,
    )
    status_filter = _normalise_uncertainty_statuses(uncertainty_statuses)
    _record_uncertainty_mode_usage(mode_value)
    _UNCERTAINTY_RETRIEVAL_STATS["find_calls_total"] += 1
    include_arg1 = argument_filter in (None, _ARG_INDEX_SUBJECT)
    include_arg2_or_later = argument_filter is None or argument_filter >= _ARG_INDEX_FIRST_OBJECT

    preview_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    hits: List[Dict[str, Any]] = []

    subject_doc = _load_accessible_relation_subject_document(resolved_concept_id)

    if include_asserted_rows and include_structural and subject_doc:
        relationships = (subject_doc.get("relationships") or {}) if subject_doc else {}
        source_updated_at = _isoformat(subject_doc.get("updated_at")) if subject_doc else None
        for predicate_id, raw_targets in relationships.items():
            if not _predicate_matches_terms(predicate_id, predicate_terms):
                continue
            targets = _normalise_relationship_targets(raw_targets)
            if not targets:
                continue
            source_preview = _resolve_concept_preview(
                resolved_concept_id,
                include_concept_preview,
                preview_cache,
            )
            for target_index, target_value in enumerate(targets):
                matched_indexes = [_ARG_INDEX_SUBJECT]
                arg_target_index = _ARG_INDEX_FIRST_OBJECT + target_index
                if target_value == resolved_concept_id:
                    matched_indexes.append(arg_target_index)
                if not _argument_indexes_match(matched_indexes, argument_filter):
                    continue
                target_preview = None
                if include_concept_preview and isinstance(target_value, str) and target_value.startswith("#V#"):
                    target_preview = _resolve_concept_preview(
                        target_value,
                        True,
                        preview_cache,
                    )
                hits.append(
                    {
                        "source_concept_id": resolved_concept_id,
                        "predicate_concept_id": predicate_id,
                        "relation_kind": "binary",
                        "argument_indexes": matched_indexes,
                        "target_value": target_value,
                        "target_concept_preview": target_preview,
                        "relation_metadata": {
                            "relation_id": f"struct::{resolved_concept_id}::{predicate_id}::{target_index}",
                            "updated_at": source_updated_at,
                            "match_type": "exact",
                        },
                        "access_granted": source_preview is not None or not include_concept_preview,
                        "follow_up_actions": _build_follow_up_actions(
                            [target_value],
                            exclude={resolved_concept_id},
                        ),
                        "score": 1.0,
                        "is_asserted": True,
                        "relation_state": "asserted",
                    }
                )

    if include_asserted_rows and include_structural and include_arg2_or_later:
        incoming_pipeline = [
            {"$match": {"relationships": {"$type": "object"}}},
            {
                "$project": {
                    "concept_id": 1,
                    "updated_at": 1,
                    "relationship_items": {"$objectToArray": "$relationships"},
                }
            },
            {"$unwind": "$relationship_items"},
            {
                "$project": {
                    "concept_id": 1,
                    "updated_at": 1,
                    "predicate": "$relationship_items.k",
                    "targets": "$relationship_items.v",
                }
            },
            {"$match": {"targets": resolved_concept_id}},
        ]
        for row in ConceptsRepository.aggregate(incoming_pipeline):
            source_id = row.get("concept_id")
            if not isinstance(source_id, str) or not source_id.strip():
                continue
            source_id = source_id.strip()
            predicate_id = row.get("predicate")
            if not _predicate_matches_terms(predicate_id, predicate_terms):
                continue
            targets = _normalise_relationship_targets(row.get("targets"))
            if not targets:
                continue
            source_preview = _resolve_concept_preview(
                source_id,
                include_concept_preview,
                preview_cache,
            )
            updated_at = _isoformat(row.get("updated_at"))
            for target_index, target_value in enumerate(targets):
                if target_value != resolved_concept_id:
                    continue
                matched_indexes = [_ARG_INDEX_FIRST_OBJECT + target_index]
                if not _argument_indexes_match(matched_indexes, argument_filter):
                    continue
                hits.append(
                    {
                        "source_concept_id": source_id,
                        "predicate_concept_id": predicate_id,
                        "relation_kind": "binary",
                        "argument_indexes": matched_indexes,
                        "target_value": target_value,
                        "target_concept_preview": (
                            _resolve_concept_preview(
                                resolved_concept_id,
                                True,
                                preview_cache,
                            )
                            if include_concept_preview
                            else None
                        ),
                        "relation_metadata": {
                            "relation_id": f"struct::{source_id}::{predicate_id}::incoming::{target_index}",
                            "updated_at": updated_at,
                            "match_type": "exact",
                        },
                        "access_granted": source_preview is not None or not include_concept_preview,
                        "follow_up_actions": _build_follow_up_actions(
                            [source_id],
                            exclude={resolved_concept_id},
                        ),
                        "score": 1.0,
                        "is_asserted": True,
                        "relation_state": "asserted",
                    }
                )

    if include_asserted_rows and include_text and include_arg1:
        source_preview = _resolve_concept_preview(
            resolved_concept_id,
            include_concept_preview,
            preview_cache,
        )
        for rel in get_texts_for_concept(
            subject_concept_id=resolved_concept_id,
            limit=_MAX_LIMIT,
        ):
            predicate_id = rel.get("predicate")
            if not _predicate_matches_terms(predicate_id, predicate_terms):
                continue
            text_value = rel.get("text")
            hit: Dict[str, Any] = {
                "source_concept_id": resolved_concept_id,
                "predicate_concept_id": predicate_id,
                "relation_kind": "text",
                "argument_indexes": [_ARG_INDEX_SUBJECT],
                "target_value": text_value,
                "relation_metadata": {
                    "relation_id": str(
                        rel.get("relation_id")
                        or f"text::{resolved_concept_id}::{predicate_id}"
                    ),
                    "text_value_id": rel.get("text_value_id"),
                    "lang": rel.get("lang"),
                    "match_type": "exact",
                },
                "access_granted": source_preview is not None or not include_concept_preview,
                "follow_up_actions": [],
                "score": 1.0,
                "is_asserted": True,
                "relation_state": "asserted",
            }
            snippet = _make_argument_match_snippet(
                text=text_value,
                needle=resolved_concept_id,
                include_snippets=include_text_snippets,
            )
            if snippet:
                hit["text_snippet"] = snippet
            hits.append(hit)

    if include_asserted_rows and include_text and include_arg2_or_later:
        escaped = re.escape(resolved_concept_id)
        text_values = list(
            TextValuesRepository.find(
                {"text": {"$regex": escaped, "$options": "i"}},
                {"_id": 1, "text": 1},
            )
        )
        text_ids: List[str] = []
        text_by_id: Dict[str, str] = {}
        for doc in text_values:
            text_id = str(doc.get("_id"))
            text_ids.append(text_id)
            text_by_id[text_id] = str(doc.get("text") or "")
        if text_ids:
            for rel in TextRelationsRepository.find({"object_text_id": {"$in": text_ids}}):
                source_id = rel.get("subject_concept_id")
                if not isinstance(source_id, str) or not source_id.strip():
                    continue
                source_id = source_id.strip()
                predicate_id = rel.get("predicate")
                if not _predicate_matches_terms(predicate_id, predicate_terms):
                    continue
                if not _argument_indexes_match([_ARG_INDEX_FIRST_OBJECT], argument_filter):
                    continue
                text_id = str(rel.get("object_text_id") or "")
                text_value = text_by_id.get(text_id)
                if text_value is None:
                    continue
                source_preview = _resolve_concept_preview(
                    source_id,
                    include_concept_preview,
                    preview_cache,
                )
                if include_concept_preview and source_preview is None:
                    continue
                snippet = _make_argument_match_snippet(
                    text=text_value,
                    needle=resolved_concept_id,
                    include_snippets=include_text_snippets,
                )
                hit: Dict[str, Any] = {
                    "source_concept_id": source_id,
                    "predicate_concept_id": predicate_id,
                    "relation_kind": "text",
                    "argument_indexes": [_ARG_INDEX_FIRST_OBJECT],
                    "target_value": resolved_concept_id,
                    "relation_metadata": {
                        "relation_id": str(
                            rel.get("relation_id")
                            or f"text::{source_id}::{predicate_id}::{text_id}"
                        ),
                        "text_value_id": text_id,
                        "lang": rel.get("lang"),
                        "match_type": "full_text",
                    },
                    "access_granted": source_preview is not None or not include_concept_preview,
                    "follow_up_actions": _build_follow_up_actions(
                        [source_id],
                        exclude={resolved_concept_id},
                    ),
                    "score": _compute_text_match_score(text_value, resolved_concept_id),
                    "is_asserted": True,
                    "relation_state": "asserted",
                }
                if include_concept_preview:
                    hit["target_concept_preview"] = _resolve_concept_preview(
                        resolved_concept_id,
                        True,
                        preview_cache,
                    )
                if snippet:
                    hit["text_snippet"] = snippet
                hits.append(hit)

    if include_uncertain_rows:
        hits.extend(
            _collect_uncertain_argument_hits_for_subject(
                concept_id=resolved_concept_id,
                argument_filter=argument_filter,
                relation_filter=relation_filter,
                predicate_terms=predicate_terms,
                include_concept_preview=include_concept_preview,
                preview_cache=preview_cache,
                status_filter=status_filter,
            )
        )
        if include_arg2_or_later:
            hits.extend(
                _collect_uncertain_argument_hits_for_targets(
                    concept_id=resolved_concept_id,
                    argument_filter=argument_filter,
                    relation_filter=relation_filter,
                    predicate_terms=predicate_terms,
                    include_concept_preview=include_concept_preview,
                    preview_cache=preview_cache,
                    status_filter=status_filter,
                )
            )

    deduped_hits = _dedupe_argument_hits(hits)
    sorted_hits = _sort_argument_hits(deduped_hits, sort_by)
    paged_hits = sorted_hits[
        resolved_offset : resolved_offset + resolved_limit_value
    ]
    return {
        "concept_id": resolved_concept_id,
        "total_hits": len(sorted_hits),
        "hits": paged_hits,
        "paging": {
            "limit": resolved_limit_value,
            "offset": resolved_offset,
            "returned": len(paged_hits),
            "total_available": len(sorted_hits),
        },
        "uncertainty_diagnostics": {
            "mode": mode_value,
            "include_uncertain": include_uncertain_rows,
            "statuses": status_filter or None,
        },
    }


def get_predicate_incidence(
    *,
    concept_id: Optional[str] = None,
    instance_of: Optional[str] = None,
    direct_instances_only: bool = False,
    argument_index: Union[int, str, None] = None,
    predicate_filter: Optional[Sequence[str]] = None,
    relation_kind: Optional[str] = None,
    scope: Optional[str] = None,  # Accepted for caller consistency; currently unused.
    include_text_snippets: bool = False,
    include_concept_preview: bool = True,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    sort_by: Optional[str] = None,
    include_uncertain: bool = False,
    uncertainty_mode: Optional[str] = None,
    uncertainty_statuses: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Summarise distinct predicates observed for an entity or a type's instances.

    Exactly one of ``concept_id`` (entity mode) or ``instance_of`` (type mode)
    must be provided. The returned payload groups relation hits by predicate and
    records useful counts such as relation-hit count, distinct grounding count,
    and, for type mode, the number of distinct instances contributing evidence.
    """

    resolved_concept_id = str(concept_id or "").strip()
    resolved_instance_of = str(instance_of or "").strip()
    if bool(resolved_concept_id) == bool(resolved_instance_of):
        raise ValueError("Exactly one of concept_id or instance_of is required")

    _ = scope  # Scope is enforced by repository-level access control.
    resolved_limit = _coerce_limit(limit)
    resolved_limit_value = (
        resolved_limit if resolved_limit is not None else _DEFAULT_LIMIT
    )
    resolved_offset = max(int(offset or 0), 0)
    resolved_sort_by = _normalise_predicate_incidence_sort(sort_by)
    preview_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    if resolved_concept_id:
        hits, diagnostics = _collect_all_argument_hits_for_incidence(
            concept_id=resolved_concept_id,
            argument_index=argument_index,
            predicate_filter=predicate_filter,
            relation_kind=relation_kind,
            include_text_snippets=include_text_snippets,
            include_concept_preview=include_concept_preview,
            include_uncertain=include_uncertain,
            uncertainty_mode=uncertainty_mode,
            uncertainty_statuses=uncertainty_statuses,
        )
        predicate_rows = _aggregate_predicate_incidence_rows(
            hits=hits,
            include_concept_preview=include_concept_preview,
            preview_cache=preview_cache,
            anchor_concept_id=resolved_concept_id,
            mode="entity",
            type_membership_ids=None,
        )
        sorted_rows = _sort_predicate_incidence_rows(
            predicate_rows,
            sort_by=resolved_sort_by,
        )
        paged_rows = sorted_rows[
            resolved_offset : resolved_offset + resolved_limit_value
        ]
        return {
            "mode": "entity",
            "concept_id": resolved_concept_id,
            "total_predicates": len(sorted_rows),
            "predicates": paged_rows,
            "paging": {
                "limit": resolved_limit_value,
                "offset": resolved_offset,
                "returned": len(paged_rows),
                "total_available": len(sorted_rows),
            },
            "uncertainty_diagnostics": diagnostics,
        }

    type_ids = _resolve_type_incidence_type_ids(
        resolved_instance_of,
        direct_instances_only=direct_instances_only,
    )
    instance_ids = _resolve_type_incidence_instance_ids(type_ids)
    aggregated_by_predicate: Dict[str, Dict[str, Any]] = {}
    diagnostics = {
        "mode": (
            str(uncertainty_mode).strip().lower()
            if isinstance(uncertainty_mode, str) and str(uncertainty_mode).strip()
            else (
                _UNCERTAINTY_MODE_INCLUDE_UNCERTAIN
                if include_uncertain
                else _UNCERTAINTY_MODE_ASSERTED_ONLY
            )
        ),
        "include_uncertain": bool(
            include_uncertain
            or (
                isinstance(uncertainty_mode, str)
                and str(uncertainty_mode).strip().lower()
                != _UNCERTAINTY_MODE_ASSERTED_ONLY
            )
        ),
        "statuses": _normalise_uncertainty_statuses(uncertainty_statuses) or None,
    }

    for instance_id in instance_ids:
        hits, _ = _collect_all_argument_hits_for_incidence(
            concept_id=instance_id,
            argument_index=argument_index,
            predicate_filter=predicate_filter,
            relation_kind=relation_kind,
            include_text_snippets=include_text_snippets,
            include_concept_preview=include_concept_preview,
            include_uncertain=include_uncertain,
            uncertainty_mode=uncertainty_mode,
            uncertainty_statuses=uncertainty_statuses,
        )
        _accumulate_predicate_incidence_rows(
            aggregated_by_predicate,
            hits=hits,
            include_concept_preview=include_concept_preview,
            preview_cache=preview_cache,
            anchor_concept_id=instance_id,
            mode="type",
            contributing_instance_id=instance_id,
            type_membership_ids=type_ids,
        )

    sorted_rows = _sort_predicate_incidence_rows(
        [
            _finalise_predicate_incidence_row(row, mode="type")
            for row in aggregated_by_predicate.values()
        ],
        sort_by=resolved_sort_by,
    )
    paged_rows = sorted_rows[resolved_offset : resolved_offset + resolved_limit_value]
    return {
        "mode": "type",
        "instance_of": resolved_instance_of,
        "type_ids_considered": type_ids,
        "instance_count_considered": len(instance_ids),
        "direct_instances_only": bool(direct_instances_only),
        "total_predicates": len(sorted_rows),
        "predicates": paged_rows,
        "paging": {
            "limit": resolved_limit_value,
            "offset": resolved_offset,
            "returned": len(paged_rows),
            "total_available": len(sorted_rows),
        },
        "uncertainty_diagnostics": diagnostics,
    }


def _empty_payload(limit: Optional[int], offset: Optional[int]) -> Dict[str, Any]:
    return {
        "concept_id": None,
        "relations_found": 0,
        "relations": [],
        "paging": {
            "limit": _coerce_limit(limit),
            "offset": max(int(offset or 0), 0),
            "returned": 0,
            "total_available": 0,
        },
    }


def _collect_all_argument_hits_for_incidence(
    *,
    concept_id: str,
    argument_index: Union[int, str, None],
    predicate_filter: Optional[Sequence[str]],
    relation_kind: Optional[str],
    include_text_snippets: bool,
    include_concept_preview: bool,
    include_uncertain: bool,
    uncertainty_mode: Optional[str],
    uncertainty_statuses: Optional[Sequence[str]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    batch_size = _MAX_LIMIT
    offset = 0
    all_hits: List[Dict[str, Any]] = []
    diagnostics: Dict[str, Any] = {
        "mode": (
            str(uncertainty_mode).strip().lower()
            if isinstance(uncertainty_mode, str) and str(uncertainty_mode).strip()
            else (
                _UNCERTAINTY_MODE_INCLUDE_UNCERTAIN
                if include_uncertain
                else _UNCERTAINTY_MODE_ASSERTED_ONLY
            )
        ),
        "include_uncertain": bool(
            include_uncertain
            or (
                isinstance(uncertainty_mode, str)
                and str(uncertainty_mode).strip().lower()
                != _UNCERTAINTY_MODE_ASSERTED_ONLY
            )
        ),
        "statuses": _normalise_uncertainty_statuses(uncertainty_statuses) or None,
    }

    while True:
        payload = find_relations_with_argument(
            concept_id=concept_id,
            argument_index=argument_index,
            predicate_filter=predicate_filter,
            relation_kind=relation_kind,
            include_text_snippets=include_text_snippets,
            include_concept_preview=include_concept_preview,
            limit=batch_size,
            offset=offset,
            include_uncertain=include_uncertain,
            uncertainty_mode=uncertainty_mode,
            uncertainty_statuses=uncertainty_statuses,
        )
        if isinstance(payload.get("uncertainty_diagnostics"), dict):
            diagnostics = dict(payload["uncertainty_diagnostics"])
        batch_hits = payload.get("hits")
        if not isinstance(batch_hits, list) or not batch_hits:
            break
        materialised_hits = [
            dict(hit)
            for hit in batch_hits
            if isinstance(hit, Mapping)
        ]
        all_hits.extend(materialised_hits)
        paging = payload.get("paging")
        total_available_value = (
            paging.get("total_available") if isinstance(paging, Mapping) else None
        )
        total_available = (
            int(total_available_value)
            if isinstance(total_available_value, (int, float))
            else len(all_hits)
        )
        returned = len(materialised_hits)
        offset += returned
        if returned <= 0 or offset >= total_available:
            break

    return all_hits, diagnostics


def _resolve_type_incidence_type_ids(
    instance_of: str,
    *,
    direct_instances_only: bool,
) -> List[str]:
    resolved_instance_of = str(instance_of or "").strip()
    if not resolved_instance_of:
        return []
    if direct_instances_only:
        return [resolved_instance_of]
    descendant_ids = get_vontology_node_and_descendant_ids(resolved_instance_of) or []
    if resolved_instance_of not in descendant_ids:
        descendant_ids.append(resolved_instance_of)
    return [
        concept_id
        for concept_id in dict.fromkeys(descendant_ids)
        if isinstance(concept_id, str) and concept_id.strip()
    ]


def _resolve_type_incidence_instance_ids(type_ids: Sequence[str]) -> List[str]:
    if not type_ids:
        return []
    cursor = ConceptsRepository.find(
        build_pure_instance_query(instance_of_any=type_ids),
        {"concept_id": 1},
    )
    instance_ids: List[str] = []
    for doc in cursor:
        concept_id = doc.get("concept_id")
        if not isinstance(concept_id, str) or not concept_id.strip():
            continue
        instance_ids.append(concept_id.strip())
    return list(dict.fromkeys(sorted(instance_ids)))


def _aggregate_predicate_incidence_rows(
    *,
    hits: Sequence[Mapping[str, Any]],
    include_concept_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    anchor_concept_id: str,
    mode: str,
    type_membership_ids: Sequence[str] | None,
) -> List[Dict[str, Any]]:
    aggregated_by_predicate: Dict[str, Dict[str, Any]] = {}
    _accumulate_predicate_incidence_rows(
        aggregated_by_predicate,
        hits=hits,
        include_concept_preview=include_concept_preview,
        preview_cache=preview_cache,
        anchor_concept_id=anchor_concept_id,
        mode=mode,
        contributing_instance_id=(anchor_concept_id if mode == "type" else None),
        type_membership_ids=type_membership_ids,
    )
    return [
        _finalise_predicate_incidence_row(row, mode=mode)
        for row in aggregated_by_predicate.values()
    ]


def _accumulate_predicate_incidence_rows(
    aggregated_by_predicate: Dict[str, Dict[str, Any]],
    *,
    hits: Sequence[Mapping[str, Any]],
    include_concept_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    anchor_concept_id: str,
    mode: str,
    contributing_instance_id: Optional[str],
    type_membership_ids: Sequence[str] | None,
) -> None:
    for hit in hits:
        predicate_id = hit.get("predicate_concept_id")
        if not isinstance(predicate_id, str) or not predicate_id.strip():
            continue
        resolved_predicate_id = predicate_id.strip()
        if _predicate_incidence_is_type_membership_hit(
            hit,
            predicate_concept_id=resolved_predicate_id,
            type_membership_ids=type_membership_ids,
        ):
            continue
        row = aggregated_by_predicate.get(resolved_predicate_id)
        if row is None:
            row = _initialise_predicate_incidence_row(
                resolved_predicate_id,
                include_concept_preview=include_concept_preview,
                preview_cache=preview_cache,
            )
            aggregated_by_predicate[resolved_predicate_id] = row

        row["relation_hit_count"] += 1
        relation_kind = hit.get("relation_kind")
        if isinstance(relation_kind, str):
            lowered_kind = relation_kind.strip().lower()
            if lowered_kind == "text":
                row["text_relation_hit_count"] += 1
            elif lowered_kind == "binary":
                row["binary_relation_hit_count"] += 1

        raw_indexes = hit.get("argument_indexes")
        indexes = (
            [
                int(index)
                for index in raw_indexes
                if isinstance(index, (int, float))
            ]
            if isinstance(raw_indexes, list)
            else []
        )
        row["_argument_indexes"].update(indexes)
        if _ARG_INDEX_SUBJECT in indexes:
            row["subject_argument_hit_count"] += 1
        if any(index >= _ARG_INDEX_FIRST_OBJECT for index in indexes):
            row["object_argument_hit_count"] += 1

        for grounding_key, grounding in _extract_predicate_incidence_groundings(
            hit,
            anchor_concept_id=anchor_concept_id,
            include_concept_preview=include_concept_preview,
            preview_cache=preview_cache,
        ):
            if grounding_key in row["_grounding_keys"]:
                continue
            row["_grounding_keys"].add(grounding_key)
            if len(row["sample_groundings"]) < 4:
                row["sample_groundings"].append(grounding)

        if mode == "type" and contributing_instance_id:
            row["_instance_ids"].add(contributing_instance_id)
            if len(row["sample_instances"]) < 4:
                preview = _resolve_concept_preview(
                    contributing_instance_id,
                    include_concept_preview,
                    preview_cache,
                )
                sample: Dict[str, Any] = {"concept_id": contributing_instance_id}
                if isinstance(preview, Mapping):
                    name = preview.get("name")
                    kind = preview.get("kind")
                    type_ids = preview.get("type_ids")
                    if isinstance(name, str) and name.strip():
                        sample["name"] = name.strip()
                    if isinstance(kind, str) and kind.strip():
                        sample["kind"] = kind.strip()
                    if isinstance(type_ids, list):
                        clean_type_ids = [
                            str(type_id).strip()
                            for type_id in type_ids[:6]
                            if isinstance(type_id, str) and str(type_id).strip()
                        ]
                        if clean_type_ids:
                            sample["type_ids"] = clean_type_ids
                if sample not in row["sample_instances"]:
                    row["sample_instances"].append(sample)


def _predicate_incidence_is_type_membership_hit(
    hit: Mapping[str, Any],
    *,
    predicate_concept_id: str,
    type_membership_ids: Sequence[str] | None,
) -> bool:
    if not type_membership_ids:
        return False
    lowered_predicate_id = predicate_concept_id.strip().lower()
    if lowered_predicate_id not in {"is_an_instance_of", "#v#is_an_instance_of"}:
        return False
    target_concept_id = _extract_preview_concept_id(hit.get("target_concept_preview"))
    if target_concept_id is None:
        target_concept_id = _normalise_concept_id(hit.get("target_value"))
    if not target_concept_id:
        return False
    return target_concept_id.lower() in {
        str(type_id).strip().lower()
        for type_id in type_membership_ids
        if isinstance(type_id, str) and str(type_id).strip()
    }


def _initialise_predicate_incidence_row(
    predicate_concept_id: str,
    *,
    include_concept_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "predicate_concept_id": predicate_concept_id,
        "relation_hit_count": 0,
        "binary_relation_hit_count": 0,
        "text_relation_hit_count": 0,
        "subject_argument_hit_count": 0,
        "object_argument_hit_count": 0,
        "sample_groundings": [],
        "sample_instances": [],
        "_argument_indexes": set(),
        "_grounding_keys": set(),
        "_instance_ids": set(),
    }
    predicate_preview = _resolve_concept_preview(
        predicate_concept_id,
        include_concept_preview,
        preview_cache,
    )
    if predicate_preview is not None:
        row["predicate_preview"] = predicate_preview
    return row


def _extract_predicate_incidence_groundings(
    hit: Mapping[str, Any],
    *,
    anchor_concept_id: str,
    include_concept_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
) -> List[Tuple[str, Dict[str, Any]]]:
    candidates: List[Tuple[str, Dict[str, Any]]] = []

    source_concept_id = hit.get("source_concept_id")
    if (
        isinstance(source_concept_id, str)
        and source_concept_id.strip()
        and source_concept_id.strip() != anchor_concept_id
    ):
        resolved_source_id = source_concept_id.strip()
        source_preview = hit.get("source_concept_preview")
        if not isinstance(source_preview, Mapping):
            source_preview = _resolve_concept_preview(
                resolved_source_id,
                include_concept_preview,
                preview_cache,
            )
        grounding: Dict[str, Any] = {
            "grounding_kind": "concept",
            "concept_id": resolved_source_id,
        }
        if isinstance(source_preview, Mapping):
            name = source_preview.get("name")
            kind = source_preview.get("kind")
            type_ids = source_preview.get("type_ids")
            if isinstance(name, str) and name.strip():
                grounding["name"] = name.strip()
            if isinstance(kind, str) and kind.strip():
                grounding["kind"] = kind.strip()
            if isinstance(type_ids, list):
                clean_type_ids = [
                    str(type_id).strip()
                    for type_id in type_ids[:6]
                    if isinstance(type_id, str) and str(type_id).strip()
                ]
                if clean_type_ids:
                    grounding["type_ids"] = clean_type_ids
        candidates.append((f"concept::{resolved_source_id}", grounding))

    target_preview = hit.get("target_concept_preview")
    target_concept_id = (
        _extract_preview_concept_id(target_preview)
        or _normalise_concept_id(hit.get("target_value"))
    )
    if target_concept_id and target_concept_id != anchor_concept_id:
        resolved_target_preview = target_preview
        if not isinstance(resolved_target_preview, Mapping):
            resolved_target_preview = _resolve_concept_preview(
                target_concept_id,
                include_concept_preview,
                preview_cache,
            )
        grounding: Dict[str, Any] = {
            "grounding_kind": "concept",
            "concept_id": target_concept_id,
        }
        if isinstance(resolved_target_preview, Mapping):
            name = resolved_target_preview.get("name")
            kind = resolved_target_preview.get("kind")
            type_ids = resolved_target_preview.get("type_ids")
            if isinstance(name, str) and name.strip():
                grounding["name"] = name.strip()
            if isinstance(kind, str) and kind.strip():
                grounding["kind"] = kind.strip()
            if isinstance(type_ids, list):
                clean_type_ids = [
                    str(type_id).strip()
                    for type_id in type_ids[:6]
                    if isinstance(type_id, str) and str(type_id).strip()
                ]
                if clean_type_ids:
                    grounding["type_ids"] = clean_type_ids
        candidates.append((f"concept::{target_concept_id}", grounding))
        return candidates

    target_value = hit.get("target_value")
    text_value = None
    if isinstance(target_value, str) and target_value.strip() and not target_value.startswith("#V#"):
        text_value = target_value.strip()
    text_snippet = hit.get("text_snippet")
    if isinstance(text_snippet, str) and text_snippet.strip():
        text_value = text_snippet.strip()
    if text_value:
        compact_text = text_value[:180]
        candidates.append(
            (
                f"text::{compact_text}",
                {
                    "grounding_kind": "text",
                    "text_preview": compact_text,
                },
            )
        )

    return candidates


def _extract_preview_concept_id(preview: Any) -> Optional[str]:
    if not isinstance(preview, Mapping):
        return None
    concept_id = preview.get("concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return None
    return concept_id.strip()


def _normalise_concept_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or not cleaned.startswith("#V#"):
        return None
    return cleaned


def _finalise_predicate_incidence_row(
    row: Dict[str, Any],
    *,
    mode: str,
) -> Dict[str, Any]:
    final_row = {
        key: value
        for key, value in row.items()
        if not key.startswith("_")
    }
    final_row["grounding_count"] = len(row.get("_grounding_keys") or ())
    argument_indexes = row.get("_argument_indexes") or set()
    final_row["argument_indexes"] = sorted(
        int(index) for index in argument_indexes if isinstance(index, int)
    )
    if mode == "type":
        final_row["grounded_instance_count"] = len(row.get("_instance_ids") or ())
    return final_row


def _sort_predicate_incidence_rows(
    rows: Sequence[Dict[str, Any]],
    *,
    sort_by: str,
) -> List[Dict[str, Any]]:
    def _predicate_key(row: Mapping[str, Any]) -> str:
        predicate_id = row.get("predicate_concept_id")
        if isinstance(predicate_id, str) and predicate_id.strip():
            return predicate_id.strip().lower()
        return ""

    def _int_value(row: Mapping[str, Any], field: str) -> int:
        value = row.get(field)
        if isinstance(value, (int, float)):
            return int(value)
        return 0

    if sort_by == "predicate":
        return sorted(rows, key=_predicate_key)
    if sort_by == "grounding_count":
        return sorted(
            rows,
            key=lambda row: (
                -_int_value(row, "grounding_count"),
                -_int_value(row, "relation_hit_count"),
                _predicate_key(row),
            ),
        )
    if sort_by == "grounded_instance_count":
        return sorted(
            rows,
            key=lambda row: (
                -_int_value(row, "grounded_instance_count"),
                -_int_value(row, "relation_hit_count"),
                _predicate_key(row),
            ),
        )
    return sorted(
        rows,
        key=lambda row: (
            -_int_value(row, "relation_hit_count"),
            -_int_value(row, "grounding_count"),
            -_int_value(row, "grounded_instance_count"),
            _predicate_key(row),
        ),
    )


def _normalise_predicate_incidence_sort(raw: Optional[str]) -> str:
    value = str(raw or "relation_hit_count").strip().lower()
    if value in {
        "relation_hit_count",
        "grounding_count",
        "grounded_instance_count",
        "predicate",
    }:
        return value
    return "relation_hit_count"


def _coerce_limit(limit: Optional[int]) -> Optional[int]:
    if limit is None:
        return _DEFAULT_LIMIT
    try:
        numeric = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    if numeric <= 0:
        return 0
    return min(numeric, _MAX_LIMIT)


def _collect_structural_relations_for_subject(
    *,
    concept_id: str,
    relationships: Dict[str, Any],
    predicate_allowed,
    include_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    consumer,
) -> None:
    for rel_kind, raw_targets in relationships.items():
        targets = _normalise_relationship_targets(raw_targets)
        if not targets or not predicate_allowed(rel_kind):
            continue
        entry = _make_structural_entry(
            relation_id=f"struct::{concept_id}::{rel_kind}",
            source_id=concept_id,
            predicate_id=rel_kind,
            targets=targets,
            matched_indexes=[_ARG_INDEX_SUBJECT],
            include_preview=include_preview,
            preview_cache=preview_cache,
            primary_target=None,
        )
        entry["follow_up_actions"] = _build_follow_up_actions(
            targets, exclude={concept_id}
        )
        consumer(entry)


def _collect_structural_relations_for_targets(
    *,
    concept_id: str,
    predicate_allowed,
    structural_predicates: Iterable[str],
    include_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    consumer,
) -> None:
    for rel_kind in structural_predicates:
        query = {f"relationships.{rel_kind}": concept_id}
        projection = {
            "concept_id": 1,
            "name": 1,
            f"relationships.{rel_kind}": 1,
            "relationships.is_a_type_of": 1,
            "relationships.is_an_instance_of": 1,
            "updated_at": 1,
        }
        cursor = ConceptsRepository.find(query, projection=projection)
        for doc in cursor:
            source_id = doc.get("concept_id")
            targets = _normalise_relationship_targets(
                (doc.get("relationships") or {}).get(rel_kind)
            )
            if not targets:
                continue
            positions = _match_indexes_for_target(concept_id, targets)
            if not positions or not predicate_allowed(rel_kind):
                continue
            entry = _make_structural_entry(
                relation_id=f"struct::{source_id}::{rel_kind}::{concept_id}",
                source_id=source_id,
                predicate_id=rel_kind,
                targets=targets,
                matched_indexes=positions,
                include_preview=include_preview,
                preview_cache=preview_cache,
                primary_target=concept_id,
            )
            follow_up_sources: List[str] = (
                [source_id] if isinstance(source_id, str) else []
            )
            entry["follow_up_actions"] = _build_follow_up_actions(
                follow_up_sources,
                exclude={concept_id},
            )
            entry["access_granted"] = entry.get("source_preview") is not None
            consumer(entry)


def _collect_text_relations_for_subject(
    *,
    concept_id: str,
    predicate_allowed,
    include_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    snippet_mode: str,
    consumer,
) -> None:
    text_relations = get_texts_for_concept(
        subject_concept_id=concept_id, limit=_MAX_LIMIT
    )
    for rel in text_relations:
        predicate_id = rel.get("predicate")
        if not predicate_allowed(predicate_id):
            continue
        text_value = rel.get("text")
        entry: RelationValue = {
            "relation_id": str(
                rel.get("relation_id") or f"text::{concept_id}::{predicate_id}"
            ),
            "source_concept_id": concept_id,
            "predicate_id": predicate_id,
            "relation_kind": "text",
            "matched_argument_indexes": [_ARG_INDEX_SUBJECT],
            "target_values": [text_value] if text_value is not None else [],
            "text_value": {
                "text": text_value,
                "lang": rel.get("lang"),
                "text_value_id": rel.get("text_value_id"),
                "context": rel.get("context", {}),
            },
            "follow_up_actions": [],
            "access_granted": True,
            "is_asserted": True,
            "relation_state": "asserted",
        }
        if include_preview:
            entry["source_preview"] = _resolve_concept_preview(
                concept_id,
                include_preview,
                preview_cache,
            )
        snippet = _make_snippet(text_value, snippet_mode)
        if snippet:
            entry["text_snippet"] = snippet
        consumer(entry)


def _make_structural_entry(
    *,
    relation_id: str,
    source_id: Optional[str],
    predicate_id: Optional[str],
    targets: List[str],
    matched_indexes: List[int],
    include_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    primary_target: Optional[str],
) -> RelationValue:
    entry: RelationValue = {
        "relation_id": relation_id,
        "source_concept_id": source_id,
        "predicate_id": predicate_id,
        "relation_kind": "binary",
        "matched_argument_indexes": matched_indexes,
        "target_values": targets,
        "follow_up_actions": [],
        "access_granted": True,
        "is_asserted": True,
        "relation_state": "asserted",
    }
    if include_preview and source_id:
        entry["source_preview"] = _resolve_concept_preview(
            source_id,
            include_preview,
            preview_cache,
        )
        entry["access_granted"] = entry["source_preview"] is not None
    target_preview_map: Dict[str, Dict[str, Any]] = {}

    if include_preview:
        for candidate_target in targets:
            if not isinstance(candidate_target, str):
                continue
            preview = _resolve_concept_preview(candidate_target, True, preview_cache)
            if preview is not None:
                target_preview_map[candidate_target] = preview

    if include_preview and primary_target:
        key = f"target_preview::{primary_target}"
        preview = preview_cache.get(key)
        if preview is None:
            preview_cache[key] = _resolve_concept_preview(
                primary_target,
                include_preview,
                preview_cache,
            )
            preview = preview_cache[key]
        if preview is not None:
            target_preview_map[primary_target] = preview

    if target_preview_map:
        entry["target_previews"] = target_preview_map
    return entry


def _resolve_concept_preview(
    concept_id: str,
    include_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    if not include_preview:
        return None
    if concept_id in preview_cache:
        return preview_cache[concept_id]
    doc = _load_accessible_preview_document(concept_id)
    if not doc:
        preview_cache[concept_id] = None
        return None
    preview = _build_concept_preview_from_document(doc)
    preview_cache[concept_id] = preview
    return preview


def _load_accessible_preview_document(concept_id: str) -> Optional[Dict[str, Any]]:
    if should_enforce_access_control() and not can_access_concept(concept_id):
        return None
    with bypass_access_control():
        doc = ConceptsRepository.find_one({"concept_id": concept_id}, _PREVIEW_DOC_PROJECTION)
    return dict(doc) if isinstance(doc, Mapping) else None


def _build_concept_preview_from_document(doc: Mapping[str, Any]) -> Dict[str, Any]:
    name = doc.get("name")
    if not name:
        try:
            name = get_concept_display_name_with_names_fallback(dict(doc))
        except Exception:  # pragma: no cover - best effort fallback
            name = doc.get("concept_id")

    kind = doc.get("kind")
    if not kind:
        try:
            # Check predicate FIRST: predicates can have is_a_type_of relationships.
            if is_predicate(dict(doc)):
                kind = "predicate"
            elif is_type(dict(doc)):
                kind = "type"
            else:
                kind = "individual"
        except Exception:  # pragma: no cover
            kind = "unknown"

    relationships = doc.get("relationships") or {}
    type_ids = _normalise_relationship_targets(
        relationships.get("is_an_instance_of")
        if isinstance(relationships, Mapping)
        else None
    )
    if should_enforce_access_control():
        type_ids = _filter_accessible_concept_ids(type_ids)

    preview = {
        "concept_id": doc.get("concept_id"),
        "name": name,
        "kind": kind,
        "last_updated": _isoformat(doc.get("updated_at")),
    }
    if type_ids:
        preview["type_ids"] = type_ids[:6]
    return preview


def _load_accessible_relation_subject_document(
    concept_id: str,
) -> Optional[Dict[str, Any]]:
    if should_enforce_access_control() and not can_access_concept(concept_id):
        return None
    with bypass_access_control():
        doc = ConceptsRepository.find_one(
            {"concept_id": concept_id},
            {
                "concept_id": 1,
                "name": 1,
                "names": 1,
                "updated_at": 1,
                "relationships": 1,
            },
        )
    if not isinstance(doc, Mapping):
        return None
    materialised = dict(doc)
    relationships = materialised.get("relationships")
    if isinstance(relationships, Mapping) and should_enforce_access_control():
        materialised["relationships"] = _filter_accessible_relationships(relationships)
    return materialised


def _filter_accessible_relationships(
    relationships: Mapping[str, Any],
) -> Dict[str, Any]:
    filtered: Dict[str, Any] = {}
    for predicate_id, raw_targets in relationships.items():
        filtered[predicate_id] = _filter_accessible_relationship_value(raw_targets)
    return filtered


def _filter_accessible_relationship_value(raw_targets: Any) -> Any:
    if not should_enforce_access_control():
        return raw_targets
    if isinstance(raw_targets, str):
        if raw_targets.startswith("#") and not can_access_concept(raw_targets):
            return []
        return raw_targets
    if not isinstance(raw_targets, Iterable) or isinstance(raw_targets, Mapping):
        return raw_targets
    filtered: List[Any] = []
    for entry in raw_targets:
        if isinstance(entry, str) and entry.startswith("#") and not can_access_concept(entry):
            continue
        filtered.append(entry)
    return filtered


def _filter_accessible_concept_ids(concept_ids: Sequence[str]) -> List[str]:
    if not should_enforce_access_control():
        return [str(concept_id).strip() for concept_id in concept_ids if isinstance(concept_id, str) and str(concept_id).strip()]
    return [
        str(concept_id).strip()
        for concept_id in concept_ids
        if isinstance(concept_id, str)
        and str(concept_id).strip()
        and can_access_concept(str(concept_id).strip())
    ]


def _isoformat(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return None


def _coerce_argument_index(raw: Union[int, str, None]) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, str):
        value = raw.strip().lower()
        if not value or value == "any":
            return None
        if value in {"arg1", "subject"}:
            return _ARG_INDEX_SUBJECT
        if value in {"arg2", "object"}:
            return _ARG_INDEX_FIRST_OBJECT
        raw = value
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        raise ValueError("argument_index must be an integer >= 1 or 'any'")
    if parsed < 1:
        raise ValueError("argument_index must be >= 1")
    return parsed


def _normalise_relation_kind_filter(raw: Optional[str]) -> str:
    value = str(raw or "any").strip().lower()
    if not value or value == "any":
        return "any"
    if value in {"binary", "structured", "graph"}:
        return "binary"
    if value == "text":
        return "text"
    raise ValueError("relation_kind must be one of: any, binary, text")


def _resolve_uncertainty_mode(
    *,
    uncertainty_mode: Optional[str],
    include_uncertain: bool,
) -> Tuple[str, bool, bool]:
    if uncertainty_mode is None:
        mode = (
            _UNCERTAINTY_MODE_INCLUDE_UNCERTAIN
            if include_uncertain
            else _UNCERTAINTY_MODE_ASSERTED_ONLY
        )
    else:
        mode = str(uncertainty_mode).strip().lower()
        if mode in {"combined", "all"}:
            mode = _UNCERTAINTY_MODE_INCLUDE_UNCERTAIN
    if mode not in {
        _UNCERTAINTY_MODE_ASSERTED_ONLY,
        _UNCERTAINTY_MODE_UNCERTAIN_ONLY,
        _UNCERTAINTY_MODE_INCLUDE_UNCERTAIN,
    }:
        _UNCERTAINTY_RETRIEVAL_STATS["uncertainty_mode_errors"] += 1
        raise ValueError(
            "uncertainty_mode must be one of: asserted_only, uncertain_only, include_uncertain"
        )
    if mode == _UNCERTAINTY_MODE_ASSERTED_ONLY:
        return mode, True, False
    if mode == _UNCERTAINTY_MODE_UNCERTAIN_ONLY:
        return mode, False, True
    return mode, True, True


def _normalise_uncertainty_statuses(
    raw_statuses: Optional[Sequence[str]],
) -> List[str]:
    if raw_statuses is None:
        return []
    if isinstance(raw_statuses, str):
        candidates: Sequence[Any] = [raw_statuses]
    else:
        candidates = raw_statuses
    out: List[str] = []
    for item in candidates:
        token = str(item or "").strip().lower()
        if not token:
            continue
        out.append(token)
    # stable dedupe
    return list(dict.fromkeys(out))


def _record_uncertainty_mode_usage(mode: str) -> None:
    _UNCERTAINTY_RETRIEVAL_STATS["payload_calls_total"] += 1
    if mode == _UNCERTAINTY_MODE_ASSERTED_ONLY:
        _UNCERTAINTY_RETRIEVAL_STATS["asserted_only_calls"] += 1
    elif mode == _UNCERTAINTY_MODE_UNCERTAIN_ONLY:
        _UNCERTAINTY_RETRIEVAL_STATS["uncertain_only_calls"] += 1
    elif mode == _UNCERTAINTY_MODE_INCLUDE_UNCERTAIN:
        _UNCERTAINTY_RETRIEVAL_STATS["include_uncertain_calls"] += 1


def _coerce_uncertainty_entry(assertion: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "assertion_id": assertion.get("assertion_id"),
        "status": assertion.get("status"),
        "confidence_score": assertion.get("confidence_score"),
        "provenance": assertion.get("provenance") or {},
        "created_at_utc": assertion.get("created_at_utc"),
        "updated_at_utc": assertion.get("updated_at_utc"),
        "target_kind": assertion.get("target_kind"),
    }


def _collect_uncertain_relations_for_subject(
    *,
    concept_id: str,
    predicate_allowed,
    include_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    consumer,
    status_filter: Sequence[str],
) -> int:
    from .uncertain_relationship_service import list_uncertain_relationship_assertions

    assertions = list_uncertain_relationship_assertions(
        source_id=concept_id,
        statuses=status_filter or None,
        include_legacy=True,
    )
    emitted = 0
    source_preview = (
        _resolve_concept_preview(concept_id, include_preview, preview_cache)
        if include_preview
        else None
    )
    for assertion in assertions:
        predicate_id = assertion.get("predicate")
        if not predicate_allowed(predicate_id):
            continue
        target_value = assertion.get("target")
        if target_value is None:
            continue
        target_text = str(target_value)
        target_kind = str(assertion.get("target_kind") or "")
        is_text = target_kind == "text" or not target_text.startswith("#V#")
        entry: RelationValue = {
            "relation_id": f"uncertain::{concept_id}::{assertion.get('assertion_id')}",
            "source_concept_id": concept_id,
            "predicate_id": predicate_id,
            "relation_kind": "text" if is_text else "binary",
            "matched_argument_indexes": [_ARG_INDEX_SUBJECT],
            "target_values": [target_text],
            "follow_up_actions": _build_follow_up_actions(
                [target_text] if target_text.startswith("#V#") else [],
                exclude={concept_id},
            ),
            "access_granted": source_preview is not None or not include_preview,
            "is_asserted": False,
            "relation_state": "uncertain",
            "uncertainty": _coerce_uncertainty_entry(assertion),
        }
        if include_preview:
            entry["source_preview"] = source_preview
            if target_text.startswith("#V#"):
                preview = _resolve_concept_preview(target_text, True, preview_cache)
                if preview is not None:
                    entry["target_previews"] = {target_text: preview}
        if is_text:
            entry["text_value"] = {
                "text": target_text,
                "lang": None,
                "text_value_id": None,
                "context": {
                    "uncertainty_assertion_id": assertion.get("assertion_id"),
                    "uncertainty_status": assertion.get("status"),
                },
            }
        consumer(entry)
        emitted += 1
    return emitted


def _collect_uncertain_relations_for_targets(
    *,
    concept_id: str,
    predicate_allowed,
    include_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    consumer,
    status_filter: Sequence[str],
) -> int:
    query: Dict[str, Any] = {"uncertain_relationship_assertions.target": concept_id}
    if status_filter:
        query["uncertain_relationship_assertions.status"] = {"$in": list(status_filter)}
    cursor = ConceptsRepository.find(
        query,
        projection={
            "concept_id": 1,
            "uncertain_relationship_assertions": 1,
        },
    )
    emitted = 0
    target_preview = (
        _resolve_concept_preview(concept_id, include_preview, preview_cache)
        if include_preview
        else None
    )
    for source_doc in cursor:
        source_id = source_doc.get("concept_id")
        if not isinstance(source_id, str) or not source_id.strip():
            continue
        source_id = source_id.strip()
        assertions = source_doc.get("uncertain_relationship_assertions")
        if not isinstance(assertions, list):
            continue
        source_preview = (
            _resolve_concept_preview(source_id, include_preview, preview_cache)
            if include_preview
            else None
        )
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            if str(assertion.get("target") or "") != concept_id:
                continue
            status_value = str(assertion.get("status") or "").strip().lower()
            if status_filter and status_value not in status_filter:
                continue
            predicate_id = assertion.get("predicate")
            if not predicate_allowed(predicate_id):
                continue
            entry: RelationValue = {
                "relation_id": f"uncertain::{source_id}::{assertion.get('assertion_id')}::incoming",
                "source_concept_id": source_id,
                "predicate_id": predicate_id,
                "relation_kind": "binary",
                "matched_argument_indexes": [_ARG_INDEX_FIRST_OBJECT],
                "target_values": [concept_id],
                "follow_up_actions": _build_follow_up_actions(
                    [source_id], exclude={concept_id}
                ),
                "access_granted": source_preview is not None or not include_preview,
                "is_asserted": False,
                "relation_state": "uncertain",
                "uncertainty": _coerce_uncertainty_entry(assertion),
            }
            if include_preview:
                entry["source_preview"] = source_preview
                if target_preview is not None:
                    entry["target_previews"] = {concept_id: target_preview}
            consumer(entry)
            emitted += 1
    return emitted


def _collect_uncertain_argument_hits_for_subject(
    *,
    concept_id: str,
    argument_filter: Optional[int],
    relation_filter: str,
    predicate_terms: Sequence[str],
    include_concept_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    status_filter: Sequence[str],
) -> List[Dict[str, Any]]:
    from .uncertain_relationship_service import list_uncertain_relationship_assertions

    assertions = list_uncertain_relationship_assertions(
        source_id=concept_id,
        statuses=status_filter or None,
        include_legacy=True,
    )
    source_preview = _resolve_concept_preview(
        concept_id,
        include_concept_preview,
        preview_cache,
    )
    hits: List[Dict[str, Any]] = []
    for assertion in assertions:
        predicate_id = assertion.get("predicate")
        if not _predicate_matches_terms(predicate_id, predicate_terms):
            continue
        target_value = str(assertion.get("target") or "")
        if not target_value:
            continue
        target_kind = str(assertion.get("target_kind") or "")
        is_text = target_kind == "text" or not target_value.startswith("#V#")
        if relation_filter == "binary" and is_text:
            continue
        if relation_filter == "text" and not is_text:
            continue
        matched_indexes = [_ARG_INDEX_SUBJECT]
        if target_value == concept_id and not is_text:
            matched_indexes.append(_ARG_INDEX_FIRST_OBJECT)
        if not _argument_indexes_match(matched_indexes, argument_filter):
            continue
        hit: Dict[str, Any] = {
            "source_concept_id": concept_id,
            "predicate_concept_id": predicate_id,
            "relation_kind": "text" if is_text else "binary",
            "argument_indexes": matched_indexes,
            "target_value": target_value,
            "relation_metadata": {
                "relation_id": f"uncertain::{concept_id}::{assertion.get('assertion_id')}",
                "updated_at": assertion.get("updated_at_utc"),
                "match_type": "uncertain_assertion",
            },
            "access_granted": source_preview is not None or not include_concept_preview,
            "follow_up_actions": _build_follow_up_actions(
                [target_value] if target_value.startswith("#V#") else [],
                exclude={concept_id},
            ),
            "score": float(assertion.get("confidence_score") or 0.0),
            "is_asserted": False,
            "relation_state": "uncertain",
            "uncertainty": _coerce_uncertainty_entry(assertion),
        }
        if include_concept_preview and target_value.startswith("#V#"):
            hit["target_concept_preview"] = _resolve_concept_preview(
                target_value,
                True,
                preview_cache,
            )
        hits.append(hit)
    return hits


def _collect_uncertain_argument_hits_for_targets(
    *,
    concept_id: str,
    argument_filter: Optional[int],
    relation_filter: str,
    predicate_terms: Sequence[str],
    include_concept_preview: bool,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    status_filter: Sequence[str],
) -> List[Dict[str, Any]]:
    if relation_filter == "text":
        return []
    if not _argument_indexes_match([_ARG_INDEX_FIRST_OBJECT], argument_filter):
        return []
    query: Dict[str, Any] = {"uncertain_relationship_assertions.target": concept_id}
    if status_filter:
        query["uncertain_relationship_assertions.status"] = {"$in": list(status_filter)}
    cursor = ConceptsRepository.find(
        query,
        projection={"concept_id": 1, "uncertain_relationship_assertions": 1},
    )
    target_preview = _resolve_concept_preview(
        concept_id,
        include_concept_preview,
        preview_cache,
    )
    hits: List[Dict[str, Any]] = []
    for doc in cursor:
        source_id = doc.get("concept_id")
        if not isinstance(source_id, str) or not source_id.strip():
            continue
        source_id = source_id.strip()
        assertions = doc.get("uncertain_relationship_assertions")
        if not isinstance(assertions, list):
            continue
        source_preview = _resolve_concept_preview(
            source_id, include_concept_preview, preview_cache
        )
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            if str(assertion.get("target") or "") != concept_id:
                continue
            if status_filter:
                token = str(assertion.get("status") or "").strip().lower()
                if token not in status_filter:
                    continue
            predicate_id = assertion.get("predicate")
            if not _predicate_matches_terms(predicate_id, predicate_terms):
                continue
            hit: Dict[str, Any] = {
                "source_concept_id": source_id,
                "predicate_concept_id": predicate_id,
                "relation_kind": "binary",
                "argument_indexes": [_ARG_INDEX_FIRST_OBJECT],
                "target_value": concept_id,
                "relation_metadata": {
                    "relation_id": f"uncertain::{source_id}::{assertion.get('assertion_id')}::incoming",
                    "updated_at": assertion.get("updated_at_utc"),
                    "match_type": "uncertain_assertion",
                },
                "access_granted": source_preview is not None or not include_concept_preview,
                "follow_up_actions": _build_follow_up_actions(
                    [source_id], exclude={concept_id}
                ),
                "score": float(assertion.get("confidence_score") or 0.0),
                "is_asserted": False,
                "relation_state": "uncertain",
                "uncertainty": _coerce_uncertainty_entry(assertion),
            }
            if include_concept_preview:
                hit["target_concept_preview"] = target_preview
            hits.append(hit)
    return hits


def _normalise_predicate_terms(
    predicate_filter: Optional[Sequence[str]],
) -> List[str]:
    if predicate_filter is None:
        return []
    values: Sequence[Any]
    if isinstance(predicate_filter, str):
        values = [predicate_filter]
    else:
        values = predicate_filter
    terms: List[str] = []
    for candidate in values:
        if not isinstance(candidate, str):
            continue
        token = candidate.strip().lower()
        if token:
            terms.append(token)
    return terms


def _predicate_matches_terms(predicate_id: Any, terms: Sequence[str]) -> bool:
    if not terms:
        return True
    if not isinstance(predicate_id, str) or not predicate_id.strip():
        return False
    candidate = predicate_id.strip().lower()
    for term in terms:
        if candidate == term or term in candidate:
            return True
    return False


def _argument_indexes_match(indexes: Sequence[int], wanted: Optional[int]) -> bool:
    if wanted is None:
        return True
    return wanted in indexes


def _normalise_relationship_targets(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        candidate = raw.strip()
        return [candidate] if candidate else []
    if isinstance(raw, Iterable):
        values: List[str] = []
        for entry in raw:
            if isinstance(entry, str) and entry.strip():
                values.append(entry.strip())
        return values
    return []


def _match_indexes_for_target(concept_id: str, targets: List[str]) -> List[int]:
    positions: List[int] = []
    for idx, candidate in enumerate(targets):
        if candidate == concept_id:
            positions.append(_ARG_INDEX_FIRST_OBJECT + idx)
    return positions


def _make_snippet(text: Optional[str], mode: str) -> Optional[Dict[str, Any]]:
    if not text or not mode:
        return None
    snippet = text
    if mode == "snippets":
        snippet = text[:200]
    return {
        "field": "text",
        "snippet": snippet,
        "match_offset": 0,
    }


def _make_argument_match_snippet(
    *,
    text: Any,
    needle: str,
    include_snippets: bool,
) -> Optional[Dict[str, Any]]:
    text_value = str(text or "")
    if not text_value:
        return None
    match_offset = text_value.lower().find(needle.lower()) if needle else -1
    if not include_snippets:
        return None
    if match_offset < 0:
        snippet = text_value[:200]
        return {"field": "text", "snippet": snippet, "match_offset": 0}
    snippet_start = max(0, match_offset - 80)
    snippet_end = min(len(text_value), match_offset + max(len(needle), 20) + 80)
    return {
        "field": "text",
        "snippet": text_value[snippet_start:snippet_end],
        "match_offset": match_offset,
    }


def _compute_text_match_score(text: str, needle: str) -> float:
    if not text or not needle:
        return 0.0
    count = text.lower().count(needle.lower())
    if count <= 0:
        return 0.0
    return min(1.0, 0.4 + (0.2 * min(count, 3)))


def _dedupe_argument_hits(hits: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for hit in hits:
        metadata: Dict[str, Any] = {}
        raw_metadata = hit.get("relation_metadata")
        if isinstance(raw_metadata, dict):
            metadata = raw_metadata
        key = (
            hit.get("source_concept_id"),
            hit.get("predicate_concept_id"),
            hit.get("relation_kind"),
            tuple(hit.get("argument_indexes") or []),
            hit.get("target_value"),
            metadata.get("relation_id"),
        )
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = hit
            continue
        if float(hit.get("score") or 0.0) > float(existing.get("score") or 0.0):
            deduped[key] = hit
    return list(deduped.values())


def _sort_argument_hits(
    hits: Sequence[Dict[str, Any]],
    sort_by: Optional[str],
) -> List[Dict[str, Any]]:
    mode = str(sort_by or "relevance").strip().lower()
    if mode in {"predicate", "predicate_asc"}:
        return sorted(
            hits,
            key=lambda row: (
                str(row.get("predicate_concept_id") or ""),
                str(row.get("source_concept_id") or ""),
                str(row.get("target_value") or ""),
            ),
        )
    if mode in {"updated", "updated_desc", "last_updated"}:
        return sorted(
            hits,
            key=lambda row: (
                str(
                    (row.get("relation_metadata") or {}).get("updated_at")
                    if isinstance(row.get("relation_metadata"), dict)
                    else ""
                ),
                float(row.get("score") or 0.0),
            ),
            reverse=True,
        )
    return sorted(
        hits,
        key=lambda row: (
            float(row.get("score") or 0.0),
            str(row.get("predicate_concept_id") or ""),
            str(row.get("source_concept_id") or ""),
            str(row.get("target_value") or ""),
        ),
        reverse=True,
    )


def _build_follow_up_actions(
    values: Iterable[str], *, exclude: set[str]
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for val in values:
        if not isinstance(val, str) or val in exclude:
            continue
        actions.append({"action": "fetch_concept", "args": {"concept_id": val}})
    return actions


__all__ = [
    "build_concept_relations_payload",
    "find_relations_with_argument",
    "RelationOptions",
]
