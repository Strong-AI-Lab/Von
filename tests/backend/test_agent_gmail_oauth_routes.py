from types import SimpleNamespace

from flask import Flask

from src.backend.server.routes import agent_gmail_oauth_routes
from src.backend.server.routes.agent_gmail_oauth_routes import agent_gmail_oauth_bp


def _make_app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(agent_gmail_oauth_bp, url_prefix="/von")
    return app


def test_gmail_access_test_uses_minimal_list_call(monkeypatch):
    calls = []

    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_profile_ids_from_env",
        lambda: ["vonwitbrock-gmail"],
    )

    def fake_list_messages(**kwargs):
        calls.append(kwargs)
        return {"messages": [{"id": "message-1"}], "resultSizeEstimate": 1}

    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_messages",
        fake_list_messages,
    )

    client = _make_app().test_client()
    response = client.post(
        "/von/api/agent/gmail/oauth/test_access?profile_id=vonwitbrock-gmail"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload == {
        "success": True,
        "profile_id": "vonwitbrock-gmail",
        "status": "access_ok",
        "detail": "Gmail list access succeeded for this profile.",
        "message_count": 1,
    }
    assert calls == [
        {
            "profile_id": "vonwitbrock-gmail",
            "max_results": 1,
            "query": None,
            "audit_context": {
                "source": "agent_gmail_oauth_test_access",
                "operation": "minimal_gmail_list",
            },
        }
    ]


def test_gmail_access_test_reports_invalid_grant_as_reauthorisation(monkeypatch):
    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_profile_ids_from_env",
        lambda: ["vonwitbrock-gmail"],
    )

    def fake_list_messages(**_kwargs):
        raise RuntimeError(
            "('invalid_grant: Token has been expired or revoked.', "
            "{'error': 'invalid_grant'})"
        )

    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_messages",
        fake_list_messages,
    )

    client = _make_app().test_client()
    response = client.post(
        "/von/api/agent/gmail/oauth/test_access?profile_id=vonwitbrock-gmail"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["status"] == "reauthorisation_required"
    assert payload["error"] == "invalid_grant"
    assert payload["action"] == "authorise_agent_gmail"
    assert "expired or revoked" in payload["detail"]


def test_gmail_access_test_reports_insufficient_scopes_as_reauthorisation(monkeypatch):
    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_profile_ids_from_env",
        lambda: ["vonwitbrock-gmail"],
    )

    def fake_list_messages(**_kwargs):
        raise RuntimeError("insufficient authentication scopes for Gmail API request")

    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_messages",
        fake_list_messages,
    )

    client = _make_app().test_client()
    response = client.post(
        "/von/api/agent/gmail/oauth/test_access?profile_id=vonwitbrock-gmail"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["status"] == "reauthorisation_required"
    assert payload["error"] == "insufficient_scopes"
    assert payload["action"] == "authorise_agent_gmail"


def test_gmail_access_test_reports_generic_api_failure(monkeypatch):
    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_profile_ids_from_env",
        lambda: ["vonwitbrock-gmail"],
    )

    def fake_list_messages(**_kwargs):
        raise RuntimeError("Gmail API temporarily unavailable")

    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_messages",
        fake_list_messages,
    )

    client = _make_app().test_client()
    response = client.post(
        "/von/api/agent/gmail/oauth/test_access?profile_id=vonwitbrock-gmail"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["status"] == "access_failed"
    assert payload["error"] == "gmail_access_test_failed"
    assert payload["detail"] == "Gmail API temporarily unavailable"


def test_gmail_access_test_rejects_unknown_profile(monkeypatch):
    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_profile_ids_from_env",
        lambda: ["vonwitbrock-gmail"],
    )

    client = _make_app().test_client()
    response = client.post("/von/api/agent/gmail/oauth/test_access?profile_id=missing")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"] == "unknown_gmail_profile"
    assert payload["available_profiles"] == ["vonwitbrock-gmail"]


def test_status_endpoint_does_not_perform_live_gmail_access(monkeypatch):
    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_profile_ids_from_env",
        lambda: ["vonwitbrock-gmail"],
    )

    def fail_if_called(**_kwargs):
        raise AssertionError("status endpoint must not list Gmail messages")

    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_messages",
        fail_if_called,
    )
    monkeypatch.setattr(
        agent_gmail_oauth_routes,
        "get_agent_gmail_token_status",
        lambda profile_id: SimpleNamespace(
            profile_id=profile_id,
            has_tokens=True,
            authorised_email="agent@example.test",
            expires_at=None,
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        ),
    )

    client = _make_app().test_client()
    response = client.get(
        "/von/api/agent/gmail/oauth/status?profile_id=vonwitbrock-gmail"
    )

    assert response.status_code == 200
    assert response.get_json()["has_tokens"] is True


def test_oauth_start_uses_current_local_callback_port(monkeypatch):
    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_profile_ids_from_env",
        lambda: ["vonwitbrock-gmail"],
    )
    agent_gmail_oauth_routes._agent_oauth_states.clear()
    captured = {}

    class FakeOAuthService:
        def get_authorisation_url(self, *, profile_id, redirect_uri=None):
            captured["profile_id"] = profile_id
            captured["redirect_uri"] = redirect_uri
            return (
                "https://accounts.google.com/o/oauth2/v2/auth",
                "state-1",
                "verifier-1",
            )

    monkeypatch.setattr(
        agent_gmail_oauth_routes,
        "_get_service",
        lambda: FakeOAuthService(),
    )

    client = _make_app().test_client()
    response = client.get(
        "/von/api/agent/gmail/oauth/start?profile_id=vonwitbrock-gmail",
        base_url="http://localhost:5001",
    )

    assert response.status_code == 302
    assert captured == {
        "profile_id": "vonwitbrock-gmail",
        "redirect_uri": "http://localhost:5001/von/api/agent/gmail/oauth/callback",
    }

    agent_gmail_oauth_routes._agent_oauth_states.clear()


def test_oauth_callback_reuses_start_code_verifier(monkeypatch):
    monkeypatch.setattr(
        agent_gmail_oauth_routes.gmail_service,
        "list_profile_ids_from_env",
        lambda: ["vonwitbrock-gmail"],
    )
    agent_gmail_oauth_routes._agent_oauth_states.clear()
    captured = {}

    class FakeOAuthService:
        def get_authorisation_url(self, *, profile_id, redirect_uri=None):
            return (
                "https://accounts.google.com/o/oauth2/v2/auth",
                "state-1",
                "verifier-1",
            )

        def exchange_code_for_tokens(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                profile_id=kwargs["profile_id"],
                authorised_email="agent@example.test",
                scopes=["https://www.googleapis.com/auth/gmail.readonly"],
                expires_at=None,
            )

    monkeypatch.setattr(
        agent_gmail_oauth_routes,
        "_get_service",
        lambda: FakeOAuthService(),
    )

    client = _make_app().test_client()
    start_response = client.get(
        "/von/api/agent/gmail/oauth/start?profile_id=vonwitbrock-gmail",
        base_url="http://localhost:5001",
    )
    assert start_response.status_code == 302

    callback_response = client.get(
        "/von/api/agent/gmail/oauth/callback?state=state-1&code=abc",
        base_url="http://localhost:5001",
    )

    assert callback_response.status_code == 200
    assert captured["profile_id"] == "vonwitbrock-gmail"
    assert captured["code_verifier"] == "verifier-1"
    assert (
        captured["redirect_uri"]
        == "http://localhost:5001/von/api/agent/gmail/oauth/callback"
    )

    agent_gmail_oauth_routes._agent_oauth_states.clear()
