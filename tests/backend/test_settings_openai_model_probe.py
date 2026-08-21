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


def test_model_key_presence_check_rejects_arbitrary_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    monkeypatch.setenv("REVIEW_SHORT_SECRET", "review-secret")
    monkeypatch.setattr(
        settings_routes,
        "get_openai_env_var",
        lambda: "OPENAI_API_KEY",
    )

    response = _make_app().test_client().post(
        "/api/settings/env_var/check",
        json={"env_var_name": "REVIEW_SHORT_SECRET"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "Unsupported model API key source."}
    assert response.headers.get("Cache-Control") == "no-store"


def test_model_key_presence_check_never_returns_or_logs_key_fragments(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    fake_key = "review-short-key"
    caplog.set_level("INFO")
    monkeypatch.setenv("GEMINI_API_KEY", fake_key)
    monkeypatch.delenv("GEMINI_API_KEY_FILE", raising=False)
    monkeypatch.setattr(
        settings_routes,
        "read_repo_dotenv_values",
        lambda _keys: {},
    )

    response = _make_app().test_client().post(
        "/api/settings/env_var/check",
        json={"env_var_name": "GEMINI_API_KEY"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"exists": True, "source": "process"}
    assert response.headers.get("Cache-Control") == "no-store"
    assert fake_key not in caplog.text


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


def test_gemini_model_list_uses_configured_key_and_returns_bare_ids(monkeypatch) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    captured: dict[str, object] = {}

    class _FakeGeminiClient:
        def __init__(self, *, api_key: str, default_model: str | None = None):
            captured["api_key"] = api_key
            captured["default_model"] = default_model
            self.last_response_metadata: dict[str, object] = {}

        @staticmethod
        def list_models() -> list[str]:
            return ["gemini-3.7-flash", "gemini-3.1-pro-preview"]

    monkeypatch.setattr(settings_routes, "GeminiClient", _FakeGeminiClient)
    monkeypatch.setattr(
        settings_routes,
        "_resolve_settings_api_key",
        lambda *_args, **_kwargs: "configured-gemini-key",
    )

    response = _make_app().test_client().get("/api/settings/models/gemini")

    assert response.status_code == 200
    assert response.get_json() == ["gemini-3.7-flash", "gemini-3.1-pro-preview"]
    assert captured == {
        "api_key": "configured-gemini-key",
        "default_model": None,
    }
    assert response.headers.get("Cache-Control") == "no-store"


def test_gemini_key_verification_lists_models_without_generation(monkeypatch) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    generated = False

    class _FakeGeminiClient:
        def __init__(self, *, api_key: str, default_model: str | None = None):
            assert api_key == "configured-gemini-key"
            assert default_model is None

        @staticmethod
        def list_models() -> list[str]:
            return ["gemini-3.7-flash"]

        @staticmethod
        def generate(*_args, **_kwargs):  # pragma: no cover
            nonlocal generated
            generated = True

    monkeypatch.setattr(settings_routes, "GeminiClient", _FakeGeminiClient)
    monkeypatch.setattr(
        settings_routes,
        "_resolve_settings_api_key",
        lambda name, **_kwargs: (
            "configured-gemini-key" if name == "GEMINI_API_KEY" else None
        ),
    )

    response = _make_app().test_client().post(
        "/api/settings/gemini/verify",
        json={"api_key_env_var": "GEMINI_API_KEY"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "models": ["gemini-3.7-flash"],
    }
    assert generated is False


def test_llm_info_checks_the_exact_effective_gemini_model(monkeypatch) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    captured: dict[str, object] = {}

    class _FakeGeminiClient:
        def __init__(self, *, api_key: str, default_model: str):
            captured["api_key"] = api_key
            captured["default_model"] = default_model

        @staticmethod
        def list_models() -> list[str]:
            return ["gemini-3.7-flash"]

    monkeypatch.setattr(settings_routes, "GeminiClient", _FakeGeminiClient)
    monkeypatch.setattr(
        settings_routes,
        "resolve_llm_setting",
        lambda **_kwargs: {"provider": "openai", "model": "gpt-server-default"},
    )
    monkeypatch.setattr(
        settings_routes,
        "_resolve_settings_api_key",
        lambda *_args, **_kwargs: "configured-gemini-key",
    )

    response = _make_app().test_client().get(
        "/api/settings/llm/info"
        "?effective_provider=gemini&effective_model=gemini-3.7-flash"
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "provider": "gemini",
        "model": "gemini-3.7-flash",
        "status": "ready",
        "ping_ok": True,
        "error": None,
        "details": {},
    }
    assert captured == {
        "api_key": "configured-gemini-key",
        "default_model": "gemini-3.7-flash",
    }


def test_gemini_model_probe_uses_exact_allowed_provider_and_model(monkeypatch) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    captured: dict[str, object] = {}

    class _FakeGeminiClient:
        def __init__(self, *, api_key: str, default_model: str | None = None):
            captured["api_key"] = api_key
            captured["default_model"] = default_model

        @staticmethod
        def list_models() -> list[str]:
            return ["gemini-3.7-flash"]

        def generate(self, prompt: str, **kwargs):
            captured["prompt"] = prompt
            captured.update(kwargs)
            self.last_response_metadata = {"api_surface": "interactions"}
            return "OK"

    def _allow(**kwargs):
        captured["eligibility"] = kwargs
        return {"allowed": True}

    monkeypatch.setattr(settings_routes, "GeminiClient", _FakeGeminiClient)
    monkeypatch.setattr(settings_routes, "assert_model_execution_allowed", _allow)
    monkeypatch.setattr(
        settings_routes,
        "_resolve_settings_api_key",
        lambda *_args, **_kwargs: "configured-gemini-key",
    )

    response = _make_app().test_client().post(
        "/api/settings/gemini/test_model",
        json={
            "api_key_env_var": "GEMINI_API_KEY",
            "model": "gemini-3.7-flash",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["usable"] is True
    assert payload["model"] == "gemini-3.7-flash"
    assert payload["api_surface"] == "interactions"
    assert captured["eligibility"] == {
        "provider": "gemini",
        "model": "gemini-3.7-flash",
    }
    assert captured["default_model"] == "gemini-3.7-flash"
    assert captured["model"] == "gemini-3.7-flash"
    assert captured["llm_params"] is None
    assert response.headers.get("Cache-Control") == "no-store"


def test_gemini_model_probe_rejects_non_enabled_pair_before_key_or_client(
    monkeypatch,
) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    key_resolved = False
    constructed = False

    class _UnexpectedGeminiClient:
        def __init__(self, **_kwargs):
            nonlocal constructed
            constructed = True

    def _deny(**_kwargs):
        raise settings_routes.ModelExecutionEligibilityError(
            "Gemini model 'gemini-3.7-flash' is not enabled for the current actor.",
            provider="gemini",
            model="gemini-3.7-flash",
        )

    def _unexpected_key(*_args, **_kwargs):
        nonlocal key_resolved
        key_resolved = True
        return "unexpected"

    monkeypatch.setattr(settings_routes, "GeminiClient", _UnexpectedGeminiClient)
    monkeypatch.setattr(settings_routes, "assert_model_execution_allowed", _deny)
    monkeypatch.setattr(settings_routes, "_resolve_settings_api_key", _unexpected_key)

    response = _make_app().test_client().post(
        "/api/settings/gemini/test_model",
        json={"model": "gemini-3.7-flash"},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["usable"] is False
    assert payload["failure_kind"] == "model_not_enabled"
    assert key_resolved is False
    assert constructed is False


def test_gemini_model_probe_rejects_arbitrary_secret_source(monkeypatch) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    key_resolved = False
    constructed = False

    class _UnexpectedGeminiClient:
        def __init__(self, **_kwargs):
            nonlocal constructed
            constructed = True

    def _unexpected_key(*_args, **_kwargs):
        nonlocal key_resolved
        key_resolved = True
        return "unexpected"

    monkeypatch.setattr(settings_routes, "GeminiClient", _UnexpectedGeminiClient)
    monkeypatch.setattr(settings_routes, "_resolve_settings_api_key", _unexpected_key)

    response = _make_app().test_client().post(
        "/api/settings/gemini/test_model",
        json={
            "api_key_env_var": "ATLASSIAN_API_TOKEN",
            "model": "gemini-3.7-flash",
        },
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["usable"] is False
    assert payload["failure_kind"] == "invalid_key_env_var"
    assert key_resolved is False
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
