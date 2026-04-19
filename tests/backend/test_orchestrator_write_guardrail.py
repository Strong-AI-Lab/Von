"""Regression tests for internal MCP write-tool guardrails."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, cast

from orchestrator_test_harness import build_db_independent_orchestrator
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
                "file_path": "data/arxiv_cache/2510.06248.pdf",
                "uri": "swift://von-artifacts/arxiv/2510.06248.pdf",
                "computer_file_copy_concept_id": "#V#computer_file_copy_2510_06248",
                "scholarly_representation": {
                    "attempted": True,
                    "verified": True,
                    "paper_concept_id": "#V#paper_on_arxiv_2510_06248",
                },
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


def _write_request_evidence_response(tool_name: str) -> str:
    return (
        '{'
        '"schema_version":"write_tool_request_evidence.v1",'
        '"tool_evidence":['
        "{"
        f'"tool_name":"{tool_name}",'
        '"request_state":"low_confidence",'
        '"confirmation_state":"low_confidence",'
        '"rationale":"test stub"'
        "}"
        "]"
        "}"
    )


def test_write_guard_allows_download_paper_for_bare_arxiv_url(monkeypatch):
    gateway = _WriteToolGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )

    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"download_paper","payload":{"arxiv_id":"2510.06248"}}',
            _write_request_evidence_response("download_paper"),
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2510.06248",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert any(call["tool"] == "download_paper" for call in gateway.invocations)
    records = [
        record
        for record in result.tool_invocations
        if record.get("tool") == "download_paper"
    ]
    assert records
    assert all(not record.get("blocked") for record in records)
    assert all(
        record.get("write_policy_risk_class") == "additive_low_risk"
        for record in records
    )


def test_write_guard_blocks_download_paper_when_user_explicitly_denies_write(monkeypatch):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )

    gateway = _WriteToolGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )

    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"download_paper","payload":{"arxiv_id":"2510.06248"}}',
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="Do not download or store this: https://arxiv.org/abs/2510.06248",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert not any(
        call.get("tool") == "download_paper" for call in gateway.invocations
    )
    blocked = [
        record
        for record in result.tool_invocations
        if record.get("tool") == "download_paper"
    ]
    assert blocked, "Expected blocked download_paper invocation"
    assert all(record.get("blocked") for record in blocked)
    assert all(
        record.get("write_policy_blocked_reason") == "explicit_write_denial_detected"
        for record in blocked
    )
