"""Chat history service for persistent conversation storage."""

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
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
        # Fetch all sessions for the user, sorted by last update time
        # This ensures that recently active sessions appear at the end of the history
        cursor = chat_history_coll.find({"user_id": user_id}).sort("updated_at", 1)

        all_segments: List[List[Dict[str, Any]]] = []
        found_any_history = False

        for doc in cursor:
            history = doc.get("history", [])
            if not history:
                continue

            found_any_history = True
            current_session_segments: List[List[Dict[str, Any]]] = []
            current_segment: List[Dict[str, Any]] = []

            for entry in history:
                if (
                    entry.get("role") == "system"
                    and entry.get("content") == "__RESET__"
                ):
                    current_session_segments.append(current_segment)
                    current_segment = []
                    continue
                current_segment.append(entry)

            # Always append the tail segment of the session
            current_session_segments.append(current_segment)
            all_segments.extend(current_session_segments)

        # If no history found at all, return empty list
        if not found_any_history:
            return []

        return all_segments
    except PyMongoError as e:
        logger.error(f"Error retrieving segmented chat history: {e}", exc_info=True)
        raise ChatHistoryServiceError(
            f"Could not retrieve segmented chat history: {e}"
        ) from e


def add_message_to_history(
    user_id: str, session_id: str, message: Dict[str, Any]
) -> None:
    """
    Adds a message to the chat history for a specific user and session.

    Args:
        user_id: The user's concept ID
        session_id: The session UUID
        message: Message dictionary with 'role' and 'content' keys
    """
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    if not message or "role" not in message or "content" not in message:
        raise ChatHistoryServiceError("Message must contain 'role' and 'content' keys.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        # Add timestamp to message
        message_with_timestamp = {**message, "timestamp": datetime.now(timezone.utc)}

        # Update or insert the session document
        result = chat_history_coll.update_one(
            {"user_id": user_id, "session_id": session_id},
            {
                "$push": {"history": message_with_timestamp},
                "$set": {"updated_at": datetime.now(timezone.utc)},
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
                    session_context = get_session_context()

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
                    # Use a specific namespace for chat history to allow filtered queries later
                    rag.upsert_documents([doc], namespace="chat_history")
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
            total_turns += len(doc.get("history", []))
        return total_turns
    except PyMongoError as e:
        logger.error(f"Error retrieving chat history length: {e}", exc_info=True)
        raise ChatHistoryServiceError(
            f"Could not retrieve chat history length: {e}"
        ) from e
