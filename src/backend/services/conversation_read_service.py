"""Per-viewer read cursors for ordinary conversations, with a quiet baseline."""

import hashlib
from datetime import UTC, datetime

from ..db.mongo_setup import get_application_settings_collection
from . import settings_service


def _key(actor, session_id):
    return (
        "conversation_read:"
        + hashlib.sha256(f"{actor}\0{session_id}".encode()).hexdigest()
    )


def project_unread(actor, rows):
    baseline_key = _key(actor, "__baseline__")
    keys = [_key(actor, row["session_id"]) for row in rows]
    values = settings_service.get_settings_batch([baseline_key, *keys])
    baseline = values.get(baseline_key)
    if not baseline:
        collection = get_application_settings_collection()
        if collection is None:
            raise RuntimeError("Read state unavailable.")
        collection.update_one(
            {"setting_name": baseline_key},
            {
                "$setOnInsert": {
                    "value": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
        baseline = settings_service.get_setting(baseline_key)
    from .chat_history_service import _coerce_datetime

    baseline_at = _coerce_datetime(baseline)
    for row, key in zip(rows, keys):
        if row.get("shared_with_me"):
            continue  # Existing invitation receipts remain authoritative.
        last = _coerce_datetime(row.get("last_incoming_contribution_at"))
        read = _coerce_datetime(values.get(key)) or baseline_at
        row["shared_unread_count"] = int(bool(last and read and last > read))
    return rows


def acknowledge(actor, session_id, published_at):
    from .chat_history_service import _coerce_datetime

    timestamp = _coerce_datetime(published_at)
    if timestamp is None:
        raise ValueError("Invalid contribution timestamp.")
    collection = get_application_settings_collection()
    if collection is None:
        raise RuntimeError("Read state unavailable.")
    # Canonical settings value; max makes concurrent acknowledgements monotonic.
    key = _key(actor, session_id)
    collection.update_one(
        {"setting_name": key},
        {
            "$max": {"value": timestamp.isoformat()},
            "$set": {"updated_at": datetime.now(UTC)},
        },
        upsert=True,
    )
    return settings_service.get_setting(key)
