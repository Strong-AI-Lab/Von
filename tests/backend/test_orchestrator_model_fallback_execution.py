from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any, Mapping, cast

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    CancellationRequested,
    InternalMCPChatOrchestrator,
    _ModelCandidate,
    _ModelCandidateClientResolutionError,
    _PromptRequirementEvaluation,
    _WorkflowModelPolicyState,
)
from src.backend.languagemodels.structured_tool_calling.types import (
    LLMContinuation,
    LLMResponse,
    StructuredToolCapabilityRejectedError,
    StructuredToolProtocolError,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from src.backend.services.synthesiser_context_framing_service import (
    SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA,
    SynthesiserContextFramingTemplate,
)
from src.backend.workflows.action_registry import WorkflowActionResult
from src.backend.workflows.durable import synthesiser_context_prep_actions as synth_mod


class _StubGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}

    def invoke(self, _tool_name: str, _payload: Mapping[str, Any]):  # pragma: no cover
        raise AssertionError("Gateway should not be invoked in this test")


class _FailingClient:
    def generate(self, *_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("primary model unavailable")


class _SuccessfulClient:
    def generate(self, *_args: Any, **_kwargs: Any) -> str:
        return '["Proceed", "Hold"]'


class _NeverUsedClient:
    def __init__(self) -> None:
        self.generate_calls = 0
        self.structured_calls = 0

    def generate(self, *_args: Any, **_kwargs: Any) -> str:
        self.generate_calls += 1
        raise AssertionError("default client must not replace a Gemini candidate")

    def _should_use_structured_calling(self) -> bool:
        return True

    def generate_with_tools(self, **_kwargs: Any) -> LLMResponse:
        self.structured_calls += 1
        raise AssertionError("default client must not replace a Gemini candidate")


class _SuccessfulOllamaClient(_SuccessfulClient):
    def __init__(self, *, default_model: str = "gemma4:26b") -> None:
        self.default_model = default_model
        self.host = "http://localhost:11434"


class _StubOpenAIClient:
    def generate(self, *_args: Any, **_kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("OpenAI client should not be used in this test")


class _StubOllamaClient:
    def __init__(self, *, default_model: str = "llama3.2:latest") -> None:
        self.default_model = default_model
        self.host = "http://localhost:11434"
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        _prompt: str,
        *,
        context: list[dict[str, Any]] | None = None,
        model: str | None = None,
        **_kwargs: Any,
    ) -> str:
        self.calls.append({"context": context, "model": model})
        return "resolved client default"


class _StructuredToolClient:
    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def _should_use_structured_calling(self) -> bool:
        return True

    def generate_with_tools(
        self,
        *,
        prompt: str,
        available_tools: list[ToolDefinition],
        context: list[dict[str, Any]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "available_tools": [tool.name for tool in available_tools],
                "context": context,
                "model": model,
                "kwargs": dict(kwargs),
            }
        )
        return self.response


class _RejectingStructuredToolClient(_StructuredToolClient):
    def __init__(self, error: Exception) -> None:
        super().__init__(LLMResponse(text_response="unused"))
        self.error = error

    def generate_with_tools(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(dict(kwargs))
        raise self.error


def _policy_state() -> _WorkflowModelPolicyState:
    return _WorkflowModelPolicyState(
        enabled=True,
        policy={"stages": {"buttonify": {"primary": "ollama:granite3.3:2b"}}},
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )


def _active_llm_policy_state() -> _WorkflowModelPolicyState:
    return _WorkflowModelPolicyState(
        enabled=True,
        policy={"stages": {"classifier": {"primary": "active_llm", "fallback": []}}},
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )


def _bare_orchestrator() -> InternalMCPChatOrchestrator:
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    orchestrator._logger = logging.getLogger(__name__)
    orchestrator._provider_probe_cache = {}
    orchestrator._provider_probe_cache_max_entries = 32
    orchestrator._provider_probe_cooldown_seconds = 0
    orchestrator._workflow_model_policy_cache = {}
    orchestrator._workflow_model_policy_cache_ttl_seconds = 60
    orchestrator._max_context_chars = 120_000
    orchestrator._follow_up_context_chars = 40_000
    orchestrator._max_tool_result_chars = 5_000
    orchestrator._max_tool_result_field_chars = 1_500
    orchestrator._max_missing_tool_call_retries_per_turn = 3
    orchestrator._structured_tool_provider_default_limit = 128
    orchestrator._structured_tool_cap_headroom = 0
    orchestrator._structured_tool_candidate_cap_override = 0
    return orchestrator


def _install_gemini_failure_then_ollama_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ollama_client: Any,
) -> list[str | None]:
    from src.backend.languagemodels import llm_interface

    requested_providers: list[str | None] = []

    def _get_llm_client(*, client_type: str | None = None, **_kwargs: Any) -> Any:
        requested_providers.append(client_type)
        if client_type == "gemini":
            raise RuntimeError("api_key=must-not-appear")
        assert client_type == "ollama"
        return ollama_client

    monkeypatch.setattr(llm_interface, "get_llm_client", _get_llm_client)
    monkeypatch.setattr(
        llm_interface,
        "infer_llm_client_provider",
        lambda _client: "ollama",
    )
    monkeypatch.setattr(
        llm_interface,
        "resolve_effective_llm_model_for_client",
        lambda _client, requested_model: requested_model,
    )
    return requested_providers


def _install_synthesiser_context_template(monkeypatch: pytest.MonkeyPatch) -> None:
    template = SynthesiserContextFramingTemplate(
        prompt_concept_id="#V#test_synthesiser_context_framing_prompt",
        loaded_prompt_concept_id="#V#test_synthesiser_context_framing_prompt",
        schema_version=SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA,
        active_request_template="AUTH active request: {active_user_message}",
        tool_hints_template="AUTH tool {tool_concept_id}:\n{hint_sections}",
        collection_presentation_hint_template=(
            "AUTH collection: {collection_presentation_hint}"
        ),
        item_summary_hint_template="AUTH item: {item_summary_hint}",
        diagnostics={
            "loaded_prompt_concept_id": "#V#test_synthesiser_context_framing_prompt",
            "schema_version": SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA,
        },
    )
    monkeypatch.setattr(
        synth_mod,
        "resolve_synthesiser_context_framing_template",
        lambda **_kwargs: (template, dict(template.diagnostics)),
    )


def test_load_workflow_model_policy_returns_disabled_state_without_resolution(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    monkeypatch.setenv("VON_WORKFLOW_MODEL_POLICY_ENABLE", "0")
    monkeypatch.setattr(
        orchestrator,
        "_resolve_concept_id_by_name",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled policy load should not resolve concept names")
        ),
    )

    state, telemetry = orchestrator._load_workflow_model_policy(None)

    assert state.enabled is False
    assert state.policy is None
    assert state.policy_id is None
    assert state.predicate_id is None
    assert state.errors == ()
    assert telemetry is not None
    assert telemetry["policy_source"] == "disabled"
    assert telemetry["loaded"] is False


def test_resolve_concept_id_by_name_uses_direct_slug_for_existing_concept(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, *_args, **_kwargs: (
            {"_id": "1"}
            if query == {"concept_id": "#V#default_workflow_model_policy"}
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.vontology.code_concepts_registry.is_code_concept_id",
        lambda _concept_id: False,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct slug resolution should not call name search")
        ),
    )

    resolved = orchestrator._resolve_concept_id_by_name("default_workflow_model_policy")

    assert resolved == "#V#default_workflow_model_policy"


def test_resolve_concept_id_by_name_skips_name_search_for_missing_slug(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.vontology.code_concepts_registry.is_code_concept_id",
        lambda _concept_id: False,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing slug should fail fast without fuzzy search")
        ),
    )

    resolved = orchestrator._resolve_concept_id_by_name("default_workflow_model_policy")

    assert resolved is None


def test_run_llm_with_fallbacks_records_attempt_chain_and_fallback_metadata(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    ollama_candidate = _ModelCandidate(
        provider="ollama",
        model="granite3.3:2b",
        raw="ollama:granite3.3:2b",
        source="policy",
        host="http://localhost:11434",
    )
    openai_candidate = _ModelCandidate(
        provider="openai",
        model="gpt-5.2-chat-latest",
        raw="openai:gpt-5.2-chat-latest",
        source="policy",
    )

    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [ollama_candidate, openai_candidate],
    )

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **_kwargs: Any,
    ) -> tuple[Any, str, Mapping[str, Any]]:
        if candidate.provider == "ollama":
            return (
                _FailingClient(),
                "granite3.3:2b",
                {
                    "provider": "ollama",
                    "model": "granite3.3:2b",
                    "raw": candidate.raw,
                    "source": candidate.source,
                    "host": "http://localhost:11434",
                },
            )
        return (
            _SuccessfulClient(),
            "gpt-5.2-chat-latest",
            {
                "provider": "openai",
                "model": "gpt-5.2-chat-latest",
                "raw": candidate.raw,
                "source": candidate.source,
                "host": None,
            },
        )

    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        _create_client_for_candidate,
    )

    def _probe_model_candidate_reachability(
        *,
        telemetry: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        if telemetry.get("provider") != "ollama":
            return None
        return {
            "provider": "ollama",
            "host": "http://localhost:11434",
            "probe_url": "http://localhost:11434/api/tags",
            "probe_timeout_ms": 1200,
            "duration_ms": 7,
            "reachable": False,
            "error": "connection refused",
            "error_class": "ConnectionError",
        }

    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        _probe_model_candidate_reachability,
    )

    progress_events: list[dict[str, Any]] = []
    llm_calls_log: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []

    def _record_llm_call(**payload: Any) -> None:
        recorded_calls.append(dict(payload))

    response, model_name, telemetry = orchestrator._run_llm_with_fallbacks(
        stage="buttonify",
        prompt="Return quick-reply options",
        context=[],
        default_client=object(),
        default_model="gpt-5.2-chat-latest",
        policy_state=_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=llm_calls_log,
        aux_log=aux_log,
        record_llm_call=_record_llm_call,
        emit_progress=lambda payload: progress_events.append(dict(payload)),
    )

    assert response == '["Proceed", "Hold"]'
    assert model_name == "gpt-5.2-chat-latest"
    assert telemetry.get("provider") == "openai"

    first_end = next(
        event
        for event in progress_events
        if event.get("status") == "llm_call_end"
        and event.get("fallback_attempt_no") == 1
    )
    assert first_end["success"] is False
    assert first_end["failure_kind"] == "provider_unreachable"
    assert first_end["error_class"] == "ConnectionError"

    second_end = next(
        event
        for event in progress_events
        if event.get("status") == "llm_call_end"
        and event.get("fallback_attempt_no") == 2
    )
    assert second_end["success"] is True
    assert second_end["fallback_used"] is True

    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["fallback_attempt_count"] == 2
    assert stage_summary["failure_count"] == 1
    assert stage_summary["selected"]["provider"] == "openai"

    attempts = stage_summary["fallback_attempts"]
    assert isinstance(attempts, list)
    assert attempts[0]["failure_kind"] == "provider_unreachable"
    assert attempts[0]["error_class"] == "ConnectionError"
    assert attempts[1]["status"] == "succeeded"

    assert (
        recorded_calls[0]["note"]
        == "candidate reachability probe failed; trying fallback"
    )


def test_plain_fallback_does_not_substitute_default_client_for_failed_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _bare_orchestrator()
    default_client = _NeverUsedClient()
    fallback_client = _SuccessfulClient()
    requested_providers = _install_gemini_failure_then_ollama_client(
        monkeypatch,
        ollama_client=fallback_client,
    )
    candidates = [
        _ModelCandidate(
            provider="gemini",
            model="gemini-3.7-flash",
            raw="gemini:gemini-3.7-flash",
            source="enabled_settings",
        ),
        _ModelCandidate(
            provider="ollama",
            model="qwen3:8b",
            raw="ollama:qwen3:8b",
            source="settings_fallback",
        ),
    ]
    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: candidates,
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )
    recorded_calls: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []

    response, model_name, telemetry = orchestrator._run_llm_with_fallbacks(
        stage="summary",
        prompt="Summarise.",
        context=[],
        default_client=default_client,
        default_model="gemini-3.7-flash",
        policy_state=_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **payload: recorded_calls.append(dict(payload)),
    )

    assert response == '["Proceed", "Hold"]'
    assert model_name == "qwen3:8b"
    assert telemetry["provider"] == "ollama"
    assert requested_providers == ["gemini", "ollama"]
    assert default_client.generate_calls == 0
    assert recorded_calls[0]["provider"] == "gemini"
    assert recorded_calls[0]["error_class"] == (
        _ModelCandidateClientResolutionError.__name__
    )
    assert recorded_calls[0]["failure_kind"] == "candidate_client_resolution"
    assert "must-not-appear" not in str(recorded_calls)
    summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert summary["fallback_attempts"][0]["provider"] == "gemini"
    assert summary["selected"]["provider"] == "ollama"


def test_structured_fallback_does_not_substitute_default_client_for_failed_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _bare_orchestrator()
    default_client = _NeverUsedClient()
    fallback_client = _StructuredToolClient(LLMResponse(text_response="done"))
    requested_providers = _install_gemini_failure_then_ollama_client(
        monkeypatch,
        ollama_client=fallback_client,
    )
    candidates = [
        _ModelCandidate(
            provider="gemini",
            model="gemini-3.7-flash",
            raw="gemini:gemini-3.7-flash",
            source="enabled_settings",
        ),
        _ModelCandidate(
            provider="ollama",
            model="qwen3:8b",
            raw="ollama:qwen3:8b",
            source="settings_fallback",
        ),
    ]
    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: candidates,
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )
    recorded_calls: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []

    response, model_name, telemetry = orchestrator._run_llm_with_tools_fallbacks(
        stage="tool_call",
        prompt="Find evidence.",
        context=[],
        tool_definitions=[
            ToolDefinition(
                name="fetch_concept",
                description="Fetch a represented concept.",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        default_client=default_client,
        default_model="gemini-3.7-flash",
        policy_state=_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **payload: recorded_calls.append(dict(payload)),
        method_catalogue={"fetch_concept": {"category": "read"}},
    )

    assert response.text_response == "done"
    assert model_name == "qwen3:8b"
    assert telemetry["provider"] == "ollama"
    assert requested_providers == ["gemini", "ollama"]
    assert default_client.structured_calls == 0
    assert recorded_calls[0]["provider"] == "gemini"
    assert recorded_calls[0]["error_class"] == (
        _ModelCandidateClientResolutionError.__name__
    )
    assert recorded_calls[0]["failure_kind"] == "candidate_client_resolution"
    assert "must-not-appear" not in str(recorded_calls)
    summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert summary["fallback_attempts"][0]["provider"] == "gemini"
    assert summary["selected"]["provider"] == "ollama"


def test_run_llm_with_fallbacks_passes_candidate_model_parameters(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    candidate = _ModelCandidate(
        provider="openai",
        model="gpt-5.5",
        raw="openai:gpt-5.5",
        source="enabled_settings",
        model_parameters={"reasoning_effort": "low"},
    )

    class _RecordingClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def generate(self, _prompt: str, **kwargs: Any) -> str:
            self.calls.append(dict(kwargs))
            return "ok"

    client = _RecordingClient()
    monkeypatch.setattr(
        orchestrator, "_stage_model_candidates", lambda **_kwargs: [candidate]
    )
    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        lambda *_args, **_kwargs: (
            client,
            "gpt-5.5",
            {
                "provider": "openai",
                "model": "gpt-5.5",
                "raw": candidate.raw,
                "source": candidate.source,
                "model_parameters": {"reasoning_effort": "low"},
            },
        ),
    )
    monkeypatch.setattr(
        orchestrator, "_probe_model_candidate_reachability", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "_invoke_with_llm_heartbeat", lambda *, call, **_kwargs: call()
    )

    progress_events: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []

    response, model_name, telemetry = orchestrator._run_llm_with_fallbacks(
        stage="summary",
        prompt="Summarise",
        context=[],
        default_client=object(),
        default_model="gpt-5.5",
        default_model_parameters={"reasoning_effort": "low"},
        policy_state=_WorkflowModelPolicyState(
            enabled=False,
            policy=None,
            policy_id=None,
            predicate_id=None,
            errors=(),
        ),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **_payload: None,
        emit_progress=lambda payload: progress_events.append(dict(payload)),
    )

    assert response == "ok"
    assert model_name == "gpt-5.5"
    assert telemetry["model_parameters"] == {"reasoning_effort": "low"}
    assert client.calls[0]["llm_params"] == {"reasoning_effort": "low"}
    end_event = next(
        event for event in progress_events if event.get("status") == "llm_call_end"
    )
    assert end_event["requested_model"] == "gpt-5.5"
    assert end_event["selected_model"] == "gpt-5.5"
    assert end_event["effective_model"] is None
    assert end_event["model_identity_source"] is None
    assert end_event["effective_model_parameters"] == {"reasoning_effort": "low"}
    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["selected_model"] == "gpt-5.5"
    assert stage_summary["effective_model"] is None
    assert stage_summary["model_identity_source"] is None
    assert stage_summary["effective_model_parameters"] == {"reasoning_effort": "low"}


def test_run_llm_with_tools_fallbacks_passes_default_model_parameters(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    requested_parameters = {"reasoning_effort": "none"}
    candidate = _ModelCandidate(
        provider="openai",
        model="gpt-5.6-luna",
        raw="openai:gpt-5.6-luna",
        source="active_llm",
        model_parameters=requested_parameters,
    )
    client = _StructuredToolClient(
        LLMResponse(
            text_response="",
            model="gpt-5.6-luna-provider-response",
            tool_calls=[
                ToolCall(
                    tool_name="fetch_concept",
                    payload={"concept_id": "#V#represented_workflow"},
                    call_id="call-parameters",
                )
            ],
        )
    )

    def _stage_model_candidates(**kwargs: Any) -> list[_ModelCandidate]:
        assert kwargs["default_model_parameters"] == requested_parameters
        return [candidate]

    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        _stage_model_candidates,
    )
    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        lambda *_args, **_kwargs: (
            client,
            "gpt-5.6-luna",
            {
                "provider": "openai",
                "model": "gpt-5.6-luna",
                "raw": candidate.raw,
                "source": candidate.source,
                "model_parameters": requested_parameters,
            },
        ),
    )
    monkeypatch.setattr(
        orchestrator, "_probe_model_candidate_reachability", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "_invoke_with_llm_heartbeat", lambda *, call, **_kwargs: call()
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.resolve_structured_tool_transport",
        lambda **_kwargs: SimpleNamespace(
            to_telemetry=lambda: {
                "effective_api_surface": "responses",
                "status": "compatible",
            }
        ),
    )
    progress_events: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []

    response, model_name, _ = orchestrator._run_llm_with_tools_fallbacks(
        stage="tool_call",
        prompt="Describe the represented workflow.",
        context=[],
        tool_definitions=[
            ToolDefinition(
                name="fetch_concept",
                description="Fetch a represented concept.",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        default_client=object(),
        default_model="gpt-5.6-luna",
        default_model_parameters=requested_parameters,
        policy_state=_WorkflowModelPolicyState(
            enabled=False,
            policy=None,
            policy_id=None,
            predicate_id=None,
            errors=(),
        ),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **_payload: None,
        emit_progress=lambda payload: progress_events.append(dict(payload)),
        method_catalogue={"fetch_concept": {"category": "read"}},
    )

    assert response.tool_calls[0].call_id == "call-parameters"
    assert model_name == "gpt-5.6-luna"
    assert client.calls[0]["kwargs"]["llm_params"] == requested_parameters
    end_event = next(
        event for event in progress_events if event.get("status") == "llm_call_end"
    )
    assert end_event["requested_model"] == "gpt-5.6-luna"
    assert end_event["selected_model"] == "gpt-5.6-luna"
    assert end_event["effective_model"] == "gpt-5.6-luna-provider-response"
    assert end_event["model_identity_source"] == "provider_response"
    assert end_event["effective_model_parameters"] == requested_parameters
    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["selected_model"] == "gpt-5.6-luna"
    assert stage_summary["effective_model"] == "gpt-5.6-luna-provider-response"
    assert stage_summary["model_identity_source"] == "provider_response"
    assert stage_summary["requested_model_parameters"] == requested_parameters
    assert stage_summary["effective_model_parameters"] == requested_parameters


def test_plain_generate_records_provider_observed_gemini_identity_and_usage(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    candidate = _ModelCandidate(
        provider="gemini",
        model="gemini-3.7-flash",
        raw="gemini:gemini-3.7-flash",
        source="active_llm",
    )

    class _GeminiClient:
        last_response_metadata: dict[str, Any] = {}

        def generate(self, _prompt: str, **_kwargs: Any) -> str:
            self.last_response_metadata = {
                "api_surface": "interactions",
                "effective_model": "gemini-3.7-flash-20260815",
                "usage": {
                    "input_tokens": 20,
                    "visible_output_tokens": 6,
                    "output_tokens": 15,
                    "thought_tokens": 9,
                    "total_tokens": 35,
                },
                "transport_metadata": {
                    "provider": "gemini",
                    "effective_api_surface": "interactions",
                    "store": False,
                },
            }
            return "grounded"

    client = _GeminiClient()
    monkeypatch.setattr(
        orchestrator, "_stage_model_candidates", lambda **_kwargs: [candidate]
    )
    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        lambda *_args, **_kwargs: (
            client,
            "gemini-3.7-flash",
            {
                "provider": "gemini",
                "model": "gemini-3.7-flash",
                "raw": candidate.raw,
                "source": candidate.source,
            },
        ),
    )
    monkeypatch.setattr(
        orchestrator, "_probe_model_candidate_reachability", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "_invoke_with_llm_heartbeat", lambda *, call, **_kwargs: call()
    )
    recorded_calls: list[dict[str, Any]] = []
    progress_events: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []

    response, _, telemetry = orchestrator._run_llm_with_fallbacks(
        stage="summary",
        prompt="Summarise.",
        context=[],
        default_client=object(),
        default_model="gemini-3.7-flash",
        policy_state=_WorkflowModelPolicyState(
            enabled=False,
            policy=None,
            policy_id=None,
            predicate_id=None,
            errors=(),
        ),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **payload: recorded_calls.append(dict(payload)),
        emit_progress=lambda payload: progress_events.append(dict(payload)),
    )

    assert response == "grounded"
    assert telemetry["effective_model"] == "gemini-3.7-flash-20260815"
    assert telemetry["api_surface"] == "interactions"
    assert telemetry["usage"]["thought_tokens"] == 9
    assert recorded_calls[0]["effective_model_name"] == ("gemini-3.7-flash-20260815")
    assert recorded_calls[0]["model_identity_source"] == "provider_response"
    assert recorded_calls[0]["usage"]["output_tokens"] == 15
    chunk = next(
        event for event in progress_events if event.get("status") == "llm_call_chunk"
    )
    assert chunk["tokens_streamed"] == 6
    summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert summary["effective_model"] == "gemini-3.7-flash-20260815"


def test_gemini_structured_stage_receives_model_parameters_without_openai_options(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    requested_parameters = {"reasoning_effort": "high"}
    candidate = _ModelCandidate(
        provider="gemini",
        model="gemini-3.7-flash",
        raw="gemini:gemini-3.7-flash",
        source="active_llm",
        model_parameters=requested_parameters,
    )
    client = _StructuredToolClient(
        LLMResponse(
            text_response="done",
            model="gemini-3.7-flash-20260815",
            usage={
                "input_tokens": 8,
                "visible_output_tokens": 5,
                "output_tokens": 12,
                "thought_tokens": 7,
            },
            transport_metadata={
                "provider": "gemini",
                "credential_source": "backup",
                "credential_failover_used": True,
                "primary_credential_failure_kind": "rate_limited",
            },
        )
    )
    monkeypatch.setattr(
        orchestrator, "_stage_model_candidates", lambda **_kwargs: [candidate]
    )
    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        lambda *_args, **_kwargs: (
            client,
            "gemini-3.7-flash",
            {
                "provider": "gemini",
                "model": "gemini-3.7-flash",
                "raw": candidate.raw,
                "source": candidate.source,
                "model_parameters": requested_parameters,
            },
        ),
    )
    monkeypatch.setattr(
        orchestrator, "_probe_model_candidate_reachability", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "_invoke_with_llm_heartbeat", lambda *, call, **_kwargs: call()
    )

    progress_events: list[dict[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []
    response, _, _ = orchestrator._run_llm_with_tools_fallbacks(
        stage="tool_call",
        prompt="Find evidence.",
        context=[],
        tool_definitions=[
            ToolDefinition(
                name="fetch_concept",
                description="Fetch a represented concept.",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        default_client=object(),
        default_model="gemini-3.7-flash",
        default_model_parameters=requested_parameters,
        policy_state=_WorkflowModelPolicyState(
            enabled=False,
            policy=None,
            policy_id=None,
            predicate_id=None,
            errors=(),
        ),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **payload: recorded_calls.append(dict(payload)),
        emit_progress=lambda payload: progress_events.append(dict(payload)),
        method_catalogue={"fetch_concept": {"category": "read"}},
    )

    assert response.text_response == "done"
    structured_kwargs = client.calls[0]["kwargs"]
    assert structured_kwargs["llm_params"] == requested_parameters
    assert "parallel_tool_calls" not in structured_kwargs
    assert "tool_choice" not in structured_kwargs
    chunk = next(
        event for event in progress_events if event.get("status") == "llm_call_chunk"
    )
    assert chunk["tokens_streamed"] == 5
    assert recorded_calls[0]["candidate"]["transport_metadata"] == {
        "provider": "gemini",
        "credential_source": "backup",
        "credential_failover_used": True,
        "primary_credential_failure_kind": "rate_limited",
    }
    transport_event = next(
        event
        for event in aux_log
        if event.get("type") == "structured_tool_transport_decision"
    )
    assert transport_event["credential_source"] == "backup"


def test_failed_structured_stage_retains_requested_model_parameters(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    requested_parameters = {"reasoning_effort": "none"}
    candidate = _ModelCandidate(
        provider="openai",
        model="gpt-5.6-luna",
        raw="openai:gpt-5.6-luna",
        source="active_llm",
        model_parameters=requested_parameters,
    )
    client = _RejectingStructuredToolClient(RuntimeError("provider unavailable"))
    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [candidate],
    )
    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        lambda *_args, **_kwargs: (
            client,
            candidate.model,
            {
                "provider": candidate.provider,
                "model": candidate.model,
                "raw": candidate.raw,
                "source": candidate.source,
                "model_parameters": requested_parameters,
            },
        ),
    )
    monkeypatch.setattr(
        orchestrator, "_probe_model_candidate_reachability", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "_invoke_with_llm_heartbeat", lambda *, call, **_kwargs: call()
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.resolve_structured_tool_transport",
        lambda **_kwargs: SimpleNamespace(
            to_telemetry=lambda: {
                "effective_api_surface": "responses",
                "status": "compatible",
            }
        ),
    )
    aux_log: list[Mapping[str, Any]] = []

    with pytest.raises(RuntimeError, match="provider unavailable"):
        orchestrator._run_llm_with_tools_fallbacks(
            stage="tool_call",
            prompt="Describe the represented workflow.",
            context=[],
            tool_definitions=[
                ToolDefinition(
                    name="fetch_concept",
                    description="Fetch a represented concept.",
                    input_schema={"type": "object", "properties": {}},
                )
            ],
            default_client=object(),
            default_model=candidate.model,
            default_model_parameters=requested_parameters,
            policy_state=_WorkflowModelPolicyState(
                enabled=False,
                policy=None,
                policy_id=None,
                predicate_id=None,
                errors=(),
            ),
            registry_snapshot=None,
            user_concept_id=None,
            org_concept_id=None,
            llm_calls_log=[],
            aux_log=aux_log,
            record_llm_call=lambda **_payload: None,
            method_catalogue={"fetch_concept": {"category": "read"}},
        )

    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["requested_model_parameters"] == requested_parameters


def test_run_llm_with_fallbacks_tries_next_candidate_after_response_validation_failure(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    qwen_candidate = _ModelCandidate(
        provider="ollama",
        model="qwen3:8b",
        raw="ollama:qwen3:8b",
        source="active_llm",
        host="http://localhost:11434",
    )
    granite_candidate = _ModelCandidate(
        provider="ollama",
        model="granite3.3:2b",
        raw="ollama:granite3.3:2b",
        source="policy",
        host="http://localhost:11434",
    )

    class _MalformedJsonClient:
        def generate(self, *_args: Any, **_kwargs: Any) -> str:
            return '{"summary":"Use only the current request."}'

    class _ValidJsonClient:
        def generate(self, *_args: Any, **_kwargs: Any) -> str:
            return (
                '{"mode":"no_prior_context","summary":"Use only the current request."}'
            )

    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [qwen_candidate, granite_candidate],
    )

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **_kwargs: Any,
    ) -> tuple[Any, str, Mapping[str, Any]]:
        if candidate.model == "qwen3:8b":
            return (
                _MalformedJsonClient(),
                "qwen3:8b",
                {
                    "provider": "ollama",
                    "model": "qwen3:8b",
                    "raw": candidate.raw,
                    "source": candidate.source,
                    "host": "http://localhost:11434",
                },
            )
        return (
            _ValidJsonClient(),
            "granite3.3:2b",
            {
                "provider": "ollama",
                "model": "granite3.3:2b",
                "raw": candidate.raw,
                "source": candidate.source,
                "host": "http://localhost:11434",
            },
        )

    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        _create_client_for_candidate,
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )

    progress_events: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []

    def _response_validator(
        response_text: str,
        _model_name: str | None,
        _candidate: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        if '"mode"' in response_text:
            return None
        return {
            "reason": "json_required_fields_missing",
            "error_class": "WorkflowLLMStepValidationError",
            "failure_kind": "response_validation_failed",
            "validation": {
                "status": "failed",
                "missing_required_fields": ["mode"],
            },
        }

    response, model_name, telemetry = orchestrator._run_llm_with_fallbacks(
        stage="context_adjudication",
        prompt="Return context adjudication JSON",
        context=[],
        default_client=object(),
        default_model="qwen3:8b",
        policy_state=_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **payload: recorded_calls.append(dict(payload)),
        emit_progress=lambda payload: progress_events.append(dict(payload)),
        response_validator=_response_validator,
    )

    assert '"mode":"no_prior_context"' in response
    assert model_name == "granite3.3:2b"
    assert telemetry.get("model") == "granite3.3:2b"

    first_end = next(
        event
        for event in progress_events
        if event.get("status") == "llm_call_end"
        and event.get("fallback_attempt_no") == 1
    )
    assert first_end["success"] is False
    assert first_end["error"] == "json_required_fields_missing"
    assert first_end["failure_kind"] == "response_validation_failed"

    second_end = next(
        event
        for event in progress_events
        if event.get("status") == "llm_call_end"
        and event.get("fallback_attempt_no") == 2
    )
    assert second_end["success"] is True
    assert second_end["fallback_used"] is True

    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["fallback_attempt_count"] == 2
    assert stage_summary["failure_count"] == 1
    attempts = stage_summary["fallback_attempts"]
    assert attempts[0]["failure_kind"] == "response_validation_failed"
    assert attempts[0]["validation"]["validation"]["missing_required_fields"] == [
        "mode"
    ]
    assert attempts[1]["status"] == "succeeded"

    assert (
        recorded_calls[0]["note"]
        == "llm.generate response failed validation; trying fallback"
    )


def test_run_llm_with_tools_fallbacks_retries_when_required_tool_omitted(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    qwen_candidate = _ModelCandidate(
        provider="ollama",
        model="qwen3:8b",
        raw="ollama:qwen3:8b",
        source="active_llm",
        host="http://localhost:11434",
    )
    granite_candidate = _ModelCandidate(
        provider="ollama",
        model="granite3.3:2b",
        raw="ollama:granite3.3:2b",
        source="policy",
        host="http://localhost:11434",
    )
    first_client = _StructuredToolClient(
        LLMResponse(text_response="I can list those messages.", tool_calls=[])
    )
    second_client = _StructuredToolClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="gmail_list_messages",
                    payload={"max_results": 6},
                    call_id="call-2",
                )
            ],
            model="granite3.3:2b",
        )
    )

    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [qwen_candidate, granite_candidate],
    )

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **_kwargs: Any,
    ) -> tuple[Any, str, Mapping[str, Any]]:
        client = first_client if candidate.model == "qwen3:8b" else second_client
        return (
            client,
            str(candidate.model),
            {
                "provider": "ollama",
                "model": candidate.model,
                "raw": candidate.raw,
                "source": candidate.source,
                "host": "http://localhost:11434",
            },
        )

    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        _create_client_for_candidate,
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )

    progress_events: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []
    tool_definition = ToolDefinition(
        name="gmail_list_messages",
        description="List Gmail messages.",
        input_schema={"type": "object", "properties": {}},
    )

    response, model_name, telemetry = orchestrator._run_llm_with_tools_fallbacks(
        stage="tool_call",
        prompt="List my six most recent email messages.",
        context=[],
        tool_definitions=[tool_definition],
        default_client=object(),
        default_model="qwen3:8b",
        policy_state=_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **payload: recorded_calls.append(dict(payload)),
        emit_progress=lambda payload: progress_events.append(dict(payload)),
        method_catalogue={"gmail_list_messages": {"category": "read"}},
        required_prompt_tools=["gmail_list_messages"],
    )

    assert model_name == "granite3.3:2b"
    assert telemetry.get("model") == "granite3.3:2b"
    assert response.tool_calls[0].tool_name == "gmail_list_messages"
    assert first_client.calls[0]["available_tools"] == ["gmail_list_messages"]
    assert second_client.calls[0]["available_tools"] == ["gmail_list_messages"]

    first_end = next(
        event
        for event in progress_events
        if event.get("status") == "llm_call_end"
        and event.get("fallback_attempt_no") == 1
    )
    assert first_end["success"] is False
    assert first_end["error"] == "required_tool_call_omitted"
    assert first_end["failure_kind"] == "response_validation_failed"

    second_end = next(
        event
        for event in progress_events
        if event.get("status") == "llm_call_end"
        and event.get("fallback_attempt_no") == 2
    )
    assert second_end["success"] is True
    assert second_end["fallback_used"] is True

    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["fallback_attempt_count"] == 2
    assert stage_summary["failure_count"] == 1
    attempts = stage_summary["fallback_attempts"]
    assert attempts[0]["failure_kind"] == "response_validation_failed"
    assert attempts[0]["validation"]["reason"] == "required_tool_call_omitted"
    assert attempts[0]["validation"]["required_available_tools"] == [
        "gmail_list_messages"
    ]
    assert attempts[1]["status"] == "succeeded"
    assert recorded_calls[0]["status"] == "failed"
    assert recorded_calls[0]["error"] == "required_tool_call_omitted"


@pytest.mark.parametrize(
    ("response", "required_prompt_tools", "expected_reason"),
    [
        (
            LLMResponse(text_response="", tool_calls=[]),
            [],
            "empty_structured_response",
        ),
        (
            LLMResponse(
                text_response="I will look up the represented evidence.",
                tool_calls=[],
            ),
            ["synthetic_lookup"],
            "required_tool_call_omitted",
        ),
    ],
)
def test_run_llm_with_tools_fallbacks_rejects_invalid_final_candidate(
    monkeypatch,
    response: LLMResponse,
    required_prompt_tools: list[str],
    expected_reason: str,
) -> None:
    orchestrator = _bare_orchestrator()
    candidate = _ModelCandidate(
        provider="ollama",
        model="synthetic-tool-model",
        raw="ollama:synthetic-tool-model",
        source="active_llm",
        host="http://localhost:11434",
    )
    client = _StructuredToolClient(response)
    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [candidate],
    )
    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        lambda *_args, **_kwargs: (
            client,
            "synthetic-tool-model",
            {
                "provider": "ollama",
                "model": "synthetic-tool-model",
                "raw": "ollama:synthetic-tool-model",
                "source": "active_llm",
                "host": "http://localhost:11434",
            },
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )

    progress_events: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []
    tool_definition = ToolDefinition(
        name="synthetic_lookup",
        description="Retrieve represented evidence.",
        input_schema={"type": "object", "properties": {}},
    )

    with pytest.raises(
        RuntimeError,
        match="all_structured_model_candidates_failed:stage=tool_call",
    ):
        orchestrator._run_llm_with_tools_fallbacks(
            stage="tool_call",
            prompt="Retrieve represented evidence.",
            context=[],
            tool_definitions=[tool_definition],
            default_client=object(),
            default_model="synthetic-tool-model",
            policy_state=_policy_state(),
            registry_snapshot=None,
            user_concept_id=None,
            org_concept_id=None,
            llm_calls_log=[],
            aux_log=aux_log,
            record_llm_call=lambda **payload: recorded_calls.append(dict(payload)),
            emit_progress=lambda payload: progress_events.append(dict(payload)),
            method_catalogue={"synthetic_lookup": {"category": "read"}},
            required_prompt_tools=required_prompt_tools,
        )

    terminal_event = next(
        event for event in progress_events if event.get("status") == "llm_call_end"
    )
    assert terminal_event["success"] is False
    assert terminal_event["error"] == expected_reason
    assert terminal_event["failure_kind"] == "response_validation_failed"
    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["selected"] is None
    assert stage_summary["fallback_attempts"][0]["validation"]["reason"] == (
        expected_reason
    )
    assert recorded_calls[0]["status"] == "failed"
    assert recorded_calls[0]["error"] == expected_reason


def test_run_llm_with_tools_fallbacks_stops_after_typed_transport_rejection(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    first_candidate = _ModelCandidate(
        provider="openai",
        model="synthetic-primary",
        raw="openai:synthetic-primary",
        source="active_llm",
    )
    second_candidate = _ModelCandidate(
        provider="openai",
        model="synthetic-fallback",
        raw="openai:synthetic-fallback",
        source="policy",
    )
    decision = {
        "schema_version": "structured_tool_transport_decision.v1",
        "status": "compatible",
        "effective_api_surface": "chat_completions",
        "capability_key": "synthetic-primary-chat-tools",
    }
    first_client = _RejectingStructuredToolClient(
        StructuredToolCapabilityRejectedError(
            "The provider rejected this represented surface.",
            decision=decision,
        )
    )
    second_client = _StructuredToolClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="synthetic_lookup",
                    payload={"query": "value"},
                    call_id="call-fallback",
                )
            ],
        )
    )
    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [first_candidate, second_candidate],
    )

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **_kwargs: Any,
    ) -> tuple[Any, str, Mapping[str, Any]]:
        client = first_client if candidate is first_candidate else second_client
        return (
            client,
            str(candidate.model),
            {
                "provider": "openai",
                "model": candidate.model,
                "raw": candidate.raw,
                "source": candidate.source,
            },
        )

    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        _create_client_for_candidate,
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )
    aux_log: list[Mapping[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []

    with pytest.raises(StructuredToolCapabilityRejectedError):
        orchestrator._run_llm_with_tools_fallbacks(
            stage="tool_call",
            prompt="Look it up.",
            context=[],
            tool_definitions=[
                ToolDefinition(
                    name="synthetic_lookup",
                    description="Look up a synthetic value.",
                    input_schema={"type": "object", "properties": {}},
                )
            ],
            default_client=object(),
            default_model="synthetic-primary",
            policy_state=_policy_state(),
            registry_snapshot=None,
            user_concept_id=None,
            org_concept_id=None,
            llm_calls_log=[],
            aux_log=aux_log,
            record_llm_call=lambda **payload: recorded_calls.append(dict(payload)),
            method_catalogue={"synthetic_lookup": {"category": "read"}},
        )

    assert len(first_client.calls) == 1
    assert second_client.calls == []
    blocker = next(
        entry
        for entry in aux_log
        if entry.get("type") == "structured_tool_transport_blocker"
    )
    assert blocker["transport"] == decision
    assert blocker["llm_exchange_id"]
    assert blocker["call_id"]
    assert recorded_calls[0]["failure_kind"] == ("structured_tool_capability_rejected")


def test_run_llm_with_tools_fallbacks_retries_native_error_with_transport_evidence(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    first_candidate = _ModelCandidate(
        provider="openai",
        model="synthetic-primary",
        raw="openai:synthetic-primary",
        source="active_llm",
    )
    second_candidate = _ModelCandidate(
        provider="openai",
        model="synthetic-fallback",
        raw="openai:synthetic-fallback",
        source="policy",
    )
    transport_decision = {
        "schema_version": "structured_tool_transport_decision.v1",
        "status": "compatible",
        "effective_api_surface": "responses",
        "capability_key": "synthetic-primary-responses-tools",
        "alternate_failure_kind": "provider_error",
    }
    secret = "secret-provider-token-2583"
    native_error = RuntimeError(
        "transient provider timeout; Authorization: Bearer "
        f"{secret}; token={secret}; " + ("diagnostic-padding-" * 80)
    )
    native_error.structured_tool_transport_decision = transport_decision  # type: ignore[attr-defined]
    first_client = _RejectingStructuredToolClient(native_error)
    second_client = _StructuredToolClient(
        LLMResponse(
            text_response="",
            tool_calls=[
                ToolCall(
                    tool_name="synthetic_lookup",
                    payload={"query": "value"},
                    call_id="call-fallback",
                )
            ],
        )
    )
    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [first_candidate, second_candidate],
    )

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **_kwargs: Any,
    ) -> tuple[Any, str, Mapping[str, Any]]:
        client = first_client if candidate is first_candidate else second_client
        return (
            client,
            str(candidate.model),
            {
                "provider": "openai",
                "model": candidate.model,
                "raw": candidate.raw,
                "source": candidate.source,
            },
        )

    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        _create_client_for_candidate,
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )
    progress_events: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []

    response, model_name, telemetry = orchestrator._run_llm_with_tools_fallbacks(
        stage="tool_call",
        prompt="Look it up.",
        context=[],
        tool_definitions=[
            ToolDefinition(
                name="synthetic_lookup",
                description="Look up a synthetic value.",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        default_client=object(),
        default_model="synthetic-primary",
        policy_state=_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **payload: recorded_calls.append(dict(payload)),
        emit_progress=lambda payload: progress_events.append(dict(payload)),
        method_catalogue={"synthetic_lookup": {"category": "read"}},
    )

    assert model_name == "synthetic-fallback"
    assert telemetry["model"] == "synthetic-fallback"
    assert response.tool_calls[0].call_id == "call-fallback"
    assert len(first_client.calls) == 1
    assert len(second_client.calls) == 1
    first_end = next(
        event
        for event in progress_events
        if event.get("status") == "llm_call_end"
        and event.get("fallback_attempt_no") == 1
    )
    assert first_end["failure_kind"] == "candidate_error"
    assert first_end["structured_tool_transport"] == transport_decision
    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["errors"][0]["transport"] == transport_decision
    assert stage_summary["fallback_attempts"][0]["transport"] == transport_decision
    assert stage_summary["fallback_attempts"][1]["status"] == "succeeded"
    diagnostic_payloads = [
        first_end,
        stage_summary["errors"][0],
        stage_summary["fallback_attempts"][0],
        recorded_calls[0],
    ]
    for payload in diagnostic_payloads:
        persisted = json.dumps(payload, sort_keys=True)
        assert secret not in persisted
        error_text = payload.get("error")
        assert isinstance(error_text, str)
        assert len(error_text) <= 512


def test_run_llm_with_tools_fallbacks_rejects_malformed_continuation_before_client(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    candidate = _ModelCandidate(
        provider="openai",
        model="synthetic-primary",
        raw="openai:synthetic-primary",
        source="active_llm",
    )
    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [candidate],
    )
    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("malformed continuation must fail before client creation")
        ),
    )

    with pytest.raises(
        StructuredToolProtocolError,
        match="malformed continuation payload",
    ):
        orchestrator._run_llm_with_tools_fallbacks(
            stage="tool_follow_up",
            prompt="Continue.",
            context=[],
            tool_definitions=[
                ToolDefinition(
                    name="synthetic_lookup",
                    description="Look up a synthetic value.",
                    input_schema={"type": "object", "properties": {}},
                )
            ],
            default_client=object(),
            default_model="synthetic-primary",
            policy_state=_policy_state(),
            registry_snapshot=None,
            user_concept_id=None,
            org_concept_id=None,
            llm_calls_log=[],
            aux_log=[],
            record_llm_call=lambda **_payload: None,
            method_catalogue={"synthetic_lookup": {"category": "read"}},
            continuation={"provider": "openai"},
            tool_results=[
                ToolResult(
                    call_id="call-1",
                    tool_name="synthetic_lookup",
                    output="evidence",
                )
            ],
        )


def test_structured_tool_candidate_telemetry_omits_continuation_and_result_bodies(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    candidate = _ModelCandidate(
        provider="openai",
        model="synthetic-responses-model",
        raw="openai:synthetic-responses-model",
        source="active_llm",
    )
    client = _StructuredToolClient(
        LLMResponse(
            text_response="done",
            transport_metadata={
                "effective_api_surface": "responses",
                "status": "compatible",
            },
        )
    )
    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [candidate],
    )
    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        lambda *_args, **_kwargs: (
            client,
            candidate.model,
            {
                "provider": candidate.provider,
                "model": candidate.model,
                "raw": candidate.raw,
                "source": candidate.source,
            },
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.resolve_structured_tool_transport",
        lambda **_kwargs: SimpleNamespace(
            to_telemetry=lambda: {
                "effective_api_surface": "responses",
                "status": "compatible",
            }
        ),
    )
    continuation = LLMContinuation(
        provider="openai",
        api_surface="responses",
        model=candidate.model,
        transport_decision={
            "parameter_projection": {"reasoning_effort": "none"},
        },
        output_items=[
            {
                "type": "reasoning",
                "encrypted_content": "encrypted-provider-state-must-not-persist",
            }
        ],
    )
    tool_result = ToolResult(
        call_id="call-sensitive",
        tool_name="synthetic_lookup",
        output={"private": "tool-result-body-must-not-persist"},
    )
    aux_log: list[Mapping[str, Any]] = []

    response, _, _ = orchestrator._run_llm_with_tools_fallbacks(
        stage="tool_follow_up",
        prompt="Continue.",
        context=[],
        tool_definitions=[
            ToolDefinition(
                name="synthetic_lookup",
                description="Look up a synthetic value.",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        default_client=object(),
        default_model=candidate.model,
        default_model_parameters={"reasoning_effort": "low"},
        policy_state=_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **_payload: None,
        method_catalogue={"synthetic_lookup": {"category": "read"}},
        continuation=continuation,
        tool_results=[tool_result],
    )

    assert response.text_response == "done"
    assert client.calls[0]["kwargs"]["continuation"] is continuation
    assert client.calls[0]["kwargs"]["tool_results"] == [tool_result]
    assert client.calls[0]["kwargs"]["llm_params"] == {"reasoning_effort": "none"}
    candidate_event = next(
        item for item in aux_log if item.get("type") == "structured_tool_candidates"
    )
    options = candidate_event["structured_call_options"]
    assert options["continuation_present"] is True
    assert options["continuation_api_surface"] == "responses"
    assert options["continuation_output_item_count"] == 1
    assert options["tool_result_count"] == 1
    persisted = json.dumps(options, sort_keys=True)
    assert "encrypted-provider-state-must-not-persist" not in persisted
    assert "tool-result-body-must-not-persist" not in persisted
    assert "call-sensitive" not in persisted
    transport_event = next(
        item
        for item in aux_log
        if item.get("type") == "structured_tool_transport_decision"
    )
    assert transport_event["llm_exchange_id"]
    assert transport_event["call_id"]


def test_run_llm_with_fallbacks_emits_stable_live_llm_exchange_identity(
    monkeypatch,
) -> None:
    # JVNAUTOSCI-2517: the live Thinking card groups a prepared/sent/received/
    # completed lifecycle (and every fallback attempt) by a stable exchange id
    # plus per-attempt call id. Prove the orchestrator actually emits them.
    orchestrator = _bare_orchestrator()
    monkeypatch.setenv("VON_LLM_REQUEST_PREPARATION_PROGRESS_THRESHOLD_MS", "0")
    ollama_candidate = _ModelCandidate(
        provider="ollama",
        model="granite3.3:2b",
        raw="ollama:granite3.3:2b",
        source="policy",
        host="http://localhost:11434",
    )
    openai_candidate = _ModelCandidate(
        provider="openai",
        model="gpt-5.2-chat-latest",
        raw="openai:gpt-5.2-chat-latest",
        source="policy",
    )
    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [ollama_candidate, openai_candidate],
    )

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **_kwargs: Any,
    ) -> tuple[Any, str, Mapping[str, Any]]:
        if candidate.provider == "ollama":
            return (
                _FailingClient(),
                "granite3.3:2b",
                {"provider": "ollama", "model": "granite3.3:2b", "host": None},
            )
        return (
            _SuccessfulClient(),
            "gpt-5.2-chat-latest",
            {"provider": "openai", "model": "gpt-5.2-chat-latest", "host": None},
        )

    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        _create_client_for_candidate,
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )

    progress_events: list[dict[str, Any]] = []
    orchestrator._run_llm_with_fallbacks(
        stage="buttonify",
        prompt="Return quick-reply options",
        context=[],
        default_client=object(),
        default_model="gpt-5.2-chat-latest",
        policy_state=_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=[],
        record_llm_call=lambda **_payload: None,
        emit_progress=lambda payload: progress_events.append(dict(payload)),
        workflow_stage_id="tool_plan",
    )

    prepared = next(
        e for e in progress_events if e.get("status") == "llm_request_prepared"
    )
    preparation_steps = [
        e for e in progress_events if e.get("status") == "llm_request_preparation_step"
    ]
    assert {event.get("request_preparation_step") for event in preparation_steps} == {
        "model_candidate_resolution",
        "request_telemetry_build",
    }
    assert all(
        event.get("workflow_stage_id") == "tool_plan" for event in preparation_steps
    )

    exchange_id = prepared["llm_exchange_id"]
    assert exchange_id.startswith("llm-")
    assert prepared["call_id"] == f"{exchange_id}:attempt:1"

    # Every live LLM lifecycle event shares the one exchange id, and each
    # attempt's call id is derived from its fallback attempt number.
    lifecycle = [e for e in progress_events if e.get("llm_exchange_id")]
    assert len(lifecycle) >= 3
    assert {e["llm_exchange_id"] for e in lifecycle} == {exchange_id}
    for event in lifecycle:
        attempt_no = event.get("fallback_attempt_no")
        if isinstance(attempt_no, int) and attempt_no > 0:
            assert event["call_id"] == f"{exchange_id}:attempt:{attempt_no}"

    # The successful second attempt is reported under attempt-2's call id.
    completed = next(
        e
        for e in progress_events
        if e.get("status") == "llm_call_end" and e.get("success") is True
    )
    assert completed["call_id"] == f"{exchange_id}:attempt:2"


def test_run_llm_with_fallbacks_marks_policy_primary_active_llm_selection(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )

    llm_calls_log: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []

    response, model_name, telemetry = orchestrator._run_llm_with_fallbacks(
        stage="workflow_dispatch",
        policy_stage="classifier",
        prompt="Select workflow",
        context=[],
        default_client=_SuccessfulOllamaClient(default_model="gemma4:26b"),
        default_model="gpt-5.4-mini",
        policy_state=_active_llm_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=llm_calls_log,
        aux_log=aux_log,
        record_llm_call=lambda **_payload: None,
        emit_progress=None,
    )

    assert response == '["Proceed", "Hold"]'
    assert model_name == "gpt-5.4-mini"
    assert telemetry.get("source") == "active_llm"

    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["policy_stage"] == "classifier"
    assert stage_summary["requested_model"] == "gpt-5.4-mini"
    assert stage_summary["selection_mode"] == "policy_primary_active_llm"
    assert stage_summary["follows_active_llm"] is True
    assert stage_summary["explicit_stage_model_override"] is False


def test_run_llm_with_fallbacks_records_client_default_when_active_llm_is_null(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    default_client = _StubOllamaClient(default_model="llama3.2:latest")
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )

    progress_events: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []

    response, model_name, telemetry = orchestrator._run_llm_with_fallbacks(
        stage="workflow_dispatch",
        policy_stage="classifier",
        prompt="Select workflow",
        context=[],
        default_client=default_client,
        default_model=None,
        policy_state=_active_llm_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **payload: recorded_calls.append(dict(payload)),
        emit_progress=lambda payload: progress_events.append(dict(payload)),
    )

    assert response == "resolved client default"
    assert model_name == "llama3.2:latest"
    assert default_client.calls == [{"context": [], "model": "llama3.2:latest"}]
    assert telemetry["provider"] == "ollama"
    assert telemetry["model"] == "llama3.2:latest"
    assert telemetry["model_resolution_source"] == "client_default"

    start_event = next(
        event for event in progress_events if event.get("status") == "llm_call_start"
    )
    assert start_event["model"] == "llama3.2:latest"
    assert start_event["candidate"]["provider"] == "ollama"
    assert start_event["candidate"]["model"] == "llama3.2:latest"

    assert recorded_calls[0]["model_name"] == "llama3.2:latest"
    assert recorded_calls[0]["provider"] == "ollama"

    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["selection_mode"] == "policy_primary_active_llm"
    assert stage_summary["selected"]["model_resolved"] == "llama3.2:latest"
    assert stage_summary["selected"]["model"] == "llama3.2:latest"
    assert stage_summary["selected"]["model_resolution_source"] == "client_default"


def test_run_llm_with_fallbacks_ignores_browser_object_active_model(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    default_client = _StubOllamaClient(default_model="llama3.2:latest")
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )

    progress_events: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []

    response, model_name, telemetry = orchestrator._run_llm_with_fallbacks(
        stage="workflow_dispatch",
        policy_stage="classifier",
        prompt="Select workflow",
        context=[],
        default_client=default_client,
        default_model="[object PointerEvent]",
        policy_state=_active_llm_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **_payload: None,
        emit_progress=lambda payload: progress_events.append(dict(payload)),
    )

    assert response == "resolved client default"
    assert model_name == "llama3.2:latest"
    assert default_client.calls == [{"context": [], "model": "llama3.2:latest"}]
    assert telemetry["model"] == "llama3.2:latest"
    assert all(
        event.get("model") != "[object PointerEvent]" for event in progress_events
    )

    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["requested_model"] is None
    assert stage_summary["selected"]["model"] == "llama3.2:latest"


def test_raw_ollama_tag_policy_candidate_keeps_whole_model_name() -> None:
    candidate = InternalMCPChatOrchestrator._parse_policy_model_candidate("qwen3:8b")

    assert candidate is not None
    assert candidate.provider == "ollama"
    assert candidate.model == "qwen3:8b"
    assert candidate.raw == "qwen3:8b"


def test_active_llm_local_model_switches_from_openai_client_to_ollama(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    default_client = _StubOpenAIClient()
    ollama_client = _StubOllamaClient(default_model="qwen3:8b")
    requested_clients: list[str | None] = []

    def _get_llm_client(*, client_type: str | None = None, **_kwargs: Any) -> Any:
        requested_clients.append(client_type)
        assert client_type == "ollama"
        return ollama_client

    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_llm_client",
        _get_llm_client,
    )

    client, model_name, telemetry = orchestrator._create_client_for_candidate(
        _ModelCandidate(
            provider=None,
            model="qwen3:8b",
            raw="active_llm",
            source="active_llm",
            host=None,
        ),
        default_client=default_client,
        default_model="qwen3:8b",
        user_concept_id="#V#user",
        org_concept_id="#V#org",
    )

    assert client is ollama_client
    assert requested_clients == ["ollama"]
    assert model_name == "qwen3:8b"
    assert telemetry["provider"] == "ollama"
    assert telemetry["model"] == "qwen3:8b"
    assert telemetry["host"] == "http://localhost:11434"


def test_enabled_ollama_candidate_preserves_its_scoped_host(monkeypatch) -> None:
    orchestrator = _bare_orchestrator()
    ollama_client = _StubOllamaClient(default_model="gemma4:latest")
    ollama_client.host = "http://127.0.0.1:11434"
    requested_clients: list[dict[str, Any]] = []

    def _get_llm_client(**kwargs: Any) -> Any:
        requested_clients.append(dict(kwargs))
        return ollama_client

    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_llm_client",
        _get_llm_client,
    )

    client, model_name, telemetry = orchestrator._create_client_for_candidate(
        _ModelCandidate(
            provider="ollama",
            model="gemma4:latest",
            raw="ollama:gemma4:latest",
            source="enabled_settings",
            host="http://127.0.0.1:11434",
        ),
        default_client=object(),
        default_model="gpt-5.6-luna",
        user_concept_id="#V#user",
        org_concept_id="#V#org",
    )

    assert client is ollama_client
    assert requested_clients == [
        {
            "client_type": "ollama",
            "user_concept_id": "#V#user",
            "org_concept_id": "#V#org",
            "host": "http://127.0.0.1:11434",
        }
    ]
    assert model_name == "gemma4:latest"
    assert telemetry["provider"] == "ollama"
    assert telemetry["host"] == "http://127.0.0.1:11434"


def test_provider_diagnostics_treat_raw_ollama_tags_as_ollama() -> None:
    assert (
        InternalMCPChatOrchestrator._infer_provider_from_model_reference("gemma4:26b")
        == "ollama"
    )
    assert (
        InternalMCPChatOrchestrator._infer_provider_from_model_reference(
            "ollama:llama3.2:latest"
        )
        == "ollama"
    )
    assert (
        InternalMCPChatOrchestrator._infer_provider_from_model_reference(
            "openai:gpt-5.4-mini"
        )
        == "openai"
    )


def test_run_llm_with_fallbacks_marks_explicit_policy_stage_override(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    override_candidate = _ModelCandidate(
        provider="openai",
        model="gpt-4o-mini",
        raw="openai:gpt-4o-mini",
        source="policy",
    )
    policy_state = _WorkflowModelPolicyState(
        enabled=True,
        policy={
            "stages": {
                "classifier": {
                    "primary": "openai:gpt-4o-mini",
                    "fallback": [],
                }
            }
        },
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )
    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [override_candidate],
    )
    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        lambda candidate, **_kwargs: (
            _SuccessfulClient(),
            "gpt-4o-mini",
            {
                "provider": candidate.provider,
                "model": candidate.model,
                "raw": candidate.raw,
                "source": candidate.source,
                "host": candidate.host,
            },
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )

    aux_log: list[Mapping[str, Any]] = []
    response, model_name, telemetry = orchestrator._run_llm_with_fallbacks(
        stage="workflow_dispatch",
        policy_stage="classifier",
        prompt="Select workflow",
        context=[],
        default_client=object(),
        default_model="gpt-5.4-mini",
        policy_state=policy_state,
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **_payload: None,
        emit_progress=None,
    )

    assert response == '["Proceed", "Hold"]'
    assert model_name == "gpt-4o-mini"
    assert telemetry.get("source") == "policy"

    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["policy_stage"] == "classifier"
    assert stage_summary["requested_model"] == "gpt-5.4-mini"
    assert stage_summary["selection_mode"] == "policy_primary_override"
    assert stage_summary["follows_active_llm"] is False
    assert stage_summary["explicit_stage_model_override"] is True
    assert stage_summary["explicit_stage_model_override_origin"] == "policy_primary"


def test_run_llm_with_fallbacks_prefers_default_model_when_requested(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    policy_state = _WorkflowModelPolicyState(
        enabled=True,
        policy={
            "stages": {
                "classifier": {
                    "primary": "openai:gpt-4o-mini",
                    "fallback": [],
                }
            }
        },
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_invoke_with_llm_heartbeat",
        lambda *, call, **_kwargs: call(),
    )

    aux_log: list[Mapping[str, Any]] = []
    response, model_name, telemetry = orchestrator._run_llm_with_fallbacks(
        stage="workflow_dispatch",
        policy_stage="classifier",
        prompt="Select workflow",
        context=[],
        default_client=_SuccessfulOllamaClient(default_model="gemma4:26b"),
        default_model="gemma4:26b",
        policy_state=policy_state,
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=lambda **_payload: None,
        emit_progress=None,
        prefer_default_model=True,
    )

    assert response == '["Proceed", "Hold"]'
    assert model_name == "gemma4:26b"
    assert telemetry.get("source") == "active_llm"

    stage_summary = next(
        entry for entry in aux_log if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["requested_model"] == "gemma4:26b"
    assert stage_summary["selection_mode"] == "preferred_default_model"
    assert stage_summary["prefer_default_model"] is True
    assert stage_summary["follows_active_llm"] is True


def test_stage_model_candidates_try_requested_default_before_policy_fallbacks(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    policy_state = _WorkflowModelPolicyState(
        enabled=True,
        policy={
            "stages": {
                "classifier": {
                    "primary": "openai:gpt-4o-mini",
                    "fallback": ["ollama:granite3.3:2b"],
                }
            }
        },
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_enabled_llm_settings",
        lambda **_kwargs: [{"provider": "openai", "model": "gpt-5.4-mini"}],
    )

    candidates = orchestrator._stage_model_candidates(
        stage="classifier",
        default_model="gemma4:26b",
        policy_state=policy_state,
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        prefer_default_model=True,
    )

    assert [
        (candidate.source, candidate.provider, candidate.model)
        for candidate in candidates
    ] == [
        ("active_llm", None, "gemma4:26b"),
        ("policy", "openai", "gpt-4o-mini"),
        ("policy", "ollama", "granite3.3:2b"),
        ("enabled_settings", "openai", "gpt-5.4-mini"),
    ]


def test_stage_model_candidates_uses_default_client_provider_for_parameters(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_enabled_llm_settings",
        lambda **_kwargs: [],
    )

    candidates = orchestrator._stage_model_candidates(
        stage="tool_call",
        default_model="gpt-5.6-luna",
        default_model_parameters={"reasoning_effort": "none"},
        default_provider="openai",
        policy_state=_WorkflowModelPolicyState(
            enabled=False,
            policy=None,
            policy_id=None,
            predicate_id=None,
            errors=(),
        ),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        prefer_default_model=True,
    )

    assert len(candidates) == 1
    assert candidates[0].source == "active_llm"
    assert candidates[0].model == "gpt-5.6-luna"
    assert candidates[0].model_parameters == {"reasoning_effort": "none"}


def test_stage_model_candidates_dedupe_active_and_enabled_before_policy_fallback(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    policy_state = _WorkflowModelPolicyState(
        enabled=True,
        policy={
            "stages": {
                "mail_review_response_rendering": {
                    "primary": "active_llm",
                    "fallback": ["ollama:granite3.3:2b"],
                }
            }
        },
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )
    monkeypatch.setattr(
        "src.backend.services.settings_service.resolve_enabled_llm_settings",
        lambda **_kwargs: [
            {
                "provider": "ollama",
                "model": "qwen3:8b",
                "host": "http://127.0.0.1:11434",
            }
        ],
    )

    candidates = orchestrator._stage_model_candidates(
        stage="mail_review_response_rendering",
        default_model="qwen3:8b",
        policy_state=policy_state,
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        prefer_default_model=True,
    )

    assert [
        (candidate.source, candidate.provider, candidate.model)
        for candidate in candidates
    ] == [
        ("active_llm", None, "qwen3:8b"),
        ("policy", "ollama", "granite3.3:2b"),
    ]


def test_probe_model_candidate_reachability_uses_failure_cooldown_cache(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    orchestrator._provider_probe_cooldown_seconds = 120

    request_calls = {"count": 0}

    class _FailingResponse:
        def raise_for_status(self) -> None:
            raise RuntimeError("connection refused")

    def _fake_get(*_args: Any, **_kwargs: Any) -> _FailingResponse:
        request_calls["count"] += 1
        time.sleep(0.02)
        return _FailingResponse()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.requests.get",
        _fake_get,
    )

    telemetry = {
        "provider": "ollama",
        "host": "http://localhost:11434",
        "model": "granite3.3:2b",
    }
    first = orchestrator._probe_model_candidate_reachability(telemetry=telemetry)
    second = orchestrator._probe_model_candidate_reachability(telemetry=telemetry)

    assert isinstance(first, Mapping)
    assert first.get("reachable") is False
    assert first.get("cooldown_hit") is not True

    assert isinstance(second, Mapping)
    assert second.get("reachable") is False
    assert second.get("cooldown_hit") is True
    assert second.get("cached") is True
    assert second.get("cached_result_duration_ms") == first.get("duration_ms")
    assert isinstance(first.get("duration_ms"), int)
    assert isinstance(second.get("duration_ms"), int)
    assert second["duration_ms"] < first["duration_ms"]
    assert request_calls["count"] == 1


def test_probe_model_candidate_reachability_uses_shorter_timeout_for_local_ollama(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    seen_timeouts: dict[str, float] = {}

    class _SuccessfulResponse:
        def raise_for_status(self) -> None:
            return None

    def _fake_get(url: str, *_args: Any, **kwargs: Any) -> _SuccessfulResponse:
        seen_timeouts[url] = float(kwargs["timeout"])
        return _SuccessfulResponse()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.requests.get",
        _fake_get,
    )

    local_probe = orchestrator._probe_model_candidate_reachability(
        telemetry={"provider": "ollama", "host": "http://localhost:11434"}
    )
    remote_probe = orchestrator._probe_model_candidate_reachability(
        telemetry={"provider": "ollama", "host": "http://10.0.0.8:11434"}
    )

    assert local_probe is not None
    assert local_probe["provider"] == "ollama"
    assert local_probe["host"] == "http://localhost:11434"
    assert local_probe["probe_url"] == "http://localhost:11434/api/tags"
    assert local_probe["probe_timeout_ms"] == 1000
    assert local_probe["reachable"] is True
    assert isinstance(local_probe["duration_ms"], int)

    assert remote_probe is not None
    assert remote_probe["provider"] == "ollama"
    assert remote_probe["host"] == "http://10.0.0.8:11434"
    assert remote_probe["probe_url"] == "http://10.0.0.8:11434/api/tags"
    assert remote_probe["probe_timeout_ms"] == 1200
    assert remote_probe["reachable"] is True
    assert isinstance(remote_probe["duration_ms"], int)
    assert seen_timeouts["http://localhost:11434/api/tags"] == pytest.approx(1.0)
    assert seen_timeouts["http://10.0.0.8:11434/api/tags"] == pytest.approx(1.2)


def test_probe_timeout_is_inconclusive_and_not_cached(monkeypatch) -> None:
    """JVNAUTOSCI-2505: a probe timeout means busy-or-slow, not unreachable.

    The candidate must proceed to a real attempt, and the negative result
    must not be cooldown-cached or treated as provider_unreachable.
    """

    orchestrator = _bare_orchestrator()
    orchestrator._provider_probe_cooldown_seconds = 120

    request_calls = {"count": 0}

    import requests

    def _fake_get(*_args: Any, **_kwargs: Any):
        request_calls["count"] += 1
        raise requests.exceptions.ReadTimeout("probe timed out")

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.requests.get",
        _fake_get,
    )

    telemetry = {
        "provider": "ollama",
        "host": "http://localhost:11434",
        "model": "qwen3:8b",
    }
    first = orchestrator._probe_model_candidate_reachability(telemetry=telemetry)
    second = orchestrator._probe_model_candidate_reachability(telemetry=telemetry)

    assert isinstance(first, Mapping)
    assert first.get("reachable") is None
    assert first.get("inconclusive") is True
    assert first.get("reason") == "probe_timeout_busy_or_slow"

    # No cooldown cache write: the second probe really probes again.
    assert isinstance(second, Mapping)
    assert second.get("cooldown_hit") is not True
    assert request_calls["count"] == 2


def test_all_candidates_probe_failed_raises_named_failure_summary(
    monkeypatch,
) -> None:
    """JVNAUTOSCI-2505: when every candidate fails (e.g. probe refusal), the
    stage error must name the candidates and failure kinds instead of the
    misleading generic 'No model candidates available for stage'."""

    orchestrator = _bare_orchestrator()

    def _probe_refused(**_kwargs: Any) -> Mapping[str, Any]:
        return {
            "provider": "ollama",
            "host": "http://localhost:11434",
            "reachable": False,
            "error": "connection refused",
            "error_class": "ConnectionError",
            "duration_ms": 3,
        }

    monkeypatch.setattr(
        type(orchestrator),
        "_probe_model_candidate_reachability",
        lambda self, *, telemetry: _probe_refused(),
    )

    with pytest.raises(RuntimeError) as excinfo:
        orchestrator._run_llm_with_fallbacks(
            stage="expected_outcome_inference",
            prompt="probe failure path",
            context=None,
            default_client=None,
            default_model="qwen3:8b",
            policy_state=_WorkflowModelPolicyState(
                enabled=False,
                policy=None,
                policy_id=None,
                predicate_id=None,
                errors=(),
            ),
            registry_snapshot=None,
            user_concept_id=None,
            org_concept_id=None,
            llm_calls_log=[],
            aux_log=[],
            record_llm_call=lambda **_kwargs: None,
            prefer_default_model=True,
        )

    message = str(excinfo.value)
    assert message.startswith("all_model_candidates_failed:stage=")
    assert "provider_unreachable" in message
    assert "qwen3:8b" in message


def test_invoke_with_llm_heartbeat_uses_backfill_advisory_for_summariser(
    monkeypatch,
) -> None:
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    monkeypatch.setenv("VON_LLM_HEARTBEAT_INTERVAL_SEC", "1")
    monkeypatch.setenv("VON_SCREEN_BACKFILL_LLM_TIMEOUT_SEC", "1")
    monkeypatch.delenv("VON_LLM_CALL_TIMEOUT_SEC", raising=False)

    progress_events: list[dict[str, Any]] = []

    def _slow_call() -> str:
        time.sleep(2.0)
        return "done"

    result = orchestrator._invoke_with_llm_heartbeat(
        call=_slow_call,
        stage_name="summariser",
        model_name="gpt-test",
        emit_progress=lambda payload: progress_events.append(dict(payload)),
    )

    assert result == "done"
    assert any(
        event.get("status") == "llm_call_advisory_exceeded"
        and event.get("stage") == "summariser"
        for event in progress_events
    )


def test_invoke_with_llm_heartbeat_preserves_actor_context() -> None:
    from src.backend.security.access_control import (
        get_effective_organisation_concept_id,
        get_effective_user_concept_id,
        override_current_actor,
    )

    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    with override_current_actor("#V#current_user", "#V#current_org"):
        result = orchestrator._invoke_with_llm_heartbeat(
            call=lambda: (
                get_effective_user_concept_id(),
                get_effective_organisation_concept_id(),
            ),
            stage_name="plain_response",
            model_name="gpt-test",
            emit_progress=lambda _payload: None,
        )

    assert result == ("#V#current_user", "#V#current_org")


def test_invoke_with_llm_heartbeat_treats_explicit_override_as_advisory(
    monkeypatch,
) -> None:
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    monkeypatch.setenv("VON_LLM_HEARTBEAT_INTERVAL_SEC", "1")
    monkeypatch.delenv("VON_LLM_CALL_TIMEOUT_SEC", raising=False)

    progress_events: list[dict[str, Any]] = []

    def _slow_call() -> str:
        time.sleep(2.0)
        return "done"

    result = orchestrator._invoke_with_llm_heartbeat(
        call=_slow_call,
        stage_name="classifier",
        model_name="gemma4:26b",
        emit_progress=lambda payload: progress_events.append(dict(payload)),
        timeout_override_sec=1.0,
    )

    assert result == "done"
    assert any(
        event.get("status") == "llm_call_advisory_exceeded"
        and event.get("stage") == "classifier"
        for event in progress_events
    )


def test_invoke_with_llm_heartbeat_checks_cancellation(monkeypatch) -> None:
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    monkeypatch.setenv("VON_LLM_HEARTBEAT_INTERVAL_SEC", "1")
    monkeypatch.delenv("VON_LLM_CALL_TIMEOUT_SEC", raising=False)

    progress_events: list[dict[str, Any]] = []

    def _slow_call() -> str:
        time.sleep(2.0)
        return "done"

    def _cancel() -> None:
        raise CancellationRequested(task_id="task-123")

    with pytest.raises(CancellationRequested) as exc_info:
        orchestrator._invoke_with_llm_heartbeat(
            call=_slow_call,
            stage_name="plain_response",
            model_name="gemma4:31b",
            emit_progress=lambda payload: progress_events.append(dict(payload)),
            check_cancellation=_cancel,
        )

    assert exc_info.value.task_id == "task-123"
    assert progress_events, "expected heartbeat progress before cancellation"
    assert progress_events[-1]["status"] == "heartbeat"
    assert progress_events[-1]["stage"] == "plain_response"
    assert progress_events[-1]["liveness_reason"] == "llm_call_pending"


def test_run_llm_with_fallbacks_stops_fallback_chain_on_cancellation(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    candidates = [
        _ModelCandidate(
            provider="ollama",
            model="gemma4:31b",
            raw="ollama:gemma4:31b",
            source="active_llm",
            host="http://localhost:11434",
        ),
        _ModelCandidate(
            provider="openai",
            model="gpt-5.4-mini",
            raw="openai:gpt-5.4-mini",
            source="settings_fallback",
        ),
    ]
    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: candidates,
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda **_kwargs: None,
    )

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **_kwargs: Any,
    ) -> tuple[Any, str, Mapping[str, Any]]:
        return (
            _SuccessfulClient(),
            candidate.model or "",
            {
                "provider": candidate.provider,
                "model": candidate.model,
                "raw": candidate.raw,
                "source": candidate.source,
                "host": candidate.host,
            },
        )

    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        _create_client_for_candidate,
    )
    attempted_models: list[str | None] = []

    def _cancel_llm_call(**kwargs: Any) -> str:
        attempted_models.append(kwargs.get("model_name"))
        raise CancellationRequested(task_id="task-123")

    monkeypatch.setattr(orchestrator, "_invoke_with_llm_heartbeat", _cancel_llm_call)

    progress_events: list[dict[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []

    with pytest.raises(CancellationRequested):
        orchestrator._run_llm_with_fallbacks(
            stage="plain_response",
            policy_stage="planner",
            prompt="Answer directly.",
            context=[],
            default_client=object(),
            default_model="gemma4:31b",
            policy_state=_active_llm_policy_state(),
            registry_snapshot=None,
            user_concept_id=None,
            org_concept_id=None,
            llm_calls_log=[],
            aux_log=[],
            record_llm_call=lambda **payload: recorded_calls.append(dict(payload)),
            emit_progress=lambda payload: progress_events.append(dict(payload)),
            turn_model_failures={},
        )

    assert attempted_models == ["gemma4:31b"]
    cancelled_event = next(
        event for event in progress_events if event.get("failure_kind") == "cancelled"
    )
    assert cancelled_event["status"] == "llm_call_end"
    assert cancelled_event["success"] is False
    assert recorded_calls[-1]["note"] == "llm.generate cancelled"


def test_limit_context_preserves_leading_system_messages() -> None:
    orchestrator = _bare_orchestrator()

    messages: list[Mapping[str, Any]] = [
        {"role": "system", "content": "s" * 2000},
        {"role": "system", "content": "t" * 1800},
        {"role": "user", "content": "u" * 1200},
        {"role": "assistant", "content": "a" * 900},
    ]

    limited = orchestrator._limit_context_for_llm(messages, max_chars=4000)

    assert [msg["role"] for msg in limited] == ["system", "system", "assistant"]
    assert limited[0]["content"] == "s" * 2000
    assert limited[1]["content"] == "t" * 1800
    assert limited[-1]["content"] == "a" * 900


def test_build_follow_up_llm_context_keeps_recent_relevant_messages() -> None:
    orchestrator = _bare_orchestrator()

    messages = [
        {"role": "system", "content": "system-one"},
        {"role": "system", "content": "system-two"},
        {"role": "user", "content": "old-user"},
        {"role": "assistant", "content": "old-assistant"},
        {"role": "tool", "content": "old-tool"},
        {"role": "user", "content": "new-user"},
        {"role": "assistant", "content": "new-assistant"},
        {"role": "tool", "content": "recent-tool-1"},
        {"role": "tool", "content": "recent-tool-2"},
    ]

    compacted = orchestrator._build_follow_up_llm_context(
        messages,
        max_chars=200,
        keep_recent_tool_messages=2,
        keep_recent_assistant_messages=1,
        keep_recent_user_messages=1,
    )

    assert compacted[0]["content"] == "system-one"
    assert compacted[1]["content"] == "system-two"
    assert compacted[2]["role"] == "system"
    assert "compacted" in compacted[2]["content"]
    assert [msg["content"] for msg in compacted[3:]] == [
        "new-user",
        "new-assistant",
        "recent-tool-1",
        "recent-tool-2",
    ]


def test_tool_calling_backfill_uses_compacted_follow_up_context(monkeypatch) -> None:
    orchestrator = _bare_orchestrator()
    orchestrator._follow_up_context_chars = 200

    captured: dict[str, Any] = {}

    def _run_llm_with_fallbacks(**kwargs: Any) -> tuple[str, str, Mapping[str, Any]]:
        captured["context"] = kwargs.get("context")
        return "Final answer", "gpt-test", {}

    monkeypatch.setattr(
        orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks
    )
    monkeypatch.setattr(
        orchestrator,
        "_interpret_model_turn",
        lambda _text: SimpleNamespace(tool_calls=[], tool_call_parse_error=None),
    )
    monkeypatch.setattr(
        orchestrator,
        "_evaluate_prompt_requirements",
        lambda **_kwargs: SimpleNamespace(
            required_tools=[],
            required_fetch_concept_ids=[],
            required_read_file_copy_ids=[],
            required_scholarly_representation_file_copy_ids=[],
            required_create_type_name=None,
            missing_tools=[],
            missing_fetch_concept_ids=[],
            missing_read_file_copy_ids=[],
            missing_scholarly_representation_file_copy_ids=[],
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_augment_prompt_requirements_with_turn_contract",
        lambda **_kwargs: _kwargs["evaluation"],
    )
    monkeypatch.setattr(
        orchestrator,
        "_store_prompt_requirement_evaluation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator, "_run_missing_tool_call_recovery_workflow", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        orchestrator,
        "_sanitise_user_visible_action_output",
        lambda text, **_kwargs: text,
    )
    monkeypatch.setattr(
        orchestrator, "_resolve_environment_max_tool_invocations", lambda _env: 4
    )
    monkeypatch.setattr(
        orchestrator,
        "_assess_missing_tool_call",
        lambda **_kwargs: SimpleNamespace(retry_reason=None),
    )

    augmented_context = [
        {"role": "system", "content": "system-one"},
        {"role": "system", "content": "system-two"},
        {"role": "user", "content": "old-user-1"},
        {"role": "assistant", "content": "old-assistant-1"},
        {"role": "tool", "content": "old-tool-1"},
        {"role": "user", "content": "old-user-2"},
        {"role": "assistant", "content": "old-assistant-2"},
        {"role": "tool", "content": "old-tool-2"},
        {"role": "user", "content": "new-user"},
        {"role": "assistant", "content": "new-assistant"},
        {"role": "tool", "content": "recent-tool-0"},
        {"role": "tool", "content": "recent-tool-1"},
        {"role": "tool", "content": "recent-tool-2"},
        {"role": "tool", "content": "recent-tool-3"},
        {"role": "tool", "content": "recent-tool-4"},
        {"role": "tool", "content": "recent-tool-5"},
        {"role": "tool", "content": "recent-tool-6"},
    ]

    request = SimpleNamespace(
        data={
            "augmented_context": augmented_context,
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": None,
            "org_concept_id": None,
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 0,
            "remaining_tool_calls": [],
            "invocations": [],
            "method_catalogue": {},
            "prompt": "Run the follow-up step",
            "prompt_for_requirements": "Run the follow-up step",
            "emit_progress": None,
        },
        environment=SimpleNamespace(llm_client=object(), model="gpt-test"),
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.outputs["final_response"] == "Final answer"
    compacted = cast(list[Mapping[str, Any]], captured["context"])
    assert compacted[0]["content"] == "system-one"
    assert compacted[1]["content"] == "system-two"
    assert compacted[2]["role"] == "system"
    assert [msg["content"] for msg in compacted[3:]] == [
        "old-tool-1",
        "old-user-2",
        "old-assistant-2",
        "old-tool-2",
        "new-user",
        "new-assistant",
        "recent-tool-0",
        "recent-tool-1",
        "recent-tool-2",
        "recent-tool-3",
        "recent-tool-4",
        "recent-tool-5",
        "recent-tool-6",
    ]


def test_tool_calling_backfill_rejects_malformed_stored_continuation(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()

    monkeypatch.setattr(
        orchestrator,
        "_run_llm_with_fallbacks",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("ordinary summarisation must not run")
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_evaluate_prompt_requirements",
        lambda **_kwargs: SimpleNamespace(
            required_tools=[],
            required_fetch_concept_ids=[],
            required_read_file_copy_ids=[],
            required_scholarly_representation_file_copy_ids=[],
            required_create_type_name=None,
            missing_tools=[],
            missing_fetch_concept_ids=[],
            missing_read_file_copy_ids=[],
            missing_scholarly_representation_file_copy_ids=[],
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_augment_prompt_requirements_with_turn_contract",
        lambda **_kwargs: _kwargs["evaluation"],
    )
    monkeypatch.setattr(
        orchestrator,
        "_store_prompt_requirement_evaluation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator, "_run_missing_tool_call_recovery_workflow", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        orchestrator, "_resolve_environment_max_tool_invocations", lambda _env: 4
    )
    monkeypatch.setattr(
        orchestrator,
        "_assess_missing_tool_call",
        lambda **_kwargs: SimpleNamespace(retry_reason=None),
    )
    request = SimpleNamespace(
        data={
            "augmented_context": [],
            "structured_tool_continuation": {"provider": "openai"},
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": None,
            "org_concept_id": None,
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 0,
            "remaining_tool_calls": [],
            "invocations": [],
            "method_catalogue": {},
            "prompt": "Run the follow-up step",
            "prompt_for_requirements": "Run the follow-up step",
            "emit_progress": None,
        },
        environment=SimpleNamespace(llm_client=object(), model="gpt-test"),
    )

    with pytest.raises(
        StructuredToolProtocolError,
        match="Stored structured continuation payload is malformed",
    ):
        orchestrator._action_tool_calling_backfill(request)


def test_tool_call_repair_preserves_provider_call_id_for_continuation(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_attempt_tool_call_repair",
        lambda **_kwargs: [
            {
                "action": "call",
                "tool": "synthetic_lookup",
                "payload": {"query": "repaired"},
            }
        ],
    )
    request = SimpleNamespace(
        data={
            "tool_calls": [
                {
                    "action": "call",
                    "tool": "synthetic_lookup",
                    "payload": {"query": 123},
                    "_call_id": "call-provider-1",
                }
            ],
            "structured_tool_continuation": {
                "provider": "openai",
                "api_surface": "responses",
                "model": "synthetic-model",
            },
            "tool_call_validation_errors": ["query must be a string"],
            "method_catalogue": {
                "synthetic_lookup": {"category": "read"},
            },
            "tool_call_repair_attempts": 0,
            "tool_call_repair_budget": 1,
            "policy_state": _policy_state(),
            "registry_snapshot": None,
            "model_for_stage": lambda _stage: "synthetic-model",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "user_concept_id": "#V#user",
            "org_concept_id": "#V#org",
            "tool_call_model": "synthetic-model",
        },
        environment=SimpleNamespace(llm_client=object()),
    )

    result = orchestrator._action_tool_calling_repair(request)

    assert result.ok
    assert result.outputs["tool_call_repair_succeeded"] is True
    assert result.outputs["tool_calls"] == [
        {
            "action": "call",
            "tool": "synthetic_lookup",
            "payload": {"query": "repaired"},
            "_call_id": "call-provider-1",
        }
    ]


def test_tool_call_repair_rejects_reordered_provider_calls_before_execution(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_attempt_tool_call_repair",
        lambda **_kwargs: [
            {
                "action": "call",
                "tool": "synthetic_second",
                "payload": {"query": "repaired-second"},
            },
            {
                "action": "call",
                "tool": "synthetic_first",
                "payload": {"query": "repaired-first"},
            },
        ],
    )
    request = SimpleNamespace(
        data={
            "tool_calls": [
                {
                    "action": "call",
                    "tool": "synthetic_first",
                    "payload": {"query": 1},
                    "_call_id": "call-provider-first",
                },
                {
                    "action": "call",
                    "tool": "synthetic_second",
                    "payload": {"query": 2},
                    "_call_id": "call-provider-second",
                },
            ],
            "structured_tool_continuation": {
                "provider": "openai",
                "api_surface": "responses",
                "model": "synthetic-model",
            },
            "tool_call_validation_errors": ["query must be a string"],
            "method_catalogue": {
                "synthetic_first": {"category": "read"},
                "synthetic_second": {"category": "read"},
            },
            "tool_call_repair_attempts": 0,
            "tool_call_repair_budget": 1,
            "policy_state": _policy_state(),
            "registry_snapshot": None,
            "model_for_stage": lambda _stage: "synthetic-model",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "user_concept_id": "#V#user",
            "org_concept_id": "#V#org",
            "tool_call_model": "synthetic-model",
        },
        environment=SimpleNamespace(llm_client=object()),
    )

    result = orchestrator._action_tool_calling_repair(request)

    assert not result.ok
    assert result.error == "structured_tool_call_repair_correlation_failed"
    assert result.outputs["tool_call_repair_outcome"] == "correlation_failed"
    assert result.outputs["tool_call_repair_succeeded"] is False
    assert "tool_calls" not in result.outputs


def test_tool_calling_backfill_prompt_preserves_structured_workflow_contract(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    captured: dict[str, Any] = {}

    def _render_authoritative_prompt(
        prompt_ids,
        *,
        variables,
        max_chars,
        error_context,
        required=False,
    ):
        captured.update(
            {
                "prompt_ids": prompt_ids,
                "variables": variables,
                "max_chars": max_chars,
                "error_context": error_context,
                "required": required,
            }
        )
        return SimpleNamespace(
            prompt_id="#V#workflow_step_structured_output_backfill_prompt",
            text=(
                "represented continuation "
                f"{variables['output_format']} "
                f"{variables['workflow_step_output_contract']} "
                f"{variables['original_authoritative_workflow_step_instructions']}"
            ),
        )

    monkeypatch.setattr(
        orchestrator,
        "_render_authoritative_prompt",
        _render_authoritative_prompt,
    )
    prompt, preserves_contract, prompt_id = orchestrator._workflow_step_backfill_prompt(
        {
            "prompt": "Resolve the represented artefact and return JSON only.",
            "workflow_step_output_contract": {
                "schema_version": "workflow_step_output_contract.v1",
                "output_format": "json_value",
                "prompt_concept_id": "#V#prompt_represented_plan",
                "workflow_id": "#V#represented_workflow",
                "workflow_state_id": "plan",
                "response_contract_text": (
                    "Return JSON with decision and target_contracts."
                ),
                "required_json_fields": ["decision", "target_contracts"],
                "json_field_defaults": {"target_contracts": []},
            },
        }
    )

    assert preserves_contract is True
    assert prompt_id == "#V#workflow_step_structured_output_backfill_prompt"
    assert captured["prompt_ids"] == (
        "#V#workflow_step_structured_output_backfill_prompt",
    )
    assert captured["required"] is True
    assert captured["error_context"] == "workflow_step_structured_output_backfill"
    assert captured["variables"]["output_format"] == "json_value"
    assert "represented continuation" in prompt
    assert "json_value" in prompt
    assert "#V#prompt_represented_plan" in prompt
    assert "Resolve the represented artefact and return JSON only." in prompt
    assert "Provide a final answer to the user now" not in prompt


def test_tool_calling_backfill_injects_synthesiser_context_prep_messages(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    orchestrator._follow_up_context_chars = 260

    captured: dict[str, Any] = {}

    def _run_llm_with_fallbacks(**kwargs: Any) -> tuple[str, str, Mapping[str, Any]]:
        captured["context"] = kwargs.get("context")
        captured["context_telemetry"] = kwargs.get("context_telemetry")
        return "Final answer", "gpt-test", {}

    monkeypatch.setattr(
        orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks
    )
    monkeypatch.setattr(
        orchestrator,
        "_interpret_model_turn",
        lambda _text: SimpleNamespace(tool_calls=[], tool_call_parse_error=None),
    )
    monkeypatch.setattr(
        orchestrator,
        "_evaluate_prompt_requirements",
        lambda **_kwargs: SimpleNamespace(
            required_tools=[],
            required_fetch_concept_ids=[],
            required_read_file_copy_ids=[],
            required_scholarly_representation_file_copy_ids=[],
            required_create_type_name=None,
            missing_tools=[],
            missing_fetch_concept_ids=[],
            missing_read_file_copy_ids=[],
            missing_scholarly_representation_file_copy_ids=[],
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_augment_prompt_requirements_with_turn_contract",
        lambda **_kwargs: _kwargs["evaluation"],
    )
    monkeypatch.setattr(
        orchestrator,
        "_store_prompt_requirement_evaluation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator, "_run_missing_tool_call_recovery_workflow", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        orchestrator,
        "_sanitise_user_visible_action_output",
        lambda text, **_kwargs: text,
    )
    monkeypatch.setattr(
        orchestrator, "_resolve_environment_max_tool_invocations", lambda _env: 20
    )
    monkeypatch.setattr(
        orchestrator,
        "_assess_missing_tool_call",
        lambda **_kwargs: SimpleNamespace(retry_reason=None),
    )

    augmented_context = [
        {"role": "system", "content": "system-one"},
        {
            "role": "user",
            "content": (
                "Get the message ID for the single most recent email message "
                "received by zhanvonwitbrock@gmail.com"
            ),
        },
        {
            "role": "user",
            "content": "List the last ten email messages received by zhanvonwitbrock@gmail.com",
        },
        {"role": "tool", "content": "gmail_list_messages returned 10 message IDs"},
        {"role": "tool", "content": "gmail_get_message returned details for all 10"},
    ]

    request = SimpleNamespace(
        data={
            "augmented_context": augmented_context,
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": None,
            "org_concept_id": None,
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 11,
            "remaining_tool_calls": [],
            "invocations": [],
            "method_catalogue": {},
            "prompt": "List the last ten email messages received by zhanvonwitbrock@gmail.com",
            "prompt_for_requirements": "List the last ten email messages received by zhanvonwitbrock@gmail.com",
            "synthesiser_system_messages": [
                {
                    "role": "system",
                    "content": "AUTH active request: List the last ten email messages received by zhanvonwitbrock@gmail.com",
                    "source": "vontology_prompt_template",
                    "source_prompt_concept_id": "#V#test_synthesiser_context_framing_prompt",
                    "template_schema": SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA,
                    "template_field": "active_request_template",
                },
                {
                    "role": "system",
                    "content": "AUTH active request: List the last ten email messages received by zhanvonwitbrock@gmail.com",
                    "source": "vontology_prompt_template",
                    "source_prompt_concept_id": "#V#test_synthesiser_context_framing_prompt",
                    "template_schema": SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA,
                    "template_field": "active_request_template",
                },
                {
                    "role": "system",
                    "content": "AUTH tool gmail_list_messages:\nAUTH collection: Enumerate every retrieved email item.",
                    "source": "vontology_prompt_template",
                    "source_prompt_concept_id": "#V#test_synthesiser_context_framing_prompt",
                    "template_schema": SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA,
                    "template_field": "tool_hints_template",
                    "tool_concept_id": "gmail_list_messages",
                    "hint_predicate_ids": ["#V#output_collection_presentation_hint"],
                },
            ],
            "emit_progress": None,
        },
        environment=SimpleNamespace(llm_client=object(), model="gpt-test"),
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.outputs["final_response"] == "Final answer"
    context = cast(list[Mapping[str, Any]], captured["context"])
    system_contents = [
        str(message.get("content"))
        for message in context
        if message.get("role") == "system"
    ]
    assert (
        system_contents.count(
            "AUTH active request: List the last ten email messages received by zhanvonwitbrock@gmail.com"
        )
        == 1
    )
    assert any(
        "Enumerate every retrieved email item." in content
        for content in system_contents
    )
    assert any(
        "Get the message ID for the single most recent email message"
        in str(message.get("content"))
        for message in context
        if message.get("role") == "user"
    )
    telemetry = cast(Mapping[str, Any], captured["context_telemetry"])
    assert telemetry["stage_added_message_count"] >= 2
    stage_added_messages = cast(
        list[Mapping[str, Any]], telemetry["stage_added_messages"]
    )
    assert [message.get("role") for message in stage_added_messages[:2]] == [
        "system",
        "system",
    ]
    assert (
        stage_added_messages[0]["source_prompt_concept_id"]
        == "#V#test_synthesiser_context_framing_prompt"
    )
    assert stage_added_messages[0]["template_field"] == "active_request_template"
    assert stage_added_messages[1]["template_field"] == "tool_hints_template"
    assert stage_added_messages[1]["tool_concept_id"] == "gmail_list_messages"
    assert all(
        isinstance(message.get("content_char_count"), int)
        and message["content_char_count"] > 0
        for message in stage_added_messages[:2]
    )


def test_tool_calling_respond_runs_synthesiser_context_prep_before_backfill(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    _install_synthesiser_context_template(monkeypatch)
    data: dict[str, Any] = {
        "prompt": "List the last ten email messages received by zhanvonwitbrock@gmail.com",
        "augmented_context": [],
        "invocations": [],
    }
    request = SimpleNamespace(
        data=data,
        environment=SimpleNamespace(
            llm_client=object(),
            model="gpt-test",
            user_concept_id=None,
            org_concept_id=None,
        ),
    )
    backfill_seen_messages: list[str] = []

    monkeypatch.setattr(
        orchestrator,
        "_action_tool_calling_plan",
        lambda _request: WorkflowActionResult(
            outputs={
                "tool_calls_present": True,
                "direct_response": False,
                "result": False,
            }
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_action_tool_calling_validate",
        lambda _request: WorkflowActionResult(
            outputs={
                "tool_call_repair_required": False,
                "tool_calls_validated": True,
            }
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_action_tool_calling_execute",
        lambda _request: WorkflowActionResult(outputs={"result": True}),
    )

    def _backfill(_request: Any) -> WorkflowActionResult:
        raw_messages = data.get("synthesiser_system_messages")
        if isinstance(raw_messages, list):
            backfill_seen_messages.extend(str(message) for message in raw_messages)
        return WorkflowActionResult(
            outputs={
                "more_tool_calls": False,
                "tool_calls_present": False,
                "direct_response": False,
                "result": True,
                "final_response": "Final answer",
            }
        )

    monkeypatch.setattr(orchestrator, "_action_tool_calling_backfill", _backfill)

    result = orchestrator._action_tool_calling_respond(request)

    assert result.ok
    assert any(
        "AUTH active request: List the last ten email messages received by zhanvonwitbrock@gmail.com"
        in message
        for message in backfill_seen_messages
    )


def test_tool_calling_backfill_surfaces_ontology_predicate_evidence(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()

    captured: dict[str, Any] = {}

    def _run_llm_with_fallbacks(**kwargs: Any) -> tuple[str, str, Mapping[str, Any]]:
        context_messages = cast(list[Mapping[str, Any]], kwargs.get("context") or [])
        captured["context"] = context_messages
        context_text = "\n".join(
            str(message.get("content") or "")
            for message in context_messages
            if isinstance(message, Mapping)
        )
        if (
            "Expected answer contract for this turn" in context_text
            and "#V#has_phd_supervisor" in context_text
            and "#V#member_of_organisation" in context_text
            and "Timothy Pistotti has relation #V#has_phd_supervisor" in context_text
        ):
            return (
                "Grounded predicate evidence for SAIL students includes "
                "#V#has_phd_supervisor and #V#member_of_organisation.",
                "gpt-test",
                {},
            )
        return "I cannot identify any grounded predicates yet.", "gpt-test", {}

    monkeypatch.setattr(
        orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks
    )
    monkeypatch.setattr(
        orchestrator,
        "_interpret_model_turn",
        lambda _text: SimpleNamespace(tool_calls=[], tool_call_parse_error=None),
    )
    monkeypatch.setattr(
        orchestrator,
        "_evaluate_prompt_requirements",
        lambda **_kwargs: SimpleNamespace(
            required_tools=[],
            required_fetch_concept_ids=[],
            required_read_file_copy_ids=[],
            required_scholarly_representation_file_copy_ids=[],
            required_create_type_name=None,
            missing_tools=[],
            missing_fetch_concept_ids=[],
            missing_read_file_copy_ids=[],
            missing_scholarly_representation_file_copy_ids=[],
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_augment_prompt_requirements_with_turn_contract",
        lambda **_kwargs: _kwargs["evaluation"],
    )
    monkeypatch.setattr(
        orchestrator,
        "_store_prompt_requirement_evaluation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator, "_run_missing_tool_call_recovery_workflow", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        orchestrator,
        "_sanitise_user_visible_action_output",
        lambda text, **_kwargs: text,
    )
    monkeypatch.setattr(
        orchestrator, "_resolve_environment_max_tool_invocations", lambda _env: 4
    )
    monkeypatch.setattr(
        orchestrator,
        "_assess_missing_tool_call",
        lambda **_kwargs: SimpleNamespace(retry_reason=None),
    )

    text_relations_payload = {
        "concept_id": "#V#sail_student_group",
        "groups_found": 2,
        "total_relations_scanned": 6,
        "groups": [
            {
                "predicate": "#V#has_phd_supervisor",
                "language": "en-NZ",
                "count": 3,
                "relation_ids": ["r1", "r2", "r3"],
                "latest_relation_id": "r3",
            },
            {
                "predicate": "#V#member_of_organisation",
                "language": "en-NZ",
                "count": 3,
                "relation_ids": ["r4", "r5", "r6"],
                "latest_relation_id": "r6",
            },
        ],
    }
    related_concepts_payload = {
        "concept_id": "#V#sail_student_group",
        "seed_text": "SAIL students and their represented relationships.",
        "count": 1,
        "fallback_used": True,
        "fallback_mode": "graph_text",
        "results": [
            {
                "id": "graph_text::timothy",
                "score": 0.94,
                "text": (
                    "Timothy Pistotti has relation #V#has_phd_supervisor with "
                    "SAIL Student Group. Timothy Pistotti: PhD student in SAIL."
                ),
                "metadata": {
                    "item_kind": "concept_relation_fallback",
                    "source_system": "vontology.graph",
                    "predicate": "#V#has_phd_supervisor",
                    "direction": "incoming",
                    "concept_id": "#V#timothy_pistotti",
                    "subject_concept_id": "#V#sail_student_group",
                },
            }
        ],
    }

    augmented_context = [
        {"role": "system", "content": "system-one"},
        {"role": "user", "content": "What predicates are salient to SAIL students?"},
        {
            "role": "tool",
            "content": orchestrator._format_tool_result(
                "get_text_relations_summary",
                text_relations_payload,
                1.0,
                "ok",
            ),
        },
        {
            "role": "tool",
            "content": orchestrator._format_tool_result(
                "get_related_concepts",
                related_concepts_payload,
                1.0,
                "ok",
            ),
        },
    ]

    request = SimpleNamespace(
        data={
            "augmented_context": augmented_context,
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": None,
            "org_concept_id": None,
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 0,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "get_text_relations_summary",
                    "arguments": {"concept_id": "#V#sail_student_group"},
                    "payload": text_relations_payload,
                },
                {
                    "tool": "get_related_concepts",
                    "arguments": {"concept_id": "#V#sail_student_group"},
                    "payload": related_concepts_payload,
                },
            ],
            "method_catalogue": {},
            "prompt": "What predicates are salient to SAIL students?",
            "prompt_for_requirements": "What predicates are salient to SAIL students?",
            "turn_expected_outcome_summary": (
                "Identify predicates salient to SAIL students."
            ),
            "turn_expected_grounding_requirement": (
                "Predicates must be grounded in represented relationships or text relations."
            ),
            "turn_answering_guidance": (
                "Surface the grounded predicate evidence directly rather than returning only counts."
            ),
            "emit_progress": None,
        },
        environment=SimpleNamespace(llm_client=object(), model="gpt-test"),
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.outputs["final_response"] == (
        "Grounded predicate evidence for SAIL students includes "
        "#V#has_phd_supervisor and #V#member_of_organisation."
    )
    context_text = "\n".join(
        str(message.get("content") or "")
        for message in cast(list[Mapping[str, Any]], captured["context"])
        if isinstance(message, Mapping)
    )
    assert "Predicate evidence observed" in context_text
    assert "Related evidence excerpts" in context_text


def test_tool_calling_backfill_surfaces_relation_argument_evidence(monkeypatch) -> None:
    orchestrator = _bare_orchestrator()

    captured: dict[str, Any] = {}

    def _run_llm_with_fallbacks(**kwargs: Any) -> tuple[str, str, Mapping[str, Any]]:
        context_messages = cast(list[Mapping[str, Any]], kwargs.get("context") or [])
        captured["context"] = context_messages
        context_text = "\n".join(
            str(message.get("content") or "")
            for message in context_messages
            if isinstance(message, Mapping)
        )
        if (
            "Relation-bearing evidence excerpts" in context_text
            and "#V#member_of_organisation" in context_text
            and "Timothy Pistotti" in context_text
        ):
            return (
                "Grounded relation evidence for SAIL students includes "
                "#V#member_of_organisation.",
                "gpt-test",
                {},
            )
        return "I cannot identify any grounded relation evidence yet.", "gpt-test", {}

    monkeypatch.setattr(
        orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks
    )
    monkeypatch.setattr(
        orchestrator,
        "_interpret_model_turn",
        lambda _text: SimpleNamespace(tool_calls=[], tool_call_parse_error=None),
    )
    monkeypatch.setattr(
        orchestrator,
        "_evaluate_prompt_requirements",
        lambda **_kwargs: SimpleNamespace(
            required_tools=[],
            required_fetch_concept_ids=[],
            required_read_file_copy_ids=[],
            required_scholarly_representation_file_copy_ids=[],
            required_create_type_name=None,
            missing_tools=[],
            missing_fetch_concept_ids=[],
            missing_read_file_copy_ids=[],
            missing_scholarly_representation_file_copy_ids=[],
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_augment_prompt_requirements_with_turn_contract",
        lambda **_kwargs: _kwargs["evaluation"],
    )
    monkeypatch.setattr(
        orchestrator,
        "_store_prompt_requirement_evaluation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator, "_run_missing_tool_call_recovery_workflow", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        orchestrator,
        "_sanitise_user_visible_action_output",
        lambda text, **_kwargs: text,
    )
    monkeypatch.setattr(
        orchestrator, "_resolve_environment_max_tool_invocations", lambda _env: 4
    )
    monkeypatch.setattr(
        orchestrator,
        "_assess_missing_tool_call",
        lambda **_kwargs: SimpleNamespace(retry_reason=None),
    )

    relation_payload = {
        "concept_id": "#V#sail_student_group",
        "total_hits": 2,
        "hits": [
            {
                "source_concept_id": "#V#timothy_pistotti",
                "predicate_concept_id": "#V#member_of_organisation",
                "relation_kind": "binary",
                "argument_indexes": [2],
                "target_value": "#V#sail_student_group",
                "source_concept_preview": {
                    "concept_id": "#V#timothy_pistotti",
                    "name": "Timothy Pistotti",
                    "kind": "individual",
                },
                "target_concept_preview": {
                    "concept_id": "#V#sail_student_group",
                    "name": "SAIL Student Group",
                    "kind": "type",
                },
                "relation_metadata": {
                    "relation_id": "struct::timothy::member_of_organisation",
                    "match_type": "exact",
                },
                "score": 1.0,
                "is_asserted": True,
                "relation_state": "asserted",
            },
            {
                "source_concept_id": "#V#rebecca_allcock",
                "predicate_concept_id": "#V#member_of_organisation",
                "relation_kind": "binary",
                "argument_indexes": [2],
                "target_value": "#V#sail_student_group",
                "source_concept_preview": {
                    "concept_id": "#V#rebecca_allcock",
                    "name": "Rebecca Allcock",
                    "kind": "individual",
                },
                "target_concept_preview": {
                    "concept_id": "#V#sail_student_group",
                    "name": "SAIL Student Group",
                    "kind": "type",
                },
                "relation_metadata": {
                    "relation_id": "struct::rebecca::member_of_organisation",
                    "match_type": "exact",
                },
                "score": 1.0,
                "is_asserted": True,
                "relation_state": "asserted",
            },
        ],
    }

    request = SimpleNamespace(
        data={
            "augmented_context": [
                {"role": "system", "content": "system-one"},
                {
                    "role": "user",
                    "content": "What predicates are salient to SAIL students?",
                },
            ],
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": None,
            "org_concept_id": None,
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 0,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "find_relations_with_argument",
                    "arguments": {"concept_id": "#V#sail_student_group"},
                    "payload": relation_payload,
                }
            ],
            "method_catalogue": {},
            "prompt": "What predicates are salient to SAIL students?",
            "prompt_for_requirements": "What predicates are salient to SAIL students?",
            "turn_expected_outcome_summary": (
                "Identify relation-bearing predicate evidence for SAIL students."
            ),
            "turn_expected_grounding_requirement": (
                "Predicates must be grounded in retrieved relation instances."
            ),
            "turn_answering_guidance": (
                "Surface the grounded relation evidence directly rather than returning only counts."
            ),
            "emit_progress": None,
        },
        environment=SimpleNamespace(llm_client=object(), model="gpt-test"),
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.outputs["final_response"] == (
        "Grounded relation evidence for SAIL students includes "
        "#V#member_of_organisation."
    )
    context_text = "\n".join(
        str(message.get("content") or "")
        for message in cast(list[Mapping[str, Any]], captured["context"])
        if isinstance(message, Mapping)
    )
    assert "find_relations_with_argument returned 2 relation hits" in context_text
    assert "Relation predicates observed: #V#member_of_organisation" in context_text
    assert "Relation-bearing evidence excerpts" in context_text


def test_tool_calling_backfill_prioritises_positive_evidence_over_zero_result_surfaces(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()

    captured: dict[str, Any] = {}

    def _run_llm_with_fallbacks(**kwargs: Any) -> tuple[str, str, Mapping[str, Any]]:
        context_messages = cast(list[Mapping[str, Any]], kwargs.get("context") or [])
        captured["context"] = context_messages
        context_text = "\n".join(
            str(message.get("content") or "")
            for message in context_messages
            if isinstance(message, Mapping)
        )
        positive_index = context_text.find("Positive retrieval signals for this turn:")
        zero_index = context_text.find(
            "Zero-result or inconclusive retrieval surfaces for this turn:"
        )
        if (
            positive_index != -1
            and zero_index != -1
            and positive_index < zero_index
            and "Treat zero-result notes as query-specific misses only." in context_text
            and "Relation-bearing evidence excerpts" in context_text
            and "#V#linked_to_user" in context_text
            and "Example Record" in context_text
        ):
            return (
                "I found grounded represented evidence linking Example Record to the current user.",
                "gpt-test",
                {},
            )
        return "I couldn't find any grounded represented links.", "gpt-test", {}

    monkeypatch.setattr(
        orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks
    )
    monkeypatch.setattr(
        orchestrator,
        "_interpret_model_turn",
        lambda _text: SimpleNamespace(tool_calls=[], tool_call_parse_error=None),
    )
    monkeypatch.setattr(
        orchestrator,
        "_evaluate_prompt_requirements",
        lambda **_kwargs: SimpleNamespace(
            required_tools=[],
            required_fetch_concept_ids=[],
            required_read_file_copy_ids=[],
            required_scholarly_representation_file_copy_ids=[],
            required_create_type_name=None,
            missing_tools=[],
            missing_fetch_concept_ids=[],
            missing_read_file_copy_ids=[],
            missing_scholarly_representation_file_copy_ids=[],
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_augment_prompt_requirements_with_turn_contract",
        lambda **_kwargs: _kwargs["evaluation"],
    )
    monkeypatch.setattr(
        orchestrator,
        "_store_prompt_requirement_evaluation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator, "_run_missing_tool_call_recovery_workflow", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        orchestrator,
        "_sanitise_user_visible_action_output",
        lambda text, **_kwargs: text,
    )
    monkeypatch.setattr(
        orchestrator, "_resolve_environment_max_tool_invocations", lambda _env: 4
    )
    monkeypatch.setattr(
        orchestrator,
        "_assess_missing_tool_call",
        lambda **_kwargs: SimpleNamespace(retry_reason=None),
    )

    relation_payload = {
        "concept_id": "#V#test_user",
        "total_hits": 1,
        "hits": [
            {
                "source_concept_id": "#V#example_record",
                "predicate_concept_id": "#V#linked_to_user",
                "relation_kind": "binary",
                "argument_indexes": [2],
                "target_value": "#V#test_user",
                "source_concept_preview": {
                    "concept_id": "#V#example_record",
                    "name": "Example Record",
                    "kind": "individual",
                },
                "target_concept_preview": {
                    "concept_id": "#V#test_user",
                    "name": "Test User",
                    "kind": "individual",
                },
                "relation_metadata": {
                    "relation_id": "struct::example_record::linked_to_user",
                    "match_type": "exact",
                },
                "score": 1.0,
                "is_asserted": True,
                "relation_state": "asserted",
            }
        ],
    }

    request = SimpleNamespace(
        data={
            "augmented_context": [
                {"role": "system", "content": "system-one"},
                {
                    "role": "user",
                    "content": "List grounded represented records linked to the current user.",
                },
            ],
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": "#V#test_user",
            "org_concept_id": "#V#test_org",
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 1,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "search_knowledge_base",
                    "arguments": {"query": "current user represented links"},
                    "payload": {
                        "query": "current user represented links",
                        "count": 0,
                        "results": [],
                    },
                },
                {
                    "tool": "find_relations_with_argument",
                    "arguments": {"concept_id": "#V#test_user"},
                    "payload": relation_payload,
                },
            ],
            "method_catalogue": {},
            "prompt": "List grounded represented records linked to the current user.",
            "prompt_for_requirements": (
                "List grounded represented records linked to the current user."
            ),
            "turn_expected_outcome_summary": (
                "List grounded represented records linked to the current user."
            ),
            "turn_expected_grounding_requirement": (
                "Only surface represented records when they are grounded in retrieved evidence."
            ),
            "turn_answering_guidance": (
                "Prefer concrete grounded evidence over count-only or zero-result summaries."
            ),
            "emit_progress": None,
        },
        environment=SimpleNamespace(llm_client=object(), model="gpt-test"),
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.outputs["final_response"] == (
        "I found grounded represented evidence linking Example Record to the current user."
    )
    context_text = "\n".join(
        str(message.get("content") or "")
        for message in cast(list[Mapping[str, Any]], captured["context"])
        if isinstance(message, Mapping)
    )
    assert "Positive retrieval signals for this turn:" in context_text
    assert (
        "Zero-result or inconclusive retrieval surfaces for this turn:" in context_text
    )
    assert context_text.find(
        "Positive retrieval signals for this turn:"
    ) < context_text.find(
        "Zero-result or inconclusive retrieval surfaces for this turn:"
    )
    assert "Treat zero-result notes as query-specific misses only." in context_text
    assert "Example Record via #V#linked_to_user -> Test User" in context_text


def test_tool_calling_backfill_finalises_from_completed_results_when_tool_cap_reached(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_build_follow_up_llm_context",
        lambda context, max_chars=40_000: list(context),
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_expected_outcome_stage_messages",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_tool_follow_up_stage_messages",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_stage_llm_context",
        lambda **kwargs: (
            list(kwargs.get("base_context") or []),
            {"stage": "summariser"},
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_attach_memory_context_lineage",
        lambda *_args, **_kwargs: None,
    )

    prompts: list[str] = []

    def _run_llm_with_fallbacks(**kwargs: Any) -> tuple[str, str | None, None]:
        prompt = str(kwargs.get("prompt") or "")
        prompts.append(prompt)
        if "tool invocation limit" in prompt:
            return (
                "Partial final answer grounded in the completed tool results.",
                kwargs.get("default_model"),
                None,
            )
        return (
            '{"action":"call_tool","tool":"fetch_more","payload":{"id":"next"}}',
            kwargs.get("default_model"),
            None,
        )

    monkeypatch.setattr(
        orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks
    )

    progress_events: list[Mapping[str, Any]] = []
    request = SimpleNamespace(
        data={
            "augmented_context": [
                {"role": "user", "content": "List the records."},
                {"role": "tool", "content": '{"tool":"list_records","status":"ok"}'},
            ],
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": "#V#test_user",
            "org_concept_id": "#V#test_org",
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 8,
            "remaining_tool_calls": [],
            "invocations": [{"tool": "list_records", "status": "ok"}],
            "method_catalogue": {},
            "prompt": "List the records.",
            "prompt_for_requirements": "List the records.",
            "emit_progress": progress_events.append,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gpt-test",
            max_tool_invocations=8,
        ),
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.outputs["final_response"].startswith(
        "Tool-use limit reached: this turn reached "
        "`internal_mcp_max_tool_invocations=8` after 8 tool call(s)."
    )
    assert (
        "Partial final answer grounded in the completed tool results."
        in (result.outputs["final_response"])
    )
    assert len(prompts) == 2
    assert "internal_mcp_max_tool_invocations=8" in prompts[-1]
    assert any(event.get("status") == "tool_limit_reached" for event in progress_events)
    assert any(
        event.get("settings_key") == "internal_mcp_max_tool_invocations"
        for event in progress_events
    )
    aux_entries = [
        entry
        for entry in request.data["aux_llm_calls"]
        if isinstance(entry, Mapping) and entry.get("type") == "tool_limit_finalisation"
    ]
    assert aux_entries
    assert aux_entries[-1]["settings_key"] == "internal_mcp_max_tool_invocations"


def test_tool_calling_backfill_names_cap_when_follow_up_contract_is_blocked(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_build_follow_up_llm_context",
        lambda context, max_chars=40_000: list(context),
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_expected_outcome_stage_messages",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_tool_follow_up_stage_messages",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_stage_llm_context",
        lambda **kwargs: (
            list(kwargs.get("base_context") or []),
            {"stage": "summariser"},
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_attach_memory_context_lineage",
        lambda *_args, **_kwargs: None,
    )

    prompts: list[str] = []

    def _run_llm_with_fallbacks(**kwargs: Any) -> tuple[str, str | None, None]:
        prompt = str(kwargs.get("prompt") or "")
        prompts.append(prompt)
        return (
            "Partial answer from completed results.",
            kwargs.get("default_model"),
            None,
        )

    monkeypatch.setattr(
        orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks
    )

    progress_events: list[Mapping[str, Any]] = []
    request = SimpleNamespace(
        data={
            "augmented_context": [
                {"role": "user", "content": "List records and details."},
                {"role": "tool", "content": '{"tool":"list_records","status":"ok"}'},
            ],
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": "#V#test_user",
            "org_concept_id": "#V#test_org",
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 2,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "list_records",
                    "status": "ok",
                    "effective_arguments": {"workspace_id": "lab"},
                    "effective_payload": {
                        "records": [{"record_id": "r1"}],
                        "_tool_follow_up": {
                            "schema_version": "mcp_tool_follow_up.v1",
                            "item_array_field": "records",
                            "required_when_any_item_missing_fields": ["detail"],
                            "follow_up_tools": [
                                {
                                    "tool": "get_record",
                                    "input_bindings": {
                                        "workspace_id": {
                                            "source": "request",
                                            "field": "workspace_id",
                                        },
                                        "record_id": {
                                            "source": "item",
                                            "field": "record_id",
                                        },
                                    },
                                }
                            ],
                        },
                    },
                },
                {
                    "tool": "other_tool",
                    "status": "ok",
                    "effective_arguments": {"id": "already-used"},
                    "effective_payload": {"ok": True},
                },
            ],
            "method_catalogue": {},
            "prompt": "List records and details.",
            "prompt_for_requirements": "List records and details.",
            "emit_progress": progress_events.append,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gpt-test",
            max_tool_invocations=2,
        ),
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.outputs["tool_limit_reached"] is True
    assert result.outputs["pending_follow_up_tool_call_count"] == 1
    assert result.outputs["final_response"].startswith(
        "Tool-use limit reached: this turn reached "
        "`internal_mcp_max_tool_invocations=2` after 2 tool call(s). "
        "1 pending required or follow-up tool call(s) were blocked by that setting."
    )
    assert "Partial answer from completed results." in result.outputs["final_response"]
    assert len(prompts) == 1
    assert "1 pending follow-up call(s) blocked by that setting" in prompts[0]
    assert any(
        event.get("status") == "tool_limit_reached"
        and event.get("settings_key") == "internal_mcp_max_tool_invocations"
        and event.get("pending_follow_up_tool_call_count") == 1
        for event in progress_events
    )


def test_tool_calling_backfill_finalises_when_required_tool_is_missing_at_cap(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_build_follow_up_llm_context",
        lambda context, max_chars=40_000: list(context),
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_expected_outcome_stage_messages",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_synthesiser_context_prep_stage_messages",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_tool_follow_up_stage_messages",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_stage_llm_context",
        lambda **kwargs: (
            list(kwargs.get("base_context") or []),
            {"stage": "summariser"},
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_attach_memory_context_lineage",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_evaluate_prompt_requirements",
        lambda **_kwargs: _PromptRequirementEvaluation(
            required_tools=("create_concepts",),
            missing_tools=("create_concepts",),
            missing_retry_reason="Required tool create_concepts was not invoked.",
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_augment_prompt_requirements_with_turn_contract",
        lambda **kwargs: kwargs["evaluation"],
    )
    monkeypatch.setattr(
        orchestrator,
        "_merge_prompt_requirements_with_existing_tool_policy",
        lambda **kwargs: kwargs["evaluation"],
    )
    monkeypatch.setattr(
        orchestrator,
        "_store_prompt_requirement_evaluation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_infer_missing_tool_call_retry_tool_calls",
        lambda *_args, **_kwargs: [
            {
                "action": "call_tool",
                "tool": "create_concepts",
                "payload": {"concepts": []},
            }
        ],
    )
    monkeypatch.setattr(
        orchestrator,
        "_agent_test_local_relation_backfill_result",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_convert_mcp_tools_to_structured_definitions",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_sanitise_user_visible_action_output",
        lambda text, **_kwargs: text,
    )

    continuation_calls: list[Mapping[str, Any]] = []

    def _run_llm_with_tools_fallbacks(
        **kwargs: Any,
    ) -> tuple[LLMResponse, str, Mapping[str, Any]]:
        continuation_calls.append(dict(kwargs))
        return (
            LLMResponse(
                text_response=(
                    "Partial answer from completed reads; the required write was not run."
                )
            ),
            "gpt-test",
            {},
        )

    monkeypatch.setattr(
        orchestrator,
        "_run_llm_with_tools_fallbacks",
        _run_llm_with_tools_fallbacks,
    )

    progress_events: list[Mapping[str, Any]] = []
    request = SimpleNamespace(
        data={
            "augmented_context": [
                {"role": "user", "content": "Represent the supplied meeting."},
                {"role": "tool", "content": '{"tool":"fetch_concept","status":"ok"}'},
            ],
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": "#V#test_user",
            "org_concept_id": "#V#test_org",
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 1,
            "remaining_tool_calls": [],
            "invocations": [{"tool": "fetch_concept", "status": "ok"}],
            "method_catalogue": {
                "create_concepts": {"category": "write"},
            },
            "prompt": "Represent the supplied meeting.",
            "prompt_for_requirements": "Represent the supplied meeting.",
            "structured_tool_continuation": {
                "provider": "openai",
                "api_surface": "responses",
                "model": "gpt-test",
                "output_items": [
                    {
                        "type": "function_call",
                        "name": "fetch_concept",
                        "call_id": "call-fetch",
                    }
                ],
                "transport_decision": {
                    "accepted_tool_call_ids": ["call-fetch"],
                },
            },
            "tool_messages": [
                {
                    "role": "tool",
                    "name": "fetch_concept",
                    "tool_call_id": "call-fetch",
                    "content": '{"success":true}',
                }
            ],
            "emit_progress": progress_events.append,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gpt-test",
            max_tool_invocations=1,
        ),
        trace=None,
        workflow_id="#V#meeting_representation_workflow",
        workflow_state_id="#V#represent_meeting",
        action_id="llm.action",
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.outputs["more_tool_calls"] is False
    assert result.outputs["tool_calls_present"] is False
    assert result.outputs["tool_limit_reached"] is True
    assert result.outputs["pending_follow_up_tool_call_count"] == 1
    assert result.outputs["structured_tool_continuation"] is None
    assert result.outputs["missing_tool_call_retry_suppressed"] is True
    assert (
        result.outputs["missing_tool_call_retry_stop_reason"]
        == "tool_invocation_budget_exhausted"
    )
    assert (
        result.outputs["missing_tool_call_recovery_outcome"]
        == "tool_invocation_budget_exhausted"
    )
    assert result.outputs["final_response"].startswith(
        "Tool-use limit reached: this turn reached "
        "`internal_mcp_max_tool_invocations=1` after 1 tool call(s)."
    )
    assert len(continuation_calls) == 1
    assert continuation_calls[0]["tool_choice_override"] == "none"
    assert [result.call_id for result in continuation_calls[0]["tool_results"]] == [
        "call-fetch"
    ]
    assert any(
        event.get("status") == "tool_limit_reached"
        and event.get("pending_follow_up_tool_call_count") == 1
        for event in progress_events
    )
    assert not any(
        event.get("type") == "missing_tool_call_retry"
        for event in request.data["aux_llm_calls"]
    )
    assert (
        sum(
            event.get("type") == "tool_limit_finalisation"
            for event in request.data["aux_llm_calls"]
        )
        == 1
    )


def _install_parent_forced_required_tool_backfill_stubs(
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: InternalMCPChatOrchestrator,
) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_agent_test_local_relation_backfill_result",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_follow_up_llm_context",
        lambda context, max_chars=40_000: list(context),
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_expected_outcome_stage_messages",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_synthesiser_context_prep_stage_messages",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_tool_follow_up_stage_messages",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_stage_llm_context",
        lambda **kwargs: (
            list(kwargs.get("base_context") or []),
            {"stage": "summariser"},
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_attach_memory_context_lineage",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_evaluate_prompt_requirements",
        lambda **_kwargs: _PromptRequirementEvaluation(
            required_tools=("create_concepts",),
            missing_tools=("create_concepts",),
            missing_retry_reason="Required tool create_concepts was not invoked.",
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_augment_prompt_requirements_with_turn_contract",
        lambda **kwargs: kwargs["evaluation"],
    )
    monkeypatch.setattr(
        orchestrator,
        "_merge_prompt_requirements_with_existing_tool_policy",
        lambda **kwargs: kwargs["evaluation"],
    )
    monkeypatch.setattr(
        orchestrator,
        "_store_prompt_requirement_evaluation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_infer_missing_tool_call_retry_tool_calls",
        lambda *_args, **_kwargs: [
            {
                "action": "call_tool",
                "tool": "create_concepts",
                "payload": {"concepts": []},
            }
        ],
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_turn_launchability_probe_inputs",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        orchestrator,
        "_resolve_method_catalogue_for_workflow_data",
        lambda _data: {"create_concepts": {"category": "write"}},
    )
    monkeypatch.setattr(
        orchestrator,
        "_selected_gmail_profile_for_workflow_data",
        lambda _data, _env: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_convert_mcp_tools_to_structured_definitions",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        orchestrator,
        "_sanitise_user_visible_action_output",
        lambda text, **_kwargs: text,
    )


def test_tool_calling_respond_terminates_after_single_exhausted_cap_cycle(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    _install_parent_forced_required_tool_backfill_stubs(monkeypatch, orchestrator)

    monkeypatch.setattr(
        orchestrator,
        "_action_tool_calling_plan",
        lambda _request: WorkflowActionResult(
            outputs={
                "tool_calls_present": True,
                "direct_response": False,
                "result": True,
            }
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_action_tool_calling_validate",
        lambda _request: WorkflowActionResult(
            outputs={
                "tool_call_repair_required": False,
                "tool_calls_validated": True,
                "result": True,
            }
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_synthesiser_context_prep_stage",
        lambda _request: WorkflowActionResult(outputs={"result": True}),
    )

    real_execute = orchestrator._action_tool_calling_execute
    execute_iterations: list[int] = []

    def _execute_once(request: Any) -> WorkflowActionResult:
        execute_iterations.append(int(request.data["iteration_count"]))
        if len(execute_iterations) > 1:
            pytest.fail("exhausted-cap composite entered a second execute cycle")
        return real_execute(request)

    monkeypatch.setattr(orchestrator, "_action_tool_calling_execute", _execute_once)

    continuation_calls: list[Mapping[str, Any]] = []

    def _run_llm_with_tools_fallbacks(
        **kwargs: Any,
    ) -> tuple[LLMResponse, str, Mapping[str, Any]]:
        continuation_calls.append(dict(kwargs))
        return (
            LLMResponse(text_response="Partial answer from the completed reads."),
            "gpt-test",
            {},
        )

    monkeypatch.setattr(
        orchestrator,
        "_run_llm_with_tools_fallbacks",
        _run_llm_with_tools_fallbacks,
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_llm_with_fallbacks",
        lambda **_kwargs: pytest.fail("legacy model path must not run"),
    )

    pending_call = {
        "action": "call_tool",
        "tool": "create_concepts",
        "payload": {"concepts": []},
        "_call_id": "call-create",
    }
    initial_context = [
        {"role": "user", "content": "Represent the supplied meeting."},
        {"role": "tool", "content": '{"tool":"fetch_concept","status":"ok"}'},
    ]
    request = SimpleNamespace(
        data={
            "prompt": "Represent the supplied meeting.",
            "prompt_for_requirements": "Represent the supplied meeting.",
            "augmented_context": list(initial_context),
            "tool_calls": [pending_call],
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": "#V#test_user",
            "org_concept_id": "#V#test_org",
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 1,
            "remaining_tool_calls": [],
            "invocations": [{"tool": "fetch_concept", "status": "ok"}],
            "method_catalogue": {
                "create_concepts": {"category": "write"},
            },
            "tool_categories": {"create_concepts": "write"},
            "tool_messages": [
                {
                    "role": "tool",
                    "name": "fetch_concept",
                    "tool_call_id": "call-fetch",
                    "content": '{"success":true}',
                }
            ],
            "structured_tool_continuation": {
                "provider": "openai",
                "api_surface": "responses",
                "model": "gpt-test",
                "output_items": [
                    {
                        "type": "function_call",
                        "name": "fetch_concept",
                        "call_id": "call-fetch",
                    }
                ],
                "transport_decision": {
                    "accepted_tool_call_ids": ["call-fetch"],
                },
            },
            "allowed_write_tools": set(),
            "recent_user_prompts": [],
            "emit_progress": None,
            "check_cancellation": None,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gpt-test",
            max_tool_invocations=1,
            user_namespace="#V#test_user",
            default_gmail_profile=None,
        ),
        trace=None,
        workflow_id="#V#meeting_representation_workflow",
        workflow_state_id="#V#represent_meeting",
        workflow_state_metadata={},
        action_id="tool_calling.respond",
    )

    result = orchestrator._action_tool_calling_respond(request)

    assert result.ok
    assert execute_iterations == [1]
    assert request.data["iteration_count"] == 1
    assert request.data["more_tool_calls"] is False
    assert request.data["tool_limit_reached"] is True
    assert len(request.data["augmented_context"]) == len(initial_context) + 1
    assert len(request.data["tool_messages"]) == 2
    cap_receipts = [
        json.loads(message["content"])
        for message in request.data["tool_messages"]
        if isinstance(message, Mapping)
        and isinstance(message.get("content"), str)
        and "tool_limit_reached" in message["content"]
    ]
    assert cap_receipts == [
        {
            "status": "not_executed",
            "error_code": "tool_limit_reached",
            "settings_key": "internal_mcp_max_tool_invocations",
            "tool_calls_cap": 1,
        }
    ]
    assert len(continuation_calls) == 1
    assert continuation_calls[0]["tool_choice_override"] == "none"
    assert result.outputs["final_response"].startswith(
        "Tool-use limit reached: this turn reached "
        "`internal_mcp_max_tool_invocations=1` after 1 tool call(s)."
    )


def test_tool_calling_backfill_parent_forced_retry_remains_available_below_cap(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    _install_parent_forced_required_tool_backfill_stubs(monkeypatch, orchestrator)
    monkeypatch.setattr(
        orchestrator,
        "_run_llm_with_tools_fallbacks",
        lambda **_kwargs: pytest.fail("structured continuation must not run below cap"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_llm_with_fallbacks",
        lambda **_kwargs: pytest.fail("legacy model path must not run below cap"),
    )

    request = SimpleNamespace(
        data={
            "prompt": "Represent the supplied meeting.",
            "prompt_for_requirements": "Represent the supplied meeting.",
            "augmented_context": [
                {"role": "user", "content": "Represent the supplied meeting."},
            ],
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": "#V#test_user",
            "org_concept_id": "#V#test_org",
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 1,
            "remaining_tool_calls": [],
            "invocations": [{"tool": "fetch_concept", "status": "ok"}],
            "method_catalogue": {
                "create_concepts": {"category": "write"},
            },
            "tool_messages": [],
            "structured_tool_continuation": {
                "provider": "openai",
                "api_surface": "responses",
                "model": "gpt-test",
            },
            "emit_progress": None,
        },
        environment=SimpleNamespace(
            llm_client=object(),
            model="gpt-test",
            max_tool_invocations=2,
            user_namespace="#V#test_user",
            default_gmail_profile=None,
        ),
        trace=None,
        workflow_id="#V#meeting_representation_workflow",
        workflow_state_id="#V#represent_meeting",
        workflow_state_metadata={},
        action_id="tool_calling.backfill",
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.ok
    assert result.outputs["more_tool_calls"] is True
    assert result.outputs["tool_calls_present"] is True
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "create_concepts",
            "payload": {"concepts": []},
        }
    ]
    assert result.outputs["missing_tool_call_recovery_outcome"] == (
        "retry_succeeded_parent_fallback"
    )
    assert (
        sum(
            event.get("type") == "missing_tool_call_retry"
            for event in request.data["aux_llm_calls"]
        )
        == 1
    )


def test_load_workflow_model_policy_distinguishes_graph_completeness(
    monkeypatch,
) -> None:
    """Telemetry distinguishes graph_complete, graph_incomplete, and JSON
    fallback resolution paths (JVNAUTOSCI-2496)."""

    import src.backend.services.text_value_service as text_value_service
    import src.backend.services.workflow_policy_graph_service as graph_service

    json_policy_text = '{"stages": {"planner": {"primary": "ollama:gemma4:26b"}}}'

    def _orchestrator_with_concepts() -> InternalMCPChatOrchestrator:
        orchestrator = _bare_orchestrator()
        monkeypatch.setenv("VON_WORKFLOW_MODEL_POLICY_ENABLE", "1")
        monkeypatch.setattr(
            orchestrator,
            "_resolve_concept_id_by_name",
            lambda name, **_kwargs: f"#V#{name}",
        )
        return orchestrator

    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [{"text": json_policy_text}],
    )

    complete_payload = {
        "policy_id": "#V#default_workflow_model_policy",
        "stages": {"planner": {"primary": "ollama:gemma4:26b"}},
        "constraints": {"local_only_stages": [], "max_fallback_hops": 2},
        "completeness": "graph_complete",
        "incomplete_reasons": [],
    }
    monkeypatch.setattr(
        graph_service,
        "resolve_policy_from_graph",
        lambda _policy_id: complete_payload,
    )
    orchestrator = _orchestrator_with_concepts()
    state, telemetry = orchestrator._load_workflow_model_policy(None)
    assert telemetry["policy_source"] == "graph"
    assert telemetry["graph_completeness"] == "graph_complete"
    assert state.policy is complete_payload

    incomplete_payload = dict(complete_payload)
    incomplete_payload["completeness"] = "graph_incomplete"
    incomplete_payload["incomplete_reasons"] = ["missing_max_fallback_hops"]
    monkeypatch.setattr(
        graph_service,
        "resolve_policy_from_graph",
        lambda _policy_id: incomplete_payload,
    )
    orchestrator = _orchestrator_with_concepts()
    state, telemetry = orchestrator._load_workflow_model_policy(None)
    assert telemetry["policy_source"] == "json"
    assert telemetry["graph_completeness"] == "graph_incomplete"
    assert telemetry["graph_incomplete_reasons"] == ["missing_max_fallback_hops"]
    assert state.policy is not None
    assert state.policy["stages"]["planner"]["primary"] == "ollama:gemma4:26b"

    monkeypatch.setattr(
        graph_service,
        "resolve_policy_from_graph",
        lambda _policy_id: None,
    )
    orchestrator = _orchestrator_with_concepts()
    state, telemetry = orchestrator._load_workflow_model_policy(None)
    assert telemetry["policy_source"] == "json"
    assert telemetry["graph_completeness"] == "graph_absent"
