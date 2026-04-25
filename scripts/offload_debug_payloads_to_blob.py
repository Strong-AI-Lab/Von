#!/usr/bin/env python
"""Offload oversized stored debug payloads from MongoDB to the blob store.

The command is intentionally conservative: dry-run is the default, and apply
mode only replaces known storage-heavy debug fields with compact blob refs after
the durable blob write succeeds or a degraded compact marker is produced.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
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
    compact_debug_payload_for_storage,
    default_debug_payload_threshold_bytes,
    default_tool_message_threshold_bytes,
    estimate_payload_size_bytes,
)


CHAT_HISTORY_COLLECTION = "chat_history"
TURN_EXECUTION_RECORDS_COLLECTION = "turn_execution_records"


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


def _request_id_from_debug(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    request_id = value.get("request_id")
    if isinstance(request_id, str) and request_id.strip():
        return request_id.strip()
    turn_record = value.get("turn_execution_record")
    if isinstance(turn_record, Mapping):
        request_id = turn_record.get("request_id")
        if isinstance(request_id, str) and request_id.strip():
            return request_id.strip()
    return None


def _history_entry_request_id(entry: Mapping[str, Any]) -> str | None:
    request_id = _request_id_from_debug(entry.get("llm_debug_data"))
    if request_id:
        return request_id
    request_id = entry.get("request_id")
    return (
        request_id.strip()
        if isinstance(request_id, str) and request_id.strip()
        else None
    )


def _is_candidate(value: Any, *, threshold_bytes: int) -> bool:
    return estimate_payload_size_bytes(value) > threshold_bytes


def _scan_chat_history(
    *,
    coll: Any,
    apply: bool,
    limit: int | None,
    threshold_bytes: int,
    tool_threshold_bytes: int,
) -> OffloadStats:
    stats = OffloadStats(collection=CHAT_HISTORY_COLLECTION)
    cursor = coll.find(
        {}, {"history": 1, "namespace": 1, "user_id": 1, "session_id": 1}
    )
    if limit:
        cursor = cursor.limit(limit)

    for doc in cursor:
        stats.scanned += 1
        history = doc.get("history")
        if not isinstance(history, list):
            stats.skipped += 1
            continue

        namespace = doc.get("namespace")
        if not isinstance(namespace, str) or not namespace.strip():
            namespace = doc.get("user_id")
        before = estimate_payload_size_bytes(history)
        is_candidate = before > threshold_bytes
        if not is_candidate:
            is_candidate = any(
                isinstance(entry, Mapping)
                and (
                    _is_candidate(
                        entry.get("llm_debug_data"), threshold_bytes=threshold_bytes
                    )
                    or (
                        entry.get("role") == "tool"
                        and _is_candidate(
                            entry.get("content"), threshold_bytes=tool_threshold_bytes
                        )
                    )
                )
                for entry in history
            )
        if not is_candidate:
            stats.skipped += 1
            continue

        stats.candidates += 1
        stats.inline_bytes_before += before
        if not apply:
            continue

        try:
            changed = False
            new_history: list[Any] = []
            for entry in history:
                if not isinstance(entry, Mapping):
                    new_history.append(entry)
                    continue
                request_id = _history_entry_request_id(entry)
                threshold = (
                    tool_threshold_bytes
                    if entry.get("role") == "tool"
                    else threshold_bytes
                )
                compacted = compact_debug_payload_for_storage(
                    dict(entry),
                    root_kind="chat_history.history_entry",
                    namespace=namespace if isinstance(namespace, str) else None,
                    request_id=request_id,
                    threshold_bytes=threshold,
                    fail_soft=True,
                )
                new_entry = compacted.payload
                new_history.append(new_entry)
                changed = (
                    changed
                    or compacted.offloaded_count > 0
                    or compacted.degraded_count > 0
                )
                stats.offloaded_payloads += compacted.offloaded_count
                stats.degraded_payloads += compacted.degraded_count

            after = estimate_payload_size_bytes(new_history)
            stats.inline_bytes_after += after
            if not changed:
                stats.skipped += 1
                continue
            coll.update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "history": new_history,
                        "debug_payload_offload_migration": {
                            "schema_version": "debug_payload_offload_migration.v1",
                            "status": "applied",
                        },
                    }
                },
            )
            stats.updated += 1
        except Exception as exc:
            stats.errors += 1
            print(
                f"[offload] ERROR chat_history _id={doc.get('_id')}: {exc}",
                file=sys.stderr,
            )

    return stats


def _scan_turn_execution_records(
    *,
    coll: Any,
    apply: bool,
    limit: int | None,
    threshold_bytes: int,
) -> OffloadStats:
    stats = OffloadStats(collection=TURN_EXECUTION_RECORDS_COLLECTION)
    cursor = coll.find({})
    if limit:
        cursor = cursor.limit(limit)

    for doc in cursor:
        stats.scanned += 1
        before = estimate_payload_size_bytes(doc)
        if before <= threshold_bytes:
            stats.skipped += 1
            continue
        stats.candidates += 1
        stats.inline_bytes_before += before
        if not apply:
            continue

        try:
            request_id = doc.get("request_id")
            namespace = doc.get("namespace")
            compacted = compact_debug_payload_for_storage(
                {key: value for key, value in doc.items() if key != "_id"},
                root_kind="turn_execution_record",
                namespace=namespace if isinstance(namespace, str) else None,
                request_id=request_id if isinstance(request_id, str) else None,
                threshold_bytes=threshold_bytes,
                fail_soft=True,
            )
            if not isinstance(compacted.payload, Mapping):
                stats.skipped += 1
                continue
            replacement = dict(compacted.payload)
            after = estimate_payload_size_bytes(replacement)
            stats.inline_bytes_after += after
            if compacted.offloaded_count <= 0 and compacted.degraded_count <= 0:
                stats.skipped += 1
                continue
            replacement["debug_payload_offload_migration"] = {
                "schema_version": "debug_payload_offload_migration.v1",
                "status": "applied",
            }
            coll.update_one({"_id": doc["_id"]}, {"$set": replacement})
            stats.updated += 1
            stats.offloaded_payloads += compacted.offloaded_count
            stats.degraded_payloads += compacted.degraded_count
        except Exception as exc:
            stats.errors += 1
            print(
                f"[offload] ERROR turn_execution_records _id={doc.get('_id')}: {exc}",
                file=sys.stderr,
            )

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offload oversized Von debug payloads from MongoDB to blob storage."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply changes. Default is dry-run."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Maximum documents per collection."
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
    print("[offload] mode=" + ("apply" if apply else "dry-run"))
    print(
        "[offload] thresholds "
        f"debug={args.threshold_bytes} tool={args.tool_threshold_bytes}"
    )

    results = [
        _scan_chat_history(
            coll=db[CHAT_HISTORY_COLLECTION],
            apply=apply,
            limit=args.limit,
            threshold_bytes=args.threshold_bytes,
            tool_threshold_bytes=args.tool_threshold_bytes,
        ),
        _scan_turn_execution_records(
            coll=db[TURN_EXECUTION_RECORDS_COLLECTION],
            apply=apply,
            limit=args.limit,
            threshold_bytes=args.threshold_bytes,
        ),
    ]
    payload = {
        "schema_version": "debug_payload_offload_report.v1",
        "results": [asdict(r) for r in results],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if all(result.errors == 0 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
