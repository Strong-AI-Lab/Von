from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

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
    app.config["CONTEXT"] = ["existing"]

    with app.test_client() as client:
        yield app, client


def test_von_reset_clears_context_and_records_history_marker(
    app_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = app_client
    reset_calls: list[dict[str, object]] = []

    def _reset_state(**kwargs):
        reset_calls.append(dict(kwargs))
        return {"updated": True, "matched": True}

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.reset_chat_history_conversation_state",
        _reset_state,
    )

    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#test_user"
        flask_session["session_id"] = "session-1"

    response = client.post("/von/reset")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "reset",
        "message": "Context reset successfully",
    }
    assert app.config["CONTEXT"] == []
    assert reset_calls == [
        {
            "user_id": "#V#test_user",
            "session_id": "session-1",
            "updated_by": "#V#test_user",
            "namespace": None,
        }
    ]


def test_von_reset_succeeds_even_if_request_logging_is_broken(
    app_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = app_client

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.reset_chat_history_conversation_state",
        lambda **_kwargs: {"updated": True, "matched": True},
    )

    def _raise_invalid_argument(*_args, **_kwargs):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(app.logger, "info", _raise_invalid_argument)
    monkeypatch.setattr(app.logger, "exception", _raise_invalid_argument)

    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#test_user"
        flask_session["session_id"] = "session-1"

    response = client.post("/von/reset")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "reset",
        "message": "Context reset successfully",
    }
    assert app.config["CONTEXT"] == []


def test_shared_conversation_invitee_cannot_reset_owner_carrier(
    app_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = app_client
    reset_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_shared_conversation_owner",
        lambda **_kwargs: (
            "#V#owner",
            {
                "conversation_owner_user_id": "#V#owner",
                "invitee_user_id": "#V#invitee",
                "status": "accepted",
            },
        ),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.reset_chat_history_conversation_state",
        lambda **kwargs: reset_calls.append(dict(kwargs)),
    )

    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#invitee"
        flask_session["session_id"] = "shared-session"

    response = client.post("/von/reset")

    assert response.status_code == 403
    assert response.get_json()["error_code"] == (
        "conversation_owner_required_for_shared_reset"
    )
    assert reset_calls == []
    assert app.config["CONTEXT"] == ["existing"]
