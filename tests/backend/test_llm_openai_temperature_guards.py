"""Regression tests for OpenAI temperature guards in llm_interface."""

from __future__ import annotations

import types

import src.backend.services  # noqa: F401


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
