from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from src.backend.db.mongo_client import get_db

logger = logging.getLogger(__name__)

TOOL_PROGRESS_STATE_COLLECTION_NAME = "tool_progress_state"

_indexes_ensured = False


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
