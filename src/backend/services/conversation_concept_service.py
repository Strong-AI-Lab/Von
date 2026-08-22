"""Conversation concept service for JVNAUTOSCI-1040.

Provides lazy creation and management of conversation concepts, linking
chat sessions to Vontology for task management and other cross-conversation
features.

Conversations are created lazily when first referenced (e.g., by a task).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.text_value_service import upsert_text_for_concept
from ..utils.concept_id_utils import ensure_v_concept_prefix
from ..security.visibility_predicates import set_specific_to_user_values

logger = logging.getLogger(__name__)

# Conversation type concept ID
CONVERSATION_TYPE_ID = "#V#conversation"

# Conversation predicates
PREDICATE_HAS_SESSION_ID = "#V#hasSessionId"
PREDICATE_HAS_PARTICIPANT = "#V#hasParticipant"
PREDICATE_HAS_OWNER = "#V#hasOwner"
PREDICATE_HAS_ORGANISATION = "#V#hasOrganisation"
PREDICATE_HAS_NAMESPACE = "#V#hasNamespace"
PREDICATE_HAS_TOPIC = "#V#hasTopic"
PREDICATE_HAS_START_TIME = "#V#hasStartTime"
PREDICATE_HAS_NAME = "#V#hasName"

# A legacy session identifier can be attached to more than one text relation,
# but this lookup is a compatibility path rather than a discovery API.  Keep
# every legacy read bounded so task creation cannot spend its whole turn budget
# scanning unrelated text relations.
_LEGACY_SESSION_TEXT_VALUE_LIMIT = 4
_LEGACY_SESSION_RELATION_LIMIT = 8


class ConversationConceptError(Exception):
    """Base exception for conversation concept operations."""

    pass


def _generate_conversation_concept_id(session_id: str) -> str:
    """Generate a concept_id for a conversation based on session_id.

    Uses a deterministic mapping so the same session_id always produces
    the same concept_id.
    """
    # Hash the session_id to create a stable, shorter identifier
    import hashlib

    hash_suffix = hashlib.sha256(session_id.encode()).hexdigest()[:12]
    return f"#V#conversation_{hash_suffix}"


def _session_id_text_fingerprint(session_id: str) -> str:
    """Return the TextValue fingerprint used for the normal session-id write."""

    normalised = session_id.strip()
    normalised = normalised.replace("\r\n", "\n").replace("\r", "\n")
    normalised = re.sub(r"[\t\f\v ]+", " ", normalised)
    normalised = re.sub(r" *\n *", "\n", normalised)
    return f"{normalised.lower()}||en"


def _is_visible_conversation_document(doc: Any) -> bool:
    """Return whether an access-filtered concept document is a conversation."""

    if not isinstance(doc, dict):
        return False
    concept_id = doc.get("concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return False
    relationships = doc.get("relationships")
    if not isinstance(relationships, dict):
        return False
    instance_of = relationships.get("is_an_instance_of")
    if isinstance(instance_of, str):
        instance_of = [instance_of]
    return isinstance(instance_of, list) and CONVERSATION_TYPE_ID in instance_of


def _visible_conversation_id(doc: Any) -> Optional[str]:
    """Return the verified conversation ID, preserving repository access scope."""

    if not _is_visible_conversation_document(doc):
        return None
    concept_id = doc.get("concept_id")
    return concept_id.strip() if isinstance(concept_id, str) else None


def get_conversation_concept_by_session_id(session_id: str) -> Optional[str]:
    """Find an existing conversation concept by session_id.

    Args:
        session_id: The chat session_id to look up

    Returns:
        The conversation concept_id if found, None otherwise
    """
    if not session_id or not isinstance(session_id, str):
        return None

    # Look up by the deterministic concept_id
    expected_concept_id = _generate_conversation_concept_id(session_id)
    doc = ConceptsRepository.find_one({"concept_id": expected_concept_id})
    existing_id = _visible_conversation_id(doc)
    if existing_id:
        return existing_id

    # Legacy conversations created before the deterministic scheme stored the
    # session in metadata.  This is an exact, actor-filtered concept lookup.
    doc = ConceptsRepository.find_one(
        {
            "metadata.session_id": session_id,
            "relationships.is_an_instance_of": CONVERSATION_TYPE_ID,
        }
    )
    existing_id = _visible_conversation_id(doc)
    if existing_id:
        return existing_id

    # Last compatibility path: use an exact TextValue candidate, then one
    # bounded relation lookup.  Do not scan every hasSessionId relation and
    # hydrate its text value one-by-one: that turns a normal task create into
    # an unbounded global/N+1 read.
    from ..db.repositories.text_value_repository import (
        TextRelationsRepository,
        TextValuesRepository,
    )

    # Keep the normal indexed fingerprint lookup separate from the legacy raw
    # text fallback.  Combining them in one ``$or`` allows the unindexed legacy
    # branch to determine the whole query plan on a large TextValues collection.
    text_values = list(
        TextValuesRepository.find(
            {
                "fingerprint": _session_id_text_fingerprint(session_id),
                "lang": "en",
            },
            projection={"_id": 1, "text": 1},
            limit=_LEGACY_SESSION_TEXT_VALUE_LIMIT,
        )
    )
    if not any(
        isinstance(text_value, dict) and text_value.get("text") == session_id
        for text_value in text_values
    ):
        text_values = list(
            TextValuesRepository.find(
                {"text": session_id},
                projection={"_id": 1, "text": 1},
                limit=_LEGACY_SESSION_TEXT_VALUE_LIMIT,
            )
        )
    text_value_ids: list[Any] = []
    for text_value in text_values:
        if not isinstance(text_value, dict) or text_value.get("text") != session_id:
            continue
        text_value_id = text_value.get("_id")
        if text_value_id is None:
            continue
        if text_value_id not in text_value_ids:
            text_value_ids.append(text_value_id)
        string_id = str(text_value_id)
        if string_id and string_id not in text_value_ids:
            text_value_ids.append(string_id)
    if not text_value_ids:
        return None

    relations = list(
        TextRelationsRepository.find(
            {
                "predicate": {"$in": [PREDICATE_HAS_SESSION_ID, "hasSessionId"]},
                "object_text_id": {"$in": text_value_ids},
            },
            projection={"subject_concept_id": 1},
            limit=_LEGACY_SESSION_RELATION_LIMIT,
        )
    )
    subject_ids: list[str] = []
    for rel in relations:
        subject_id = rel.get("subject_concept_id") if isinstance(rel, dict) else None
        if (
            isinstance(subject_id, str)
            and subject_id.strip()
            and subject_id not in subject_ids
        ):
            subject_ids.append(subject_id)
    if not subject_ids:
        return None

    # ConceptsRepository applies the current actor's visibility filter.  Batch
    # candidate validation deliberately avoids a relation-to-concept N+1 path.
    visible_candidates = ConceptsRepository.find(
        {
            "concept_id": {"$in": subject_ids},
            "relationships.is_an_instance_of": CONVERSATION_TYPE_ID,
        },
        projection={"concept_id": 1, "relationships.is_an_instance_of": 1},
        limit=_LEGACY_SESSION_RELATION_LIMIT,
    )
    visible_ids = {
        conversation_id
        for conversation_id in (
            _visible_conversation_id(candidate) for candidate in visible_candidates
        )
        if conversation_id
    }
    for subject_id in subject_ids:
        if subject_id in visible_ids:
            return subject_id

    return None


def get_or_create_conversation_concept(
    session_id: str,
    *,
    owner_concept_id: Optional[str] = None,
    organisation_concept_id: Optional[str] = None,
    namespace: Optional[str] = None,
    topic: Optional[str] = None,
) -> str:
    """Get or create a conversation concept for a chat session.

    This is the primary entry point for lazy conversation concept creation.
    If a conversation concept already exists for the session_id, it returns
    that concept_id. Otherwise, it creates a new conversation concept.

    Args:
        session_id: The chat session_id (required)
        owner_concept_id: The user who owns/initiated the conversation
        organisation_concept_id: The organisation context, if applicable
        namespace: The composite namespace for multi-tenant scoping
        topic: Optional conversation topic/summary

    Returns:
        The conversation concept_id

    Raises:
        ConversationConceptError: If creation fails
    """
    if not session_id or not isinstance(session_id, str):
        raise ConversationConceptError("session_id is required")

    # Check if conversation concept already exists
    existing_id = get_conversation_concept_by_session_id(session_id)
    if existing_id:
        logger.debug(f"Found existing conversation concept: {existing_id}")
        return existing_id

    # Create new conversation concept
    conversation_concept_id = _generate_conversation_concept_id(session_id)
    now = datetime.now(timezone.utc)

    # Build relationships
    relationships: Dict[str, Any] = {
        "is_an_instance_of": [CONVERSATION_TYPE_ID],
    }

    if owner_concept_id:
        owner_concept_id = ensure_v_concept_prefix(owner_concept_id)
        if owner_concept_id is None:
            raise ConversationConceptError("owner_concept_id is invalid")
        relationships[PREDICATE_HAS_OWNER] = [owner_concept_id]
        relationships[PREDICATE_HAS_PARTICIPANT] = [owner_concept_id]
        # Conversation concepts are projections of an owner-scoped transcript.
        # Never publish this identity merely because it has a stable ID.
        relationships = set_specific_to_user_values(relationships, [owner_concept_id])

    if organisation_concept_id:
        organisation_concept_id = ensure_v_concept_prefix(organisation_concept_id)
        relationships[PREDICATE_HAS_ORGANISATION] = [organisation_concept_id]

    # Create the concept document
    concept_doc: Dict[str, Any] = {
        "concept_id": conversation_concept_id,
        "guid": str(uuid.uuid4()),
        "relationships": relationships,
        "created_at": now,
        "updated_at": now,
        "metadata": {
            "concept_type": "conversation",
            "session_id": session_id,  # Also store in metadata for quick lookup
            "organisation_concept_id": organisation_concept_id,
        },
    }

    try:
        ConceptsRepository.insert_one(concept_doc)
        logger.info(
            f"Created conversation concept: {conversation_concept_id} for session {session_id}"
        )
    except Exception as e:
        # Handle race condition - another process may have created it
        existing_id = get_conversation_concept_by_session_id(session_id)
        if existing_id:
            logger.debug(
                f"Conversation concept created by another process: {existing_id}"
            )
            return existing_id
        logger.error(f"Failed to create conversation concept: {e}")
        raise ConversationConceptError(f"Failed to create conversation: {e}") from e

    # Store session_id as text relation
    try:
        upsert_text_for_concept(
            subject_concept_id=conversation_concept_id,
            predicate=PREDICATE_HAS_SESSION_ID,
            text=session_id,
            lang="en",
        )
    except Exception as e:
        logger.warning(f"Failed to store session_id text relation: {e}")

    # Store start time
    try:
        upsert_text_for_concept(
            subject_concept_id=conversation_concept_id,
            predicate=PREDICATE_HAS_START_TIME,
            text=now.isoformat(),
            lang="en",
        )
    except Exception as e:
        logger.warning(f"Failed to store start time: {e}")

    # Store namespace if provided
    if namespace:
        try:
            upsert_text_for_concept(
                subject_concept_id=conversation_concept_id,
                predicate=PREDICATE_HAS_NAMESPACE,
                text=namespace,
                lang="en",
            )
        except Exception as e:
            logger.warning(f"Failed to store namespace: {e}")

    # Store topic if provided
    if topic:
        try:
            upsert_text_for_concept(
                subject_concept_id=conversation_concept_id,
                predicate=PREDICATE_HAS_TOPIC,
                text=topic,
                lang="en-NZ",
            )
        except Exception as e:
            logger.warning(f"Failed to store topic: {e}")

    # Try to get conversation name from chat history
    try:
        from .chat_history_service import get_chat_history_session_summary

        # Get summary to retrieve session name
        summary = get_chat_history_session_summary(
            user_id=owner_concept_id or "",
            session_id=session_id,
            summary_mode="light",
        )
        if summary:
            session_name = summary.get("session_name")
            if session_name:
                upsert_text_for_concept(
                    subject_concept_id=conversation_concept_id,
                    predicate=PREDICATE_HAS_NAME,
                    text=session_name,
                    lang="en-NZ",
                )
    except Exception as e:
        logger.debug(f"Could not retrieve conversation name from chat history: {e}")

    return conversation_concept_id


def get_conversation_concept(conversation_concept_id: str) -> Optional[Dict[str, Any]]:
    """Get a conversation concept by concept_id.

    Args:
        conversation_concept_id: The conversation's concept_id

    Returns:
        Dict with conversation details, or None if not found
    """
    normalised_id = ensure_v_concept_prefix(conversation_concept_id)
    if not normalised_id:
        return None
    conversation_concept_id = normalised_id

    doc = ConceptsRepository.find_one({"concept_id": conversation_concept_id})
    if not doc:
        return None

    # Check it's actually a conversation
    relationships = doc.get("relationships", {})
    instance_of = relationships.get("is_an_instance_of", [])
    if CONVERSATION_TYPE_ID not in instance_of:
        return None

    return _build_conversation_response(doc)


def _build_conversation_response(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Build a conversation response dict from a concept document."""
    from ..services.text_value_service import get_texts_for_concept

    conversation_concept_id = doc.get("concept_id", "")
    relationships = doc.get("relationships", {})
    metadata = doc.get("metadata", {})

    # Get text relations
    texts = []
    try:
        texts = get_texts_for_concept(conversation_concept_id) or []
    except Exception:
        pass

    session_id = metadata.get("session_id")
    name = None
    topic = None
    namespace = None
    start_time = None

    for text_item in texts:
        predicate = text_item.get("predicate", "")
        text_value = text_item.get("text", "")

        if predicate in (PREDICATE_HAS_SESSION_ID, "hasSessionId"):
            session_id = text_value
        elif predicate in (PREDICATE_HAS_NAME, "hasName"):
            name = text_value
        elif predicate in (PREDICATE_HAS_TOPIC, "hasTopic"):
            topic = text_value
        elif predicate in (PREDICATE_HAS_NAMESPACE, "hasNamespace"):
            namespace = text_value
        elif predicate in (PREDICATE_HAS_START_TIME, "hasStartTime"):
            start_time = text_value

    # Get relationship values
    owner = (relationships.get(PREDICATE_HAS_OWNER) or [None])[0]
    participants = relationships.get(PREDICATE_HAS_PARTICIPANT, [])
    organisation = (relationships.get(PREDICATE_HAS_ORGANISATION) or [None])[0]

    return {
        "conversation_concept_id": conversation_concept_id,
        "session_id": session_id,
        "name": name,
        "topic": topic,
        "owner_concept_id": owner,
        "participants": participants,
        "organisation_concept_id": organisation,
        "namespace": namespace,
        "start_time": start_time,
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }


def add_participant_to_conversation(
    conversation_concept_id: str, participant_concept_id: str
) -> bool:
    """Add a participant to a conversation.

    Args:
        conversation_concept_id: The conversation's concept_id
        participant_concept_id: The participant's concept_id

    Returns:
        True if added successfully
    """
    normalised_conv_id = ensure_v_concept_prefix(conversation_concept_id)
    normalised_part_id = ensure_v_concept_prefix(participant_concept_id)
    if not normalised_conv_id or not normalised_part_id:
        return False
    conversation_concept_id = normalised_conv_id
    participant_concept_id = normalised_part_id

    try:
        ConceptsRepository.mutate_relationship_edge(
            source_id=conversation_concept_id,
            kind=PREDICATE_HAS_PARTICIPANT,
            target_id=participant_concept_id,
            action="add",
        )
        logger.info(
            f"Added participant {participant_concept_id} to conversation {conversation_concept_id}"
        )
        return True
    except Exception as e:
        logger.error(f"Failed to add participant: {e}")
        return False


def update_conversation_topic(conversation_concept_id: str, topic: str) -> bool:
    """Update the topic/summary of a conversation.

    Args:
        conversation_concept_id: The conversation's concept_id
        topic: The new topic/summary

    Returns:
        True if updated successfully
    """
    normalised_id = ensure_v_concept_prefix(conversation_concept_id)
    if not normalised_id:
        return False
    conversation_concept_id = normalised_id

    try:
        upsert_text_for_concept(
            subject_concept_id=conversation_concept_id,
            predicate=PREDICATE_HAS_TOPIC,
            text=topic,
            lang="en-NZ",
        )
        logger.info(f"Updated topic for conversation {conversation_concept_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to update topic: {e}")
        return False


__all__ = [
    "CONVERSATION_TYPE_ID",
    "ConversationConceptError",
    "get_conversation_concept_by_session_id",
    "get_or_create_conversation_concept",
    "get_conversation_concept",
    "add_participant_to_conversation",
    "update_conversation_topic",
]
