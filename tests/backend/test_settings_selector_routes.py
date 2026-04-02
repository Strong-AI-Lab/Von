from __future__ import annotations

from flask import Flask

from src.backend.server.routes.settings_routes import settings_bp


def _make_settings_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(settings_bp, url_prefix="/api/settings")
    return app


def test_people_selector_returns_retryable_503_when_concepts_collection_unavailable(
    monkeypatch,
):
    import src.backend.server.routes.settings_routes as settings_routes

    app = _make_settings_app()
    monkeypatch.setattr(
        settings_routes.ConceptsRepository,
        "collection",
        staticmethod(lambda: None),
    )

    with app.test_client() as client:
        response = client.get("/api/settings/people")

    payload = response.get_json() or {}
    assert response.status_code == 503
    assert payload["retryable"] is True
    assert payload["reason"] == "concepts_collection_unavailable"
    assert payload["retry_after_seconds"] == 5
    assert response.headers["Retry-After"] == "5"


def test_organisation_selector_returns_retryable_503_when_concepts_collection_unavailable(
    monkeypatch,
):
    import src.backend.server.routes.settings_routes as settings_routes

    app = _make_settings_app()
    monkeypatch.setattr(
        settings_routes.ConceptsRepository,
        "collection",
        staticmethod(lambda: None),
    )

    with app.test_client() as client:
        response = client.get("/api/settings/organisations")

    payload = response.get_json() or {}
    assert response.status_code == 503
    assert payload["retryable"] is True
    assert payload["reason"] == "concepts_collection_unavailable"
    assert payload["retry_after_seconds"] == 5
    assert response.headers["Retry-After"] == "5"
