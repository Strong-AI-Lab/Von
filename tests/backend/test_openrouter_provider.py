from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest
from flask import Flask

from src.backend.languagemodels.structured_tool_calling import (
    LLMClientConfig,
    ToolDefinition,
    get_llm_client as get_structured_client,
)
from src.backend.languagemodels.structured_tool_calling.providers.openai_client import (
    OpenAIClient as StructuredOpenAIClient,
)
from src.backend.server.routes.settings_routes import settings_bp
from src.backend.services.llm_api_key_resolution import get_openrouter_api_key
from src.backend.services.settings_service import _normalise_llm_setting_entry


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="lookup",
        description="Look up a value.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )


def _settings_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(settings_bp, url_prefix="/api/settings")
    return app


def test_openrouter_key_resolves_from_secret_file(monkeypatch, tmp_path) -> None:
    secret_path = tmp_path / "openrouter_api_key"
    secret_path.write_text("test-openrouter-file-key\n", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(secret_path))

    assert get_openrouter_api_key() == "test-openrouter-file-key"


def test_explicit_openrouter_structured_provider_reuses_compatible_adapter() -> None:
    client = get_structured_client(
        LLMClientConfig(
            model="anthropic/claude-test",
            provider="openrouter",
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
        )
    )

    assert isinstance(client, StructuredOpenAIClient)
    assert client.config.provider == "openrouter"


def test_openrouter_structured_transport_never_selects_responses_beta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels.structured_tool_calling import transport

    monkeypatch.setattr(
        transport,
        "resolve_model_api_profiles",
        lambda **_kwargs: {
            "source": "test_registry",
            "api_profiles": [
                {
                    "api_surface": "responses",
                    "structured_tool_calling": "required",
                }
            ],
        },
    )

    decision = transport.resolve_structured_tool_transport(
        provider="openrouter",
        model="anthropic/claude-test",
        tools_present=True,
        requested_api_surface="chat_completions",
        connection_id="#V#openrouter_provider",
        deployment_id="anthropic/claude-test",
    )

    assert decision.status == "unsupported"
    assert decision.reason == "openrouter_chat_completions_only"


def test_unknown_explicit_structured_provider_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unknown provider 'unknown-provider'"):
        get_structured_client(
            LLMClientConfig(model="vendor/model", provider="unknown-provider")
        )


def test_openrouter_scoped_setting_preserves_qualified_model_slug() -> None:
    assert _normalise_llm_setting_entry(
        {"provider": "OpenRouter", "model": "anthropic/claude-test"}
    ) == {
        "provider": "openrouter",
        "model": "anthropic/claude-test",
    }


def test_openrouter_model_concept_resolves_to_catalogue_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels import llm_interface
    from src.backend.services import text_value_service

    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [
            {"text": "Example model"},
            {"text": "openrouter:anthropic/claude-test"},
        ],
    )

    assert (
        llm_interface.resolve_openrouter_model_name("#V#openrouter_example_model")
        == "anthropic/claude-test"
    )


def test_openrouter_chat_parameters_use_openrouter_registry_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import model_parameter_service

    observed: list[dict[str, Any]] = []

    def registry_lookup(**kwargs: Any) -> dict[str, Any]:
        observed.append(dict(kwargs))
        return {
            "action": "allow",
            "allowed_values": ["low"],
            "fixed_value": None,
            "source": "test_registry",
        }

    monkeypatch.setattr(
        model_parameter_service,
        "_registry_parameter_policy",
        registry_lookup,
    )

    normalised = model_parameter_service.normalise_model_parameters_for_storage(
        {"reasoning_effort": "low"},
        provider="openrouter",
        model="anthropic/claude-test",
        api_surface="chat_completions",
        include_registry=True,
        profile_concept_id="#V#openrouter_chat_profile",
    )

    assert normalised == {"reasoning_effort": "low"}
    assert (
        model_parameter_service.chat_completions_kwargs_from_model_parameters(
            normalised,
            provider="openrouter",
            model="anthropic/claude-test",
            profile_concept_id="#V#openrouter_chat_profile",
        )
        == {}
    )
    assert observed
    assert {item["provider"] for item in observed} == {"openrouter"}
    assert {item["profile_concept_id"] for item in observed} == {
        "#V#openrouter_chat_profile"
    }


def test_generate_request_honours_explicit_openrouter_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.server.routes import von_routes

    monkeypatch.setattr(
        von_routes,
        "normalise_model_parameters_for_storage",
        lambda *_args, **_kwargs: {},
    )

    model, provider, parameters = von_routes._resolve_generate_requested_model(
        {
            "model": "anthropic/claude-test",
            "model_provider": "openrouter",
        },
        user_concept_id="#V#user",
        org_concept_id="#V#org",
        configured_model=None,
    )

    assert model == "anthropic/claude-test"
    assert provider == "openrouter"
    assert parameters == {}


def test_openrouter_structured_request_enforces_privacy_and_retains_router_metadata() -> (
    None
):
    captured: dict[str, Any] = {}

    async def create(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "id": "gen-test-1",
            "model": "anthropic/claude-test-20260801",
            "choices": [{"message": {"content": "done", "tool_calls": []}}],
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 2,
                "total_tokens": 9,
                "cost": 0.00125,
                "cost_details": {"upstream_inference_cost": 0.001},
            },
            "openrouter_metadata": {
                "requested": {"model": "anthropic/claude-test"},
                "attempts": [{"provider": "Provider A", "status": 200}],
            },
        }

    request_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create),
        )
    )
    client = StructuredOpenAIClient(
        LLMClientConfig(
            model="anthropic/claude-test",
            provider="openrouter",
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            connection_id="#V#openrouter_provider",
            deployment_id="anthropic/claude-test",
            requested_api_surface="chat_completions",
        )
    )

    response = asyncio.run(
        client.generate_with_tools(
            prompt="Use the tool if needed.",
            available_tools=[_tool()],
            _request_client=request_client,
        )
    )

    assert captured["extra_body"]["provider"] == {
        "zdr": True,
        "data_collection": "deny",
        "require_parameters": True,
    }
    assert captured["extra_headers"]["X-OpenRouter-Metadata"] == "enabled"
    assert response.model == "anthropic/claude-test-20260801"
    assert response.usage is not None
    assert response.usage["provider_reported_cost"] == 0.00125
    assert response.transport_metadata["requested_provider"] == "openrouter"
    assert response.transport_metadata["effective_api_surface"] == "chat_completions"
    assert response.transport_metadata["generation_id"] == "gen-test-1"
    assert (
        response.transport_metadata["openrouter_metadata"]["attempts"][0]["provider"]
        == "Provider A"
    )
    assert response.transport_metadata["provider_routing_preferences"]["zdr"] is True


def test_openrouter_legacy_client_uses_chat_completions_and_records_actual_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.languagemodels.llm_interface as llm_interface

    captured: dict[str, Any] = {}
    response = types.SimpleNamespace(
        id="gen-test-2",
        model="google/gemini-test-202608",
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content="OK"),
                finish_reason="stop",
            )
        ],
        usage=types.SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=1,
            total_tokens=6,
            cost=0.0002,
            cost_details={"upstream_inference_cost": 0.00015},
        ),
        openrouter_metadata={"attempts": [{"provider": "Provider B"}]},
    )

    class _Completions:
        @staticmethod
        def create(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return response

    fake_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_Completions()),
    )
    monkeypatch.setattr(llm_interface.openai, "OpenAI", lambda **_kwargs: fake_client)
    client = llm_interface.OpenRouterClient(api_key="test-key")
    monkeypatch.setattr(
        client,
        "_assert_model_execution_allowed",
        lambda **_kwargs: {"allowed": True},
    )

    result = client.generate("Reply exactly with OK.", model="google/gemini-test")

    assert result == "OK"
    assert "input" not in captured
    assert captured["model"] == "google/gemini-test"
    assert captured["extra_body"]["provider"]["data_collection"] == "deny"
    assert client.last_response_metadata["provider"] == "openrouter"
    assert (
        client.last_response_metadata["effective_model"] == "google/gemini-test-202608"
    )
    assert client.last_response_metadata["usage"]["provider_reported_cost"] == 0.0002
    assert (
        client.last_response_metadata["transport_metadata"]["openrouter_metadata"][
            "attempts"
        ][0]["provider"]
        == "Provider B"
    )


def test_openrouter_settings_catalogue_uses_fixed_key_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, *, api_key: str):
            captured["api_key"] = api_key

        @staticmethod
        def list_models() -> list[str]:
            return ["anthropic/claude-test", "google/gemini-test"]

    monkeypatch.setattr(settings_routes, "OpenRouterClient", _Client)
    monkeypatch.setattr(
        settings_routes,
        "_resolve_settings_api_key",
        lambda name, **_kwargs: (
            "configured-key" if name == "OPENROUTER_API_KEY" else None
        ),
    )

    response = _settings_app().test_client().get("/api/settings/models/openrouter")

    assert response.status_code == 200
    assert response.get_json() == ["anthropic/claude-test", "google/gemini-test"]
    assert response.headers["Cache-Control"] == "no-store"
    assert captured["api_key"] == "configured-key"


def test_openrouter_settings_probe_requires_exact_eligibility_and_reports_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.server.routes.settings_routes as settings_routes

    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, *, api_key: str):
            captured["api_key"] = api_key

        @staticmethod
        def list_models() -> list[str]:
            return ["anthropic/claude-test"]

        @staticmethod
        def generate(prompt: str, *, model: str, llm_params=None) -> str:
            captured.update(prompt=prompt, model=model, llm_params=llm_params)
            return "OK"

    monkeypatch.setattr(settings_routes, "OpenRouterClient", _Client)
    monkeypatch.setattr(
        settings_routes,
        "assert_model_execution_allowed",
        lambda **kwargs: captured.setdefault("eligibility", dict(kwargs))
        or {"allowed": True},
    )
    monkeypatch.setattr(
        settings_routes,
        "_resolve_settings_api_key",
        lambda name, **_kwargs: (
            "configured-key" if name == "OPENROUTER_API_KEY" else None
        ),
    )
    monkeypatch.setattr(
        settings_routes,
        "normalise_model_parameters_for_storage",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        settings_routes,
        "build_model_parameter_capabilities",
        lambda **_kwargs: {"parameters": {}},
    )

    response = (
        _settings_app()
        .test_client()
        .post(
            "/api/settings/openrouter/test_model",
            json={
                "api_key_env_var": "OPENROUTER_API_KEY",
                "model": "anthropic/claude-test",
            },
        )
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["usable"] is True
    assert payload["model"] == "anthropic/claude-test"
    assert payload["api_surface"] == "chat_completions"
    assert payload["privacy_profile"] == {
        "zdr": True,
        "data_collection": "deny",
        "require_parameters": True,
    }
    assert captured["eligibility"] == {
        "provider": "openrouter",
        "model": "anthropic/claude-test",
    }
    assert captured["model"] == "anthropic/claude-test"
