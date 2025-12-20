"""Helpers for assembling relation payloads for concept fetch requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .text_value_service import get_texts_for_concept
from ..db.repositories.concepts_repository import ConceptsRepository, RELATIONSHIP_KINDS
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
            if is_type(doc):
                kind = "type"
            elif is_predicate(doc):
                kind = "predicate"
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


def _build_follow_up_actions(
    values: Iterable[str], *, exclude: set[str]
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for val in values:
        if not isinstance(val, str) or val in exclude:
            continue
        actions.append({"action": "fetch_concept", "args": {"concept_id": val}})
    return actions


__all__ = ["build_concept_relations_payload", "RelationOptions"]
