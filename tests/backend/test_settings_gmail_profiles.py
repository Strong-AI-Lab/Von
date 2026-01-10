from __future__ import annotations

from flask import Flask

from src.backend.server.routes.settings_routes import settings_bp


def _make_settings_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(settings_bp, url_prefix="/api/settings")
    return app


def test_settings_endpoint_returns_gmail_profiles_and_default(monkeypatch):
    app = _make_settings_app()

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.list_profile_ids_from_env",
        lambda: ["von-service", "zhan-gmail"],
    )
    monkeypatch.setenv("VON_GMAIL_DEFAULT_PROFILE", "von-service")

    with app.test_client() as client:
        resp = client.get("/api/settings/")
        assert resp.status_code == 200
        payload = resp.get_json() or {}
        assert set(payload.get("gmail_profiles") or []) == {"von-service", "zhan-gmail"}
        assert payload.get("gmail_default_profile") == "von-service"


def test_settings_endpoint_ignores_unknown_default(monkeypatch):
    app = _make_settings_app()

    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.list_profile_ids_from_env",
        lambda: ["von-service"],
    )
    monkeypatch.setenv("VON_GMAIL_DEFAULT_PROFILE", "missing-profile")

    with app.test_client() as client:
        resp = client.get("/api/settings/")
        assert resp.status_code == 200
        payload = resp.get_json() or {}
        assert payload.get("gmail_profiles") == ["von-service"]
        assert payload.get("gmail_default_profile") is None
