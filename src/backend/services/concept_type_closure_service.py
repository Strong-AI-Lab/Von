"""Bounded, actor-visible type-closure reads for concept presentation paths."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository

DEFAULT_MAX_DEPTH = 16
DEFAULT_MAX_NODES = 250


def _normalise_ids(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for item in raw:
        concept_id = str(item or "").strip()
        if not concept_id or concept_id in seen:
            continue
        seen.add(concept_id)
        ordered.append(concept_id)
    return ordered


def load_type_closure(
    direct_type_ids: Sequence[str] | None,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict[str, Any]:
    """Return direct types plus ancestors using one access-filtered query per depth.

    The limits protect concept-page latency from malformed cycles or unexpectedly
    broad hierarchies. The repository applies the trusted actor visibility filter.
    """

    direct = _normalise_ids(direct_type_ids)
    ordered = list(direct)
    seen = set(direct)
    frontier = list(direct)
    docs_by_id: dict[str, dict[str, Any]] = {}
    depth = 0
    truncated = False

    while frontier and depth < max_depth and len(seen) <= max_nodes:
        docs = list(
            ConceptsRepository.find(
                {"concept_id": {"$in": frontier}},
                {"concept_id": 1, "name": 1, "names": 1, "relationships": 1},
                limit=min(len(frontier), max_nodes),
            )
        )
        next_frontier: list[str] = []
        for doc in docs:
            if not isinstance(doc, Mapping):
                continue
            concept_id = str(doc.get("concept_id") or "").strip()
            if not concept_id:
                continue
            docs_by_id[concept_id] = dict(doc)
            relationships = doc.get("relationships")
            parents = _normalise_ids(
                relationships.get("is_a_type_of")
                if isinstance(relationships, Mapping)
                else None
            )
            for parent_id in parents:
                if parent_id in seen:
                    continue
                if len(seen) >= max_nodes:
                    truncated = True
                    break
                seen.add(parent_id)
                ordered.append(parent_id)
                next_frontier.append(parent_id)
        frontier = next_frontier
        depth += 1

    if frontier:
        truncated = True
    return {
        "ordered_type_ids": ordered,
        "ancestor_type_ids": ordered[len(direct) :],
        "documents_by_id": docs_by_id,
        "depth": depth,
        "truncated": truncated,
    }


__all__ = ["load_type_closure"]
