import json
import os

import pytest

from oauthlib.oauth2.rfc6749.parameters import parse_token_response

from src.backend.services import agent_gmail_oauth_service as oauth_module
from src.backend.services.agent_gmail_oauth_service import AgentGmailOAuthService


def test_oauthlib_scope_change_is_warning_exception() -> None:
    body = json.dumps(
        {
            "access_token": "x",
            "token_type": "Bearer",
            "scope": "openid https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/userinfo.email",
        }
    )

    with pytest.raises(Warning):
        parse_token_response(
            body, scope="https://www.googleapis.com/auth/gmail.readonly"
        )


def test_agent_gmail_oauth_relaxes_token_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyCreds:
        expiry = None

        def to_json(self) -> str:
            return json.dumps({"token": "x"})

    class DummyFlow:
        def __init__(self):
            self.credentials = DummyCreds()

        def fetch_token(self, authorization_response: str) -> None:
            # This mimics oauthlib raising Warning when scopes differ.
            body = json.dumps(
                {
                    "access_token": "x",
                    "token_type": "Bearer",
                    "scope": "openid https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/userinfo.email",
                }
            )
            parse_token_response(
                body, scope="https://www.googleapis.com/auth/gmail.readonly"
            )

    class DummyProfile:
        scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

    stored: dict[str, object] = {}

    def fake_upsert_agent_gmail_tokens(**kwargs):
        stored.update(kwargs)

    monkeypatch.setattr(
        "src.backend.services.agent_gmail_oauth_service.upsert_agent_gmail_tokens",
        fake_upsert_agent_gmail_tokens,
    )

    class TestService(AgentGmailOAuthService):
        def _build_flow(self, *, profile_id: str):  # type: ignore[override]
            return DummyFlow()

        def _get_profile(self, profile_id: str):  # type: ignore[override]
            return DummyProfile()  # type: ignore[return-value]

        def _get_authorised_email(self, creds: object):  # type: ignore[override]
            return None

    previous = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
    os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)

    try:
        result = TestService().exchange_code_for_tokens(
            profile_id="zhan-gmail",
            authorisation_response_url="http://localhost/callback?code=abc&state=xyz",
        )

        assert result.profile_id == "zhan-gmail"
        assert stored.get("profile_id") == "zhan-gmail"
        assert stored.get("token_payload") == {"token": "x"}

        # Ensure we don't leak env var changes outside the exchange.
        assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") is None
    finally:
        if previous is None:
            os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
        else:
            os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous


def test_agent_gmail_oauth_allows_localhost_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_ALLOW_INSECURE_TRANSPORT", raising=False)
    monkeypatch.setenv(
        "VON_AGENT_GMAIL_OAUTH_REDIRECT_URI",
        "http://localhost:5000/von/api/agent/gmail/oauth/callback",
    )

    secret_path = tmp_path / "client_secret.json"
    secret_path.write_text("{}", encoding="utf-8")

    class DummyProfile:
        credentials_path = str(secret_path)
        scopes = ["https://www.googleapis.com/auth/gmail.send"]

    captured: dict[str, object] = {}

    def fake_from_client_secrets_file(path, *, scopes, redirect_uri):
        captured.update(
            {
                "path": path,
                "scopes": list(scopes),
                "redirect_uri": redirect_uri,
            }
        )
        return object()

    monkeypatch.setattr(
        oauth_module.Flow,
        "from_client_secrets_file",
        staticmethod(fake_from_client_secrets_file),
    )

    service = AgentGmailOAuthService(profiles={"zhan-gmail": DummyProfile()})  # type: ignore[arg-type]
    service._build_flow(profile_id="zhan-gmail")

    assert os.getenv("OAUTHLIB_INSECURE_TRANSPORT") == "1"
    assert captured == {
        "path": str(secret_path),
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        "redirect_uri": "http://localhost:5000/von/api/agent/gmail/oauth/callback",
    }
