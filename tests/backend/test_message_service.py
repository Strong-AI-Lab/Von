"""Tests for message service (JVNAUTOSCI-1071).

Unit tests for message creation, retrieval, and management.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from src.backend.services.message_service import (
    MESSAGE_TYPE_CONCEPT_ID,
    MESSAGE_STATUS_SENT,
    MESSAGE_STATUS_READ,
    PREDICATE_SENDER,
    PREDICATE_RECIPIENT,
    create_message,
    get_message,
    get_messages_for_user,
    get_conversation_between_users,
    get_unread_count,
    mark_message_read,
    mark_messages_read_bulk,
    delete_message,
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
        assert "#V#user_alice" in result["relationships"]["specific_to_user"]
        assert "#V#user_bob" in result["relationships"]["specific_to_user"]
        assert result["relationships"][PREDICATE_SENDER] == ["#V#user_alice"]
        assert result["relationships"][PREDICATE_RECIPIENT] == ["#V#user_bob"]
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

        visibility = result["relationships"]["specific_to_user"]
        assert "#V#user_alice" in visibility
        assert "#V#user_bob" in visibility
        assert "#V#user_charlie" in visibility
        mock_launch_workflow.assert_called_once()

    @patch("src.backend.services.message_service.get_concepts_collection")
    @patch("src.backend.services.message_service.ConceptsRepository")
    @patch("src.backend.services.message_service.upsert_text_for_concept")
    @patch("src.backend.services.message_service.maybe_launch_direct_message_workflow")
    def test_create_message_with_org_scoping(
        self,
        mock_launch_workflow: MagicMock,
        mock_upsert: MagicMock,
        mock_repo: MagicMock,
        mock_get_coll: MagicMock,
    ) -> None:
        """create_message() with org_id should include org in visibility."""
        mock_get_coll.return_value = MagicMock()
        mock_repo.insert_one.return_value = None

        result = create_message(
            sender_id="#V#user_alice",
            recipient_ids=["#V#user_bob"],
            content="Org-scoped message",
            org_id="#V#nao_institute",
        )

        assert result["relationships"]["specific_to_org"] == ["#V#nao_institute"]
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


class TestGetMessagesForUser:
    """Tests for get_messages_for_user function."""

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
