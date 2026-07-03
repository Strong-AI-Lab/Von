from __future__ import annotations

import os
import types

import pytest

from src.backend import auth_service
from src.backend.auth_service import GoogleAuthService


class _DummyFlow:
    def __init__(self) -> None:
        self.credentials = types.SimpleNamespace(id_token="dummy-id-token")
        self.redirect_uri = None
        self.client_config = {"web": {"redirect_uris": []}}
        self.fetched_authorization_response = None

    def fetch_token(self, *, authorization_response: str) -> None:
        self.fetched_authorization_response = authorization_response


def _install_dummy_flow(monkeypatch: pytest.MonkeyPatch) -> _DummyFlow:
    dummy_flow = _DummyFlow()

    def _from_client_config(*args, **kwargs):
        del args, kwargs
        return dummy_flow

    monkeypatch.setattr(
        auth_service.Flow,
        "from_client_config",
        staticmethod(_from_client_config),
    )
    return dummy_flow


def _set_required_google_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "GOOGLE_OAUTH_REDIRECT_URI",
        "http://localhost:5001/von/api/auth/google/callback",
    )


def test_google_oauth_verifier_uses_small_default_clock_skew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_google_oauth_env(monkeypatch)
    monkeypatch.delenv("GOOGLE_OAUTH_CLOCK_SKEW_SECONDS", raising=False)
    dummy_flow = _install_dummy_flow(monkeypatch)

    verify_kwargs: dict[str, object] = {}

    def _verify_oauth2_token(**kwargs):
        verify_kwargs.update(kwargs)
        return {"email": "user@example.test", "name": "User"}

    monkeypatch.setattr(auth_service.id_token, "verify_oauth2_token", _verify_oauth2_token)

    previous = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
    os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
    try:
        result = GoogleAuthService().exchange_code_for_tokens(
            "http://localhost:5001/von/api/auth/google/callback?code=abc&state=xyz"
        )
    finally:
        if previous is None:
            os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
        else:
            os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous

    assert result["email"] == "user@example.test"
    assert dummy_flow.fetched_authorization_response.endswith("code=abc&state=xyz")
    assert verify_kwargs["id_token"] == "dummy-id-token"
    assert verify_kwargs["audience"] == "client-id"
    assert verify_kwargs["clock_skew_in_seconds"] == 10
    assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") is None


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("2", 2),
        ("999", 300),
        ("not-an-int", 10),
    ],
)
def test_google_oauth_clock_skew_env_override_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str,
    expected: int,
) -> None:
    _set_required_google_oauth_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_OAUTH_CLOCK_SKEW_SECONDS", raw_value)
    _install_dummy_flow(monkeypatch)

    assert GoogleAuthService().clock_skew_seconds == expected
