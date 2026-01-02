"""Tests for on-demand backfill of historical <spoken> talk tracks."""

from __future__ import annotations

import types

import pytest


class _StubLLM:
    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls: list[dict] = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        return self._response_text


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


def _insert_history_doc(*, user_id: str, session_id: str, history: list[dict]):
    from src.backend.db.mongo_client import get_db
    from src.backend.models.chat_history_model import chat_history_collection_name

    db = get_db()
    assert db is not None
    coll = db[chat_history_collection_name]
    coll.insert_one({"user_id": user_id, "session_id": session_id, "history": history})


def test_backfill_spoken_requires_authentication(app_client):
    _, client = app_client

    resp = client.post(
        "/von/history/backfill_spoken",
        json={"history_location": {"session_id": "s", "history_index": 0}},
    )

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Not authenticated"


def test_backfill_spoken_generates_and_persists_presenter_channels(
    app_client, monkeypatch
):
    _, client = app_client

    user_id = "#V#tester"
    session_id = "sess-1"
    history = [
        {"role": "user", "content": "What is this?"},
        {"role": "assistant", "content": "Here is the answer on screen."},
    ]
    _insert_history_doc(user_id=user_id, session_id=session_id, history=history)

    llm = _StubLLM("<spoken>Short talk track.</spoken>")
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: user_id,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda *_args, **_kwargs: [],
    )

    resp = client.post(
        "/von/history/backfill_spoken",
        json={"history_location": {"session_id": session_id, "history_index": 1}},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["presenter_channels"]["spoken"] == "Short talk track."
    assert body["presenter_channels"]["screen"] == "Here is the answer on screen."
    assert body["presenter_channels"]["format"] == "narration_fallback_v1"

    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"] == "Generate <spoken> talk track"

    # Verify persistence to the history message.
    from src.backend.db.mongo_client import get_db
    from src.backend.models.chat_history_model import chat_history_collection_name

    db = get_db()
    assert db is not None
    coll = db[chat_history_collection_name]
    stored = coll.find_one({"user_id": user_id, "session_id": session_id})
    assert stored is not None
    stored_history = stored["history"]
    assert (
        stored_history[1]["llm_debug_data"]["presenter_channels"]["spoken"]
        == "Short talk track."
    )
