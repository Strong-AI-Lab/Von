"""Regression tests for /von/history/sessions resiliency."""

from __future__ import annotations

import pytest
from flask import Flask
import src.backend.server.routes.von_routes as von_routes


@pytest.fixture
def app_client():
    """Build a minimal Flask test client with only Von routes registered."""
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    with app.test_client() as client:
        yield app, client


def test_history_sessions_tolerates_shared_invite_lookup_failure(
    monkeypatch, app_client
):
    """A shared-invite lookup failure must not take down the whole endpoint."""
    _, client = app_client

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#u",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *args, **kwargs: {
            "user_id": "#V#u",
            "organisation_id": "#V#org",
            "role": None,
            "namespace": "#V#u@org",
            "chat_session_id": None,
            "source": "test",
        },
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_summaries",
        lambda *args, **kwargs: [
            {
                "session_id": "sess-1",
                "session_name": "Session 1",
                "last_message_at": "2026-02-25T00:00:00Z",
                "namespace": "#V#u@org",
            }
        ],
    )

    import src.backend.services.shared_conversation_service as shared_conversation_service

    def _raise_shared_invites(**kwargs):
        raise RuntimeError("malformed invite payload")

    monkeypatch.setattr(
        shared_conversation_service,
        "list_accepted_invites_for_user",
        _raise_shared_invites,
    )
    monkeypatch.setattr(
        shared_conversation_service,
        "list_outgoing_accepted_invites_for_user",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        shared_conversation_service,
        "resolve_conversation_owner",
        lambda **kwargs: None,
    )

    response = client.get(
        "/von/history/sessions?limit=50&summary=light",
        headers={"X-Von-Window-Session": "ws_test"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["authenticated"] is True
    assert [s["session_id"] for s in payload["sessions"]] == ["sess-1"]
    assert "accepted_invites_unavailable" in (payload.get("warnings") or [])


def test_history_sessions_skips_bad_shared_invite_rows(monkeypatch, app_client):
    """One bad shared invite row should be skipped without failing endpoint."""
    _, client = app_client

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#u",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *args, **kwargs: {
            "user_id": "#V#u",
            "organisation_id": "#V#org",
            "role": None,
            "namespace": "#V#u@org",
            "chat_session_id": None,
            "source": "test",
        },
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_summaries",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        von_routes,
        "_derive_namespace_for_user_org",
        lambda user_concept_id, org_concept_id: "#V#owner@org",
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "resolve_chat_history_namespace",
        lambda user_id: "#V#owner@org",
    )

    import src.backend.services.shared_conversation_service as shared_conversation_service

    monkeypatch.setattr(
        shared_conversation_service,
        "list_accepted_invites_for_user",
        lambda **kwargs: [
            {
                "session_id": "shared-ok",
                "inviter_user_id": "#V#owner",
                "conversation_owner_user_id": "#V#owner",
                "organisation_concept_id": "#V#org",
                "invite_id": "invite-1",
                "accepted_at": "2026-02-25T01:00:00Z",
            },
            {
                "session_id": "shared-bad",
                "inviter_user_id": "#V#owner",
                "conversation_owner_user_id": "#V#owner",
                "organisation_concept_id": "#V#org",
                "invite_id": "invite-2",
                "accepted_at": "2026-02-25T01:00:00Z",
            },
        ],
    )
    monkeypatch.setattr(
        shared_conversation_service,
        "list_outgoing_accepted_invites_for_user",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        shared_conversation_service,
        "resolve_conversation_owner",
        lambda **kwargs: "#V#owner",
    )

    def _fake_shared_summary(owner_id, session_id, **kwargs):
        if session_id == "shared-bad":
            raise RuntimeError("broken shared row")
        return {
            "session_id": session_id,
            "session_name": "Shared Session",
            "last_message_at": "2026-02-25T01:00:00Z",
        }

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_summary",
        _fake_shared_summary,
    )

    response = client.get(
        "/von/history/sessions?limit=50&summary=light",
        headers={"X-Von-Window-Session": "ws_test"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["authenticated"] is True
    assert [s["session_id"] for s in payload["sessions"]] == ["shared-ok"]
    assert "shared_invite_resolution_errors:1" in (payload.get("warnings") or [])


def test_history_sessions_returns_degraded_payload_for_transient_history_errors(
    monkeypatch, app_client
):
    _, client = app_client
    import src.backend.services.chat_history_service as chat_history_service

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#u",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *args, **kwargs: {
            "user_id": "#V#u",
            "organisation_id": "#V#org",
            "role": None,
            "namespace": "#V#u@org",
            "chat_session_id": None,
            "source": "test",
        },
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_summaries",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            chat_history_service.ChatHistoryServiceError(
                "read circuit open for 3.0s"
            )
        ),
    )

    response = client.get(
        "/von/history/sessions?limit=50&summary=light",
        headers={"X-Von-Window-Session": "ws_test"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["authenticated"] is True
    assert payload["degraded"] is True
    assert payload["retryable"] is True
    assert payload["sessions"] == []
    assert "chat_history_temporarily_unavailable" in (payload.get("warnings") or [])


def test_history_sessions_ignores_stale_flask_active_session_id(
    monkeypatch, app_client
):
    """The endpoint must not advertise a stale Flask-session active id after scope repair."""
    _, client = app_client

    with client.session_transaction() as sess:
        sess["session_id"] = "stale-session-id"

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#u",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *args, **kwargs: {
            "user_id": "#V#u",
            "organisation_id": "#V#org",
            "role": None,
            "namespace": "#V#u@org",
            "chat_session_id": None,
            "source": "test",
        },
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_summaries",
        lambda *args, **kwargs: [
            {
                "session_id": "sess-1",
                "session_name": "Session 1",
                "last_message_at": "2026-02-25T00:00:00Z",
                "namespace": "#V#u@org",
            }
        ],
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "has_chat_history_session",
        lambda *args, **kwargs: False,
    )

    import src.backend.services.shared_conversation_service as shared_conversation_service

    monkeypatch.setattr(
        shared_conversation_service,
        "list_accepted_invites_for_user",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        shared_conversation_service,
        "list_outgoing_accepted_invites_for_user",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        shared_conversation_service,
        "resolve_conversation_owner",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **kwargs: (None, None),
    )

    response = client.get(
        "/von/history/sessions?limit=50&summary=light",
        headers={"X-Von-Window-Session": "ws_test"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["authenticated"] is True
    assert payload["active_session_id"] is None


def test_history_sessions_returns_effective_active_session_id_when_authorised(
    monkeypatch, app_client
):
    """The endpoint should still expose the active session when the effective scope authorises it."""
    _, client = app_client

    with client.session_transaction() as sess:
        sess["session_id"] = "stale-session-id"

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#u",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *args, **kwargs: {
            "user_id": "#V#u",
            "organisation_id": "#V#org",
            "role": None,
            "namespace": "#V#u@org",
            "chat_session_id": "sess-1",
            "source": "test",
        },
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_summaries",
        lambda *args, **kwargs: [
            {
                "session_id": "sess-1",
                "session_name": "Session 1",
                "last_message_at": "2026-02-25T00:00:00Z",
                "namespace": "#V#u@org",
            }
        ],
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "has_chat_history_session",
        lambda user_id, session_id, namespace=None: session_id == "sess-1",
    )

    import src.backend.services.shared_conversation_service as shared_conversation_service

    monkeypatch.setattr(
        shared_conversation_service,
        "list_accepted_invites_for_user",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        shared_conversation_service,
        "list_outgoing_accepted_invites_for_user",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        shared_conversation_service,
        "resolve_conversation_owner",
        lambda **kwargs: None,
    )

    response = client.get(
        "/von/history/sessions?limit=50&summary=light",
        headers={"X-Von-Window-Session": "ws_test"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["authenticated"] is True
    assert payload["active_session_id"] == "sess-1"
