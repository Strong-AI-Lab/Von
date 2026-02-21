"""High-impact Vontology write guard tests (JVNAUTOSCI-925)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.services import settings_service


@dataclass(frozen=True)
class _TransportResult:
    payload: Any
    duration_ms: float


class _CreateConceptsGateway:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "create_concepts": {
                "category": "write",
                "description": "Create one or more Vontology concepts.",
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
        '{"action":"call_tool","tool":"create_concepts","payload":'
        '{"parent_id":"#V#thing","concepts":[{"name":"guarded concept"}]}}'
    )


def test_high_impact_guard_blocks_without_explicit_review_approval(monkeypatch):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: True
    )
    monkeypatch.setattr(
        settings_service,
        "get_require_human_review_for_high_impact_kb_writes",
        lambda: True,
    )

    gateway = _CreateConceptsGateway()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM([_tool_call(), "Done."])

    result = orchestrator.run(
        prompt="Create a concept in the Vontology for guarded extension.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#michael_witbrock@nao",
    )

    create_invocations = [
        call for call in gateway.invocations if call.get("tool") == "create_concepts"
    ]
    assert create_invocations == []
    blocked = [
        record
        for record in result.tool_invocations
        if record.get("tool") == "create_concepts"
    ]
    assert blocked
    assert blocked[0].get("blocked") is True
    assert (
        blocked[0].get("write_policy_reason")
        == "high_impact_kb_write_requires_human_review"
    )


def test_high_impact_guard_blocks_when_namespace_missing(monkeypatch):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: True
    )
    monkeypatch.setattr(
        settings_service,
        "get_require_human_review_for_high_impact_kb_writes",
        lambda: True,
    )

    gateway = _CreateConceptsGateway()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM([_tool_call(), "Done."])

    result = orchestrator.run(
        prompt="Approved: proceed with the Vontology write.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace=None,
    )

    create_invocations = [
        call for call in gateway.invocations if call.get("tool") == "create_concepts"
    ]
    assert create_invocations == []
    blocked = [
        record
        for record in result.tool_invocations
        if record.get("tool") == "create_concepts"
    ]
    assert blocked
    assert blocked[0].get("blocked") is True
    assert (
        blocked[0].get("write_policy_reason")
        == "high_impact_kb_write_requires_namespace"
    )


def test_high_impact_guard_allows_namespaced_approved_write(monkeypatch):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: True
    )
    monkeypatch.setattr(
        settings_service,
        "get_require_human_review_for_high_impact_kb_writes",
        lambda: True,
    )

    gateway = _CreateConceptsGateway()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM([_tool_call(), "Done."])

    result = orchestrator.run(
        prompt="Approved. Go ahead with the Vontology concept write now.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#michael_witbrock@nao",
    )

    create_invocations = [
        call for call in gateway.invocations if call.get("tool") == "create_concepts"
    ]
    assert len(create_invocations) == 1
    invoked = create_invocations[0]
    records = [
        record
        for record in result.tool_invocations
        if record.get("tool") == "create_concepts"
    ]
    assert records
    assert all(not record.get("blocked") for record in records)
