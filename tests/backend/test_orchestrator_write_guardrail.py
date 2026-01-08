"""Regression tests for internal MCP write-tool guardrails."""

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


class _WriteToolGateway:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "download_paper": {
                "category": "write",
                "description": "Download an arXiv paper and store as an artefact",
            }
        }

    def invoke(self, tool_name: str, payload: Mapping[str, Any]) -> _TransportResult:
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        return _TransportResult(
            payload={
                "file_path": "data/arxiv_cache/2506.16596.pdf",
                "uri": "swift://von-artifacts/arxiv/2506.16596.pdf",
            },
            duration_ms=1.23,
        )


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


def test_write_guard_allows_download_paper_when_user_requests_artefact_download():
    """download_paper should not be blocked when the user explicitly asks for it."""

    gateway = _WriteToolGateway()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )

    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"download_paper","payload":{"arxiv_id":"2506.16596"}}',
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="Download arXiv:2506.16596 and save it as an artefact.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert any(call["tool"] == "download_paper" for call in gateway.invocations)
    assert all(
        not record.get("blocked")
        for record in result.tool_invocations
        if record.get("tool") == "download_paper"
    )


def test_write_guard_blocks_download_paper_without_explicit_request(monkeypatch):
    """download_paper should be blocked when the user does not ask for storage."""

    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )

    gateway = _WriteToolGateway()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )

    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"download_paper","payload":{"arxiv_id":"2506.16596"}}',
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="Summarise arXiv:2506.16596.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert gateway.invocations == []
    blocked = [
        record
        for record in result.tool_invocations
        if record.get("tool") == "download_paper"
    ]
    assert blocked, "Expected blocked download_paper invocation"
    assert all(record.get("blocked") for record in blocked)
    assert all("read-only" in record.get("error", "") for record in blocked)
