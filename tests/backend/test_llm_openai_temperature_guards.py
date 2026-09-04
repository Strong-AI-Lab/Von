"""Regression tests for OpenAI temperature guards in llm_interface."""

from __future__ import annotations

import types

import pytest

import src.backend.services  # noqa: F401


@pytest.fixture(autouse=True)
def _allow_external_model_for_parameter_transport_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These tests isolate OpenAI parameter transport, not eligibility policy."""

    from src.backend.languagemodels import llm_interface

    monkeypatch.setattr(
        llm_interface,
        "assert_model_execution_allowed",
        lambda **_kwargs: {"allowed": True},
    )


def _install_openai_temperature_registry(monkeypatch) -> None:
    import src.backend.services.model_registry_service as registry_module

    snapshot = {
        "source": "vontology_graph",
        "models": [
            {
                "model_id": "gpt-5.2",
                "provider": "openai",
                "concept_id": "#V#openai_gpt52",
                "registry_entry_id": "#V#openai_gpt52_registry_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#openai_gpt52_chat_completions_profile",
                        "api_surface": "chat_completions",
                        "parameter_constraints": [
                            {
                                "constraint_concept_id": "#V#openai_gpt52_temperature_omit_constraint",
                                "parameter_concept_id": "#V#temperature_parameter",
                                "parameter": "temperature",
                                "action": "omit",
                                "fixed_value": "1.0",
                            }
                        ],
                    }
                ],
            },
            {
                "model_id": "gpt-5-mini",
                "provider": "openai",
                "concept_id": "#V#openai_gpt_5_mini",
                "registry_entry_id": "#V#openai_gpt5_mini_registry_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": "#V#openai_gpt5_mini_chat_completions_profile",
                        "api_surface": "chat_completions",
                        "parameter_constraints": [
                            {
                                "constraint_concept_id": "#V#openai_gpt5_mini_temperature_omit_constraint",
                                "parameter_concept_id": "#V#temperature_parameter",
                                "parameter": "temperature",
                                "action": "omit",
                                "fixed_value": "1.0",
                            }
                        ],
                    }
                ],
            },
            {
                "model_id": "gpt-5.6-terra",
                "provider": "openai",
                "concept_id": "#V#openai_gpt_5_6_terra",
                "registry_entry_id": "#V#openai_gpt_5_6_terra_registry_entry",
                "api_profiles": [
                    {
                        "profile_concept_id": (
                            "#V#openai_gpt_5_6_terra_responses_profile"
                        ),
                        "api_surface": "responses",
                        "structured_tool_calling": "required",
                        "tool_continuation_mode": "stateless",
                        "response_storage_policy": "disabled",
                        "parameter_constraints": [
                            {
                                "constraint_concept_id": (
                                    "#V#terra_responses_temperature_omit"
                                ),
                                "parameter_concept_id": "#V#temperature_parameter",
                                "parameter": "temperature",
                                "action": "omit",
                            }
                        ],
                    },
                    {
                        "profile_concept_id": (
                            "#V#openai_gpt_5_6_terra_chat_completions_profile"
                        ),
                        "api_surface": "chat_completions",
                        "structured_tool_calling": "unsupported",
                        "tool_continuation_mode": "stateless",
                        "response_storage_policy": "disabled",
                        "parameter_constraints": [],
                    },
                ],
            },
        ],
    }
    monkeypatch.setattr(
        registry_module,
        "get_model_registry_snapshot",
        lambda *, preferred_language=None: snapshot,
    )


def _build_fake_openai_response(model: str, content: str = "ok"):
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ],
        model=model,
    )


def test_llm_interface_openai_structured_config_defers_temperature_policy(
    monkeypatch,
) -> None:
    import src.backend.languagemodels.llm_interface as mod

    _install_openai_temperature_registry(monkeypatch)
    monkeypatch.setattr(
        mod.openai,
        "OpenAI",
        lambda **_kwargs: object(),
    )

    client = mod.OpenAIClient(api_key="test-key")
    config = client._get_structured_client_config("gpt-5-mini")

    assert config.model == "gpt-5-mini"
    assert config.temperature == 0.7


def test_llm_interface_answer_only_uses_required_responses_profile(
    monkeypatch,
) -> None:
    import src.backend.languagemodels.llm_interface as mod
    from src.backend.languagemodels.structured_tool_calling.providers import (
        openai_client as provider_module,
    )

    _install_openai_temperature_registry(monkeypatch)
    captured: dict[str, list[dict[str, object]]] = {
        "responses": [],
        "chat": [],
        "client_options": [],
    }

    class _FakeAsyncOpenAI:
        def __init__(self, **_kwargs) -> None:
            self.responses = types.SimpleNamespace(create=self._responses_create)
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self._chat_create)
            )

        def with_options(self, **kwargs):
            captured["client_options"].append(dict(kwargs))
            return self

        async def close(self) -> None:
            return None

        async def _responses_create(self, **kwargs):
            captured["responses"].append(dict(kwargs))
            return {
                "id": "resp-answer-only",
                "model": "gpt-5.6-terra",
                "output": [
                    {
                        "id": "msg-answer-only",
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "A grounded answer-only response.",
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 6,
                    "total_tokens": 26,
                },
            }

        async def _chat_create(self, **kwargs):
            captured["chat"].append(dict(kwargs))
            raise AssertionError("Answer-only request unexpectedly used Chat")

    monkeypatch.setattr(
        provider_module.openai,
        "AsyncOpenAI",
        _FakeAsyncOpenAI,
    )
    monkeypatch.setattr(mod.openai, "OpenAI", lambda **_kwargs: object())

    client = mod.OpenAIClient(api_key="test-key")
    result = client.generate_with_tools(
        prompt="Produce the final grounded answer.",
        available_tools=[],
        context=[
            {
                "role": "user",
                "content": "Use the bounded evidence already provided.",
            }
        ],
        model="gpt-5.6-terra",
        system_message="Answer from the supplied evidence. Do not call tools.",
        llm_params={"request_timeout_seconds": 20.0},
    )

    assert result.text_response == "A grounded answer-only response."
    assert captured["chat"] == []
    assert len(captured["responses"]) == 1
    assert "temperature" not in captured["responses"][0]
    assert captured["responses"][0]["store"] is False
    assert result.transport_metadata["tools_present"] is False
    assert result.transport_metadata["effective_api_surface"] == "responses"
    assert result.transport_metadata["profile_concept_id"] == (
        "#V#openai_gpt_5_6_terra_responses_profile"
    )
    assert captured["client_options"] == []


def test_llm_interface_openai_generate_omits_temperature_for_gpt5_mini(
    monkeypatch,
) -> None:
    import src.backend.languagemodels.llm_interface as mod

    _install_openai_temperature_registry(monkeypatch)
    captured_kwargs: dict[str, object] = {}

    def _create(**kwargs):
        captured_kwargs.update(kwargs)
        return _build_fake_openai_response("gpt-5-mini")

    monkeypatch.setattr(
        mod.openai,
        "OpenAI",
        lambda **_kwargs: types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=_create)
            )
        ),
    )

    client = mod.OpenAIClient(api_key="test-key")
    result = client.generate(
        prompt="hello",
        model="gpt-5-mini",
        llm_params={"temperature": 0.7},
    )

    assert result == "ok"
    assert "temperature" not in captured_kwargs


def test_llm_interface_openai_generate_preserves_temperature_for_gpt4(
    monkeypatch,
) -> None:
    import src.backend.languagemodels.llm_interface as mod

    _install_openai_temperature_registry(monkeypatch)
    captured_kwargs: dict[str, object] = {}

    def _create(**kwargs):
        captured_kwargs.update(kwargs)
        return _build_fake_openai_response("gpt-4")

    monkeypatch.setattr(
        mod.openai,
        "OpenAI",
        lambda **_kwargs: types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=_create)
            )
        ),
    )

    client = mod.OpenAIClient(api_key="test-key")
    result = client.generate(
        prompt="hello",
        model="gpt-4",
        llm_params={"temperature": 0.7},
    )

    assert result == "ok"
    assert captured_kwargs["temperature"] == 0.7


def test_llm_interface_openai_generate_treats_timeout_alias_as_advisory(
    monkeypatch,
) -> None:
    import src.backend.languagemodels.llm_interface as mod

    captured_options: dict[str, object] = {}
    captured_kwargs: dict[str, object] = {}

    def _create(**kwargs):
        captured_kwargs.update(kwargs)
        return _build_fake_openai_response("gpt-4")

    class _FakeOpenAI:
        def __init__(self):
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=_create)
            )

        def with_options(self, **kwargs):
            captured_options.update(kwargs)
            return self

    monkeypatch.setattr(
        mod.openai,
        "OpenAI",
        lambda **_kwargs: _FakeOpenAI(),
    )

    client = mod.OpenAIClient(api_key="test-key")
    result = client.generate(
        prompt="hello",
        model="gpt-4",
        llm_params={"temperature": 0.7, "timeout_seconds": 12},
    )

    assert result == "ok"
    assert captured_options == {}
    assert captured_kwargs["temperature"] == 0.7
    assert "timeout_seconds" not in captured_kwargs


def test_llm_interface_openai_generate_uses_responses_reasoning_effort(
    monkeypatch,
) -> None:
    import src.backend.languagemodels.llm_interface as mod

    captured_responses_kwargs: dict[str, object] = {}

    def _responses_create(**kwargs):
        captured_responses_kwargs.update(kwargs)
        return types.SimpleNamespace(output_text="ok", model="gpt-5.5")

    def _chat_create(**_kwargs):  # pragma: no cover
        raise AssertionError("chat completions should not be used for reasoning effort")

    monkeypatch.setattr(
        mod.openai,
        "OpenAI",
        lambda **_kwargs: types.SimpleNamespace(
            responses=types.SimpleNamespace(create=_responses_create),
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=_chat_create)
            ),
        ),
    )

    client = mod.OpenAIClient(api_key="test-key")
    result = client.generate(
        prompt="hello",
        model="gpt-5.5",
        llm_params={"model_parameters": {"reasoning_effort": "low"}},
    )

    assert result == "ok"
    assert captured_responses_kwargs["model"] == "gpt-5.5"
    assert captured_responses_kwargs["reasoning"] == {"effort": "low"}
    assert captured_responses_kwargs["max_output_tokens"] == 4096


def test_llm_interface_openai_generate_honours_bounded_responses_output(
    monkeypatch,
) -> None:
    import src.backend.languagemodels.llm_interface as mod

    captured_responses_kwargs: dict[str, object] = {}

    def _responses_create(**kwargs):
        captured_responses_kwargs.update(kwargs)
        return types.SimpleNamespace(output_text="ok", model="gpt-5.6-luna")

    monkeypatch.setattr(
        mod.openai,
        "OpenAI",
        lambda **_kwargs: types.SimpleNamespace(
            responses=types.SimpleNamespace(create=_responses_create),
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(
                    create=lambda **_kwargs: (_ for _ in ()).throw(
                        AssertionError("chat completions should not be used")
                    )
                )
            ),
        ),
    )

    client = mod.OpenAIClient(api_key="test-key")
    result = client.generate(
        prompt="hello",
        model="gpt-5.6-luna",
        llm_params={
            "model_parameters": {
                "reasoning_effort": "low",
                "max_output_tokens": 2048,
            }
        },
    )

    assert result == "ok"
    assert captured_responses_kwargs["max_output_tokens"] == 2048


def test_llm_interface_openai_generate_defaults_luna_to_responses(
    monkeypatch,
) -> None:
    import src.backend.languagemodels.llm_interface as mod

    captured_responses_kwargs: dict[str, object] = {}

    def _responses_create(**kwargs):
        captured_responses_kwargs.update(kwargs)
        return types.SimpleNamespace(output_text="ok", model="gpt-5.6-luna")

    monkeypatch.setattr(
        mod.openai,
        "OpenAI",
        lambda **_kwargs: types.SimpleNamespace(
            responses=types.SimpleNamespace(create=_responses_create),
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(
                    create=lambda **_kwargs: (_ for _ in ()).throw(
                        AssertionError("Luna must use the Responses API")
                    )
                )
            ),
        ),
    )

    client = mod.OpenAIClient(api_key="test-key")
    result = client.generate(prompt="hello", model="gpt-5.6-luna")

    assert result == "ok"
    assert captured_responses_kwargs["model"] == "gpt-5.6-luna"
    assert captured_responses_kwargs["max_output_tokens"] == 4096
    assert client.last_response_metadata["provider_observed_model"] == "gpt-5.6-luna"
    assert (
        client.last_response_metadata["transport_metadata"][
            "provider_observed_model"
        ]
        == "gpt-5.6-luna"
    )


def test_llm_interface_does_not_fabricate_missing_provider_model(
    monkeypatch,
) -> None:
    import src.backend.languagemodels.llm_interface as mod

    def _responses_create(**_kwargs):
        return types.SimpleNamespace(output_text="ok")

    monkeypatch.setattr(
        mod.openai,
        "OpenAI",
        lambda **_kwargs: types.SimpleNamespace(
            responses=types.SimpleNamespace(create=_responses_create),
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(
                    create=lambda **_kwargs: (_ for _ in ()).throw(
                        AssertionError("Luna must use the Responses API")
                    )
                )
            ),
        ),
    )

    client = mod.OpenAIClient(api_key="test-key")

    assert client.generate(prompt="hello", model="gpt-5.6-luna") == "ok"
    assert client.last_response_metadata["effective_model"] == "gpt-5.6-luna"
    assert client.last_response_metadata["provider_observed_model"] is None
    assert (
        client.last_response_metadata["transport_metadata"][
            "provider_observed_model"
        ]
        is None
    )


def test_openai_embedding_does_not_impose_fixed_request_timeout(monkeypatch) -> None:
    import src.backend.languagemodels.llm_interface as mod

    captured_options: dict[str, object] = {}

    def _embedding_create(**_kwargs):
        return types.SimpleNamespace(
            data=[types.SimpleNamespace(embedding=[0.25, 0.75])]
        )

    class _FakeClient:
        embeddings = types.SimpleNamespace(create=_embedding_create)

        def with_options(self, **kwargs):
            captured_options.update(kwargs)
            return self

    client = object.__new__(mod.OpenAIClient)
    client.client = _FakeClient()

    assert client.get_embedding("hello") == [0.25, 0.75]
    assert captured_options == {"max_retries": 0}
