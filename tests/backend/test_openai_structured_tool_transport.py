"""Focused tests for profile-driven OpenAI structured-tool transport."""

from __future__ import annotations

import asyncio
import json
import types
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from src.backend.languagemodels.structured_tool_calling import (
    LLMContinuation,
    LLMClientConfig,
    StructuredToolCapabilityRejectedError,
    StructuredToolProtocolError,
    ToolDefinition,
    ToolResult,
    UnsupportedStructuredToolTransportError,
)
from src.backend.languagemodels.structured_tool_calling.providers import OpenAIClient


def _registry_profiles(*profiles: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": "vontology_graph",
        "registry_concept_id": "#V#default_model_registry",
        "registry_entry_id": "#V#test_model_registry_entry",
        "concept_id": "#V#test_model",
        "api_profiles": [dict(profile) for profile in profiles],
    }


def _responses_profile(*, capability: str = "required") -> dict[str, Any]:
    return {
        "profile_concept_id": "#V#test_responses_profile",
        "api_surface": "responses",
        "structured_tool_calling": capability,
        "tool_continuation_mode": "stateless",
        "response_storage_policy": "disabled",
    }


def _chat_profile(*, capability: str = "supported") -> dict[str, Any]:
    return {
        "profile_concept_id": "#V#test_chat_profile",
        "api_surface": "chat_completions",
        "structured_tool_calling": capability,
        "tool_continuation_mode": "stateless",
        "response_storage_policy": "disabled",
    }


class _CapabilityError(RuntimeError):
    status_code = 400
    body = {
        "error": {
            "code": "unsupported_model_tool_transport",
            "param": "tools",
            "type": "invalid_request_error",
        }
    }


class _RateLimitError(RuntimeError):
    status_code = 429


def _tool(name: str = "lookup") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Run {name}.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )


def _text_response(*, model: str, text: str = "ok", response_id: str = "resp_1"):
    return {
        "id": response_id,
        "model": model,
        "output": [
            {
                "id": f"msg_{response_id}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    }


def _install_fake_openai(
    monkeypatch: pytest.MonkeyPatch,
    *,
    responses: Sequence[Any] = (),
    chat_response: Any | None = None,
    chat_responses: Sequence[Any] = (),
) -> dict[str, list[dict[str, Any]]]:
    from src.backend.languagemodels.structured_tool_calling.providers import (
        openai_client as provider_module,
    )

    response_queue = list(responses)
    chat_response_queue = list(chat_responses)
    captured: dict[str, list[dict[str, Any]]] = {"responses": [], "chat": []}

    async def responses_create(**kwargs: Any) -> Any:
        captured["responses"].append(dict(kwargs))
        if not response_queue:
            raise AssertionError("Unexpected Responses API request")
        next_response = response_queue.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return next_response

    async def chat_create(**kwargs: Any) -> Any:
        captured["chat"].append(dict(kwargs))
        if chat_response_queue:
            next_response = chat_response_queue.pop(0)
            if isinstance(next_response, Exception):
                raise next_response
            return next_response
        if isinstance(chat_response, Exception):
            raise chat_response
        if chat_response is not None:
            return chat_response
        return {
            "model": kwargs["model"],
            "choices": [{"message": {"content": "chat ok", "tool_calls": []}}],
            "usage": None,
        }

    monkeypatch.setattr(
        provider_module.openai,
        "AsyncOpenAI",
        lambda **_kwargs: types.SimpleNamespace(
            responses=types.SimpleNamespace(create=responses_create),
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=chat_create)
            ),
        ),
    )
    monkeypatch.setattr(
        provider_module.openai,
        "OpenAI",
        lambda **_kwargs: object(),
    )
    return captured


def test_chat_completions_continuation_replays_assistant_call_before_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "chat-compatible-loop-model"
    _install_profiles(monkeypatch, _registry_profiles(_chat_profile()))
    captured = _install_fake_openai(
        monkeypatch,
        chat_responses=[
            {
                "model": model,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_chat_lookup",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"query":"alpha"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": None,
            },
            {
                "model": model,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Grounded Chat answer",
                            "tool_calls": [],
                        }
                    }
                ],
                "usage": None,
            },
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    first = asyncio.run(
        client.generate_with_tools(
            prompt="Find alpha.",
            available_tools=[_tool("lookup")],
        )
    )

    assert first.continuation is not None
    assert first.continuation.api_surface == "chat_completions"
    assert first.continuation.transport_decision["accepted_tool_call_ids"] == [
        "call_chat_lookup"
    ]

    final = asyncio.run(
        client.generate_with_tools(
            prompt="",
            available_tools=[_tool("lookup")],
            context=[
                {"role": "user", "content": "Find alpha."},
                {
                    "role": "system",
                    "content": "Use the completed tool evidence.",
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_chat_lookup",
                    "name": "lookup",
                    "content": "duplicate context result",
                },
            ],
            continuation=first.continuation,
            tool_results=[
                ToolResult(
                    call_id="call_chat_lookup",
                    tool_name="lookup",
                    output={"value": "alpha evidence"},
                )
            ],
        )
    )

    assert final.text_response == "Grounded Chat answer"
    follow_up_messages = captured["chat"][1]["messages"]
    assistant_index = next(
        index
        for index, message in enumerate(follow_up_messages)
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assistant_call = follow_up_messages[assistant_index]["tool_calls"][0]
    tool_message = follow_up_messages[assistant_index + 1]
    assert assistant_call["id"] == "call_chat_lookup"
    assert tool_message == {
        "role": "tool",
        "tool_call_id": "call_chat_lookup",
        "content": '{"value": "alpha evidence"}',
    }
    assert follow_up_messages.count({"role": "user", "content": "Find alpha."}) == 1
    assert {
        "role": "system",
        "content": "Use the completed tool evidence.",
    } in follow_up_messages
    assert not any(
        message.get("content") == "duplicate context result"
        for message in follow_up_messages
    )
    assert final.transport_metadata["continuation_stage_context_item_count"] == 1


def test_chat_mixed_valid_and_rejected_calls_continue_with_bounded_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "chat-mixed-call-model"
    _install_profiles(monkeypatch, _registry_profiles(_chat_profile()))
    captured = _install_fake_openai(
        monkeypatch,
        chat_responses=[
            {
                "model": model,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_chat_good",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"query":"good"}',
                                    },
                                },
                                {
                                    "id": "call_chat_rejected",
                                    "type": "function",
                                    "function": {
                                        "name": "not_advertised",
                                        "arguments": "{}",
                                    },
                                },
                            ],
                        }
                    }
                ],
                "usage": None,
            },
            {
                "model": model,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Recovered Chat answer",
                            "tool_calls": [],
                        }
                    }
                ],
                "usage": None,
            },
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    first = asyncio.run(
        client.generate_with_tools(
            prompt="Use a lookup.",
            available_tools=[_tool("lookup")],
        )
    )
    assert [call.call_id for call in first.tool_calls] == ["call_chat_good"]
    assert first.continuation is not None
    assert first.continuation.transport_decision["rejected_tool_calls"] == [
        {"call_id": "call_chat_rejected", "error_code": "unknown_tool"}
    ]

    final = asyncio.run(
        client.generate_with_tools(
            prompt="",
            available_tools=[_tool("lookup")],
            continuation=first.continuation,
            tool_results=[
                ToolResult(
                    call_id="call_chat_good",
                    tool_name="lookup",
                    output={"value": "grounded"},
                )
            ],
        )
    )

    assert final.text_response == "Recovered Chat answer"
    follow_up_messages = captured["chat"][1]["messages"]
    assistant = next(
        message
        for message in follow_up_messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert [item["id"] for item in assistant["tool_calls"]] == [
        "call_chat_good",
        "call_chat_rejected",
    ]
    tool_outputs = [
        message for message in follow_up_messages if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_outputs] == [
        "call_chat_good",
        "call_chat_rejected",
    ]
    assert json.loads(tool_outputs[1]["content"]) == {
        "status": "not_executed",
        "error_code": "unknown_tool",
    }


def test_malformed_continuation_fails_before_any_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_profiles(monkeypatch, _registry_profiles(_chat_profile()))
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(
            model="malformed-continuation-model",
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    with pytest.raises(StructuredToolProtocolError, match="payload is malformed"):
        asyncio.run(
            client.generate_with_tools(
                prompt="Continue.",
                available_tools=[_tool()],
                continuation={"provider": "openai"},
                tool_results=[
                    {
                        "call_id": "call_should_not_run",
                        "output": "sensitive result",
                    }
                ],
            )
        )

    with pytest.raises(StructuredToolProtocolError, match="payload is malformed"):
        asyncio.run(
            client.generate_with_tools(
                prompt="Continue.",
                available_tools=[_tool()],
                continuation={
                    "provider": "openai",
                    "api_surface": "chat_completions",
                    "model": "malformed-continuation-model",
                    "input_items": "not-a-message-list",
                    "output_items": "not-an-output-list",
                    "transport_decision": {
                        "accepted_tool_call_ids": ["call_should_not_run"]
                    },
                },
                tool_results=[
                    {
                        "call_id": "call_should_not_run",
                        "output": "sensitive result",
                    }
                ],
            )
        )

    with pytest.raises(StructuredToolProtocolError, match="not backed"):
        asyncio.run(
            client.generate_with_tools(
                prompt="Continue.",
                available_tools=[_tool()],
                continuation={
                    "provider": "openai",
                    "api_surface": "chat_completions",
                    "model": "malformed-continuation-model",
                    "state_mode": "stateless",
                    "connection_id": "#V#openai_provider",
                    "deployment_id": "malformed-continuation-model",
                    "input_items": [
                        {"role": "user", "content": "Start."},
                    ],
                    "output_items": [],
                    "transport_decision": {
                        "accepted_tool_call_ids": ["call_should_not_run"]
                    },
                },
                tool_results=[
                    {
                        "call_id": "call_should_not_run",
                        "output": "sensitive result",
                    }
                ],
            )
        )

    assert captured == {"responses": [], "chat": []}


def _install_profiles(
    monkeypatch: pytest.MonkeyPatch,
    resolved: Mapping[str, Any] | None,
) -> None:
    from src.backend.languagemodels.structured_tool_calling import transport

    monkeypatch.setattr(
        transport,
        "resolve_model_api_profiles",
        lambda **_kwargs: resolved,
    )


def test_arbitrary_model_responses_profile_selects_responses_and_flat_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deployment-blue-42"
    responses_profile = _responses_profile()
    responses_profile["connection_id"] = "#V#test_responses_connection"
    _install_profiles(monkeypatch, _registry_profiles(responses_profile))
    captured = _install_fake_openai(
        monkeypatch,
        responses=[_text_response(model=model)],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            base_url="https://compatible.example.test/v1",
            connection_id="#V#test_responses_connection",
            temperature=None,
        )
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Find it",
            available_tools=[_tool()],
        )
    )

    assert result.text_response == "ok"
    assert captured["chat"] == []
    assert len(captured["responses"]) == 1
    request = captured["responses"][0]
    assert request["model"] == model
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Run lookup.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    ]
    assert "function" not in request["tools"][0]
    assert result.transport_metadata["effective_api_surface"] == "responses"
    assert result.transport_metadata["profile_concept_id"] == (
        "#V#test_responses_profile"
    )


def test_responses_maps_reasoning_effort_and_disables_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import model_parameter_service

    model = "gpt-5.6-luna"
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    monkeypatch.setattr(
        model_parameter_service,
        "_registry_parameter_policy",
        lambda **_kwargs: None,
    )
    captured = _install_fake_openai(
        monkeypatch,
        responses=[_text_response(model=model)],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    asyncio.run(
        client.generate_with_tools(
            prompt="Use the tool",
            available_tools=[_tool()],
            llm_params={"reasoning_effort": "high"},
        )
    )

    assert captured["chat"] == []
    request = captured["responses"][0]
    assert request["reasoning"] == {"effort": "high"}
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert "reasoning_effort" not in request


def test_responses_preserves_none_reasoning_and_records_parameter_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import model_parameter_service

    model = "gpt-5.6-luna"
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    monkeypatch.setattr(
        model_parameter_service,
        "_registry_parameter_policy",
        lambda **_kwargs: None,
    )
    captured = _install_fake_openai(
        monkeypatch,
        responses=[_text_response(model=model)],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Use the tool",
            available_tools=[_tool()],
            llm_params={
                "reasoning_effort": "none",
                "api_key": "sk-test-credential-should-not-appear",
            },
        )
    )

    assert captured["responses"][0]["reasoning"] == {"effort": "none"}
    assert captured["responses"][0]["include"] == ["reasoning.encrypted_content"]
    assert result.transport_metadata["requested_model_parameters"] == {
        "reasoning_effort": "none",
        "api_key": "[redacted]",
    }
    assert result.transport_metadata["effective_provider_parameters"] == {
        "reasoning": {"effort": "none"}
    }
    assert result.transport_metadata["omitted_model_parameter_names"] == ["api_key"]
    assert result.transport_metadata["requested_api_surface"] == "auto"
    assert result.transport_metadata["effective_api_surface"] == "responses"
    assert result.transport_metadata["capability_source"] == "vontology_graph"
    assert result.transport_metadata["reason"] == "represented_profile_required"
    assert "sk-test-credential" not in json.dumps(result.transport_metadata)


def test_responses_parses_mixed_output_and_preserves_ordered_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deployment-mixed-output"
    output_items = [
        {
            "id": "rs_1",
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "encrypted-reasoning",
        },
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Planning "}],
        },
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_alpha",
            "name": "lookup",
            "arguments": '{"query":"alpha"}',
        },
        {
            "id": "msg_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done."}],
        },
        {
            "id": "fc_2",
            "type": "function_call",
            "call_id": "call_beta",
            "name": "search",
            "arguments": '{"query":"beta"}',
        },
    ]
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    _install_fake_openai(
        monkeypatch,
        responses=[
            {
                "id": "resp_mixed",
                "model": model,
                "output": output_items,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 8,
                    "total_tokens": 18,
                },
            }
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Plan",
            available_tools=[_tool("lookup"), _tool("search")],
        )
    )

    assert result.text_response == "Planning done."
    assert [call.call_id for call in result.tool_calls] == [
        "call_alpha",
        "call_beta",
    ]
    assert [call.provider_item_id for call in result.tool_calls] == ["fc_1", "fc_2"]
    assert [call.payload for call in result.tool_calls] == [
        {"query": "alpha"},
        {"query": "beta"},
    ]
    assert result.continuation is not None
    assert result.continuation.response_id == "resp_mixed"
    assert result.continuation.output_items == output_items
    assert result.continuation.output_items[0]["encrypted_content"] == (
        "encrypted-reasoning"
    )
    assert result.transport_metadata["response_output_item_types"] == [
        "reasoning",
        "message",
        "function_call",
        "message",
        "function_call",
    ]
    assert result.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 8,
        "total_tokens": 18,
    }


def test_responses_continuation_emits_exact_call_outputs_and_reasoning_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deployment-continuation"
    first_output = [
        {
            "id": "rs_continue",
            "type": "reasoning",
            "encrypted_content": "encrypted-state",
            "summary": [],
        },
        {
            "id": "fc_alpha",
            "type": "function_call",
            "call_id": "call_alpha",
            "name": "lookup",
            "arguments": '{"query":"alpha"}',
        },
        {
            "id": "fc_beta",
            "type": "function_call",
            "call_id": "call_beta",
            "name": "search",
            "arguments": '{"query":"beta"}',
        },
    ]
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    captured = _install_fake_openai(
        monkeypatch,
        responses=[
            {
                "id": "resp_calls",
                "model": model,
                "output": first_output,
                "usage": None,
            },
            _text_response(
                model=model, text="Grounded answer", response_id="resp_done"
            ),
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    first = asyncio.run(
        client.generate_with_tools(
            prompt="Find both",
            available_tools=[_tool("lookup"), _tool("search")],
        )
    )
    assert first.continuation is not None

    final = asyncio.run(
        client.generate_with_tools(
            prompt="",
            available_tools=[_tool("lookup"), _tool("search")],
            context=[
                {"role": "user", "content": "Find both"},
                {
                    "role": "system",
                    "content": "Stage-specific completion guidance.",
                },
            ],
            continuation=first.continuation,
            tool_results=[
                ToolResult(
                    call_id="call_alpha",
                    tool_name="lookup",
                    output={"value": "alpha result"},
                ),
                ToolResult(
                    call_id="call_beta",
                    tool_name="search",
                    output="beta result",
                ),
            ],
        )
    )

    assert final.text_response == "Grounded answer"
    follow_up_input = captured["responses"][1]["input"]
    assert first_output[0] in follow_up_input
    assert follow_up_input.index(first_output[0]) < follow_up_input.index(
        first_output[1]
    )
    output_items = [
        item for item in follow_up_input if item.get("type") == "function_call_output"
    ]
    assert [item["call_id"] for item in output_items] == [
        "call_alpha",
        "call_beta",
    ]
    assert json.loads(output_items[0]["output"]) == {"value": "alpha result"}
    assert output_items[1]["output"] == "beta result"
    assert {
        "role": "system",
        "content": "Stage-specific completion guidance.",
    } in follow_up_input
    assert follow_up_input.count({"role": "user", "content": "Find both"}) == 1
    assert "previous_response_id" not in captured["responses"][1]
    assert captured["responses"][1]["store"] is False
    assert captured["responses"][0]["include"] == ["reasoning.encrypted_content"]
    assert captured["responses"][1]["include"] == ["reasoning.encrypted_content"]
    assert final.transport_metadata["continuation_stage_context_item_count"] == 1


def test_responses_mixed_valid_and_rejected_calls_continue_with_bounded_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deployment-mixed-call-recovery"
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    captured = _install_fake_openai(
        monkeypatch,
        responses=[
            {
                "id": "resp_mixed_calls",
                "model": model,
                "output": [
                    {
                        "id": "fc_good",
                        "type": "function_call",
                        "call_id": "call_good",
                        "name": "lookup",
                        "arguments": '{"query":"good"}',
                    },
                    {
                        "id": "fc_bad",
                        "type": "function_call",
                        "call_id": "call_bad",
                        "name": "not_advertised",
                        "arguments": "{}",
                    },
                ],
                "usage": None,
            },
            _text_response(model=model, text="Recovered answer"),
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    first = asyncio.run(
        client.generate_with_tools(
            prompt="Use a lookup.",
            available_tools=[_tool("lookup")],
        )
    )
    assert [call.call_id for call in first.tool_calls] == ["call_good"]
    assert first.continuation is not None
    assert first.continuation.transport_decision["accepted_tool_call_ids"] == [
        "call_good"
    ]
    assert first.continuation.transport_decision["rejected_tool_calls"] == [
        {"call_id": "call_bad", "error_code": "unknown_tool"}
    ]

    final = asyncio.run(
        client.generate_with_tools(
            prompt="",
            available_tools=[_tool("lookup")],
            continuation=first.continuation,
            tool_results=[
                ToolResult(
                    call_id="call_good",
                    tool_name="lookup",
                    output={"value": "grounded"},
                )
            ],
        )
    )

    assert final.text_response == "Recovered answer"
    outputs = [
        item
        for item in captured["responses"][1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert [item["call_id"] for item in outputs] == ["call_good", "call_bad"]
    assert json.loads(outputs[1]["output"]) == {
        "status": "not_executed",
        "error_code": "unknown_tool",
    }


def test_responses_mixed_unreplayable_call_fails_before_tool_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deployment-mixed-unreplayable-call"
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    captured = _install_fake_openai(
        monkeypatch,
        responses=[
            {
                "id": "resp_mixed_unreplayable",
                "model": model,
                "output": [
                    {
                        "id": "fc_good",
                        "type": "function_call",
                        "call_id": "call_good",
                        "name": "lookup",
                        "arguments": '{"query":"good"}',
                    },
                    {
                        "id": "fc_missing_arguments",
                        "type": "function_call",
                        "call_id": "call_missing_arguments",
                        "name": "lookup",
                    },
                ],
                "usage": None,
            }
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(model=model, provider="openai", api_key="test-key")
    )

    with pytest.raises(
        StructuredToolProtocolError,
        match="not backed by valid provider tool-call output items",
    ):
        asyncio.run(
            client.generate_with_tools(
                prompt="Use a lookup.",
                available_tools=[_tool("lookup")],
            )
        )

    assert len(captured["responses"]) == 1


def test_provider_managed_responses_requires_response_id_before_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "provider-managed-responses-model"
    profile = _responses_profile()
    profile["tool_continuation_mode"] = "provider_managed"
    profile["response_storage_policy"] = "enabled"
    _install_profiles(monkeypatch, _registry_profiles(profile))
    captured = _install_fake_openai(
        monkeypatch,
        responses=[
            {
                "model": model,
                "output": [
                    {
                        "id": "fc_provider_managed",
                        "type": "function_call",
                        "call_id": "call_provider_managed",
                        "name": "lookup",
                        "arguments": '{"query":"value"}',
                    }
                ],
                "usage": None,
            }
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    with pytest.raises(StructuredToolProtocolError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="Use the tool.",
                available_tools=[_tool()],
            )
        )

    assert len(captured["responses"]) == 1
    assert captured["chat"] == []
    assert exc_info.value.decision["failure_kind"] == ("missing_provider_response_id")


def test_responses_continuation_never_falls_back_across_api_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deployment-pinned-continuation"
    _install_profiles(
        monkeypatch,
        _registry_profiles(_responses_profile(), _chat_profile()),
    )
    captured = _install_fake_openai(
        monkeypatch,
        responses=[
            _CapabilityError(
                "Function tools are not supported on this Responses deployment."
            )
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )
    continuation = LLMContinuation(
        provider="openai",
        api_surface="responses",
        model=model,
        connection_id="#V#openai_provider",
        deployment_id=model,
        output_items=[
            {
                "id": "fc_pinned",
                "type": "function_call",
                "call_id": "call_pinned",
                "name": "lookup",
                "arguments": '{"query":"value"}',
            }
        ],
        transport_decision={"accepted_tool_call_ids": ["call_pinned"]},
    )

    with pytest.raises(StructuredToolCapabilityRejectedError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="",
                available_tools=[_tool("lookup")],
                continuation=continuation,
                tool_results=[
                    ToolResult(
                        call_id="call_pinned",
                        tool_name="lookup",
                        output="result",
                    )
                ],
            )
        )

    assert len(captured["responses"]) == 1
    assert captured["chat"] == []
    assert exc_info.value.decision["surface_fallback_used"] is False
    assert exc_info.value.decision["surface_fallback_blocked_reason"] == (
        "provider_continuation_surface_is_pinned"
    )


def test_advertised_chat_to_responses_fallback_uses_pristine_surface_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import model_parameter_service

    model = "gpt-5.6-fallback"
    _install_profiles(
        monkeypatch,
        _registry_profiles(
            _chat_profile(),
            _responses_profile(capability="supported"),
        ),
    )
    monkeypatch.setattr(
        model_parameter_service,
        "_registry_parameter_policy",
        lambda **_kwargs: None,
    )
    captured = _install_fake_openai(
        monkeypatch,
        responses=[_text_response(model=model)],
        chat_response=_CapabilityError(
            "Function tools are not supported; use /v1/responses."
        ),
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            max_tokens=123,
            temperature=None,
        )
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Use the tool",
            available_tools=[_tool()],
            llm_params={"reasoning_effort": "high"},
        )
    )

    assert len(captured["chat"]) == 1
    assert len(captured["responses"]) == 1
    chat_request = captured["chat"][0]
    responses_request = captured["responses"][0]
    assert chat_request["max_tokens"] == 123
    assert "reasoning_effort" not in chat_request
    assert "max_output_tokens" not in chat_request
    assert "store" not in chat_request
    assert "include" not in chat_request
    assert responses_request["max_output_tokens"] == 123
    assert responses_request["reasoning"] == {"effort": "high"}
    assert "max_tokens" not in responses_request
    assert "reasoning_effort" not in responses_request
    assert responses_request["store"] is False
    assert responses_request["include"] == ["reasoning.encrypted_content"]
    assert result.transport_metadata["profile_concept_id"] == (
        "#V#test_responses_profile"
    )
    assert result.transport_metadata["surface_fallback_used"] is True
    assert result.transport_metadata["advertised_surface_attempts"] == [
        "chat_completions",
        "responses",
    ]


def test_chat_to_responses_fallback_continuation_remains_on_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "fallback-loop-model"
    _install_profiles(
        monkeypatch,
        _registry_profiles(
            _chat_profile(),
            _responses_profile(capability="supported"),
        ),
    )
    captured = _install_fake_openai(
        monkeypatch,
        responses=[
            {
                "id": "resp_fallback_call",
                "model": model,
                "output": [
                    {
                        "id": "fc_fallback",
                        "type": "function_call",
                        "call_id": "call_fallback",
                        "name": "lookup",
                        "arguments": '{"query":"value"}',
                    }
                ],
                "usage": None,
            },
            _text_response(model=model, text="Grounded fallback answer"),
        ],
        chat_response=_CapabilityError(
            "Function tools are not supported; use /v1/responses."
        ),
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    first = asyncio.run(
        client.generate_with_tools(
            prompt="Use the tool.",
            available_tools=[_tool("lookup")],
        )
    )
    assert first.continuation is not None
    assert first.continuation.api_surface == "responses"
    final = asyncio.run(
        client.generate_with_tools(
            prompt="",
            available_tools=[_tool("lookup")],
            continuation=first.continuation,
            tool_results=[
                ToolResult(
                    call_id="call_fallback",
                    tool_name="lookup",
                    output="grounded result",
                )
            ],
        )
    )

    assert final.text_response == "Grounded fallback answer"
    assert len(captured["chat"]) == 1
    assert len(captured["responses"]) == 2
    assert captured["responses"][1]["input"][-1]["call_id"] == "call_fallback"


def test_advertised_responses_to_chat_fallback_uses_actual_chat_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import model_parameter_service

    model = "gpt-5.6-reverse-fallback"
    _install_profiles(
        monkeypatch,
        _registry_profiles(_responses_profile(), _chat_profile()),
    )
    monkeypatch.setattr(
        model_parameter_service,
        "_registry_parameter_policy",
        lambda **_kwargs: None,
    )
    captured = _install_fake_openai(
        monkeypatch,
        responses=[
            _CapabilityError(
                "Function tools are not supported on this Responses deployment."
            )
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            max_tokens=321,
            temperature=None,
        )
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Use the tool",
            available_tools=[_tool()],
            llm_params={"reasoning_effort": "high"},
        )
    )

    assert len(captured["responses"]) == 1
    assert len(captured["chat"]) == 1
    responses_request = captured["responses"][0]
    chat_request = captured["chat"][0]
    assert responses_request["max_output_tokens"] == 321
    assert responses_request["reasoning"] == {"effort": "high"}
    assert responses_request["store"] is False
    assert responses_request["include"] == ["reasoning.encrypted_content"]
    assert "max_tokens" not in responses_request
    assert chat_request["max_tokens"] == 321
    assert "max_output_tokens" not in chat_request
    assert "reasoning" not in chat_request
    assert "store" not in chat_request
    assert "include" not in chat_request
    assert result.transport_metadata["profile_concept_id"] == "#V#test_chat_profile"
    assert result.transport_metadata["effective_api_surface"] == ("chat_completions")
    assert result.transport_metadata["surface_fallback_used"] is True


def test_alternate_rate_limit_remains_provider_error_after_capability_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "rate-limited-alternate-model"
    _install_profiles(
        monkeypatch,
        _registry_profiles(
            _responses_profile(),
            _chat_profile(capability="supported"),
        ),
    )
    rate_limit = _RateLimitError("rate limit reached")
    captured = _install_fake_openai(
        monkeypatch,
        responses=[
            _CapabilityError(
                "Function tools are not supported on this Responses deployment."
            )
        ],
        chat_response=rate_limit,
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    with pytest.raises(_RateLimitError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="Use the tool.",
                available_tools=[_tool()],
            )
        )

    assert exc_info.value is rate_limit
    assert len(captured["responses"]) == 1
    assert len(captured["chat"]) == 1
    transport = getattr(
        exc_info.value,
        "structured_tool_transport_decision",
    )
    assert transport["surface_fallback_used"] is True
    assert transport["initial_effective_api_surface"] == "responses"
    assert transport["effective_api_surface"] == "chat_completions"
    assert transport["alternate_failure_kind"] == "provider_error"


def test_responses_invalid_calls_share_bounded_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deployment-invalid-calls"
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    _install_fake_openai(
        monkeypatch,
        responses=[
            {
                "id": "resp_invalid",
                "model": model,
                "output": [
                    {
                        "id": "fc_bad_json",
                        "type": "function_call",
                        "call_id": "call_bad_json",
                        "name": "lookup",
                        "arguments": "{bad json",
                    },
                    {
                        "id": "fc_not_object",
                        "type": "function_call",
                        "call_id": "call_not_object",
                        "name": "lookup",
                        "arguments": '["not", "an", "object"]',
                    },
                    {
                        "id": "fc_unknown",
                        "type": "function_call",
                        "call_id": "call_unknown",
                        "name": "not_advertised",
                        "arguments": "{}",
                    },
                ],
                "usage": None,
            }
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    with pytest.raises(StructuredToolProtocolError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="Call tools",
                available_tools=[_tool("lookup")],
            )
        )

    assert exc_info.value.decision["failure_kind"] == (
        "all_provider_tool_calls_rejected"
    )
    assert exc_info.value.decision["rejected_tool_calls"] == [
        {
            "call_id": "call_bad_json",
            "error_code": "provider_tool_call_parse_error",
        },
        {"call_id": "call_not_object", "error_code": "arguments_not_object"},
        {"call_id": "call_unknown", "error_code": "unknown_tool"},
    ]


def test_responses_rejects_duplicate_provider_call_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deployment-duplicate-calls"
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    _install_fake_openai(
        monkeypatch,
        responses=[
            {
                "id": "resp_duplicate",
                "model": model,
                "output": [
                    {
                        "id": "fc_one",
                        "type": "function_call",
                        "call_id": "call_duplicate",
                        "name": "lookup",
                        "arguments": '{"query":"one"}',
                    },
                    {
                        "id": "fc_two",
                        "type": "function_call",
                        "call_id": "call_duplicate",
                        "name": "lookup",
                        "arguments": '{"query":"two"}',
                    },
                ],
                "usage": None,
            }
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(model=model, provider="openai", api_key="test-key")
    )

    with pytest.raises(StructuredToolProtocolError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="Call lookup.",
                available_tools=[_tool("lookup")],
            )
        )

    assert exc_info.value.decision["failure_kind"] == "duplicate_provider_call_id"


def test_responses_rejects_mixed_valid_and_missing_provider_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deployment-missing-call-id"
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    _install_fake_openai(
        monkeypatch,
        responses=[
            {
                "id": "resp_missing_call_id",
                "model": model,
                "output": [
                    {
                        "id": "fc_good",
                        "type": "function_call",
                        "call_id": "call_good",
                        "name": "lookup",
                        "arguments": '{"query":"good"}',
                    },
                    {
                        "id": "fc_missing",
                        "type": "function_call",
                        "name": "lookup",
                        "arguments": '{"query":"missing"}',
                    },
                ],
                "usage": None,
            }
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(model=model, provider="openai", api_key="test-key")
    )

    with pytest.raises(StructuredToolProtocolError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="Call lookup.",
                available_tools=[_tool("lookup")],
            )
        )

    assert exc_info.value.decision["failure_kind"] == "missing_provider_call_id"


def test_provider_controlled_response_and_call_ids_are_sanitised_in_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deployment-provider-ids"
    raw_response_id = "token=response-id-secret"
    raw_call_id = "token=call-id-secret"
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    _install_fake_openai(
        monkeypatch,
        responses=[
            {
                "id": raw_response_id,
                "model": model,
                "output": [
                    {
                        "id": "fc_safe",
                        "type": "function_call",
                        "call_id": raw_call_id,
                        "name": "lookup",
                        "arguments": '{"query":"value"}',
                    }
                ],
                "usage": None,
            }
        ],
    )
    client = OpenAIClient(
        LLMClientConfig(model=model, provider="openai", api_key="test-key")
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Call lookup.",
            available_tools=[_tool("lookup")],
        )
    )

    transport_json = json.dumps(result.transport_metadata, sort_keys=True)
    assert "response-id-secret" not in transport_json
    assert "call-id-secret" not in transport_json
    assert result.continuation is not None
    assert result.continuation.response_id == raw_response_id
    assert result.continuation.transport_decision["accepted_tool_call_ids"] == [
        raw_call_id
    ]


@pytest.mark.parametrize(
    ("state_mode", "response_id", "message_fragment"),
    [
        ("stateless", "resp_previous", "state mode"),
        ("provider_managed", None, "response_id"),
    ],
)
def test_provider_managed_continuation_fails_closed_on_state_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
    state_mode: str,
    response_id: str | None,
    message_fragment: str,
) -> None:
    model = "deployment-provider-managed"
    profile = _responses_profile()
    profile["tool_continuation_mode"] = "provider_managed"
    profile["response_storage_policy"] = "enabled"
    _install_profiles(monkeypatch, _registry_profiles(profile))
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(model=model, provider="openai", api_key="test-key")
    )
    continuation = LLMContinuation(
        provider="openai",
        api_surface="responses",
        model=model,
        state_mode=state_mode,
        response_id=response_id,
        connection_id="#V#openai_provider",
        deployment_id=model,
        output_items=[
            {
                "type": "function_call",
                "call_id": "call_managed",
                "name": "lookup",
                "arguments": '{"query":"value"}',
            }
        ],
        transport_decision={"accepted_tool_call_ids": ["call_managed"]},
    )

    with pytest.raises(StructuredToolProtocolError, match=message_fragment):
        asyncio.run(
            client.generate_with_tools(
                prompt="",
                available_tools=[_tool("lookup")],
                continuation=continuation,
                tool_results=[
                    ToolResult(
                        call_id="call_managed",
                        tool_name="lookup",
                        output="result",
                    )
                ],
            )
        )

    assert captured["responses"] == []
    assert captured["chat"] == []


@pytest.mark.parametrize(
    ("connection_id", "deployment_id", "message_fragment"),
    [
        (None, "deployment-exact-continuation", "connection"),
        ("#V#wrong_connection", "deployment-exact-continuation", "connection"),
        ("#V#openai_provider", None, "deployment"),
        ("#V#openai_provider", "wrong-deployment", "deployment"),
    ],
)
def test_continuation_requires_exact_connection_and_deployment_identity(
    monkeypatch: pytest.MonkeyPatch,
    connection_id: str | None,
    deployment_id: str | None,
    message_fragment: str,
) -> None:
    model = "deployment-exact-continuation"
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(model=model, provider="openai", api_key="test-key")
    )
    continuation = LLMContinuation(
        provider="openai",
        api_surface="responses",
        model=model,
        connection_id=connection_id,
        deployment_id=deployment_id,
        output_items=[
            {
                "type": "function_call",
                "call_id": "call_exact",
                "name": "lookup",
                "arguments": '{"query":"value"}',
            }
        ],
        transport_decision={"accepted_tool_call_ids": ["call_exact"]},
    )

    with pytest.raises(StructuredToolProtocolError, match=message_fragment):
        asyncio.run(
            client.generate_with_tools(
                prompt="",
                available_tools=[_tool("lookup")],
                continuation=continuation,
                tool_results=[
                    ToolResult(
                        call_id="call_exact",
                        tool_name="lookup",
                        output="result",
                    )
                ],
            )
        )

    assert captured["responses"] == []
    assert captured["chat"] == []


def test_transport_and_provider_error_telemetry_redacts_adversarial_credentials() -> (
    None
):
    from src.backend.languagemodels.structured_tool_calling.transport import (
        sanitise_transport_telemetry_value,
    )
    from src.backend.workflows.trace_model import sanitise_for_trace_storage

    raw = {
        "token": "generic-token-value",
        "authorization": "Bearer auth-value",
        "cookie": "session=private-cookie",
        "private_key": "private-key-material",
        "nested": {"api_key": "non-sk-api-value"},
        "message": (
            "Authorization: Bearer header-value token=query-value "
            "Cookie: session=header-cookie"
        ),
    }
    sanitised = sanitise_transport_telemetry_value(raw)
    persisted = json.dumps(sanitised, sort_keys=True)
    for secret in (
        "generic-token-value",
        "auth-value",
        "private-cookie",
        "private-key-material",
        "non-sk-api-value",
        "header-value",
        "query-value",
        "header-cookie",
    ):
        assert secret not in persisted

    provider_error = OpenAIClient._sanitise_provider_error(
        RuntimeError(
            "Authorization: Bearer provider-auth token=provider-token "
            "Cookie: session=provider-cookie api_key=provider-key"
        )
    )
    for secret in (
        "provider-auth",
        "provider-token",
        "provider-cookie",
        "provider-key",
    ):
        assert secret not in provider_error

    provider_metadata_error = _CapabilityError("rejected")
    provider_metadata_error.body = {
        "error": {
            "code": "token=metadata-token",
            "param": "Authorization: Bearer metadata-auth",
            "type": "https://user:metadata-password@example.test/private",
        }
    }
    provider_metadata = OpenAIClient._provider_error_metadata(provider_metadata_error)
    provider_metadata_json = json.dumps(provider_metadata, sort_keys=True)
    for secret in ("metadata-token", "metadata-auth", "metadata-password"):
        assert secret not in provider_metadata_json

    trace_projection = sanitise_for_trace_storage(
        {"continuation": {"encrypted_content": "opaque-reasoning-state"}}
    )
    assert "opaque-reasoning-state" not in json.dumps(trace_projection)


def test_unknown_profile_conservatively_uses_chat_completions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "unknown-compatible-model"
    _install_profiles(monkeypatch, None)
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Use a tool",
            available_tools=[_tool()],
        )
    )

    assert result.text_response == "chat ok"
    assert captured["responses"] == []
    assert len(captured["chat"]) == 1
    request = captured["chat"][0]
    assert request["tools"][0]["function"]["name"] == "lookup"
    assert result.transport_metadata["effective_api_surface"] == ("chat_completions")
    assert result.transport_metadata["reason"] == (
        "conservative_chat_completions_default"
    )


def test_unscoped_native_profile_does_not_force_responses_on_compatible_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "native-profile-name-on-compatible-endpoint"
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            base_url="https://chat-only.example.test/v1",
            temperature=None,
        )
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Use a tool",
            available_tools=[_tool()],
        )
    )

    assert len(captured["chat"]) == 1
    assert captured["responses"] == []
    assert result.transport_metadata["capability_class"] == (
        "unknown_conservative_default"
    )
    assert str(result.transport_metadata["connection_id"]).startswith(
        "openai_compatible:"
    )


def test_connection_profile_overrides_unscoped_required_model_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels.structured_tool_calling.transport import (
        resolve_structured_tool_transport,
    )

    connection_chat = _chat_profile()
    connection_chat["connection_id"] = "#V#explicit_openai_connection"
    _install_profiles(
        monkeypatch,
        _registry_profiles(_responses_profile(), connection_chat),
    )

    decision = resolve_structured_tool_transport(
        provider="openai",
        model="arbitrary-model",
        tools_present=True,
        connection_id="#V#explicit_openai_connection",
        deployment_id="arbitrary-model",
    )

    assert decision.status == "compatible"
    assert decision.effective_api_surface == "chat_completions"
    assert decision.profile_concept_id == "#V#test_chat_profile"


def test_scoped_unsupported_chat_profile_is_not_masked_by_another_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels.structured_tool_calling.transport import (
        resolve_structured_tool_transport,
    )

    unsupported_chat = _chat_profile(capability="unsupported")
    unsupported_chat["connection_id"] = "#V#connection_a"
    supported_responses = _responses_profile(capability="supported")
    supported_responses["connection_id"] = "#V#connection_b"
    _install_profiles(
        monkeypatch,
        _registry_profiles(unsupported_chat, supported_responses),
    )

    decision = resolve_structured_tool_transport(
        provider="openai",
        model="arbitrary-model",
        tools_present=True,
        connection_id="#V#connection_a",
        deployment_id="arbitrary-model",
    )

    assert decision.status == "unsupported"
    assert decision.capability_class == "explicitly_unsupported"


def test_malformed_authoritative_api_surface_fails_closed_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = _responses_profile()
    malformed["api_surface"] = "response"
    _install_profiles(monkeypatch, _registry_profiles(malformed))
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(
            model="malformed-profile-model",
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    with pytest.raises(UnsupportedStructuredToolTransportError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="Use the tool.",
                available_tools=[_tool()],
            )
        )

    assert captured == {"responses": [], "chat": []}
    assert exc_info.value.decision["status"] == "unsupported"
    assert exc_info.value.decision["effective_api_surface"] == "response"
    assert exc_info.value.decision["reason"] == (
        "represented_profile_invalid_api_surface"
    )
    assert exc_info.value.decision["capability_class"] == (
        "invalid_represented_authority"
    )


@pytest.mark.parametrize(
    ("surface", "profile_updates", "expected_reason"),
    [
        (
            "responses",
            {"structured_tool_calling": "sometimes"},
            "represented_profile_invalid_structured_tool_capability",
        ),
        (
            "responses",
            {"tool_continuation_mode": "sticky_session"},
            "represented_profile_invalid_continuation_mode",
        ),
        (
            "responses",
            {"response_storage_policy": "retain_forever"},
            "represented_profile_invalid_response_storage_policy",
        ),
        (
            "responses",
            {
                "tool_continuation_mode": "provider_managed",
                "response_storage_policy": "disabled",
            },
            "represented_profile_contradictory_continuation_storage",
        ),
        (
            "responses",
            {
                "tool_continuation_mode": "stateless",
                "response_storage_policy": "provider_managed",
            },
            "represented_profile_contradictory_continuation_storage",
        ),
        (
            "chat_completions",
            {
                "tool_continuation_mode": "provider_managed",
                "response_storage_policy": "enabled",
            },
            "represented_profile_chat_provider_managed_continuation",
        ),
    ],
)
def test_malformed_structured_transport_profile_fails_closed_before_request(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    profile_updates: Mapping[str, Any],
    expected_reason: str,
) -> None:
    profile = _chat_profile() if surface == "chat_completions" else _responses_profile()
    profile.update(profile_updates)
    _install_profiles(monkeypatch, _registry_profiles(profile))
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(
            model="malformed-transport-profile-model",
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    with pytest.raises(UnsupportedStructuredToolTransportError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="Use the tool.",
                available_tools=[_tool()],
            )
        )

    assert captured == {"responses": [], "chat": []}
    assert exc_info.value.decision["status"] == "unsupported"
    assert exc_info.value.decision["reason"] == expected_reason
    assert exc_info.value.decision["capability_class"] == (
        "invalid_represented_authority"
    )


@pytest.mark.parametrize("unsupported_first", [False, True])
def test_conflicting_same_surface_capabilities_fail_closed_before_request(
    monkeypatch: pytest.MonkeyPatch,
    unsupported_first: bool,
) -> None:
    supported = _responses_profile(capability="supported")
    unsupported = _responses_profile(capability="unsupported")
    profiles = (
        (unsupported, supported) if unsupported_first else (supported, unsupported)
    )
    _install_profiles(monkeypatch, _registry_profiles(*profiles))
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(
            model="conflicting-responses-profile-model",
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    with pytest.raises(UnsupportedStructuredToolTransportError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="Use the tool.",
                available_tools=[_tool()],
            )
        )

    assert captured == {"responses": [], "chat": []}
    assert exc_info.value.decision["reason"] == (
        "represented_profiles_conflicting_surface_capability"
    )
    assert exc_info.value.decision["capability_class"] == (
        "invalid_represented_authority"
    )


def test_multiple_required_surfaces_fail_closed_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_profiles(
        monkeypatch,
        _registry_profiles(
            _chat_profile(capability="required"),
            _responses_profile(capability="required"),
        ),
    )
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(
            model="multiple-required-surfaces-model",
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    with pytest.raises(UnsupportedStructuredToolTransportError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="Use the tool.",
                available_tools=[_tool()],
            )
        )

    assert captured == {"responses": [], "chat": []}
    assert exc_info.value.decision["reason"] == (
        "represented_profiles_multiple_required_surfaces"
    )
    assert exc_info.value.decision["capability_class"] == (
        "invalid_represented_authority"
    )


def test_legacy_profile_without_structured_capability_remains_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_profile = {
        "profile_concept_id": "#V#legacy_chat_profile",
        "api_surface": "chat_completions",
        "tool_continuation_mode": "legacy_unspecified_mode",
        "response_storage_policy": "legacy_unspecified_storage",
    }
    _install_profiles(monkeypatch, _registry_profiles(legacy_profile))
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(
            model="legacy-profile-model",
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Use the tool.",
            available_tools=[_tool()],
        )
    )

    assert captured["responses"] == []
    assert len(captured["chat"]) == 1
    assert result.transport_metadata["reason"] == (
        "conservative_chat_completions_default"
    )


def test_no_tool_request_preserves_chat_surface_even_with_responses_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "responses-profile-without-tools"
    _install_profiles(monkeypatch, _registry_profiles(_responses_profile()))
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Answer without tools",
            available_tools=[],
        )
    )

    assert len(captured["chat"]) == 1
    assert captured["responses"] == []
    assert result.transport_metadata["effective_api_surface"] == ("chat_completions")
    assert result.transport_metadata["reason"] == "no_structured_tools_in_request"


def test_explicitly_unsupported_profile_raises_typed_error_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "unsupported-deployment"
    _install_profiles(
        monkeypatch,
        _registry_profiles(_responses_profile(capability="unsupported")),
    )
    captured = _install_fake_openai(monkeypatch)
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    with pytest.raises(UnsupportedStructuredToolTransportError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="Use a tool",
                available_tools=[_tool()],
            )
        )

    assert exc_info.value.failure_kind == "structured_tool_transport_unsupported"
    assert exc_info.value.decision["status"] == "unsupported"
    assert exc_info.value.decision["reason"] == (
        "represented_profiles_explicitly_unsupported"
    )
    assert captured == {"responses": [], "chat": []}


def test_provider_capability_400_becomes_sanitised_typed_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels.structured_tool_calling.providers import (
        openai_client as provider_module,
    )

    model = "chat-capability-rejection"
    _install_profiles(monkeypatch, None)

    async def rejected_chat_create(**_kwargs: Any) -> Any:
        raise _CapabilityError(
            "Function tools are not supported; use /v1/responses. "
            "See https://user:secret@example.test/private?api_key=credential"
        )

    monkeypatch.setattr(
        provider_module.openai,
        "AsyncOpenAI",
        lambda **_kwargs: types.SimpleNamespace(
            responses=types.SimpleNamespace(create=lambda **_call_kwargs: None),
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=rejected_chat_create)
            ),
        ),
    )
    monkeypatch.setattr(provider_module.openai, "OpenAI", lambda **_kwargs: object())
    client = OpenAIClient(
        LLMClientConfig(
            model=model,
            provider="openai",
            api_key="test-key",
            temperature=None,
        )
    )

    with pytest.raises(StructuredToolCapabilityRejectedError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="Use a tool",
                available_tools=[_tool()],
            )
        )

    error = exc_info.value
    assert error.failure_kind == "structured_tool_capability_rejected"
    assert error.decision["provider_status_code"] == 400
    assert error.decision["provider_error_code"] == ("unsupported_model_tool_transport")
    assert error.decision["provider_error_param"] == "tools"
    assert error.decision["effective_api_surface"] == "chat_completions"
    assert "user:secret" not in str(error)
    assert "/private" not in str(error)
    assert "credential" not in str(error)
