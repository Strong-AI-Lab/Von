"""Tests for workflow selector routing in the internal MCP orchestrator.

JVNAUTOSCI-922 Phase 1.3 + 2.2: Tests cover:
- Workflow selector fires for any authenticated turn (no presenter-mode gate)
- Narration workflow routing
- Discovered workflow routing
- Voice hint injection (selector disabled)

JVNAUTOSCI-825: Additional tests cover:
- Plain response routing (skips tool-calling overhead)
- WorkflowRoutingInfo presence in results
- Routing timing telemetry
- Selector default-on behaviour

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
    WorkflowRoutingInfo,
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


class _InvokeResult:
    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)
        self.duration_ms = 0.0


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

    # Keep this unit-test harness DB-independent even if the production
    # registry factory discovers workflows from Vontology.
    from src.backend.workflows import WorkflowRegistry, register_default_workflows

    def _build_test_registry() -> WorkflowRegistry:
        registry = WorkflowRegistry()
        register_default_workflows(registry)
        return registry

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.build_workflow_registry",
        _build_test_registry,
    )

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


def test_renderer_applicability_can_enable_narration_when_flag_enabled(monkeypatch):
    """When enabled, renderer applicability can request narration on tool-calling turns."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#narration_renderer",
    )

    renderer_invocations: list[dict[str, Any]] = []

    def _invoke(tool_name: str, payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        renderer_invocations.append({"tool_name": tool_name, "payload": dict(payload)})
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#narration_renderer",
                        "renderer_type": "narration",
                        "modalities": ["narrated_audio"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    llm = _CapturingLLM(
        [
            "tool_seeking",  # workflow selector verdict
            "Here is the answer on screen.",  # tool-calling final response
            "<spoken>Short talk track.</spoken>",  # narration generation
        ]
    )

    result = orchestrator.run(
        prompt="Explain this briefly",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.response_text == (
        "<spoken>Short talk track.</spoken>\n\n"
        "<screen>Here is the answer on screen.</screen>"
    )
    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("render_mode") == "spoken+screen"
    assert result.render_plan.get("should_narrate") is True
    assert len(renderer_invocations) == 1
    assert (
        renderer_invocations[0]["payload"]["renderer_definition_concept_ids"]
        == ["#V#narration_renderer"]
    )

    renderer_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "renderer_applicability_routing"
        ),
        None,
    )
    assert renderer_entry is not None
    assert renderer_entry.get("enabled") is True
    assert renderer_entry.get("attempted") is True
    assert renderer_entry.get("success") is True
    assert renderer_entry.get("should_narrate") is True
    assert renderer_entry.get("render_mode") == "spoken+screen"
    assert "#V#narration_renderer" in renderer_entry.get("selected_renderer_ids", [])


def test_renderer_applicability_error_preserves_selector_narration(monkeypatch):
    """Renderer applicability failures must not block selector-driven narration."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#narration_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name == "renderer_resolve_applicability":
            raise RuntimeError("simulated renderer tool failure")
        raise AssertionError(f"Unexpected tool invocation: {tool_name}")

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

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

    renderer_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "renderer_applicability_routing"
        ),
        None,
    )
    assert renderer_entry is not None
    assert renderer_entry.get("enabled") is True
    assert renderer_entry.get("attempted") is True
    assert renderer_entry.get("success") is False
    assert renderer_entry.get("reason") == "tool_error"


def test_renderer_applicability_flag_off_preserves_default_rendering(monkeypatch):
    """With the feature flag off, routing should remain unchanged and skip tool calls."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.delenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", raising=False)
    monkeypatch.delenv("VON_RENDERER_APPLICABILITY_DEFINITION_IDS", raising=False)

    def _invoke(_tool_name: str, _payload: Mapping[str, Any]):
        raise AssertionError("Renderer applicability tool must not be invoked when disabled")

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    llm = _CapturingLLM(
        [
            "tool_seeking",
            "Here is the answer on screen.",
        ]
    )

    result = orchestrator.run(
        prompt="Explain this briefly",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.response_text == "Here is the answer on screen."
    assert result.render_plan is None
    assert len(llm.calls) == 2
    assert all(
        not (isinstance(entry, dict) and entry.get("type") == "renderer_applicability_routing")
        for entry in result.aux_llm_calls
    )


def test_renderer_applicability_non_narration_renderer_keeps_screen_only(monkeypatch):
    """Successful non-narration renderer selection should keep screen-only output."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#table_renderer",
                        "renderer_type": "table",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    llm = _CapturingLLM(
        [
            "tool_seeking",
            "Here is the answer on screen.",
        ]
    )

    result = orchestrator.run(
        prompt="Explain this briefly",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.response_text == "Here is the answer on screen."
    assert len(llm.calls) == 2
    renderer_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "renderer_applicability_routing"
        ),
        None,
    )
    assert renderer_entry is not None
    assert renderer_entry.get("success") is True
    assert renderer_entry.get("should_narrate") is False
    assert renderer_entry.get("render_mode") == "screen_only"
    assert renderer_entry.get("selected_renderer_types") == ["table"]


def test_renderer_applicability_uses_concept_backed_request_when_available(monkeypatch):
    """When tool invocations carry concept IDs, resolver request should be concept-backed."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#table_renderer",
    )

    renderer_requests: list[dict[str, Any]] = []

    def _invoke(tool_name: str, payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        renderer_requests.append(dict(payload))
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#table_renderer",
                        "renderer_type": "table",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Here is the answer on screen.",
                "tool_messages": [],
                "invocations": [
                    {
                        "tool": "get_task",
                        "payload": {"concept_id": "#V#task_123"},
                    }
                ],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(
        [
            "tool_seeking",
        ]
    )

    result = orchestrator.run(
        prompt="Summarise this task",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.response_text == "Here is the answer on screen."
    assert len(renderer_requests) == 1
    request_payload = renderer_requests[0]["request_payload"]
    assert request_payload["object_kind"] == "concept"
    assert request_payload["concept_id"] == "#V#task_123"
    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("request_payload_object_kind") == "concept"
    assert result.render_plan.get("request_payload_selected_concept_id") == "#V#task_123"

    renderer_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "renderer_applicability_routing"
        ),
        None,
    )
    assert renderer_entry is not None
    assert renderer_entry.get("request_payload_object_kind") == "concept"
    assert renderer_entry.get("request_payload_selected_concept_id") == "#V#task_123"


def test_renderer_applicability_missing_definitions_falls_back_screen_only(monkeypatch):
    """Enabled routing with missing renderer definitions should fail closed to screen-only."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.delenv("VON_RENDERER_APPLICABILITY_DEFINITION_IDS", raising=False)

    def _invoke(_tool_name: str, _payload: Mapping[str, Any]):
        raise AssertionError("Renderer applicability tool should not be invoked")

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    llm = _CapturingLLM(
        [
            "tool_seeking",
            "Here is the answer on screen.",
        ]
    )

    result = orchestrator.run(
        prompt="Explain this briefly",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.response_text == "Here is the answer on screen."
    renderer_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "renderer_applicability_routing"
        ),
        None,
    )
    assert renderer_entry is not None
    assert renderer_entry.get("enabled") is True
    assert renderer_entry.get("attempted") is False
    assert renderer_entry.get("success") is False
    assert renderer_entry.get("reason") == "renderer_definition_ids_missing"
    assert renderer_entry.get("render_mode") == "screen_only"
    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("render_mode") == "screen_only"
    assert result.render_plan.get("reason") == "renderer_definition_ids_missing"


def test_renderer_applicability_multimodal_selection_sets_spoken_plus_screen(monkeypatch):
    """Multimodal selection should expose screen+spoken render mode deterministically."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#table_renderer,#V#narration_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#table_renderer",
                        "renderer_type": "table",
                        "modalities": ["visual"],
                    },
                    {
                        "renderer_id": "#V#narration_renderer",
                        "renderer_type": "narration",
                        "modalities": ["narrated_audio", "textual"],
                    },
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    llm = _CapturingLLM(
        [
            "tool_seeking",
            "Here is the answer on screen.",
            "<spoken>Short talk track.</spoken>",
        ]
    )

    result = orchestrator.run(
        prompt="Explain this briefly",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.response_text == (
        "<spoken>Short talk track.</spoken>\n\n"
        "<screen>Here is the answer on screen.</screen>"
    )
    renderer_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "renderer_applicability_routing"
        ),
        None,
    )
    assert renderer_entry is not None
    assert renderer_entry.get("render_mode") == "spoken+screen"
    assert renderer_entry.get("should_narrate") is True
    assert renderer_entry.get("selected_renderer_ids") == [
        "#V#table_renderer",
        "#V#narration_renderer",
    ]
    assert renderer_entry.get("selected_modalities") == [
        "visual",
        "narrated_audio",
        "textual",
    ]


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
    """Policy-unsafe discovered workflows should be excluded from selector context."""

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
    assert selector_entry.get("discovered_workflow_ids", []) == []
    assert selector_entry.get("discovery_candidate_count") == 1
    assert selector_entry.get("discovery_excluded_count") == 1

    # Since the workflow is not in the registry, execute_workflow returns None
    # and we fall through to tool-calling.  The response should come from the
    # plan handler (second LLM call).
    assert "Falling back" in result.response_text or result.response_text


def test_non_executable_discovered_workflow_filtered_by_default(monkeypatch):
    """Non-executable discovered workflows should not reach selector candidates by default."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_NON_EXECUTABLE", "0")

    llm = _CapturingLLM(
        [
            TODO_REFRESH_WORKFLOW_ID.lower(),  # selector verdict (ignored)
            "Fallback response.",
        ]
    )

    discovery_result = {
        "candidates": [
            {
                "concept_id": TODO_REFRESH_WORKFLOW_ID,
                "name": "Todo Refresh Workflow",
                "description": "Refreshes user's todo list from Jira.",
                "is_executable": False,
                "executability_reason": "graph_incomplete",
            }
        ],
        "candidate_count": 1,
    }

    result = orchestrator.run(
        prompt="Refresh my todo list",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
    )

    selector_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_selector"
        ),
        None,
    )
    assert selector_entry is not None
    assert selector_entry.get("discovered_workflow_ids", []) == []
    assert selector_entry.get("discovery_excluded_count") == 1

    execution_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_execution"
        ),
        None,
    )
    assert execution_entry is None


def test_non_executable_discovered_workflow_can_be_overridden(monkeypatch):
    """Explicit override should allow non-executable discovered workflows into selector context."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_NON_EXECUTABLE", "1")

    llm = _CapturingLLM(
        [
            TODO_REFRESH_WORKFLOW_ID.lower(),  # selector verdict
        ]
    )

    discovery_result = {
        "candidates": [
            {
                "concept_id": TODO_REFRESH_WORKFLOW_ID,
                "name": "Todo Refresh Workflow",
                "description": "Refreshes user's todo list from Jira.",
                "is_executable": False,
                "executability_reason": "graph_incomplete",
            }
        ],
        "candidate_count": 1,
    }

    result = orchestrator.run(
        prompt="Refresh my todo list",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
    )

    selector_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_selector"
        ),
        None,
    )
    assert selector_entry is not None
    assert TODO_REFRESH_WORKFLOW_ID in selector_entry.get("discovered_workflow_ids", [])

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


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Plain response routing (skips tool-calling overhead).
# ---------------------------------------------------------------------------


def test_plain_response_skips_tool_calling(monkeypatch):
    """When the classifier returns 'plain_response', the orchestrator should
    generate a direct LLM response without invoking the tool-calling workflow.
    This means only 2 LLM calls: selector + planner (no plan handler overhead).
    """
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(
        [
            "plain_response",  # selector verdict
            "Hello! How can I help?",  # direct planner response
        ]
    )

    result = orchestrator.run(
        prompt="Hi there",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    # Exactly 2 LLM calls: selector + planner.
    assert len(llm.calls) == 2
    assert llm.calls[0]["prompt"] == "Select workflow"
    # The planner prompt should be the user's prompt, not a tool-call prompt.
    assert llm.calls[1]["prompt"] == "Hi there"

    assert result.response_text == "Hello! How can I help?"
    # No tool invocations for a plain response.
    assert result.tool_invocations == ()
    assert result.extra_messages == ()


def test_plain_response_has_routing_info(monkeypatch):
    """Plain response should include WorkflowRoutingInfo in the result."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(
        [
            "plain_response",  # selector verdict
            "Just a chat reply.",  # planner response
        ]
    )

    result = orchestrator.run(
        prompt="Tell me a joke",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert isinstance(result.workflow_routing, WorkflowRoutingInfo)
    assert result.workflow_routing.verdict == "plain_response"
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Routing info on tool-calling path.
# ---------------------------------------------------------------------------


def test_tool_seeking_has_routing_info(monkeypatch):
    """Tool-calling path should also include WorkflowRoutingInfo."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(
        [
            "tool_seeking",  # selector verdict
            "Let me search for that.",  # plan handler response (no tools found)
        ]
    )

    result = orchestrator.run(
        prompt="Search for transformers papers",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "tool_seeking"
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Routing timing telemetry.
# ---------------------------------------------------------------------------


def test_routing_duration_ms_in_aux_llm_calls(monkeypatch):
    """Routing telemetry should include timing in aux_llm_calls."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(
        [
            "plain_response",
            "Quick reply.",
        ]
    )

    result = orchestrator.run(
        prompt="Hi",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    selector_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_selector"
        ),
        None,
    )
    assert selector_entry is not None
    assert "routing_duration_ms" in selector_entry
    assert isinstance(selector_entry["routing_duration_ms"], float)
    assert selector_entry["routing_duration_ms"] >= 0

    # Also check WorkflowRoutingInfo has timing.
    assert result.workflow_routing is not None
    assert result.workflow_routing.routing_duration_ms is not None
    assert result.workflow_routing.routing_duration_ms >= 0


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Selector default-on behaviour.
# ---------------------------------------------------------------------------


def test_selector_enabled_by_default(monkeypatch):
    """Without setting the env var, the selector should be enabled by default."""
    # Remove the env var entirely so we test the code-level default ("1").
    monkeypatch.delenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", raising=False)

    selector = WorkflowSelector(
        registry=MagicMock(),
        prompt_service=MagicMock(),
        verdict_mapping={},
    )
    assert selector.enabled()


def test_selector_can_be_disabled_via_env(monkeypatch):
    """Setting VON_CHAT_WORKFLOW_SELECTOR_ENABLED=0 should disable the selector."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=False)
    assert not orchestrator._workflow_selector.enabled()


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Routing info absent when selector disabled.
# ---------------------------------------------------------------------------


def test_no_routing_info_when_selector_disabled(monkeypatch):
    """When the selector is disabled, workflow_routing should be None."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=False)

    llm = _CapturingLLM(["A direct response."])

    result = orchestrator.run(
        prompt="Hello",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is None
