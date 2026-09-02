from __future__ import annotations

import time

import pytest
from flask import Flask

from src.backend.server.routes import auth_routes


class _ProxyAwareGoogleAuthService:
    canonical_origin = "https://spark.example.test"
    redirect_uri = "https://spark.example.test/von/api/auth/google/callback"

    def __init__(self) -> None:
        self.exchanged_url: str | None = None

    def request_host_matches_canonical_origin(self, host: str) -> bool:
        return host.casefold() == "spark.example.test"

    def allows_dynamic_host(self, host: str) -> bool:
        del host
        return False

    def get_authorization_url(self) -> tuple[str, str]:
        return (
            "https://accounts.google.test/o/oauth2/auth?state=proxy-state",
            "proxy-state",
        )

    def canonicalise_authorization_response_url(self, request_url: str) -> str:
        if request_url.startswith("http://spark.example.test/"):
            return request_url.replace(
                "http://spark.example.test/",
                "https://spark.example.test/",
                1,
            )
        return request_url

    def exchange_code_for_tokens(
        self, authorization_response_url: str
    ) -> dict[str, str]:
        self.exchanged_url = authorization_response_url
        return {"email": "user@example.test", "name": "Example User"}


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch):
    app = Flask(__name__)
    app.secret_key = "proxy-oauth-test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(auth_routes.auth_bp, url_prefix="/von")
    service = _ProxyAwareGoogleAuthService()
    auth_routes._set_google_auth_service(service)
    auth_routes._oauth_states.clear()
    monkeypatch.setattr(
        auth_routes,
        "set_current_user_by_email",
        lambda email, name: {
            "concept_id": "#V#example_user",
            "email": email,
            "name": name,
        },
    )

    with app.test_client() as client:
        yield client, service

    auth_routes._set_google_auth_service(None)
    auth_routes._oauth_states.clear()


def test_login_accepts_internal_http_for_exact_configured_proxy_host(
    app_client,
) -> None:
    client, _ = app_client

    response = client.get(
        "/von/api/auth/google/login",
        base_url="http://spark.example.test",
    )

    assert response.status_code == 302
    assert response.headers["Location"].startswith("https://accounts.google.test/")


def test_login_rejects_a_different_proxy_host(app_client) -> None:
    client, _ = app_client

    response = client.get(
        "/von/api/auth/google/login",
        base_url="http://other.example.test",
    )

    assert response.status_code == 400
    assert b"Host Mismatch Detected" in response.data


def test_callback_exchanges_using_configured_external_https_url(app_client) -> None:
    client, service = app_client
    auth_routes._oauth_states["proxy-state"] = {
        "timestamp": time.time(),
        "used": False,
    }

    response = client.get(
        "/von/api/auth/google/callback?code=abc&state=proxy-state",
        base_url="http://spark.example.test",
    )

    assert response.status_code == 302
    assert service.exchanged_url == (
        "https://spark.example.test/von/api/auth/google/callback"
        "?code=abc&state=proxy-state"
    )
