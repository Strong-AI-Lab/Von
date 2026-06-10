"""Regression tests for JVNAUTOSCI-2133 workflow_stage_id threading.

Verifies that when a caller passes ``workflow_stage_id`` to the LLM
chokepoints (``_run_llm_with_fallbacks`` / ``_run_llm_with_tools_fallbacks``),
the canonical workflow stage label is propagated to:

* every emit_progress event,
* every record_llm_call payload,
* every aux_log entry,

without overriding the orchestrator-internal ``stage`` field. This is the
authority surface von_routes uses to bucket LLM exchange diagnostics into the
correct workflow stage row (Bug B in the JVNAUTOSCI-2133 diagnosis: the
chokepoint emits ``stage="tool_call"`` while the workflow stage_id is
``tool_plan``).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    _ModelCandidate,
    _WorkflowModelPolicyState,
)


class _SuccessfulClient:
    def generate(self, *_args: Any, **_kwargs: Any) -> str:
        return "ok"


class _FailingClient:
    def generate(self, *_args: Any, **_kwargs: Any) -> str:
        raise TimeoutError("LLM timed out after 120s")


def _policy_state() -> _WorkflowModelPolicyState:
    return _WorkflowModelPolicyState(
        enabled=True,
        policy={"stages": {"summariser": {"primary": "ollama:granite3.3:2b"}}},
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


def test_run_llm_with_fallbacks_threads_workflow_stage_id_into_telemetry(
    monkeypatch,
) -> None:
    orchestrator = _bare_orchestrator()
    candidate = _ModelCandidate(
        provider="ollama",
        model="granite3.3:2b",
        raw="ollama:granite3.3:2b",
        source="policy",
        host="http://localhost:11434",
    )

    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [candidate],
    )

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **_kwargs: Any,
    ) -> tuple[Any, str, Mapping[str, Any]]:
        return (
            _SuccessfulClient(),
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

    progress_events: list[dict[str, Any]] = []
    llm_calls_log: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []

    def _record_llm_call(**payload: Any) -> None:
        recorded_calls.append(dict(payload))

    response, _model_name, _telemetry = orchestrator._run_llm_with_fallbacks(
        stage="summariser",
        prompt="finalise tool result",
        context=[],
        default_client=object(),
        default_model="granite3.3:2b",
        policy_state=_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=llm_calls_log,
        aux_log=aux_log,
        record_llm_call=_record_llm_call,
        emit_progress=lambda payload: progress_events.append(dict(payload)),
        workflow_stage_id="screen_backfill",
    )

    assert response == "ok"

    # Every progress event must carry the canonical workflow_stage_id while
    # leaving the orchestrator-internal `stage` label untouched.
    assert progress_events, "expected at least one progress event"
    for event in progress_events:
        assert (
            event.get("workflow_stage_id") == "screen_backfill"
        ), f"missing workflow_stage_id on progress event: {event}"
        # Sanity-check: chokepoint-internal stage label is preserved (not
        # overridden by workflow_stage_id).
        if "stage" in event:
            assert event["stage"] == "summariser"

    # Every record_llm_call payload must carry workflow_stage_id.
    assert recorded_calls, "expected at least one record_llm_call payload"
    for payload in recorded_calls:
        assert payload.get("workflow_stage_id") == "screen_backfill"

    # Aux log entries from the chokepoint should also carry workflow_stage_id.
    chokepoint_aux = [
        entry
        for entry in aux_log
        if isinstance(entry, Mapping)
        and entry.get("type") == "workflow_model_policy_stage"
    ]
    assert chokepoint_aux, "expected at least one chokepoint aux_log entry"
    for entry in chokepoint_aux:
        assert entry.get("workflow_stage_id") == "screen_backfill"


def test_run_llm_with_fallbacks_omits_workflow_stage_id_when_not_provided(
    monkeypatch,
) -> None:
    """When workflow_stage_id is not threaded, telemetry must not invent one.

    This guards against silently assigning a stage_id that would mismatch the
    workflow row and re-introduce the bug from a different direction.
    """

    orchestrator = _bare_orchestrator()
    candidate = _ModelCandidate(
        provider="ollama",
        model="granite3.3:2b",
        raw="ollama:granite3.3:2b",
        source="policy",
        host="http://localhost:11434",
    )

    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [candidate],
    )

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **_kwargs: Any,
    ) -> tuple[Any, str, Mapping[str, Any]]:
        return (
            _SuccessfulClient(),
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

    progress_events: list[dict[str, Any]] = []
    aux_log: list[Mapping[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []

    def _record_llm_call(**payload: Any) -> None:
        recorded_calls.append(dict(payload))

    orchestrator._run_llm_with_fallbacks(
        stage="summariser",
        prompt="finalise",
        context=[],
        default_client=object(),
        default_model="granite3.3:2b",
        policy_state=_policy_state(),
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        llm_calls_log=[],
        aux_log=aux_log,
        record_llm_call=_record_llm_call,
        emit_progress=lambda payload: progress_events.append(dict(payload)),
    )

    for event in progress_events:
        assert "workflow_stage_id" not in event or not event.get("workflow_stage_id")
    for payload in recorded_calls:
        assert "workflow_stage_id" not in payload or payload.get(
            "workflow_stage_id"
        ) in (None, "")


def test_run_llm_with_fallbacks_records_failed_exchange_blob(monkeypatch) -> None:
    orchestrator = _bare_orchestrator()
    candidate = _ModelCandidate(
        provider="ollama",
        model="qwen3:8b",
        raw="ollama:qwen3:8b",
        source="policy",
        host="http://localhost:11434",
    )

    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **_kwargs: [candidate],
    )

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **_kwargs: Any,
    ) -> tuple[Any, str, Mapping[str, Any]]:
        return (
            _FailingClient(),
            "qwen3:8b",
            {
                "provider": "ollama",
                "model": "qwen3:8b",
                "raw": candidate.raw,
                "source": candidate.source,
                "host": "http://localhost:11434",
            },
        )

    captured_blobs: list[dict[str, Any]] = []

    def _capture_llm_exchange_blob(**payload: Any) -> dict[str, Any]:
        captured_blobs.append(dict(payload))
        return {
            "schema_version": "llm_exchange_blob.v1",
            "backend": "local",
            "key": "llm_exchanges/failure.json.gz",
            "truncated": False,
        }

    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        _create_client_for_candidate,
    )
    monkeypatch.setattr(
        orchestrator,
        "_capture_llm_exchange_blob",
        _capture_llm_exchange_blob,
    )

    progress_events: list[dict[str, Any]] = []
    recorded_calls: list[dict[str, Any]] = []

    def _record_llm_call(**payload: Any) -> None:
        recorded_calls.append(dict(payload))

    with pytest.raises(TimeoutError):
        orchestrator._run_llm_with_fallbacks(
            stage="response_finalising",
            prompt="Captured failing prompt.",
            context=[{"role": "system", "content": "Captured context."}],
            default_client=object(),
            default_model="qwen3:8b",
            policy_state=_policy_state(),
            registry_snapshot=None,
            user_concept_id=None,
            org_concept_id=None,
            llm_calls_log=[],
            aux_log=[],
            record_llm_call=_record_llm_call,
            emit_progress=lambda payload: progress_events.append(dict(payload)),
            workflow_stage_id="response_finalising",
        )

    assert recorded_calls, "expected failed LLM call telemetry"
    failed_call = recorded_calls[0]
    assert failed_call["status"] == "failed"
    assert failed_call["success"] is False
    assert failed_call["error_class"] == "TimeoutError"
    assert failed_call["failure_kind"] == "candidate_error"
    assert failed_call["exchange_blob_ref"]["key"] == "llm_exchanges/failure.json.gz"

    assert captured_blobs, "expected failed exchange blob capture"
    blob_payload = captured_blobs[0]
    assert blob_payload["prompt"] == "Captured failing prompt."
    assert blob_payload["context"] == [
        {"role": "system", "content": "Captured context."}
    ]
    assert blob_payload["response"] is None
    assert blob_payload["extra"]["status"] == "failed"
    assert blob_payload["extra"]["failure"]["error_class"] == "TimeoutError"

    end_events = [
        event for event in progress_events if event.get("status") == "llm_call_end"
    ]
    assert end_events
    assert end_events[-1]["success"] is False
    assert end_events[-1]["error_class"] == "TimeoutError"
