"""Tests for shared_conversation_stream_service (JVNAUTOSCI-1002)."""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.backend.services.shared_conversation_stream_service import (
    SharedConversationStreamService,
    Subscriber,
    TurnEvent,
    broadcast_shared_turn,
    get_stream_service,
)


class TestSharedConversationStreamService:
    """Tests for the SSE streaming service."""

    def test_subscribe_creates_subscriber(self):
        """Test that subscribe creates a new subscriber."""
        service = SharedConversationStreamService()
        try:
            subscriber = service.subscribe(
                session_id="test-session-1",
                user_concept_id="#V#test_user",
            )
            assert subscriber.session_id == "test-session-1"
            assert subscriber.user_concept_id == "#V#test_user"
            assert subscriber.subscriber_id is not None
            assert service.get_subscriber_count("test-session-1") == 1
        finally:
            service.shutdown()

    def test_unsubscribe_removes_subscriber(self):
        """Test that unsubscribe removes the subscriber."""
        service = SharedConversationStreamService()
        try:
            subscriber = service.subscribe(
                session_id="test-session-2",
                user_concept_id="#V#test_user",
            )
            assert service.get_subscriber_count("test-session-2") == 1

            service.unsubscribe(subscriber)
            assert service.get_subscriber_count("test-session-2") == 0
        finally:
            service.shutdown()

    def test_broadcast_turn_reaches_subscribers(self):
        """Test that broadcast_turn sends events to subscribers."""
        service = SharedConversationStreamService()
        try:
            subscriber = service.subscribe(
                session_id="test-session-3",
                user_concept_id="#V#user_a",
            )

            notified = service.broadcast_turn(
                session_id="test-session-3",
                turn_id="turn-1",
                speaker="user",
                content="Hello, world!",
                author_user_id="#V#user_b",
            )

            assert notified == 1

            # Check the event was queued
            event = subscriber.event_queue.get_nowait()
            assert event.turn_id == "turn-1"
            assert event.speaker == "user"
            assert event.content == "Hello, world!"
            assert event.author_user_id == "#V#user_b"
        finally:
            service.shutdown()

    def test_broadcast_turn_excludes_author(self):
        """Test that broadcast_turn excludes the specified user."""
        service = SharedConversationStreamService()
        try:
            # Subscribe two users
            subscriber_a = service.subscribe(
                session_id="test-session-4",
                user_concept_id="#V#user_a",
            )
            subscriber_b = service.subscribe(
                session_id="test-session-4",
                user_concept_id="#V#user_b",
            )

            # Broadcast excluding user_a
            notified = service.broadcast_turn(
                session_id="test-session-4",
                turn_id="turn-2",
                speaker="user",
                content="Test message",
                exclude_user_id="#V#user_a",
            )

            # Only user_b should receive
            assert notified == 1
            assert subscriber_a.event_queue.empty()
            assert not subscriber_b.event_queue.empty()
        finally:
            service.shutdown()

    def test_broadcast_turn_deduplicates_by_turn_id(self):
        """Test that duplicate turn_ids are not sent twice."""
        service = SharedConversationStreamService()
        try:
            subscriber = service.subscribe(
                session_id="test-session-5",
                user_concept_id="#V#user_a",
            )

            # Broadcast same turn_id twice
            service.broadcast_turn(
                session_id="test-session-5",
                turn_id="turn-3",
                speaker="user",
                content="First",
            )
            service.broadcast_turn(
                session_id="test-session-5",
                turn_id="turn-3",
                speaker="user",
                content="Duplicate",
            )

            # Should only have one event
            event1 = subscriber.event_queue.get_nowait()
            assert event1.content == "First"
            assert subscriber.event_queue.empty()
        finally:
            service.shutdown()

    def test_generate_events_yields_sse_format(self):
        """Test that generate_events yields proper SSE format."""
        service = SharedConversationStreamService()
        try:
            subscriber = service.subscribe(
                session_id="test-session-6",
                user_concept_id="#V#user_a",
            )

            # Add an event
            service.broadcast_turn(
                session_id="test-session-6",
                turn_id="turn-4",
                speaker="assistant",
                content="Hello",
            )

            # Get generator
            gen = service.generate_events(subscriber, timeout=0.1)
            event_str = next(gen)

            # Verify SSE format
            assert event_str.startswith("event: assistant_turn\n")
            assert "data: " in event_str
            assert '"turn_id": "turn-4"' in event_str
            assert '"content": "Hello"' in event_str
        finally:
            service.shutdown()

    def test_singleton_instance(self):
        """Test that get_stream_service returns singleton."""
        service1 = get_stream_service()
        service2 = get_stream_service()
        assert service1 is service2


class TestBroadcastSharedTurnConvenience:
    """Tests for the convenience broadcast_shared_turn function."""

    def test_broadcast_shared_turn_uses_singleton(self):
        """Test that broadcast_shared_turn uses the singleton service."""
        service = get_stream_service()

        # Subscribe to a session
        subscriber = service.subscribe(
            session_id="conv-test-1",
            user_concept_id="#V#user_x",
        )

        try:
            notified = broadcast_shared_turn(
                session_id="conv-test-1",
                turn_id="conv-turn-1",
                speaker="user",
                content="Via convenience function",
            )

            assert notified == 1
            event = subscriber.event_queue.get_nowait()
            assert event.content == "Via convenience function"
        finally:
            service.unsubscribe(subscriber)


class TestSubscriberState:
    """Tests for Subscriber dataclass state management."""

    def test_seen_turn_ids_tracks_received_turns(self):
        """Test that seen_turn_ids is populated correctly."""
        service = SharedConversationStreamService()
        try:
            subscriber = service.subscribe(
                session_id="state-test-1",
                user_concept_id="#V#user_a",
            )

            service.broadcast_turn(
                session_id="state-test-1",
                turn_id="state-turn-1",
                speaker="user",
                content="Test",
            )

            assert "state-turn-1" in subscriber.seen_turn_ids
        finally:
            service.shutdown()

    def test_last_event_at_updated_on_broadcast(self):
        """Test that last_event_at is updated when event is queued."""
        service = SharedConversationStreamService()
        try:
            subscriber = service.subscribe(
                session_id="state-test-2",
                user_concept_id="#V#user_a",
            )

            assert subscriber.last_event_at is None

            service.broadcast_turn(
                session_id="state-test-2",
                turn_id="state-turn-2",
                speaker="user",
                content="Test",
            )

            assert subscriber.last_event_at is not None
            assert isinstance(subscriber.last_event_at, datetime)
        finally:
            service.shutdown()
