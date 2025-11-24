"""Chat history service for persistent conversation storage."""

import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pymongo.errors import PyMongoError
from ..db.mongo_client import get_db
from ..models.chat_history_model import chat_history_collection_name

logger = logging.getLogger(__name__)


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
        doc = chat_history_coll.find_one({
            "user_id": user_id,
            "session_id": session_id
        })

        if doc:
            return doc.get("history", [])
        return []
    except PyMongoError as e:
        logger.error(f"Error retrieving chat history: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not retrieve chat history: {e}") from e


def get_chat_history_segments(user_id: str, session_id: str) -> List[List[Dict[str, Any]]]:
    """Return chat history split into segments separated by reset markers."""
    if not user_id or not session_id:
        raise ChatHistoryServiceError("user_id and session_id are required.")

    chat_history_coll = get_chat_history_collection_service()
    if chat_history_coll is None:
        raise ChatHistoryServiceError("Could not connect to chat history collection.")

    try:
        doc = chat_history_coll.find_one({
            "user_id": user_id,
            "session_id": session_id
        })

        if not doc or "history" not in doc:
            return []

        history = doc.get("history", [])
        if not history:
            return []

        segments: List[List[Dict[str, Any]]] = []
        current_segment: List[Dict[str, Any]] = []

        for entry in history:
            if entry.get("role") == "system" and entry.get("content") == "__RESET__":
                segments.append(current_segment)
                current_segment = []
                continue
            current_segment.append(entry)

        # Always append the tail segment (even if empty) to record fresh resets
        segments.append(current_segment)
        return segments
    except PyMongoError as e:
        logger.error(f"Error retrieving segmented chat history: {e}", exc_info=True)
        raise ChatHistoryServiceError(f"Could not retrieve segmented chat history: {e}") from e


def add_message_to_history(user_id: str, session_id: str, message: Dict[str, Any]) -> None:
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
        message_with_timestamp = {
            **message,
            "timestamp": datetime.now(timezone.utc)
        }

        # Update or insert the session document
        result = chat_history_coll.update_one(
            {
                "user_id": user_id,
                "session_id": session_id
            },
            {
                "$push": {"history": message_with_timestamp},
                "$set": {"updated_at": datetime.now(timezone.utc)},
                "$setOnInsert": {"created_at": datetime.now(timezone.utc)}
            },
            upsert=True
        )

        logger.debug(f"Added message to history for user {user_id}, session {session_id}")
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
            "timestamp": datetime.now(timezone.utc)
        }

        result = chat_history_coll.update_one(
            {
                "user_id": user_id,
                "session_id": session_id
            },
            {
                "$push": {"history": reset_marker},
                "$set": {"updated_at": datetime.now(timezone.utc)}
            }
        )

        if result.matched_count > 0:
            logger.info(f"Added reset marker for user {user_id}, session {session_id}")
        else:
            logger.warning(f"No session found to add reset marker for user {user_id}, session {session_id}")
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
        result = chat_history_coll.delete_one({
            "user_id": user_id,
            "session_id": session_id
        })

        if result.deleted_count > 0:
            logger.info(f"Deleted chat history for user {user_id}, session {session_id}")
        else:
            logger.warning(f"No history found to delete for user {user_id}, session {session_id}")
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
        raise ChatHistoryServiceError(f"Could not retrieve chat history length: {e}") from e
