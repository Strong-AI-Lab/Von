"""Shared conversation invitation storage and retrieval."""

from __future__ import annotations

import logging
import threading
import uuid
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


def list_accepted_invites_for_user(*, user_concept_id: str) -> List[Dict[str, Any]]:
    return list_invites_for_user(
        user_concept_id=user_concept_id,
        status="accepted",
        direction="incoming",
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
