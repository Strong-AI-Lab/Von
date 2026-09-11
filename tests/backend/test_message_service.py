"""Tests for message service (JVNAUTOSCI-1071).

Unit tests for message creation, retrieval, and management.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pymongo.errors import DuplicateKeyError

from src.backend.security.visibility_predicates import (
    CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
    CANONICAL_SPECIFIC_TO_USER_PREDICATE,
)
from src.backend.services.message_service import (
    DIRECT_MESSAGE_DELIVERY_FINGERPRINT_FIELD,
    DIRECT_MESSAGE_IDEMPOTENCY_DOCUMENT_ID_PREFIX,
    DIRECT_MESSAGE_IDEMPOTENCY_SCOPE_FIELD,
    DIRECT_MESSAGE_PAYLOAD_FINGERPRINT_FIELD,
    MESSAGE_STATUS_READ,
    MESSAGE_STATUS_SENT,
    MESSAGE_TYPE_CONCEPT_ID,
    PREDICATE_RECIPIENT,
    PREDICATE_SENDER,
    DirectMessageIdempotencyConflict,
    build_direct_message_delivery_identity,
    create_message,
    create_message_idempotently,
    delete_message,
    get_message,
    get_message_for_user,
    get_messages_for_user,
    get_unread_count,
    mark_message_read,
)


class TestMessageConstants:
    """Test message service constants."""

    def test_message_type_id(self) -> None:
        """MESSAGE_TYPE_CONCEPT_ID should be the expected value."""
        assert MESSAGE_TYPE_CONCEPT_ID == "#V#direct_message"

    def test_status_constants(self) -> None:
        """Status constants should match expected strings."""
        assert MESSAGE_STATUS_SENT == "sent"
        assert MESSAGE_STATUS_READ == "read"

    def test_predicate_constants(self) -> None:
        """Predicate constants should be correct."""
        assert PREDICATE_SENDER == "#V#has_sender"
        assert PREDICATE_RECIPIENT == "#V#has_recipient"


class TestCreateMessage:
    """Tests for create_message function."""

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.ConceptsRepository")
    @patch("src.backend.services.message_service.upsert_text_for_concept")
    @patch("src.backend.services.message_service.maybe_launch_direct_message_workflow")
    def test_create_message_with_single_recipient(
        self,
        mock_launch_workflow: MagicMock,
        mock_upsert: MagicMock,
        mock_repo: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """create_message() with single recipient should work."""
        mock_get_coll.return_value = MagicMock()
        mock_repo.insert_one.return_value = None

        result = create_message(
            sender_id="#V#user_alice",
            recipient_ids=["#V#user_bob"],
            content="Hello Bob!",
        )

        assert result["concept_id"].startswith("#V#message_")
        assert (
            "#V#user_alice"
            in result["relationships"][CANONICAL_SPECIFIC_TO_USER_PREDICATE]
        )
        assert (
            "#V#user_bob"
            in result["relationships"][CANONICAL_SPECIFIC_TO_USER_PREDICATE]
        )
        assert result["relationships"][PREDICATE_SENDER] == ["#V#user_alice"]
        assert result["relationships"][PREDICATE_RECIPIENT] == ["#V#user_bob"]
        assert result["concept_data"]["content_fallback"] == "Hello Bob!"
        assert (
            result["concept_data"]["attribution"]
            == "Sent by Von on behalf of #V#user_alice"
        )
        mock_launch_workflow.assert_called_once()

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.ConceptsRepository")
    @patch("src.backend.services.message_service.upsert_text_for_concept")
    @patch("src.backend.services.message_service.maybe_launch_direct_message_workflow")
    def test_create_message_with_multiple_recipients(
        self,
        mock_launch_workflow: MagicMock,
        mock_upsert: MagicMock,
        mock_repo: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """create_message() with multiple recipients should include all in visibility."""
        mock_get_coll.return_value = MagicMock()
        mock_repo.insert_one.return_value = None

        result = create_message(
            sender_id="#V#user_alice",
            recipient_ids=["#V#user_bob", "#V#user_charlie"],
            content="Hello everyone!",
        )

        visibility = result["relationships"][CANONICAL_SPECIFIC_TO_USER_PREDICATE]
        assert "#V#user_alice" in visibility
        assert "#V#user_bob" in visibility
        assert "#V#user_charlie" in visibility
        mock_launch_workflow.assert_called_once()

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.ConceptsRepository")
    @patch("src.backend.services.message_service.upsert_text_for_concept")
    @patch("src.backend.services.message_service.maybe_launch_direct_message_workflow")
    def test_create_message_keeps_org_as_provenance_not_visibility(
        self,
        mock_launch_workflow: MagicMock,
        mock_upsert: MagicMock,
        mock_repo: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """Organisation context must not reveal a direct message to nonparticipants."""
        mock_get_coll.return_value = MagicMock()
        mock_repo.insert_one.return_value = None

        result = create_message(
            sender_id="#V#user_alice",
            recipient_ids=["#V#user_bob"],
            content="Org-scoped message",
            org_id="#V#nao_institute",
        )

        assert result["concept_data"]["organisation_concept_id"] == "#V#nao_institute"
        assert CANONICAL_SPECIFIC_TO_ORG_PREDICATE not in result["relationships"]
        mock_launch_workflow.assert_called_once()

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.ConceptsRepository")
    @patch("src.backend.services.message_service.upsert_text_for_concept")
    @patch("src.backend.services.message_service.maybe_launch_direct_message_workflow")
    def test_create_message_with_subject(
        self,
        mock_launch_workflow: MagicMock,
        mock_upsert: MagicMock,
        mock_repo: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """create_message() with subject should store it in concept_data."""
        mock_get_coll.return_value = MagicMock()
        mock_repo.insert_one.return_value = None

        result = create_message(
            sender_id="#V#user_alice",
            recipient_ids=["#V#user_bob"],
            content="Message with subject",
            subject="Important Update",
        )

        assert result["concept_data"]["subject"] == "Important Update"
        mock_launch_workflow.assert_called_once()

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.ConceptsRepository")
    @patch("src.backend.services.message_service.upsert_text_for_concept")
    @patch("src.backend.services.message_service.maybe_launch_direct_message_workflow")
    def test_create_message_uses_metadata_attribution_when_provided(
        self,
        mock_launch_workflow: MagicMock,
        mock_upsert: MagicMock,
        mock_repo: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """create_message() should keep explicit attribution metadata."""
        mock_get_coll.return_value = MagicMock()
        mock_repo.insert_one.return_value = None

        result = create_message(
            sender_id="#V#user_alice",
            recipient_ids=["#V#user_bob"],
            content="Attribution override",
            metadata={"attribution": "Sent by Von on behalf of Alice"},
        )

        assert result["concept_data"]["attribution"] == "Sent by Von on behalf of Alice"
        assert result["concept_data"]["metadata"]["attribution"] == (
            "Sent by Von on behalf of Alice"
        )
        mock_launch_workflow.assert_called_once()

    def test_create_message_empty_sender(self) -> None:
        """create_message() with empty sender should raise ValueError."""
        with pytest.raises(ValueError, match="sender_id is required"):
            create_message(
                sender_id="",
                recipient_ids=["#V#user_bob"],
                content="Hello",
            )

    def test_create_message_empty_recipients(self) -> None:
        """create_message() with empty recipients should raise ValueError."""
        with pytest.raises(ValueError, match="At least one recipient"):
            create_message(
                sender_id="#V#user_alice",
                recipient_ids=[],
                content="Hello",
            )

    def test_create_message_empty_content(self) -> None:
        """create_message() with empty content should raise ValueError."""
        with pytest.raises(ValueError, match="content cannot be empty"):
            create_message(
                sender_id="#V#user_alice",
                recipient_ids=["#V#user_bob"],
                content="   ",
            )

    def test_create_message_rejects_unbound_server_document_identity(self) -> None:
        with pytest.raises(
            ValueError,
            match="Invalid server-owned direct-message document identity",
        ):
            create_message(
                sender_id="#V#user_alice",
                recipient_ids=["#V#user_bob"],
                content="Hello",
                metadata={DIRECT_MESSAGE_IDEMPOTENCY_SCOPE_FIELD: "a" * 64},
                _server_owned_document_id=(
                    DIRECT_MESSAGE_IDEMPOTENCY_DOCUMENT_ID_PREFIX + ("b" * 64)
                ),
            )


class TestIdempotentMessageCreate:
    """Tests for sender-scoped REST delivery retry reconciliation."""

    @staticmethod
    def _identity(**overrides):
        arguments = {
            "delivery_idempotency_key": "browser-send-1",
            "sender_id": "#V#user_alice",
            "organisation_concept_id": "#V#org_test",
            "recipient_ids": ["#V#user_bob", "#V#user_charlie"],
            "content": "Please review this.",
            "subject": "Review",
        }
        arguments.update(overrides)
        return build_direct_message_delivery_identity(**arguments)

    @staticmethod
    def _message_for_identity(identity):
        return {
            "_id": identity.document_id,
            "concept_id": "#V#message_winner",
            "relationships": {
                "is_an_instance_of": [MESSAGE_TYPE_CONCEPT_ID],
                PREDICATE_SENDER: ["#V#user_alice"],
                PREDICATE_RECIPIENT: ["#V#user_bob", "#V#user_charlie"],
            },
            "concept_data": {
                "organisation_concept_id": "#V#org_test",
                "sent_at": "2026-08-28T05:00:00+00:00",
                "metadata": {
                    DIRECT_MESSAGE_IDEMPOTENCY_SCOPE_FIELD: (
                        identity.idempotency_scope
                    ),
                    DIRECT_MESSAGE_PAYLOAD_FINGERPRINT_FIELD: (
                        identity.payload_fingerprint
                    ),
                    DIRECT_MESSAGE_DELIVERY_FINGERPRINT_FIELD: (
                        identity.delivery_fingerprint
                    ),
                },
            },
        }

    def test_identity_is_sender_scoped_and_payload_binds_org_recipients_content(
        self,
    ) -> None:
        baseline = self._identity()
        reordered_recipients = self._identity(
            recipient_ids=["#V#user_charlie", "#V#user_bob"]
        )
        changed_sender = self._identity(sender_id="#V#user_dana")
        changed_org = self._identity(organisation_concept_id="#V#org_other")
        changed_recipients = self._identity(recipient_ids=["#V#user_bob"])
        changed_content = self._identity(content="A materially different message")
        changed_metadata = self._identity(metadata={"intent": "action_required"})

        assert reordered_recipients == baseline
        assert changed_sender.idempotency_scope != baseline.idempotency_scope
        assert changed_org.idempotency_scope == baseline.idempotency_scope
        assert changed_recipients.idempotency_scope == baseline.idempotency_scope
        assert changed_content.idempotency_scope == baseline.idempotency_scope
        assert changed_metadata.idempotency_scope == baseline.idempotency_scope
        assert changed_org.payload_fingerprint != baseline.payload_fingerprint
        assert changed_recipients.payload_fingerprint != baseline.payload_fingerprint
        assert changed_content.payload_fingerprint != baseline.payload_fingerprint
        assert changed_metadata.payload_fingerprint != baseline.payload_fingerprint

    @patch("src.backend.services.message_service.create_message")
    @patch(
        "src.backend.services.message_service."
        "get_message_for_delivery_idempotency_scope"
    )
    def test_first_delivery_persists_server_owned_fingerprints(
        self,
        mock_get_prior: MagicMock,
        mock_create: MagicMock,
    ) -> None:
        mock_get_prior.return_value = None
        mock_create.side_effect = lambda **kwargs: {
            "concept_id": "#V#message_created",
            "concept_data": {"metadata": kwargs["metadata"]},
        }

        receipt = create_message_idempotently(
            delivery_idempotency_key="browser-send-1",
            sender_id="#V#user_alice",
            organisation_concept_id="#V#org_test",
            recipient_ids=["#V#user_bob", "#V#user_charlie"],
            content="Please review this.",
            subject="Review",
            metadata={
                "intent": "review_request",
                DIRECT_MESSAGE_IDEMPOTENCY_SCOPE_FIELD: "client-spoofed",
            },
        )

        assert receipt.reused is False
        persisted_metadata = mock_create.call_args.kwargs["metadata"]
        assert persisted_metadata["intent"] == "review_request"
        assert persisted_metadata[DIRECT_MESSAGE_IDEMPOTENCY_SCOPE_FIELD] == (
            receipt.delivery_identity.idempotency_scope
        )
        assert persisted_metadata[DIRECT_MESSAGE_PAYLOAD_FINGERPRINT_FIELD] == (
            receipt.delivery_identity.payload_fingerprint
        )
        assert persisted_metadata[DIRECT_MESSAGE_DELIVERY_FINGERPRINT_FIELD] == (
            receipt.delivery_identity.delivery_fingerprint
        )
        assert mock_create.call_args.kwargs["_server_owned_document_id"] == (
            receipt.delivery_identity.document_id
        )

    @patch("src.backend.services.message_service.create_message")
    @patch(
        "src.backend.services.message_service."
        "get_message_for_delivery_idempotency_scope"
    )
    def test_duplicate_key_race_reuses_atomic_winner(
        self,
        mock_get_prior: MagicMock,
        mock_create: MagicMock,
    ) -> None:
        identity = self._identity()
        winner = self._message_for_identity(identity)
        mock_get_prior.side_effect = [None, winner]
        mock_create.side_effect = DuplicateKeyError("duplicate retry scope")

        receipt = create_message_idempotently(
            delivery_idempotency_key="browser-send-1",
            sender_id="#V#user_alice",
            organisation_concept_id="#V#org_test",
            recipient_ids=["#V#user_bob", "#V#user_charlie"],
            content="Please review this.",
            subject="Review",
        )

        assert receipt.reused is True
        assert receipt.message["concept_id"] == "#V#message_winner"
        assert mock_get_prior.call_count == 2
        assert mock_create.call_args.kwargs["_server_owned_document_id"] == (
            identity.document_id
        )

    @patch("src.backend.services.message_service.create_message")
    @patch(
        "src.backend.services.message_service."
        "get_message_for_delivery_idempotency_scope"
    )
    def test_duplicate_document_id_race_with_changed_payload_conflicts(
        self,
        mock_get_prior: MagicMock,
        mock_create: MagicMock,
    ) -> None:
        winning_identity = self._identity(content="Winning payload")
        winner = self._message_for_identity(winning_identity)
        mock_get_prior.side_effect = [None, winner]
        mock_create.side_effect = DuplicateKeyError("duplicate built-in _id")

        with pytest.raises(
            DirectMessageIdempotencyConflict,
            match="different message payload",
        ):
            create_message_idempotently(
                delivery_idempotency_key="browser-send-1",
                sender_id="#V#user_alice",
                organisation_concept_id="#V#org_test",
                recipient_ids=["#V#user_bob", "#V#user_charlie"],
                content="Losing changed payload",
                subject="Review",
            )

        losing_identity = self._identity(content="Losing changed payload")
        assert mock_create.call_args.kwargs["_server_owned_document_id"] == (
            losing_identity.document_id
        )
        assert losing_identity.document_id == winning_identity.document_id

    @patch("src.backend.services.message_service.create_message")
    @patch(
        "src.backend.services.message_service."
        "get_message_for_delivery_idempotency_scope"
    )
    def test_same_sender_key_with_changed_material_payload_is_rejected(
        self,
        mock_get_prior: MagicMock,
        mock_create: MagicMock,
    ) -> None:
        existing_identity = self._identity()
        mock_get_prior.return_value = self._message_for_identity(existing_identity)
        baseline_arguments = {
            "delivery_idempotency_key": "browser-send-1",
            "sender_id": "#V#user_alice",
            "organisation_concept_id": "#V#org_test",
            "recipient_ids": ["#V#user_bob", "#V#user_charlie"],
            "content": "Please review this.",
            "subject": "Review",
        }

        for changed_fields in (
            {"organisation_concept_id": "#V#org_other"},
            {"recipient_ids": ["#V#user_bob"]},
            {"content": "Changed message"},
            {"metadata": {"intent": "action_required"}},
        ):
            arguments = {**baseline_arguments, **changed_fields}
            with pytest.raises(
                DirectMessageIdempotencyConflict,
                match="different message payload",
            ):
                create_message_idempotently(
                    **arguments,
                )

        mock_create.assert_not_called()

    @patch("src.backend.services.message_service.create_message")
    @patch(
        "src.backend.services.message_service."
        "get_message_for_delivery_idempotency_scope"
    )
    def test_same_client_key_is_isolated_between_trusted_senders(
        self,
        mock_get_prior: MagicMock,
        mock_create: MagicMock,
    ) -> None:
        mock_get_prior.return_value = None
        mock_create.side_effect = lambda **kwargs: {
            "concept_id": "#V#message_dana",
            "concept_data": {"metadata": kwargs["metadata"]},
        }

        receipt = create_message_idempotently(
            delivery_idempotency_key="browser-send-1",
            sender_id="#V#user_dana",
            organisation_concept_id="#V#org_test",
            recipient_ids=["#V#user_bob", "#V#user_charlie"],
            content="Please review this.",
            subject="Review",
        )

        assert receipt.reused is False
        assert mock_get_prior.call_args.kwargs["sender_id"] == "#V#user_dana"
        assert receipt.delivery_identity.idempotency_scope != (
            self._identity().idempotency_scope
        )

    def test_builtin_document_id_concurrent_race_works_without_custom_index(
        self,
        monkeypatch,
    ) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor

        import mongomock

        from src.backend.services import message_service as message_service_module

        collection = mongomock.MongoClient().von_test.concepts
        assert "direct_message_idempotency_scope_unique" not in (
            collection.index_information()
        )
        monkeypatch.setattr(
            message_service_module,
            "get_concepts_collection",
            lambda: collection,
        )
        monkeypatch.setattr(
            message_service_module,
            "apply_concept_query_filter",
            lambda query: query,
        )
        monkeypatch.setattr(
            message_service_module.ConceptsRepository,
            "insert_one",
            lambda document: collection.insert_one(dict(document)),
        )
        monkeypatch.setattr(
            message_service_module,
            "upsert_text_for_concept",
            lambda **_kwargs: {},
        )
        monkeypatch.setattr(
            message_service_module,
            "maybe_launch_direct_message_workflow",
            lambda **_kwargs: {"triggered": False},
        )
        original_get = message_service_module.get_message_for_delivery_idempotency_scope
        first_reads_lock = threading.Lock()
        first_reads_barrier = threading.Barrier(2)
        first_read_count = 0

        def force_both_concurrent_pre_reads_to_miss(**kwargs):
            nonlocal first_read_count
            with first_reads_lock:
                is_initial_read = first_read_count < 2
                if is_initial_read:
                    first_read_count += 1
            if is_initial_read:
                first_reads_barrier.wait(timeout=2)
                return None
            return original_get(**kwargs)

        monkeypatch.setattr(
            message_service_module,
            "get_message_for_delivery_idempotency_scope",
            force_both_concurrent_pre_reads_to_miss,
        )

        arguments = {
            "delivery_idempotency_key": "browser-send-no-custom-index",
            "sender_id": "#V#user_alice",
            "organisation_concept_id": "#V#org_test",
            "recipient_ids": ["#V#user_bob"],
            "content": "One durable message",
            "metadata": {"intent": "info"},
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(
                executor.map(
                    lambda _index: create_message_idempotently(**arguments),
                    range(2),
                )
            )

        assert sorted(receipt.reused for receipt in receipts) == [False, True]
        assert len({receipt.message["concept_id"] for receipt in receipts}) == 1
        assert receipts[0].message["concept_id"].startswith("#V#message_user_alice_")
        assert collection.count_documents({}) == 1
        assert "direct_message_idempotency_scope_unique" not in (
            collection.index_information()
        )
        stored = collection.find_one({})
        assert stored is not None
        assert stored["_id"] == receipts[0].delivery_identity.document_id


class TestGetMessage:
    """Tests for get_message function."""

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.apply_concept_query_filter")
    def test_get_message_found(
        self,
        mock_filter: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """get_message() should return message when found."""
        mock_coll = MagicMock()
        mock_get_coll.return_value = mock_coll
        mock_filter.return_value = {"concept_id": "#V#message_abc"}
        mock_coll.find_one.return_value = {
            "concept_id": "#V#message_abc",
            "relationships": {
                PREDICATE_SENDER: ["#V#user_alice"],
                PREDICATE_RECIPIENT: ["#V#user_bob"],
            },
        }

        result = get_message("#V#message_abc")

        assert result is not None
        assert result["concept_id"] == "#V#message_abc"

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.apply_concept_query_filter")
    def test_get_message_not_found(
        self,
        mock_filter: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """get_message() should return None when not found."""
        mock_coll = MagicMock()
        mock_get_coll.return_value = mock_coll
        mock_filter.return_value = {"concept_id": "#V#nonexistent"}
        mock_coll.find_one.return_value = None

        result = get_message("#V#nonexistent")

        assert result is None

    @patch("src.backend.services.message_service.get_concepts_collection")
    def test_get_message_empty_id(
        self,
        mock_get_coll: MagicMock,
    ) -> None:
        """get_message() with empty ID should return None."""
        result = get_message("")
        assert result is None

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.apply_concept_query_filter")
    def test_get_message_for_user_requires_sender_or_recipient(
        self,
        mock_filter: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        mock_coll = MagicMock()
        mock_get_coll.return_value = mock_coll
        mock_filter.side_effect = lambda query: query
        mock_coll.find_one.return_value = None

        result = get_message_for_user("#V#message_abc", "#V#user_charlie")

        assert result is None
        query = mock_coll.find_one.call_args.args[0]
        assert query["relationships.is_an_instance_of"] == MESSAGE_TYPE_CONCEPT_ID
        assert query["$or"] == [
            {f"relationships.{PREDICATE_SENDER}": "#V#user_charlie"},
            {f"relationships.{PREDICATE_RECIPIENT}": "#V#user_charlie"},
        ]

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.apply_concept_query_filter")
    def test_idempotency_lookup_uses_builtin_document_id_not_custom_index(
        self,
        mock_filter: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        from src.backend.services.message_service import (
            get_message_for_delivery_idempotency_scope,
        )

        collection = MagicMock()
        mock_get_coll.return_value = collection
        mock_filter.side_effect = lambda query: query
        collection.find_one.return_value = None

        result = get_message_for_delivery_idempotency_scope(
            sender_id="#V#user_alice",
            idempotency_scope="a" * 64,
        )

        assert result is None
        query = collection.find_one.call_args.args[0]
        assert query["_id"] == (
            f"{DIRECT_MESSAGE_IDEMPOTENCY_DOCUMENT_ID_PREFIX}{'a' * 64}"
        )
        assert query[f"relationships.{PREDICATE_SENDER}"] == "#V#user_alice"
        assert "concept_data.metadata.delivery_idempotency_scope" not in query


class TestGetMessagesForUser:
    """Tests for get_messages_for_user function."""

    def test_counterpart_filter_preserves_actor_mailbox(self, monkeypatch):
        import mongomock
        from src.backend.services import message_service as service

        collection = mongomock.MongoClient().von_test.concepts
        for ident, sender, recipient in [
            ("out", "alice", "bob"),
            ("in", "bob", "alice"),
            ("private", "bob", "charlie"),
            ("other", "alice", "charlie"),
        ]:
            collection.insert_one(
                {
                    "concept_id": ident,
                    "relationships": {
                        "is_an_instance_of": [service.MESSAGE_TYPE_CONCEPT_ID],
                        service.PREDICATE_SENDER: [f"#V#{sender}"],
                        service.PREDICATE_RECIPIENT: [f"#V#{recipient}"],
                    },
                }
            )
        monkeypatch.setattr(service, "get_concepts_collection", lambda: collection)
        monkeypatch.setattr(service, "apply_concept_query_filter", lambda query: query)
        assert {
            row["concept_id"]
            for row in service.get_messages_for_user("#V#alice", other_user_id="#V#bob")
        } == {"in", "out"}
        assert {
            row["concept_id"]
            for row in service.get_messages_for_user(
                "#V#alice", include_sent=False, other_user_id="#V#bob"
            )
        } == {"in"}

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.apply_concept_query_filter")
    def test_get_messages_sent_and_received(
        self,
        mock_filter: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """get_messages_for_user() with defaults should get sent and received."""
        mock_coll = MagicMock()
        mock_get_coll.return_value = mock_coll
        mock_filter.return_value = {}

        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.skip.return_value = mock_cursor
        mock_cursor.limit.return_value = mock_cursor
        mock_cursor.__iter__ = lambda self: iter(
            [
                {"concept_id": "#V#message_1"},
                {"concept_id": "#V#message_2"},
            ]
        )
        mock_coll.find.return_value = mock_cursor

        result = get_messages_for_user("#V#user_alice")

        assert len(result) == 2
        # Verify the query includes $or for sent and received
        call_args = mock_filter.call_args[0][0]
        assert "$or" in call_args

    @patch("src.backend.services.message_service.get_concepts_collection")
    def test_get_messages_empty_user(
        self,
        mock_get_coll: MagicMock,
    ) -> None:
        """get_messages_for_user() with empty user should return empty list."""
        result = get_messages_for_user("")
        assert result == []


class TestMarkMessageRead:
    """Tests for mark_message_read function."""

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.apply_concept_query_filter")
    def test_mark_message_read_success(
        self,
        mock_filter: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """mark_message_read() should update message when found."""
        mock_coll = MagicMock()
        mock_get_coll.return_value = mock_coll
        mock_filter.return_value = {"concept_id": "#V#message_abc"}
        mock_coll.update_one.return_value = MagicMock(modified_count=1)

        result = mark_message_read("#V#message_abc", "#V#user_bob")

        assert result is True
        base_filter = mock_filter.call_args.args[0]
        assert base_filter[f"relationships.{PREDICATE_RECIPIENT}"] == "#V#user_bob"

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.apply_concept_query_filter")
    def test_mark_message_read_not_found(
        self,
        mock_filter: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """mark_message_read() should return False when message not found."""
        mock_coll = MagicMock()
        mock_get_coll.return_value = mock_coll
        mock_filter.return_value = {"concept_id": "#V#nonexistent"}
        mock_coll.update_one.return_value = MagicMock(modified_count=0)

        result = mark_message_read("#V#nonexistent", "#V#user_bob")

        assert result is False


class TestDeleteMessage:
    """Tests for delete_message function."""

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.apply_concept_query_filter")
    def test_delete_message_success(
        self,
        mock_filter: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """delete_message() should soft-delete when sender requests it."""
        mock_coll = MagicMock()
        mock_get_coll.return_value = mock_coll
        mock_filter.return_value = {
            "concept_id": "#V#message_abc",
            f"relationships.{PREDICATE_SENDER}": "#V#user_alice",
        }
        mock_coll.update_one.return_value = MagicMock(modified_count=1)

        result = delete_message("#V#message_abc", "#V#user_alice")

        assert result is True

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.apply_concept_query_filter")
    def test_delete_message_not_sender(
        self,
        mock_filter: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """delete_message() should fail when non-sender tries to delete."""
        mock_coll = MagicMock()
        mock_get_coll.return_value = mock_coll
        mock_filter.return_value = {
            "concept_id": "#V#message_abc",
            f"relationships.{PREDICATE_SENDER}": "#V#user_bob",
        }
        # Update fails because sender doesn't match
        mock_coll.update_one.return_value = MagicMock(modified_count=0)

        result = delete_message("#V#message_abc", "#V#user_bob")

        assert result is False


class TestGetUnreadCount:
    """Tests for get_unread_count function."""

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.apply_concept_query_filter")
    def test_get_unread_count(
        self,
        mock_filter: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """get_unread_count() should return count of unread messages."""
        mock_coll = MagicMock()
        mock_get_coll.return_value = mock_coll
        mock_filter.return_value = {}
        mock_coll.count_documents.return_value = 5

        result = get_unread_count("#V#user_alice")

        assert result == 5

    @patch("src.backend.services.message_service.get_concepts_collection")
    def test_get_unread_count_empty_user(
        self,
        mock_get_coll: MagicMock,
    ) -> None:
        """get_unread_count() with empty user should return 0."""
        result = get_unread_count("")
        assert result == 0
