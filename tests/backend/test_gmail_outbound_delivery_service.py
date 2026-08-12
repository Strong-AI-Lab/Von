import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import mongomock
import pytest

from src.backend.services.gmail_outbound_delivery_service import (
    GmailOutboundDeliveryConflict,
    GmailOutboundDeliveryLedgerUnavailable,
    claim_gmail_outbound_delivery,
    get_gmail_outbound_delivery,
    record_gmail_outbound_delivery_result,
)
from src.backend.services.gmail_outbound_mailbox_identity_service import (
    derive_canonical_gmail_mailbox_key,
)

MAILBOX_KEY = derive_canonical_gmail_mailbox_key("von@example.test")


def _collection(*, create_secondary_unique_index=True):
    collection = mongomock.MongoClient().von_test.gmail_outbound_deliveries
    if create_secondary_unique_index:
        collection.create_index("delivery_fingerprint", unique=True)
    return collection


def _claim(collection, *, fingerprint="delivery-1"):
    return claim_gmail_outbound_delivery(
        delivery_fingerprint=fingerprint,
        canonical_mailbox_key=MAILBOX_KEY,
        profile_resource_concept_id="#V#gmail_profile_vonwitbrock_gmail",
        profile_id="vonwitbrock-gmail",
        actor_user_concept_id="#V#michael_witbrock",
        actor_organisation_concept_id="#V#strong_ai_lab",
        request_id="request-1",
        recipient_count=2,
        recipients_sha256="recipients-digest",
        subject_sha256="subject-digest",
        content_sha256="content-digest",
        collection=collection,
        now=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
    )


def test_claim_is_unique_and_duplicate_returns_existing_receipt():
    collection = _collection()

    first = _claim(collection)
    duplicate = _claim(collection)

    assert first.claimed is True
    assert first.duplicate is False
    assert duplicate.claimed is False
    assert duplicate.duplicate is True
    assert duplicate.status == "dispatch_claimed"
    assert duplicate.receipt is not None
    assert duplicate.receipt["request_id"] == "request-1"
    assert collection.count_documents({}) == 1


def test_duplicate_alias_preserves_provenance_without_changing_effect_identity():
    collection = _collection()
    _claim(collection)

    duplicate = claim_gmail_outbound_delivery(
        delivery_fingerprint="delivery-1",
        canonical_mailbox_key=MAILBOX_KEY,
        profile_resource_concept_id="#V#gmail_profile_second_alias",
        profile_id="second-runtime-alias",
        actor_user_concept_id="#V#michael_witbrock",
        actor_organisation_concept_id="#V#strong_ai_lab",
        request_id="request-1",
        recipient_count=2,
        recipients_sha256="recipients-digest",
        subject_sha256="subject-digest",
        content_sha256="content-digest",
        collection=collection,
    )

    assert duplicate.duplicate is True
    assert duplicate.receipt["profile_resource_concept_id"] == (
        "#V#gmail_profile_vonwitbrock_gmail"
    )
    assert duplicate.receipt["observed_profile_resource_concept_ids"] == [
        "#V#gmail_profile_vonwitbrock_gmail",
        "#V#gmail_profile_second_alias",
    ]
    assert duplicate.receipt["observed_profile_alias_digests"] == [
        hashlib.sha256(b"vonwitbrock-gmail").hexdigest(),
        hashlib.sha256(b"second-runtime-alias").hexdigest(),
    ]


def test_concurrent_claim_is_intrinsically_unique_without_secondary_index():
    collection = _collection(create_secondary_unique_index=False)
    worker_count = 8
    start = Barrier(worker_count)

    def claim_once():
        start.wait()
        return _claim(collection)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        claims = list(executor.map(lambda _index: claim_once(), range(worker_count)))

    assert sum(claim.claimed for claim in claims) == 1
    assert sum(claim.duplicate for claim in claims) == worker_count - 1
    assert collection.count_documents({}) == 1
    stored = collection.find_one({})
    assert stored is not None
    assert stored["_id"] == "delivery-1"
    assert stored["delivery_fingerprint"] == "delivery-1"


def test_terminal_result_is_persisted_and_read_back_without_private_content():
    collection = _collection()
    _claim(collection)

    stored = record_gmail_outbound_delivery_result(
        delivery_fingerprint="delivery-1",
        status="succeeded",
        message_id="gmail-message-1",
        thread_id="gmail-thread-1",
        receipt={
            "effect_status": "succeeded",
            "canonical_readback": {"status": "verified", "verified": True},
        },
        collection=collection,
        now=datetime(2026, 8, 12, 9, 1, tzinfo=UTC),
    )

    assert stored["status"] == "succeeded"
    assert stored["message_id"] == "gmail-message-1"
    assert stored["receipt"]["canonical_readback"]["verified"] is True
    assert "_id" not in stored
    assert "subject_sha256" not in stored
    assert "content_sha256" not in stored
    assert get_gmail_outbound_delivery(
        "delivery-1", collection=collection
    ) == stored


@pytest.mark.parametrize("later_status", ["indeterminate", "failed", "not_started"])
def test_succeeded_result_is_absorbing(later_status):
    collection = _collection(create_secondary_unique_index=False)
    _claim(collection)
    succeeded_at = datetime(2026, 8, 12, 9, 1, tzinfo=UTC)
    succeeded = record_gmail_outbound_delivery_result(
        delivery_fingerprint="delivery-1",
        status="succeeded",
        message_id="gmail-message-1",
        thread_id="gmail-thread-1",
        receipt={
            "effect_status": "succeeded",
            "canonical_readback": {"status": "verified", "verified": True},
        },
        collection=collection,
        now=succeeded_at,
    )

    observed = record_gmail_outbound_delivery_result(
        delivery_fingerprint="delivery-1",
        status=later_status,
        receipt={"effect_status": later_status, "error_code": "late-weaker-state"},
        collection=collection,
        now=datetime(2026, 8, 12, 9, 2, tzinfo=UTC),
    )

    assert observed == succeeded
    assert observed["status"] == "succeeded"
    assert observed["receipt"]["canonical_readback"]["verified"] is True
    assert "late-weaker-state" not in str(observed)


def test_succeeded_promotes_prior_indeterminate_result():
    collection = _collection(create_secondary_unique_index=False)
    _claim(collection)
    record_gmail_outbound_delivery_result(
        delivery_fingerprint="delivery-1",
        status="indeterminate",
        receipt={"effect_status": "indeterminate"},
        collection=collection,
    )

    promoted = record_gmail_outbound_delivery_result(
        delivery_fingerprint="delivery-1",
        status="succeeded",
        message_id="gmail-message-1",
        receipt={
            "effect_status": "succeeded",
            "canonical_readback": {"status": "verified", "verified": True},
        },
        collection=collection,
    )

    assert promoted["status"] == "succeeded"
    assert promoted["message_id"] == "gmail-message-1"
    assert promoted["receipt"]["canonical_readback"]["verified"] is True


def test_concurrent_succeeded_and_indeterminate_writers_finish_succeeded():
    collection = _collection(create_secondary_unique_index=False)
    _claim(collection)
    start = Barrier(2)

    def record(status):
        start.wait()
        return record_gmail_outbound_delivery_result(
            delivery_fingerprint="delivery-1",
            status=status,
            message_id=("gmail-message-1" if status == "succeeded" else None),
            receipt=(
                {
                    "effect_status": "succeeded",
                    "canonical_readback": {
                        "status": "verified",
                        "verified": True,
                    },
                }
                if status == "succeeded"
                else {
                    "effect_status": "indeterminate",
                    "error_code": "late-weaker-state",
                }
            ),
            collection=collection,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(record, ("succeeded", "indeterminate")))

    stored = get_gmail_outbound_delivery("delivery-1", collection=collection)
    assert stored is not None
    assert stored["status"] == "succeeded"
    assert stored["message_id"] == "gmail-message-1"
    assert stored["receipt"]["canonical_readback"]["verified"] is True
    assert "late-weaker-state" not in str(stored)


def test_duplicate_request_with_different_content_is_rejected():
    collection = _collection()
    _claim(collection)

    with pytest.raises(GmailOutboundDeliveryConflict):
        claim_gmail_outbound_delivery(
            delivery_fingerprint="delivery-1",
            canonical_mailbox_key=MAILBOX_KEY,
            profile_resource_concept_id="#V#gmail_profile_vonwitbrock_gmail",
            profile_id="vonwitbrock-gmail",
            actor_user_concept_id="#V#michael_witbrock",
            actor_organisation_concept_id="#V#strong_ai_lab",
            request_id="request-1",
            recipient_count=2,
            recipients_sha256="different-recipients",
            subject_sha256="subject-digest",
            content_sha256="content-digest",
            collection=collection,
        )


def test_missing_ledger_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "src.backend.services.gmail_outbound_delivery_service."
        "get_gmail_outbound_deliveries_collection",
        lambda: None,
    )

    with pytest.raises(GmailOutboundDeliveryLedgerUnavailable):
        claim_gmail_outbound_delivery(
            delivery_fingerprint="delivery-1",
            canonical_mailbox_key=MAILBOX_KEY,
            profile_resource_concept_id="#V#gmail_profile_vonwitbrock_gmail",
            profile_id="vonwitbrock-gmail",
            actor_user_concept_id="#V#michael_witbrock",
            actor_organisation_concept_id="#V#strong_ai_lab",
            request_id="request-1",
            recipient_count=1,
            recipients_sha256="recipients-digest",
            subject_sha256="subject-digest",
            content_sha256="content-digest",
        )
