"""Attempt-bound steering mailbox on the existing durable prompt queue record.

Steering never changes FIFO admission or grants a new actor's authority. Delivery
means copied into the active model context, not that a requested effect occurred.
"""

from __future__ import annotations

from typing import Any, Mapping
from pymongo import ReturnDocument

from . import chat_prompt_queue_service as queue


def _query(scope: Mapping[str, Any], queue_id: str) -> dict[str, Any]:
    return {
        **queue._compatible_scope_query(scope),
        "queue_id": queue._coerce_scope_value(queue_id, field="queue_id"),
    }


def read(*, scope: Mapping[str, Any], queue_id: str) -> dict[str, Any]:
    doc = queue._collection().find_one(_query(scope, queue_id))
    if doc is None:
        raise queue.ChatPromptQueueRecordNotFound("Turn not found")
    pending = doc.get("steering_pending", [])
    cancelled = doc.get("steering_cancelled", [])
    delivered = doc.get("steering_delivered", [])
    active = doc.get("status") == queue.STATUS_IN_PROGRESS and not doc.get(
        "cancellation_requested_at"
    )
    items = []
    for item in doc.get("steering_messages", []):
        if item["id"] in cancelled:
            status = "cancelled"
        elif item["id"] in delivered:
            status = "delivered"
        elif (
            active
            and item.get("attempt_id") == doc.get("attempt_id")
            and item["id"] in pending
        ):
            status = "pending"
        else:
            status = "not_applied"
        items.append({**item, "status": status})
    return {"queue_id": queue_id, "items": items}


def submit(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    attempt_id: str,
    submission_id: str,
    text: str,
) -> dict[str, Any]:
    attempt_id = queue._coerce_scope_value(attempt_id, field="attempt_id")
    submission_id = queue._coerce_scope_value(submission_id, field="submission_id")
    if (
        len(submission_id) > 200
        or not isinstance(text, str)
        or not text.strip()
        or len(text) > queue.MAX_PROMPT_RAW_CHARS
    ):
        raise queue.InvalidChatPromptQueueInput(
            "Supply a non-empty steering message within the prompt size limit"
        )
    query = {**_query(scope, queue_id), "attempt_id": attempt_id}
    item = {"id": submission_id, "text": text, "attempt_id": attempt_id}
    doc = queue._collection().find_one_and_update(
        {
            **query,
            "status": queue.STATUS_IN_PROGRESS,
            "active_conversation_key": {"$exists": True},
            "cancellation_requested_at": {"$exists": False},
            "steering_closed_attempt_id": {"$ne": attempt_id},
            "steering_messages.id": {"$ne": submission_id},
            # Keep this embedded mailbox below Mongo's document size boundary.
            "steering_messages.99": {"$exists": False},
            "$or": [
                {"steering_char_count": {"$exists": False}},
                {
                    "steering_char_count": {
                        "$lte": queue.MAX_PROMPT_RAW_CHARS - len(text)
                    }
                },
            ],
        },
        {
            "$push": {"steering_messages": item, "steering_pending": submission_id},
            "$inc": {"steering_char_count": len(text)},
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        doc = queue._collection().find_one(query)
        previous = next(
            (
                m
                for m in (doc or {}).get("steering_messages", [])
                if m["id"] == submission_id
            ),
            None,
        )
        if previous != item:
            raise queue.ChatPromptQueueRecordNotFound(
                "Steering was not accepted: the turn ended, is stopping, the mailbox is full, or the message conflicts. Your draft can be queued instead.",
                error_code="steering_not_accepted",
            )
    return read(scope=scope, queue_id=queue_id)


def cancel(
    *, scope: Mapping[str, Any], queue_id: str, submission_id: str
) -> dict[str, Any]:
    submission_id = queue._coerce_scope_value(submission_id, field="submission_id")
    query = _query(scope, queue_id)
    doc = queue._collection().find_one_and_update(
        {**query, "steering_pending": submission_id},
        {
            "$pull": {"steering_pending": submission_id},
            "$addToSet": {"steering_cancelled": submission_id},
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        doc = queue._collection().find_one(
            {**query, "steering_cancelled": submission_id}
        )
        if doc is None:
            raise queue.ChatPromptQueueRecordNotFound(
                "Steering already delivered or not found; it cannot be withdrawn"
            )
    return read(scope=scope, queue_id=queue_id)


def take(
    *,
    scope: Mapping[str, Any],
    queue_id: str,
    attempt_id: str,
    close_if_empty: bool = False,
) -> list[dict[str, str]]:
    query = {
        **_query(scope, queue_id),
        "attempt_id": attempt_id,
        "status": queue.STATUS_IN_PROGRESS,
        "cancellation_requested_at": {"$exists": False},
    }
    coll = queue._collection()
    while True:
        if close_if_empty:
            closed = coll.find_one_and_update(
                {**query, "steering_pending.0": {"$exists": False}},
                {"$set": {"steering_closed_attempt_id": attempt_id}},
            )
            if closed is not None:
                return []
        doc = coll.find_one({**query, "steering_pending.0": {"$exists": True}})
        if not doc:
            if close_if_empty and coll.find_one(query):
                # Withdrawal raced final closure: retry the empty-mailbox CAS.
                continue
            return []
        pending = doc["steering_pending"]
        items = [
            m
            for m in doc.get("steering_messages", [])
            if m["id"] in pending and m.get("attempt_id") == attempt_id
        ]
        changed = coll.find_one_and_update(
            {**query, "steering_pending": pending},
            {
                "$set": {"steering_pending": []},
                "$addToSet": {
                    "steering_delivered": {"$each": [m["id"] for m in items]}
                },
            },
        )
        if changed is None:
            # A submit/withdrawal raced this snapshot; preserve its ordering.
            continue
        if items or not close_if_empty:
            return items
        # Only stale-attempt input remained. Close this attempt atomically too.
