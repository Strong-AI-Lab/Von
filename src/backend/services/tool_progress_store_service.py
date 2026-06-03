from __future__ import annotations

import copy
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from src.backend.db.mongo_client import get_db

logger = logging.getLogger(__name__)

TOOL_PROGRESS_STATE_COLLECTION_NAME = "tool_progress_state"
TOOL_PROGRESS_PERSISTENCE_TELEMETRY_SCHEMA_VERSION = (
    "tool_progress_persistence.v1"
)
DEFAULT_PROGRESS_FLUSH_INTERVAL_SECONDS = 10.0

_indexes_ensured = False
_queue_lock = threading.Lock()
_pending_progress_flushes: dict[tuple[str, str], "_PendingProgressFlush"] = {}
_last_flush_epoch_by_key: dict[tuple[str, str], float] = {}


@dataclass
class _PendingProgressFlush:
    scope_key: str
    request_id: str
    payload: dict[str, Any]
    ttl_seconds: int
    queued_at_epoch: float
    queued_at_utc: str
    update_count: int = 1
    last_error: str | None = None
    failed_flush_count: int = 0


def _get_collection():
    try:
        db = get_db()
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.debug("[tool_progress_store] Failed to resolve DB: %s", exc)
        return None
    if db is None:
        return None
    return db[TOOL_PROGRESS_STATE_COLLECTION_NAME]


def _ensure_indexes() -> None:
    global _indexes_ensured
    if _indexes_ensured:
        return

    coll = _get_collection()
    if coll is None:
        return

    try:
        coll.create_index(
            [("scope_key", 1), ("request_id", 1)],
            unique=True,
            name="scope_request_unique",
        )
        coll.create_index([("request_id", 1)], name="request_id_1")
        coll.create_index([("updated_at", -1)], name="updated_at_-1")
        # TTL based on the stored expiry timestamp. Mongo ignores documents that do
        # not have this field, so local-memory fallback still works cleanly.
        coll.create_index(
            [("expires_at", 1)],
            expireAfterSeconds=0,
            name="expires_at_ttl",
        )
    except Exception as exc:  # pragma: no cover - DB backend dependent
        logger.debug("[tool_progress_store] Index creation skipped/failed: %s", exc)
    finally:
        _indexes_ensured = True


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_float(name: str, default: float, *, min_value: float) -> float:
    raw = os.environ.get(name)
    if not isinstance(raw, str) or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except Exception:
        return default
    if value < min_value:
        return min_value
    return value


def tool_progress_persistence_flush_interval_seconds() -> float:
    return _env_float(
        "VON_TOOL_PROGRESS_PERSISTENCE_FLUSH_INTERVAL_SECONDS",
        DEFAULT_PROGRESS_FLUSH_INTERVAL_SECONDS,
        min_value=0.0,
    )


def _sync_progress_persistence_enabled() -> bool:
    mode = os.environ.get("VON_TOOL_PROGRESS_PERSISTENCE_MODE", "").strip().lower()
    if mode in {"sync", "synchronous", "foreground"}:
        return True
    raw = os.environ.get("VON_TOOL_PROGRESS_PERSISTENCE_SYNC")
    return isinstance(raw, str) and raw.strip().lower() in {"1", "true", "yes", "on"}


def _base_progress_persistence_telemetry(
    *,
    status: str,
    pending_count: int,
    queued_update_count: int,
    flush_reason: str | None = None,
    synchronous_durability: bool = False,
    terminal_state: bool = False,
) -> dict[str, Any]:
    telemetry: dict[str, Any] = {
        "schema_version": TOOL_PROGRESS_PERSISTENCE_TELEMETRY_SCHEMA_VERSION,
        "status": status,
        "pending_count": max(0, int(pending_count)),
        "queued_update_count": max(0, int(queued_update_count)),
        "synchronous_durability": bool(synchronous_durability),
        "terminal_state": bool(terminal_state),
        "flush_interval_seconds": tool_progress_persistence_flush_interval_seconds(),
        "updated_at_utc": _utc_iso_now(),
    }
    if flush_reason:
        telemetry["flush_reason"] = flush_reason
    return telemetry


def _payload_with_progress_persistence(
    payload: Mapping[str, Any],
    telemetry: Mapping[str, Any],
) -> dict[str, Any]:
    stored_payload = copy.deepcopy(dict(payload))
    stored_payload["progress_persistence"] = copy.deepcopy(dict(telemetry))
    return stored_payload


def _flush_pending_progress_entry(
    key: tuple[str, str],
    pending: _PendingProgressFlush,
    *,
    flush_reason: str,
    synchronous_durability: bool,
    terminal_state: bool,
) -> dict[str, Any]:
    telemetry = _base_progress_persistence_telemetry(
        status="flushed",
        pending_count=len(_pending_progress_flushes),
        queued_update_count=pending.update_count,
        flush_reason=flush_reason,
        synchronous_durability=synchronous_durability,
        terminal_state=terminal_state,
    )
    telemetry["flush_started_at_utc"] = _utc_iso_now()
    ok = store_tool_progress_state(
        scope_key=pending.scope_key,
        request_id=pending.request_id,
        payload=_payload_with_progress_persistence(pending.payload, telemetry),
        ttl_seconds=pending.ttl_seconds,
    )
    if ok:
        _last_flush_epoch_by_key[key] = time.time()
        _pending_progress_flushes.pop(key, None)
        telemetry.update(
            {
                "status": "flushed",
                "flushed_at_utc": _utc_iso_now(),
                "pending_count": len(_pending_progress_flushes),
            }
        )
        return telemetry

    pending.failed_flush_count += 1
    pending.last_error = "store_tool_progress_state returned false"
    _pending_progress_flushes[key] = pending
    telemetry.update(
        {
            "status": "failed",
            "failed_flush_count": pending.failed_flush_count,
            "last_error": pending.last_error,
            "pending_count": len(_pending_progress_flushes),
        }
    )
    return telemetry


def queue_tool_progress_state_persistence(
    *,
    scope_key: str,
    request_id: str,
    payload: Mapping[str, Any],
    ttl_seconds: int,
    terminal_state: bool = False,
    synchronous_durability: bool = False,
    force_flush: bool = False,
) -> dict[str, Any]:
    """Queue a progress snapshot for durable persistence.

    Live progress remains memory-first.  This helper coalesces repeated
    foreground updates by ``(scope_key, request_id)`` and flushes only when a
    terminal state, explicit synchronous durability request, or bounded flush
    interval requires it.  It is a support surface: it does not interpret turn
    semantics beyond the caller-provided terminal/synchronous flags.
    """

    if not isinstance(scope_key, str) or not scope_key.strip():
        return _base_progress_persistence_telemetry(
            status="skipped",
            pending_count=0,
            queued_update_count=0,
        )
    if not isinstance(request_id, str) or not request_id.strip():
        return _base_progress_persistence_telemetry(
            status="skipped",
            pending_count=0,
            queued_update_count=0,
        )
    if not isinstance(payload, Mapping):
        return _base_progress_persistence_telemetry(
            status="skipped",
            pending_count=0,
            queued_update_count=0,
        )

    clean_scope = scope_key.strip()
    clean_request = request_id.strip()
    key = (clean_scope, clean_request)
    now_epoch = time.time()
    now_utc = _utc_iso_now()
    flush_interval = tool_progress_persistence_flush_interval_seconds()
    sync_requested = bool(synchronous_durability) or _sync_progress_persistence_enabled()

    with _queue_lock:
        existing = _pending_progress_flushes.get(key)
        update_count = int(existing.update_count) + 1 if existing else 1
        pending = _PendingProgressFlush(
            scope_key=clean_scope,
            request_id=clean_request,
            payload=copy.deepcopy(dict(payload)),
            ttl_seconds=max(1, int(ttl_seconds)),
            queued_at_epoch=existing.queued_at_epoch if existing else now_epoch,
            queued_at_utc=existing.queued_at_utc if existing else now_utc,
            update_count=update_count,
            last_error=existing.last_error if existing else None,
            failed_flush_count=existing.failed_flush_count if existing else 0,
        )
        _pending_progress_flushes[key] = pending
        last_flush = _last_flush_epoch_by_key.get(key)
        comparison_epoch = (
            float(last_flush) if last_flush is not None else pending.queued_at_epoch
        )
        interval_due = (
            flush_interval <= 0
            or (now_epoch - comparison_epoch) >= flush_interval
        )
        should_flush = bool(force_flush or terminal_state or sync_requested or interval_due)
        if not should_flush:
            telemetry = _base_progress_persistence_telemetry(
                status="queued",
                pending_count=len(_pending_progress_flushes),
                queued_update_count=update_count,
                synchronous_durability=sync_requested,
                terminal_state=terminal_state,
            )
            if pending.last_error:
                telemetry["last_error"] = pending.last_error
                telemetry["failed_flush_count"] = pending.failed_flush_count
            return telemetry

        if terminal_state:
            reason = "terminal_state"
        elif sync_requested:
            reason = "synchronous_durability"
        elif force_flush:
            reason = "force_flush"
        else:
            reason = "flush_interval"
        return _flush_pending_progress_entry(
            key,
            pending,
            flush_reason=reason,
            synchronous_durability=sync_requested,
            terminal_state=terminal_state,
        )


def flush_queued_tool_progress_states(
    *,
    max_items: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Flush due queued progress snapshots.

    Used by the optional background flusher and tests.  ``force=True`` flushes
    all pending entries; otherwise only entries whose bounded interval has
    elapsed are persisted.
    """

    limit = max_items if isinstance(max_items, int) and max_items > 0 else None
    now_epoch = time.time()
    flush_interval = tool_progress_persistence_flush_interval_seconds()
    flushed = failed = skipped = 0

    with _queue_lock:
        keys = list(_pending_progress_flushes.keys())
        if limit is not None:
            keys = keys[:limit]
        for key in keys:
            pending = _pending_progress_flushes.get(key)
            if pending is None:
                continue
            last_flush = _last_flush_epoch_by_key.get(key)
            comparison_epoch = (
                float(last_flush) if last_flush is not None else pending.queued_at_epoch
            )
            due = force or flush_interval <= 0 or (
                now_epoch - comparison_epoch
            ) >= flush_interval
            if not due:
                skipped += 1
                continue
            telemetry = _flush_pending_progress_entry(
                key,
                pending,
                flush_reason="force_flush" if force else "flush_interval",
                synchronous_durability=False,
                terminal_state=False,
            )
            if telemetry.get("status") == "flushed":
                flushed += 1
            else:
                failed += 1

    return {
        "schema_version": TOOL_PROGRESS_PERSISTENCE_TELEMETRY_SCHEMA_VERSION,
        "status": "flushed" if failed == 0 else "failed",
        "flushed_count": flushed,
        "failed_count": failed,
        "skipped_count": skipped,
        "pending_count": queued_tool_progress_state_count(),
        "updated_at_utc": _utc_iso_now(),
    }


def queued_tool_progress_state_count() -> int:
    with _queue_lock:
        return len(_pending_progress_flushes)


def discard_queued_tool_progress_state(*, scope_key: str, request_id: str) -> None:
    if not isinstance(scope_key, str) or not scope_key.strip():
        return
    if not isinstance(request_id, str) or not request_id.strip():
        return
    with _queue_lock:
        _pending_progress_flushes.pop((scope_key.strip(), request_id.strip()), None)
        _last_flush_epoch_by_key.pop((scope_key.strip(), request_id.strip()), None)


def reset_tool_progress_persistence_queue_for_tests() -> None:
    with _queue_lock:
        _pending_progress_flushes.clear()
        _last_flush_epoch_by_key.clear()


def store_tool_progress_state(
    *,
    scope_key: str,
    request_id: str,
    payload: Mapping[str, Any],
    ttl_seconds: int,
) -> bool:
    if not isinstance(scope_key, str) or not scope_key.strip():
        return False
    if not isinstance(request_id, str) or not request_id.strip():
        return False
    if not isinstance(payload, Mapping):
        return False

    coll = _get_collection()
    if coll is None:
        return False

    _ensure_indexes()

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=max(1, int(ttl_seconds)))
    stored_payload = copy.deepcopy(dict(payload))

    try:
        coll.update_one(
            {"scope_key": scope_key.strip(), "request_id": request_id.strip()},
            {
                "$set": {
                    "scope_key": scope_key.strip(),
                    "request_id": request_id.strip(),
                    "status": stored_payload.get("status"),
                    "stage": stored_payload.get("stage"),
                    "updated_at": now,
                    "expires_at": expires_at,
                    "payload": stored_payload,
                }
            },
            upsert=True,
        )
        return True
    except Exception as exc:  # pragma: no cover - DB backend dependent
        logger.debug(
            "[tool_progress_store] Failed to persist progress scope=%s request=%s: %s",
            scope_key,
            request_id,
            exc,
        )
        return False


def fetch_tool_progress_state(
    *,
    scope_key: str,
    request_id: str,
) -> dict[str, Any] | None:
    if not isinstance(scope_key, str) or not scope_key.strip():
        return None
    if not isinstance(request_id, str) or not request_id.strip():
        return None

    coll = _get_collection()
    if coll is None:
        return None

    _ensure_indexes()

    try:
        doc = coll.find_one(
            {"scope_key": scope_key.strip(), "request_id": request_id.strip()},
            {"_id": 0, "payload": 1},
        )
    except Exception as exc:  # pragma: no cover - DB backend dependent
        logger.debug(
            "[tool_progress_store] Failed to fetch progress scope=%s request=%s: %s",
            scope_key,
            request_id,
            exc,
        )
        return None

    if not isinstance(doc, Mapping):
        return None
    payload = doc.get("payload")
    if not isinstance(payload, Mapping):
        return None
    return copy.deepcopy(dict(payload))


def delete_tool_progress_state(*, scope_key: str, request_id: str) -> bool:
    coll = _get_collection()
    if coll is None:
        return False

    try:
        result = coll.delete_one(
            {"scope_key": scope_key.strip(), "request_id": request_id.strip()}
        )
        return bool(getattr(result, "deleted_count", 0))
    except Exception as exc:  # pragma: no cover - DB backend dependent
        logger.debug(
            "[tool_progress_store] Failed to delete progress scope=%s request=%s: %s",
            scope_key,
            request_id,
            exc,
        )
        return False


def clear_tool_progress_documents_for_tests() -> None:
    """Best-effort test helper to clear persisted progress state."""

    coll = _get_collection()
    if coll is None:
        return
    try:
        coll.delete_many({})
    except Exception as exc:  # pragma: no cover - DB backend dependent
        logger.debug("[tool_progress_store] Failed to clear test progress docs: %s", exc)
