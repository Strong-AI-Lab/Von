"""Tests for setting active chat session in the Flask session."""

from __future__ import annotations

import types
import pytest
import src.backend.server.routes.von_routes as von_routes


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


def test_set_chat_session_requires_authentication(app_client):
    _, client = app_client

    resp = client.post("/von/api/session/set_chat_session", json={"session_id": "s1"})

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Not authenticated"


def test_set_chat_session_returns_history_and_updates_session(monkeypatch, app_client):
    _, client = app_client

    class _FakeColl:
        def find_one(self, query, projection=None):
            if query.get("user_id") != "#V#u":
                return None
            if query.get("session_id") != "s1":
                return None
            return {
                "history": [
                    {
                        "role": "user",
                        "content": "hi",
                        "timestamp": "2025-01-01T00:00:00Z",
                    },
                    {
                        "role": "assistant",
                        "content": "ok",
                        "timestamp": "2025-01-01T00:00:01Z",
                    },
                ]
            }

    import src.backend.services.chat_history_service as chat_history_service

    monkeypatch.setattr(
        chat_history_service, "get_chat_history_collection_service", lambda: _FakeColl()
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#u"

    resp = client.post("/von/api/session/set_chat_session", json={"session_id": "s1"})

    assert resp.status_code == 200
    js = resp.get_json()
    assert js["status"] == "updated"
    assert js["session_id"] == "s1"
    assert isinstance(js["history"], list)

    with client.session_transaction() as sess:
        assert sess.get("session_id") == "s1"


def test_set_chat_session_skips_history_when_requested(monkeypatch, app_client):
    _, client = app_client

    class _FakeColl:
        def __init__(self):
            self.projections = []

        def find_one(self, query, projection=None):
            self.projections.append(projection)
            return {
                "history": [
                    {
                        "role": "user",
                        "content": "hi",
                        "timestamp": "2025-01-01T00:00:00Z",
                    },
                    {
                        "role": "assistant",
                        "content": "ok",
                        "timestamp": "2025-01-01T00:00:01Z",
                    },
                ],
                "session_name": "Session One",
            }

    coll = _FakeColl()

    import src.backend.services.chat_history_service as chat_history_service

    monkeypatch.setattr(
        chat_history_service, "get_chat_history_collection_service", lambda: coll
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#u"

    resp = client.post(
        "/von/api/session/set_chat_session",
        json={"session_id": "s1", "include_history": False},
    )

    assert resp.status_code == 200
    js = resp.get_json()
    assert js["history"] == []
    assert coll.projections
    assert "history" not in (coll.projections[-1] or {})

    with client.session_transaction() as sess:
        assert sess.get("session_id") == "s1"


def test_set_chat_session_allows_shared_invite(monkeypatch, app_client):
    _, client = app_client

    class _FakeColl:
        def __init__(self):
            self.queries = []

        def find_one(self, query, projection=None):
            self.queries.append(query)
            if query.get("user_id") == "#V#inviter" and query.get("session_id") == "s2":
                return {
                    "history": [
                        {"role": "user", "content": "shared hi"},
                        {"role": "assistant", "content": "shared ok"},
                    ],
                    "session_name": "Shared Session",
                }
            return None

    coll = _FakeColl()

    import src.backend.services.chat_history_service as chat_history_service

    monkeypatch.setattr(
        chat_history_service, "get_chat_history_collection_service", lambda: coll
    )

    import src.backend.services.shared_conversation_service as shared_conversation_service

    monkeypatch.setattr(
        shared_conversation_service,
        "get_accepted_invite_for_user_session",
        lambda **kwargs: {"inviter_user_id": "#V#inviter"},
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#u"

    resp = client.post("/von/api/session/set_chat_session", json={"session_id": "s2"})

    assert resp.status_code == 200
    js = resp.get_json()
    assert js["session_id"] == "s2"
    assert js["session_name"] == "Shared Session"
    assert isinstance(js["history"], list)
    assert any(q.get("user_id") == "#V#u" for q in coll.queries)
    assert any(q.get("user_id") == "#V#inviter" for q in coll.queries)

    with client.session_transaction() as sess:
        assert sess.get("session_id") == "s2"


def test_history_uses_query_session_id(monkeypatch, app_client):
    _, client = app_client
    captured: dict[str, str] = {}

    def fake_get_segments(user_id, session_id, **kwargs):
        captured["user_id"] = user_id
        captured["session_id"] = session_id
        return [[{"role": "user", "content": "hi"}]]

    import src.backend.services.chat_history_service as chat_history_service

    monkeypatch.setattr(
        chat_history_service, "get_chat_history_segments", fake_get_segments
    )
    monkeypatch.setattr(
        chat_history_service, "has_chat_history_session", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(von_routes, "chat_history_service", chat_history_service)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "has_chat_history_session",
        lambda *args, **kwargs: True,
    )

    import src.backend.services.shared_conversation_service as shared_conversation_service

    monkeypatch.setattr(
        shared_conversation_service,
        "get_accepted_invite_for_user_session",
        lambda *args, **kwargs: {"inviter_user_id": "#V#u"},
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#u"
        sess["session_id"] = "session-from-cookie"

    resp = client.get("/von/history?segments=1&session_id=session-from-query")

    assert resp.status_code == 200
    js = resp.get_json()
    assert js["history"] == [{"role": "user", "content": "hi"}]
    assert captured["session_id"] == "session-from-query"

    with client.session_transaction() as sess:
        assert sess.get("session_id") == "session-from-cookie"
