from __future__ import annotations

import time
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


def _bare_orchestrator() -> InternalMCPChatOrchestrator:
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    orchestrator._provider_probe_cache = {}
    orchestrator._provider_probe_cache_max_entries = 32
    orchestrator._provider_probe_cooldown_seconds = 0
    return orchestrator


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
