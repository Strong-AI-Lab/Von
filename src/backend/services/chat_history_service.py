"""Chat history service for persistent conversation storage."""

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Iterable, Tuple
from pymongo.errors import PyMongoError
from ..db.mongo_client import get_db
from ..models.chat_history_model import chat_history_collection_name

# Try to import RAG service, but don't fail if it's not available (circular imports etc)
try:
    from .rag_service import get_rag_service
except ImportError:
    get_rag_service = None

logger = logging.getLogger(__name__)


def get_session_context() -> Dict[str, Any]:
    """
    Get organisation and role context from Flask session.
    Returns dict with org_id and role keys (may be None if not in context).
    """
    try:
        from flask import session as flask_session

        return {
            "org_id": flask_session.get("org_id"),
            "role_in_org": flask_session.get("role_in_org"),
            "namespace": flask_session.get("namespace"),
        }
    except (ImportError, RuntimeError):
        # Not in Flask context or session not available
        return {"org_id": None, "role_in_org": None}


class ChatHistoryServiceError(Exception):
    """Exception raised for chat history service errors."""

    pass


def get_chat_history_collection_service():
    """Get the chat_history collection."""
    db = get_db()
    if db is None:
        return None
    return db[chat_history_collection_name]


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        # Normalise to tz-aware
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return None
        # Support common ISO strings ending with Z
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(txt)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _is_reset_marker(entry: Dict[str, Any]) -> bool:
    return entry.get("role") == "system" and entry.get("content") == "__RESET__"


def _iter_non_reset_messages(
    history: Iterable[Dict[str, Any]],
) -> Iterable[Dict[str, Any]]:
    for msg in history:
        if isinstance(msg, dict) and not _is_reset_marker(msg):
            yield msg


def _infer_last_message_timestamp(doc: Dict[str, Any]) -> Optional[datetime]:
    history = doc.get("history") or []
    last_ts: Optional[datetime] = None
    for msg in _iter_non_reset_messages(history):
        ts = _coerce_datetime(msg.get("timestamp"))
        if ts is None:
            continue
        if last_ts is None or ts > last_ts:
            last_ts = ts
    if last_ts is not None:
        return last_ts
    return _coerce_datetime(doc.get("updated_at")) or _coerce_datetime(
        doc.get("created_at")
    )


def _infer_created_timestamp(doc: Dict[str, Any]) -> Optional[datetime]:
    return _coerce_datetime(doc.get("created_at")) or _coerce_datetime(
        doc.get("updated_at")
    )


def _split_history_into_segments(
    history: List[Dict[str, Any]],
) -> List[List[Dict[str, Any]]]:
    segments: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        if _is_reset_marker(entry):
            if current:
                segments.append(current)
            current = []
            continue
        current.append(entry)
    if current:
        segments.append(current)
    return segments


def get_chat_history(user_id: str, session_id: str) -> List[Dict[str, Any]]:
    """
    Retrieves the chat history for a specific user and session.

    Args:
        user_id: The user's concept ID (e.g., "#V#michael_witbrock")
        session_id: The session UUID

    Returns:
        List of message dictionaries with role and content
    """
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        doc = chat_history_coll.find_one({"user_id": user_id, "session_id": session_id})

        if doc:
            return doc.get("history", [])
        return []
    except PyMongoError as e:
        logger.error(f"Error retrieving chat history: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not retrieve chat history: {e}") from e


def get_chat_history_segments(
    user_id: str, session_id: str
) -> List[List[Dict[str, Any]]]:
    """
    Return chat history split into segments separated by reset markers.
    Retrieves history from ALL sessions for the user, sorted chronologically.
    """
    if not user_id:
        raise ChatHistoryServiceError("user_id is required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        docs = list(chat_history_coll.find({"user_id": user_id}))

        # Sort sessions by inferred last-message timestamp so “recent” really means recent.
        def _sort_key(d: Dict[str, Any]) -> Tuple[int, datetime]:
            ts = _infer_last_message_timestamp(d)
            if ts is None:
                # Stable, but always first
                return (0, datetime(1970, 1, 1, tzinfo=timezone.utc))
            return (1, ts)

        docs.sort(key=_sort_key)

        all_segments: List[List[Dict[str, Any]]] = []
        for doc in docs:
            history = doc.get("history") or []
            if not isinstance(history, list) or not history:
                continue
            all_segments.extend(_split_history_into_segments(history))

        return all_segments
    except PyMongoError as e:
        logger.error(f"Error retrieving segmented chat history: {e}", exc_info=True)
        raise ChatHistoryServiceError(
            f"Could not retrieve segmented chat history: {e}"
        ) from e


def add_message_to_history(
    user_id: str,
    session_id: str,
    message: Dict[str, Any],
    llm_debug_data: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Adds a message to the chat history for a specific user and session.

    Args:
        user_id: The user's concept ID
        session_id: The session UUID
        message: Message dictionary with 'role' and 'content' keys
        llm_debug_data: Optional LLM debug information (model, context stats, tool usage, etc.)
    """
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    if not message or "role" not in message or "content" not in message:
        raise ChatHistoryServiceError("Message must contain 'role' and 'content' keys.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        # Get session context for organisation/role/namespace (best effort).
        session_context = get_session_context()

        # Determine namespace used for both persistence and RAG indexing.
        # Prefer an explicit session namespace, else derive one from user/org when possible.
        ns = session_context.get("namespace")
        if not isinstance(ns, str) or not ns.strip():
            org_id = session_context.get("org_id")
            if isinstance(org_id, str) and org_id.strip():
                try:
                    from src.backend.services.namespace_service import derive_namespace

                    user_slug = user_id[3:] if user_id.startswith("#V#") else user_id
                    org_slug = org_id[3:] if org_id.startswith("#V#") else org_id
                    ns = derive_namespace(user_slug, org_slug)
                except Exception:
                    ns = None

        # Add timestamp to message (and llm_debug_data if present)
        message_with_timestamp = {**message, "timestamp": datetime.now(timezone.utc)}
        if llm_debug_data:
            message_with_timestamp["llm_debug_data"] = llm_debug_data

        set_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        if isinstance(ns, str) and ns.strip():
            set_fields["namespace"] = ns.strip()

        # Update or insert the session document
        result = chat_history_coll.update_one(
            {"user_id": user_id, "session_id": session_id},
            {
                "$push": {"history": message_with_timestamp},
                "$set": set_fields,
                "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
            },
            upsert=True,
        )

        logger.debug(
            f"Added message to history for user {user_id}, session {session_id}"
        )

        # Index to RAG (Best effort)
        if get_rag_service:
            try:
                rag = get_rag_service()
                content = message.get("content", "")
                # Only index string content that isn't empty
                if isinstance(content, str) and content.strip():
                    # Skip indexing tool outputs that are just "truncated" markers or very small
                    if len(content) > 5000:  # Truncate for indexing if huge
                        content = content[:5000]

                    # Get session context for organisation and role
                    doc_id = str(uuid.uuid4())
                    metadata = {
                        "user_id": user_id,
                        "session_id": session_id,
                        "role": message.get("role", "unknown"),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "chat_message",
                    }

                    # Include organisation and role metadata if available
                    if session_context.get("org_id"):
                        metadata["organisation_concept_id"] = session_context["org_id"]
                    if session_context.get("role_in_org"):
                        metadata["role_in_org"] = session_context["role_in_org"]

                    doc = {"id": doc_id, "text": content, "metadata": metadata}
                    if not isinstance(ns, str) or not ns.strip():
                        ns = "chat_history"
                    rag.upsert_documents([doc], namespace=ns)
            except Exception as e:
                # Log but don't fail the chat request
                logger.warning(f"Failed to index chat message to RAG: {e}")

    except PyMongoError as e:
        logger.error(f"Error adding message to history: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not add message to history: {e}") from e


def add_reset_marker_to_history(user_id: str, session_id: str) -> None:
    """
    Adds a reset marker to the chat history instead of deleting it.
    This preserves the full conversation history while allowing the UI to display only post-reset messages.

    Args:
        user_id: The user's concept ID
        session_id: The session UUID
    """
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        reset_marker = {
            "role": "system",
            "content": "__RESET__",
            "timestamp": datetime.now(timezone.utc),
        }

        result = chat_history_coll.update_one(
            {"user_id": user_id, "session_id": session_id},
            {
                "$push": {"history": reset_marker},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )

        if result.matched_count > 0:
            logger.info(f"Added reset marker for user {user_id}, session {session_id}")
        else:
            logger.warning(
                f"No session found to add reset marker for user {user_id}, session {session_id}"
            )
    except PyMongoError as e:
        logger.error(f"Error adding reset marker: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not add reset marker: {e}") from e


def delete_chat_history(user_id: str, session_id: str) -> None:
    """
    Deletes the chat history for a specific user and session.
    Note: This is kept for backward compatibility but add_reset_marker_to_history() is preferred.

    Args:
        user_id: The user's concept ID
        session_id: The session UUID
    """
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        result = chat_history_coll.delete_one(
            {"user_id": user_id, "session_id": session_id}
        )

        if result.deleted_count > 0:
            logger.info(
                f"Deleted chat history for user {user_id}, session {session_id}"
            )
        else:
            logger.warning(
                f"No history found to delete for user {user_id}, session {session_id}"
            )
    except PyMongoError as e:
        logger.error(f"Error deleting chat history: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not delete chat history: {e}") from e


def get_chat_history_length(user_id: str) -> int:
    """
    Retrieves the total number of chat turns across ALL sessions for a user.
    Used to display "History: NNN" in the footer.

    Args:
        user_id: The user's concept ID

    Returns:
        Total count of messages across all sessions
    """
    if not user_id:
        raise ChatHistoryServiceError("user_id is required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        total_turns = 0
        for doc in chat_history_coll.find({"user_id": user_id}):
            history = doc.get("history", [])
            if not isinstance(history, list):
                continue
            total_turns += sum(1 for _ in _iter_non_reset_messages(history))
        return total_turns
    except PyMongoError as e:
        logger.error(f"Error retrieving chat history length: {e}", exc_info=True)
        raise ChatHistoryServiceError(
            f"Could not retrieve chat history length: {e}"
        ) from e


def get_chat_history_session_count(user_id: str) -> int:
    """Return number of sessions for a user that contain any non-reset messages."""
    if not user_id:
        raise ChatHistoryServiceError("user_id is required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        count = 0
        for doc in chat_history_coll.find({"user_id": user_id}, {"history": 1}):
            history = doc.get("history") or []
            if not isinstance(history, list) or not history:
                continue
            if any(True for _ in _iter_non_reset_messages(history)):
                count += 1
        return count
    except PyMongoError as e:
        logger.error(f"Error retrieving chat history session count: {e}", exc_info=True)
        raise ChatHistoryServiceError(
            f"Could not retrieve chat history session count: {e}"
        ) from e


def get_chat_history_session_summaries(
    user_id: str, limit: int = 50
) -> List[Dict[str, Any]]:
    """Return per-session summaries ordered by inferred last message timestamp desc."""
    if not user_id:
        raise ChatHistoryServiceError("user_id is required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    safe_limit = 50
    if isinstance(limit, int) and limit > 0:
        safe_limit = min(limit, 500)

    try:
        docs = list(
            chat_history_coll.find(
                {"user_id": user_id},
                {
                    "session_id": 1,
                    "history": 1,
                    "created_at": 1,
                    "updated_at": 1,
                    "namespace": 1,
                },
            )
        )

        summaries: List[Dict[str, Any]] = []
        for doc in docs:
            session_id = doc.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue

            history = doc.get("history") or []
            if not isinstance(history, list) or not history:
                continue

            non_reset = list(_iter_non_reset_messages(history))
            if not non_reset:
                continue

            last_ts = _infer_last_message_timestamp(doc)
            created_ts = _infer_created_timestamp(doc)

            last_user_msg = None
            for msg in reversed(non_reset):
                if msg.get("role") == "user":
                    content = msg.get("content")
                    if isinstance(content, str) and content.strip():
                        last_user_msg = content.strip()
                        break

            preview = last_user_msg
            if isinstance(preview, str) and len(preview) > 140:
                preview = preview[:140] + "…"

            summaries.append(
                {
                    "session_id": session_id,
                    "message_count": len(non_reset),
                    "last_message_at": last_ts.isoformat() if last_ts else None,
                    "created_at": created_ts.isoformat() if created_ts else None,
                    "namespace": doc.get("namespace"),
                    "preview": preview,
                }
            )

        summaries.sort(
            key=lambda s: _coerce_datetime(s.get("last_message_at"))
            or datetime(1970, 1, 1, tzinfo=timezone.utc),
            reverse=True,
        )
        return summaries[:safe_limit]
    except PyMongoError as e:
        logger.error(
            f"Error retrieving chat history session summaries: {e}", exc_info=True
        )
        raise ChatHistoryServiceError(
            f"Could not retrieve chat history session summaries: {e}"
        ) from e


def backfill_chat_history_for_user(
    *,
    user_concept_id: str,
    target_namespace: str,
    organisation_concept_id: Optional[str] = None,
    role_in_org: Optional[str] = None,
    max_sessions: int = 10,
    max_messages: int = 500,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Backfill missing namespaces on legacy chat history docs and index messages to RAG.

    Safety properties:
    - Only updates docs where `namespace` is missing/None/blank.
    - Does NOT touch `updated_at` (avoids breaking recency ordering).
    - Reindex uses deterministic IDs (idempotent).
    """

    if not isinstance(user_concept_id, str) or not user_concept_id:
        raise ChatHistoryServiceError("user_concept_id is required")
    if not isinstance(target_namespace, str) or not target_namespace:
        raise ChatHistoryServiceError("target_namespace is required")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    safe_max_sessions = 10
    if isinstance(max_sessions, int) and max_sessions > 0:
        safe_max_sessions = min(max_sessions, 500)

    safe_max_messages = 500
    if isinstance(max_messages, int) and max_messages > 0:
        safe_max_messages = min(max_messages, 100000)

    rag = None
    if get_rag_service:
        try:
            rag = get_rag_service()
        except Exception:
            rag = None

    query = {
        "user_id": user_concept_id,
        "$or": [
            {"namespace": {"$exists": False}},
            {"namespace": {"$eq": None}},
            {"namespace": {"$in": ["", " "]}},
        ],
    }

    sessions_examined = 0
    sessions_updated = 0
    messages_indexed_attempted = 0
    messages_indexed_success = 0
    messages_indexed_failed = 0
    errors: List[Dict[str, Any]] = []

    # Stable namespace UUID for deterministic doc ids.
    deterministic_namespace_uuid = uuid.UUID("8c5a7fa9-9a7c-4f0f-8c1f-f4ad7f9f6fd7")

    try:
        cursor = chat_history_coll.find(query)

        for doc in cursor:
            if sessions_examined >= safe_max_sessions:
                break
            sessions_examined += 1

            session_id = doc.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue

            set_fields: Dict[str, Any] = {"namespace": target_namespace}
            if organisation_concept_id and not doc.get("organisation_concept_id"):
                set_fields["organisation_concept_id"] = organisation_concept_id
            if role_in_org and not doc.get("role_in_org"):
                set_fields["role_in_org"] = role_in_org

            if not dry_run:
                try:
                    chat_history_coll.update_one(
                        {"_id": doc["_id"]}, {"$set": set_fields}
                    )
                    sessions_updated += 1
                except Exception as e:
                    errors.append(
                        {
                            "type": "update_failed",
                            "session_id": session_id,
                            "error": str(e),
                        }
                    )
                    # Continue to next session
                    continue
            else:
                sessions_updated += 1

            # Best-effort reindex
            history = doc.get("history") or []
            if not isinstance(history, list) or not history:
                continue

            for idx, msg in enumerate(history):
                if messages_indexed_attempted >= safe_max_messages:
                    break
                if not isinstance(msg, dict) or _is_reset_marker(msg):
                    continue

                content = msg.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue

                messages_indexed_attempted += 1

                if rag is None or dry_run:
                    messages_indexed_success += 1
                    continue

                try:
                    ts = _coerce_datetime(msg.get("timestamp"))
                    doc_id = str(
                        uuid.uuid5(
                            deterministic_namespace_uuid,
                            f"{target_namespace}|{user_concept_id}|{session_id}|{idx}",
                        )
                    )
                    text = content.strip()
                    if len(text) > 5000:
                        text = text[:5000]

                    metadata = {
                        "type": "chat_message",
                        "user_id": user_concept_id,
                        "session_id": session_id,
                        "role": msg.get("role", "unknown"),
                        "timestamp": ts.isoformat() if ts else None,
                        "organisation_concept_id": organisation_concept_id,
                        "role_in_org": role_in_org,
                    }
                    rag.upsert_documents(
                        [{"id": doc_id, "text": text, "metadata": metadata}],
                        namespace=target_namespace,
                    )
                    messages_indexed_success += 1
                except Exception as e:
                    messages_indexed_failed += 1
                    errors.append(
                        {
                            "type": "index_failed",
                            "session_id": session_id,
                            "message_index": idx,
                            "error": str(e),
                        }
                    )

        return {
            "status": "ok",
            "user_concept_id": user_concept_id,
            "target_namespace": target_namespace,
            "dry_run": bool(dry_run),
            "sessions_examined": sessions_examined,
            "sessions_updated": sessions_updated,
            "messages_indexed_attempted": messages_indexed_attempted,
            "messages_indexed_success": messages_indexed_success,
            "messages_indexed_failed": messages_indexed_failed,
            "errors": errors,
        }
    except PyMongoError as e:
        logger.error(f"Error during chat history backfill: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Backfill failed: {e}") from e
