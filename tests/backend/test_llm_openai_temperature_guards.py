"""Regression tests for OpenAI temperature guards in llm_interface."""

from __future__ import annotations

import types

import src.backend.services  # noqa: F401


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


def test_llm_interface_openai_structured_config_omits_temperature_for_gpt5_mini(
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
    assert config.temperature is None


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


def test_llm_interface_openai_generate_applies_timeout_as_request_option(
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
    assert captured_options == {"timeout": 12.0, "max_retries": 0}
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
