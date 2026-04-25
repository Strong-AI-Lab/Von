#!/usr/bin/env python
"""Offload oversized stored debug payloads from MongoDB to the blob store.

Dry-run is the default.  Apply mode is intentionally conservative:

1. serialise and upload only known/debug-heavy payload fields;
2. reload every new blob reference and compare its canonical SHA-256 with the
   original value;
3. update only the exact Mongo dotted fields that changed.

The migration never replaces historical payloads with degraded summaries.  If a
blob write or verification fails, the Mongo document is left untouched and the
error is reported.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


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
from src.backend.services.debug_payload_store import (  # noqa: E402
    DebugPayloadOffloadResult,
    compact_debug_payload_for_storage,
    debug_payload_sha256,
    default_debug_payload_threshold_bytes,
    default_tool_message_threshold_bytes,
    estimate_payload_size_bytes,
    hydrate_debug_payload_blob_refs,
)


CHAT_HISTORY_COLLECTION = "chat_history"
TURN_EXECUTION_RECORDS_COLLECTION = "turn_execution_records"
SUPPORTED_COLLECTIONS = (CHAT_HISTORY_COLLECTION, TURN_EXECUTION_RECORDS_COLLECTION)


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


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _request_id_from_debug(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    request_id = _clean_str(value.get("request_id"))
    if request_id:
        return request_id
    turn_record = value.get("turn_execution_record")
    if isinstance(turn_record, Mapping):
        return _clean_str(turn_record.get("request_id"))
    return None


def _history_entry_request_id(entry: Mapping[str, Any]) -> str | None:
    request_id = _request_id_from_debug(entry.get("llm_debug_data"))
    return request_id or _clean_str(entry.get("request_id"))


def _is_candidate(value: Any, *, threshold_bytes: int) -> bool:
    return estimate_payload_size_bytes(value) > threshold_bytes


def _history_query(
    *,
    namespace: str | None,
    user_id: str | None,
    session_id: str | None,
    request_id: str | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if namespace:
        query["namespace"] = namespace
    if user_id:
        query["user_id"] = user_id
    if session_id:
        query["session_id"] = session_id
    if request_id:
        query["history"] = {
            "$elemMatch": {
                "role": "assistant",
                "llm_debug_data.request_id": request_id,
            }
        }
    return query


def _turn_record_query(
    *,
    namespace: str | None,
    request_id: str | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if namespace:
        query["namespace"] = namespace
    if request_id:
        query["request_id"] = request_id
    return query


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
    """Return dotted Mongo `$set` fields needed to transform original to compacted."""

    if debug_payload_sha256(original) == debug_payload_sha256(compacted):
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
    hydrated = hydrate_debug_payload_blob_refs(compacted, fail_soft=False)
    if debug_payload_sha256(hydrated.payload) != debug_payload_sha256(original):
        raise RuntimeError(f"Blob round-trip verification failed for {field_label}")


def _compact_verified(
    *,
    value: Any,
    root_kind: str,
    namespace: str | None,
    request_id: str | None,
    threshold_bytes: int,
    field_label: str,
) -> DebugPayloadOffloadResult | None:
    compacted = compact_debug_payload_for_storage(
        value,
        root_kind=root_kind,
        namespace=namespace,
        request_id=request_id,
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


def _scan_chat_history(
    *,
    coll: Any,
    apply: bool,
    limit: int | None,
    threshold_bytes: int,
    tool_threshold_bytes: int,
    namespace: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
) -> OffloadStats:
    stats = OffloadStats(collection=CHAT_HISTORY_COLLECTION)
    cursor = coll.find(
        _history_query(
            namespace=namespace,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
        ),
        {"history": 1, "namespace": 1, "user_id": 1, "session_id": 1},
    )
    if limit:
        cursor = cursor.limit(limit)

    for doc in cursor:
        stats.scanned += 1
        history = doc.get("history")
        if not isinstance(history, list):
            stats.skipped += 1
            continue

        doc_namespace = _clean_str(doc.get("namespace")) or _clean_str(doc.get("user_id"))
        before = estimate_payload_size_bytes(history)
        set_fields: dict[str, Any] = {}
        doc_offloaded = 0
        doc_verified = 0

        try:
            for index, entry in enumerate(history):
                if not isinstance(entry, Mapping):
                    continue
                entry_request_id = _history_entry_request_id(entry)
                if request_id and entry_request_id != request_id:
                    continue

                llm_debug_data = entry.get("llm_debug_data")
                if _is_candidate(llm_debug_data, threshold_bytes=threshold_bytes):
                    if not apply:
                        doc_offloaded += 1
                    else:
                        compacted = _compact_verified(
                            value=llm_debug_data,
                            root_kind="chat_history.llm_debug_data",
                            namespace=doc_namespace,
                            request_id=entry_request_id,
                            threshold_bytes=threshold_bytes,
                            field_label=(
                                f"chat_history.{doc.get('_id')}.history."
                                f"{index}.llm_debug_data"
                            ),
                        )
                        if compacted:
                            set_fields.update(
                                _diff_set_operations(
                                    llm_debug_data,
                                    compacted.payload,
                                    prefix=("history", str(index), "llm_debug_data"),
                                )
                            )
                            doc_offloaded += compacted.offloaded_count
                            doc_verified += compacted.offloaded_count

                content = entry.get("content")
                if entry.get("role") == "tool" and _is_candidate(
                    content, threshold_bytes=tool_threshold_bytes
                ):
                    if not apply:
                        doc_offloaded += 1
                    else:
                        compacted = _compact_verified(
                            value={"content": content},
                            root_kind="chat_history.tool_message",
                            namespace=doc_namespace,
                            request_id=entry_request_id,
                            threshold_bytes=tool_threshold_bytes,
                            field_label=(
                                f"chat_history.{doc.get('_id')}.history."
                                f"{index}.content"
                            ),
                        )
                        if compacted and isinstance(compacted.payload, Mapping):
                            set_fields.update(
                                _diff_set_operations(
                                    {"content": content},
                                    compacted.payload,
                                    prefix=("history", str(index)),
                                )
                            )
                            doc_offloaded += compacted.offloaded_count
                            doc_verified += compacted.offloaded_count
        except Exception as exc:
            stats.errors += 1
            print(
                f"[offload] ERROR chat_history _id={doc.get('_id')}: {exc}",
                file=sys.stderr,
            )
            continue

        if doc_offloaded <= 0 and not set_fields:
            stats.skipped += 1
            continue

        stats.candidates += 1
        stats.inline_bytes_before += before
        if not apply:
            continue

        try:
            set_fields["debug_payload_offload_migration"] = {
                "schema_version": "debug_payload_offload_migration.v1",
                "status": "applied",
                "updated_at_utc": _utcnow_iso(),
                "strategy": "field_patch_after_blob_round_trip_verification",
            }
            _apply_update(coll, {"_id": doc["_id"]}, set_fields)
            stats.updated += 1
            stats.offloaded_payloads += doc_offloaded
            stats.verified_payloads += doc_verified
            stats.set_operations += len(set_fields)
            after_history = json.loads(json.dumps(history, default=str))
            for dotted_path, value in set_fields.items():
                if dotted_path == "debug_payload_offload_migration":
                    continue
                _apply_local_set(after_history, dotted_path, value)
            stats.inline_bytes_after += estimate_payload_size_bytes(after_history)
        except Exception as exc:
            stats.errors += 1
            print(
                f"[offload] ERROR chat_history _id={doc.get('_id')}: {exc}",
                file=sys.stderr,
            )

    return stats


def _apply_local_set(payload: Any, dotted_path: str, value: Any) -> None:
    current = payload
    parts = dotted_path.split(".")
    if parts and parts[0] == "history":
        parts = parts[1:]
    for raw_part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(raw_part)]
        elif isinstance(current, dict):
            current = current.setdefault(raw_part, {})
        else:
            return
    final = parts[-1] if parts else None
    if final is None:
        return
    if isinstance(current, list):
        current[int(final)] = value
    elif isinstance(current, dict):
        current[final] = value


def _scan_turn_execution_records(
    *,
    coll: Any,
    apply: bool,
    limit: int | None,
    threshold_bytes: int,
    namespace: str | None = None,
    request_id: str | None = None,
) -> OffloadStats:
    stats = OffloadStats(collection=TURN_EXECUTION_RECORDS_COLLECTION)
    cursor = coll.find(_turn_record_query(namespace=namespace, request_id=request_id))
    if limit:
        cursor = cursor.limit(limit)

    for doc in cursor:
        stats.scanned += 1
        original = {key: value for key, value in doc.items() if key != "_id"}
        before = estimate_payload_size_bytes(original)
        if before <= threshold_bytes:
            stats.skipped += 1
            continue

        stats.candidates += 1
        stats.inline_bytes_before += before
        if not apply:
            continue

        try:
            doc_request_id = _clean_str(original.get("request_id"))
            doc_namespace = _clean_str(original.get("namespace"))
            compacted = _compact_verified(
                value=original,
                root_kind="turn_execution_record",
                namespace=doc_namespace,
                request_id=doc_request_id,
                threshold_bytes=threshold_bytes,
                field_label=f"turn_execution_records.{doc.get('_id')}",
            )
            if not compacted or not isinstance(compacted.payload, Mapping):
                stats.skipped += 1
                continue

            set_fields = _diff_set_operations(
                original,
                compacted.payload,
                prefix=(),
            )
            if not set_fields:
                stats.skipped += 1
                continue
            set_fields["debug_payload_offload_migration"] = {
                "schema_version": "debug_payload_offload_migration.v1",
                "status": "applied",
                "updated_at_utc": _utcnow_iso(),
                "strategy": "field_patch_after_blob_round_trip_verification",
            }
            update_query: dict[str, Any] = {"_id": doc["_id"]}
            if doc_request_id:
                update_query["request_id"] = doc_request_id
            _apply_update(coll, update_query, set_fields)
            stats.updated += 1
            stats.inline_bytes_after += estimate_payload_size_bytes(compacted.payload)
            stats.offloaded_payloads += compacted.offloaded_count
            stats.verified_payloads += compacted.offloaded_count
            stats.degraded_payloads += compacted.degraded_count
            stats.set_operations += len(set_fields)
        except Exception as exc:
            stats.errors += 1
            print(
                f"[offload] ERROR turn_execution_records _id={doc.get('_id')}: {exc}",
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
        description="Offload oversized Von debug payloads from MongoDB to blob storage."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply verified field updates. Default is dry-run."
    )
    parser.add_argument(
        "--collection",
        action="append",
        help="Collection to scan. Repeat or comma-separate. Default scans both.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Maximum documents per collection."
    )
    parser.add_argument("--namespace", default=None, help="Restrict to one namespace.")
    parser.add_argument(
        "--user-id", default=None, help="Restrict chat_history to one user_id."
    )
    parser.add_argument(
        "--session-id", default=None, help="Restrict chat_history to one session_id."
    )
    parser.add_argument(
        "--request-id", default=None, help="Restrict to one request_id where available."
    )
    parser.add_argument(
        "--threshold-bytes",
        type=int,
        default=default_debug_payload_threshold_bytes(),
        help="Debug payload threshold in bytes.",
    )
    parser.add_argument(
        "--tool-threshold-bytes",
        type=int,
        default=default_tool_message_threshold_bytes(),
        help="Tool message threshold in bytes.",
    )
    args = parser.parse_args(argv)

    db = get_db()
    if db is None:
        raise SystemExit("Could not connect to MongoDB")

    apply = bool(args.apply)
    selected_collections = _parse_collections(args.collection)
    namespace = _clean_str(args.namespace)
    user_id = _clean_str(args.user_id)
    session_id = _clean_str(args.session_id)
    request_id = _clean_str(args.request_id)
    print("[offload] mode=" + ("apply" if apply else "dry-run"))
    print("[offload] collections=" + ",".join(selected_collections))
    print(
        "[offload] thresholds "
        f"debug={args.threshold_bytes} tool={args.tool_threshold_bytes}"
    )
    if any((namespace, user_id, session_id, request_id)):
        print(
            "[offload] filters "
            + json.dumps(
                {
                    "namespace": namespace,
                    "user_id": user_id,
                    "session_id": session_id,
                    "request_id": request_id,
                },
                sort_keys=True,
            )
        )

    results: list[OffloadStats] = []
    if CHAT_HISTORY_COLLECTION in selected_collections:
        results.append(
            _scan_chat_history(
                coll=db[CHAT_HISTORY_COLLECTION],
                apply=apply,
                limit=args.limit,
                threshold_bytes=args.threshold_bytes,
                tool_threshold_bytes=args.tool_threshold_bytes,
                namespace=namespace,
                user_id=user_id,
                session_id=session_id,
                request_id=request_id,
            )
        )
    if TURN_EXECUTION_RECORDS_COLLECTION in selected_collections:
        results.append(
            _scan_turn_execution_records(
                coll=db[TURN_EXECUTION_RECORDS_COLLECTION],
                apply=apply,
                limit=args.limit,
                threshold_bytes=args.threshold_bytes,
                namespace=namespace,
                request_id=request_id,
            )
        )
    payload = {
        "schema_version": "debug_payload_offload_report.v1",
        "results": [asdict(r) for r in results],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if all(result.errors == 0 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
