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
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

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

    coll = collection or get_relationship_extent_index_collection()
    if coll is None:
        return {"success": False, "reason": "collection_unavailable"}

    docs = _index_docs_for_concept(concept_doc)
    deleted = coll.delete_many({"source_concept_id": source_id}).deleted_count
    inserted = 0
    if docs:
        result = coll.insert_many(docs, ordered=False)
        inserted = len(result.inserted_ids)
    return {
        "success": True,
        "source_concept_id": source_id,
        "deleted": deleted,
        "inserted": inserted,
    }


def sync_relationship_extent_index_for_concept_id(source_id: str) -> dict[str, Any]:
    """Refresh derived extent-index rows for a source concept id."""

    if not isinstance(source_id, str) or not source_id.strip():
        return {"success": False, "reason": "missing_source_concept_id"}
    with bypass_access_control():
        concept_doc = ConceptsRepository.find_one(
            {"concept_id": source_id.strip()},
            {"concept_id": 1, "relationships": 1, "updated_at": 1},
        )
    if not concept_doc:
        coll = get_relationship_extent_index_collection()
        if coll is not None:
            deleted = coll.delete_many(
                {"source_concept_id": source_id.strip()}
            ).deleted_count
            return {
                "success": True,
                "source_concept_id": source_id.strip(),
                "deleted": deleted,
                "inserted": 0,
                "source_missing": True,
            }
        return {"success": False, "reason": "collection_unavailable"}
    return sync_relationship_extent_index_for_concept_doc(concept_doc)


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
    settings = get_application_settings_collection()
    if settings is not None:
        state = settings.find_one(
            {"setting_name": RELATIONSHIP_EXTENT_INDEX_STATE_SETTING},
            {"value": 1},
        )
        value = state.get("value") if isinstance(state, Mapping) else None
        if isinstance(value, Mapping) and value.get("status") == "ready":
            if value.get("schema_version") == RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION:
                ready = True

    if not ready:
        coll = get_relationship_extent_index_collection()
        if coll is not None:
            try:
                ready = coll.find_one({}, {"_id": 1}) is not None
            except Exception:
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
    try:
        coll.delete_many({})
        with bypass_access_control():
            cursor = ConceptsRepository.find(
                {"relationships": {"$type": "object"}},
                {"concept_id": 1, "relationships": 1, "updated_at": 1},
            )
            pending_docs: list[dict[str, Any]] = []
            for concept_doc in cursor:
                source_id = concept_doc.get("concept_id")
                if not isinstance(source_id, str) or not source_id.strip():
                    continue
                total_sources += 1
                docs = _index_docs_for_concept(concept_doc)
                total_edges += len(docs)
                pending_docs.extend(docs)
                if batch_size > 0 and len(pending_docs) >= batch_size:
                    coll.insert_many(pending_docs, ordered=False)
                    pending_docs = []
                if batch_size > 0 and total_sources % batch_size == 0:
                    logger.info(
                        "[relationship_extent_index] rebuilt %s source concepts",
                        total_sources,
                    )
            if pending_docs:
                coll.insert_many(pending_docs, ordered=False)

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
    except Exception:
        logger.exception(
            "Failed reading relationship extent index page for target=%s",
            target_concept_id,
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
