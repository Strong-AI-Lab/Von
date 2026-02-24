from __future__ import annotations

from flask import Flask

from src.backend.server.routes.settings_routes import settings_bp


def _make_settings_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(settings_bp, url_prefix="/api/settings")
    return app


def test_ollama_models_route_uses_cache(monkeypatch):
    import src.backend.server.routes.settings_routes as settings_routes

    app = _make_settings_app()
    monkeypatch.setenv("VON_OLLAMA_MODELS_CACHE_TTL_SECONDS", "60")

    calls = {"count": 0}

    def _fake_list_models_from_all_hosts():
        calls["count"] += 1
        return [{"host_url": "http://127.0.0.1:11434", "name": "llama3.2"}]

    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.OllamaClient.list_models_from_all_hosts",
        _fake_list_models_from_all_hosts,
    )

    with settings_routes._OLLAMA_MODELS_CACHE_LOCK:
        settings_routes._OLLAMA_MODELS_CACHE.clear()

    with app.test_client() as client:
        first = client.get("/api/settings/ollama/models")
        second = client.get("/api/settings/ollama/models")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["count"] == 1
    payload = second.get_json() or {}
    assert payload.get("success") is True
    assert len(payload.get("models") or []) == 1


def test_ollama_models_route_nocache_bypasses_cache(monkeypatch):
    import src.backend.server.routes.settings_routes as settings_routes

    app = _make_settings_app()
    monkeypatch.setenv("VON_OLLAMA_MODELS_CACHE_TTL_SECONDS", "60")

    calls = {"count": 0}

    def _fake_list_models_from_all_hosts():
        calls["count"] += 1
        return [{"host_url": "http://127.0.0.1:11434", "name": "llama3.2"}]

    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.OllamaClient.list_models_from_all_hosts",
        _fake_list_models_from_all_hosts,
    )

    with settings_routes._OLLAMA_MODELS_CACHE_LOCK:
        settings_routes._OLLAMA_MODELS_CACHE.clear()

    with app.test_client() as client:
        first = client.get("/api/settings/ollama/models")
        second = client.get("/api/settings/ollama/models?nocache=true")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls["count"] == 2
