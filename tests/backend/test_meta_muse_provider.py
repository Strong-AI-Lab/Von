from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import src.backend.languagemodels.llm_interface as llm_module
from src.backend.languagemodels.llm_interface import (
    META_MODEL_API_BASE_URL,
    MetaMuseClient,
    get_llm_client,
    infer_llm_client_provider,
)
from src.backend.languagemodels.structured_tool_calling import (
    LLMClientConfig,
    StructuredToolProtocolError,
    ToolDefinition,
)
from src.backend.languagemodels.structured_tool_calling.providers import (
    OpenAIClient as StructuredOpenAIClient,
)
from src.backend.languagemodels.structured_tool_calling import transport
from src.backend.services.llm_api_key_resolution import get_meta_api_key
from src.backend.services.model_parameter_service import (
    build_model_parameter_capabilities,
    meta_responses_kwargs_from_model_parameters,
)


class _SyncMetaStream:
    def __init__(self, events: list[Any]):
        self.events = list(events)
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self) -> None:
        self.closed = True


class _AsyncMetaStream:
    def __init__(self, events: list[Any]):
        self.events = list(events)
        self.closed = False

    def __aiter__(self):
        async def _iterate():
            for event in self.events:
                yield event

        return _iterate()

    async def aclose(self) -> None:
        self.closed = True


def _completed_text_response(text: str = "META-MUSE-OK") -> dict[str, Any]:
    return {
        "id": "resp_meta_1",
        "model": "muse-spark-1.3",
        "status": "completed",
        "output": [
            {
                "id": "msg_meta_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
    }


def _install_sync_meta_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: _SyncMetaStream | None = None,
    models: list[str] | None = None,
) -> tuple[dict[str, Any], _SyncMetaStream]:
    captured: dict[str, Any] = {}
    selected_stream = stream or _SyncMetaStream(
        [
            {"type": "response.output_text.delta", "delta": "META-MUSE-OK"},
            {"type": "response.completed", "response": _completed_text_response()},
        ]
    )

    class _Responses:
        @staticmethod
        def create(**kwargs: Any) -> _SyncMetaStream:
            captured["request"] = dict(kwargs)
            return selected_stream

    class _Models:
        @staticmethod
        def list() -> dict[str, Any]:
            return {"data": [{"id": model} for model in (models or [])]}

    def _factory(**kwargs: Any) -> Any:
        captured["client"] = dict(kwargs)
        return SimpleNamespace(responses=_Responses(), models=_Models())

    monkeypatch.setattr(llm_module.openai, "OpenAI", _factory)
    return captured, selected_stream


def test_meta_key_uses_only_canonical_direct_or_file_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    secret_file = tmp_path / "meta_api_key"
    secret_file.write_text("file-meta-key", encoding="utf-8")
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.setenv("META_API_KEY_FILE", str(secret_file))
    monkeypatch.setenv("MODEL_API_KEY", "must-not-be-used")

    assert get_meta_api_key() == "file-meta-key"

    monkeypatch.setenv("META_API_KEY", "direct-meta-key")
    assert get_meta_api_key() == "direct-meta-key"


def test_meta_reasoning_effort_capability_maps_to_responses() -> None:
    capability = build_model_parameter_capabilities(
        provider="meta",
        model="muse-spark-1.3",
        api_surface="responses",
        include_registry=False,
    )

    reasoning = capability["parameters"]["reasoning_effort"]
    assert reasoning["supported"] is True
    assert reasoning["allowed_values"] == ["minimal", "low", "medium", "high"]
    assert meta_responses_kwargs_from_model_parameters(
        {"reasoning_effort": "medium"},
        model="muse-spark-1.3",
    ) == {"reasoning": {"effort": "medium"}}


def test_meta_generate_uses_exact_streaming_responses_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, stream = _install_sync_meta_client(monkeypatch)
    eligibility_events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        llm_module,
        "assert_model_execution_allowed",
        lambda **kwargs: eligibility_events.append(dict(kwargs)) or {"allowed": True},
    )
    client = MetaMuseClient(
        api_key="configured-meta-key",
        model_execution_user_concept_id="#V#meta_user",
        model_execution_actor_scope_bound=True,
    )

    result = client.generate("Reply exactly with META-MUSE-OK")

    assert result == "META-MUSE-OK"
    assert captured["client"] == {
        "api_key": "configured-meta-key",
        "base_url": META_MODEL_API_BASE_URL,
    }
    request = captured["request"]
    assert request["model"] == "muse-spark-1.3"
    assert request["input"] == [
        {"role": "user", "content": "Reply exactly with META-MUSE-OK"}
    ]
    assert request["stream"] is True
    assert request["extra_headers"] == {"Accept": "text/event-stream"}
    assert request["temperature"] == 1.0
    assert request["top_p"] == 1.0
    assert request["max_output_tokens"] == 32000
    assert request["reasoning"] == {"effort": "medium"}
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert eligibility_events == [
        {
            "provider": "meta",
            "model": "muse-spark-1.3",
            "user_concept_id": "#V#meta_user",
            "org_concept_id": None,
            "allow_ambient_actor_scope": False,
        }
    ]
    assert stream.closed is True
    assert client.last_response_metadata["provider"] == "meta"
    assert client.last_response_metadata["api_surface"] == "responses"
    assert client.last_response_metadata["usage"] == {
        "input_tokens": 5,
        "output_tokens": 3,
        "total_tokens": 8,
    }


def test_meta_rejects_other_models_before_eligibility_or_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, _stream = _install_sync_meta_client(monkeypatch)
    eligibility_checked = False

    def _eligibility(**_kwargs: Any) -> dict[str, bool]:
        nonlocal eligibility_checked
        eligibility_checked = True
        return {"allowed": True}

    monkeypatch.setattr(llm_module, "assert_model_execution_allowed", _eligibility)
    client = MetaMuseClient(api_key="configured-meta-key")

    with pytest.raises(ValueError, match="supports only model"):
        client.generate("test", model="future-meta-model")

    assert eligibility_checked is False
    assert "request" not in captured


def test_meta_stream_failure_redacts_the_exact_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "opaque-meta-secret-sentinel"
    failed_stream = _SyncMetaStream(
        [
            {
                "type": "response.failed",
                "error": f"billing_not_configured {secret}",
            }
        ]
    )
    _install_sync_meta_client(monkeypatch, stream=failed_stream)
    monkeypatch.setattr(
        llm_module,
        "assert_model_execution_allowed",
        lambda **_kwargs: {"allowed": True},
    )
    client = MetaMuseClient(api_key=secret)

    with pytest.raises(RuntimeError) as exc_info:
        client.generate("test")

    assert secret not in str(exc_info.value)
    assert "billing_not_configured" in str(exc_info.value)
    assert failed_stream.closed is True


def test_meta_catalogue_filters_to_the_supported_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sync_meta_client(
        monkeypatch,
        models=["muse-spark-1.3", "muse-spark-1.3-contributor", "muse-image-1.0"],
    )
    client = MetaMuseClient(api_key="configured-meta-key")

    assert client.list_models() == ["muse-spark-1.3"]


def test_meta_factory_binds_actor_scope_without_cross_provider_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sync_meta_client(monkeypatch)
    monkeypatch.setattr(llm_module, "initialize_clients", lambda **_kwargs: None)
    monkeypatch.setattr(
        llm_module,
        "_resolve_effective_llm_actor_scope",
        lambda *_args, **_kwargs: ("#V#meta_user", None),
    )
    fallback_used = False

    def _fallback() -> None:
        nonlocal fallback_used
        fallback_used = True
        return None

    monkeypatch.setattr(llm_module, "_ensure_ollama_client", _fallback)

    client = get_llm_client(client_type="meta", api_key="configured-meta-key")

    assert isinstance(client, MetaMuseClient)
    assert infer_llm_client_provider(client) == "meta"
    assert client._model_execution_user_concept_id == "#V#meta_user"
    assert client._model_execution_actor_scope_bound is True
    assert fallback_used is False


def test_meta_factory_missing_key_does_not_fall_back_to_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("META_API_KEY_FILE", raising=False)
    monkeypatch.setattr(llm_module, "initialize_clients", lambda **_kwargs: None)
    monkeypatch.setattr(
        llm_module,
        "_resolve_effective_llm_actor_scope",
        lambda *_args, **_kwargs: ("#V#meta_user", None),
    )
    fallback_used = False

    def _fallback() -> None:
        nonlocal fallback_used
        fallback_used = True
        return None

    monkeypatch.setattr(llm_module, "_ensure_ollama_client", _fallback)

    with pytest.raises(RuntimeError, match="No cross-provider fallback"):
        get_llm_client(client_type="meta")

    assert fallback_used is False


def test_meta_provider_qualification_is_preserved_across_workflow_routing() -> None:
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )
    from src.backend.server.routes.von_routes import (
        _resolve_generate_requested_model,
    )
    from src.backend.workflows.durable.durable_executor import (
        _infer_client_type_from_model,
    )
    from src.backend.workflows.model_execution_budget_policy import (
        _split_provider_from_model as split_budget_model,
    )
    from src.backend.workflows.prompt_metadata_resolution import (
        _provider_from_model_token as split_prompt_model,
    )

    candidate = InternalMCPChatOrchestrator._parse_policy_model_candidate(
        "meta:muse-spark-1.3"
    )
    default_candidate = InternalMCPChatOrchestrator._parse_policy_model_candidate(
        "meta:default"
    )
    request_model, request_provider, request_parameters = (
        _resolve_generate_requested_model(
            {
                "model": "muse-spark-1.3",
                "model_provider": "meta",
            },
            user_concept_id="#V#meta_user",
            org_concept_id=None,
            configured_model=None,
        )
    )

    assert candidate is not None
    assert candidate.provider == "meta"
    assert candidate.model == "muse-spark-1.3"
    assert default_candidate is not None
    assert default_candidate.provider == "meta"
    assert default_candidate.model == "muse-spark-1.3"
    assert _infer_client_type_from_model("meta:muse-spark-1.3") == "meta"
    assert split_budget_model("meta:muse-spark-1.3") == (
        "meta",
        "muse-spark-1.3",
    )
    assert split_prompt_model("meta:muse-spark-1.3") == (
        "meta",
        "muse-spark-1.3",
    )
    assert (request_model, request_provider, request_parameters) == (
        "muse-spark-1.3",
        "meta",
        {},
    )


def test_meta_structured_tools_use_streaming_responses_and_stateless_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport, "resolve_model_api_profiles", lambda **_kwargs: {})
    response = {
        "id": "resp_tool_1",
        "model": "muse-spark-1.3",
        "status": "completed",
        "output": [
            {
                "id": "reasoning_1",
                "type": "reasoning",
                "encrypted_content": "opaque-reasoning",
            },
            {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": '{"query":"Muse"}',
            },
        ],
        "usage": {"input_tokens": 8, "output_tokens": 5, "total_tokens": 13},
    }
    stream = _AsyncMetaStream(
        [{"type": "response.completed", "response": response}]
    )
    captured: dict[str, Any] = {}

    async def _create(**kwargs: Any) -> _AsyncMetaStream:
        captured.update(kwargs)
        return stream

    request_client = SimpleNamespace(
        responses=SimpleNamespace(create=_create),
    )
    client = StructuredOpenAIClient(
        LLMClientConfig(
            model="muse-spark-1.3",
            provider="meta",
            api_key="configured-meta-key",
            base_url=META_MODEL_API_BASE_URL,
            connection_id="#V#meta_provider",
            deployment_id="muse-spark-1.3",
            requested_api_surface="responses",
            temperature=1.0,
            max_tokens=32000,
        )
    )
    tool = ToolDefinition(
        name="lookup",
        description="Look up a query.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    result = asyncio.run(
        client.generate_with_tools(
            prompt="Use lookup.",
            available_tools=[tool],
            llm_params={
                "reasoning_effort": "medium",
                "temperature": 0.2,
                "top_p": 0.3,
                "max_output_tokens": 7,
            },
            top_p=0.4,
            _request_client=request_client,
        )
    )

    assert captured["stream"] is True
    assert captured["extra_headers"] == {"Accept": "text/event-stream"}
    assert captured["model"] == "muse-spark-1.3"
    assert captured["max_output_tokens"] == 32000
    assert captured["temperature"] == 1.0
    assert captured["top_p"] == 1.0
    assert captured["reasoning"] == {"effort": "medium"}
    assert captured["store"] is False
    assert captured["include"] == ["reasoning.encrypted_content"]
    assert result.tool_calls[0].tool_name == "lookup"
    assert result.tool_calls[0].call_id == "call_1"
    assert result.continuation is not None
    assert result.continuation.provider == "meta"
    assert result.continuation.state_mode == "stateless"
    assert result.transport_metadata["stream"] is True
    assert stream.closed is True


def test_meta_structured_stream_requires_completed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport, "resolve_model_api_profiles", lambda **_kwargs: {})
    stream = _AsyncMetaStream(
        [{"type": "response.output_text.delta", "delta": "partial"}]
    )

    async def _create(**_kwargs: Any) -> _AsyncMetaStream:
        return stream

    client = StructuredOpenAIClient(
        LLMClientConfig(
            model="muse-spark-1.3",
            provider="meta",
            api_key="configured-meta-key",
            requested_api_surface="responses",
        )
    )

    with pytest.raises(StructuredToolProtocolError) as exc_info:
        asyncio.run(
            client.generate_with_tools(
                prompt="test",
                available_tools=[],
                _request_client=SimpleNamespace(
                    responses=SimpleNamespace(create=_create)
                ),
            )
        )

    assert exc_info.value.decision["failure_kind"] == "meta_stream_incomplete"
    assert stream.closed is True


def test_meta_transport_rejects_represented_chat_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        transport,
        "resolve_model_api_profiles",
        lambda **_kwargs: {
            "source": "vontology_graph",
            "api_profiles": [
                {
                    "profile_concept_id": "#V#meta_chat_profile",
                    "api_surface": "chat_completions",
                    "structured_tool_calling": "supported",
                    "tool_continuation_mode": "stateless",
                    "response_storage_policy": "disabled",
                }
            ],
        },
    )

    decision = transport.resolve_structured_tool_transport(
        provider="meta",
        model="muse-spark-1.3",
        tools_present=True,
        requested_api_surface="responses",
    )

    assert decision.status == "unsupported"
    assert decision.reason == "meta_responses_only"
    assert decision.effective_api_surface == "chat_completions"
