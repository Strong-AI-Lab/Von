"""Durable, mailbox-wide outbound Gmail rate limiting.

The limiter uses one Mongo document per canonical Gmail mailbox key and UTC day.
Daily message and recipient-delivery counters, plus every fixed ten-minute
message bucket for that day, are incremented in one ``find_one_and_update``.
Consequently concurrent workers cannot independently pass one limit while
overshooting another.  The settings and UI are control surfaces only: every
send path must reserve here immediately before dispatching to Gmail.

The caller supplies the already-claimed delivery fingerprint as
``reservation_id``.  Its SHA-256 key is added to the same daily document in the
atomic counter update, so concurrent retries within that UTC day consume one
quota slot.  The list is bounded by the maximum 1,000 messages/day setting.
Cross-day/global duplicate suppression remains the responsibility of the
unique ``gmail_outbound_deliveries`` ledger, which callers must claim first;
duplicate non-claimants must not call this quota service or dispatch Gmail.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..db.mongo_client import get_gmail_outbound_quota_collection
from ..utils.time_utils import utc_now
from .gmail_outbound_mailbox_identity_service import (
    require_canonical_gmail_mailbox_key,
)
from .settings_service import (
    GmailOutboundRateLimitSettings,
    get_gmail_outbound_rate_limit_settings,
)

logger = logging.getLogger(__name__)

GMAIL_OUTBOUND_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
GMAIL_OUTBOUND_RATE_LIMIT_RETENTION_DAYS = 2
_DEFAULT_COLLECTION = object()


@dataclass(frozen=True)
class GmailOutboundQuotaDecision:
    """Structured result of an outbound quota reservation attempt."""

    allowed: bool
    code: str
    message: str
    policy: GmailOutboundRateLimitSettings
    usage: Mapping[str, int]
    exhausted_limits: tuple[str, ...] = ()
    retry_at: datetime | None = None
    reservation_id: str | None = None
    mailbox_key_digest: str | None = None
    daily_window_start: datetime | None = None
    daily_window_end: datetime | None = None
    short_window_start: datetime | None = None
    short_window_end: datetime | None = None

    @property
    def retry_after_seconds(self) -> int | None:
        if self.retry_at is None:
            return None
        return max(1, math.ceil((self.retry_at - utc_now()).total_seconds()))

    def as_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Return a JSON-safe, body- and recipient-free decision receipt."""

        reference_now = _normalise_utc_datetime(now or utc_now())
        retry_after_seconds = None
        if self.retry_at is not None:
            retry_after_seconds = max(
                1,
                math.ceil((self.retry_at - reference_now).total_seconds()),
            )
        return {
            "schema_version": "gmail_outbound_quota_decision.v1",
            "allowed": self.allowed,
            "code": self.code,
            "message": self.message,
            "policy": self.policy.as_dict(),
            "usage": dict(self.usage),
            "exhausted_limits": list(self.exhausted_limits),
            "retry_at": _isoformat_z(self.retry_at),
            "retry_after_seconds": retry_after_seconds,
            "reservation_id": self.reservation_id,
            "mailbox_key_digest": self.mailbox_key_digest,
            "windows": {
                "daily_start": _isoformat_z(self.daily_window_start),
                "daily_end": _isoformat_z(self.daily_window_end),
                "ten_minute_start": _isoformat_z(self.short_window_start),
                "ten_minute_end": _isoformat_z(self.short_window_end),
            },
        }


def _normalise_utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _isoformat_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _normalise_utc_datetime(value).isoformat().replace("+00:00", "Z")


def _window_bounds(now: datetime) -> tuple[datetime, datetime, datetime, datetime]:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    short_start = now.replace(
        minute=(now.minute // 10) * 10,
        second=0,
        microsecond=0,
    )
    short_end = short_start + timedelta(
        seconds=GMAIL_OUTBOUND_RATE_LIMIT_WINDOW_SECONDS
    )
    return day_start, day_end, short_start, short_end


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _nested_int(document: Mapping[str, Any], path: str) -> int:
    current: Any = document
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return 0
        current = current.get(segment)
    return _safe_int(current)


def _usage_from_document(
    document: Mapping[str, Any] | None,
    *,
    short_bucket_field: str,
) -> dict[str, int]:
    payload = document or {}
    return {
        "messages_in_10_minute_window": _nested_int(
            payload,
            f"ten_minute_buckets.{short_bucket_field}",
        ),
        "messages_in_day": _safe_int(payload.get("messages_in_day")),
        "recipient_deliveries_in_day": _safe_int(
            payload.get("recipient_deliveries_in_day")
        ),
    }


def _mailbox_key(value: Any) -> tuple[str, str]:
    canonical = require_canonical_gmail_mailbox_key(value)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, digest


def _reservation_key(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reservation_id is required")
    reservation_id = value.strip()
    if len(reservation_id) > 512:
        raise ValueError("reservation_id is too long")
    return reservation_id, hashlib.sha256(reservation_id.encode("utf-8")).hexdigest()


def _quota_filter(
    *,
    document_id: str,
    short_bucket_path: str,
    policy: GmailOutboundRateLimitSettings,
    recipient_count: int,
    reservation_key: str,
) -> dict[str, Any]:
    return {
        "_id": document_id,
        "$and": [
            {"reservation_keys": {"$ne": reservation_key}},
            {
                "$or": [
                    {"messages_in_day": {"$exists": False}},
                    {"messages_in_day": {"$lte": policy.max_messages_per_day - 1}},
                ]
            },
            {
                "$or": [
                    {"recipient_deliveries_in_day": {"$exists": False}},
                    {
                        "recipient_deliveries_in_day": {
                            "$lte": (
                                policy.max_recipient_deliveries_per_day
                                - recipient_count
                            )
                        }
                    },
                ]
            },
            {
                "$or": [
                    {short_bucket_path: {"$exists": False}},
                    {
                        short_bucket_path: {
                            "$lte": policy.max_messages_per_10_minutes - 1
                        }
                    },
                ]
            },
        ],
    }


def _quota_update(
    *,
    canonical_mailbox_key: str,
    mailbox_key_digest: str,
    day_start: datetime,
    day_end: datetime,
    short_start: datetime,
    short_bucket_path: str,
    recipient_count: int,
    reservation_key: str,
    now: datetime,
) -> dict[str, Any]:
    return {
        "$setOnInsert": {
            "canonical_mailbox_key": canonical_mailbox_key,
            "mailbox_key_digest": mailbox_key_digest,
            "daily_window_start": day_start,
            "daily_window_end": day_end,
            "created_at": now,
        },
        "$set": {
            "updated_at": now,
            "expires_at": day_end
            + timedelta(days=GMAIL_OUTBOUND_RATE_LIMIT_RETENTION_DAYS),
            f"ten_minute_window_starts.{short_bucket_path.rsplit('.', 1)[-1]}": (
                short_start
            ),
        },
        "$inc": {
            "messages_in_day": 1,
            "recipient_deliveries_in_day": recipient_count,
            short_bucket_path: 1,
        },
        "$addToSet": {"reservation_keys": reservation_key},
    }


def _rate_limit_decision(
    *,
    policy: GmailOutboundRateLimitSettings,
    document: Mapping[str, Any] | None,
    recipient_count: int,
    reservation_id: str,
    reservation_key: str,
    mailbox_key_digest: str,
    short_bucket_field: str,
    day_start: datetime,
    day_end: datetime,
    short_start: datetime,
    short_end: datetime,
) -> GmailOutboundQuotaDecision:
    usage = _usage_from_document(
        document,
        short_bucket_field=short_bucket_field,
    )
    existing_reservations = (
        document.get("reservation_keys") if isinstance(document, Mapping) else None
    )
    if isinstance(existing_reservations, list) and reservation_key in (
        str(item) for item in existing_reservations
    ):
        return GmailOutboundQuotaDecision(
            allowed=True,
            code="gmail_outbound_quota_already_reserved",
            message="Outbound Gmail quota was already reserved for this delivery.",
            policy=policy,
            usage=usage,
            reservation_id=reservation_id,
            mailbox_key_digest=mailbox_key_digest,
            daily_window_start=day_start,
            daily_window_end=day_end,
            short_window_start=short_start,
            short_window_end=short_end,
        )
    exhausted: list[str] = []
    retry_candidates: list[datetime] = []
    if usage["messages_in_10_minute_window"] + 1 > (policy.max_messages_per_10_minutes):
        exhausted.append("messages_per_10_minutes")
        retry_candidates.append(short_end)
    if usage["messages_in_day"] + 1 > policy.max_messages_per_day:
        exhausted.append("messages_per_day")
        retry_candidates.append(day_end)
    if usage["recipient_deliveries_in_day"] + recipient_count > (
        policy.max_recipient_deliveries_per_day
    ):
        exhausted.append("recipient_deliveries_per_day")
        retry_candidates.append(day_end)

    if not exhausted:
        return GmailOutboundQuotaDecision(
            allowed=False,
            code="gmail_outbound_quota_store_unavailable",
            message=(
                "Outbound email was not sent because the durable quota reservation "
                "could not be confirmed."
            ),
            policy=policy,
            usage=usage,
            mailbox_key_digest=mailbox_key_digest,
            daily_window_start=day_start,
            daily_window_end=day_end,
            short_window_start=short_start,
            short_window_end=short_end,
        )

    retry_at = max(retry_candidates)
    return GmailOutboundQuotaDecision(
        allowed=False,
        code="gmail_outbound_rate_limit_exceeded",
        message="Outbound email was not sent because its mailbox quota is exhausted.",
        policy=policy,
        usage=usage,
        exhausted_limits=tuple(exhausted),
        retry_at=retry_at,
        mailbox_key_digest=mailbox_key_digest,
        daily_window_start=day_start,
        daily_window_end=day_end,
        short_window_start=short_start,
        short_window_end=short_end,
    )


def reserve_gmail_outbound_quota(
    *,
    canonical_mailbox_key: str,
    reservation_id: str,
    recipient_count: int,
    settings: GmailOutboundRateLimitSettings | None = None,
    collection: Any = _DEFAULT_COLLECTION,
    now: datetime | None = None,
) -> GmailOutboundQuotaDecision:
    """Atomically reserve one message and all recipient deliveries.

    ``canonical_mailbox_key`` must be the privacy-preserving stable key derived
    from Gmail ``users.getProfile`` by the trusted send boundary. It must not
    come from model output, an unverified client alias, or a represented profile
    resource id. ``recipient_count`` must include To,
    Cc and Bcc deliveries after address normalisation.  ``reservation_id`` is
    the delivery-ledger fingerprint already claimed by this caller; repeats in
    the same UTC-day document return ``gmail_outbound_quota_already_reserved``
    without changing either counter.

    Expected policy denials and persistence failures return structured denied
    decisions.  Invalid programmer inputs raise ``ValueError`` before storage.
    """

    canonical_mailbox_key, mailbox_key_digest = _mailbox_key(
        canonical_mailbox_key
    )
    reservation_id, reservation_key = _reservation_key(reservation_id)
    if isinstance(recipient_count, bool) or not isinstance(recipient_count, int):
        raise TypeError("recipient_count must be an integer")
    if recipient_count < 1:
        raise ValueError("recipient_count must be at least 1")

    policy = settings or get_gmail_outbound_rate_limit_settings()
    if not isinstance(policy, GmailOutboundRateLimitSettings):
        raise TypeError("settings must be GmailOutboundRateLimitSettings")

    effective_now = _normalise_utc_datetime(now or utc_now())
    day_start, day_end, short_start, short_end = _window_bounds(effective_now)
    short_bucket_field = "b" + short_start.strftime("%Y%m%dT%H%MZ")
    short_bucket_path = f"ten_minute_buckets.{short_bucket_field}"
    usage = {
        "messages_in_10_minute_window": 0,
        "messages_in_day": 0,
        "recipient_deliveries_in_day": 0,
    }

    common_decision_fields = {
        "policy": policy,
        "usage": usage,
        "mailbox_key_digest": mailbox_key_digest,
        "daily_window_start": day_start,
        "daily_window_end": day_end,
        "short_window_start": short_start,
        "short_window_end": short_end,
    }
    if not policy.enabled:
        return GmailOutboundQuotaDecision(
            allowed=False,
            code="gmail_outbound_disabled",
            message="Outbound email is disabled by the administrator setting.",
            **common_decision_fields,
        )
    if recipient_count > policy.max_recipients_per_message:
        return GmailOutboundQuotaDecision(
            allowed=False,
            code="gmail_outbound_recipient_limit_exceeded",
            message=(
                "Outbound email was not sent because the message has more "
                "recipients than the configured per-message limit."
            ),
            exhausted_limits=("recipients_per_message",),
            **common_decision_fields,
        )

    quota_collection = (
        get_gmail_outbound_quota_collection()
        if collection is _DEFAULT_COLLECTION
        else collection
    )
    if quota_collection is None:
        return GmailOutboundQuotaDecision(
            allowed=False,
            code="gmail_outbound_quota_store_unavailable",
            message=(
                "Outbound email was not sent because the durable quota store is "
                "unavailable."
            ),
            **common_decision_fields,
        )

    document_id = (
        f"gmail-outbound:{mailbox_key_digest}:{day_start.strftime('%Y-%m-%d')}"
    )
    query = _quota_filter(
        document_id=document_id,
        short_bucket_path=short_bucket_path,
        policy=policy,
        recipient_count=recipient_count,
        reservation_key=reservation_key,
    )
    update = _quota_update(
        canonical_mailbox_key=canonical_mailbox_key,
        mailbox_key_digest=mailbox_key_digest,
        day_start=day_start,
        day_end=day_end,
        short_start=short_start,
        short_bucket_path=short_bucket_path,
        recipient_count=recipient_count,
        reservation_key=reservation_key,
        now=effective_now,
    )

    try:
        try:
            document = quota_collection.find_one_and_update(
                query,
                update,
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            # Two first reservations can race to upsert the daily document.  A
            # non-upserting retry still applies the same atomic limit filter.
            # It also turns the duplicate-key signal used by a capped existing
            # document into a normal no-match decision.
            document = quota_collection.find_one_and_update(
                query,
                update,
                upsert=False,
                return_document=ReturnDocument.AFTER,
            )
        if document is None:
            current_document = quota_collection.find_one({"_id": document_id})
            return _rate_limit_decision(
                policy=policy,
                document=current_document,
                recipient_count=recipient_count,
                reservation_id=reservation_id,
                reservation_key=reservation_key,
                mailbox_key_digest=mailbox_key_digest,
                short_bucket_field=short_bucket_field,
                day_start=day_start,
                day_end=day_end,
                short_start=short_start,
                short_end=short_end,
            )
    except Exception as exc:  # noqa: BLE001 - fail closed before Gmail dispatch
        logger.error(
            "Outbound Gmail quota reservation failed for profile digest %s: %s",
            mailbox_key_digest[:12],
            type(exc).__name__,
        )
        return GmailOutboundQuotaDecision(
            allowed=False,
            code="gmail_outbound_quota_store_unavailable",
            message=(
                "Outbound email was not sent because the durable quota reservation "
                "failed."
            ),
            **common_decision_fields,
        )

    post_usage = _usage_from_document(
        document,
        short_bucket_field=short_bucket_field,
    )
    return GmailOutboundQuotaDecision(
        allowed=True,
        code="gmail_outbound_quota_reserved",
        message="Outbound Gmail quota reserved.",
        policy=policy,
        usage=post_usage,
        reservation_id=reservation_id,
        mailbox_key_digest=mailbox_key_digest,
        daily_window_start=day_start,
        daily_window_end=day_end,
        short_window_start=short_start,
        short_window_end=short_end,
    )


__all__ = [
    "GMAIL_OUTBOUND_RATE_LIMIT_WINDOW_SECONDS",
    "GmailOutboundQuotaDecision",
    "reserve_gmail_outbound_quota",
]
