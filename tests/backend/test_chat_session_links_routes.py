"""Tests for the /von/api/session/chat_session_links endpoint.

These tests focus on authentication behaviour and payload normalisation without
requiring a real MongoDB instance.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def app_client(monkeypatch):
    """Provide a Flask test client with side effects stubbed out."""

    # Stub Google auth dependencies pulled in by utils_flask -> auth_routes imports.
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

    # Avoid starting the DB monitor thread or touching Mongo during tests.
    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
    # Keep prompt concept health check lightweight.
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


def test_chat_session_links_requires_authentication(app_client):
    _, client = app_client

    resp = client.get("/von/api/session/chat_session_links?session_id=abc")

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Not authenticated"


def test_chat_session_links_get_returns_links(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#test_user"
        sess["session_id"] = "sess-default"

    expected = {
        "programmes": ["#V#programme_a"],
        "projects": [],
        "activities": ["#V#activity_b"],
        "modalities": ["#V#zoom"],
    }

    with (
        patch(
            "src.backend.services.chat_history_service.resolve_chat_history_namespace",
            return_value="#V#test_user",
        ),
        patch(
            "src.backend.services.chat_history_service.get_chat_session_links",
            return_value=expected,
        ) as mock_get,
        patch(
            "src.backend.vontology.utils_vontology.ensure_conversation_modality_concepts",
            return_value={},
        ),
    ):
        resp = client.get("/von/api/session/chat_session_links?session_id=sess-1")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["session_id"] == "sess-1"
    assert data["session_links"] == expected

    mock_get.assert_called_once()


def test_chat_session_links_post_filters_missing_concepts(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#test_user"
        sess["session_id"] = "sess-default"

    payload = {
        "session_id": "sess-2",
        "session_links": {
            "programmes": ["#V#programme_a", "#V#missing_programme"],
            "projects": ["#V#project_x"],
            "activities": [],
            "modalities": ["#V#zoom"],
        },
    }

    def fake_find(query: Dict[str, Any], projection: Dict[str, Any]):
        # Only return a subset as "existing".
        wanted = set(query.get("concept_id", {}).get("$in", []))
        existing = wanted.intersection({"#V#programme_a", "#V#project_x", "#V#zoom"})
        return [{"concept_id": cid} for cid in existing]

    mock_set = MagicMock(
        return_value={
            "updated": True,
            "matched": True,
            "session_links": {
                "programmes": ["#V#programme_a"],
                "projects": ["#V#project_x"],
                "activities": [],
                "modalities": ["#V#zoom"],
            },
        }
    )

    with (
        patch(
            "src.backend.services.chat_history_service.resolve_chat_history_namespace",
            return_value="#V#test_user",
        ),
        patch(
            "src.backend.db.repositories.concepts_repository.ConceptsRepository.find",
            side_effect=fake_find,
        ),
        patch(
            "src.backend.services.chat_history_service.set_chat_session_links",
            mock_set,
        ),
        patch(
            "src.backend.vontology.utils_vontology.ensure_conversation_modality_concepts",
            return_value={},
        ),
    ):
        resp = client.post("/von/api/session/chat_session_links", json=payload)

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "updated"
    assert data["session_id"] == "sess-2"
    assert data["session_links"]["programmes"] == ["#V#programme_a"]
    assert data["missing_concepts"] == ["#V#missing_programme"]

    # Ensure the service only receives filtered concepts.
    called_links = mock_set.call_args.kwargs["session_links"]
    assert called_links["programmes"] == ["#V#programme_a"]


def test_chat_session_links_post_404_when_session_missing(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#test_user"

    with (
        patch(
            "src.backend.services.chat_history_service.resolve_chat_history_namespace",
            return_value="#V#test_user",
        ),
        patch(
            "src.backend.services.chat_history_service.set_chat_session_links",
            return_value={"updated": False, "matched": False, "session_links": {}},
        ),
        patch(
            "src.backend.vontology.utils_vontology.ensure_conversation_modality_concepts",
            return_value={},
        ),
    ):
        resp = client.post(
            "/von/api/session/chat_session_links",
            json={"session_id": "sess-missing", "session_links": {}},
        )

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Session not found"
