from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from google import genai

from src.backend.languagemodels.structured_tool_calling.client import (
    LLMClient,
    LLMClientConfig,
)
from src.backend.languagemodels.structured_tool_calling.providers.gemini_client import (
    GeminiClient,
)
from src.backend.languagemodels.structured_tool_calling.types import (
    StructuredToolProtocolError,
    StructuredToolTransportError,
    ToolDefinition,
    ToolResult,
)


class _Resource:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return dict(self.__dict__)


class _Interactions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(dict(kwargs))
        return self.responses.pop(0)


class _Models:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.requests.append(dict(kwargs))
        return self.responses.pop(0)


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="lookup",
        description="Look up nested evidence.",
        input_schema={
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"term": {"type": "string"}},
                        "required": ["term"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["queries"],
            "additionalProperties": False,
        },
    )


def _client(interactions: _Interactions) -> GeminiClient:
    client = GeminiClient.__new__(GeminiClient)
    LLMClient.__init__(
        client,
        LLMClientConfig(
            model="gemini-3.7-flash",
            provider="gemini",
            api_key="test-key",
            connection_id="gemini_developer_api",
            deployment_id="gemini-3.7-flash",
            temperature=None,
        ),
    )
    client._genai = SimpleNamespace()  # type: ignore[attr-defined]
    client._client_kwargs = {"api_key": "test-key"}  # type: ignore[attr-defined]
    client._client = SimpleNamespace(  # type: ignore[attr-defined]
        aio=SimpleNamespace(interactions=interactions)
    )
    client._model_name = "gemini-3.7-flash"  # type: ignore[attr-defined]
    client._temperature = None  # type: ignore[attr-defined]
    client._max_tokens = 2048  # type: ignore[attr-defined]
    return client


def _legacy_client(models: _Models) -> GeminiClient:
    client = GeminiClient.__new__(GeminiClient)
    LLMClient.__init__(
        client,
        LLMClientConfig(
            model="gemini-2.5-flash",
            provider="gemini",
            api_key="test-key",
            connection_id="gemini_developer_api",
            deployment_id="gemini-2.5-flash",
            temperature=None,
        ),
    )
    client._genai = genai  # type: ignore[attr-defined]
    client._client_kwargs = {"api_key": "test-key"}  # type: ignore[attr-defined]
    client._client = SimpleNamespace(  # type: ignore[attr-defined]
        aio=SimpleNamespace(models=models)
    )
    client._model_name = "gemini-2.5-flash"  # type: ignore[attr-defined]
    client._temperature = None  # type: ignore[attr-defined]
    client._max_tokens = 2048  # type: ignore[attr-defined]
    return client


@pytest.fixture(autouse=True)
def _no_registry_hydration(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.backend.services import model_parameter_service

    monkeypatch.setattr(
        model_parameter_service,
        "_registry_parameter_policy",
        lambda **_kwargs: None,
    )


def test_interactions_replays_exact_steps_and_correlated_function_result() -> None:
    thought = _Resource(
        type="thought",
        summary=[{"type": "text", "text": "opaque reasoning"}],
        signature="thought-signature",
    )
    function_call = _Resource(
        type="function_call",
        id="call-17",
        name="lookup",
        arguments={"queries": [{"term": "evidence"}]},
        signature="function-signature",
    )
    first = _Resource(
        id="interaction-1",
        model="gemini-3.7-flash-20260815",
        status="requires_action",
        output_text="",
        steps=[thought, function_call],
        usage=_Resource(
            total_input_tokens=31,
            total_output_tokens=9,
            total_cached_tokens=4,
            total_thought_tokens=3,
            total_tool_use_tokens=2,
            total_tokens=40,
        ),
        errors=None,
    )
    final_step = _Resource(
        type="model_output",
        content=[{"type": "text", "text": "Grounded answer."}],
        signature="answer-signature",
    )
    second = _Resource(
        id="interaction-2",
        model="gemini-3.7-flash-20260815",
        status="completed",
        output_text="Grounded answer.",
        steps=[final_step],
        usage=_Resource(
            total_input_tokens=52,
            total_output_tokens=5,
            total_tokens=57,
        ),
        errors=None,
    )
    interactions = _Interactions([first, second])
    client = _client(interactions)

    initial = asyncio.run(
        client.generate_with_tools(
            "Find evidence.",
            [_tool()],
            context=[{"role": "system", "content": "Cite sources."}],
            llm_params={"reasoning_effort": "low"},
        )
    )

    first_request = interactions.requests[0]
    assert first_request["store"] is False
    assert "previous_interaction_id" not in first_request
    assert first_request["system_instruction"] == "Cite sources."
    assert first_request["generation_config"] == {
        "max_output_tokens": 2048,
        "thinking_level": "low",
    }
    assert "temperature" not in first_request["generation_config"]
    assert first_request["tools"][0]["parameters"] == _tool().input_schema
    assert first_request["input"] == [
        {
            "type": "user_input",
            "content": [{"type": "text", "text": "Find evidence."}],
        }
    ]
    assert initial.model == "gemini-3.7-flash-20260815"
    assert initial.usage == {
        "input_tokens": 31,
        "output_tokens": 12,
        "visible_output_tokens": 9,
        "total_tokens": 40,
        "cached_input_tokens": 4,
        "thought_tokens": 3,
        "tool_use_tokens": 2,
    }
    assert initial.continuation is not None
    assert initial.continuation.state_mode == "stateless"
    assert initial.continuation.output_items == [
        thought.model_dump(),
        function_call.model_dump(),
    ]
    assert initial.tool_calls[0].call_id == "call-17"

    completed = asyncio.run(
        client.generate_with_tools(
            "",
            [_tool()],
            system_message="Cite sources.",
            continuation=initial.continuation,
            tool_results=[
                ToolResult(
                    call_id="call-17",
                    tool_name="lookup",
                    output={"source": "primary"},
                )
            ],
            llm_params={"reasoning_effort": "low"},
        )
    )

    second_request = interactions.requests[1]
    assert second_request["store"] is False
    assert "previous_interaction_id" not in second_request
    assert second_request["input"] == [
        *first_request["input"],
        thought.model_dump(),
        function_call.model_dump(),
        {
            "type": "function_result",
            "name": "lookup",
            "call_id": "call-17",
            "result": [{"type": "text", "text": '{"source":"primary"}'}],
        },
    ]
    assert completed.text_response == "Grounded answer."
    assert completed.raw_response is not None
    assert "sdk_http_response" not in completed.raw_response


def test_interactions_requires_action_without_function_call_is_protocol_failure() -> None:
    interaction = _Resource(
        id="interaction-missing-action",
        model="gemini-3.7-flash",
        status="requires_action",
        output_text="",
        steps=[],
        usage=None,
        errors=None,
    )
    client = _client(_Interactions([interaction]))

    with pytest.raises(
        StructuredToolProtocolError,
        match="required action without a correlated function call",
    ) as exc_info:
        asyncio.run(client.generate_with_tools("Find evidence.", [_tool()]))

    assert exc_info.value.decision == {
        "provider": "gemini",
        "effective_api_surface": "interactions",
        "provider_status": "requires_action",
        "failure_kind": "missing_provider_call_correlation",
    }


def test_interactions_missing_call_id_is_typed_protocol_failure() -> None:
    interaction = _Resource(
        id="interaction-bad",
        model="gemini-3.7-flash",
        status="completed",
        output_text="",
        steps=[
            _Resource(
                type="function_call",
                id=None,
                name="lookup",
                arguments={"queries": []},
            )
        ],
        usage=None,
        errors=None,
    )
    client = _client(_Interactions([interaction]))

    with pytest.raises(
        StructuredToolProtocolError, match="required id and name"
    ) as exc_info:
        asyncio.run(client.generate_with_tools("Find evidence.", [_tool()]))

    assert exc_info.value.decision == {
        "provider": "gemini",
        "effective_api_surface": "interactions",
        "failure_kind": "missing_provider_call_correlation",
    }


def test_generate_content_assigns_and_replays_id_when_sdk_call_id_is_missing() -> None:
    function_call = genai.types.FunctionCall(
        name="lookup",
        args={"queries": [{"term": "legacy evidence"}]},
    )
    assert function_call.id is None
    first_content = genai.types.Content(
        role="model",
        parts=[genai.types.Part(function_call=function_call)],
    )
    first = genai.types.GenerateContentResponse(
        model_version="gemini-2.5-flash-001",
        candidates=[genai.types.Candidate(content=first_content)],
    )
    second = genai.types.GenerateContentResponse(
        model_version="gemini-2.5-flash-001",
        candidates=[
            genai.types.Candidate(
                content=genai.types.Content(
                    role="model",
                    parts=[genai.types.Part(text="Grounded legacy answer.")],
                )
            )
        ],
    )
    models = _Models([first, second])
    client = _legacy_client(models)

    initial = asyncio.run(
        client.generate_with_tools("Find legacy evidence.", [_tool()])
    )

    assert len(initial.tool_calls) == 1
    call = initial.tool_calls[0]
    assert call.call_id.startswith("von-gemini-")
    assert call.provider_item_id is None
    assert initial.continuation is not None
    retained_call = initial.continuation.output_items[0]["parts"][0][
        "function_call"
    ]
    assert retained_call == {
        "args": {"queries": [{"term": "legacy evidence"}]},
        "name": "lookup",
        "id": call.call_id,
    }

    completed = asyncio.run(
        client.generate_with_tools(
            "",
            [_tool()],
            continuation=initial.continuation,
            tool_results=[
                ToolResult(
                    call_id=call.call_id,
                    tool_name="lookup",
                    output={"source": "legacy-primary"},
                )
            ],
        )
    )

    replayed_contents = models.requests[1]["contents"]
    assert replayed_contents[1].model_dump(mode="json", exclude_none=True) == (
        initial.continuation.output_items[0]
    )
    assert replayed_contents[1].parts[0].function_call.id == call.call_id
    function_response = replayed_contents[2].parts[0].function_response
    assert function_response.id == call.call_id
    assert function_response.name == "lookup"
    assert function_response.response == {
        "output": {"source": "legacy-primary"}
    }
    assert completed.text_response == "Grounded legacy answer."


def test_gemini_37_fails_clearly_without_interactions_sdk_surface() -> None:
    with pytest.raises(ImportError, match="client.aio.interactions"):
        GeminiClient._assert_interactions_available(
            SimpleNamespace(aio=SimpleNamespace()),
            "gemini-3.7-flash",
        )


@pytest.mark.parametrize(
    "status",
    [
        "failed",
        "cancelled",
        "in_progress",
        "incomplete",
        "budget_exceeded",
        "completed",
    ],
)
def test_interactions_unsuccessful_status_or_errors_are_typed_and_sanitised(
    status: str,
) -> None:
    interaction = _Resource(
        id="interaction-failed",
        model="gemini-3.7-flash",
        status=status,
        output_text="plausible but invalid",
        steps=[],
        usage=None,
        errors=[
            _Resource(
                code="provider_failure",
                message="api_key=secret-provider-value",
            )
        ],
    )
    client = _client(_Interactions([interaction]))

    with pytest.raises(StructuredToolTransportError) as exc_info:
        asyncio.run(client.generate_with_tools("Find evidence.", [_tool()]))

    assert exc_info.value.decision["provider_status"] == status
    assert exc_info.value.decision["failure_kind"] == "provider_response_failed"
    assert "secret-provider-value" not in str(exc_info.value.decision)
    assert "[redacted]" in str(exc_info.value.decision)


def test_interactions_incomplete_retains_only_visible_partial_response() -> None:
    interaction = _Resource(
        id="interaction-incomplete",
        model="gemini-3.7-flash-20260815",
        status="incomplete",
        output_text="",
        steps=[
            _Resource(
                type="thought",
                summary=[{"type": "text", "text": "private reasoning"}],
                signature="private-signature",
            ),
            _Resource(
                type="model_output",
                content=[
                    {
                        "type": "text",
                        "text": "| Student | End date |\n|---|---|\n| A | 2027 |",
                    }
                ],
                signature="answer-signature",
            ),
        ],
        usage=_Resource(
            total_input_tokens=320,
            total_output_tokens=41,
            total_thought_tokens=9,
            total_tokens=370,
        ),
        errors=None,
    )
    client = _client(_Interactions([interaction]))

    with pytest.raises(StructuredToolTransportError) as exc_info:
        asyncio.run(client.generate_with_tools("Make the table.", [_tool()]))

    error = exc_info.value
    assert error.decision["provider_status"] == "incomplete"
    assert error.decision["partial_response_available"] is True
    assert error.decision["partial_response_char_count"] == 45
    assert error.decision["provider_output_step_types"] == [
        "thought",
        "model_output",
    ]
    assert "Student" not in str(error.decision)
    assert "private reasoning" not in str(error.decision)

    partial = error.partial_response
    assert partial is not None
    assert partial.text_response == (
        "| Student | End date |\n|---|---|\n| A | 2027 |"
    )
    assert partial.tool_calls == []
    assert partial.model == "gemini-3.7-flash-20260815"
    assert partial.usage == {
        "input_tokens": 320,
        "output_tokens": 50,
        "visible_output_tokens": 41,
        "total_tokens": 370,
        "thought_tokens": 9,
    }
    assert partial.transport_metadata["response_complete"] is False
    assert partial.transport_metadata["provider_status"] == "incomplete"
    assert partial.raw_response is not None
    assert partial.raw_response["step_types"] == ["thought", "model_output"]
    assert "private reasoning" not in str(partial.raw_response)
    assert "private-signature" not in str(partial.raw_response)
