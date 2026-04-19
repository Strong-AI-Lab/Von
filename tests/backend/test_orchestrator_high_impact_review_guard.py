"""Destructive-write confirmation guard tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, cast

from orchestrator_test_harness import build_db_independent_orchestrator
from src.backend.services import settings_service


@dataclass(frozen=True)
class _TransportResult:
    payload: Any
    duration_ms: float


class _DeleteConceptGateway:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "delete_concept": {
                "category": "write",
                "description": "Delete a Vontology concept.",
            }
        }

    def invoke(self, tool_name: str, payload: Mapping[str, Any]) -> _TransportResult:
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        return _TransportResult(payload={"success": True}, duration_ms=1.0)


class _CapturingLLM:
    def __init__(self, responses: Sequence[str]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model=None,
    ):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if not self._responses:
            raise AssertionError("LLM called more times than expected")
        return self._responses.pop(0)


def _tool_call() -> str:
    return (
        '{"action":"call_tool","tool":"delete_concept","payload":'
        '{"concept_id":"#V#guarded_concept"}}'
    )


def _write_request_evidence_response(
    *,
    confirmation_state: str = "low_confidence",
) -> str:
    return (
        '{'
        '"schema_version":"write_tool_request_evidence.v1",'
        '"tool_evidence":['
        "{"
        '"tool_name":"delete_concept",'
        '"request_state":"low_confidence",'
        f'"confirmation_state":"{confirmation_state}",'
        '"rationale":"test stub"'
        "}"
        "]"
        "}"
    )


def test_destructive_write_blocks_pending_confirmation(monkeypatch):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )

    gateway = _DeleteConceptGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM(
        [_tool_call(), _write_request_evidence_response(), "Done.", "Done."]
    )

    result = orchestrator.run(
        prompt="Delete concept #V#guarded_concept.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#michael_witbrock@nao",
    )

    assert not any(
        call.get("tool") == "delete_concept" for call in gateway.invocations
    )
    blocked = [
        record
        for record in result.tool_invocations
        if record.get("tool") == "delete_concept"
    ]
    assert blocked
    assert blocked[0].get("blocked") is True
    assert blocked[0].get("write_policy_blocked_reason") == (
        "destructive_confirmation_required"
    )
    assert blocked[0].get("write_policy_requires_confirmation") is True
    assert "Confirm delete_concept" in str(blocked[0].get("confirmation_prompt"))


def test_destructive_write_allows_recent_confirmation(monkeypatch):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )

    gateway = _DeleteConceptGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM(
        [_tool_call(), _write_request_evidence_response(), "Done.", "Done."]
    )

    result = orchestrator.run(
        prompt="Yes, do it.",
        context=[{"role": "user", "content": "Delete concept #V#guarded_concept."}],
        llm_client=llm,
        model=None,
        user_namespace="#V#michael_witbrock@nao",
    )

    create_invocations = [
        call for call in gateway.invocations if call.get("tool") == "delete_concept"
    ]
    assert len(create_invocations) == 1
    records = [
        record
        for record in result.tool_invocations
        if record.get("tool") == "delete_concept"
    ]
    assert records
    assert all(not record.get("blocked") for record in records)
    assert all(
        record.get("write_policy_risk_class") == "destructive" for record in records
    )
