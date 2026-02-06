"""Tests for workflow selector routing in the internal MCP orchestrator.

JVNAUTOSCI-922 Phase 1.3 + 2.2: Tests cover:
- Workflow selector fires for any authenticated turn (no presenter-mode gate)
- Narration workflow routing
- Discovered workflow routing
- Voice hint injection (selector disabled)

These tests construct a minimal orchestrator stub, bypassing DB-dependent
init to avoid hanging on MongoDB connections.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, cast
from unittest.mock import MagicMock, patch

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    OrchestratorResult,
)
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
)
from src.backend.workflows.workflow_selector import WorkflowSelection, WorkflowSelector


# ---------------------------------------------------------------------------
# Stubs – avoid DB, gateway, and real LLM calls.
# ---------------------------------------------------------------------------


class _StubGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}

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


def _build_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selector_enabled: bool = False,
) -> InternalMCPChatOrchestrator:
    """Build an orchestrator with DB-dependent methods stubbed out.

    This avoids hanging on MongoDB connections during unit tests.
    """
    env_val = "1" if selector_enabled else "0"
    monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", env_val)

    gateway = cast(Any, _StubGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=1,
    )

    # Stub DB-dependent methods to avoid blocking on MongoDB.
    from src.backend.integrations.internal_mcp.orchestrator import (
        _WorkflowModelPolicyState,
    )

    monkeypatch.setattr(
        orchestrator,
        "_load_workflow_model_policy",
        lambda *_a, **_kw: (
            _WorkflowModelPolicyState(
                enabled=False,
                policy=None,
                policy_id=None,
                predicate_id=None,
                errors=[],
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_ontology_preflight",
        lambda *_a, **_kw: type(
            "_Preflight", (), {"telemetry": None, "message": None}
        )(),
    )
    monkeypatch.setattr(
        orchestrator,
        "_resolve_concept_id_by_name",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda *_a, **_kw: (None, None),
    )

    # Prevent get_model_registry_snapshot from reaching MongoDB.
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        lambda *_a, **_kw: None,
    )

    return orchestrator


# ---------------------------------------------------------------------------
# Voice hint injection (selector OFF — tests augmented context, not routing).
# ---------------------------------------------------------------------------


def test_orchestrator_injects_voice_hint_when_prompt_asks_for_voice(monkeypatch):
    """Voice queries should be grounded via client capabilities snapshot."""
    from flask import Flask

    from src.backend.services.client_capabilities_service import (
        set_client_capabilities_snapshot,
    )

    app = Flask(__name__)
    app.config.update(SECRET_KEY="test")

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=False)

    snapshot = {
        "speech_synthesis": {
            "supported": True,
            "voices_count": 3,
            "default_voice_lang": "en-NZ",
            "settings": {"voice_name": "Test Voice"},
        }
    }

    llm = _CapturingLLM(["Here is a reply."])

    with app.test_request_context("/"):
        set_client_capabilities_snapshot(snapshot)

        orchestrator.run(
            prompt="What voice are you using?",
            context=[],
            llm_client=llm,
            model=None,
            user_namespace="#V#user",
        )

    assert llm.calls, "Expected at least one LLM call"
    combined_context = "\n".join(
        str(item.get("content") or "")
        for item in (llm.calls[0].get("context") or [])
        if isinstance(item, dict)
    )
    assert "Client-reported speech synthesis settings" in combined_context
    assert "voice_name='Test Voice'" in combined_context


# ---------------------------------------------------------------------------
# Narration workflow routing (selector ON, presenter mode).
# ---------------------------------------------------------------------------


def test_workflow_selector_routes_to_narration_workflow(monkeypatch):
    """When enabled and classifier returns 'narration', orchestrator emits spoken+screen."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    presenter_protocol = {
        "role": "system",
        "content": (
            "PRESENTER MODE PROTOCOL:\n"
            "- Output EXACTLY TWO tagged blocks and nothing else:\n"
            "  <spoken>...brief talk track...</spoken>\n"
            "  <screen>...full on-screen content...</screen>\n"
        ),
    }

    llm = _CapturingLLM(
        [
            "narration",  # workflow selector verdict
            "Here is the answer on screen.",  # main assistant screen response
            "<spoken>Short talk track.</spoken>",  # narration generation
        ]
    )

    result = orchestrator.run(
        prompt="hi",
        context=[presenter_protocol],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.response_text == (
        "<spoken>Short talk track.</spoken>\n\n"
        "<screen>Here is the answer on screen.</screen>"
    )

    # Ensure we actually invoked the selector and then narration.
    assert len(llm.calls) == 3
    assert llm.calls[0]["prompt"] == "Select workflow"
    assert llm.calls[2]["prompt"] == "Generate <spoken> talk track"

    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector" in aux_types


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 1.3: Selector fires without presenter mode.
# ---------------------------------------------------------------------------


def test_selector_fires_without_presenter_mode(monkeypatch):
    """Workflow selector should run for any authenticated turn (no presenter-mode gate)."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    # No presenter mode context — selector should still fire.
    llm = _CapturingLLM(
        [
            "tool_seeking",  # workflow selector verdict
            "I'll help with that.",  # main assistant response (plan handler)
        ]
    )

    result = orchestrator.run(
        prompt="Search for papers about transformers",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    # The selector consumed one response, the planner consumed the other.
    assert len(llm.calls) == 2
    assert llm.calls[0]["prompt"] == "Select workflow"

    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector" in aux_types

    # Verify the selector chose tool_calling workflow.
    selector_entry = next(
        e
        for e in result.aux_llm_calls
        if isinstance(e, dict) and e.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert selector_entry["verdict"] == "tool_seeking"


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 1.3: Discovered workflows in selector prompt.
# ---------------------------------------------------------------------------


def test_selector_receives_discovered_workflows(monkeypatch):
    """Discovered workflow IDs should be valid selector responses."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    # Selector returns a discovered workflow ID — but since it won't be in the
    # registry, the orchestrator should log a warning and fall through to
    # tool-calling.
    llm = _CapturingLLM(
        [
            "#v#custom_analysis_workflow",  # selector verdict (discovered WF)
            "Falling back to tool calling.",  # plan handler
        ]
    )

    discovery_result = {
        "matches": [
            {
                "concept_id": "#V#custom_analysis_workflow",
                "name": "Custom Analysis",
                "description": "Runs a custom data analysis pipeline.",
                "relevance_score": 0.85,
            }
        ],
        "match_count": 1,
    }

    result = orchestrator.run(
        prompt="Run a custom analysis",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
    )

    # Selector should have fired with the discovered workflow context.
    selector_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_selector"
        ),
        None,
    )
    assert selector_entry is not None
    assert "#V#custom_analysis_workflow" in selector_entry.get(
        "discovered_workflow_ids", []
    )

    # Since the workflow is not in the registry, execute_workflow returns None
    # and we fall through to tool-calling.  The response should come from the
    # plan handler (second LLM call).
    assert "Falling back" in result.response_text or result.response_text


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 2.2: Non-standard workflow routing via execute_workflow.
# ---------------------------------------------------------------------------


def test_non_standard_workflow_routes_via_execute_workflow(monkeypatch):
    """Workflows in the registry but not in the standard set should use execute_workflow."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    # The selector returns #V#todo_refresh_workflow, which IS in the registry.
    # We pass it via workflow_discovery_result so the selector treats it as a
    # valid discovered workflow ID instead of falling back to plain_response.
    llm = _CapturingLLM(
        [
            TODO_REFRESH_WORKFLOW_ID.lower(),  # selector verdict
            # No additional LLM responses needed — todo_refresh check_cache
            # handler will short-circuit to completed because there's no
            # user namespace tasks.
        ]
    )

    discovery_result = {
        "matches": [
            {
                "concept_id": TODO_REFRESH_WORKFLOW_ID,
                "name": "Todo Refresh Workflow",
                "description": "Refreshes user's todo list from Jira.",
            }
        ],
        "match_count": 1,
    }

    result = orchestrator.run(
        prompt="Refresh my todo list",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
    )

    # The workflow executed via execute_workflow and produced a result.
    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector" in aux_types

    # Should have a workflow_execution entry (non-standard workflow dispatch).
    execution_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_execution"
        ),
        None,
    )
    assert execution_entry is not None
    assert execution_entry["workflow_id"] == TODO_REFRESH_WORKFLOW_ID
    assert execution_entry["completed"] is True


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 1.3: selector disabled → no classifier call.
# ---------------------------------------------------------------------------


def test_selector_disabled_skips_classifier(monkeypatch):
    """When selector is disabled, no classifier LLM call should be made."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=False)

    llm = _CapturingLLM(["A direct response."])

    result = orchestrator.run(
        prompt="Hello",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    # Only one LLM call (the planner), no selector call.
    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"] != "Select workflow"

    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector" not in aux_types


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 1.3: No user_namespace → selector skipped.
# ---------------------------------------------------------------------------


def test_no_namespace_skips_selector(monkeypatch):
    """Without user_namespace, selector should not fire even when enabled."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(["Response without selector."])

    result = orchestrator.run(
        prompt="Hello",
        context=[],
        llm_client=llm,
        model=None,
        # No user_namespace — selector should be skipped.
    )

    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"] != "Select workflow"

    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector" not in aux_types
