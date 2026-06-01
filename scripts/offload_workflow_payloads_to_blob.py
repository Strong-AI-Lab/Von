#!/usr/bin/env python
"""Offload oversized durable workflow payloads from MongoDB to the blob store.

Dry-run is the default. Apply mode uploads oversized workflow leaves to the
configured Von blob store, reloads and verifies every new blob reference, then
updates only the changed Mongo fields.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from bson import ObjectId
from pymongo.errors import PyMongoError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
except Exception:
    pass

from src.backend.db.connection_manager import get_db  # noqa: E402
from src.backend.services.workflow_payload_store import (  # noqa: E402
    WorkflowPayloadOffloadResult,
    compact_workflow_payload_for_storage,
    count_workflow_payload_offload_candidates,
    default_workflow_payload_threshold_bytes,
    estimate_workflow_payload_size_bytes,
    hydrate_workflow_payload_blob_refs,
    workflow_payload_sha256,
)


WORKFLOW_INSTANCES_COLLECTION = "workflow_instances"
WORKFLOW_EXECUTIONS_COLLECTION = "workflow_executions"
SUPPORTED_COLLECTIONS = (WORKFLOW_INSTANCES_COLLECTION, WORKFLOW_EXECUTIONS_COLLECTION)

INSTANCE_PAYLOAD_FIELDS = ("inputs", "workflow_data", "outputs")
EXECUTION_PAYLOAD_FIELDS = ("metadata", "state_transitions", "actions", "steps")


@dataclass
class OffloadStats:
    collection: str
    scanned: int = 0
    candidates: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    inline_bytes_before: int = 0
    inline_bytes_after: int = 0
    offloaded_payloads: int = 0
    degraded_payloads: int = 0
    verified_payloads: int = 0
    set_operations: int = 0
    last_scanned_id: str | None = None
    marked_skipped: int = 0


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _coerce_id_bound(value: Any) -> Any | None:
    cleaned = _clean_str(value)
    if not cleaned:
        return None
    if ObjectId.is_valid(cleaned):
        return ObjectId(cleaned)
    return cleaned


def _iter_collection_docs(
    *,
    coll: Any,
    query: Mapping[str, Any],
    projection: Mapping[str, Any],
    limit: int | None,
    skip_scanned: int = 0,
    batch_size: int = 10,
    page_by_id: bool = False,
    page_size: int = 1,
    after_id: Any | None = None,
):
    if not page_by_id:
        cursor = coll.find(dict(query), dict(projection))
        if batch_size > 0:
            cursor = cursor.batch_size(batch_size)
        if skip_scanned > 0:
            cursor = cursor.skip(skip_scanned)
        if limit:
            cursor = cursor.limit(limit)
        yield from cursor
        return

    remaining = limit if limit and limit > 0 else None
    skip_remaining = max(0, int(skip_scanned))
    last_id = after_id
    effective_page_size = max(1, int(page_size))
    while remaining is None or remaining > 0:
        page_query = dict(query)
        if last_id is not None:
            page_query["_id"] = {"$gt": last_id}
        fetch_count = effective_page_size
        if remaining is not None:
            fetch_count = min(fetch_count, remaining + skip_remaining)
        if fetch_count <= 0:
            return

        cursor = coll.find(page_query, dict(projection)).sort("_id", 1).limit(fetch_count)
        page_docs = list(cursor)
        if not page_docs:
            return

        for doc in page_docs:
            last_id = doc.get("_id", last_id)
            if skip_remaining > 0:
                skip_remaining -= 1
                continue
            yield doc
            if remaining is not None:
                remaining -= 1
                if remaining <= 0:
                    return


def _iter_docs_with_interruption_reporting(
    *,
    collection_name: str,
    stats: OffloadStats,
    coll: Any,
    query: Mapping[str, Any],
    projection: Mapping[str, Any],
    limit: int | None,
    skip_scanned: int = 0,
    batch_size: int = 10,
    page_by_id: bool = False,
    page_size: int = 1,
    after_id: Any | None = None,
):
    try:
        yield from _iter_collection_docs(
            coll=coll,
            query=query,
            projection=projection,
            limit=limit,
            skip_scanned=skip_scanned,
            batch_size=batch_size,
            page_by_id=page_by_id,
            page_size=page_size,
            after_id=after_id,
        )
    except PyMongoError as exc:
        stats.errors += 1
        print(
            f"[offload] ERROR {collection_name} scan interrupted "
            f"after scanned={stats.scanned} last_scanned_id={stats.last_scanned_id}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def _is_safe_mongo_path_component(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value != ""
        and "." not in value
        and not value.startswith("$")
    )


def _mongo_path(parts: tuple[str, ...]) -> str:
    if not parts or not all(_is_safe_mongo_path_component(part) for part in parts):
        raise ValueError(f"Unsafe Mongo update path: {parts!r}")
    return ".".join(parts)


def _diff_set_operations(
    original: Any,
    compacted: Any,
    *,
    prefix: tuple[str, ...],
) -> dict[str, Any]:
    if workflow_payload_sha256(original) == workflow_payload_sha256(compacted):
        return {}

    if isinstance(original, Mapping) and isinstance(compacted, Mapping):
        updates: dict[str, Any] = {}
        keys = sorted(
            {str(key) for key in original.keys()}
            | {str(key) for key in compacted.keys()}
        )
        for key in keys:
            if key not in compacted:
                continue
            if key not in original:
                updates[_mongo_path((*prefix, key))] = compacted[key]
                continue
            if not _is_safe_mongo_path_component(key):
                updates[_mongo_path(prefix)] = compacted
                return updates
            updates.update(
                _diff_set_operations(
                    original[key],
                    compacted[key],
                    prefix=(*prefix, key),
                )
            )
        return updates

    return {_mongo_path(prefix): compacted}


def _verify_compaction_round_trip(
    *,
    original: Any,
    compacted: Any,
    field_label: str,
) -> None:
    hydrated = hydrate_workflow_payload_blob_refs(compacted, fail_soft=False)
    if workflow_payload_sha256(hydrated.payload) != workflow_payload_sha256(original):
        raise RuntimeError(f"Blob round-trip verification failed for {field_label}")


def _compact_verified(
    *,
    value: Any,
    record_family: str,
    record_id: str | None,
    namespace: str | None,
    workflow_id: str | None,
    threshold_bytes: int,
    field_label: str,
) -> WorkflowPayloadOffloadResult | None:
    compacted = compact_workflow_payload_for_storage(
        value,
        record_family=record_family,
        record_id=record_id,
        namespace=namespace,
        workflow_id=workflow_id,
        threshold_bytes=threshold_bytes,
        fail_soft=False,
    )
    if compacted.offloaded_count <= 0:
        return None
    _verify_compaction_round_trip(
        original=value,
        compacted=compacted.payload,
        field_label=field_label,
    )
    return compacted


def _apply_update(coll: Any, query: Mapping[str, Any], set_fields: Mapping[str, Any]) -> bool:
    if not set_fields:
        return False
    result = coll.update_one(dict(query), {"$set": dict(set_fields)})
    matched = getattr(result, "matched_count", None)
    if isinstance(matched, int) and matched <= 0:
        raise RuntimeError(f"Mongo update matched no documents for query={dict(query)!r}")
    return True


def _payload_subset(doc: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: doc[field] for field in fields if field in doc and doc[field] is not None}


def _candidate_count_for_subset(
    payloads: Mapping[str, Any],
    *,
    threshold_bytes: int,
) -> int:
    return count_workflow_payload_offload_candidates(
        payloads,
        threshold_bytes=threshold_bytes,
    )


def _build_instance_query(
    *,
    namespace: str | None,
    workflow_id: str | None,
    instance_id: str | None,
    status: str | None,
    unmigrated_only: bool = False,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if namespace:
        query["namespace"] = namespace
    if workflow_id:
        query["workflow_id"] = workflow_id
    if instance_id:
        query["instance_id"] = instance_id
    if status:
        query["status"] = status
    if unmigrated_only:
        query["workflow_payload_offload_migration"] = {"$exists": False}
    return query


def _build_execution_query(
    *,
    namespace: str | None,
    workflow_id: str | None,
    instance_id: str | None,
    execution_id: str | None,
    status: str | None,
    unmigrated_only: bool = False,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if namespace:
        query["user_namespace"] = namespace
    if workflow_id:
        query["workflow_id"] = workflow_id
    if instance_id:
        query["instance_id"] = instance_id
    if execution_id:
        query["execution_id"] = execution_id
    if status:
        query["status"] = status
    if unmigrated_only:
        query["workflow_payload_offload_migration"] = {"$exists": False}
    return query


def _skipped_marker() -> dict[str, Any]:
    return {
        "schema_version": "workflow_payload_offload_migration.v1",
        "status": "skipped_no_inline_candidates",
        "updated_at_utc": _utcnow_iso(),
        "strategy": "field_scan_found_no_payload_above_threshold",
    }


def _scan_workflow_instances(
    *,
    coll: Any,
    apply: bool,
    limit: int | None,
    skip_scanned: int = 0,
    batch_size: int = 10,
    page_by_id: bool = False,
    page_size: int = 1,
    after_id: Any | None = None,
    threshold_bytes: int,
    progress_every: int = 25,
    namespace: str | None = None,
    workflow_id: str | None = None,
    instance_id: str | None = None,
    status: str | None = None,
    unmigrated_only: bool = False,
    mark_inspected_skips: bool = False,
) -> OffloadStats:
    stats = OffloadStats(collection=WORKFLOW_INSTANCES_COLLECTION)
    query = _build_instance_query(
        namespace=namespace,
        workflow_id=workflow_id,
        instance_id=instance_id,
        status=status,
        unmigrated_only=unmigrated_only,
    )
    projection = {
        "instance_id": 1,
        "workflow_id": 1,
        "namespace": 1,
        "status": 1,
        **{field: 1 for field in INSTANCE_PAYLOAD_FIELDS},
    }
    for doc in _iter_docs_with_interruption_reporting(
        collection_name=WORKFLOW_INSTANCES_COLLECTION,
        stats=stats,
        coll=coll,
        query=query,
        projection=projection,
        limit=limit,
        skip_scanned=skip_scanned,
        batch_size=batch_size,
        page_by_id=page_by_id,
        page_size=page_size,
        after_id=after_id,
    ):
        stats.scanned += 1
        stats.last_scanned_id = str(doc.get("_id")) if doc.get("_id") is not None else None
        payloads = _payload_subset(doc, INSTANCE_PAYLOAD_FIELDS)
        before = estimate_workflow_payload_size_bytes(payloads)
        candidate_count = _candidate_count_for_subset(
            payloads,
            threshold_bytes=threshold_bytes,
        )
        if candidate_count <= 0:
            if apply and mark_inspected_skips:
                update_query: dict[str, Any] = {"_id": doc["_id"]}
                record_id = _clean_str(doc.get("instance_id"))
                if record_id:
                    update_query["instance_id"] = record_id
                _apply_update(
                    coll,
                    update_query,
                    {"workflow_payload_offload_migration": _skipped_marker()},
                )
                stats.marked_skipped += 1
                stats.set_operations += 1
            stats.skipped += 1
            continue

        stats.candidates += 1
        stats.inline_bytes_before += before
        if not apply:
            continue

        set_fields: dict[str, Any] = {}
        compacted_payloads = dict(payloads)
        doc_offloaded = 0
        doc_verified = 0
        try:
            record_id = _clean_str(doc.get("instance_id"))
            doc_namespace = _clean_str(doc.get("namespace"))
            doc_workflow_id = _clean_str(doc.get("workflow_id"))
            for field, value in payloads.items():
                compacted = _compact_verified(
                    value={field: value},
                    record_family=f"{WORKFLOW_INSTANCES_COLLECTION}.{field}",
                    record_id=record_id,
                    namespace=doc_namespace,
                    workflow_id=doc_workflow_id,
                    threshold_bytes=threshold_bytes,
                    field_label=f"workflow_instances.{record_id}.{field}",
                )
                if not compacted or not isinstance(compacted.payload, Mapping):
                    continue
                compacted_value = compacted.payload.get(field)
                set_fields.update(
                    _diff_set_operations(value, compacted_value, prefix=(field,))
                )
                compacted_payloads[field] = compacted_value
                doc_offloaded += compacted.offloaded_count
                doc_verified += compacted.offloaded_count

            if not set_fields:
                stats.skipped += 1
                continue
            set_fields["workflow_payload_offload_migration"] = {
                "schema_version": "workflow_payload_offload_migration.v1",
                "status": "applied",
                "updated_at_utc": _utcnow_iso(),
                "strategy": "field_patch_after_blob_round_trip_verification",
            }
            update_query: dict[str, Any] = {"_id": doc["_id"]}
            if record_id:
                update_query["instance_id"] = record_id
            _apply_update(coll, update_query, set_fields)
            stats.updated += 1
            stats.inline_bytes_after += estimate_workflow_payload_size_bytes(
                compacted_payloads
            )
            stats.offloaded_payloads += doc_offloaded
            stats.verified_payloads += doc_verified
            stats.set_operations += len(set_fields)
            if apply and progress_every > 0 and stats.updated % progress_every == 0:
                print(
                    "[offload] progress workflow_instances "
                    f"updated={stats.updated} offloaded={stats.offloaded_payloads} "
                    f"scanned={stats.scanned}",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as exc:
            stats.errors += 1
            print(
                f"[offload] ERROR workflow_instances instance_id={doc.get('instance_id')}: {exc}",
                file=sys.stderr,
            )

    return stats


def _scan_workflow_executions(
    *,
    coll: Any,
    apply: bool,
    limit: int | None,
    skip_scanned: int = 0,
    batch_size: int = 10,
    page_by_id: bool = False,
    page_size: int = 1,
    after_id: Any | None = None,
    threshold_bytes: int,
    progress_every: int = 25,
    namespace: str | None = None,
    workflow_id: str | None = None,
    instance_id: str | None = None,
    execution_id: str | None = None,
    status: str | None = None,
    unmigrated_only: bool = False,
    mark_inspected_skips: bool = False,
) -> OffloadStats:
    stats = OffloadStats(collection=WORKFLOW_EXECUTIONS_COLLECTION)
    query = _build_execution_query(
        namespace=namespace,
        workflow_id=workflow_id,
        instance_id=instance_id,
        execution_id=execution_id,
        status=status,
        unmigrated_only=unmigrated_only,
    )
    projection = {
        "execution_id": 1,
        "instance_id": 1,
        "workflow_id": 1,
        "user_namespace": 1,
        "status": 1,
        **{field: 1 for field in EXECUTION_PAYLOAD_FIELDS},
    }
    for doc in _iter_docs_with_interruption_reporting(
        collection_name=WORKFLOW_EXECUTIONS_COLLECTION,
        stats=stats,
        coll=coll,
        query=query,
        projection=projection,
        limit=limit,
        skip_scanned=skip_scanned,
        batch_size=batch_size,
        page_by_id=page_by_id,
        page_size=page_size,
        after_id=after_id,
    ):
        stats.scanned += 1
        stats.last_scanned_id = str(doc.get("_id")) if doc.get("_id") is not None else None
        payloads = _payload_subset(doc, EXECUTION_PAYLOAD_FIELDS)
        before = estimate_workflow_payload_size_bytes(payloads)
        candidate_count = _candidate_count_for_subset(
            payloads,
            threshold_bytes=threshold_bytes,
        )
        if candidate_count <= 0:
            if apply and mark_inspected_skips:
                _apply_update(
                    coll,
                    {"_id": doc["_id"]},
                    {"workflow_payload_offload_migration": _skipped_marker()},
                )
                stats.marked_skipped += 1
                stats.set_operations += 1
            stats.skipped += 1
            continue

        stats.candidates += 1
        stats.inline_bytes_before += before
        if not apply:
            continue

        set_fields: dict[str, Any] = {}
        compacted_payloads = dict(payloads)
        doc_offloaded = 0
        doc_verified = 0
        try:
            record_id = _clean_str(doc.get("execution_id"))
            doc_namespace = _clean_str(doc.get("user_namespace"))
            doc_workflow_id = _clean_str(doc.get("workflow_id"))
            for field, value in payloads.items():
                compacted = _compact_verified(
                    value={field: value},
                    record_family=f"{WORKFLOW_EXECUTIONS_COLLECTION}.{field}",
                    record_id=record_id,
                    namespace=doc_namespace,
                    workflow_id=doc_workflow_id,
                    threshold_bytes=threshold_bytes,
                    field_label=f"workflow_executions.{record_id}.{field}",
                )
                if not compacted or not isinstance(compacted.payload, Mapping):
                    continue
                compacted_value = compacted.payload.get(field)
                set_fields.update(
                    _diff_set_operations(value, compacted_value, prefix=(field,))
                )
                compacted_payloads[field] = compacted_value
                doc_offloaded += compacted.offloaded_count
                doc_verified += compacted.offloaded_count

            if not set_fields:
                stats.skipped += 1
                continue
            set_fields["workflow_payload_offload_migration"] = {
                "schema_version": "workflow_payload_offload_migration.v1",
                "status": "applied",
                "updated_at_utc": _utcnow_iso(),
                "strategy": "field_patch_after_blob_round_trip_verification",
            }
            _apply_update(coll, {"_id": doc["_id"]}, set_fields)
            stats.updated += 1
            stats.inline_bytes_after += estimate_workflow_payload_size_bytes(
                compacted_payloads
            )
            stats.offloaded_payloads += doc_offloaded
            stats.verified_payloads += doc_verified
            stats.set_operations += len(set_fields)
            if apply and progress_every > 0 and stats.updated % progress_every == 0:
                print(
                    "[offload] progress workflow_executions "
                    f"updated={stats.updated} offloaded={stats.offloaded_payloads} "
                    f"scanned={stats.scanned}",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as exc:
            stats.errors += 1
            print(
                f"[offload] ERROR workflow_executions execution_id={doc.get('execution_id')}: {exc}",
                file=sys.stderr,
            )

    return stats


def _parse_collections(values: list[str] | None) -> list[str]:
    if not values:
        return list(SUPPORTED_COLLECTIONS)
    selected: list[str] = []
    for raw in values:
        for part in str(raw).split(","):
            cleaned = part.strip()
            if not cleaned:
                continue
            if cleaned not in SUPPORTED_COLLECTIONS:
                raise SystemExit(
                    f"Unsupported collection {cleaned!r}. Expected one of: "
                    + ", ".join(SUPPORTED_COLLECTIONS)
                )
            if cleaned not in selected:
                selected.append(cleaned)
    return selected or list(SUPPORTED_COLLECTIONS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offload oversized durable workflow payloads from MongoDB to blob storage."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply verified field updates. Default is dry-run."
    )
    parser.add_argument(
        "--collection",
        action="append",
        help="Collection to scan. Repeat or comma-separate. Default scans both.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximum documents per collection.")
    parser.add_argument(
        "--skip-scanned",
        type=int,
        default=0,
        help="Skip this many matching documents before scanning. Useful for windowed backfills.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Mongo cursor batch size. Smaller batches avoid large-document getMore timeouts.",
    )
    parser.add_argument(
        "--page-by-id",
        action="store_true",
        help="Use repeated _id-ordered page queries instead of one long-lived cursor.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=1,
        help="Documents per _id page when --page-by-id is enabled.",
    )
    parser.add_argument(
        "--after-id",
        default=None,
        help="Resume a --page-by-id scan after this Mongo _id value.",
    )
    parser.add_argument(
        "--unmigrated-only",
        action="store_true",
        help="Scan only documents that do not already have a workflow offload migration marker.",
    )
    parser.add_argument(
        "--mark-inspected-skips",
        action="store_true",
        help="In apply mode, mark below-threshold inspected docs so unmigrated-only scans can advance.",
    )
    parser.add_argument("--namespace", default=None, help="Restrict to one namespace.")
    parser.add_argument("--workflow-id", default=None, help="Restrict to one workflow_id.")
    parser.add_argument("--instance-id", default=None, help="Restrict to one instance_id.")
    parser.add_argument("--execution-id", default=None, help="Restrict to one execution_id.")
    parser.add_argument("--status", default=None, help="Restrict to one workflow status.")
    parser.add_argument(
        "--threshold-bytes",
        type=int,
        default=default_workflow_payload_threshold_bytes(),
        help="Workflow payload offload threshold in bytes.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print apply progress after this many updated documents. Use 0 to disable.",
    )
    args = parser.parse_args(argv)

    db = get_db()
    if db is None:
        raise SystemExit("Could not connect to MongoDB")

    apply = bool(args.apply)
    selected_collections = _parse_collections(args.collection)
    namespace = _clean_str(args.namespace)
    workflow_id = _clean_str(args.workflow_id)
    instance_id = _clean_str(args.instance_id)
    execution_id = _clean_str(args.execution_id)
    status = _clean_str(args.status)
    after_id = _coerce_id_bound(args.after_id)

    print("[offload] mode=" + ("apply" if apply else "dry-run"))
    print("[offload] collections=" + ",".join(selected_collections))
    print(f"[offload] threshold workflow={args.threshold_bytes}")
    print(f"[offload] scan skip={max(0, int(args.skip_scanned))} batch_size={max(0, int(args.batch_size))}")
    if args.unmigrated_only:
        print(
            "[offload] scan unmigrated_only=true "
            f"mark_inspected_skips={bool(args.mark_inspected_skips and apply)}"
        )
    if args.page_by_id:
        print(
            "[offload] scan page_by_id=true "
            f"page_size={max(1, int(args.page_size))} after_id={args.after_id or ''}"
        )
    if any((namespace, workflow_id, instance_id, execution_id, status)):
        print(
            "[offload] filters "
            + json.dumps(
                {
                    "namespace": namespace,
                    "workflow_id": workflow_id,
                    "instance_id": instance_id,
                    "execution_id": execution_id,
                    "status": status,
                },
                sort_keys=True,
            )
        )

    results: list[OffloadStats] = []
    if WORKFLOW_INSTANCES_COLLECTION in selected_collections:
        results.append(
            _scan_workflow_instances(
                coll=db[WORKFLOW_INSTANCES_COLLECTION],
                apply=apply,
                limit=args.limit,
                skip_scanned=max(0, int(args.skip_scanned)),
                batch_size=max(0, int(args.batch_size)),
                page_by_id=bool(args.page_by_id),
                page_size=max(1, int(args.page_size)),
                after_id=after_id,
                threshold_bytes=args.threshold_bytes,
                progress_every=max(0, int(args.progress_every)),
                namespace=namespace,
                workflow_id=workflow_id,
                instance_id=instance_id,
                status=status,
                unmigrated_only=bool(args.unmigrated_only),
                mark_inspected_skips=bool(args.mark_inspected_skips and apply),
            )
        )
    if WORKFLOW_EXECUTIONS_COLLECTION in selected_collections:
        results.append(
            _scan_workflow_executions(
                coll=db[WORKFLOW_EXECUTIONS_COLLECTION],
                apply=apply,
                limit=args.limit,
                skip_scanned=max(0, int(args.skip_scanned)),
                batch_size=max(0, int(args.batch_size)),
                page_by_id=bool(args.page_by_id),
                page_size=max(1, int(args.page_size)),
                after_id=after_id,
                threshold_bytes=args.threshold_bytes,
                progress_every=max(0, int(args.progress_every)),
                namespace=namespace,
                workflow_id=workflow_id,
                instance_id=instance_id,
                execution_id=execution_id,
                status=status,
                unmigrated_only=bool(args.unmigrated_only),
                mark_inspected_skips=bool(args.mark_inspected_skips and apply),
            )
        )

    payload = {
        "schema_version": "workflow_payload_offload_report.v1",
        "results": [asdict(result) for result in results],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if all(result.errors == 0 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())