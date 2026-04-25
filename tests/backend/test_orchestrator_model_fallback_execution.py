from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any, Mapping, cast

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    _ModelCandidate,
    _WorkflowModelPolicyState,
)


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
    return orchestrator


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
        lambda query, *_args, **_kwargs: {"_id": "1"}
        if query == {"concept_id": "#V#default_workflow_model_policy"}
        else None,
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

    resolved = orchestrator._resolve_concept_id_by_name(
        "default_workflow_model_policy"
    )

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

    resolved = orchestrator._resolve_concept_id_by_name(
        "default_workflow_model_policy"
    )

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
        entry
        for entry in aux_log
        if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["fallback_attempt_count"] == 2
    assert stage_summary["failure_count"] == 1
    assert stage_summary["selected"]["provider"] == "openai"

    attempts = stage_summary["fallback_attempts"]
    assert isinstance(attempts, list)
    assert attempts[0]["failure_kind"] == "provider_unreachable"
    assert attempts[0]["error_class"] == "ConnectionError"
    assert attempts[1]["status"] == "succeeded"

    assert recorded_calls[0]["note"] == "candidate reachability probe failed; trying fallback"


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
        default_client=_SuccessfulClient(),
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
        entry
        for entry in aux_log
        if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["policy_stage"] == "classifier"
    assert stage_summary["requested_model"] == "gpt-5.4-mini"
    assert stage_summary["selection_mode"] == "policy_primary_active_llm"
    assert stage_summary["follows_active_llm"] is True
    assert stage_summary["explicit_stage_model_override"] is False


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
        entry
        for entry in aux_log
        if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["policy_stage"] == "classifier"
    assert stage_summary["requested_model"] == "gpt-5.4-mini"
    assert stage_summary["selection_mode"] == "policy_primary_override"
    assert stage_summary["follows_active_llm"] is False
    assert stage_summary["explicit_stage_model_override"] is True
    assert (
        stage_summary["explicit_stage_model_override_origin"] == "policy_primary"
    )


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
        default_client=_SuccessfulClient(),
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
        entry
        for entry in aux_log
        if entry.get("type") == "workflow_model_policy_stage"
    )
    assert stage_summary["requested_model"] == "gemma4:26b"
    assert stage_summary["selection_mode"] == "preferred_default_model"
    assert stage_summary["prefer_default_model"] is True
    assert stage_summary["follows_active_llm"] is True


def test_stage_model_candidates_stay_on_requested_default_model(monkeypatch) -> None:
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

    assert [(candidate.source, candidate.provider, candidate.model) for candidate in candidates] == [
        ("active_llm", None, "gemma4:26b")
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
    assert local_probe["probe_timeout_ms"] == 200
    assert local_probe["reachable"] is True
    assert isinstance(local_probe["duration_ms"], int)

    assert remote_probe is not None
    assert remote_probe["provider"] == "ollama"
    assert remote_probe["host"] == "http://10.0.0.8:11434"
    assert remote_probe["probe_url"] == "http://10.0.0.8:11434/api/tags"
    assert remote_probe["probe_timeout_ms"] == 1200
    assert remote_probe["reachable"] is True
    assert isinstance(remote_probe["duration_ms"], int)
    assert seen_timeouts["http://localhost:11434/api/tags"] == pytest.approx(0.2)
    assert seen_timeouts["http://10.0.0.8:11434/api/tags"] == pytest.approx(1.2)


def test_invoke_with_llm_heartbeat_uses_backfill_timeout_for_summariser(
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

    with pytest.raises(TimeoutError, match=r"stage=summariser"):
        orchestrator._invoke_with_llm_heartbeat(
            call=_slow_call,
            stage_name="summariser",
            model_name="gpt-test",
            emit_progress=lambda payload: progress_events.append(dict(payload)),
        )

    assert progress_events, "expected heartbeat progress before timeout"
    assert progress_events[-1]["status"] == "heartbeat"
    assert progress_events[-1]["stage"] == "summariser"
    assert progress_events[-1]["liveness_reason"] == "llm_call_pending"


def test_invoke_with_llm_heartbeat_prefers_explicit_timeout_override(
    monkeypatch,
) -> None:
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    monkeypatch.setenv("VON_LLM_HEARTBEAT_INTERVAL_SEC", "1")
    monkeypatch.delenv("VON_LLM_CALL_TIMEOUT_SEC", raising=False)

    progress_events: list[dict[str, Any]] = []

    def _slow_call() -> str:
        time.sleep(2.0)
        return "done"

    with pytest.raises(TimeoutError, match=r"stage=classifier"):
        orchestrator._invoke_with_llm_heartbeat(
            call=_slow_call,
            stage_name="classifier",
            model_name="gemma4:26b",
            emit_progress=lambda payload: progress_events.append(dict(payload)),
            timeout_override_sec=1.0,
        )

    assert progress_events, "expected heartbeat progress before timeout"
    assert progress_events[-1]["status"] == "heartbeat"
    assert progress_events[-1]["stage"] == "classifier"
    assert progress_events[-1]["liveness_reason"] == "llm_call_pending"


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

    monkeypatch.setattr(orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks)
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
        orchestrator, "_store_prompt_requirement_evaluation", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "_run_missing_tool_call_recovery_workflow", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        orchestrator, "_sanitise_user_visible_action_output", lambda text, **_kwargs: text
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
        "old-user-2",
        "old-assistant-2",
        "new-user",
        "new-assistant",
        "recent-tool-1",
        "recent-tool-2",
        "recent-tool-3",
        "recent-tool-4",
        "recent-tool-5",
        "recent-tool-6",
    ]


def test_tool_calling_backfill_surfaces_ontology_predicate_evidence(monkeypatch) -> None:
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

    monkeypatch.setattr(orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks)
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
        orchestrator, "_store_prompt_requirement_evaluation", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "_run_missing_tool_call_recovery_workflow", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        orchestrator, "_sanitise_user_visible_action_output", lambda text, **_kwargs: text
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

    monkeypatch.setattr(orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks)
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
        orchestrator, "_store_prompt_requirement_evaluation", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "_run_missing_tool_call_recovery_workflow", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        orchestrator, "_sanitise_user_visible_action_output", lambda text, **_kwargs: text
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

    monkeypatch.setattr(orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks)
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
        orchestrator, "_store_prompt_requirement_evaluation", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator, "_run_missing_tool_call_recovery_workflow", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        orchestrator, "_sanitise_user_visible_action_output", lambda text, **_kwargs: text
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
    assert "Zero-result or inconclusive retrieval surfaces for this turn:" in context_text
    assert (
        context_text.find("Positive retrieval signals for this turn:")
        < context_text.find("Zero-result or inconclusive retrieval surfaces for this turn:")
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
        "_build_selected_workflow_policy_memory_stage_messages",
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
        if "tool invocation budget" in prompt:
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

    monkeypatch.setattr(orchestrator, "_run_llm_with_fallbacks", _run_llm_with_fallbacks)

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

    assert result.outputs["final_response"] == (
        "Partial final answer grounded in the completed tool results."
    )
    assert len(prompts) == 2
    assert any(event.get("status") == "tool_limit_reached" for event in progress_events)
    aux_entries = [
        entry
        for entry in request.data["aux_llm_calls"]
        if isinstance(entry, Mapping)
        and entry.get("type") == "tool_limit_finalisation"
    ]
    assert aux_entries
