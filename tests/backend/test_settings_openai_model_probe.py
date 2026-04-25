from __future__ import annotations

import types

from flask import Flask

from src.backend.server.routes.settings_routes import settings_bp


def _make_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(settings_bp, url_prefix="/api/settings")
    return app


def test_openai_model_probe_chat_fallback_omits_temperature(monkeypatch) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    captured_completion_kwargs: dict[str, object] = {}

    class _FakeResponses:
        @staticmethod
        def create(**_kwargs):
            raise RuntimeError("responses unavailable")

    class _FakeCompletions:
        @staticmethod
        def create(**kwargs):
            captured_completion_kwargs.update(kwargs)
            return types.SimpleNamespace(model="gpt-5.5-2026-04-23")

    class _FakeOpenAIClient:
        def __init__(self, *, api_key_env_var: str):
            self.api_key_env_var = api_key_env_var
            self.client = types.SimpleNamespace(
                responses=_FakeResponses(),
                chat=types.SimpleNamespace(
                    completions=_FakeCompletions(),
                ),
            )

        @staticmethod
        def list_models() -> list[str]:
            return ["gpt-5.5-2026-04-23"]

    monkeypatch.setattr(settings_routes, "OpenAIClient", _FakeOpenAIClient)
    monkeypatch.setattr(settings_routes, "get_openai_env_var", lambda: "OPENAI_API_KEY")

    response = _make_app().test_client().post(
        "/api/settings/openai/test_model",
        json={
            "api_key_env_var": "OPENAI_API_KEY",
            "model": "gpt-5.5-2026-04-23",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["usable"] is True
    assert captured_completion_kwargs["model"] == "gpt-5.5-2026-04-23"
    assert "temperature" not in captured_completion_kwargs
