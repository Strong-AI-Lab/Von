"""Tests for chat history backfill admin endpoint.

This verifies the endpoint is only available when logged in and that it
invokes the backfill service with a session-derived namespace.
"""

from __future__ import annotations

import types

import pytest


@pytest.fixture
def app_client(monkeypatch):
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


def test_chat_history_backfill_requires_authentication(app_client):
    _, client = app_client

    resp = client.post("/admin/chat_history_backfill", json={"dry_run": True})

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Not authenticated"


def test_agent_provenance_backfill_requires_authentication(app_client):
    _, client = app_client

    resp = client.post(
        "/admin/chat_history_agent_provenance_backfill",
        json={"dry_run": True},
    )

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Not authenticated"


def test_chat_history_backfill_calls_service_when_logged_in(app_client, monkeypatch):
    _, client = app_client

    called = {}

    def _fake_backfill(**kwargs):
        called.update(kwargs)
        return {
            "status": "ok",
            "sessions_updated": 0,
            "messages_indexed_attempted": 0,
            "messages_indexed_success": 0,
            "messages_indexed_failed": 0,
        }

    monkeypatch.setattr(
        "src.backend.services.chat_history_service.backfill_chat_history_for_user",
        _fake_backfill,
    )

    with client.session_transaction() as sess:
        sess["user_email"] = "michael_witbrock@example.com"
        sess["user_concept_id"] = "#V#michael_witbrock"
        sess["organisation_concept_id"] = "#V#university_of_auckland_strong_ai_lab"

    resp = client.post(
        "/admin/chat_history_backfill",
        json={"max_sessions": 1, "max_messages": 2, "dry_run": True},
    )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"

    assert called["user_concept_id"] == "#V#michael_witbrock"
    assert (
        called["target_namespace"]
        == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )
    assert called["organisation_concept_id"] == "university_of_auckland_strong_ai_lab"
    assert called["max_sessions"] == 1
    assert called["max_messages"] == 2
    assert called["dry_run"] is True


def test_agent_provenance_backfill_defaults_to_dry_run(app_client, monkeypatch):
    _, client = app_client

    called = {}

    def _fake_backfill(**kwargs):
        called.update(kwargs)
        return {
            "status": "ok",
            "dry_run": kwargs["dry_run"],
            "reliable_candidate_count": 2,
            "sessions_marked": 0,
        }

    monkeypatch.setattr(
        "src.backend.services.chat_history_service.backfill_agent_created_chat_session_provenance",
        _fake_backfill,
    )

    with client.session_transaction() as sess:
        sess["user_email"] = "michael_witbrock@example.com"
        sess["user_concept_id"] = "#V#michael_witbrock"
        sess["organisation_concept_id"] = "#V#university_of_auckland_strong_ai_lab"

    resp = client.post(
        "/admin/chat_history_agent_provenance_backfill",
        json={"max_sessions": 25},
    )

    assert resp.status_code == 200
    assert resp.get_json()["dry_run"] is True
    assert called["user_concept_id"] == "#V#michael_witbrock"
    assert (
        called["namespace"]
        == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )
    assert called["include_legacy"] is False
    assert called["max_sessions"] == 25
    assert called["dry_run"] is True
