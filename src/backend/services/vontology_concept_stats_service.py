"""Ultra-fast concept statistics snapshots for Vontology.

JVNAUTOSCI-959:
- Provide O(1) lookup for per-concept tree/extent-derived statistics.
- Enforce strict invalidation via a mutation-version barrier.
- Fast-fail with explicit status markers (available/stale/rebuilding/failed)
  rather than returning misleading zero values.

Design notes:
- A snapshot is built per access-control scope (see `cache_scope_key()`), so
  namespace isolation remains intact.
- Snapshot reads are constant-time dictionary lookups.
- Invalidation does not silently recompute; callers choose whether to rebuild.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import TextRelationsRepository
from ..security.access_control import cache_scope_key
from ..utils.time_utils import utc_iso_now
from ..vontology.utils_vontology import is_predicate, is_pure_instance, is_type
from .text_relation_read_policy import (
    generic_text_read_predicate_filter,
    is_hidden_from_generic_text_reads,
)

logger = logging.getLogger(__name__)

STATS_STATUS_AVAILABLE = "available"
STATS_STATUS_STALE = "stale"
STATS_STATUS_REBUILDING = "rebuilding"
STATS_STATUS_FAILED = "failed"

_STATE_LOCK = threading.Lock()
_SCOPE_STATE: dict[str, dict[str, Any]] = {}
_MUTATION_VERSION = 0


def _new_scope_state() -> dict[str, Any]:
    return {
        "snapshot_by_concept": None,  # dict[str, dict[str, Any]] | None
        "snapshot_mutation_version": None,
        "stats_generated_at": None,
        "is_rebuilding": False,
        "last_build_started_at": None,
        "last_build_finished_at": None,
        "last_build_duration_ms": None,
        "last_build_error": None,
        "last_build_details": {},
        "last_rebuild_reason": None,
        "rebuild_count": 0,
        "invalidation_count": 0,
        "invalidated_at": None,
        "invalidation_reason": None,
        "invalidated_concepts_sample": [],
    }


def _resolve_scope_key(scope_key: str | None) -> str:
    if isinstance(scope_key, str) and scope_key.strip():
        return scope_key.strip()
    return cache_scope_key()


def _scope_status(state: Mapping[str, Any], current_version: int) -> str:
    snapshot = state.get("snapshot_by_concept")
    snapshot_version = state.get("snapshot_mutation_version")

    has_snapshot = isinstance(snapshot, dict)
    if has_snapshot and snapshot_version == current_version:
        return STATS_STATUS_AVAILABLE
    if bool(state.get("is_rebuilding")):
        return STATS_STATUS_REBUILDING
    if (
        has_snapshot
        and isinstance(snapshot_version, int)
        and snapshot_version < current_version
    ):
        return STATS_STATUS_STALE
    return STATS_STATUS_FAILED


def _normalise_id_values(raw: Any) -> set[str]:
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        return set()
    out: set[str] = set()
    for item in values:
        if isinstance(item, str):
            value = item.strip()
            if value.startswith("#V#"):
                out.add(value)
    return out


def _relationship_value_cardinality(raw: Any) -> int:
    if isinstance(raw, str):
        return 1 if raw.strip() else 0
    if isinstance(raw, list):
        return sum(1 for item in raw if isinstance(item, str) and bool(item.strip()))
    return 0


def _compute_snapshot_for_scope() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    projection = {
        "concept_id": 1,
        "relationships": 1,
    }
    docs = list(ConceptsRepository.find({}, projection))

    relationships_by_id: dict[str, dict[str, Any]] = {}
    kind_by_id: dict[str, str] = {}
    type_ids: set[str] = set()
    predicate_ids: set[str] = set()

    for doc in docs:
        if not isinstance(doc, Mapping):
            continue
        concept_id = doc.get("concept_id")
        if not isinstance(concept_id, str) or not concept_id.startswith("#V#"):
            continue

        relationships_raw = doc.get("relationships")
        relationships = (
            dict(relationships_raw) if isinstance(relationships_raw, Mapping) else {}
        )
        relationships_by_id[concept_id] = relationships

        node_stub = {
            "concept_id": concept_id,
            "relationships": relationships,
        }
        if is_predicate(node_stub):
            kind = "predicate"
            predicate_ids.add(concept_id)
        elif is_type(node_stub):
            kind = "type"
            type_ids.add(concept_id)
        else:
            kind = "individual"
        kind_by_id[concept_id] = kind

    type_parents: dict[str, set[str]] = defaultdict(set)
    type_children: dict[str, set[str]] = defaultdict(set)
    direct_instance_types_by_concept: dict[str, set[str]] = {}

    relationship_extent_counts: dict[str, int] = defaultdict(int)

    for concept_id, relationships in relationships_by_id.items():
        # Predicate extents from concept->concept relationship payloads.
        for predicate, value in relationships.items():
            if (
                isinstance(predicate, str)
                and predicate.startswith("#V#")
                and not is_hidden_from_generic_text_reads(predicate)
            ):
                relationship_extent_counts[predicate] += (
                    _relationship_value_cardinality(value)
                )

        # Type hierarchy for subtype/ancestor computations.
        if concept_id in type_ids:
            parents = {
                parent_id
                for parent_id in _normalise_id_values(relationships.get("is_a_type_of"))
                if parent_id in type_ids and parent_id != concept_id
            }
            type_parents[concept_id] = parents
            for parent_id in parents:
                type_children[parent_id].add(concept_id)

        # Direct instance typing (for direct + inherited instance counts).
        direct_types = {
            type_id
            for type_id in _normalise_id_values(relationships.get("is_an_instance_of"))
            if type_id in type_ids
        }
        if direct_types:
            direct_instance_types_by_concept[concept_id] = direct_types

    descendant_cache: dict[str, set[str]] = {}

    def _descendants(type_id: str, trail: set[str]) -> set[str]:
        cached = descendant_cache.get(type_id)
        if cached is not None:
            return cached
        if type_id in trail:
            return set()
        trail.add(type_id)
        result: set[str] = set()
        for child_id in type_children.get(type_id, set()):
            if child_id in trail:
                continue
            result.add(child_id)
            result.update(_descendants(child_id, trail))
        trail.remove(type_id)
        descendant_cache[type_id] = result
        return result

    ancestor_cache: dict[str, set[str]] = {}

    def _ancestors_including_self(type_id: str, trail: set[str]) -> set[str]:
        cached = ancestor_cache.get(type_id)
        if cached is not None:
            return cached
        if type_id in trail:
            return {type_id}
        trail.add(type_id)
        result: set[str] = {type_id}
        for parent_id in type_parents.get(type_id, set()):
            result.update(_ancestors_including_self(parent_id, trail))
        trail.remove(type_id)
        ancestor_cache[type_id] = result
        return result

    direct_instance_count_by_type = {type_id: 0 for type_id in type_ids}
    direct_pure_instance_count_by_type = {type_id: 0 for type_id in type_ids}
    total_instance_count_by_type = {type_id: 0 for type_id in type_ids}

    for concept_id, direct_types in direct_instance_types_by_concept.items():
        for direct_type in direct_types:
            direct_instance_count_by_type[direct_type] = (
                direct_instance_count_by_type.get(direct_type, 0) + 1
            )
        doc_relationships = relationships_by_id.get(concept_id) or {}
        if is_pure_instance(
            {
                "concept_id": concept_id,
                "relationships": doc_relationships,
            }
        ):
            for direct_type in direct_types:
                direct_pure_instance_count_by_type[direct_type] = (
                    direct_pure_instance_count_by_type.get(direct_type, 0) + 1
                )

        seen_for_concept: set[str] = set()
        for direct_type in direct_types:
            for ancestor_id in _ancestors_including_self(direct_type, set()):
                if ancestor_id in type_ids and ancestor_id not in seen_for_concept:
                    total_instance_count_by_type[ancestor_id] = (
                        total_instance_count_by_type.get(ancestor_id, 0) + 1
                    )
                    seen_for_concept.add(ancestor_id)

    text_extent_counts: dict[str, int] = {}
    text_extent_available = True
    text_extent_error: str | None = None

    if TextRelationsRepository.collection() is None:
        text_extent_available = False
        text_extent_error = "text_relations_collection_unavailable"
    else:
        try:
            pipeline = [
                {
                    "$match": {
                        "predicate": {
                            "$regex": r"^#V#",
                            **generic_text_read_predicate_filter(),
                        }
                    }
                },
                {"$group": {"_id": "$predicate", "count": {"$sum": 1}}},
            ]
            for row in TextRelationsRepository.aggregate(pipeline):
                if not isinstance(row, Mapping):
                    continue
                predicate = row.get("_id")
                count_raw = row.get("count")
                if (
                    isinstance(predicate, str)
                    and isinstance(count_raw, (int, float))
                    and not is_hidden_from_generic_text_reads(predicate)
                ):
                    text_extent_counts[predicate] = int(count_raw)
        except Exception as exc:  # pragma: no cover - defensive
            text_extent_available = False
            text_extent_error = str(exc)

    snapshot_by_concept: dict[str, dict[str, Any]] = {}
    for concept_id, kind in kind_by_id.items():
        entry: dict[str, Any] = {"kind": kind}

        if kind == "type":
            direct_instance_count = int(
                direct_instance_count_by_type.get(concept_id, 0)
            )
            direct_pure_instance_count = int(
                direct_pure_instance_count_by_type.get(concept_id, 0)
            )
            total_instance_count = int(total_instance_count_by_type.get(concept_id, 0))
            direct_subtype_count = int(len(type_children.get(concept_id, set())))
            total_subtype_count = int(len(_descendants(concept_id, set())))

            entry.update(
                {
                    "has_any_instances_in_subtree": total_instance_count > 0,
                    "has_direct_pure_instances": direct_pure_instance_count > 0,
                    "direct_instance_count": direct_instance_count,
                    "direct_pure_instance_count": direct_pure_instance_count,
                    "total_instance_count_in_subtree": total_instance_count,
                    "direct_subtype_count": direct_subtype_count,
                    "total_subtype_count_in_subtree": total_subtype_count,
                }
            )
        elif kind == "predicate":
            if text_extent_available:
                extent_count = int(
                    relationship_extent_counts.get(concept_id, 0)
                    + text_extent_counts.get(concept_id, 0)
                )
                entry.update(
                    {
                        "extent_count": extent_count,
                        "extent_count_is_exact": True,
                        "extent_count_unavailable": False,
                    }
                )
            else:
                entry.update(
                    {
                        "extent_count": None,
                        "extent_count_is_exact": False,
                        "extent_count_unavailable": True,
                    }
                )

        snapshot_by_concept[concept_id] = entry

    details = {
        "concept_count": len(snapshot_by_concept),
        "type_count": len(type_ids),
        "predicate_count": len(predicate_ids),
        "extent_text_relations_available": text_extent_available,
        "extent_text_relations_error": text_extent_error,
    }
    return snapshot_by_concept, details


def invalidate_vontology_concept_stats_cache(
    *,
    reason: str,
    affected_concepts: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Invalidate all scope snapshots by advancing mutation version."""

    global _MUTATION_VERSION
    now_iso = utc_iso_now()
    affected_sample = [
        concept_id
        for concept_id in (affected_concepts or [])
        if isinstance(concept_id, str) and concept_id.startswith("#V#")
    ][:25]

    with _STATE_LOCK:
        _MUTATION_VERSION += 1
        new_version = _MUTATION_VERSION
        for state in _SCOPE_STATE.values():
            state["invalidation_count"] = int(state.get("invalidation_count", 0)) + 1
            state["invalidated_at"] = now_iso
            state["invalidation_reason"] = reason
            state["invalidated_concepts_sample"] = affected_sample

    return {
        "mutation_version": new_version,
        "invalidated_at": now_iso,
        "reason": reason,
        "affected_concepts_sample": affected_sample,
    }


def rebuild_vontology_concept_stats_snapshot(
    *,
    scope_key: str | None = None,
    reason: str = "manual",
) -> dict[str, Any]:
    """Build a fresh scope snapshot from repository state."""

    scope = _resolve_scope_key(scope_key)

    with _STATE_LOCK:
        state = _SCOPE_STATE.setdefault(scope, _new_scope_state())
        if state.get("is_rebuilding"):
            return {
                "success": False,
                "scope_key": scope,
                "stats_status": STATS_STATUS_REBUILDING,
                "message": "rebuild_already_in_progress",
            }
        state["is_rebuilding"] = True
        state["last_build_started_at"] = utc_iso_now()
        state["last_rebuild_reason"] = reason
        target_mutation_version = _MUTATION_VERSION

    start = time.perf_counter()
    try:
        snapshot_by_concept, details = _compute_snapshot_for_scope()
    except Exception as exc:
        with _STATE_LOCK:
            state = _SCOPE_STATE.setdefault(scope, _new_scope_state())
            state["is_rebuilding"] = False
            state["last_build_finished_at"] = utc_iso_now()
            state["last_build_error"] = str(exc)
            state["last_build_duration_ms"] = int((time.perf_counter() - start) * 1000)
        logger.error("Failed rebuilding Vontology concept stats snapshot: %s", exc)
        return {
            "success": False,
            "scope_key": scope,
            "stats_status": STATS_STATUS_FAILED,
            "error": str(exc),
        }

    duration_ms = int((time.perf_counter() - start) * 1000)
    generated_at = utc_iso_now()
    with _STATE_LOCK:
        state = _SCOPE_STATE.setdefault(scope, _new_scope_state())
        state["snapshot_by_concept"] = snapshot_by_concept
        state["snapshot_mutation_version"] = target_mutation_version
        state["stats_generated_at"] = generated_at
        state["is_rebuilding"] = False
        state["last_build_finished_at"] = generated_at
        state["last_build_duration_ms"] = duration_ms
        state["last_build_error"] = None
        state["last_build_details"] = details
        state["rebuild_count"] = int(state.get("rebuild_count", 0)) + 1
        current_mutation_version = _MUTATION_VERSION

    status = (
        STATS_STATUS_AVAILABLE
        if target_mutation_version == current_mutation_version
        else STATS_STATUS_STALE
    )
    return {
        "success": True,
        "scope_key": scope,
        "stats_status": status,
        "stats_generated_at": generated_at,
        "snapshot_mutation_version": target_mutation_version,
        "current_mutation_version": current_mutation_version,
        "build_duration_ms": duration_ms,
        "build_details": details,
    }


def get_vontology_concept_stats(
    concept_ids: Sequence[str],
    *,
    scope_key: str | None = None,
    rebuild_if_needed: bool = False,
    include_stale_values: bool = False,
) -> dict[str, Any]:
    """Return stats for specific concept IDs, with explicit cache status markers."""

    scope = _resolve_scope_key(scope_key)
    requested_ids = [
        concept_id
        for concept_id in dict.fromkeys(concept_ids)
        if isinstance(concept_id, str) and concept_id.startswith("#V#")
    ]

    if rebuild_if_needed:
        with _STATE_LOCK:
            state = _SCOPE_STATE.setdefault(scope, _new_scope_state())
            current_version = _MUTATION_VERSION
            status = _scope_status(state, current_version)
        if status != STATS_STATUS_AVAILABLE:
            rebuild_vontology_concept_stats_snapshot(
                scope_key=scope, reason="on_demand"
            )

    with _STATE_LOCK:
        state = _SCOPE_STATE.setdefault(scope, _new_scope_state())
        current_version = _MUTATION_VERSION
        status = _scope_status(state, current_version)
        snapshot = state.get("snapshot_by_concept")
        if not isinstance(snapshot, dict):
            snapshot = {}
        snapshot_version = state.get("snapshot_mutation_version")
        generated_at = state.get("stats_generated_at")
        last_error = state.get("last_build_error")

    concept_stats: dict[str, dict[str, Any]] = {}
    for concept_id in requested_ids:
        cached_entry = snapshot.get(concept_id)
        if isinstance(cached_entry, Mapping):
            entry: dict[str, Any] = dict(cached_entry)
        else:
            entry = {"kind": None}

        if status == STATS_STATUS_AVAILABLE:
            if cached_entry is None:
                entry.update(
                    {
                        "stats_status": STATS_STATUS_AVAILABLE,
                        "stats_generated_at": generated_at,
                        "stats_unavailable": True,
                        "stats_reason": "concept_not_found_in_snapshot",
                    }
                )
            else:
                entry.update(
                    {
                        "stats_status": STATS_STATUS_AVAILABLE,
                        "stats_generated_at": generated_at,
                    }
                )
        elif include_stale_values and cached_entry is not None:
            entry.update(
                {
                    "stats_status": status,
                    "stats_generated_at": generated_at,
                    "stats_unavailable": True,
                    "stats_reason": "stale_snapshot_value",
                }
            )
        else:
            entry = {
                "kind": entry.get("kind"),
                "stats_status": status,
                "stats_generated_at": generated_at,
                "stats_unavailable": True,
                "stats_reason": (
                    "cache_stale"
                    if status == STATS_STATUS_STALE
                    else (
                        "cache_rebuilding"
                        if status == STATS_STATUS_REBUILDING
                        else "cache_unavailable"
                    )
                ),
            }
            if status == STATS_STATUS_FAILED and isinstance(last_error, str):
                entry["stats_error"] = last_error

        concept_stats[concept_id] = entry

    return {
        "scope_key": scope,
        "stats_status": status,
        "stats_generated_at": generated_at,
        "snapshot_mutation_version": snapshot_version,
        "current_mutation_version": current_version,
        "concept_stats": concept_stats,
    }


def get_vontology_concept_stats_cache_summary(
    *, scope_key: str | None = None
) -> dict[str, Any]:
    """Return cache diagnostics for one scope (or current scope)."""

    scope = _resolve_scope_key(scope_key)
    with _STATE_LOCK:
        state = _SCOPE_STATE.setdefault(scope, _new_scope_state())
        current_version = _MUTATION_VERSION
        status = _scope_status(state, current_version)
        snapshot = state.get("snapshot_by_concept")
        snapshot_size = len(snapshot) if isinstance(snapshot, dict) else 0
        return {
            "scope_key": scope,
            "stats_status": status,
            "snapshot_size": snapshot_size,
            "snapshot_mutation_version": state.get("snapshot_mutation_version"),
            "current_mutation_version": current_version,
            "stats_generated_at": state.get("stats_generated_at"),
            "is_rebuilding": bool(state.get("is_rebuilding")),
            "last_build_started_at": state.get("last_build_started_at"),
            "last_build_finished_at": state.get("last_build_finished_at"),
            "last_build_duration_ms": state.get("last_build_duration_ms"),
            "last_build_error": state.get("last_build_error"),
            "rebuild_count": state.get("rebuild_count", 0),
            "invalidation_count": state.get("invalidation_count", 0),
            "invalidated_at": state.get("invalidated_at"),
            "invalidation_reason": state.get("invalidation_reason"),
            "build_details": state.get("last_build_details") or {},
        }


def _reset_vontology_concept_stats_cache_for_tests() -> None:
    """Test helper: clear in-memory cache state."""

    global _MUTATION_VERSION
    with _STATE_LOCK:
        _SCOPE_STATE.clear()
        _MUTATION_VERSION = 0
