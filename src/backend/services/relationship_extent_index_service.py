"""Derived query index for structured Vontology relationship extents.

Canonical relationship authority remains in ``concepts.relationships``. This
module only materialises one support document per stored concept-to-concept edge
so concept and predicate extent views can query by target/predicate without
unwinding every concept document.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from pymongo import DeleteMany, ReplaceOne, timeout
from pymongo.collection import Collection

from ..db.mongo_client import (
    get_application_settings_collection,
    get_relationship_extent_index_collection,
)
from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import bypass_access_control, can_access_concept
from ..security.access_control import filter_accessible_concept_ids
from .concept_predicate_metadata_service import get_relationship_kinds_set

logger = logging.getLogger(__name__)

RELATIONSHIP_EXTENT_INDEX_STATE_SETTING = "relationship_extent_index_state"
RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION = 1
_READINESS_CACHE: dict[str, Any] = {"ready": None, "checked_at": 0.0}
_READINESS_CACHE_TTL_SECONDS = 10.0
RELATIONSHIP_EXTENT_PAGE_BATCH_SIZE = int(
    os.environ.get("VON_RELATIONSHIP_EXTENT_PAGE_BATCH_SIZE", "128")
)
RELATIONSHIP_EXTENT_PAGE_MAX_INDEX_SCAN = int(
    os.environ.get("VON_RELATIONSHIP_EXTENT_PAGE_MAX_INDEX_SCAN", "128")
)
RELATIONSHIP_EXTENT_PAGE_TIME_BUDGET_MS = int(
    os.environ.get("VON_RELATIONSHIP_EXTENT_PAGE_TIME_BUDGET_MS", "1200")
)
RELATIONSHIP_EXTENT_SYNC_TIMEOUT_SECONDS = max(
    0.1,
    float(os.environ.get("VON_RELATIONSHIP_EXTENT_SYNC_TIMEOUT_SECONDS", "5")),
)
RELATIONSHIP_EXTENT_SYNC_SLOW_MS = max(
    1.0,
    float(os.environ.get("VON_RELATIONSHIP_EXTENT_SYNC_SLOW_MS", "1000")),
)
RELATIONSHIP_EXTENT_STATE_WRITE_TIMEOUT_SECONDS = max(
    0.1,
    float(
        os.environ.get(
            "VON_RELATIONSHIP_EXTENT_STATE_WRITE_TIMEOUT_SECONDS",
            "1",
        )
    ),
)
RELATIONSHIP_EXTENT_READ_TIMEOUT_SECONDS = max(
    0.1,
    float(os.environ.get("VON_RELATIONSHIP_EXTENT_READ_TIMEOUT_SECONDS", "2")),
)
RELATIONSHIP_EXTENT_READINESS_TIMEOUT_SECONDS = max(
    0.1,
    float(os.environ.get("VON_RELATIONSHIP_EXTENT_READINESS_TIMEOUT_SECONDS", "1")),
)
_DEFERRED_SYNC_DEPTH: ContextVar[int] = ContextVar(
    "relationship_extent_deferred_sync_depth",
    default=0,
)
_DEFERRED_SYNC_SOURCE_IDS: ContextVar[set[str] | None] = ContextVar(
    "relationship_extent_deferred_sync_source_ids",
    default=None,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_targets(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        targets: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                targets.append(item.strip())
        return targets
    return []


def _index_docs_for_concept(concept_doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    source_id = concept_doc.get("concept_id")
    if not isinstance(source_id, str) or not source_id.strip():
        return []
    source_id = source_id.strip()

    relationships = concept_doc.get("relationships")
    if not isinstance(relationships, Mapping):
        return []

    source_updated_at = concept_doc.get("updated_at")
    now = _utc_now()
    docs: list[dict[str, Any]] = []
    for predicate_raw, targets_raw in relationships.items():
        if not isinstance(predicate_raw, str) or not predicate_raw.strip():
            continue
        predicate_id = predicate_raw.strip()
        for target_index, target_value in enumerate(_normalise_targets(targets_raw)):
            docs.append(
                {
                    "relation_id": (
                        f"struct::{source_id}::{predicate_id}::target::{target_index}"
                    ),
                    "source_concept_id": source_id,
                    "predicate_id": predicate_id,
                    "target_value": target_value,
                    "target_index": target_index,
                    "arg2_index": target_index + 2,
                    "target_is_concept": target_value.startswith("#"),
                    "source_updated_at": source_updated_at,
                    "updated_at": source_updated_at or now,
                    "indexed_at": now,
                    "schema_version": RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
                    "canonical_source": "concepts.relationships",
                }
            )
    return docs


def _replace_relationship_extent_rows(
    *,
    collection: Collection,
    source_id: str,
    docs: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    """Refresh one source without deleting its old rows before replacements exist."""

    _upsert_relationship_extent_rows(collection=collection, docs=docs)
    relation_ids = [
        relation_id
        for doc in docs
        if isinstance(relation_id := doc.get("relation_id"), str) and relation_id
    ]
    stale_filter: dict[str, Any] = {"source_concept_id": source_id}
    if relation_ids:
        stale_filter["relation_id"] = {"$nin": relation_ids}
    deleted = collection.delete_many(stale_filter).deleted_count
    return deleted, len(docs)


def _upsert_relationship_extent_rows(
    *,
    collection: Collection,
    docs: Sequence[Mapping[str, Any]],
) -> None:
    """Upsert new rows before stale-row removal.

    MongoDB cannot atomically replace a variable-sized per-source row set
    without a transaction. Updating each deterministic relation id first means
    a failed refresh leaves the previous rows available; the caller also marks
    the whole derived index degraded so reads fall back to canonical concepts.
    The narrow fallback supports mongomock and older PyMongo-compatible test
    backends whose bulk-write adapters do not yet accept PyMongo's ``sort``
    argument for ``ReplaceOne``.
    """

    rows = [dict(doc) for doc in docs]
    if not rows:
        return
    operations = [
        ReplaceOne({"relation_id": row["relation_id"]}, row, upsert=True)
        for row in rows
    ]
    try:
        collection.bulk_write(operations, ordered=False)
    except TypeError as exc:
        if "unexpected keyword argument 'sort'" not in str(exc):
            raise
        for row in rows:
            collection.replace_one(
                {"relation_id": row["relation_id"]},
                row,
                upsert=True,
            )


def _mark_relationship_extent_index_degraded(
    *,
    reason: str,
    source_count: int,
    error_type: str,
) -> None:
    """Fail the derived index closed after an incomplete refresh."""

    now = _utc_now()
    _READINESS_CACHE["ready"] = False
    _READINESS_CACHE["checked_at"] = time.monotonic()
    try:
        with timeout(RELATIONSHIP_EXTENT_STATE_WRITE_TIMEOUT_SECONDS):
            _record_rebuild_state(
                {
                    "status": "degraded",
                    "schema_version": RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
                    "reason": reason,
                    "degraded_at": now.isoformat(),
                    "source_count": max(0, int(source_count)),
                    "error_type": error_type,
                }
            )
    except Exception:
        logger.warning(
            "[relationship_extent_index] unable_to_record_degraded_state "
            "source_count=%s error_type=%s",
            max(0, int(source_count)),
            error_type,
            exc_info=True,
        )


def sync_relationship_extent_index_for_concept_doc(
    concept_doc: Mapping[str, Any],
    *,
    collection: Collection | None = None,
) -> dict[str, Any]:
    """Replace derived extent-index rows for one concept document."""

    source_id = concept_doc.get("concept_id")
    if not isinstance(source_id, str) or not source_id.strip():
        return {"success": False, "reason": "missing_source_concept_id"}
    source_id = source_id.strip()

    coll = (
        collection
        if collection is not None
        else get_relationship_extent_index_collection()
    )
    if coll is None:
        return {"success": False, "reason": "collection_unavailable"}

    docs = _index_docs_for_concept(concept_doc)
    started = time.perf_counter()
    try:
        with timeout(RELATIONSHIP_EXTENT_SYNC_TIMEOUT_SECONDS):
            deleted, inserted = _replace_relationship_extent_rows(
                collection=coll,
                source_id=source_id,
                docs=docs,
            )
    except Exception as exc:
        _mark_relationship_extent_index_degraded(
            reason="source_refresh_failed",
            source_count=1,
            error_type=type(exc).__name__,
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000.0
    if duration_ms >= RELATIONSHIP_EXTENT_SYNC_SLOW_MS:
        logger.warning(
            "[relationship_extent_index] sync_slow duration_ms=%.1f "
            "deleted=%s inserted=%s",
            duration_ms,
            deleted,
            inserted,
        )
    return {
        "success": True,
        "source_concept_id": source_id,
        "deleted": deleted,
        "inserted": inserted,
        "duration_ms": round(duration_ms, 3),
    }


def _sync_relationship_extent_index_for_concept_id_now(
    source_id: str,
) -> dict[str, Any]:
    if not isinstance(source_id, str) or not source_id.strip():
        return {"success": False, "reason": "missing_source_concept_id"}
    source_id = source_id.strip()
    coll = get_relationship_extent_index_collection()
    if coll is None:
        return {"success": False, "reason": "collection_unavailable"}

    started = time.perf_counter()
    try:
        with timeout(RELATIONSHIP_EXTENT_SYNC_TIMEOUT_SECONDS):
            with bypass_access_control():
                concept_doc = ConceptsRepository.find_one(
                    {"concept_id": source_id},
                    {"concept_id": 1, "relationships": 1, "updated_at": 1},
                )
            if not concept_doc:
                deleted = coll.delete_many(
                    {"source_concept_id": source_id}
                ).deleted_count
                inserted = 0
            else:
                deleted, inserted = _replace_relationship_extent_rows(
                    collection=coll,
                    source_id=source_id,
                    docs=_index_docs_for_concept(concept_doc),
                )
    except Exception as exc:
        _mark_relationship_extent_index_degraded(
            reason="source_refresh_failed",
            source_count=1,
            error_type=type(exc).__name__,
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000.0
    if duration_ms >= RELATIONSHIP_EXTENT_SYNC_SLOW_MS:
        logger.warning(
            "[relationship_extent_index] sync_slow duration_ms=%.1f "
            "deleted=%s inserted=%s",
            duration_ms,
            deleted,
            inserted,
        )
    result: dict[str, Any] = {
        "success": True,
        "source_concept_id": source_id,
        "deleted": deleted,
        "inserted": inserted,
        "duration_ms": round(duration_ms, 3),
    }
    if not concept_doc:
        result["source_missing"] = True
    return result


def _sync_relationship_extent_index_for_concept_ids_now(
    source_ids: Sequence[str],
) -> dict[str, Any]:
    """Replace derived rows for a bounded set of source concepts in one batch."""

    normalised_ids = sorted(
        {
            source_id.strip()
            for source_id in source_ids
            if isinstance(source_id, str) and source_id.strip()
        }
    )
    if not normalised_ids:
        return {
            "success": True,
            "source_count": 0,
            "deleted": 0,
            "inserted": 0,
        }

    coll = get_relationship_extent_index_collection()
    if coll is None:
        return {"success": False, "reason": "collection_unavailable"}

    started = time.perf_counter()
    try:
        with timeout(RELATIONSHIP_EXTENT_SYNC_TIMEOUT_SECONDS):
            with bypass_access_control():
                concept_docs = list(
                    ConceptsRepository.find(
                        {"concept_id": {"$in": normalised_ids}},
                        {"concept_id": 1, "relationships": 1, "updated_at": 1},
                    )
                )

            docs_by_source_id: dict[str, Mapping[str, Any]] = {}
            for concept_doc in concept_docs:
                source_id = concept_doc.get("concept_id")
                if not isinstance(source_id, str) or not source_id.strip():
                    continue
                docs_by_source_id[source_id.strip()] = concept_doc

            docs_by_source: dict[str, list[dict[str, Any]]] = {
                source_id: (
                    _index_docs_for_concept(docs_by_source_id[source_id])
                    if source_id in docs_by_source_id
                    else []
                )
                for source_id in normalised_ids
            }
            index_docs = [
                doc
                for source_id in normalised_ids
                for doc in docs_by_source[source_id]
            ]
            _upsert_relationship_extent_rows(collection=coll, docs=index_docs)

            stale_delete_operations = []
            for source_id in normalised_ids:
                relation_ids = [
                    str(doc["relation_id"])
                    for doc in docs_by_source[source_id]
                    if doc.get("relation_id")
                ]
                stale_filter: dict[str, Any] = {"source_concept_id": source_id}
                if relation_ids:
                    stale_filter["relation_id"] = {"$nin": relation_ids}
                stale_delete_operations.append(DeleteMany(stale_filter))
            delete_result = coll.bulk_write(
                stale_delete_operations,
                ordered=False,
            )
            deleted = delete_result.deleted_count
            inserted = len(index_docs)
    except Exception as exc:
        _mark_relationship_extent_index_degraded(
            reason="batch_refresh_failed",
            source_count=len(normalised_ids),
            error_type=type(exc).__name__,
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000.0
    if duration_ms >= RELATIONSHIP_EXTENT_SYNC_SLOW_MS:
        logger.warning(
            "[relationship_extent_index] batch_sync_slow duration_ms=%.1f "
            "source_count=%s deleted=%s inserted=%s",
            duration_ms,
            len(normalised_ids),
            deleted,
            inserted,
        )
    return {
        "success": True,
        "source_count": len(normalised_ids),
        "source_missing_count": len(normalised_ids) - len(docs_by_source_id),
        "deleted": deleted,
        "inserted": inserted,
        "duration_ms": round(duration_ms, 3),
    }


def sync_relationship_extent_index_for_concept_id(source_id: str) -> dict[str, Any]:
    """Refresh derived extent rows, or coalesce the refresh inside a bulk scope."""

    if not isinstance(source_id, str) or not source_id.strip():
        return {"success": False, "reason": "missing_source_concept_id"}
    source_id = source_id.strip()
    if _DEFERRED_SYNC_DEPTH.get() > 0:
        pending = _DEFERRED_SYNC_SOURCE_IDS.get()
        if pending is not None:
            pending.add(source_id)
        return {
            "success": True,
            "source_concept_id": source_id,
            "deferred": True,
        }
    return _sync_relationship_extent_index_for_concept_id_now(source_id)


@contextmanager
def defer_relationship_extent_index_sync() -> Iterator[None]:
    """Coalesce repeated best-effort extent refreshes within a bulk mutation."""

    depth = _DEFERRED_SYNC_DEPTH.get()
    pending = _DEFERRED_SYNC_SOURCE_IDS.get()
    pending_token = None
    if depth == 0 or pending is None:
        pending = set()
        pending_token = _DEFERRED_SYNC_SOURCE_IDS.set(pending)
    depth_token = _DEFERRED_SYNC_DEPTH.set(depth + 1)
    try:
        yield
    finally:
        _DEFERRED_SYNC_DEPTH.reset(depth_token)
        if depth == 0:
            source_ids = sorted(pending or ())
            if pending_token is not None:
                _DEFERRED_SYNC_SOURCE_IDS.reset(pending_token)
            started = time.perf_counter()
            failures = 0
            try:
                result = _sync_relationship_extent_index_for_concept_ids_now(
                    source_ids
                )
                if result.get("success") is not True:
                    failures = len(source_ids)
                    logger.warning(
                        "[relationship_extent_index] deferred_sync_failed "
                        "source_count=%s reason=%s",
                        len(source_ids),
                        result.get("reason") or "unknown",
                    )
            except Exception:
                failures = len(source_ids)
                logger.warning(
                    "[relationship_extent_index] deferred_sync_failed "
                    "source_count=%s",
                    len(source_ids),
                    exc_info=True,
                )
            logger.info(
                "[relationship_extent_index] deferred_sync_complete "
                "source_count=%s failure_count=%s duration_ms=%.1f",
                len(source_ids),
                failures,
                (time.perf_counter() - started) * 1000.0,
            )


def _record_rebuild_state(payload: Mapping[str, Any]) -> None:
    settings = get_application_settings_collection()
    if settings is None:
        return
    settings.update_one(
        {"setting_name": RELATIONSHIP_EXTENT_INDEX_STATE_SETTING},
        {
            "$set": {
                "setting_name": RELATIONSHIP_EXTENT_INDEX_STATE_SETTING,
                "value": dict(payload),
                "updated_at": _utc_now(),
            }
        },
        upsert=True,
    )


def relationship_extent_index_ready() -> bool:
    now_monotonic = time.monotonic()
    cached_ready = _READINESS_CACHE.get("ready")
    cached_at = float(_READINESS_CACHE.get("checked_at") or 0.0)
    if (
        isinstance(cached_ready, bool)
        and now_monotonic - cached_at < _READINESS_CACHE_TTL_SECONDS
    ):
        return cached_ready

    ready = False
    try:
        with timeout(RELATIONSHIP_EXTENT_READINESS_TIMEOUT_SECONDS):
            settings = get_application_settings_collection()
            if settings is not None:
                state = settings.find_one(
                    {"setting_name": RELATIONSHIP_EXTENT_INDEX_STATE_SETTING},
                    {"value": 1},
                )
                value = state.get("value") if isinstance(state, Mapping) else None
                if isinstance(value, Mapping):
                    state_is_current = (
                        value.get("schema_version")
                        == RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION
                    )
                    if value.get("status") == "ready" and state_is_current:
                        ready = True
    except Exception as exc:
        logger.warning(
            "[relationship_extent_index] readiness_query_failed error_type=%s",
            type(exc).__name__,
        )
        ready = False

    _READINESS_CACHE["ready"] = ready
    _READINESS_CACHE["checked_at"] = now_monotonic
    return ready


def rebuild_relationship_extent_index(
    *,
    batch_size: int = 500,
    reason: str = "manual",
) -> dict[str, Any]:
    """Rebuild the derived support index from canonical concept documents."""

    coll = get_relationship_extent_index_collection()
    if coll is None:
        return {"success": False, "status": "unavailable"}

    started_at = _utc_now()
    _READINESS_CACHE["ready"] = False
    _READINESS_CACHE["checked_at"] = time.monotonic()
    _record_rebuild_state(
        {
            "status": "rebuilding",
            "schema_version": RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
            "reason": reason,
            "started_at": started_at.isoformat(),
        }
    )

    total_sources = 0
    total_edges = 0
    def _flush_pending_docs(pending_docs: list[dict[str, Any]]) -> None:
        if not pending_docs:
            return
        docs_by_relation_id: dict[str, dict[str, Any]] = {}
        for doc in pending_docs:
            relation_id = doc.get("relation_id")
            if not isinstance(relation_id, str) or not relation_id:
                continue
            docs_by_relation_id[relation_id] = doc
        if not docs_by_relation_id:
            return
        operations = [
            ReplaceOne({"relation_id": relation_id}, doc, upsert=True)
            for relation_id, doc in docs_by_relation_id.items()
        ]
        coll.bulk_write(operations, ordered=False)

    try:
        coll.delete_many({})
        with bypass_access_control():
            cursor = ConceptsRepository.find(
                {"relationships": {"$type": "object"}},
                {"concept_id": 1, "relationships": 1, "updated_at": 1},
            )
            pending_docs: list[dict[str, Any]] = []
            seen_source_ids: set[str] = set()
            for concept_doc in cursor:
                source_id = concept_doc.get("concept_id")
                if not isinstance(source_id, str) or not source_id.strip():
                    continue
                source_id = source_id.strip()
                if source_id in seen_source_ids:
                    logger.warning(
                        "[relationship_extent_index] skipping duplicate source concept_id during rebuild: %s",
                        source_id,
                    )
                    continue
                seen_source_ids.add(source_id)
                total_sources += 1
                docs = _index_docs_for_concept(concept_doc)
                total_edges += len(docs)
                pending_docs.extend(docs)
                if batch_size > 0 and len(pending_docs) >= batch_size:
                    _flush_pending_docs(pending_docs)
                    pending_docs = []
                if batch_size > 0 and total_sources % batch_size == 0:
                    logger.info(
                        "[relationship_extent_index] rebuilt %s source concepts",
                        total_sources,
                    )
            if pending_docs:
                _flush_pending_docs(pending_docs)

        finished_at = _utc_now()
        state = {
            "status": "ready",
            "schema_version": RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
            "reason": reason,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "source_count": total_sources,
            "edge_count": total_edges,
        }
        _record_rebuild_state(state)
        _READINESS_CACHE["ready"] = True
        _READINESS_CACHE["checked_at"] = time.monotonic()
        return {"success": True, **state}
    except Exception as exc:
        logger.error(
            "Failed rebuilding relationship extent index: %s", exc, exc_info=True
        )
        _READINESS_CACHE["ready"] = False
        _READINESS_CACHE["checked_at"] = time.monotonic()
        _record_rebuild_state(
            {
                "status": "failed",
                "schema_version": RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
                "reason": reason,
                "started_at": started_at.isoformat(),
                "failed_at": _utc_now().isoformat(),
                "error_type": type(exc).__name__,
            }
        )
        return {"success": False, "status": "failed", "error_type": type(exc).__name__}


def _build_relationship_extent_index_query(
    *,
    predicate_id: str | None = None,
    target_value: str | None = None,
    target_values: Iterable[str] | None = None,
    source_concept_id: str | None = None,
    exclude_structural_predicates: bool = False,
) -> tuple[dict[str, Any], bool]:
    query: dict[str, Any] = {
        "schema_version": RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
    }
    if predicate_id:
        query["predicate_id"] = predicate_id
    if target_value:
        query["target_value"] = target_value
    elif target_values is not None:
        values = [
            item for item in target_values if isinstance(item, str) and item.strip()
        ]
        if not values:
            return {}, False
        query["target_value"] = {"$in": values}
    if source_concept_id:
        query["source_concept_id"] = source_concept_id
    if exclude_structural_predicates:
        if predicate_id and predicate_id in get_relationship_kinds_set():
            return {}, False
        predicate_filter: dict[str, Any] = {"$nin": list(get_relationship_kinds_set())}
        if predicate_id:
            predicate_filter["$eq"] = predicate_id
        query["predicate_id"] = predicate_filter
    return query, True


def query_relationship_extent_index(
    *,
    predicate_id: str | None = None,
    target_value: str | None = None,
    target_values: Iterable[str] | None = None,
    source_concept_id: str | None = None,
    exclude_structural_predicates: bool = False,
    offset: int = 0,
    limit: int = 0,
    sort: list[tuple[str, int]] | None = None,
    count_total: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """Query derived structured relationship rows.

    Returns ``([], -1)`` when the support index is unavailable/not ready so
    callers can choose a correctness-preserving fallback.
    """

    if not relationship_extent_index_ready():
        return [], -1
    coll = get_relationship_extent_index_collection()
    if coll is None:
        return [], -1

    query, should_query = _build_relationship_extent_index_query(
        predicate_id=predicate_id,
        target_value=target_value,
        target_values=target_values,
        source_concept_id=source_concept_id,
        exclude_structural_predicates=exclude_structural_predicates,
    )
    if not should_query:
        return [], 0

    try:
        with timeout(RELATIONSHIP_EXTENT_READ_TIMEOUT_SECONDS):
            cursor = coll.find(query)
            if sort:
                cursor = cursor.sort(sort)
            if offset:
                cursor = cursor.skip(max(0, int(offset)))
            if limit:
                cursor = cursor.limit(max(0, int(limit)))
            docs = list(cursor)
            total = coll.count_documents(query) if count_total else len(docs)
            return docs, total
    except Exception as exc:
        logger.warning(
            "[relationship_extent_index] extent_query_failed error_type=%s",
            type(exc).__name__,
        )
        return [], -1


def _incoming_row_from_index_doc(
    item: Mapping[str, Any],
    *,
    target_concept_id: str,
) -> dict[str, Any] | None:
    source_concept_id = item.get("source_concept_id")
    predicate_id = item.get("predicate_id")
    target_value = item.get("target_value")
    if not isinstance(source_concept_id, str) or not source_concept_id.strip():
        return None
    source_concept_id = source_concept_id.strip()
    if not isinstance(predicate_id, str) or not predicate_id.strip():
        return None
    predicate_id = predicate_id.strip()
    if target_value != target_concept_id:
        return None
    if source_concept_id == target_concept_id:
        return None

    updated_at = item.get("updated_at")
    updated_at_value = (
        updated_at.isoformat() if hasattr(updated_at, "isoformat") else None
    )
    target_index = int(item.get("target_index") or 0)
    return {
        "relation_id": (
            f"struct::{source_concept_id}::{predicate_id}::incoming::{target_index}"
        ),
        "source": "structured",
        "relation_kind": "binary",
        "role": "arg2",
        "predicate_id": predicate_id,
        "arg1_value": source_concept_id,
        "arg1_is_concept": source_concept_id.startswith("#V#"),
        "arg2_value": target_value,
        "arg2_is_concept": True,
        "arg2_index": int(item.get("arg2_index") or target_index + 2),
        "source_concept_id": source_concept_id,
        "target_value": target_value,
        "updated_at": updated_at_value,
        "is_asserted": True,
        "relation_state": "asserted",
    }


def _incoming_rows_from_index_docs(
    docs: Sequence[Mapping[str, Any]],
    *,
    target_concept_id: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    source_ids: list[str] = []
    row_candidates: list[tuple[Mapping[str, Any], str]] = []
    for item in docs:
        source_concept_id = item.get("source_concept_id")
        target_value = item.get("target_value")
        if (
            isinstance(source_concept_id, str)
            and source_concept_id.strip()
            and target_value == target_concept_id
            and source_concept_id.strip() != target_concept_id
        ):
            clean_source_id = source_concept_id.strip()
            source_ids.append(clean_source_id)
            row_candidates.append((item, clean_source_id))

    accessible_source_ids = filter_accessible_concept_ids(source_ids)
    rows: list[dict[str, Any]] = []
    filtered_by_access = 0
    for item, source_concept_id in row_candidates:
        if source_concept_id not in accessible_source_ids:
            filtered_by_access += 1
            continue
        row = _incoming_row_from_index_doc(
            item,
            target_concept_id=target_concept_id,
        )
        if row is not None:
            rows.append(row)

    return rows, {
        "source_concepts_seen": len(set(source_ids)),
        "rows_filtered_by_access": filtered_by_access,
    }


def incoming_dynamic_extent_rows_page_for_target(
    target_concept_id: str,
    *,
    requested_predicate: str | None = None,
    exclude_structural_predicates: bool = True,
    visible_offset: int = 0,
    visible_limit: int = 50,
    batch_size: int | None = None,
    max_index_rows_scanned: int | None = None,
    time_budget_ms: int | None = None,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    """Return a bounded visible incoming extent page from the derived index."""

    clean_offset = max(0, int(visible_offset or 0))
    clean_limit = max(1, int(visible_limit or 1))
    clean_batch_size = max(
        1,
        int(batch_size or RELATIONSHIP_EXTENT_PAGE_BATCH_SIZE or 1),
    )
    clean_max_scan = max(
        clean_batch_size,
        int(max_index_rows_scanned or RELATIONSHIP_EXTENT_PAGE_MAX_INDEX_SCAN or 1),
    )
    clean_time_budget_ms = max(
        1,
        int(time_budget_ms or RELATIONSHIP_EXTENT_PAGE_TIME_BUDGET_MS or 1),
    )
    wanted_visible = clean_offset + clean_limit + 1
    rows: list[dict[str, Any]] = []
    scanned = 0
    access_checked: set[str] = set()
    filtered_by_access = 0
    batches = 0
    source_exhausted = False
    stop_reason: str | None = None
    started_at = time.monotonic()

    if not relationship_extent_index_ready():
        return (
            [],
            False,
            {
                "used_extent_index": False,
                "complete": False,
                "bounded": False,
                "reason": "extent_index_unavailable",
            },
        )
    coll = get_relationship_extent_index_collection()
    if coll is None:
        return (
            [],
            False,
            {
                "used_extent_index": False,
                "complete": False,
                "bounded": False,
                "reason": "extent_index_unavailable",
            },
        )
    query, should_query = _build_relationship_extent_index_query(
        predicate_id=requested_predicate,
        target_value=target_concept_id,
        exclude_structural_predicates=exclude_structural_predicates,
    )
    if not should_query:
        return (
            [],
            True,
            {
                "used_extent_index": True,
                "complete": True,
                "bounded": False,
                "has_more": False,
                "index_rows_scanned": 0,
                "index_batches": 0,
                "source_concepts_access_checked": 0,
                "rows_filtered_by_access": 0,
                "visible_rows_collected": 0,
                "rows_returned": 0,
                "visible_offset": clean_offset,
                "visible_limit": clean_limit,
                "batch_size": clean_batch_size,
                "max_index_rows_scanned": clean_max_scan,
                "time_budget_ms": clean_time_budget_ms,
                "elapsed_ms": 0,
            },
        )

    cursor = (
        coll.find(query)
        .sort([("predicate_id", 1), ("source_concept_id", 1), ("target_index", 1)])
        .limit(clean_max_scan)
        .batch_size(clean_batch_size)
    )
    pending_docs: list[Mapping[str, Any]] = []

    def _process_pending_batch() -> None:
        nonlocal batches, filtered_by_access
        if not pending_docs:
            return
        batches += 1
        batch_rows, batch_stats = _incoming_rows_from_index_docs(
            pending_docs,
            target_concept_id=target_concept_id,
        )
        rows.extend(batch_rows)
        access_checked.update(
            str(item.get("source_concept_id")).strip()
            for item in pending_docs
            if isinstance(item.get("source_concept_id"), str)
            and str(item.get("source_concept_id")).strip()
        )
        filtered_by_access += int(batch_stats.get("rows_filtered_by_access") or 0)
        pending_docs.clear()

    try:
        elapsed_before_cursor_ms = int((time.monotonic() - started_at) * 1000)
        remaining_budget_seconds = max(
            0.001,
            (clean_time_budget_ms - elapsed_before_cursor_ms) / 1000.0,
        )
        cursor_timeout_seconds = min(
            RELATIONSHIP_EXTENT_READ_TIMEOUT_SECONDS,
            remaining_budget_seconds,
        )
        with timeout(cursor_timeout_seconds):
            for item in cursor:
                pending_docs.append(item)
                scanned += 1
                if len(pending_docs) < clean_batch_size and scanned < clean_max_scan:
                    continue
                _process_pending_batch()
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                if len(rows) >= wanted_visible:
                    stop_reason = "visible_page_filled"
                    break
                if scanned >= clean_max_scan:
                    stop_reason = "scan_cap_exhausted"
                    break
                if elapsed_ms >= clean_time_budget_ms:
                    stop_reason = "time_budget_exhausted"
                    break
            else:
                source_exhausted = True
            if pending_docs and len(rows) < wanted_visible:
                _process_pending_batch()
    except Exception as exc:
        logger.warning(
            "[relationship_extent_index] extent_page_query_failed error_type=%s",
            type(exc).__name__,
        )
        return (
            [],
            False,
            {
                "used_extent_index": False,
                "complete": False,
                "bounded": False,
                "reason": "extent_index_query_failed",
            },
        )

    has_more_visible = len(rows) > clean_offset + clean_limit
    complete = source_exhausted
    bounded = not complete
    page_rows = rows[clean_offset : clean_offset + clean_limit]
    diagnostics = {
        "used_extent_index": True,
        "complete": complete,
        "bounded": bounded,
        "has_more": has_more_visible or bounded,
        "index_rows_scanned": scanned,
        "index_batches": batches,
        "source_concepts_access_checked": len(access_checked),
        "rows_filtered_by_access": filtered_by_access,
        "visible_rows_collected": len(rows),
        "rows_returned": len(page_rows),
        "visible_offset": clean_offset,
        "visible_limit": clean_limit,
        "batch_size": clean_batch_size,
        "max_index_rows_scanned": clean_max_scan,
        "time_budget_ms": clean_time_budget_ms,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        "stop_reason": stop_reason or ("source_exhausted" if complete else None),
    }
    return page_rows, True, diagnostics


def incoming_dynamic_extent_rows_for_target(
    target_concept_id: str,
    *,
    requested_predicate: str | None = None,
    exclude_structural_predicates: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Return concept-view incoming structured rows from the derived index.

    The boolean is ``True`` when the index was used, ``False`` when callers
    should fall back to the legacy scan to preserve correctness.
    """

    docs, total = query_relationship_extent_index(
        predicate_id=requested_predicate,
        target_value=target_concept_id,
        exclude_structural_predicates=exclude_structural_predicates,
        sort=[("predicate_id", 1), ("source_concept_id", 1), ("target_index", 1)],
        count_total=False,
    )
    if total < 0:
        return [], False

    try:
        rows, _stats = _incoming_rows_from_index_docs(
            docs,
            target_concept_id=target_concept_id,
        )
    except Exception:
        rows = []
        for item in docs:
            source_concept_id = item.get("source_concept_id")
            if not isinstance(source_concept_id, str) or not source_concept_id.strip():
                continue
            try:
                if not can_access_concept(source_concept_id):
                    continue
            except Exception:
                continue
            row = _incoming_row_from_index_doc(
                item,
                target_concept_id=target_concept_id,
            )
            if row is not None:
                rows.append(row)
    return rows, True
