"""Bounded actor-scoped input diagnostics. No audio or transcript contents."""

from __future__ import annotations

import hashlib
import math
import re
import threading
from datetime import UTC, datetime, timedelta

from ..db.mongo_client import get_db

_indexed = set()
_index_lock = threading.Lock()


def sanitise_client_context(value, user_agent=None):
    from .client_capabilities_service import _summarise_user_agent

    value = value if isinstance(value, dict) else {}
    out = {
        "schema_version": "client_context.v1",
        "client_reported": True,
        "user_agent_summary": _summarise_user_agent(
            user_agent
            or (
                value.get("user_agent")
                if isinstance(value.get("user_agent"), str)
                else None
            )
        ),
    }
    original_summary = value.get("user_agent_summary")
    if out["user_agent_summary"]["browser_family"] == "Other" and isinstance(
        original_summary, dict
    ):
        family = original_summary.get("browser_family")
        major = original_summary.get("major_version")
        if family in {"Chrome", "Safari", "Edge", "Firefox", "Other"}:
            out["user_agent_summary"] = {
                "browser_family": family,
                "major_version": major
                if isinstance(major, int) and 0 < major < 10000
                else None,
            }
    for key in (
        "client_id",
        "captured_at",
        "platform",
        "platform_version",
        "device_model",
        "orientation",
        "display_mode",
        "visibility",
        "frontend_asset",
        "language",
        "speech_item_id",
    ):
        raw = value.get(key)
        if isinstance(raw, str):
            out[key] = raw[:160]
    for key in ("touch_points", "viewport_width", "viewport_height"):
        raw = value.get(key)
        if (
            isinstance(raw, (int, float))
            and not isinstance(raw, bool)
            and math.isfinite(raw)
        ):
            out[key] = max(0, min(20000, int(raw)))
    for key in ("mobile", "secure_context"):
        if isinstance(value.get(key), bool):
            out[key] = value[key]
    out["brands"] = [
        {"brand": b["brand"][:80], "version": b["version"][:40]}
        for b in (value.get("brands") if isinstance(value.get("brands"), list) else [])[
            :5
        ]
        if isinstance(b, dict)
        and isinstance(b.get("brand"), str)
        and isinstance(b.get("version"), str)
    ]
    out["speech_attempt_ids"] = (
        [
            v[:100]
            for v in (value.get("speech_attempt_ids") or [])[:8]
            if isinstance(v, str)
        ]
        if isinstance(value.get("speech_attempt_ids"), list)
        else []
    )
    return out


def request_client_context():
    from flask import has_request_context, request

    if not has_request_context():
        return None
    payload = request.get_json(silent=True) if request.is_json else None
    raw = payload.get("client_context") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return None
    return sanitise_client_context(raw, request.headers.get("User-Agent"))


def _collection():
    db = get_db()
    if db is None:
        raise RuntimeError("Speech telemetry storage is unavailable")
    coll = db["speech_input_attempts"]
    key = (id(db.client), db.name)
    with _index_lock:
        if key not in _indexed:
            coll.create_index("expires_at", expireAfterSeconds=0)
            _indexed.add(key)
    return coll


def _key(actor, organisation, attempt_id):
    if not isinstance(attempt_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{8,100}", attempt_id
    ):
        raise ValueError("Invalid speech attempt ID")
    return hashlib.sha256(
        (actor + "|" + (organisation or "") + "|" + attempt_id).encode()
    ).hexdigest()


def record_attempt(*, actor, organisation, attempt_id, payload, user_agent=None):
    key = _key(actor, organisation, attempt_id)
    payload = payload if isinstance(payload, dict) else {}
    raw = payload.get("event")
    raw = raw if isinstance(raw, dict) else {}
    allowed = {
        "state",
        "started",
        "ended",
        "error",
        "revision",
        "committed",
        "submitted",
        "speech_started",
        "speech_stopped",
        "playback_started",
        "playback_ended",
        "interrupted",
        "recovery",
    }
    if raw.get("name") not in allowed:
        raise ValueError("Unknown speech event")
    event = {"name": raw["name"]}
    for name in (
        "state",
        "engine",
        "model",
        "reason",
        "item_id",
        "request_id",
        "submission_id",
        "format",
        "context_version",
    ):
        if isinstance(raw.get(name), str):
            event[name] = raw[name][:120]
    for name in (
        "sequence",
        "elapsed_ms",
        "result_index",
        "result_count",
        "text_length",
        "audio_bytes",
        "vocabulary_count",
        "context_chars",
        "sample_rate",
        "channel_count",
    ):
        value = raw.get(name)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            event[name] = max(0, min(100000000, int(value)))
    if isinstance(raw.get("final"), bool):
        event["final"] = raw["final"]
    from .runtime_code_version_service import get_runtime_code_version

    now = datetime.now(UTC)
    event["received_at"] = now
    conversation_id = payload.get("conversation_id")
    coll = _collection()
    coll.update_one(
        {"_id": key},
        {
            "$setOnInsert": {
                "actor": actor,
                "organisation": organisation,
                "attempt_id": attempt_id,
                "client_context": sanitise_client_context(
                    payload.get("client_context"), user_agent
                ),
                "conversation_id": conversation_id[:100]
                if isinstance(conversation_id, str)
                else None,
                "backend_version": get_runtime_code_version(),
                "created_at": now,
            },
            "$set": {"updated_at": now, "expires_at": now + timedelta(days=30)},
            "$push": {"events": {"$each": [event], "$slice": -100}},
        },
        upsert=True,
    )
    return {
        "stored": True,
        "attempt_id": attempt_id,
        "event_sequence": event.get("sequence"),
    }


def get_attempt(*, actor, organisation, attempt_id):
    return _collection().find_one(
        {
            "_id": _key(actor, organisation, attempt_id),
            "expires_at": {"$gt": datetime.now(UTC)},
        },
        {"_id": 0},
    )
