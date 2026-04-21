"""Focused regressions for bounded workflow-contract resolution."""

from __future__ import annotations

from typing import Any, Mapping, cast

from src.backend.workflows.definitions import (
    CHAT_NARRATION_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)
from orchestrator_test_harness import build_db_independent_orchestrator


class _StubGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {
            "renderer_resolve_applicability": {"category": "read"},
        }

    def invoke(self, _tool_name: str, _payload: Mapping[str, Any]):
        raise AssertionError("Gateway should not be invoked in this test")


def test_contract_resolution_uses_bounded_candidates_without_registry_scan(
    monkeypatch,
):
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _StubGateway()),
        selector_enabled=True,
        max_tool_invocations=1,
    )
    monkeypatch.setattr(
        orchestrator._workflow_registry,
        "all_workflow_ids",
        lambda: (_ for _ in ()).throw(
            AssertionError("contract resolution should not scan all workflow ids")
        ),
    )

    resolved = orchestrator._resolve_workflow_id_for_action_contract(
        required_action_ids=frozenset(
            {
                "tool_calling.preflight_requirements",
                "tool_calling.respond",
                "workflow_invoke_subworkflow",
                "turn_execution.completion_gate",
            }
        ),
        preferred_workflow_id=TODO_REFRESH_WORKFLOW_ID,
        candidate_workflow_ids=(TODO_REFRESH_WORKFLOW_ID, TOOL_CALLING_WORKFLOW_ID),
    )

    assert resolved == TOOL_CALLING_WORKFLOW_ID


def test_contract_resolution_can_use_explicit_canonical_fallback(monkeypatch):
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _StubGateway()),
        selector_enabled=True,
        max_tool_invocations=1,
    )
    monkeypatch.setattr(
        orchestrator._workflow_registry,
        "all_workflow_ids",
        lambda: (_ for _ in ()).throw(
            AssertionError("contract resolution should not scan all workflow ids")
        ),
    )

    resolved = orchestrator._resolve_workflow_id_for_action_contract(
        required_action_ids=frozenset(
            {
                "narration.classify",
                "narration.select_prompts",
                "narration.render",
                "narration.emit_audio",
            }
        ),
        fallback_workflow_ids=(CHAT_NARRATION_WORKFLOW_ID,),
    )

    assert resolved == CHAT_NARRATION_WORKFLOW_ID
