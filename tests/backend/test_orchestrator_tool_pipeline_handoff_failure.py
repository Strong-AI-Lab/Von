"""Focused regression tests for tool-pipeline handoff failure telemetry."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, cast

import src.backend.services.workflow_selection_policy_service as policy_module
from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.workflows.definitions import TOOL_CALLING_WORKFLOW_ID
from orchestrator_test_harness import build_db_independent_orchestrator


class _StubGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {
            "renderer_resolve_applicability": {"category": "read"},
        }

    def invoke(self, _tool_name: str, _payload: Mapping[str, Any]):
        raise AssertionError("Gateway should not be invoked in this test")


class _CapturingLLM:
    """Test double that returns canned responses and records calls."""

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


def _build_orchestrator(monkeypatch) -> InternalMCPChatOrchestrator:
    return build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _StubGateway()),
        selector_enabled=True,
        max_tool_invocations=1,
    )


def test_tool_pipeline_setup_failure_emits_local_handoff_boundary(monkeypatch):
    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch)

    def _raise_setup_failure() -> bool:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(
        orchestrator,
        "_get_auto_proceed_minimal_imposition_enabled",
        _raise_setup_failure,
    )

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])

    result = orchestrator.run(
        prompt="Use tools to inspect the workflow runtime.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert "tool-pipeline handoff failed before tool execution could begin" in (
        result.response_text
    )
    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    assert [entry.get("boundary") for entry in dispatch_boundaries[-2:]] == [
        "workflow_handoff",
        "workflow_terminal",
    ]
    assert dispatch_boundaries[-2].get("status") == "failed"
    assert dispatch_boundaries[-2].get("reason") == "tool_pipeline_setup_exception"
    assert dispatch_boundaries[-2].get("error_class") == "RuntimeError"
    assert dispatch_boundaries[-1].get("status") == "failed"
    assert dispatch_boundaries[-1].get("reason") == "tool_pipeline_setup_exception"
    assert result.workflow_routing is not None
    assert result.workflow_routing.routing_duration_ms is not None
    assert result.workflow_routing.routing_duration_ms >= 0
