from __future__ import annotations

import types

import pytest
from flask import Flask

from src.backend.server.routes.settings_routes import settings_bp


@pytest.fixture(autouse=True)
def _allow_external_model_for_probe_transport_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These tests isolate probe transport; policy denial has its own case."""

    import src.backend.server.routes.settings_routes as settings_routes

    monkeypatch.setattr(
        settings_routes,
        "assert_model_execution_allowed",
        lambda **_kwargs: {"allowed": True},
    )


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
    assert payload["api_surface"] == "chat_completions"
    assert payload["fallback_used"] is True
    assert payload["responses_failure_kind"] == "unexpected_error"
    assert captured_completion_kwargs["model"] == "gpt-5.5-2026-04-23"
    assert "temperature" not in captured_completion_kwargs


def test_openai_model_probe_passes_reasoning_effort_to_responses(
    monkeypatch,
) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    captured_responses_kwargs: dict[str, object] = {}

    class _FakeResponses:
        @staticmethod
        def create(**kwargs):
            captured_responses_kwargs.update(kwargs)
            return types.SimpleNamespace(model="gpt-5.5")

    class _FakeCompletions:
        @staticmethod
        def create(**_kwargs):  # pragma: no cover
            raise AssertionError("chat fallback should not be used")

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
            return ["gpt-5.5"]

    monkeypatch.setattr(settings_routes, "OpenAIClient", _FakeOpenAIClient)
    monkeypatch.setattr(settings_routes, "get_openai_env_var", lambda: "OPENAI_API_KEY")

    response = _make_app().test_client().post(
        "/api/settings/openai/test_model",
        json={
            "api_key_env_var": "OPENAI_API_KEY",
            "model": "gpt-5.5",
            "model_parameters": {"reasoning_effort": "low"},
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["usable"] is True
    assert payload["model_parameters"] == {"reasoning_effort": "low"}
    assert payload["api_surface"] == "responses"
    assert payload["fallback_used"] is False
    assert captured_responses_kwargs["reasoning"] == {"effort": "low"}
    assert captured_responses_kwargs["max_output_tokens"] == 4096


def test_openai_model_probe_rejects_non_enabled_model_before_client_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    constructed = False

    class _UnexpectedOpenAIClient:
        def __init__(self, **_kwargs):
            nonlocal constructed
            constructed = True

    def _deny(**_kwargs):
        raise settings_routes.ModelExecutionEligibilityError(
            "OpenAI model 'gpt-5.6-terra' is not enabled for the current user "
            "or organisation.",
            provider="openai",
            model="gpt-5.6-terra",
        )

    monkeypatch.setattr(settings_routes, "OpenAIClient", _UnexpectedOpenAIClient)
    monkeypatch.setattr(settings_routes, "assert_model_execution_allowed", _deny)
    monkeypatch.setattr(settings_routes, "get_openai_env_var", lambda: "OPENAI_API_KEY")

    response = _make_app().test_client().post(
        "/api/settings/openai/test_model",
        json={
            "api_key_env_var": "OPENAI_API_KEY",
            "model": "gpt-5.6-terra",
        },
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["usable"] is False
    assert payload["failure_kind"] == "model_not_enabled"
    assert "not enabled" in payload["reason"]
    assert constructed is False


def test_ollama_model_probe_uses_selected_host_and_model(monkeypatch) -> None:
    import src.backend.languagemodels.llm_interface as llm_interface

    captured_generate_kwargs: dict[str, object] = {}

    class _FakeOllamaClient:
        def __init__(self, *, host: str | None = None):
            self.host = host or "http://localhost:11434"

        @staticmethod
        def list_models() -> list[str]:
            return ["llama3.1:8b"]

        def generate(self, prompt: str, **kwargs):
            captured_generate_kwargs["prompt"] = prompt
            captured_generate_kwargs.update(kwargs)
            return "OK"

    monkeypatch.setattr(llm_interface, "OllamaClient", _FakeOllamaClient)

    response = _make_app().test_client().post(
        "/api/settings/ollama/test_model",
        json={
            "host_url": "http://localhost:11434",
            "model": "llama3.1:8b",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["usable"] is True
    assert payload["model"] == "llama3.1:8b"
    assert payload["host_url"] == "http://localhost:11434"
    assert captured_generate_kwargs["model"] == "llama3.1:8b"
    assert captured_generate_kwargs["llm_params"] == {"num_predict": 8}
    assert response.headers.get("Cache-Control") == "no-store"


def test_ollama_model_probe_reports_missing_model() -> None:
    response = _make_app().test_client().post(
        "/api/settings/ollama/test_model",
        json={"host_url": "http://localhost:11434", "model": ""},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["usable"] is False
    assert payload["failure_kind"] == "missing_model"


def test_ollama_model_probe_reports_unavailable_model(monkeypatch) -> None:
    import src.backend.languagemodels.llm_interface as llm_interface

    class _FakeOllamaClient:
        def __init__(self, *, host: str | None = None):
            self.host = host or "http://localhost:11434"

        @staticmethod
        def list_models() -> list[str]:
            return ["mistral:7b"]

    monkeypatch.setattr(llm_interface, "OllamaClient", _FakeOllamaClient)

    response = _make_app().test_client().post(
        "/api/settings/ollama/test_model",
        json={"host_url": "http://localhost:11434", "model": "llama3.1:8b"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["usable"] is False
    assert payload["failure_kind"] == "model_unavailable"
