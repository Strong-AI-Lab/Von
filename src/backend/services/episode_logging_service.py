"""Episode logging for interuser communication and shared conversation events."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..db.mongo_client import get_db

logger = logging.getLogger(__name__)

EPISODE_LOG_COLLECTION_NAME = "episode_logs"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_collection():
    db = get_db()
    if db is None:
        return None
    return db[EPISODE_LOG_COLLECTION_NAME]


def log_episode(
    *,
    episode_type: str,
    actor_user_id: Optional[str],
    organisation_concept_id: Optional[str] = None,
    session_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
    related_invite_id: Optional[str] = None,
) -> Optional[str]:
    """Record an episode event for future learning and improvement."""

    if not isinstance(episode_type, str) or not episode_type.strip():
        return None

    coll = _get_collection()
    if coll is None:
        logger.warning("[episode_log] Mongo unavailable; episode not stored")
        return None

    episode_id = str(uuid.uuid4())
    doc: Dict[str, Any] = {
        "episode_id": episode_id,
        "episode_type": episode_type.strip(),
        "actor_user_id": actor_user_id,
        "organisation_concept_id": organisation_concept_id,
        "session_id": session_id,
        "status": status,
        "payload": payload or {},
        "created_at": _utcnow(),
    }
    if related_invite_id:
        doc["related_invite_id"] = related_invite_id

    try:
        coll.insert_one(doc)
        return episode_id
    except Exception as exc:  # pragma: no cover
        logger.warning("[episode_log] Failed to store episode: %s", exc)
        return None
