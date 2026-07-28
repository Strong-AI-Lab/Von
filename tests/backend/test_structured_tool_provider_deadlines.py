"""Focused deadline tests for non-OpenAI structured-tool providers."""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from src.backend.languagemodels.structured_tool_calling.client import (
    LLMClient,
    LLMClientConfig,
)
from src.backend.languagemodels.structured_tool_calling.providers.gemini_client import (
    GeminiClient,
)
from src.backend.languagemodels.structured_tool_calling.providers.ollama_client import (
    OllamaClient,
)
from src.backend.languagemodels.structured_tool_calling.types import (
    ToolCallError,
    ToolDefinition,
)


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="lookup",
        description="Look up evidence.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )


class _Options:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def _gemini_client(
    *,
    generate_content: Any,
    captured: dict[str, Any],
) -> GeminiClient:
    client = GeminiClient.__new__(GeminiClient)
    LLMClient.__init__(
        client,
        LLMClientConfig(
            model="gemini-test",
            provider="gemini",
            api_key="test-key",
            temperature=None,
        ),
    )

    class _AioClient:
        def __init__(self) -> None:
            self.models = types.SimpleNamespace(generate_content=generate_content)

        async def aclose(self) -> None:
            captured["closed"] = True

    def client_factory(**kwargs: Any) -> Any:
        captured["client_kwargs"] = dict(kwargs)
        return types.SimpleNamespace(aio=_AioClient())

    client._genai = types.SimpleNamespace(  # type: ignore[attr-defined]
        Client=client_factory,
        types=types.SimpleNamespace(
            GenerateContentConfig=_Options,
            HttpOptions=_Options,
        ),
    )
    client._client = None  # type: ignore[attr-defined]
    client._client_kwargs = {"api_key": "test-key"}  # type: ignore[attr-defined]
    client._model_name = "gemini-test"  # type: ignore[attr-defined]
    client._temperature = None  # type: ignore[attr-defined]
    client._max_tokens = 2048  # type: ignore[attr-defined]
    client._convert_tools_to_gemini_format = lambda _tools: []  # type: ignore[method-assign]
    return client


def test_gemini_uses_native_timeout_and_keeps_it_out_of_model_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels.structured_tool_calling.providers import (
        gemini_client as provider_module,
    )

    captured: dict[str, Any] = {}

    async def generate_content(**kwargs: Any) -> Any:
        captured["request"] = dict(kwargs)
        return types.SimpleNamespace(text="grounded", function_calls=None)

    client = _gemini_client(
        generate_content=generate_content,
        captured=captured,
    )
    monotonic_values = iter((100.0, 100.0, 100.0))
    monkeypatch.setattr(
        provider_module,
        "monotonic",
        lambda: next(monotonic_values),
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Find evidence.",
            available_tools=[_tool()],
            llm_params={
                "request_timeout_seconds": 2.5,
                "reasoning_effort": "low",
            },
        )
    )

    http_options = captured["client_kwargs"]["http_options"]
    assert http_options.timeout == 2500
    assert set(captured["request"]) == {"model", "contents", "config"}
    assert result.text_response == "grounded"
    assert captured["closed"] is True


def test_gemini_absolute_deadline_cancels_native_request() -> None:
    captured: dict[str, Any] = {}

    async def generate_content(**_kwargs: Any) -> Any:
        await asyncio.sleep(1.0)
        return types.SimpleNamespace(text="too late", function_calls=None)

    client = _gemini_client(
        generate_content=generate_content,
        captured=captured,
    )

    with pytest.raises(ToolCallError, match="deadline exhausted"):
        asyncio.run(
            client.generate_with_tools(
                prompt="Find evidence.",
                available_tools=[_tool()],
                llm_params={"request_timeout_seconds": 0.01},
            )
        )

    assert captured["closed"] is True


def _ollama_client(
    *,
    chat: Any,
    captured: dict[str, Any],
) -> OllamaClient:
    client = OllamaClient.__new__(OllamaClient)
    LLMClient.__init__(
        client,
        LLMClientConfig(
            model="ollama-test",
            provider="ollama",
            base_url="http://ollama.test",
            temperature=None,
        ),
    )

    class _AsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client_kwargs"] = dict(kwargs)

        async def chat(self, **kwargs: Any) -> Any:
            return await chat(**kwargs)

        async def close(self) -> None:
            captured["closed"] = True

    client._ollama = types.SimpleNamespace(AsyncClient=_AsyncClient)  # type: ignore[attr-defined]
    client._base_url = "http://ollama.test"  # type: ignore[attr-defined]
    return client


def test_ollama_uses_native_timeout_and_keeps_it_out_of_model_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels.structured_tool_calling.providers import (
        ollama_client as provider_module,
    )

    captured: dict[str, Any] = {}

    async def chat(**kwargs: Any) -> Any:
        captured["request"] = dict(kwargs)

        async def chunks() -> Any:
            yield types.SimpleNamespace(
                message=types.SimpleNamespace(content="grounded")
            )

        return chunks()

    client = _ollama_client(chat=chat, captured=captured)
    monotonic_values = iter((100.0, 100.0, 100.0))
    monkeypatch.setattr(
        provider_module,
        "monotonic",
        lambda: next(monotonic_values),
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Find evidence.",
            available_tools=[_tool()],
            llm_params={
                "request_timeout_seconds": 2.5,
                "reasoning_effort": "low",
            },
        )
    )

    assert captured["client_kwargs"] == {
        "host": "http://ollama.test",
        "timeout": 2.5,
    }
    assert set(captured["request"]) == {"model", "messages", "stream"}
    assert result.text_response == "grounded"
    assert captured["closed"] is True


def test_ollama_absolute_deadline_cancels_native_request() -> None:
    captured: dict[str, Any] = {}

    async def chat(**_kwargs: Any) -> Any:
        await asyncio.sleep(1.0)

        async def chunks() -> Any:
            yield {"message": {"content": "too late"}}

        return chunks()

    client = _ollama_client(chat=chat, captured=captured)

    with pytest.raises(ToolCallError, match="deadline exhausted"):
        asyncio.run(
            client.generate_with_tools(
                prompt="Find evidence.",
                available_tools=[_tool()],
                llm_params={"request_timeout_seconds": 0.01},
            )
        )

    assert captured["closed"] is True
