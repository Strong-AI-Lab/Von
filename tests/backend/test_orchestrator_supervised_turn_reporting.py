from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)


class _DummyLLM:
    def generate(self, prompt, context=None, model=None):  # pragma: no cover
        raise AssertionError("LLM should not be called in this regression test")


def test_supervised_turn_preserves_gate_reported_response_when_follow_up_is_required(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, None))
    expected_response = (
        "Execution status: mutation may have run but verification is inconclusive."
    )
    monkeypatch.setattr(
        orchestrator,
        "execute_workflow",
        lambda *args, **kwargs: SimpleNamespace(
            completed=False,
            final_state="failed",
            error="follow_up_required",
            data={
                "response_text": expected_response,
                "completion_gate_decision": "partial",
                "completion_gate_requires_follow_up": True,
                "completion_gate_safe_to_claim_completion": False,
                "completion_gate_evidence_payload": {
                    "terminal_outcome": "attempt_budget_exhausted"
                },
            },
        ),
    )

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="Represent this paper.",
        context=None,
        llm_client=_DummyLLM(),
        model="test-model",
    )

    assert result.response_text == expected_response
    assert result.completion_gate_verdict is not None
    assert result.completion_gate_verdict.get("decision") == "partial"
    assert result.completion_gate_verdict.get("requires_follow_up") is True


def test_supervised_turn_still_fails_closed_without_gate_reported_response(
    monkeypatch,
) -> None:
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, None))
    monkeypatch.setattr(
        orchestrator,
        "execute_workflow",
        lambda *args, **kwargs: SimpleNamespace(
            completed=False,
            final_state="failed",
            error="workflow_failed",
            data={},
        ),
    )

    result = orchestrator.execute_conversation_turn_supervised(
        prompt="Represent this paper.",
        context=None,
        llm_client=_DummyLLM(),
        model="test-model",
    )

    assert (
        result.response_text
        == "I couldn't complete that request because the authoritative "
        "conversation-turn workflow failed."
    )
