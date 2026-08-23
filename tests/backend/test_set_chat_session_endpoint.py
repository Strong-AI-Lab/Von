"""Tests for setting active chat session in the Flask session."""

from __future__ import annotations

import types
from typing import Any

import pytest
import src.backend.server.routes.von_routes as von_routes
from pymongo.errors import PyMongoError


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
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeColl(),
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
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: coll,
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
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: coll,
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
    captured: dict[str, Any] = {}

    def fake_get_session_state(*, user_id, session_id, **kwargs):
        captured["user_id"] = user_id
        captured["session_id"] = session_id
        captured.update(kwargs)
        return {
            "session_id": session_id,
            "history": [{"role": "user", "content": "hi"}],
            "conversation_situation": None,
            "history_offset": 11,
            "history_truncated": True,
        }

    import src.backend.services.chat_history_service as chat_history_service

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_session_state",
        fake_get_session_state,
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

    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: ("#V#u", None),
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#u"
        sess["session_id"] = "session-from-cookie"

    resp = client.get(
        "/von/history?segments=1&segment_size=1&tail_limit=1"
        "&include_debug=0&session_id=session-from-query"
    )

    assert resp.status_code == 200
    js = resp.get_json()
    assert len(js["history"]) == 1
    assert js["history"][0]["role"] == "user"
    assert js["history"][0]["content"] == "hi"
    assert captured["session_id"] == "session-from-query"
    assert captured["history_tail_limit"] == 1
    assert captured["include_debug"] is False
    assert js["has_more_history"] is True

    with client.session_transaction() as sess:
        assert sess.get("session_id") == "session-from-cookie"


def test_set_chat_session_returns_retryable_on_transient_mongo_failure(
    monkeypatch, app_client
):
    _, client = app_client

    class _FailingColl:
        def find_one(self, query, projection=None, **kwargs):
            raise PyMongoError("timed out while reading chat session")

    import src.backend.services.chat_history_service as chat_history_service

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FailingColl(),
    )
    monkeypatch.setattr(
        chat_history_service,
        "find_chat_history_document_for_read",
        lambda coll, query, projection=None: coll.find_one(query, projection),
    )
    # Bind the route's service reference explicitly as well as patching the
    # imported module, so the injected collection failure is the path under
    # test even when a long process has different module bindings.
    monkeypatch.setattr(von_routes, "chat_history_service", chat_history_service)
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **kwargs: (None, None),
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *args, **kwargs: {"namespace": "#V#u"},
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#u"

    resp = client.post("/von/api/session/set_chat_session", json={"session_id": "s1"})

    assert resp.status_code == 503
    payload = resp.get_json()
    assert payload.get("retryable") is True, payload
    assert payload["degraded"] is True


def test_history_returns_degraded_payload_for_transient_chat_history_errors(
    monkeypatch, app_client
):
    _, client = app_client
    import src.backend.services.chat_history_service as chat_history_service

    monkeypatch.setattr(
        chat_history_service,
        "resolve_chat_history_namespace",
        lambda user_id: "#V#u",
    )
    monkeypatch.setattr(
        chat_history_service,
        "has_chat_history_session",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_session_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            chat_history_service.ChatHistoryServiceError(
                "timed out while retrieving history"
            )
        ),
    )
    monkeypatch.setattr(von_routes, "chat_history_service", chat_history_service)
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **kwargs: ("#V#u", None),
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#u"
        sess["session_id"] = "session-from-cookie"

    resp = client.get("/von/history?segments=1&session_id=session-from-query")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["degraded"] is True
    assert payload["retryable"] is True
    assert payload["history"] == []
    assert payload["total_segments"] == 0


def test_history_length_returns_degraded_payload_for_transient_errors(
    monkeypatch, app_client
):
    _, client = app_client
    import src.backend.services.chat_history_service as chat_history_service

    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *args, **kwargs: {"namespace": "#V#u"},
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_length",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            chat_history_service.ChatHistoryServiceError(
                "replicaSetNoPrimary timed out"
            )
        ),
    )
    monkeypatch.setattr(von_routes, "chat_history_service", chat_history_service)

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#u"

    resp = client.get("/von/history/length")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["degraded"] is True
    assert payload["retryable"] is True
    assert payload["history_length"] == 0
    assert payload["session_count"] == 0
