"""Tests for shared_conversation_stream_service (JVNAUTOSCI-1002)."""

from __future__ import annotations

import types
from datetime import datetime

import pytest

from src.backend.services.shared_conversation_stream_service import (
    SharedConversationStreamService,
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
            assert event is not None
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
            assert event1 is not None
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
            assert event is not None
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


@pytest.fixture
def app_client(monkeypatch):
    # Use mongomock so tests do not require a running Mongo.
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    # Stub Google auth deps pulled in by utils_flask -> auth_routes imports.
    import sys

    fake_flow_module = types.ModuleType("google_auth_oauthlib.flow")

    class _DummyFlow:
        def __init__(self, *args, **kwargs):
            self.credentials = types.SimpleNamespace(id_token="dummy-token")
            self.redirect_uri = kwargs.get("redirect_uri")
            self.client_config = {"web": {"redirect_uris": [self.redirect_uri]}}

        @classmethod
        def from_client_config(cls, *args, **kwargs):
            return cls(**kwargs)

        def authorization_url(self, *args, **kwargs):
            return "https://auth.example", "state-token"

        def fetch_token(self, *args, **kwargs):
            return None

    fake_flow_module.Flow = _DummyFlow  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib"] = types.ModuleType("google_auth_oauthlib")
    sys.modules["google_auth_oauthlib"].flow = fake_flow_module  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib.flow"] = fake_flow_module

    fake_id_token_module = types.ModuleType("google.oauth2.id_token")
    fake_id_token_module.verify_oauth2_token = lambda *args, **kwargs: {  # type: ignore[attr-defined]
        "sub": "dummy-user"
    }

    fake_credentials_module = types.ModuleType("google.oauth2.credentials")

    class _DummyCredentials:
        def __init__(self, id_token: str = "dummy-token"):
            self.id_token = id_token

    fake_credentials_module.Credentials = _DummyCredentials  # type: ignore[attr-defined]

    fake_service_account_module = types.ModuleType("google.oauth2.service_account")

    class _DummyServiceAccountCredentials:
        def __init__(self, *args, **kwargs):
            self.project_id = kwargs.get("project_id")

    fake_service_account_module.Credentials = _DummyServiceAccountCredentials  # type: ignore[attr-defined]

    fake_oauth2_package = types.ModuleType("google.oauth2")
    fake_oauth2_package.id_token = fake_id_token_module  # type: ignore[attr-defined]
    fake_oauth2_package.credentials = fake_credentials_module  # type: ignore[attr-defined]
    fake_oauth2_package.service_account = fake_service_account_module  # type: ignore[attr-defined]

    sys.modules["google.oauth2"] = fake_oauth2_package
    sys.modules["google.oauth2.id_token"] = fake_id_token_module
    sys.modules["google.oauth2.credentials"] = fake_credentials_module
    sys.modules["google.oauth2.service_account"] = fake_service_account_module

    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
    monkeypatch.setattr(
        utils_flask,
        "prompt_concept_health_status",
        lambda: {"available": True, "source_field": "stub"},
    )

    app = utils_flask.create_flask_app(
        list_models_func=lambda: ["dummy-model"],
        generate_func=lambda prompt, context, model: "ok",
    )
    app.config["TESTING"] = True

    with app.test_client() as client:
        yield app, client


class TestSharedConversationStreamRoute:
    """Route-level tests for the SSE stream endpoint."""

    def test_stream_requires_authentication(self, app_client, monkeypatch):
        _, client = app_client
        monkeypatch.setattr(
            "src.backend.security.access_control.get_effective_user_concept_id",
            lambda: None,
        )

        resp = client.get("/von/api/shared_conversations/stream?session_id=test")

        assert resp.status_code == 401
        assert resp.get_json()["error"] == "Not authenticated"

    def test_stream_rejects_unauthorised_user(self, app_client, monkeypatch):
        _, client = app_client
        monkeypatch.setattr(
            "src.backend.security.access_control.get_effective_user_concept_id",
            lambda: "#V#user_a",
        )
        monkeypatch.setattr(
            "src.backend.services.shared_conversation_service.resolve_conversation_owner",
            lambda **_kwargs: "#V#owner",
        )
        monkeypatch.setattr(
            "src.backend.services.shared_conversation_service.get_accepted_invite_for_user_session",
            lambda **_kwargs: None,
        )

        resp = client.get("/von/api/shared_conversations/stream?session_id=test")

        assert resp.status_code == 403
        assert resp.get_json()["error"] == "Not authorized for this conversation"

    def test_stream_allows_owner(self, app_client, monkeypatch):
        _, client = app_client
        monkeypatch.setattr(
            "src.backend.security.access_control.get_effective_user_concept_id",
            lambda: "#V#owner",
        )
        monkeypatch.setattr(
            "src.backend.services.shared_conversation_service.resolve_conversation_owner",
            lambda **_kwargs: "#V#owner",
        )

        resp = client.get(
            "/von/api/shared_conversations/stream?session_id=test",
            buffered=False,
        )

        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"

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
