"""Inter-user messaging service for Von.

Manages direct messages between users (human and agent) as Vontology concepts.
Messages are stored with visibility scoping via specific_to_user (sender + recipients).

JVNAUTOSCI-1071
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pymongo.errors import DuplicateKeyError

from ..db.mongo_client import get_concepts_collection
from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import (
    apply_concept_query_filter,
)
from ..security.visibility_predicates import (
    set_specific_to_user_values,
)
from ..services.text_value_service import upsert_text_for_concept
from .workflow_event_integration_service import (
    maybe_launch_direct_message_workflow,
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

DIRECT_MESSAGE_IDEMPOTENCY_SCHEMA_VERSION = "direct_message_idempotency.v1"
DIRECT_MESSAGE_IDEMPOTENCY_SCOPE_FIELD = "delivery_idempotency_scope"
DIRECT_MESSAGE_PAYLOAD_FINGERPRINT_FIELD = "delivery_payload_fingerprint"
DIRECT_MESSAGE_DELIVERY_FINGERPRINT_FIELD = "delivery_fingerprint"
DIRECT_MESSAGE_IDEMPOTENCY_KEY_HASH_FIELD = "delivery_idempotency_key_hash"
MAX_DIRECT_MESSAGE_IDEMPOTENCY_KEY_LENGTH = 512
DIRECT_MESSAGE_IDEMPOTENCY_DOCUMENT_ID_PREFIX = "direct_message_idempotency_v1:"

_DIRECT_MESSAGE_RESERVED_DELIVERY_METADATA_FIELDS = frozenset(
    {
        DIRECT_MESSAGE_IDEMPOTENCY_SCOPE_FIELD,
        DIRECT_MESSAGE_PAYLOAD_FINGERPRINT_FIELD,
        DIRECT_MESSAGE_DELIVERY_FINGERPRINT_FIELD,
        DIRECT_MESSAGE_IDEMPOTENCY_KEY_HASH_FIELD,
        "delivery_idempotency_schema_version",
    }
)


class DirectMessageIdempotencyConflict(ValueError):
    """The same scoped idempotency key names a different message intent."""


@dataclass(frozen=True)
class DirectMessageDeliveryIdentity:
    """Opaque hashes used to reconcile one actor-scoped delivery request."""

    idempotency_scope: str
    payload_fingerprint: str
    delivery_fingerprint: str
    idempotency_key_hash: str
    document_id: str


@dataclass(frozen=True)
class DirectMessageCreateReceipt:
    """Result of an idempotent direct-message create or replay."""

    message: Dict[str, Any]
    reused: bool
    delivery_identity: DirectMessageDeliveryIdentity


def _normalise_concept_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("#V#") else f"#V#{cleaned}"


def normalise_message_recipient_ids(value: Any) -> List[str]:
    """Return stable, de-duplicated recipient concept IDs."""

    if not isinstance(value, list):
        return []
    recipients: List[str] = []
    seen: set[str] = set()
    for item in value:
        concept_id = _normalise_concept_id(item)
        if concept_id is None:
            continue
        fingerprint = concept_id.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        recipients.append(concept_id)
    return recipients


def normalise_direct_message_delivery_idempotency_key(value: Any) -> Optional[str]:
    """Validate an optional client retry key without treating it as authority."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("delivery_idempotency_key must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("delivery_idempotency_key must not be empty")
    if len(cleaned) > MAX_DIRECT_MESSAGE_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(
            "delivery_idempotency_key must be at most "
            f"{MAX_DIRECT_MESSAGE_IDEMPOTENCY_KEY_LENGTH} characters"
        )
    return cleaned


def _normalise_optional_message_text(value: Any, *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value.strip() or None


def _canonical_json_sha256(payload: Dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Direct-message metadata must be valid JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _material_delivery_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    return {
        str(key): value
        for key, value in metadata.items()
        if isinstance(key, str)
        and key not in _DIRECT_MESSAGE_RESERVED_DELIVERY_METADATA_FIELDS
    }


def sanitise_direct_message_delivery_metadata(
    metadata: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Remove server-owned delivery identity fields from client metadata."""

    return _material_delivery_metadata(metadata)


def build_direct_message_delivery_identity(
    *,
    delivery_idempotency_key: str,
    sender_id: str,
    organisation_concept_id: str,
    recipient_ids: List[str],
    content: str,
    subject: Optional[str] = None,
    thread_id: Optional[str] = None,
    reply_to_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> DirectMessageDeliveryIdentity:
    """Build a scoped retry identity and a separately comparable payload hash.

    The client key is never an authority carrier. Its unique claim is bound to
    the trusted sender, so the same key remains isolated between actors. The
    separate payload hash binds the authorised organisation and canonical
    recipient set along with visible message content; changing any of those
    fields is therefore conflicting key reuse rather than a second delivery.
    """

    normalised_key = normalise_direct_message_delivery_idempotency_key(
        delivery_idempotency_key
    )
    normalised_sender = _normalise_concept_id(sender_id)
    normalised_org = _normalise_concept_id(organisation_concept_id)
    normalised_recipients = normalise_message_recipient_ids(recipient_ids)
    if normalised_key is None:
        raise ValueError("delivery_idempotency_key is required")
    if normalised_sender is None:
        raise ValueError("sender_id is required")
    if normalised_org is None:
        raise ValueError("organisation_concept_id is required")
    if not normalised_recipients:
        raise ValueError("At least one recipient is required")
    if not isinstance(content, str) or (
        not content.strip() and not (metadata or {}).get("attachments")
    ):
        raise ValueError("Message content cannot be empty")

    canonical_recipients = sorted(
        normalised_recipients,
        key=lambda item: item.casefold(),
    )
    idempotency_key_hash = hashlib.sha256(normalised_key.encode("utf-8")).hexdigest()
    idempotency_scope = _canonical_json_sha256(
        {
            "schema_version": DIRECT_MESSAGE_IDEMPOTENCY_SCHEMA_VERSION,
            "delivery_idempotency_key": normalised_key,
            "sender_id": normalised_sender,
        }
    )
    payload_fingerprint = _canonical_json_sha256(
        {
            "schema_version": DIRECT_MESSAGE_IDEMPOTENCY_SCHEMA_VERSION,
            "organisation_concept_id": normalised_org,
            "recipient_ids": canonical_recipients,
            "content": content.strip(),
            "subject": _normalise_optional_message_text(
                subject,
                field_name="subject",
            ),
            "thread_id": _normalise_optional_message_text(
                thread_id,
                field_name="thread_id",
            ),
            "reply_to_id": _normalise_optional_message_text(
                reply_to_id,
                field_name="reply_to_id",
            ),
            "metadata": _material_delivery_metadata(metadata),
        }
    )
    delivery_fingerprint = _canonical_json_sha256(
        {
            "schema_version": DIRECT_MESSAGE_IDEMPOTENCY_SCHEMA_VERSION,
            "idempotency_scope": idempotency_scope,
            "payload_fingerprint": payload_fingerprint,
        }
    )
    return DirectMessageDeliveryIdentity(
        idempotency_scope=idempotency_scope,
        payload_fingerprint=payload_fingerprint,
        delivery_fingerprint=delivery_fingerprint,
        idempotency_key_hash=idempotency_key_hash,
        document_id=(
            f"{DIRECT_MESSAGE_IDEMPOTENCY_DOCUMENT_ID_PREFIX}{idempotency_scope}"
        ),
    )


def authorise_direct_message_participants(
    *,
    sender_id: str,
    recipient_ids: List[str],
    organisation_concept_id: str,
) -> tuple[bool, List[str]]:
    """Check that the sender and every recipient belong to the selected org."""

    from .organisation_membership_service import get_organisation_members

    normalised_sender = _normalise_concept_id(sender_id)
    normalised_org = _normalise_concept_id(organisation_concept_id)
    normalised_recipients = normalise_message_recipient_ids(recipient_ids)
    if normalised_sender is None or normalised_org is None:
        invalid = [
            value
            for value in (sender_id, organisation_concept_id)
            if isinstance(value, str) and value.strip()
        ]
        return False, invalid

    members = get_organisation_members(normalised_org)
    member_ids = {
        concept_id
        for concept_id in (
            _normalise_concept_id(member.get("user_concept_id"))
            for member in members.get("members", [])
            if isinstance(member, dict)
        )
        if concept_id is not None
    }
    invalid_ids: List[str] = []
    if normalised_sender not in member_ids:
        invalid_ids.append(normalised_sender)
    invalid_ids.extend(
        recipient_id
        for recipient_id in normalised_recipients
        if recipient_id not in member_ids
    )
    return not invalid_ids, invalid_ids


def project_direct_message(message_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Return the bounded direct-message fields needed for receipts/read-back."""

    relationships = message_doc.get("relationships")
    relationships = relationships if isinstance(relationships, dict) else {}
    concept_data = message_doc.get("concept_data")
    concept_data = concept_data if isinstance(concept_data, dict) else {}

    def relationship_values(predicate: str) -> List[str]:
        raw = relationships.get(predicate)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, str) and item.strip()]

    sender_ids = relationship_values(PREDICATE_SENDER)
    raw_read_by = concept_data.get("read_by")
    if isinstance(raw_read_by, str):
        raw_read_by = [raw_read_by]
    if not isinstance(raw_read_by, list):
        raw_read_by = []
    from .message_attachment_service import message_attachment_descriptors

    attachments = message_attachment_descriptors(message_doc)
    return {
        **({"attachments": attachments} if attachments else {}),
        "message_id": str(message_doc.get("concept_id") or ""),
        "sender_id": sender_ids[0] if sender_ids else None,
        "recipient_ids": relationship_values(PREDICATE_RECIPIENT),
        "subject": concept_data.get("subject"),
        "content": concept_data.get("content_fallback"),
        "sent_at": concept_data.get("sent_at"),
        "status": concept_data.get("message_status"),
        "read_by": [item for item in raw_read_by if isinstance(item, str)],
        "thread_id": (
            relationship_values(PREDICATE_THREAD)[0]
            if relationship_values(PREDICATE_THREAD)
            else None
        ),
        "reply_to_id": (
            relationship_values(PREDICATE_REPLY_TO)[0]
            if relationship_values(PREDICATE_REPLY_TO)
            else None
        ),
        "organisation_concept_id": concept_data.get("organisation_concept_id"),
        "attribution": concept_data.get("attribution"),
    }


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
    _server_owned_document_id: Optional[str] = None,
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
        _server_owned_document_id: Internal deterministic Mongo identity for an
            already server-derived idempotency scope. REST clients cannot set it.

    Returns:
        The created message document.
    """
    if not sender_id:
        raise ValueError("sender_id is required")
    if not recipient_ids:
        raise ValueError("At least one recipient is required")
    if not isinstance(content, str) or (
        not content.strip() and not (metadata or {}).get("attachments")
    ):
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

    metadata_payload = metadata if isinstance(metadata, dict) else {}
    metadata_payload = {
        str(k): v for k, v in metadata_payload.items() if isinstance(k, str)
    }
    if metadata_payload.get("attachments"):
        from .message_attachment_service import authorise_message_attachments

        metadata_payload["attachments"] = authorise_message_attachments(
            [a["concept_id"] for a in metadata_payload["attachments"]], sender_id
        )
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
            # Organisation is provenance and send-authorisation context. Direct
            # message visibility remains limited to sender and recipients.
            "organisation_concept_id": org_id.strip() if org_id else None,
        },
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if _server_owned_document_id is not None:
        document_id = str(_server_owned_document_id).strip()
        scope_hash = document_id.removeprefix(
            DIRECT_MESSAGE_IDEMPOTENCY_DOCUMENT_ID_PREFIX
        )
        persisted_scope = str(
            metadata_payload.get(DIRECT_MESSAGE_IDEMPOTENCY_SCOPE_FIELD) or ""
        ).strip()
        if (
            not document_id.startswith(DIRECT_MESSAGE_IDEMPOTENCY_DOCUMENT_ID_PREFIX)
            or len(scope_hash) != 64
            or any(character not in "0123456789abcdef" for character in scope_hash)
            or persisted_scope != scope_hash
        ):
            raise ValueError("Invalid server-owned direct-message document identity")
        concept_doc["_id"] = document_id

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


def get_message_for_user(
    message_id: str,
    user_id: str,
) -> Optional[Dict[str, Any]]:
    """Retrieve one message only when the user is a sender or recipient."""

    if not message_id or not user_id:
        return None
    coll = get_concepts_collection()
    if coll is None:
        return None

    normalised_user_id = _normalise_concept_id(user_id)
    if normalised_user_id is None:
        return None
    base_filter: Dict[str, Any] = {
        "concept_id": message_id.strip(),
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        "$or": [
            {f"relationships.{PREDICATE_SENDER}": normalised_user_id},
            {f"relationships.{PREDICATE_RECIPIENT}": normalised_user_id},
        ],
    }
    return coll.find_one(apply_concept_query_filter(base_filter))


def get_message_for_delivery_fingerprint(
    *,
    sender_id: str,
    delivery_fingerprint: str,
) -> Optional[Dict[str, Any]]:
    """Read an actor's prior delivery for bounded retry reconciliation."""

    normalised_sender = _normalise_concept_id(sender_id)
    if normalised_sender is None or not delivery_fingerprint.strip():
        return None
    coll = get_concepts_collection()
    if coll is None:
        return None
    base_filter: Dict[str, Any] = {
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        f"relationships.{PREDICATE_SENDER}": normalised_sender,
        "concept_data.metadata.delivery_fingerprint": delivery_fingerprint.strip(),
    }
    return coll.find_one(apply_concept_query_filter(base_filter))


def get_message_for_delivery_idempotency_scope(
    *,
    sender_id: str,
    idempotency_scope: str,
) -> Optional[Dict[str, Any]]:
    """Read the trusted sender's message through Mongo's built-in unique key."""

    normalised_sender = _normalise_concept_id(sender_id)
    scope = str(idempotency_scope or "").strip()
    if normalised_sender is None or not scope:
        return None
    coll = get_concepts_collection()
    if coll is None:
        return None
    base_filter: Dict[str, Any] = {
        "_id": f"{DIRECT_MESSAGE_IDEMPOTENCY_DOCUMENT_ID_PREFIX}{scope}",
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        f"relationships.{PREDICATE_SENDER}": normalised_sender,
    }
    message = coll.find_one(apply_concept_query_filter(base_filter))
    return message if isinstance(message, dict) else None


def _require_matching_delivery_identity(
    message: Dict[str, Any],
    expected: DirectMessageDeliveryIdentity,
) -> None:
    concept_data = message.get("concept_data")
    concept_data = concept_data if isinstance(concept_data, dict) else {}
    metadata = concept_data.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if (
        metadata.get(DIRECT_MESSAGE_IDEMPOTENCY_SCOPE_FIELD)
        != expected.idempotency_scope
        or metadata.get(DIRECT_MESSAGE_PAYLOAD_FINGERPRINT_FIELD)
        != expected.payload_fingerprint
        or metadata.get(DIRECT_MESSAGE_DELIVERY_FINGERPRINT_FIELD)
        != expected.delivery_fingerprint
    ):
        raise DirectMessageIdempotencyConflict(
            "delivery_idempotency_key was already used for a different message payload"
        )


def create_message_idempotently(
    *,
    delivery_idempotency_key: str,
    sender_id: str,
    recipient_ids: List[str],
    content: str,
    organisation_concept_id: str,
    subject: Optional[str] = None,
    thread_id: Optional[str] = None,
    reply_to_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> DirectMessageCreateReceipt:
    """Create once, or reconcile a concurrent/retried REST delivery request.

    A deterministic, server-owned Mongo ``_id`` derived from the opaque sender
    scope is the atomic claim. The pre-read is the fast replay path; built-in
    ``_id`` uniqueness and ``DuplicateKeyError`` recovery make simultaneous
    requests converge even when optional index bootstrap fails open.
    """

    normalised_recipients = normalise_message_recipient_ids(recipient_ids)
    normalised_subject = _normalise_optional_message_text(
        subject,
        field_name="subject",
    )
    normalised_thread_id = _normalise_optional_message_text(
        thread_id,
        field_name="thread_id",
    )
    normalised_reply_to_id = _normalise_optional_message_text(
        reply_to_id,
        field_name="reply_to_id",
    )
    material_metadata = _material_delivery_metadata(metadata)
    identity = build_direct_message_delivery_identity(
        delivery_idempotency_key=delivery_idempotency_key,
        sender_id=sender_id,
        organisation_concept_id=organisation_concept_id,
        recipient_ids=normalised_recipients,
        content=content,
        subject=normalised_subject,
        thread_id=normalised_thread_id,
        reply_to_id=normalised_reply_to_id,
        metadata=material_metadata,
    )

    def find_prior() -> Optional[Dict[str, Any]]:
        return get_message_for_delivery_idempotency_scope(
            sender_id=sender_id,
            idempotency_scope=identity.idempotency_scope,
        )

    prior_message = find_prior()
    if isinstance(prior_message, dict):
        _require_matching_delivery_identity(prior_message, identity)
        return DirectMessageCreateReceipt(
            message=prior_message,
            reused=True,
            delivery_identity=identity,
        )

    persistence_metadata = dict(material_metadata)
    persistence_metadata.update(
        {
            "delivery_idempotency_schema_version": (
                DIRECT_MESSAGE_IDEMPOTENCY_SCHEMA_VERSION
            ),
            DIRECT_MESSAGE_IDEMPOTENCY_SCOPE_FIELD: identity.idempotency_scope,
            DIRECT_MESSAGE_PAYLOAD_FINGERPRINT_FIELD: identity.payload_fingerprint,
            DIRECT_MESSAGE_DELIVERY_FINGERPRINT_FIELD: identity.delivery_fingerprint,
            DIRECT_MESSAGE_IDEMPOTENCY_KEY_HASH_FIELD: identity.idempotency_key_hash,
        }
    )

    try:
        message = create_message(
            sender_id=sender_id,
            recipient_ids=normalised_recipients,
            content=content.strip(),
            subject=normalised_subject,
            thread_id=normalised_thread_id,
            reply_to_id=normalised_reply_to_id,
            org_id=organisation_concept_id,
            metadata=persistence_metadata,
            _server_owned_document_id=identity.document_id,
        )
    except DuplicateKeyError as exc:
        winner = find_prior()
        if not isinstance(winner, dict):
            raise RuntimeError(
                "Direct-message idempotency race could not be reconciled"
            ) from exc
        _require_matching_delivery_identity(winner, identity)
        return DirectMessageCreateReceipt(
            message=winner,
            reused=True,
            delivery_identity=identity,
        )

    return DirectMessageCreateReceipt(
        message=message,
        reused=False,
        delivery_identity=identity,
    )


def get_messages_for_user(
    user_id: str,
    include_sent: bool = True,
    include_received: bool = True,
    thread_id: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
    other_user_id: Optional[str] = None,
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

    if other_user_id:
        # Further narrow the actor's existing mailbox; never replace its scope.
        counterpart = other_user_id.strip()
        base_filter["$and"] = [
            {
                "$or": [
                    {
                        f"relationships.{PREDICATE_SENDER}": user_id,
                        f"relationships.{PREDICATE_RECIPIENT}": counterpart,
                    },
                    {
                        f"relationships.{PREDICATE_SENDER}": counterpart,
                        f"relationships.{PREDICATE_RECIPIENT}": user_id,
                    },
                ]
            }
        ]

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

    # A recipient may acknowledge a message; organisation visibility alone is
    # never sufficient authority to change direct-message state.
    base_filter = {
        "concept_id": message_id,
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        f"relationships.{PREDICATE_RECIPIENT}": reader_id,
    }
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

    base_filter = {
        "concept_id": {"$in": message_ids},
        "relationships.is_an_instance_of": MESSAGE_TYPE_CONCEPT_ID,
        f"relationships.{PREDICATE_RECIPIENT}": reader_id,
    }
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
