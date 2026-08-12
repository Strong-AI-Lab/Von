"""Durable idempotency and finality ledger for outbound Gmail effects.

The Gmail API does not accept an application idempotency key for message
submission.  This ledger therefore owns one delivery fingerprint before quota
reservation or provider dispatch, records the provider receipt, and gives
callers an explicit ``indeterminate`` state instead of encouraging a blind
retry after a lost response.

Message bodies and recipient addresses are deliberately excluded.  The caller
computes a fingerprint from the exact delivery request and may persist only
bounded hashes/counts alongside trusted actor/profile provenance.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..db.mongo_client import get_gmail_outbound_deliveries_collection
from ..utils.time_utils import utc_now
from .gmail_outbound_mailbox_identity_service import (
    require_canonical_gmail_mailbox_key,
)

DELIVERY_LEDGER_SCHEMA_VERSION = "gmail_outbound_delivery.v2"
_TERMINAL_STATUSES = frozenset(
    {"succeeded", "indeterminate", "failed", "not_started"}
)
_KNOWN_STATUSES = frozenset({"dispatch_claimed", *_TERMINAL_STATUSES})
_ALLOWED_PREDECESSOR_STATUSES: Mapping[str, frozenset[str]] = {
    "indeterminate": frozenset({"dispatch_claimed", "indeterminate"}),
    "failed": frozenset({"dispatch_claimed", "indeterminate", "failed"}),
    "not_started": frozenset(
        {"dispatch_claimed", "indeterminate", "not_started"}
    ),
    # A canonically verified delivery is the strongest finality observation and
    # may repair any weaker state. Once stored, no non-success result may erase it.
    "succeeded": _KNOWN_STATUSES,
}


class GmailOutboundDeliveryLedgerUnavailable(RuntimeError):
    """Raised when a delivery effect cannot be made durably observable."""


class GmailOutboundDeliveryConflict(RuntimeError):
    """Raised when one idempotency key is reused for a different message."""


@dataclass(frozen=True)
class GmailOutboundDeliveryClaim:
    claimed: bool
    duplicate: bool
    delivery_fingerprint: str
    status: str
    receipt: Mapping[str, Any] | None = None


def _clean_required(value: Any, field_name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    return cleaned


def _normalise_now(value: datetime | None) -> datetime:
    resolved = value or utc_now()
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=UTC)
    return resolved.astimezone(UTC)


def _alias_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _project(document: Mapping[str, Any]) -> dict[str, Any]:
    receipt = document.get("receipt")
    return {
        "schema_version": DELIVERY_LEDGER_SCHEMA_VERSION,
        "delivery_fingerprint": str(document.get("delivery_fingerprint") or ""),
        "status": str(document.get("status") or "unknown"),
        "canonical_mailbox_key": document.get("canonical_mailbox_key"),
        "profile_resource_concept_id": document.get("profile_resource_concept_id"),
        "profile_alias_digest": document.get("profile_alias_digest"),
        "observed_profile_resource_concept_ids": list(
            document.get("observed_profile_resource_concept_ids") or []
        ),
        "observed_profile_alias_digests": list(
            document.get("observed_profile_alias_digests") or []
        ),
        "actor_user_concept_id": document.get("actor_user_concept_id"),
        "actor_organisation_concept_id": document.get(
            "actor_organisation_concept_id"
        ),
        "request_id": document.get("request_id"),
        "recipient_count": document.get("recipient_count"),
        "message_id": document.get("message_id"),
        "thread_id": document.get("thread_id"),
        "created_at": document.get("created_at"),
        "updated_at": document.get("updated_at"),
        "receipt": dict(receipt) if isinstance(receipt, Mapping) else None,
    }


def claim_gmail_outbound_delivery(
    *,
    delivery_fingerprint: str,
    canonical_mailbox_key: str,
    profile_resource_concept_id: str,
    profile_id: str,
    actor_user_concept_id: str | None,
    actor_organisation_concept_id: str | None,
    request_id: str,
    recipient_count: int,
    recipients_sha256: str,
    subject_sha256: str,
    content_sha256: str,
    collection: Any = None,
    now: datetime | None = None,
) -> GmailOutboundDeliveryClaim:
    """Claim one exact delivery before quota reservation and Gmail dispatch."""

    fingerprint = _clean_required(delivery_fingerprint, "delivery_fingerprint")
    mailbox_key = require_canonical_gmail_mailbox_key(canonical_mailbox_key)
    resource_id = _clean_required(
        profile_resource_concept_id, "profile_resource_concept_id"
    )
    runtime_profile = _clean_required(profile_id, "profile_id")
    profile_alias_digest = _alias_digest(runtime_profile)
    trusted_request_id = _clean_required(request_id, "request_id")
    recipients_digest = _clean_required(recipients_sha256, "recipients_sha256")
    subject_digest = _clean_required(subject_sha256, "subject_sha256")
    content_digest = _clean_required(content_sha256, "content_sha256")
    if not isinstance(recipient_count, int) or isinstance(recipient_count, bool):
        raise TypeError("recipient_count must be an integer")
    if recipient_count <= 0:
        raise ValueError("recipient_count must be positive")

    ledger = (
        collection
        if collection is not None
        else get_gmail_outbound_deliveries_collection()
    )
    if ledger is None:
        raise GmailOutboundDeliveryLedgerUnavailable(
            "Outbound Gmail delivery ledger is unavailable"
        )
    timestamp = _normalise_now(now)
    document = {
        # Mongo guarantees _id uniqueness independently of optional secondary
        # index bootstrap. This is the actual dispatch-ownership boundary.
        "_id": fingerprint,
        "schema_version": DELIVERY_LEDGER_SCHEMA_VERSION,
        "delivery_fingerprint": fingerprint,
        "status": "dispatch_claimed",
        "canonical_mailbox_key": mailbox_key,
        "profile_resource_concept_id": resource_id,
        "profile_alias_digest": profile_alias_digest,
        "observed_profile_resource_concept_ids": [resource_id],
        "observed_profile_alias_digests": [profile_alias_digest],
        "actor_user_concept_id": (
            str(actor_user_concept_id).strip() if actor_user_concept_id else None
        ),
        "actor_organisation_concept_id": (
            str(actor_organisation_concept_id).strip()
            if actor_organisation_concept_id
            else None
        ),
        "request_id": trusted_request_id,
        "recipient_count": recipient_count,
        "recipients_sha256": recipients_digest,
        "subject_sha256": subject_digest,
        "content_sha256": content_digest,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        ledger.insert_one(document)
        return GmailOutboundDeliveryClaim(
            claimed=True,
            duplicate=False,
            delivery_fingerprint=fingerprint,
            status="dispatch_claimed",
        )
    except DuplicateKeyError:
        existing = ledger.find_one({"_id": fingerprint})
        if not isinstance(existing, Mapping):
            # Read-only compatibility for any pre-_id ledger record. New claims
            # are always intrinsically unique by fingerprint above.
            existing = ledger.find_one({"delivery_fingerprint": fingerprint})
        if not isinstance(existing, Mapping):
            raise GmailOutboundDeliveryLedgerUnavailable(
                "Duplicate Gmail delivery claim could not be read back"
            )
        expected_payload_identity = {
            "canonical_mailbox_key": mailbox_key,
            "request_id": trusted_request_id,
            "recipient_count": recipient_count,
            "recipients_sha256": recipients_digest,
            "subject_sha256": subject_digest,
            "content_sha256": content_digest,
        }
        if any(
            existing.get(field_name) != expected_value
            for field_name, expected_value in expected_payload_identity.items()
        ):
            raise GmailOutboundDeliveryConflict(
                "Outbound Gmail request_id was reused for different delivery content"
            )
        try:
            updated = ledger.find_one_and_update(
                {"_id": fingerprint},
                {
                    "$addToSet": {
                        "observed_profile_resource_concept_ids": resource_id,
                        "observed_profile_alias_digests": profile_alias_digest,
                    },
                    "$set": {"updated_at": timestamp},
                },
                return_document=ReturnDocument.AFTER,
            )
        except Exception as exc:
            raise GmailOutboundDeliveryLedgerUnavailable(
                "Duplicate Gmail delivery provenance could not be persisted"
            ) from exc
        if not isinstance(updated, Mapping):
            raise GmailOutboundDeliveryLedgerUnavailable(
                "Duplicate Gmail delivery provenance could not be read back"
            )
        existing = updated
        projected = _project(existing)
        return GmailOutboundDeliveryClaim(
            claimed=False,
            duplicate=True,
            delivery_fingerprint=fingerprint,
            status=str(projected.get("status") or "unknown"),
            receipt=projected,
        )
    except Exception as exc:  # fail closed before provider dispatch
        raise GmailOutboundDeliveryLedgerUnavailable(
            f"Outbound Gmail delivery claim failed: {type(exc).__name__}"
        ) from exc


def record_gmail_outbound_delivery_result(
    *,
    delivery_fingerprint: str,
    status: str,
    message_id: str | None = None,
    thread_id: str | None = None,
    receipt: Mapping[str, Any] | None = None,
    collection: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist one monotonic terminal result and return it canonically.

    Updates use compare-and-set status transitions. In particular, a canonical
    ``succeeded`` observation is absorbing: a slower worker reporting
    ``indeterminate``, ``failed``, or ``not_started`` cannot overwrite its
    verified receipt.
    """

    fingerprint = _clean_required(delivery_fingerprint, "delivery_fingerprint")
    resolved_status = str(status or "").strip().lower()
    if resolved_status not in _TERMINAL_STATUSES:
        raise ValueError(
            "status must be one of: " + ", ".join(sorted(_TERMINAL_STATUSES))
        )
    ledger = (
        collection
        if collection is not None
        else get_gmail_outbound_deliveries_collection()
    )
    if ledger is None:
        raise GmailOutboundDeliveryLedgerUnavailable(
            "Outbound Gmail delivery ledger is unavailable"
        )
    timestamp = _normalise_now(now)
    update: dict[str, Any] = {
        "status": resolved_status,
        "updated_at": timestamp,
        "receipt": dict(receipt or {}),
    }
    if isinstance(message_id, str) and message_id.strip():
        update["message_id"] = message_id.strip()
    if isinstance(thread_id, str) and thread_id.strip():
        update["thread_id"] = thread_id.strip()
    allowed_predecessors = _ALLOWED_PREDECESSOR_STATUSES[resolved_status]
    try:
        stored = ledger.find_one_and_update(
            {
                "_id": fingerprint,
                "status": {"$in": sorted(allowed_predecessors)},
            },
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
        if not isinstance(stored, Mapping):
            existing = ledger.find_one({"_id": fingerprint})
            if not isinstance(existing, Mapping):
                # Read-only compatibility for records claimed before the
                # fingerprint became the intrinsic Mongo identity.
                existing = ledger.find_one({"delivery_fingerprint": fingerprint})
                if isinstance(existing, Mapping) and existing.get("_id") is not None:
                    stored = ledger.find_one_and_update(
                        {
                            "_id": existing["_id"],
                            "status": {"$in": sorted(allowed_predecessors)},
                        },
                        {"$set": update},
                        return_document=ReturnDocument.AFTER,
                    )
                    if isinstance(stored, Mapping):
                        existing = stored
            if not isinstance(existing, Mapping):
                raise GmailOutboundDeliveryLedgerUnavailable(
                    "Outbound Gmail delivery claim disappeared before result persistence"
                )
            existing_status = str(existing.get("status") or "").strip().lower()
            if existing_status not in _KNOWN_STATUSES:
                raise GmailOutboundDeliveryLedgerUnavailable(
                    "Outbound Gmail delivery claim has an invalid finality state"
                )
            # The CAS was rejected because another worker already stored an
            # equal or stronger terminal observation. Return that canonical
            # state without modifying its status, receipt, or identifiers.
            stored = existing
    except GmailOutboundDeliveryLedgerUnavailable:
        raise
    except Exception as exc:  # effect finality must remain honest
        raise GmailOutboundDeliveryLedgerUnavailable(
            f"Outbound Gmail delivery result persistence failed: {type(exc).__name__}"
        ) from exc
    assert isinstance(stored, Mapping)
    return _project(stored)


def get_gmail_outbound_delivery(
    delivery_fingerprint: str,
    *,
    collection: Any = None,
) -> dict[str, Any] | None:
    fingerprint = _clean_required(delivery_fingerprint, "delivery_fingerprint")
    ledger = (
        collection
        if collection is not None
        else get_gmail_outbound_deliveries_collection()
    )
    if ledger is None:
        raise GmailOutboundDeliveryLedgerUnavailable(
            "Outbound Gmail delivery ledger is unavailable"
        )
    document = ledger.find_one({"_id": fingerprint})
    if not isinstance(document, Mapping):
        document = ledger.find_one({"delivery_fingerprint": fingerprint})
    return _project(document) if isinstance(document, Mapping) else None


__all__ = [
    "DELIVERY_LEDGER_SCHEMA_VERSION",
    "GmailOutboundDeliveryClaim",
    "GmailOutboundDeliveryConflict",
    "GmailOutboundDeliveryLedgerUnavailable",
    "claim_gmail_outbound_delivery",
    "get_gmail_outbound_delivery",
    "record_gmail_outbound_delivery_result",
]
