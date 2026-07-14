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


def test_ollama_hosts_post_rejects_member_without_side_effects(monkeypatch):
    app = _make_settings_app()
    calls: list[str] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_ollama_hosts_list",
        lambda _hosts: calls.append("hosts") or True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_active_ollama_host",
        lambda _host: calls.append("active") or True,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role_in_org"] = "member"

        response = client.post(
            "/api/settings/ollama/hosts",
            json={
                "hosts": [{"url": "http://127.0.0.1:11434"}],
                "active_host": "http://127.0.0.1:11434",
            },
        )

    assert response.status_code == 403
    assert calls == []


def test_ollama_hosts_post_allows_admin(monkeypatch):
    app = _make_settings_app()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_ollama_hosts_list",
        lambda hosts: calls.append(("hosts", hosts)) or True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_active_ollama_host",
        lambda host: calls.append(("active", host)) or True,
    )
    hosts = [{"url": "http://127.0.0.1:11434"}]

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role_in_org"] = "admin"

        response = client.post(
            "/api/settings/ollama/hosts",
            json={
                "hosts": hosts,
                "active_host": "http://127.0.0.1:11434",
            },
        )

    assert response.status_code == 200
    assert calls == [
        ("hosts", hosts),
        ("active", "http://127.0.0.1:11434"),
    ]


def test_ollama_hosts_post_validates_before_admin_side_effect(monkeypatch):
    app = _make_settings_app()
    calls: list[str] = []
    monkeypatch.setattr(
        "src.backend.server.routes.settings_routes.set_ollama_hosts_list",
        lambda _hosts: calls.append("hosts") or True,
    )

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role_in_org"] = "owner"

        response = client.post(
            "/api/settings/ollama/hosts",
            json={"hosts": "http://127.0.0.1:11434"},
        )

    assert response.status_code == 400
    assert calls == []
