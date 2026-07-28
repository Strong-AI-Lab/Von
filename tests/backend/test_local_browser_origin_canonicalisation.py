from __future__ import annotations

from flask import Flask

from src.backend.server import utils_flask


def _make_app(monkeypatch, redirect_uri: str | None) -> Flask:
    if redirect_uri is None:
        monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)
    else:
        monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", redirect_uri)

    app = Flask(__name__)
    app.testing = True
    utils_flask._install_local_browser_origin_canonicalisation(app)

    @app.route("/", methods=["GET", "HEAD", "POST"])
    @app.route("/von", methods=["GET", "HEAD", "POST"])
    @app.route("/von/", methods=["GET", "HEAD", "POST"])
    @app.route("/von/workflow-studio", methods=["GET", "HEAD", "POST"])
    def browser_entry():
        return "browser-entry"

    @app.route("/health")
    def health():
        return "healthy"

    @app.route("/von/api/auth/status")
    def auth_status():
        return "status"

    @app.route("/static/app.js")
    def static_asset():
        return "asset"

    return app


def test_browser_entry_url_uses_localhost_for_ipv4_loopback_bind() -> None:
    assert (
        utils_flask.build_browser_entry_url("127.0.0.1", 5001)
        == "http://localhost:5001/von/"
    )


def test_browser_entry_url_preserves_other_bind_hosts() -> None:
    assert (
        utils_flask.build_browser_entry_url("localhost", 5001)
        == "http://localhost:5001/von/"
    )
    assert (
        utils_flask.build_browser_entry_url("192.0.2.10", 5001)
        == "http://192.0.2.10:5001/von/"
    )


def test_loopback_browser_entry_redirects_to_configured_localhost(monkeypatch) -> None:
    app = _make_app(
        monkeypatch,
        "http://localhost:5001/von/api/auth/google/callback",
    )

    response = app.test_client().get(
        "/von/?tab=settings&return=%2Fvon%2F",
        base_url="http://127.0.0.1:5001",
    )

    assert response.status_code == 302
    assert (
        response.headers["Location"]
        == "http://localhost:5001/von/?tab=settings&return=%2Fvon%2F"
    )
    assert response.headers["Cache-Control"] == "no-store"


def test_localhost_browser_entry_is_not_redirected(monkeypatch) -> None:
    app = _make_app(
        monkeypatch,
        "http://localhost:5001/von/api/auth/google/callback",
    )

    response = app.test_client().get(
        "/von/",
        base_url="http://localhost:5001",
    )

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "browser-entry"


def test_non_entry_loopback_requests_are_not_redirected(monkeypatch) -> None:
    app = _make_app(
        monkeypatch,
        "http://localhost:5001/von/api/auth/google/callback",
    )
    client = app.test_client()

    assert client.get("/health", base_url="http://127.0.0.1:5001").status_code == 200
    assert (
        client.get(
            "/von/api/auth/status",
            base_url="http://127.0.0.1:5001",
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/static/app.js",
            base_url="http://127.0.0.1:5001",
        ).status_code
        == 200
    )


def test_loopback_post_is_not_redirected(monkeypatch) -> None:
    app = _make_app(
        monkeypatch,
        "http://localhost:5001/von/api/auth/google/callback",
    )

    response = app.test_client().post(
        "/von/",
        base_url="http://127.0.0.1:5001",
    )

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "browser-entry"


def test_different_port_is_not_redirected(monkeypatch) -> None:
    app = _make_app(
        monkeypatch,
        "http://localhost:5001/von/api/auth/google/callback",
    )

    response = app.test_client().get(
        "/von/",
        base_url="http://127.0.0.1:5010",
    )

    assert response.status_code == 200


def test_hosted_or_unset_oauth_redirect_does_not_canonicalise(monkeypatch) -> None:
    for redirect_uri in (
        None,
        "https://von.example.org/von/api/auth/google/callback",
        "http://127.0.0.1:5001/von/api/auth/google/callback",
    ):
        app = _make_app(monkeypatch, redirect_uri)
        response = app.test_client().get(
            "/von/",
            base_url="http://127.0.0.1:5001",
        )
        assert response.status_code == 200


def test_von_app_factory_installs_local_browser_canonicalisation(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "GOOGLE_OAUTH_REDIRECT_URI",
        "http://localhost:5001/von/api/auth/google/callback",
    )
    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "0")
    monkeypatch.setenv("VON_PREWARM_DISABLE", "1")
    monkeypatch.setenv("VON_CONCEPT_SUMMARY_FIELD_BOOTSTRAP_ENABLE", "0")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "0")
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
    app.testing = True

    response = app.test_client().get(
        "/von/?tab=settings",
        base_url="http://127.0.0.1:5001",
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "http://localhost:5001/von/?tab=settings"
