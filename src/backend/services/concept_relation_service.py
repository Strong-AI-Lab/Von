"""Helpers for assembling relation payloads for concept fetch requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .text_value_service import get_texts_for_concept
from ..db.repositories.concepts_repository import ConceptsRepository, RELATIONSHIP_KINDS
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..vontology.utils_vontology import (
    get_concept_display_name_with_names_fallback,
    is_predicate,
    is_type,
)

RelationValue = Dict[str, Any]

_ARG_INDEX_SUBJECT = 1  # Align with example payloads (1-based indexing)
_ARG_INDEX_FIRST_OBJECT = 2
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 500


@dataclass
class RelationOptions:
    include_relations_arg1: bool = False
    include_relations_any_arg: bool = False
    include_text_relations_arg1: Union[bool, str] = False
    predicate_filter: Optional[Sequence[str]] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    include_concept_preview: bool = True


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
    structural_predicates = set(RELATIONSHIP_KINDS)
    if predicate_allow_list:
        structural_predicates.update(
            key for key in predicate_allow_list if isinstance(key, str)
        )
    include_text_relations = bool(include_text_relations_arg1)
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

    if include_relations_arg1:
        relationships = (concept.get("relationships") or {}) if concept else {}
        _collect_structural_relations_for_subject(
            concept_id=concept_id,
            relationships=relationships,
            predicate_allowed=_predicate_allowed,
            include_preview=include_concept_preview,
            preview_cache=preview_cache,
            consumer=_record,
        )

    if include_relations_any_arg:
        _collect_structural_relations_for_targets(
            concept_id=concept_id,
            predicate_allowed=_predicate_allowed,
            structural_predicates=structural_predicates,
            include_preview=include_concept_preview,
            preview_cache=preview_cache,
            consumer=_record,
        )

    if include_text_relations:
        _collect_text_relations_for_subject(
            concept_id=concept_id,
            predicate_allowed=_predicate_allowed,
            include_preview=include_concept_preview,
            preview_cache=preview_cache,
            snippet_mode=snippet_mode,
            consumer=_record,
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
    include_arg1 = argument_filter in (None, _ARG_INDEX_SUBJECT)
    include_arg2_or_later = argument_filter is None or argument_filter >= _ARG_INDEX_FIRST_OBJECT

    preview_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    hits: List[Dict[str, Any]] = []

    subject_doc = ConceptsRepository.find_one(
        {"concept_id": resolved_concept_id},
        {"concept_id": 1, "name": 1, "relationships": 1, "updated_at": 1},
    )

    if include_structural and subject_doc:
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
                    }
                )

    if include_structural and include_arg2_or_later:
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
                    }
                )

    if include_text and include_arg1:
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
            }
            snippet = _make_argument_match_snippet(
                text=text_value,
                needle=resolved_concept_id,
                include_snippets=include_text_snippets,
            )
            if snippet:
                hit["text_snippet"] = snippet
            hits.append(hit)

    if include_text and include_arg2_or_later:
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
    doc = ConceptsRepository.find_one(
        {"concept_id": concept_id},
        {
            "concept_id": 1,
            "name": 1,
            "relationships": 1,
            "updated_at": 1,
        },
    )
    if not doc:
        preview_cache[concept_id] = None
        return None
    name = doc.get("name")
    if not name:
        try:
            name = get_concept_display_name_with_names_fallback(doc)
        except Exception:  # pragma: no cover - best effort fallback
            name = doc.get("concept_id")
    kind = doc.get("kind")
    if not kind:
        try:
            # Check predicate FIRST: predicates can have is_a_type_of relationships
            if is_predicate(doc):
                kind = "predicate"
            elif is_type(doc):
                kind = "type"
            else:
                kind = "individual"
        except Exception:  # pragma: no cover
            kind = "unknown"
    updated = doc.get("updated_at")
    preview = {
        "concept_id": doc.get("concept_id"),
        "name": name,
        "kind": kind,
        "last_updated": _isoformat(updated),
    }
    preview_cache[concept_id] = preview
    return preview


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
