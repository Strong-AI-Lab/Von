from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import mongomock
import pytest

from src.backend.services.gmail_outbound_mailbox_identity_service import (
    derive_canonical_gmail_mailbox_key,
)
from src.backend.services.gmail_outbound_rate_limit_service import (
    reserve_gmail_outbound_quota,
)
from src.backend.services.settings_service import GmailOutboundRateLimitSettings

NOW = datetime(2026, 8, 12, 9, 5, tzinfo=UTC)
CANONICAL_EMAIL = "von@example.test"
MAILBOX_KEY = derive_canonical_gmail_mailbox_key(CANONICAL_EMAIL)


def _collection():
    return mongomock.MongoClient().von_test.gmail_outbound_quota


def _policy(**overrides):
    values = {
        "enabled": True,
        "max_messages_per_10_minutes": 5,
        "max_messages_per_day": 25,
        "max_recipients_per_message": 10,
        "max_recipient_deliveries_per_day": 50,
    }
    values.update(overrides)
    return GmailOutboundRateLimitSettings(**values)


def _reserve(collection, reservation_id, *, recipient_count=1, policy=None, now=NOW):
    return reserve_gmail_outbound_quota(
        canonical_mailbox_key=MAILBOX_KEY,
        reservation_id=reservation_id,
        recipient_count=recipient_count,
        settings=policy or _policy(),
        collection=collection,
        now=now,
    )


def test_disabled_policy_and_unavailable_store_fail_closed():
    disabled = _reserve(
        None,
        "delivery-disabled",
        policy=_policy(enabled=False),
    )
    unavailable = _reserve(None, "delivery-unavailable")

    assert disabled.allowed is False
    assert disabled.code == "gmail_outbound_disabled"
    assert unavailable.allowed is False
    assert unavailable.code == "gmail_outbound_quota_store_unavailable"


def test_quota_boundary_rejects_raw_mailbox_address():
    collection = _collection()

    with pytest.raises(ValueError, match="opaque Gmail mailbox key"):
        reserve_gmail_outbound_quota(
            canonical_mailbox_key=CANONICAL_EMAIL,
            reservation_id="delivery-raw-address",
            recipient_count=1,
            settings=_policy(),
            collection=collection,
            now=NOW,
        )

    assert collection.count_documents({}) == 0


def test_one_atomic_document_counts_message_and_all_to_cc_bcc_deliveries():
    collection = _collection()

    decision = _reserve(collection, "delivery-1", recipient_count=4)

    assert decision.allowed is True
    assert decision.usage == {
        "messages_in_10_minute_window": 1,
        "messages_in_day": 1,
        "recipient_deliveries_in_day": 4,
    }
    document = collection.find_one({})
    assert document is not None
    assert document["canonical_mailbox_key"] == MAILBOX_KEY
    assert CANONICAL_EMAIL not in str(document)
    assert document["messages_in_day"] == 1
    assert document["recipient_deliveries_in_day"] == 4
    assert len(document["reservation_keys"]) == 1


def test_same_delivery_reservation_is_idempotent_and_consumes_quota_once():
    collection = _collection()

    first = _reserve(collection, "delivery-same", recipient_count=3)
    duplicate = _reserve(collection, "delivery-same", recipient_count=3)

    assert first.code == "gmail_outbound_quota_reserved"
    assert duplicate.allowed is True
    assert duplicate.code == "gmail_outbound_quota_already_reserved"
    assert duplicate.reservation_id == "delivery-same"
    assert duplicate.usage["messages_in_day"] == 1
    assert duplicate.usage["recipient_deliveries_in_day"] == 3


def test_ten_minute_limit_returns_structured_retry_information():
    collection = _collection()
    policy = _policy(max_messages_per_10_minutes=2)
    assert _reserve(collection, "delivery-1", policy=policy).allowed is True
    assert _reserve(collection, "delivery-2", policy=policy).allowed is True

    denied = _reserve(collection, "delivery-3", policy=policy)
    receipt = denied.as_dict(now=NOW)

    assert denied.allowed is False
    assert denied.code == "gmail_outbound_rate_limit_exceeded"
    assert denied.exhausted_limits == ("messages_per_10_minutes",)
    assert receipt["retry_at"] == "2026-08-12T09:10:00Z"
    assert receipt["retry_after_seconds"] == 300
    assert receipt["usage"]["messages_in_10_minute_window"] == 2


def test_daily_recipient_delivery_limit_is_separate_from_message_limit():
    collection = _collection()
    policy = _policy(
        max_messages_per_10_minutes=10,
        max_messages_per_day=10,
        max_recipients_per_message=4,
        max_recipient_deliveries_per_day=5,
    )
    assert _reserve(collection, "delivery-1", recipient_count=3, policy=policy).allowed
    assert _reserve(collection, "delivery-2", recipient_count=2, policy=policy).allowed

    denied = _reserve(collection, "delivery-3", recipient_count=1, policy=policy)

    assert denied.allowed is False
    assert denied.exhausted_limits == ("recipient_deliveries_per_day",)
    assert denied.as_dict(now=NOW)["retry_at"] == "2026-08-13T00:00:00Z"


def test_per_message_recipient_limit_does_not_touch_quota_store():
    collection = _collection()

    denied = _reserve(
        collection,
        "delivery-too-wide",
        recipient_count=3,
        policy=_policy(max_recipients_per_message=2),
    )

    assert denied.allowed is False
    assert denied.code == "gmail_outbound_recipient_limit_exceeded"
    assert collection.count_documents({}) == 0


def test_concurrent_unique_reservations_cannot_exceed_message_limit():
    collection = _collection()
    policy = _policy(max_messages_per_10_minutes=5)

    def reserve(index):
        return _reserve(collection, f"delivery-{index}", policy=policy)

    with ThreadPoolExecutor(max_workers=12) as executor:
        decisions = list(executor.map(reserve, range(30)))

    assert sum(decision.allowed for decision in decisions) == 5
    document = collection.find_one({})
    assert document is not None
    assert document["messages_in_day"] == 5


def test_concurrent_duplicate_reservations_consume_one_slot():
    collection = _collection()

    def reserve(_index):
        return _reserve(collection, "delivery-identical", recipient_count=2)

    with ThreadPoolExecutor(max_workers=12) as executor:
        decisions = list(executor.map(reserve, range(30)))

    assert all(decision.allowed for decision in decisions)
    assert (
        sum(decision.code == "gmail_outbound_quota_reserved" for decision in decisions)
        == 1
    )
    document = collection.find_one({})
    assert document is not None
    assert document["messages_in_day"] == 1
    assert document["recipient_deliveries_in_day"] == 2


def test_new_ten_minute_bucket_and_utc_day_receive_fresh_capacity():
    collection = _collection()
    policy = _policy(max_messages_per_10_minutes=1, max_messages_per_day=2)
    assert _reserve(collection, "delivery-1", policy=policy).allowed
    assert not _reserve(collection, "delivery-2", policy=policy).allowed

    next_bucket = datetime(2026, 8, 12, 9, 10, tzinfo=UTC)
    assert _reserve(
        collection,
        "delivery-2",
        policy=policy,
        now=next_bucket,
    ).allowed

    next_day = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)
    assert _reserve(
        collection,
        "delivery-3",
        policy=policy,
        now=next_day,
    ).allowed
    assert collection.count_documents({}) == 2
