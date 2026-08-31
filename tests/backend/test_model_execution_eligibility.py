from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask, session

from src.backend.languagemodels import llm_interface


def test_local_ollama_execution_does_not_require_scoped_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_interface,
        "resolve_enabled_llm_settings",
        lambda **_kwargs: pytest.fail("local execution must not read the premium pool"),
    )

    decision = llm_interface.assert_model_execution_allowed(
        provider="ollama",
        model="gemma4:31b",
    )

    assert decision["allowed"] is True
    assert decision["scope"] == "local_provider"


def test_unlisted_future_external_provider_does_not_bypass_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_interface,
        "resolve_enabled_llm_settings",
        lambda **_kwargs: [],
    )

    with pytest.raises(llm_interface.ModelExecutionEligibilityError):
        llm_interface.assert_model_execution_allowed(
            provider="anthropic",
            model="claude-example",
            user_concept_id="#V#current_user",
        )


def test_external_execution_requires_an_exact_enabled_provider_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_interface,
        "resolve_enabled_llm_settings",
        lambda **_kwargs: [
            {
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "scope": "user",
            }
        ],
    )

    decision = llm_interface.assert_model_execution_allowed(
        provider="openai",
        model="openai:gpt-5.6-terra",
        user_concept_id="#V#current_user",
    )

    assert decision == {
        "allowed": True,
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "scope": "user",
    }

    with pytest.raises(llm_interface.ModelExecutionEligibilityError) as exc_info:
        llm_interface.assert_model_execution_allowed(
            provider="openai",
            model="gpt-5.6-terra-preview",
            user_concept_id="#V#current_user",
        )

    assert exc_info.value.failure_kind == "model_not_enabled"
    assert "exact provider and model" in str(exc_info.value)


def test_external_execution_does_not_accept_a_provider_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_interface,
        "resolve_enabled_llm_settings",
        lambda **_kwargs: [
            {
                "provider": "gemini",
                "model": "gpt-5.6-terra",
                "scope": "organisation",
            }
        ],
    )

    with pytest.raises(llm_interface.ModelExecutionEligibilityError):
        llm_interface.assert_model_execution_allowed(
            provider="openai",
            model="gpt-5.6-terra",
            org_concept_id="#V#current_org",
        )


def test_actorless_external_execution_fails_closed_before_pool_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_interface,
        "resolve_enabled_llm_settings",
        lambda **_kwargs: pytest.fail("actorless execution must fail first"),
    )

    with pytest.raises(llm_interface.ModelExecutionEligibilityError) as exc_info:
        llm_interface.assert_model_execution_allowed(
            provider="openai",
            model="gpt-5.6-terra",
        )

    assert exc_info.value.failure_kind == "model_scope_required"
    assert "no authenticated user or organisation model scope" in str(
        exc_info.value
    )


def test_legacy_identity_header_cannot_authorise_paid_external_model_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.security import access_control

    app = Flask(__name__)
    app.secret_key = "eligibility-test"
    monkeypatch.setattr(
        access_control,
        "_validate_person_concept",
        lambda _concept_id: "#V#claimed_user",
    )
    monkeypatch.setattr(
        llm_interface,
        "resolve_enabled_llm_settings",
        lambda **_kwargs: pytest.fail(
            "legacy-header identity must be rejected before model-pool lookup"
        ),
    )

    with app.test_request_context(
        headers={"X-User-Concept-ID": "#V#claimed_user"}
    ), pytest.raises(llm_interface.ModelExecutionEligibilityError) as exc_info:
        llm_interface.assert_model_execution_allowed(
            provider="gemini",
            model="gemini-3.7-flash",
        )

    assert exc_info.value.failure_kind == "model_scope_required"


def test_authenticated_session_can_authorise_exact_enabled_gemini_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Flask(__name__)
    app.secret_key = "eligibility-test"
    monkeypatch.setattr(
        llm_interface,
        "resolve_enabled_llm_settings",
        lambda **kwargs: (
            [
                {
                    "provider": "gemini",
                    "model": "gemini-3.7-flash",
                    "scope": "user",
                }
            ]
            if kwargs.get("user_concept_id") == "#V#current_user"
            else []
        ),
    )

    with app.test_request_context():
        session["user_concept_id"] = "#V#current_user"
        decision = llm_interface.assert_model_execution_allowed(
            provider="gemini",
            model="gemini-3.7-flash",
        )

    assert decision == {
        "allowed": True,
        "provider": "gemini",
        "model": "gemini-3.7-flash",
        "scope": "user",
    }


def test_gemini_factory_binds_the_trusted_actor_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    monkeypatch.setattr(llm_interface, "initialize_clients", lambda **_kwargs: None)

    def _fake_gemini_client(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(llm_interface, "GeminiClient", _fake_gemini_client)

    result = llm_interface.get_llm_client(
        client_type="gemini",
        user_concept_id="#V#current_user",
        org_concept_id="#V#current_org",
    )

    assert result is sentinel
    assert captured["model_execution_user_concept_id"] == "#V#current_user"
    assert captured["model_execution_org_concept_id"] == "#V#current_org"
    assert captured["model_execution_actor_scope_bound"] is True


def test_zero_argument_factory_resolves_provider_from_authenticated_actor_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import settings_service

    app = Flask(__name__)
    app.secret_key = "eligibility-test"
    captured: dict[str, Any] = {}
    sentinel = object()

    monkeypatch.setattr(llm_interface, "initialize_clients", lambda **_kwargs: None)

    def fake_resolve_llm_setting(**kwargs: Any) -> dict[str, str]:
        captured["setting_scope"] = kwargs
        return {
            "provider": "gemini",
            "model": "gemini-3.7-flash",
            "scope": "user",
        }

    def fake_gemini_client(**kwargs: Any) -> object:
        captured["client_scope"] = kwargs
        return sentinel

    monkeypatch.setattr(settings_service, "resolve_llm_setting", fake_resolve_llm_setting)
    monkeypatch.setattr(llm_interface, "GeminiClient", fake_gemini_client)

    with app.test_request_context():
        session["user_concept_id"] = "#V#current_user"
        session["organisation_concept_id"] = "#V#current_org"
        result = llm_interface.get_llm_client()

    assert result is sentinel
    assert captured["setting_scope"] == {
        "user_concept_id": "#V#current_user",
        "org_concept_id": "#V#current_org",
    }
    assert captured["client_scope"]["model_execution_user_concept_id"] == (
        "#V#current_user"
    )
    assert captured["client_scope"]["model_execution_org_concept_id"] == (
        "#V#current_org"
    )


def test_actorless_factory_bound_gemini_embedding_is_denied_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[dict[str, Any]] = []

    class _Models:
        @staticmethod
        def embed_content(**kwargs: Any) -> object:
            provider_calls.append(kwargs)
            return SimpleNamespace(
                embeddings=[SimpleNamespace(values=[0.25, 0.75])]
            )

    client = object.__new__(llm_interface.GeminiClient)
    client.client = SimpleNamespace(models=_Models())
    client._model_execution_user_concept_id = None
    client._model_execution_org_concept_id = None
    client._model_execution_actor_scope_bound = True
    monkeypatch.setattr(
        llm_interface,
        "genai",
        SimpleNamespace(
            types=SimpleNamespace(EmbedContentConfig=lambda **kwargs: kwargs)
        ),
    )
    monkeypatch.setattr(
        llm_interface,
        "resolve_enabled_llm_settings",
        lambda **_kwargs: pytest.fail(
            "actorless embedding must be rejected before model-pool lookup"
        ),
    )

    with pytest.raises(llm_interface.ModelExecutionEligibilityError) as exc_info:
        client.get_embedding("private interaction text", model="gemini-embedding-001")

    assert exc_info.value.failure_kind == "model_scope_required"
    assert provider_calls == []


def test_exact_scoped_gemini_embedding_model_can_reach_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[dict[str, Any]] = []

    class _Models:
        @staticmethod
        def embed_content(**kwargs: Any) -> object:
            provider_calls.append(kwargs)
            return SimpleNamespace(
                embeddings=[SimpleNamespace(values=[0.25, 0.75])]
            )

    client = object.__new__(llm_interface.GeminiClient)
    client.client = SimpleNamespace(models=_Models())
    client._model_execution_user_concept_id = "#V#current_user"
    client._model_execution_org_concept_id = None
    client._model_execution_actor_scope_bound = True
    monkeypatch.setattr(
        llm_interface,
        "genai",
        SimpleNamespace(
            types=SimpleNamespace(EmbedContentConfig=lambda **kwargs: kwargs)
        ),
    )
    monkeypatch.setattr(
        llm_interface,
        "resolve_enabled_llm_settings",
        lambda **kwargs: (
            [
                {
                    "provider": "gemini",
                    "model": "gemini-embedding-001",
                    "scope": "user",
                }
            ]
            if kwargs.get("user_concept_id") == "#V#current_user"
            else []
        ),
    )

    embedding = client.get_embedding(
        "authorised interaction text",
        model="gemini-embedding-001",
    )

    assert embedding == [0.25, 0.75]
    assert len(provider_calls) == 1
    assert provider_calls[0]["model"] == "gemini-embedding-001"
    assert provider_calls[0]["contents"] == "authorised interaction text"


def test_structured_generation_checks_eligibility_before_client_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed = False

    def _unexpected_client(_config: Any) -> Any:
        nonlocal constructed
        constructed = True
        raise AssertionError("structured provider client must not be constructed")

    monkeypatch.setattr(llm_interface, "get_structured_client", _unexpected_client)

    class _ExternalClient(llm_interface.LLMInterface):
        def generate(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("legacy generation is not part of this test")

        def get_embedding(self, text: str) -> list[float]:
            raise AssertionError("embedding is not part of this test")

        def list_models(self) -> list[str]:
            raise AssertionError("model listing is not part of this test")

        def _get_structured_client_config(
            self, model: str | None
        ) -> llm_interface.LLMClientConfig:
            return llm_interface.LLMClientConfig(
                provider="openai",
                model=model or "gpt-5.6-terra",
            )

    with pytest.raises(llm_interface.ModelExecutionEligibilityError):
        _ExternalClient().generate_with_tools(
            prompt="hello",
            available_tools=[],
            model="gpt-5.6-terra",
        )

    assert constructed is False


def test_legacy_provider_generation_checks_eligibility_before_provider_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    class _UnexpectedResponses:
        @staticmethod
        def create(**_kwargs: Any) -> Any:
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("provider must not be called")

    openai_client = llm_interface.OpenAIClient.__new__(llm_interface.OpenAIClient)
    openai_client.client = type(
        "Client",
        (),
        {
            "responses": _UnexpectedResponses(),
            "chat": type(
                "Chat",
                (),
                {"completions": _UnexpectedResponses()},
            )(),
        },
    )()

    with pytest.raises(llm_interface.ModelExecutionEligibilityError):
        openai_client.generate("hello", model="gpt-5.6-terra")

    monkeypatch.setattr(llm_interface, "genai", object())
    gemini_client = llm_interface.GeminiClient.__new__(llm_interface.GeminiClient)
    gemini_client.default_model = "gemini-2.0-flash"
    gemini_client.client = object()

    with pytest.raises(llm_interface.ModelExecutionEligibilityError):
        gemini_client.generate("hello")

    assert provider_calls == 0
