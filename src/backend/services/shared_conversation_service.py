"""Shared conversation invitation storage and retrieval."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING, DESCENDING

from ..db.mongo_client import get_db

logger = logging.getLogger(__name__)

INVITES_COLLECTION_NAME = "shared_conversation_invites"

_INVITE_INDEXES_READY = False
_INVITE_INDEX_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_collection():
    db = get_db()
    if db is None:
        return None
    coll = db[INVITES_COLLECTION_NAME]
    _ensure_indexes(coll)
    return coll


def _ensure_indexes(coll) -> None:
    global _INVITE_INDEXES_READY
    if _INVITE_INDEXES_READY:
        return
    with _INVITE_INDEX_LOCK:
        if _INVITE_INDEXES_READY:
            return
        try:
            existing = [idx.get("name") for idx in coll.list_indexes()]
            if "invite_id_1" not in existing:
                coll.create_index([("invite_id", ASCENDING)], unique=True)
            if "invitee_user_id_1_status_1" not in existing:
                coll.create_index(
                    [("invitee_user_id", ASCENDING), ("status", ASCENDING)]
                )
            if "session_id_1_invitee_user_id_1" not in existing:
                coll.create_index(
                    [("session_id", ASCENDING), ("invitee_user_id", ASCENDING)]
                )
            if "session_id_1_updated_at_-1" not in existing:
                coll.create_index(
                    [("session_id", ASCENDING), ("updated_at", DESCENDING)]
                )
        except Exception as exc:  # pragma: no cover
            logger.debug("[shared_invites] Index creation skipped: %s", exc)
        _INVITE_INDEXES_READY = True


def create_invite(
    *,
    session_id: str,
    inviter_user_id: str,
    invitee_user_id: str,
    organisation_concept_id: str,
) -> Dict[str, Any]:
    """Create or return an existing pending invite."""
    coll = _get_collection()
    if coll is None:
        raise RuntimeError("Database unavailable")

    invite_id = str(uuid.uuid4())
    now = _utcnow()

    existing = coll.find_one(
        {
            "session_id": session_id,
            "invitee_user_id": invitee_user_id,
            "status": {"$in": ["pending", "accepted"]},
        },
        {"_id": 0},
    )
    if existing:
        return {"invite": existing, "created": False}

    conversation_owner_user_id = None
    owner_source = None
    try:
        existing_owner = coll.find_one(
            {"session_id": session_id, "conversation_owner_user_id": {"$exists": True}},
            {"_id": 0, "conversation_owner_user_id": 1},
        )
        if isinstance(existing_owner, dict):
            raw_owner = existing_owner.get("conversation_owner_user_id")
            if isinstance(raw_owner, str) and raw_owner.strip():
                conversation_owner_user_id = raw_owner.strip()
                owner_source = "existing_invite"
    except Exception:
        pass

    if not conversation_owner_user_id:
        try:
            from . import chat_history_service

            if chat_history_service.has_chat_history_session(
                inviter_user_id, session_id, namespace=None
            ):
                conversation_owner_user_id = inviter_user_id
                owner_source = "inviter"
            else:
                coll_history = (
                    chat_history_service.get_chat_history_collection_service()
                )
                if coll_history is not None:
                    owner_doc = coll_history.find_one(
                        {"session_id": session_id},
                        {"_id": 0, "user_id": 1, "created_at": 1},
                        sort=[("created_at", ASCENDING)],
                    )
                    if isinstance(owner_doc, dict):
                        owner_id = owner_doc.get("user_id")
                        if isinstance(owner_id, str) and owner_id.strip():
                            conversation_owner_user_id = owner_id.strip()
                            owner_source = "history_lookup"
        except Exception:
            pass

    doc = {
        "invite_id": invite_id,
        "session_id": session_id,
        "inviter_user_id": inviter_user_id,
        "invitee_user_id": invitee_user_id,
        "conversation_owner_user_id": conversation_owner_user_id,
        "conversation_owner_source": owner_source,
        "organisation_concept_id": organisation_concept_id,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    coll.insert_one(doc)
    if isinstance(doc, dict) and "_id" in doc:
        doc = dict(doc)
        doc.pop("_id", None)
    return {"invite": doc, "created": True}


def resolve_conversation_owner(*, session_id: str) -> Optional[str]:
    coll = _get_collection()
    if coll is None:
        return None
    if not isinstance(session_id, str) or not session_id.strip():
        return None

    session_id = session_id.strip()

    try:
        doc = coll.find_one(
            {
                "session_id": session_id,
                "conversation_owner_user_id": {"$exists": True, "$ne": None},
            },
            {"_id": 0, "conversation_owner_user_id": 1},
        )
        if isinstance(doc, dict):
            owner = doc.get("conversation_owner_user_id")
            if isinstance(owner, str) and owner.strip():
                return owner.strip()
    except Exception:
        pass

    try:
        from . import chat_history_service

        coll_history = chat_history_service.get_chat_history_collection_service()
        if coll_history is None:
            return None
        owner_doc = coll_history.find_one(
            {"session_id": session_id},
            {"_id": 0, "user_id": 1, "created_at": 1},
            sort=[("created_at", ASCENDING)],
        )
        if isinstance(owner_doc, dict):
            owner_id = owner_doc.get("user_id")
            if isinstance(owner_id, str) and owner_id.strip():
                return owner_id.strip()
    except Exception:
        return None

    return None


def list_invites_for_user(
    *,
    user_concept_id: str,
    status: Optional[str] = None,
    direction: str = "incoming",
    session_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    coll = _get_collection()
    if coll is None:
        return []

    query: Dict[str, Any] = {}
    if direction == "outgoing":
        query["inviter_user_id"] = user_concept_id
    else:
        query["invitee_user_id"] = user_concept_id

    if isinstance(status, str) and status.strip():
        query["status"] = status.strip()

    if isinstance(session_id, str) and session_id.strip():
        query["session_id"] = session_id.strip()

    cursor = coll.find(query, {"_id": 0}).sort("updated_at", DESCENDING)
    if isinstance(limit, int) and limit > 0:
        cursor = cursor.limit(min(limit, 200))
    return [doc for doc in cursor if isinstance(doc, dict)]


def get_accepted_invite_for_user_session(
    *, user_concept_id: str, session_id: str
) -> Optional[Dict[str, Any]]:
    coll = _get_collection()
    if coll is None:
        return None

    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return None
    if not isinstance(session_id, str) or not session_id.strip():
        return None

    doc = coll.find_one(
        {
            "invitee_user_id": user_concept_id.strip(),
            "session_id": session_id.strip(),
            "status": "accepted",
        },
        {"_id": 0},
    )
    return doc if isinstance(doc, dict) else None


def list_accepted_invites_for_user(
    *, user_concept_id: str, limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    return list_invites_for_user(
        user_concept_id=user_concept_id,
        status="accepted",
        direction="incoming",
        limit=limit,
    )


def list_accepted_invites_for_user_sessions(
    *, user_concept_id: str, session_ids: List[str]
) -> List[Dict[str, Any]]:
    """Return accepted invites for one actor and a bounded exact session set."""

    actor = user_concept_id.strip() if isinstance(user_concept_id, str) else ""
    unique_session_ids = list(
        dict.fromkeys(
            value.strip()
            for value in session_ids
            if isinstance(value, str) and value.strip()
        )
    )
    if not actor or not unique_session_ids:
        return []
    if len(unique_session_ids) > 100:
        raise ValueError("Accepted-invite lookup is limited to 100 session IDs.")
    coll = _get_collection()
    if coll is None:
        raise RuntimeError("Shared conversation invite storage is unavailable.")
    cursor = coll.find(
        {
            "invitee_user_id": actor,
            "status": "accepted",
            "session_id": {"$in": unique_session_ids},
        },
        {"_id": 0},
    )
    return [dict(row) for row in cursor if isinstance(row, Mapping)]


def _invite_cursor_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def list_accepted_invites_for_user_page(
    *,
    user_concept_id: str,
    page_size: int = 100,
    position: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a stable bounded keyset page of accepted incoming invites."""

    actor = user_concept_id.strip() if isinstance(user_concept_id, str) else ""
    if not actor:
        raise ValueError("user_concept_id is required.")
    if not isinstance(page_size, int) or isinstance(page_size, bool):
        raise ValueError("page_size must be an integer.")
    safe_page_size = max(1, min(page_size, 100))
    coll = _get_collection()
    if coll is None:
        return {
            "available": False,
            "invites": [],
            "count": 0,
            "has_more": False,
            "next_position": None,
        }
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "invitee_user_id": actor,
                "status": "accepted",
            }
        },
        {
            "$set": {
                "_conversation_page_timestamp": {
                    "$ifNull": ["$updated_at", {"$ifNull": ["$created_at", epoch]}]
                },
                "_conversation_page_invite_id": {
                    "$convert": {
                        "input": {"$ifNull": ["$invite_id", "$_id"]},
                        "to": "string",
                        "onError": "",
                        "onNull": "",
                    }
                },
            }
        },
    ]
    if position is not None:
        if not isinstance(position, Mapping):
            raise ValueError("invite page position is invalid.")
        marker_timestamp = _invite_cursor_datetime(position.get("timestamp"))
        marker_invite_id = position.get("invite_id")
        if marker_timestamp is None or not isinstance(marker_invite_id, str):
            raise ValueError("invite page position is incomplete.")
        pipeline.append(
            {
                "$match": {
                    "$or": [
                        {
                            "_conversation_page_timestamp": {
                                "$lt": marker_timestamp
                            }
                        },
                        {
                            "_conversation_page_timestamp": marker_timestamp,
                            "_conversation_page_invite_id": {"$lt": marker_invite_id},
                        },
                    ]
                }
            }
        )
    pipeline.extend(
        [
            {
                "$sort": {
                    "_conversation_page_timestamp": DESCENDING,
                    "_conversation_page_invite_id": DESCENDING,
                }
            },
            {"$limit": safe_page_size + 1},
            {"$project": {"_id": 0}},
        ]
    )
    rows = [dict(row) for row in coll.aggregate(pipeline) if isinstance(row, Mapping)]
    has_more = len(rows) > safe_page_size
    page = rows[:safe_page_size]
    next_position = None
    if page:
        final = page[-1]
        final_timestamp = _invite_cursor_datetime(
            final.get("_conversation_page_timestamp")
        )
        if final_timestamp is None:
            final_timestamp = epoch
        next_position = {
            "timestamp": final_timestamp.isoformat(),
            "invite_id": str(final.get("_conversation_page_invite_id") or ""),
        }
    for invite in page:
        invite.pop("_conversation_page_timestamp", None)
        invite.pop("_conversation_page_invite_id", None)
    return {
        "available": True,
        "invites": page,
        "count": len(page),
        "has_more": has_more,
        "next_position": next_position,
    }


def list_outgoing_accepted_invites_for_user(
    *, user_concept_id: str
) -> List[Dict[str, Any]]:
    """Return accepted invites where the user is the inviter (owner).

    This is used to identify sessions that the owner has shared with others,
    so the owner can subscribe to SSE updates from participants.
    """
    return list_invites_for_user(
        user_concept_id=user_concept_id,
        status="accepted",
        direction="outgoing",
    )


def get_invite_status_map(*, session_id: str, invitee_ids: List[str]) -> Dict[str, str]:
    coll = _get_collection()
    if coll is None:
        return {}

    if not invitee_ids:
        return {}

    cursor = coll.find(
        {
            "session_id": session_id,
            "invitee_user_id": {"$in": invitee_ids},
        },
        {"_id": 0, "invitee_user_id": 1, "status": 1},
    )

    status_map: Dict[str, str] = {}
    for doc in cursor:
        if not isinstance(doc, dict):
            continue
        invitee = doc.get("invitee_user_id")
        status = doc.get("status")
        if isinstance(invitee, str) and isinstance(status, str):
            status_map[invitee] = status
    return status_map


def respond_to_invite(
    *, invite_id: str, user_concept_id: str, action: str
) -> Optional[Dict[str, Any]]:
    coll = _get_collection()
    if coll is None:
        return None

    action = (action or "").strip().lower()
    if action not in {"accept", "decline"}:
        raise ValueError("action must be 'accept' or 'decline'")

    now = _utcnow()
    update: Dict[str, Any] = {
        "status": "accepted" if action == "accept" else "declined",
        "updated_at": now,
    }
    if action == "accept":
        update["accepted_at"] = now
    else:
        update["declined_at"] = now

    match = {
        "invite_id": invite_id,
        "invitee_user_id": user_concept_id,
        "status": "pending",
    }
    result = coll.update_one(match, {"$set": update})
    if getattr(result, "matched_count", 0) <= 0:
        return None
    updated = coll.find_one(
        {"invite_id": invite_id, "invitee_user_id": user_concept_id},
        {"_id": 0},
    )
    return updated if isinstance(updated, dict) else None


def revoke_invites_for_session(
    *,
    session_id: str,
    exclude_user_ids: Optional[List[str]] = None,
    reason: str = "conversation_moved",
) -> Dict[str, Any]:
    """Revoke pending and accepted invites for a session.

    Used when moving a conversation to a new organisation to invalidate
    invites for users who are not members of the target organisation.

    Args:
        session_id: The conversation session ID
        exclude_user_ids: Users to keep invites for (e.g., members of new org)
        reason: Reason for revocation (stored in invite document)

    Returns:
        Dictionary with revoked count and affected invitee IDs
    """
    coll = _get_collection()
    if coll is None:
        return {"revoked_count": 0, "invitee_ids": [], "error": "Database unavailable"}

    if not isinstance(session_id, str) or not session_id.strip():
        return {"revoked_count": 0, "invitee_ids": [], "error": "session_id required"}

    session_id = session_id.strip()
    exclude_set = set()
    if exclude_user_ids:
        for uid in exclude_user_ids:
            if isinstance(uid, str) and uid.strip():
                exclude_set.add(uid.strip())

    now = _utcnow()

    # Find active invites (pending or accepted) for this session
    query: Dict[str, Any] = {
        "session_id": session_id,
        "status": {"$in": ["pending", "accepted"]},
    }
    if exclude_set:
        query["invitee_user_id"] = {"$nin": list(exclude_set)}

    # Get the invitees being revoked before updating
    cursor = coll.find(query, {"_id": 0, "invitee_user_id": 1, "invite_id": 1})
    affected = [(doc.get("invitee_user_id"), doc.get("invite_id")) for doc in cursor]
    invitee_ids = [uid for uid, _ in affected if uid]
    invite_ids = [iid for _, iid in affected if iid]

    if not invite_ids:
        return {"revoked_count": 0, "invitee_ids": []}

    result = coll.update_many(
        {"invite_id": {"$in": invite_ids}},
        {
            "$set": {
                "status": "revoked",
                "revoked_at": now,
                "revoke_reason": reason,
                "updated_at": now,
            }
        },
    )

    return {
        "revoked_count": getattr(result, "modified_count", 0),
        "invitee_ids": invitee_ids,
        "invite_ids": invite_ids,
    }


def list_active_invites_for_session(*, session_id: str) -> List[Dict[str, Any]]:
    """List all pending or accepted invites for a session.

    Used to check what shared access exists before moving a conversation.

    Args:
        session_id: The conversation session ID

    Returns:
        List of invite documents (excluding _id)
    """
    coll = _get_collection()
    if coll is None:
        return []

    if not isinstance(session_id, str) or not session_id.strip():
        return []

    cursor = coll.find(
        {
            "session_id": session_id.strip(),
            "status": {"$in": ["pending", "accepted"]},
        },
        {"_id": 0},
    ).sort("updated_at", DESCENDING)

    return [doc for doc in cursor if isinstance(doc, dict)]
