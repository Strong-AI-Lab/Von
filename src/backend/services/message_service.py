"""Inter-user messaging service for Von.

Manages direct messages between users (human and agent) as Vontology concepts.
Messages are stored with visibility scoping via specific_to_user (sender + recipients).

JVNAUTOSCI-1071
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..db.mongo_client import get_concepts_collection
from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.text_value_service import upsert_text_for_concept
from .workflow_event_integration_service import (
    maybe_launch_direct_message_workflow,
)
from ..security.access_control import (
    apply_concept_query_filter,
)
from ..security.visibility_predicates import (
    set_specific_to_org_values,
    set_specific_to_user_values,
)

_log = logging.getLogger(__name__)

# Vontology type for messages
MESSAGE_TYPE_CONCEPT_ID = "#V#direct_message"

# Predicates for message relationships
PREDICATE_SENDER = "#V#has_sender"
PREDICATE_RECIPIENT = "#V#has_recipient"
PREDICATE_THREAD = "#V#is_part_of_thread"  # For conversation threading
PREDICATE_REPLY_TO = "#V#is_reply_to"  # For reply chains

# Message statuses
MESSAGE_STATUS_SENT = "sent"
MESSAGE_STATUS_DELIVERED = "delivered"
MESSAGE_STATUS_READ = "read"


def _generate_message_concept_id(sender_id: str, timestamp: datetime) -> str:
    """Generate a unique concept_id for a message."""
    # Extract user slug from sender_id (e.g., #V#michael_witbrock -> michael_witbrock)
    sender_slug = sender_id.replace("#V#", "").replace(" ", "_").lower()
    ts_str = timestamp.strftime("%Y%m%d_%H%M%S_%f")
    return f"#V#message_{sender_slug}_{ts_str}"


def create_message(
    sender_id: str,
    recipient_ids: List[str],
    content: str,
    subject: Optional[str] = None,
    thread_id: Optional[str] = None,
    reply_to_id: Optional[str] = None,
    org_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a new direct message.

    Args:
        sender_id: Concept ID of the sender (user or agent).
        recipient_ids: List of recipient concept IDs.
        content: Message body text.
        subject: Optional subject line.
        thread_id: Optional thread concept ID for grouping.
        reply_to_id: Optional message ID this is replying to.
        org_id: Optional organisation context for org-scoped messages.
        metadata: Optional additional metadata.

    Returns:
        The created message document.
    """
    if not sender_id:
        raise ValueError("sender_id is required")
    if not recipient_ids:
        raise ValueError("At least one recipient is required")
    if not content or not content.strip():
        raise ValueError("Message content cannot be empty")

    # Normalise IDs
    sender_id = sender_id.strip()
    recipient_ids = [rid.strip() for rid in recipient_ids if rid and rid.strip()]

    if not recipient_ids:
        raise ValueError("At least one valid recipient is required")

    timestamp = datetime.now(timezone.utc)
    concept_id = _generate_message_concept_id(sender_id, timestamp)

    # Build relationships
    relationships: Dict[str, Any] = {
        "is_an_instance_of": [MESSAGE_TYPE_CONCEPT_ID],
        PREDICATE_SENDER: [sender_id],
        PREDICATE_RECIPIENT: recipient_ids,
    }
    # Visibility: both sender and all recipients can see the message.
    relationships = set_specific_to_user_values(
        relationships,
        [sender_id] + recipient_ids,
    )

    # Optional thread/reply relationships
    if thread_id:
        relationships[PREDICATE_THREAD] = [thread_id.strip()]
    if reply_to_id:
        relationships[PREDICATE_REPLY_TO] = [reply_to_id.strip()]

    # Organisation scoping if provided
    if org_id:
        relationships = set_specific_to_org_values(relationships, [org_id.strip()])

    metadata_payload = metadata if isinstance(metadata, dict) else {}
    metadata_payload = {str(k): v for k, v in metadata_payload.items() if isinstance(k, str)}
    attribution = metadata_payload.get("attribution")
    if not isinstance(attribution, str) or not attribution.strip():
        attribution = f"Sent by Von on behalf of {sender_id}"
    else:
        attribution = attribution.strip()

    # Build concept document
    concept_doc: Dict[str, Any] = {
        "concept_id": concept_id,
        "direct_concept_name": f"Message from {sender_id}",
        "relationships": relationships,
        "concept_data": {
            "message_status": MESSAGE_STATUS_SENT,
            "sent_at": timestamp.isoformat(),
            "read_by": [],  # Track which recipients have read it
            # Keep a canonical copy for UI/search paths that do not resolve text relations.
            "content_fallback": content,
            "attribution": attribution,
        },
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    # Add optional subject
    if subject:
        concept_doc["concept_data"]["subject"] = subject.strip()

    # Add custom metadata
    if metadata_payload:
        concept_doc["concept_data"]["metadata"] = metadata_payload

    # Insert into MongoDB
    coll = get_concepts_collection()
    if coll is None:
        raise RuntimeError("Database not available")

    ConceptsRepository.insert_one(concept_doc)

    # Store content as text relation (hasDescription)
    try:
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text=content,
            lang="en-NZ",
        )
    except Exception as e:
        _log.warning(f"Failed to store message content as text relation: {e}")

    # Event-driven workflow launch is best-effort and must not block messaging.
    try:
        workflow_event_launch = maybe_launch_direct_message_workflow(
            message_concept_id=concept_id,
            sender_id=sender_id,
            recipient_ids=recipient_ids,
            org_id=org_id,
        )
        if isinstance(workflow_event_launch, dict):
            concept_doc["workflow_event_launch"] = workflow_event_launch
            if not bool(workflow_event_launch.get("triggered")):
                _log.info(
                    "Direct-message workflow not triggered for %s: reason=%s hint=%s",
                    concept_id,
                    workflow_event_launch.get("reason"),
                    workflow_event_launch.get("hint"),
                )
    except Exception as e:
        _log.warning("Direct-message workflow launch skipped for %s: %s", concept_id, e)

    _log.info(f"Created message {concept_id} from {sender_id} to {recipient_ids}")

    return concept_doc


def get_message(message_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a message by its concept_id.

    Access control is applied automatically.
    """
    if not message_id:
        return None

    coll = get_concepts_collection()
    if coll is None:
        return None

    base_filter = {"concept_id": message_id.strip()}
    query = apply_concept_query_filter(base_filter)

    return coll.find_one(query)


def get_messages_for_user(
    user_id: str,
    include_sent: bool = True,
    include_received: bool = True,
    thread_id: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
) -> List[Dict[str, Any]]:
    """Get messages for a user (sent and/or received).

    Args:
        user_id: The user's concept ID.
        include_sent: Include messages sent by this user.
        include_received: Include messages received by this user.
        thread_id: Filter to a specific thread.
        limit: Maximum number of messages to return.
        skip: Number of messages to skip (for pagination).

    Returns:
        List of message documents, newest first.
    """
    if not user_id:
        return []

    user_id = user_id.strip()
    coll = get_concepts_collection()
    if coll is None:
        return []

    # Build query conditions
    conditions = []
    if include_sent:
        conditions.append({f"relationships.{PREDICATE_SENDER}": user_id})
    if include_received:
        conditions.append({f"relationships.{PREDICATE_RECIPIENT}": user_id})

    if not conditions:
        return []

    base_filter: Dict[str, Any] = {
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        "$or": conditions,
    }

    if thread_id:
        base_filter[f"relationships.{PREDICATE_THREAD}"] = thread_id.strip()

    query = apply_concept_query_filter(base_filter)

    cursor = coll.find(query).sort("created_at", -1).skip(skip).limit(limit)

    return list(cursor)


def get_conversation_between_users(
    user_id_1: str,
    user_id_2: str,
    limit: int = 50,
    skip: int = 0,
) -> List[Dict[str, Any]]:
    """Get all messages between two users (direct conversation).

    Returns messages in both directions, sorted by time.
    """
    if not user_id_1 or not user_id_2:
        return []

    user_id_1 = user_id_1.strip()
    user_id_2 = user_id_2.strip()
    coll = get_concepts_collection()
    if coll is None:
        return []

    # Messages where user1 sent to user2 OR user2 sent to user1
    base_filter: Dict[str, Any] = {
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        "$or": [
            {
                f"relationships.{PREDICATE_SENDER}": user_id_1,
                f"relationships.{PREDICATE_RECIPIENT}": user_id_2,
            },
            {
                f"relationships.{PREDICATE_SENDER}": user_id_2,
                f"relationships.{PREDICATE_RECIPIENT}": user_id_1,
            },
        ],
    }

    query = apply_concept_query_filter(base_filter)

    cursor = (
        coll.find(query)
        .sort("created_at", 1)  # Chronological for conversations
        .skip(skip)
        .limit(limit)
    )

    return list(cursor)


def get_unread_count(user_id: str) -> int:
    """Get count of unread messages for a user."""
    if not user_id:
        return 0

    user_id = user_id.strip()
    coll = get_concepts_collection()
    if coll is None:
        return 0

    base_filter: Dict[str, Any] = {
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        f"relationships.{PREDICATE_RECIPIENT}": user_id,
        "concept_data.read_by": {"$nin": [user_id]},
    }

    query = apply_concept_query_filter(base_filter)

    return coll.count_documents(query)


def mark_message_read(message_id: str, reader_id: str) -> bool:
    """Mark a message as read by a specific user.

    Returns True if the message was updated.
    """
    if not message_id or not reader_id:
        return False

    message_id = message_id.strip()
    reader_id = reader_id.strip()

    coll = get_concepts_collection()
    if coll is None:
        return False

    # Use access control to ensure user can see the message
    base_filter = {"concept_id": message_id}
    query = apply_concept_query_filter(base_filter)

    result = coll.update_one(
        query,
        {
            "$addToSet": {"concept_data.read_by": reader_id},
            "$set": {
                "concept_data.message_status": MESSAGE_STATUS_READ,
                "updated_at": datetime.now(timezone.utc),
            },
        },
    )

    if result.modified_count > 0:
        _log.info(f"Message {message_id} marked as read by {reader_id}")
        return True

    return False


def mark_messages_read_bulk(message_ids: List[str], reader_id: str) -> int:
    """Mark multiple messages as read.

    Returns the number of messages updated.
    """
    if not message_ids or not reader_id:
        return 0

    reader_id = reader_id.strip()
    message_ids = [mid.strip() for mid in message_ids if mid and mid.strip()]

    if not message_ids:
        return 0

    coll = get_concepts_collection()
    if coll is None:
        return 0

    base_filter = {"concept_id": {"$in": message_ids}}
    query = apply_concept_query_filter(base_filter)

    result = coll.update_many(
        query,
        {
            "$addToSet": {"concept_data.read_by": reader_id},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

    if result.modified_count > 0:
        _log.info(f"Marked {result.modified_count} messages as read by {reader_id}")

    return result.modified_count


def delete_message(message_id: str, requester_id: str) -> bool:
    """Delete a message (soft delete by marking as deleted).

    Only the sender can delete a message.

    Returns True if the message was deleted.
    """
    if not message_id or not requester_id:
        return False

    message_id = message_id.strip()
    requester_id = requester_id.strip()

    coll = get_concepts_collection()
    if coll is None:
        return False

    # Only the sender can delete
    base_filter = {
        "concept_id": message_id,
        f"relationships.{PREDICATE_SENDER}": requester_id,
    }
    query = apply_concept_query_filter(base_filter)

    result = coll.update_one(
        query,
        {
            "$set": {
                "concept_data.deleted": True,
                "concept_data.deleted_at": datetime.now(timezone.utc).isoformat(),
                "concept_data.deleted_by": requester_id,
                "updated_at": datetime.now(timezone.utc),
            },
        },
    )

    if result.modified_count > 0:
        _log.info(f"Message {message_id} deleted by {requester_id}")
        return True

    return False


def get_message_threads_for_user(
    user_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Get distinct conversation threads for a user.

    Returns a list of thread summaries with the most recent message
    and other participant info.
    """
    if not user_id:
        return []

    user_id = user_id.strip()
    coll = get_concepts_collection()
    if coll is None:
        return []

    # Aggregation to get distinct conversations
    pipeline = [
        # Match messages involving this user
        {
            "$match": apply_concept_query_filter(
                {
                    "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
                    "$or": [
                        {f"relationships.{PREDICATE_SENDER}": user_id},
                        {f"relationships.{PREDICATE_RECIPIENT}": user_id},
                    ],
                    "concept_data.deleted": {"$ne": True},
                }
            )
            or {}
        },
        # Sort by created_at descending
        {"$sort": {"created_at": -1}},
        # Group by the "other" participant(s)
        {
            "$group": {
                "_id": {
                    "$cond": {
                        "if": {
                            "$eq": [
                                {
                                    "$arrayElemAt": [
                                        f"$relationships.{PREDICATE_SENDER}",
                                        0,
                                    ]
                                },
                                user_id,
                            ]
                        },
                        "then": f"$relationships.{PREDICATE_RECIPIENT}",
                        "else": f"$relationships.{PREDICATE_SENDER}",
                    }
                },
                "last_message": {"$first": "$$ROOT"},
                "message_count": {"$sum": 1},
            }
        },
        # Sort threads by most recent message
        {"$sort": {"last_message.created_at": -1}},
        {"$limit": limit},
    ]

    try:
        results = list(coll.aggregate(pipeline))
        return results
    except Exception as e:
        _log.error(f"Failed to get message threads: {e}")
        return []


def search_messages(
    user_id: str,
    query_text: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Search messages by content for a user.

    Uses text search on the message content.
    """
    if not user_id or not query_text:
        return []

    user_id = user_id.strip()
    coll = get_concepts_collection()
    if coll is None:
        return []

    # Basic regex search on concept_data.content_fallback
    # TODO: Integrate with proper text search / RAG when available
    base_filter: Dict[str, Any] = {
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        "$or": [
            {f"relationships.{PREDICATE_SENDER}": user_id},
            {f"relationships.{PREDICATE_RECIPIENT}": user_id},
        ],
        "concept_data.content_fallback": {
            "$regex": query_text,
            "$options": "i",
        },
    }

    query = apply_concept_query_filter(base_filter)

    cursor = coll.find(query).sort("created_at", -1).limit(limit)

    return list(cursor)
