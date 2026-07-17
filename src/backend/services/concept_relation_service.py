"""Helpers for assembling relation payloads for concept fetch requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .text_value_service import get_texts_for_concept, get_texts_for_concepts
from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import (
    bypass_access_control,
    can_access_concept,
    filter_accessible_concept_ids,
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

_TYPE_COUNT_MODE_DIRECT_ASSERTED = "direct_asserted"
_ROLE_EXPANSION_MODE_NONE = "none"
_ROLE_EXPANSION_MODE_EXPLICIT = "explicit"
_ROLE_EXPANSION_MODE_AUTO = "auto"
_TYPE_MEMBERSHIP_PREDICATES = {
    "is_an_instance_of",
    "#v#is_an_instance_of",
    "is_a_type_of",
    "#v#is_a_type_of",
}


def _validate_predicate_incidence_concept_identifier(
    value: str,
    *,
    field_name: str,
) -> None:
    if value.startswith("#V#"):
        return
    raise ValueError(
        f"{field_name} must be a canonical Vontology concept ID beginning with "
        "#V#; use search_concepts first to resolve plain text labels."
    )


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


@dataclass(frozen=True)
class PredicateIncidenceTypeCountOptions:
    include: bool = False
    mode: str = _TYPE_COUNT_MODE_DIRECT_ASSERTED
    include_untyped_bucket: bool = True
    max_types_per_predicate: int = 20
    max_sample_concepts_per_type: int = 3


@dataclass(frozen=True)
class PredicateIncidenceRoleExpansionOptions:
    mode: str = _ROLE_EXPANSION_MODE_NONE
    node_type_filter: Tuple[str, ...] = ()
    predicate_filter: Tuple[str, ...] = ()
    depth: int = 1
    max_nodes_per_predicate: int = 12
    max_role_predicates: int = 20
    max_sample_concepts_per_type: int = 3


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
    mode_value, include_asserted_rows, include_uncertain_rows = (
        _resolve_uncertainty_mode(
            uncertainty_mode=uncertainty_mode,
            include_uncertain=include_uncertain,
        )
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
    predicate_display_terms = _normalise_predicate_display_terms(predicate_filter)
    query_diagnostics = {
        "tool": "find_relations_with_argument",
        "concept_id": resolved_concept_id,
        "argument_index": argument_index,
        "resolved_argument_index": argument_filter,
        "predicate_filter": list(predicate_display_terms),
        "relation_kind": relation_kind,
        "resolved_relation_kind": relation_filter,
        "limit": resolved_limit_value,
        "offset": resolved_offset,
        "sort_by": sort_by,
        "include_text_snippets": bool(include_text_snippets),
        "include_concept_preview": bool(include_concept_preview),
    }

    include_structural = relation_filter in {"any", "binary"}
    include_text = relation_filter in {"any", "text"}
    mode_value, include_asserted_rows, include_uncertain_rows = (
        _resolve_uncertainty_mode(
            uncertainty_mode=uncertainty_mode,
            include_uncertain=include_uncertain,
        )
    )
    status_filter = _normalise_uncertainty_statuses(uncertainty_statuses)
    _record_uncertainty_mode_usage(mode_value)
    _UNCERTAINTY_RETRIEVAL_STATS["find_calls_total"] += 1
    include_arg1 = argument_filter in (None, _ARG_INDEX_SUBJECT)
    include_arg2_or_later = (
        argument_filter is None or argument_filter >= _ARG_INDEX_FIRST_OBJECT
    )

    preview_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    hits: List[Dict[str, Any]] = []

    subject_doc = _load_accessible_relation_subject_document(resolved_concept_id)

    if include_asserted_rows and include_structural and subject_doc:
        relationships = (subject_doc.get("relationships") or {}) if subject_doc else {}
        source_updated_at = (
            _isoformat(subject_doc.get("updated_at")) if subject_doc else None
        )
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
                if (
                    include_concept_preview
                    and isinstance(target_value, str)
                    and target_value.startswith("#V#")
                ):
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
                        "access_granted": source_preview is not None
                        or not include_concept_preview,
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
                        "access_granted": source_preview is not None
                        or not include_concept_preview,
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
                "access_granted": source_preview is not None
                or not include_concept_preview,
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
            for rel in TextRelationsRepository.find(
                {"object_text_id": {"$in": text_ids}}
            ):
                source_id = rel.get("subject_concept_id")
                if not isinstance(source_id, str) or not source_id.strip():
                    continue
                source_id = source_id.strip()
                predicate_id = rel.get("predicate")
                if not _predicate_matches_terms(predicate_id, predicate_terms):
                    continue
                if not _argument_indexes_match(
                    [_ARG_INDEX_FIRST_OBJECT], argument_filter
                ):
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
                    "access_granted": source_preview is not None
                    or not include_concept_preview,
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
    paged_hits = sorted_hits[resolved_offset : resolved_offset + resolved_limit_value]
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
        "relation_query_diagnostics": {
            **query_diagnostics,
            "uncertainty_mode": mode_value,
            "include_uncertain": include_uncertain_rows,
            "uncertainty_statuses": status_filter or None,
            "total_hits": len(sorted_hits),
            "returned": len(paged_hits),
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
    include_argument_type_counts: bool = False,
    type_count_mode: Optional[str] = None,
    include_untyped_bucket: bool = True,
    max_types_per_predicate: Optional[int] = None,
    max_sample_concepts_per_type: Optional[int] = None,
    role_expansion_mode: Optional[str] = None,
    role_node_type_filter: Optional[Sequence[str]] = None,
    role_predicate_filter: Optional[Sequence[str]] = None,
    role_expansion_depth: Optional[int] = None,
) -> Dict[str, Any]:
    """Summarise distinct predicates observed for an entity or a type's instances.

    Exactly one of ``concept_id`` (entity mode) or ``instance_of`` (type mode)
    must be provided. The returned payload groups relation hits by predicate and
    records useful counts such as relation-hit count, distinct grounding count,
    and, for type mode, the number of distinct instances contributing evidence.
    Optional typed summaries are support-surface evidence only: they count direct
    asserted types already represented in Vontology without adding domain policy
    or lexical predicate/type steering in Python.
    """

    resolved_concept_id = str(concept_id or "").strip()
    resolved_instance_of = str(instance_of or "").strip()
    if bool(resolved_concept_id) == bool(resolved_instance_of):
        raise ValueError("Exactly one of concept_id or instance_of is required")
    if resolved_concept_id:
        _validate_predicate_incidence_concept_identifier(
            resolved_concept_id,
            field_name="concept_id",
        )
    if resolved_instance_of:
        _validate_predicate_incidence_concept_identifier(
            resolved_instance_of,
            field_name="instance_of",
        )

    _ = scope  # Scope is enforced by repository-level access control.
    resolved_limit = _coerce_limit(limit)
    resolved_limit_value = (
        resolved_limit if resolved_limit is not None else _DEFAULT_LIMIT
    )
    resolved_offset = max(int(offset or 0), 0)
    resolved_sort_by = _normalise_predicate_incidence_sort(sort_by)
    preview_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    type_count_options = _normalise_predicate_incidence_type_count_options(
        include_argument_type_counts=include_argument_type_counts,
        type_count_mode=type_count_mode,
        include_untyped_bucket=include_untyped_bucket,
        max_types_per_predicate=max_types_per_predicate,
        max_sample_concepts_per_type=max_sample_concepts_per_type,
    )
    role_expansion_options = _normalise_predicate_incidence_role_expansion_options(
        role_expansion_mode=role_expansion_mode,
        role_node_type_filter=role_node_type_filter,
        role_predicate_filter=role_predicate_filter,
        role_expansion_depth=role_expansion_depth,
        max_sample_concepts_per_type=type_count_options.max_sample_concepts_per_type,
    )

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
        if type_count_options.include:
            _attach_direct_type_ids_to_fast_incidence_hits(
                {resolved_concept_id: hits}
            )
        predicate_rows = _aggregate_predicate_incidence_rows(
            hits=hits,
            include_concept_preview=include_concept_preview,
            preview_cache=preview_cache,
            anchor_concept_id=resolved_concept_id,
            mode="entity",
            type_membership_ids=None,
            type_count_options=type_count_options,
            role_expansion_options=role_expansion_options,
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
            "role_expansions": _extract_predicate_incidence_role_expansions(paged_rows),
            "paging": {
                "limit": resolved_limit_value,
                "offset": resolved_offset,
                "returned": len(paged_rows),
                "total_available": len(sorted_rows),
            },
            "uncertainty_diagnostics": diagnostics,
            "typed_predicate_incidence_diagnostics": (
                _build_typed_predicate_incidence_diagnostics(
                    type_count_options=type_count_options,
                    role_expansion_options=role_expansion_options,
                )
            ),
            "predicate_incidence_query_diagnostics": {
                "tool": "get_predicate_incidence",
                "mode": "entity",
                "concept_id": resolved_concept_id,
                "argument_index": argument_index,
                "predicate_filter": list(
                    _normalise_predicate_display_terms(predicate_filter)
                ),
                "relation_kind": relation_kind,
                "limit": resolved_limit_value,
                "offset": resolved_offset,
                "sort_by": resolved_sort_by,
                "include_text_snippets": bool(include_text_snippets),
                "include_concept_preview": bool(include_concept_preview),
                "include_argument_type_counts": type_count_options.include,
                "uncertainty_mode": diagnostics.get("mode"),
                "include_uncertain": diagnostics.get("include_uncertain"),
                "uncertainty_statuses": diagnostics.get("statuses"),
                "total_predicates": len(sorted_rows),
                "returned": len(paged_rows),
            },
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

    grouped_hits = _collect_type_subject_hits_for_incidence_fast(
        instance_ids=instance_ids,
        argument_index=argument_index,
        predicate_filter=predicate_filter,
        relation_kind=relation_kind,
        include_text_snippets=include_text_snippets,
        include_uncertain=include_uncertain,
        uncertainty_mode=uncertainty_mode,
        uncertainty_statuses=uncertainty_statuses,
    )
    retrieval_strategy = "batched_subject_asserted" if grouped_hits is not None else (
        "per_instance_relation_lookup"
    )
    if grouped_hits is not None and type_count_options.include:
        _attach_direct_type_ids_to_fast_incidence_hits(grouped_hits)

    for instance_id in instance_ids:
        if grouped_hits is None:
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
        else:
            hits = grouped_hits.get(instance_id, [])
        _accumulate_predicate_incidence_rows(
            aggregated_by_predicate,
            hits=hits,
            include_concept_preview=include_concept_preview,
            preview_cache=preview_cache,
            anchor_concept_id=instance_id,
            mode="type",
            contributing_instance_id=instance_id,
            type_membership_ids=type_ids,
            type_count_options=type_count_options,
            role_expansion_options=role_expansion_options,
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
        "role_expansions": _extract_predicate_incidence_role_expansions(paged_rows),
        "paging": {
            "limit": resolved_limit_value,
            "offset": resolved_offset,
            "returned": len(paged_rows),
            "total_available": len(sorted_rows),
        },
        "uncertainty_diagnostics": diagnostics,
        "typed_predicate_incidence_diagnostics": (
            _build_typed_predicate_incidence_diagnostics(
                type_count_options=type_count_options,
                role_expansion_options=role_expansion_options,
            )
        ),
        "predicate_incidence_query_diagnostics": {
            "tool": "get_predicate_incidence",
            "mode": "type",
            "instance_of": resolved_instance_of,
            "type_ids_considered": list(type_ids),
            "instance_count_considered": len(instance_ids),
            "direct_instances_only": bool(direct_instances_only),
            "argument_index": argument_index,
            "predicate_filter": list(
                _normalise_predicate_display_terms(predicate_filter)
            ),
            "relation_kind": relation_kind,
            "limit": resolved_limit_value,
            "offset": resolved_offset,
            "sort_by": resolved_sort_by,
            "include_text_snippets": bool(include_text_snippets),
            "include_concept_preview": bool(include_concept_preview),
            "include_argument_type_counts": type_count_options.include,
            "uncertainty_mode": diagnostics.get("mode"),
            "include_uncertain": diagnostics.get("include_uncertain"),
            "uncertainty_statuses": diagnostics.get("statuses"),
            "retrieval_strategy": retrieval_strategy,
            "total_predicates": len(sorted_rows),
            "returned": len(paged_rows),
        },
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

    argument_filter = _coerce_argument_index(argument_index)
    relation_filter = _normalise_relation_kind_filter(relation_kind)
    relation_queries: List[Tuple[str, Union[int, str, None]]] = []
    if relation_filter == "any":
        if argument_index is None:
            relation_queries.append(("binary", _ARG_INDEX_SUBJECT))
            relation_queries.append(("text", _ARG_INDEX_SUBJECT))
        else:
            relation_queries.append(("binary", argument_index))
        if argument_index is not None and argument_filter in (
            None,
            _ARG_INDEX_SUBJECT,
        ):
            relation_queries.append(("text", _ARG_INDEX_SUBJECT))
    else:
        relation_queries.append((relation_filter, argument_index))

    for query_relation_kind, query_argument_index in relation_queries:
        offset = 0
        while True:
            payload = find_relations_with_argument(
                concept_id=concept_id,
                argument_index=query_argument_index,
                predicate_filter=predicate_filter,
                relation_kind=query_relation_kind,
                include_text_snippets=include_text_snippets,
                # Incidence aggregation only exposes bounded sample groundings, so
                # resolve previews there instead of for every raw relation hit.
                include_concept_preview=False,
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
                dict(hit) for hit in batch_hits if isinstance(hit, Mapping)
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


def _collect_type_subject_hits_for_incidence_fast(
    *,
    instance_ids: Sequence[str],
    argument_index: Union[int, str, None],
    predicate_filter: Optional[Sequence[str]],
    relation_kind: Optional[str],
    include_text_snippets: bool,
    include_uncertain: bool,
    uncertainty_mode: Optional[str],
    uncertainty_statuses: Optional[Sequence[str]],
) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    argument_filter = _coerce_argument_index(argument_index)
    relation_filter = _normalise_relation_kind_filter(relation_kind)
    mode_value, include_asserted_rows, include_uncertain_rows = (
        _resolve_uncertainty_mode(
            uncertainty_mode=uncertainty_mode,
            include_uncertain=include_uncertain,
        )
    )
    if (
        not include_asserted_rows
        or include_uncertain_rows
        or _normalise_uncertainty_statuses(uncertainty_statuses)
        or argument_filter not in (None, _ARG_INDEX_SUBJECT)
        or relation_filter not in {"any", "binary", "text"}
    ):
        return None

    _record_uncertainty_mode_usage(mode_value)
    predicate_terms = _normalise_predicate_terms(predicate_filter)
    grouped: Dict[str, List[Dict[str, Any]]] = {
        instance_id: []
        for instance_id in instance_ids
        if isinstance(instance_id, str) and instance_id.strip()
    }
    if not grouped:
        return grouped

    include_structural = relation_filter in {"any", "binary"}
    include_text = relation_filter in {"any", "text"}
    enforce_access = should_enforce_access_control()

    if include_structural:
        with bypass_access_control():
            cursor = list(
                ConceptsRepository.find(
                    {"concept_id": {"$in": list(grouped)}},
                    {"concept_id": 1, "relationships": 1, "updated_at": 1},
                )
            )
        accessible_source_ids = (
            filter_accessible_concept_ids(grouped)
            if enforce_access
            else set(grouped)
        )
        accessible_target_ids: set[str] | None = None
        if enforce_access:
            candidate_target_ids: List[str] = []
            for doc in cursor:
                source_id = doc.get("concept_id")
                if not isinstance(source_id, str) or source_id not in accessible_source_ids:
                    continue
                relationships = doc.get("relationships")
                if not isinstance(relationships, Mapping):
                    continue
                for predicate_id, raw_targets in relationships.items():
                    if not _predicate_matches_terms(predicate_id, predicate_terms):
                        continue
                    candidate_target_ids.extend(_normalise_relationship_targets(raw_targets))
            accessible_target_ids = filter_accessible_concept_ids(candidate_target_ids)
        for doc in cursor:
            source_id = doc.get("concept_id")
            if not isinstance(source_id, str) or source_id not in grouped:
                continue
            if enforce_access and source_id not in accessible_source_ids:
                continue
            relationships = doc.get("relationships")
            if not isinstance(relationships, Mapping):
                continue
            updated_at = _isoformat(doc.get("updated_at"))
            for predicate_id, raw_targets in relationships.items():
                if not _predicate_matches_terms(predicate_id, predicate_terms):
                    continue
                targets = _normalise_relationship_targets(raw_targets)
                if accessible_target_ids is not None:
                    targets = [
                        target
                        for target in targets
                        if not (
                            isinstance(target, str)
                            and target.startswith("#")
                            and target not in accessible_target_ids
                        )
                    ]
                if not targets:
                    continue
                for target_index, target_value in enumerate(targets):
                    matched_indexes = [_ARG_INDEX_SUBJECT]
                    arg_target_index = _ARG_INDEX_FIRST_OBJECT + target_index
                    if target_value == source_id:
                        matched_indexes.append(arg_target_index)
                    grouped[source_id].append(
                        {
                            "source_concept_id": source_id,
                            "predicate_concept_id": predicate_id,
                            "relation_kind": "binary",
                            "argument_indexes": matched_indexes,
                            "target_value": target_value,
                            "target_concept_preview": None,
                            "relation_metadata": {
                                "relation_id": (
                                    f"struct::{source_id}::{predicate_id}::"
                                    f"{target_index}"
                                ),
                                "updated_at": updated_at,
                                "match_type": "exact",
                            },
                            "access_granted": True,
                            "follow_up_actions": _build_follow_up_actions(
                                [target_value],
                                exclude={source_id},
                            ),
                            "score": 1.0,
                            "is_asserted": True,
                            "relation_state": "asserted",
                        }
                    )

    if include_text:
        rows_by_concept = get_texts_for_concepts(
            list(grouped),
            limit_per_concept=_MAX_LIMIT,
        )
        for source_id, rows in rows_by_concept.items():
            if source_id not in grouped:
                continue
            for rel in rows:
                predicate_id = rel.get("predicate")
                if not _predicate_matches_terms(predicate_id, predicate_terms):
                    continue
                text_value = rel.get("text")
                hit: Dict[str, Any] = {
                    "source_concept_id": source_id,
                    "predicate_concept_id": predicate_id,
                    "relation_kind": "text",
                    "argument_indexes": [_ARG_INDEX_SUBJECT],
                    "target_value": text_value,
                    "relation_metadata": {
                        "relation_id": str(
                            rel.get("relation_id")
                            or f"text::{source_id}::{predicate_id}"
                        ),
                        "text_value_id": rel.get("text_value_id"),
                        "lang": rel.get("lang"),
                        "match_type": "exact",
                    },
                    "access_granted": True,
                    "follow_up_actions": [],
                    "score": 1.0,
                    "is_asserted": True,
                    "relation_state": "asserted",
                }
                snippet = _make_argument_match_snippet(
                    text=text_value,
                    needle=source_id,
                    include_snippets=include_text_snippets,
                )
                if snippet:
                    hit["text_snippet"] = snippet
                grouped[source_id].append(hit)

    return grouped


def _attach_direct_type_ids_to_fast_incidence_hits(
    grouped_hits: Mapping[str, List[Dict[str, Any]]],
) -> None:
    target_ids: List[str] = []
    for source_id, hits in grouped_hits.items():
        for hit in hits:
            target_id = _normalise_concept_id(hit.get("target_value"))
            if not target_id or target_id == source_id:
                continue
            target_ids.append(target_id)
    target_ids = list(dict.fromkeys(target_ids))
    if not target_ids:
        return

    enforce_access = should_enforce_access_control()
    accessible_target_ids = (
        list(filter_accessible_concept_ids(target_ids))
        if enforce_access
        else list(target_ids)
    )
    type_ids_by_target: Dict[str, List[str]] = {
        target_id: [] for target_id in accessible_target_ids
    }
    if accessible_target_ids:
        with bypass_access_control():
            cursor = list(
                ConceptsRepository.find(
                    {"concept_id": {"$in": accessible_target_ids}},
                    {
                        "concept_id": 1,
                        "relationships.is_an_instance_of": 1,
                        "relationships.#V#is_an_instance_of": 1,
                    },
                )
            )
        raw_type_ids_by_target: Dict[str, List[str]] = {}
        raw_type_ids: List[str] = []
        for doc in cursor:
            target_id = doc.get("concept_id")
            if not isinstance(target_id, str) or target_id not in type_ids_by_target:
                continue
            relationships = doc.get("relationships")
            if not isinstance(relationships, Mapping):
                continue
            type_ids: List[str] = []
            for predicate_id in ("is_an_instance_of", "#V#is_an_instance_of"):
                type_ids.extend(
                    _normalise_relationship_targets(relationships.get(predicate_id))
                )
            type_ids = list(dict.fromkeys(type_ids))
            raw_type_ids_by_target[target_id] = type_ids
            raw_type_ids.extend(type_ids)

        accessible_type_ids = (
            filter_accessible_concept_ids(raw_type_ids)
            if enforce_access
            else set(raw_type_ids)
        )
        for target_id, type_ids in raw_type_ids_by_target.items():
            type_ids_by_target[target_id] = [
                type_id for type_id in type_ids if type_id in accessible_type_ids
            ]

    for source_id, hits in grouped_hits.items():
        for hit in hits:
            target_id = _normalise_concept_id(hit.get("target_value"))
            if not target_id or target_id == source_id:
                continue
            if target_id in type_ids_by_target:
                hit["target_type_ids"] = list(type_ids_by_target[target_id])


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
    type_count_options: PredicateIncidenceTypeCountOptions,
    role_expansion_options: PredicateIncidenceRoleExpansionOptions,
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
        type_count_options=type_count_options,
        role_expansion_options=role_expansion_options,
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
    type_count_options: PredicateIncidenceTypeCountOptions,
    role_expansion_options: PredicateIncidenceRoleExpansionOptions,
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
            [int(index) for index in raw_indexes if isinstance(index, (int, float))]
            if isinstance(raw_indexes, list)
            else []
        )
        row["_argument_indexes"].update(indexes)
        if _ARG_INDEX_SUBJECT in indexes:
            row["subject_argument_hit_count"] += 1
        if any(index >= _ARG_INDEX_FIRST_OBJECT for index in indexes):
            row["object_argument_hit_count"] += 1

        if (
            type_count_options.include
            or role_expansion_options.mode != _ROLE_EXPANSION_MODE_NONE
        ):
            argument_entries = _extract_predicate_incidence_argument_entries(
                hit,
                anchor_concept_id=anchor_concept_id,
                preview_cache=preview_cache,
            )
            if type_count_options.include:
                _accumulate_predicate_incidence_argument_type_counts(
                    row,
                    argument_entries=argument_entries,
                    preview_cache=preview_cache,
                    type_count_options=type_count_options,
                )
            if role_expansion_options.mode != _ROLE_EXPANSION_MODE_NONE:
                _accumulate_predicate_incidence_role_expansion(
                    row,
                    argument_entries=argument_entries,
                    anchor_concept_id=anchor_concept_id,
                    predicate_concept_id=resolved_predicate_id,
                    preview_cache=preview_cache,
                    role_expansion_options=role_expansion_options,
                )

        if len(row["sample_groundings"]) < 4:
            for grounding_key, grounding in _extract_predicate_incidence_groundings(
                hit,
                anchor_concept_id=anchor_concept_id,
                include_concept_preview=include_concept_preview,
                preview_cache=preview_cache,
            ):
                if grounding_key in row["_grounding_keys"]:
                    continue
                row["_grounding_keys"].add(grounding_key)
                row["sample_groundings"].append(grounding)
                if len(row["sample_groundings"]) >= 4:
                    break

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


def _extract_predicate_incidence_argument_entries(
    hit: Mapping[str, Any],
    *,
    anchor_concept_id: str,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []

    source_concept_id = _normalise_concept_id(hit.get("source_concept_id"))
    if source_concept_id and source_concept_id != anchor_concept_id:
        entries.append(
            {
                "argument_index": _ARG_INDEX_SUBJECT,
                "argument_role": "subject",
                "concept_id": source_concept_id,
                "concept_preview": _resolve_concept_preview(
                    source_concept_id,
                    True,
                    preview_cache,
                ),
            }
        )

    target_preview = hit.get("target_concept_preview")
    target_concept_id = _extract_preview_concept_id(
        target_preview
    ) or _normalise_concept_id(hit.get("target_value"))
    if target_concept_id and target_concept_id != anchor_concept_id:
        resolved_preview = target_preview
        raw_target_type_ids = hit.get("target_type_ids")
        target_type_ids = (
            [
                str(type_id).strip()
                for type_id in raw_target_type_ids
                if isinstance(type_id, str) and str(type_id).strip()
            ]
            if isinstance(raw_target_type_ids, list)
            else None
        )
        if not isinstance(resolved_preview, Mapping):
            if target_type_ids is not None:
                resolved_preview = {
                    "concept_id": target_concept_id,
                    "type_ids": list(target_type_ids),
                }
            else:
                resolved_preview = _resolve_concept_preview(
                    target_concept_id,
                    True,
                    preview_cache,
                )
        entry = {
            "argument_index": _ARG_INDEX_FIRST_OBJECT,
            "argument_role": "object",
            "concept_id": target_concept_id,
            "concept_preview": resolved_preview,
        }
        if target_type_ids is not None:
            entry["type_ids"] = list(target_type_ids)
        entries.append(entry)
        return entries

    target_value = hit.get("target_value")
    text_value = None
    if (
        isinstance(target_value, str)
        and target_value.strip()
        and not target_value.strip().startswith("#V#")
    ):
        text_value = target_value.strip()
    text_snippet = hit.get("text_snippet")
    if isinstance(text_snippet, str) and text_snippet.strip():
        text_value = text_snippet.strip()
    if text_value:
        entries.append(
            {
                "argument_index": _ARG_INDEX_FIRST_OBJECT,
                "argument_role": "object",
                "literal_preview": text_value[:180],
            }
        )
    return entries


def _accumulate_predicate_incidence_argument_type_counts(
    row: Dict[str, Any],
    *,
    argument_entries: Sequence[Mapping[str, Any]],
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    type_count_options: PredicateIncidenceTypeCountOptions,
) -> None:
    if not argument_entries:
        return
    row["_max_types_per_predicate"] = type_count_options.max_types_per_predicate
    for entry in argument_entries:
        argument_index = entry.get("argument_index")
        argument_role = entry.get("argument_role")
        if not isinstance(argument_index, int) or not isinstance(argument_role, str):
            continue
        concept_id = _normalise_concept_id(entry.get("concept_id"))
        if concept_id is None:
            if entry.get("literal_preview"):
                row["_literal_argument_count"] += 1
            continue
        preview = entry.get("concept_preview")
        raw_type_ids = entry.get("type_ids")
        type_ids = (
            [
                str(type_id).strip()
                for type_id in raw_type_ids
                if isinstance(type_id, str) and str(type_id).strip()
            ]
            if isinstance(raw_type_ids, list)
            else None
        )
        if type_ids is None:
            if not isinstance(preview, Mapping):
                row["_inaccessible_argument_count"] += 1
                continue
            type_ids = _resolve_accessible_concept_type_ids(concept_id)
            if type_ids is None:
                row["_inaccessible_argument_count"] += 1
                continue
        elif not isinstance(preview, Mapping):
            preview = {"concept_id": concept_id, "type_ids": list(type_ids)}
        if not type_ids:
            if type_count_options.include_untyped_bucket:
                row["_untyped_argument_count"] += 1
            continue
        for type_id in list(dict.fromkeys(type_ids)):
            bucket_key = (argument_index, argument_role, type_id)
            buckets = row.get("_argument_type_counts")
            if not isinstance(buckets, dict):
                buckets = {}
                row["_argument_type_counts"] = buckets
            bucket = buckets.get(bucket_key)
            if bucket is None:
                bucket = {
                    "argument_index": argument_index,
                    "argument_role": argument_role,
                    "type_concept_id": type_id,
                    "relation_hit_count": 0,
                    "_concept_ids": set(),
                    "sample_concepts": [],
                }
                type_preview = _resolve_concept_preview(type_id, True, preview_cache)
                if isinstance(type_preview, Mapping):
                    bucket["type_preview"] = _compact_concept_sample(type_preview)
                buckets[bucket_key] = bucket
            bucket["relation_hit_count"] += 1
            concept_ids = bucket.get("_concept_ids")
            if isinstance(concept_ids, set):
                concept_ids.add(concept_id)
            samples = bucket.get("sample_concepts")
            if (
                isinstance(samples, list)
                and len(samples) < type_count_options.max_sample_concepts_per_type
            ):
                sample = _compact_concept_sample(preview)
                if sample and sample not in samples:
                    samples.append(sample)


def _accumulate_predicate_incidence_role_expansion(
    row: Dict[str, Any],
    *,
    argument_entries: Sequence[Mapping[str, Any]],
    anchor_concept_id: str,
    predicate_concept_id: str,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    role_expansion_options: PredicateIncidenceRoleExpansionOptions,
) -> None:
    if role_expansion_options.mode == _ROLE_EXPANSION_MODE_AUTO:
        # Auto mode requires Vontology-authored reification metadata. That
        # metadata is not yet surfaced here, so fail closed with diagnostics.
        return
    if role_expansion_options.mode != _ROLE_EXPANSION_MODE_EXPLICIT:
        return

    allowed_node_types = {
        type_id.lower() for type_id in role_expansion_options.node_type_filter
    }
    allowed_role_predicates = {
        predicate_id.lower() for predicate_id in role_expansion_options.predicate_filter
    }

    for entry in argument_entries:
        frame_id = _normalise_concept_id(entry.get("concept_id"))
        if frame_id is None:
            continue
        frames = row.get("_role_expansion_frames")
        if (
            isinstance(frames, dict)
            and len(frames) >= role_expansion_options.max_nodes_per_predicate
            and frame_id not in frames
        ):
            continue
        frame_type_ids = _resolve_accessible_concept_type_ids(frame_id)
        if frame_type_ids is None:
            continue
        if allowed_node_types and not (
            {type_id.lower() for type_id in frame_type_ids} & allowed_node_types
        ):
            continue
        frame_doc = _load_accessible_relation_subject_document(frame_id)
        if not isinstance(frame_doc, Mapping):
            continue
        relationships = frame_doc.get("relationships")
        if not isinstance(relationships, Mapping):
            continue

        direction = _anchor_direction_from_other_argument(entry)
        directions = row.get("_role_expansion_anchor_directions")
        if isinstance(directions, set):
            directions.add(direction)

        frame_record = _ensure_role_expansion_frame(row, frame_id, frame_type_ids)
        for role_predicate_id, raw_targets in relationships.items():
            if not isinstance(role_predicate_id, str) or not role_predicate_id.strip():
                continue
            clean_role_predicate_id = role_predicate_id.strip()
            if _is_type_membership_predicate_id(clean_role_predicate_id):
                continue
            if (
                clean_role_predicate_id == predicate_concept_id
                and _relationship_value_mentions_anchor(raw_targets, anchor_concept_id)
            ):
                continue
            if (
                allowed_role_predicates
                and clean_role_predicate_id.lower() not in allowed_role_predicates
            ):
                continue
            role_predicates = frame_record.get("_role_predicates")
            if isinstance(role_predicates, set):
                role_predicates.add(clean_role_predicate_id)
            targets = _normalise_relationship_targets(raw_targets)
            for target in targets:
                if target == anchor_concept_id or target == frame_id:
                    continue
                _accumulate_role_expansion_filler(
                    row,
                    frame_id=frame_id,
                    role_predicate_id=clean_role_predicate_id,
                    target=target,
                    preview_cache=preview_cache,
                    role_expansion_options=role_expansion_options,
                )


def _anchor_direction_from_other_argument(entry: Mapping[str, Any]) -> str:
    argument_index = entry.get("argument_index")
    if argument_index == _ARG_INDEX_SUBJECT:
        return "incoming"
    if isinstance(argument_index, int) and argument_index >= _ARG_INDEX_FIRST_OBJECT:
        return "outgoing"
    return "unknown"


def _relationship_value_mentions_anchor(
    raw_targets: Any, anchor_concept_id: str
) -> bool:
    return anchor_concept_id in _normalise_relationship_targets(raw_targets)


def _ensure_role_expansion_frame(
    row: Dict[str, Any],
    frame_id: str,
    frame_type_ids: Sequence[str],
) -> Dict[str, Any]:
    frames = row.get("_role_expansion_frames")
    if not isinstance(frames, dict):
        frames = {}
        row["_role_expansion_frames"] = frames
    frame_record = frames.get(frame_id)
    if frame_record is None:
        frame_record = {
            "concept_id": frame_id,
            "type_ids": list(frame_type_ids),
            "_role_predicates": set(),
        }
        frames[frame_id] = frame_record
    return frame_record


def _accumulate_role_expansion_filler(
    row: Dict[str, Any],
    *,
    frame_id: str,
    role_predicate_id: str,
    target: str,
    preview_cache: Dict[str, Optional[Dict[str, Any]]],
    role_expansion_options: PredicateIncidenceRoleExpansionOptions,
) -> None:
    target_concept_id = _normalise_concept_id(target)
    if target_concept_id is None:
        row["_role_expansion_literal_filler_count"] += 1
        return

    preview = _resolve_concept_preview(target_concept_id, True, preview_cache)
    if not isinstance(preview, Mapping):
        row["_role_expansion_inaccessible_filler_count"] += 1
        return
    type_ids = _resolve_accessible_concept_type_ids(target_concept_id)
    if type_ids is None:
        row["_role_expansion_inaccessible_filler_count"] += 1
        return
    if not type_ids:
        row["_role_expansion_untyped_filler_count"] += 1
        return

    role_counts = row.get("_role_expansion_role_counts")
    if not isinstance(role_counts, dict):
        role_counts = {}
        row["_role_expansion_role_counts"] = role_counts
    for type_id in list(dict.fromkeys(type_ids)):
        key = (role_predicate_id, type_id)
        bucket = role_counts.get(key)
        if bucket is None:
            bucket = {
                "role_predicate_concept_id": role_predicate_id,
                "type_concept_id": type_id,
                "relation_hit_count": 0,
                "_concept_ids": set(),
                "sample_reified_nodes": [],
                "sample_fillers": [],
            }
            type_preview = _resolve_concept_preview(type_id, True, preview_cache)
            if isinstance(type_preview, Mapping):
                bucket["type_preview"] = _compact_concept_sample(type_preview)
            role_counts[key] = bucket
        bucket["relation_hit_count"] += 1
        concept_ids = bucket.get("_concept_ids")
        if isinstance(concept_ids, set):
            concept_ids.add(target_concept_id)
        sample_nodes = bucket.get("sample_reified_nodes")
        if (
            isinstance(sample_nodes, list)
            and len(sample_nodes) < role_expansion_options.max_sample_concepts_per_type
            and frame_id not in sample_nodes
        ):
            sample_nodes.append(frame_id)
        sample_fillers = bucket.get("sample_fillers")
        if (
            isinstance(sample_fillers, list)
            and len(sample_fillers)
            < role_expansion_options.max_sample_concepts_per_type
        ):
            sample = _compact_concept_sample(preview)
            if sample and sample not in sample_fillers:
                sample_fillers.append(sample)


def _resolve_accessible_concept_type_ids(concept_id: str) -> Optional[List[str]]:
    doc = _load_accessible_preview_document(concept_id)
    if not isinstance(doc, Mapping):
        return None
    relationships = doc.get("relationships")
    if not isinstance(relationships, Mapping):
        return []
    type_ids: List[str] = []
    for predicate_id in ("is_an_instance_of", "#V#is_an_instance_of"):
        type_ids.extend(
            _normalise_relationship_targets(relationships.get(predicate_id))
        )
    if should_enforce_access_control():
        type_ids = _filter_accessible_concept_ids(type_ids)
    return list(dict.fromkeys(type_ids))


def _is_type_membership_predicate_id(predicate_id: str) -> bool:
    return predicate_id.strip().lower() in _TYPE_MEMBERSHIP_PREDICATES


def _compact_concept_sample(preview: Mapping[str, Any]) -> Dict[str, Any]:
    sample: Dict[str, Any] = {}
    concept_id = preview.get("concept_id")
    if isinstance(concept_id, str) and concept_id.strip():
        sample["concept_id"] = concept_id.strip()
    name = preview.get("name")
    if isinstance(name, str) and name.strip():
        sample["name"] = name.strip()
    kind = preview.get("kind")
    if isinstance(kind, str) and kind.strip():
        sample["kind"] = kind.strip()
    type_ids = preview.get("type_ids")
    if isinstance(type_ids, list):
        clean_type_ids = [
            str(type_id).strip()
            for type_id in type_ids[:6]
            if isinstance(type_id, str) and str(type_id).strip()
        ]
        if clean_type_ids:
            sample["type_ids"] = clean_type_ids
    return sample


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
        "_argument_type_counts": {},
        "_untyped_argument_count": 0,
        "_literal_argument_count": 0,
        "_inaccessible_argument_count": 0,
        "_role_expansion_frames": {},
        "_role_expansion_role_counts": {},
        "_role_expansion_anchor_directions": set(),
        "_role_expansion_untyped_filler_count": 0,
        "_role_expansion_literal_filler_count": 0,
        "_role_expansion_inaccessible_filler_count": 0,
    }
    predicate_preview = _resolve_concept_preview(
        predicate_concept_id,
        include_concept_preview,
        preview_cache,
    )
    if predicate_preview is not None:
        row["predicate_preview"] = predicate_preview
    return row


def _normalise_predicate_incidence_type_count_options(
    *,
    include_argument_type_counts: bool,
    type_count_mode: Optional[str],
    include_untyped_bucket: bool,
    max_types_per_predicate: Optional[int],
    max_sample_concepts_per_type: Optional[int],
) -> PredicateIncidenceTypeCountOptions:
    mode = str(type_count_mode or _TYPE_COUNT_MODE_DIRECT_ASSERTED).strip().lower()
    if mode in {"", "direct", "direct_types", "direct_asserted_types"}:
        mode = _TYPE_COUNT_MODE_DIRECT_ASSERTED
    if mode != _TYPE_COUNT_MODE_DIRECT_ASSERTED:
        raise ValueError("type_count_mode currently supports direct_asserted only")
    return PredicateIncidenceTypeCountOptions(
        include=bool(include_argument_type_counts),
        mode=mode,
        include_untyped_bucket=bool(include_untyped_bucket),
        max_types_per_predicate=_coerce_bounded_positive_int(
            max_types_per_predicate,
            default=20,
            minimum=1,
            maximum=100,
        ),
        max_sample_concepts_per_type=_coerce_bounded_positive_int(
            max_sample_concepts_per_type,
            default=3,
            minimum=1,
            maximum=10,
        ),
    )


def _normalise_predicate_incidence_role_expansion_options(
    *,
    role_expansion_mode: Optional[str],
    role_node_type_filter: Optional[Sequence[str]],
    role_predicate_filter: Optional[Sequence[str]],
    role_expansion_depth: Optional[int],
    max_sample_concepts_per_type: int,
) -> PredicateIncidenceRoleExpansionOptions:
    mode = str(role_expansion_mode or _ROLE_EXPANSION_MODE_NONE).strip().lower()
    if mode in {"", "false", "off", "disabled"}:
        mode = _ROLE_EXPANSION_MODE_NONE
    if mode in {"role_expanded", "role_expansion", "expanded"}:
        mode = _ROLE_EXPANSION_MODE_EXPLICIT
    if mode not in {
        _ROLE_EXPANSION_MODE_NONE,
        _ROLE_EXPANSION_MODE_EXPLICIT,
        _ROLE_EXPANSION_MODE_AUTO,
    }:
        raise ValueError("role_expansion_mode must be one of: none, explicit, auto")
    depth = _coerce_bounded_positive_int(
        role_expansion_depth,
        default=1,
        minimum=1,
        maximum=1,
    )
    return PredicateIncidenceRoleExpansionOptions(
        mode=mode,
        node_type_filter=tuple(_normalise_exact_concept_terms(role_node_type_filter)),
        predicate_filter=tuple(_normalise_exact_concept_terms(role_predicate_filter)),
        depth=depth,
        max_sample_concepts_per_type=max_sample_concepts_per_type,
    )


def _coerce_bounded_positive_int(
    raw: Optional[int],
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    if value < minimum:
        return minimum
    return min(value, maximum)


def _normalise_exact_concept_terms(raw: Optional[Sequence[str]]) -> List[str]:
    if raw is None:
        return []
    values: Sequence[Any]
    if isinstance(raw, str):
        values = [raw]
    else:
        values = raw
    normalised: List[str] = []
    for candidate in values:
        if not isinstance(candidate, str):
            continue
        token = candidate.strip()
        if token:
            normalised.append(token)
    return list(dict.fromkeys(normalised))


def _build_typed_predicate_incidence_diagnostics(
    *,
    type_count_options: PredicateIncidenceTypeCountOptions,
    role_expansion_options: PredicateIncidenceRoleExpansionOptions,
) -> Dict[str, Any]:
    role_status = "disabled"
    if role_expansion_options.mode == _ROLE_EXPANSION_MODE_EXPLICIT:
        role_status = "explicit"
    elif role_expansion_options.mode == _ROLE_EXPANSION_MODE_AUTO:
        role_status = "metadata_unavailable"
    return {
        "include_argument_type_counts": type_count_options.include,
        "type_count_mode": type_count_options.mode,
        "include_untyped_bucket": type_count_options.include_untyped_bucket,
        "max_types_per_predicate": type_count_options.max_types_per_predicate,
        "max_sample_concepts_per_type": (
            type_count_options.max_sample_concepts_per_type
        ),
        "role_expansion_mode": role_expansion_options.mode,
        "role_expansion_depth": role_expansion_options.depth,
        "role_expansion_status": role_status,
        "role_node_type_filter_count": len(role_expansion_options.node_type_filter),
        "role_predicate_filter_count": len(role_expansion_options.predicate_filter),
    }


def _extract_predicate_incidence_role_expansions(
    rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    expansions: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        expansion = row.get("role_expansion")
        if isinstance(expansion, Mapping):
            expansions.append(dict(expansion))
    return expansions


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
    target_concept_id = _extract_preview_concept_id(
        target_preview
    ) or _normalise_concept_id(hit.get("target_value"))
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
    if (
        isinstance(target_value, str)
        and target_value.strip()
        and not target_value.startswith("#V#")
    ):
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
    final_row = {key: value for key, value in row.items() if not key.startswith("_")}
    final_row["grounding_count"] = len(row.get("_grounding_keys") or ())
    argument_indexes = row.get("_argument_indexes") or set()
    final_row["argument_indexes"] = sorted(
        int(index) for index in argument_indexes if isinstance(index, int)
    )
    if mode == "type":
        final_row["grounded_instance_count"] = len(row.get("_instance_ids") or ())
    argument_type_counts = _finalise_argument_type_counts(row)
    if argument_type_counts:
        final_row["argument_type_counts"] = argument_type_counts
    untyped_argument_count = row.get("_untyped_argument_count")
    if isinstance(untyped_argument_count, (int, float)) and untyped_argument_count:
        final_row["untyped_argument_count"] = int(untyped_argument_count)
    literal_argument_count = row.get("_literal_argument_count")
    if isinstance(literal_argument_count, (int, float)) and literal_argument_count:
        final_row["literal_argument_count"] = int(literal_argument_count)
    inaccessible_argument_count = row.get("_inaccessible_argument_count")
    if (
        isinstance(inaccessible_argument_count, (int, float))
        and inaccessible_argument_count
    ):
        final_row["inaccessible_argument_count"] = int(inaccessible_argument_count)
    role_expansion = _finalise_role_expansion(row)
    if role_expansion:
        final_row["role_expansion"] = role_expansion
    return final_row


def _finalise_argument_type_counts(row: Mapping[str, Any]) -> List[Dict[str, Any]]:
    buckets = row.get("_argument_type_counts")
    if not isinstance(buckets, Mapping):
        return []
    final_counts: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        if not isinstance(bucket, Mapping):
            continue
        concept_ids = bucket.get("_concept_ids")
        concept_count = len(concept_ids) if isinstance(concept_ids, set) else 0
        entry = {
            key: value for key, value in bucket.items() if not str(key).startswith("_")
        }
        entry["concept_count"] = concept_count
        final_counts.append(entry)
    final_counts.sort(
        key=lambda item: (
            int(item.get("argument_index") or 0),
            str(item.get("argument_role") or ""),
            -int(item.get("relation_hit_count") or 0),
            str(item.get("type_concept_id") or ""),
        )
    )
    max_types = row.get("_max_types_per_predicate")
    if isinstance(max_types, (int, float)) and max_types > 0:
        return final_counts[: int(max_types)]
    return final_counts


def _finalise_role_expansion(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    frames = row.get("_role_expansion_frames")
    if not isinstance(frames, Mapping) or not frames:
        return None
    role_counts = _finalise_role_expansion_role_counts(row)
    directions = row.get("_role_expansion_anchor_directions")
    clean_directions = (
        sorted(direction for direction in directions if isinstance(direction, str))
        if isinstance(directions, set)
        else []
    )
    anchor_direction = (
        clean_directions[0]
        if len(clean_directions) == 1
        else ("mixed" if clean_directions else "unknown")
    )
    sample_frames: List[Dict[str, Any]] = []
    for frame in list(frames.values())[:4]:
        if not isinstance(frame, Mapping):
            continue
        frame_sample: Dict[str, Any] = {}
        concept_id = frame.get("concept_id")
        if isinstance(concept_id, str) and concept_id.strip():
            frame_sample["concept_id"] = concept_id.strip()
        type_ids = frame.get("type_ids")
        if isinstance(type_ids, list):
            clean_type_ids = [
                str(type_id).strip()
                for type_id in type_ids[:6]
                if isinstance(type_id, str) and str(type_id).strip()
            ]
            if clean_type_ids:
                frame_sample["type_ids"] = clean_type_ids
        role_predicates = frame.get("_role_predicates")
        if isinstance(role_predicates, set):
            frame_sample["role_predicate_count"] = len(role_predicates)
        if frame_sample:
            sample_frames.append(frame_sample)
    expansion: Dict[str, Any] = {
        "anchor_predicate_concept_id": row.get("predicate_concept_id"),
        "anchor_direction": anchor_direction,
        "reified_node_count": len(frames),
        "reified_node_type_counts": _finalise_role_expansion_frame_type_counts(frames),
        "role_filler_type_counts": role_counts,
        "sample_reified_nodes": sample_frames,
    }
    for field_name, output_name in (
        ("_role_expansion_untyped_filler_count", "untyped_filler_count"),
        ("_role_expansion_literal_filler_count", "literal_filler_count"),
        ("_role_expansion_inaccessible_filler_count", "inaccessible_filler_count"),
    ):
        value = row.get(field_name)
        if isinstance(value, (int, float)) and value:
            expansion[output_name] = int(value)
    return {
        key: value for key, value in expansion.items() if value not in (None, [], {})
    }


def _finalise_role_expansion_frame_type_counts(
    frames: Mapping[Any, Any],
) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for frame in frames.values():
        if not isinstance(frame, Mapping):
            continue
        frame_id = frame.get("concept_id")
        type_ids = frame.get("type_ids")
        if not isinstance(frame_id, str) or not isinstance(type_ids, list):
            continue
        for type_id in list(dict.fromkeys(type_ids)):
            if not isinstance(type_id, str) or not type_id.strip():
                continue
            bucket = buckets.setdefault(
                type_id.strip(),
                {
                    "type_concept_id": type_id.strip(),
                    "_concept_ids": set(),
                    "sample_reified_nodes": [],
                },
            )
            concept_ids = bucket.get("_concept_ids")
            if isinstance(concept_ids, set):
                concept_ids.add(frame_id)
            samples = bucket.get("sample_reified_nodes")
            if (
                isinstance(samples, list)
                and len(samples) < 3
                and frame_id not in samples
            ):
                samples.append(frame_id)
    final_counts: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        concept_ids = bucket.get("_concept_ids")
        entry = {
            key: value for key, value in bucket.items() if not str(key).startswith("_")
        }
        entry["concept_count"] = len(concept_ids) if isinstance(concept_ids, set) else 0
        final_counts.append(entry)
    return sorted(
        final_counts,
        key=lambda item: (
            -int(item.get("concept_count") or 0),
            str(item.get("type_concept_id") or ""),
        ),
    )


def _finalise_role_expansion_role_counts(
    row: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    buckets = row.get("_role_expansion_role_counts")
    if not isinstance(buckets, Mapping):
        return []
    final_counts: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        if not isinstance(bucket, Mapping):
            continue
        concept_ids = bucket.get("_concept_ids")
        entry = {
            key: value for key, value in bucket.items() if not str(key).startswith("_")
        }
        entry["concept_count"] = len(concept_ids) if isinstance(concept_ids, set) else 0
        final_counts.append(entry)
    return sorted(
        final_counts,
        key=lambda item: (
            str(item.get("role_predicate_concept_id") or ""),
            -int(item.get("relation_hit_count") or 0),
            str(item.get("type_concept_id") or ""),
        ),
    )


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
        doc = ConceptsRepository.find_one(
            {"concept_id": concept_id}, _PREVIEW_DOC_PROJECTION
        )
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
    type_ids: List[str] = []
    if isinstance(relationships, Mapping):
        for predicate_id in ("is_an_instance_of", "#V#is_an_instance_of"):
            type_ids.extend(
                _normalise_relationship_targets(relationships.get(predicate_id))
            )
    if should_enforce_access_control():
        type_ids = _filter_accessible_concept_ids(type_ids)
    type_ids = list(dict.fromkeys(type_ids))

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
        if (
            isinstance(entry, str)
            and entry.startswith("#")
            and not can_access_concept(entry)
        ):
            continue
        filtered.append(entry)
    return filtered


def _filter_accessible_concept_ids(concept_ids: Sequence[str]) -> List[str]:
    if not should_enforce_access_control():
        return [
            str(concept_id).strip()
            for concept_id in concept_ids
            if isinstance(concept_id, str) and str(concept_id).strip()
        ]
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
                "access_granted": source_preview is not None
                or not include_concept_preview,
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


def _iter_predicate_filter_tokens(
    predicate_filter: Optional[Sequence[str]],
) -> List[str]:
    if predicate_filter is None:
        return []
    values: Sequence[Any]
    if isinstance(predicate_filter, str):
        values = [predicate_filter]
    else:
        values = predicate_filter
    tokens: List[str] = []
    for candidate in values:
        if not isinstance(candidate, str):
            continue
        raw_token = candidate.strip()
        if raw_token.startswith("[") and raw_token.endswith("]") and "," in raw_token:
            split_tokens = [
                item.strip().strip("\"'")
                for item in raw_token[1:-1].split(",")
                if item.strip().strip("\"'")
            ]
        else:
            split_tokens = [raw_token]
        for split_token in split_tokens:
            token = split_token.strip()
            if token:
                tokens.append(token)
    return tokens


def _normalise_predicate_terms(
    predicate_filter: Optional[Sequence[str]],
) -> List[str]:
    return [token.lower() for token in _iter_predicate_filter_tokens(predicate_filter)]


def _normalise_predicate_display_terms(
    predicate_filter: Optional[Sequence[str]],
) -> List[str]:
    return _iter_predicate_filter_tokens(predicate_filter)


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
