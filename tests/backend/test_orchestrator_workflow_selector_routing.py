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

import json
from typing import Any, Mapping, Optional, Sequence, cast
from unittest.mock import MagicMock, patch

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    OrchestratorResult,
    WorkflowRoutingInfo,
)
from src.backend.workflows import WorkflowDefinition, WorkflowRegistration
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
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "0")
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


def test_renderer_render_plan_includes_table_record_sets_from_tool_messages(monkeypatch):
    """Render plan should carry task and predicate record sets derived from tool results."""
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

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Here is the answer on screen.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_list",
                                "status": "ok",
                                "duration_ms": 4.2,
                                "payload": {
                                    "success": True,
                                    "count": 1,
                                    "tasks": [
                                        {
                                            "task_concept_id": "#V#task_alpha",
                                            "title": "Alpha task",
                                            "status": "pending",
                                            "priority": "high",
                                            "due_date": "2026-03-01T00:00:00Z",
                                        }
                                    ],
                                },
                            }
                        ),
                    },
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "get_predicate_extent",
                                "status": "ok",
                                "duration_ms": 2.1,
                                "payload": {
                                    "success": True,
                                    "concept_id": "#V#depends_on",
                                    "extent": [
                                        {
                                            "subject": "#V#task_alpha",
                                            "predicate": "#V#depends_on",
                                            "object": "#V#task_beta",
                                            "source": "structured",
                                            "updated_at": "2026-02-16T10:00:00Z",
                                        }
                                    ],
                                    "total_count": 1,
                                    "limit": 100,
                                    "offset": 0,
                                    "has_more": False,
                                },
                            }
                        ),
                    },
                ],
                "invocations": [],
                "iteration_count": 2,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])

    result = orchestrator.run(
        prompt="Show tasks and dependencies",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_table_record_set_count") == 2
    record_sets = result.render_plan.get("screen_table_record_sets")
    assert isinstance(record_sets, list)
    assert len(record_sets) == 2

    task_record_set = next(
        item for item in record_sets if item.get("element_id") == "screen_task_table"
    )
    assert task_record_set.get("row_id_field") == "task_id"
    assert task_record_set.get("row_provenance_field") == "source"
    task_records = task_record_set.get("records")
    assert isinstance(task_records, list)
    assert task_records[0]["task_id"] == "#V#task_alpha"
    assert task_records[0]["task_name"] == "Alpha task"

    predicate_record_set = next(
        item
        for item in record_sets
        if item.get("element_id") == "screen_predicate_extent_table"
    )
    assert predicate_record_set.get("row_id_field") == "assertion_id"
    assert predicate_record_set.get("row_provenance_field") == "assertion_meta"
    predicate_records = predicate_record_set.get("records")
    assert isinstance(predicate_records, list)
    assert predicate_records[0]["subject"] == "#V#task_alpha"
    assert predicate_records[0]["predicate"] == "#V#depends_on"
    assert predicate_records[0]["object"] == "#V#task_beta"


def test_renderer_render_plan_includes_workflow_view_from_tool_messages(monkeypatch):
    """Render plan should carry workflow view payloads with Von/Jira task links."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#workflow_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#workflow_renderer",
                        "renderer_type": "workflow_view",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Workflow status summary.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "workflow_list_instances",
                                "status": "ok",
                                "duration_ms": 3.1,
                                "payload": {
                                    "success": True,
                                    "count": 1,
                                    "instances": [
                                        {
                                            "instance_id": "inst_1",
                                            "workflow_id": "#V#salient_predicate_governance_workflow",
                                            "status": "running",
                                            "current_state": "#V#salience_step_identify_type",
                                            "progress": {
                                                "current": 1,
                                                "total": 3,
                                                "message": "Processing",
                                            },
                                        }
                                    ],
                                },
                            }
                        ),
                    },
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "workflow_get_instance",
                                "status": "ok",
                                "duration_ms": 2.6,
                                "payload": {
                                    "success": True,
                                    "instance_id": "inst_1",
                                    "workflow_id": "#V#salient_predicate_governance_workflow",
                                    "status": "running",
                                    "inputs": {
                                        "task_concept_id": "#V#task_alpha",
                                        "jira_issue_key": "JVNAUTOSCI-1148",
                                    },
                                    "outputs": {},
                                },
                            }
                        ),
                    },
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])

    result = orchestrator.run(
        prompt="Show workflow progress",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.response_text == "Workflow status summary."
    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_workflow_element_count") == 1
    workflow_elements = result.render_plan.get("screen_workflow_elements")
    assert isinstance(workflow_elements, list)
    assert len(workflow_elements) == 1

    payload = workflow_elements[0].get("payload")
    assert isinstance(payload, dict)
    nodes = payload.get("nodes")
    assert isinstance(nodes, list)
    assert len(nodes) == 1

    node = nodes[0]
    assert node.get("node_id") == "inst_1"
    task_links = node.get("task_links")
    assert isinstance(task_links, list)
    link_targets = {str(link.get("target_id")) for link in task_links}
    assert "#V#task_alpha" in link_targets
    assert "JVNAUTOSCI-1148" in link_targets


def test_renderer_selection_gates_screen_element_families(monkeypatch):
    """Selected renderer types should deterministically gate emitted screen element families."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#workflow_renderer,#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#workflow_renderer",
                        "renderer_type": "workflow_view",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Workflow status summary.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_list",
                                "status": "ok",
                                "duration_ms": 2.4,
                                "payload": {
                                    "tasks": [
                                        {
                                            "task_concept_id": "#V#task_alpha",
                                            "title": "Alpha task",
                                            "status": "pending",
                                        }
                                    ]
                                },
                            }
                        ),
                    },
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "workflow_list_instances",
                                "status": "ok",
                                "duration_ms": 3.1,
                                "payload": {
                                    "instances": [
                                        {
                                            "instance_id": "inst_1",
                                            "workflow_id": "#V#salient_predicate_governance_workflow",
                                            "status": "running",
                                        }
                                    ]
                                },
                            }
                        ),
                    },
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])
    result = orchestrator.run(
        prompt="Show workflow progress",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_mapping_mode") == "selected_renderer_types"
    assert result.render_plan.get("screen_element_targets") == {
        "table": False,
        "workflow_view": True,
        "task_view": False,
        "calendar_view": False,
        "chart_view": False,
        "location_view": False,
        "document_view": False,
        "kanban_view": False,
        "timeline": False,
        "hierarchy_view": False,
        "relation_graph_view": False,
    }
    assert "screen_table_record_sets" not in result.render_plan
    assert result.render_plan.get("screen_workflow_element_count") == 1
    assert (
        "renderer_screen_elements:selected_renderer_types"
        in result.render_plan.get("screen_element_reason_codes", [])
    )


def test_renderer_selection_uses_profile_screen_families_for_custom_renderer_type(
    monkeypatch,
):
    """Profile-declared screen families should work even for unknown renderer types."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#custom_metric_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#custom_metric_renderer",
                        "renderer_type": "custom_metric_renderer",
                        "modalities": ["visual"],
                        "screen_element_families": ["chart_view"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Chart summary.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_list",
                                "status": "ok",
                                "duration_ms": 2.0,
                                "payload": {
                                    "tasks": [
                                        {"task_concept_id": "#V#task_alpha", "status": "todo"},
                                        {"task_concept_id": "#V#task_beta", "status": "done"},
                                    ]
                                },
                            }
                        ),
                    }
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])
    result = orchestrator.run(
        prompt="Show metrics chart",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_targets") == {
        "table": False,
        "workflow_view": False,
        "task_view": False,
        "calendar_view": False,
        "chart_view": True,
        "location_view": False,
        "document_view": False,
        "kanban_view": False,
        "timeline": False,
        "hierarchy_view": False,
        "relation_graph_view": False,
    }
    assert result.render_plan.get("screen_chart_element_count") == 1
    assert (
        "renderer_screen_elements:selected_renderer_profiles"
        in result.render_plan.get("screen_element_reason_codes", [])
    )
    assert "unsupported_selected_renderer_types" not in result.render_plan


def test_renderer_selection_emits_timeline_elements_for_timeline_renderer(monkeypatch):
    """Timeline renderer selection should emit timeline display elements only."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#timeline_renderer,#V#workflow_renderer,#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#timeline_renderer",
                        "renderer_type": "timeline",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Timeline summary.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_get_history",
                                "status": "ok",
                                "duration_ms": 3.2,
                                "payload": {
                                    "task_concept_id": "#V#task_alpha",
                                    "history": [
                                        {
                                            "event_id": "event_1",
                                            "event_type": "task_status_updated",
                                            "timestamp": "2026-02-17T09:10:00Z",
                                            "details": {
                                                "to_status": "in_progress",
                                                "jira_issue_key": "JVNAUTOSCI-1138",
                                            },
                                        }
                                    ],
                                },
                            }
                        ),
                    }
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])
    result = orchestrator.run(
        prompt="Show timeline",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_mapping_mode") == "selected_renderer_types"
    assert result.render_plan.get("screen_element_targets") == {
        "table": False,
        "workflow_view": False,
        "task_view": False,
        "calendar_view": False,
        "chart_view": False,
        "location_view": False,
        "document_view": False,
        "kanban_view": False,
        "timeline": True,
        "hierarchy_view": False,
        "relation_graph_view": False,
    }
    assert "screen_table_record_sets" not in result.render_plan
    assert "screen_workflow_elements" not in result.render_plan
    assert result.render_plan.get("screen_timeline_element_count") == 1

    timeline_elements = result.render_plan.get("screen_timeline_elements")
    assert isinstance(timeline_elements, list)
    assert len(timeline_elements) == 1
    timeline_items = timeline_elements[0].get("payload", {}).get("items")
    assert isinstance(timeline_items, list)
    assert timeline_items[0]["item_id"] == "event_1"
    assert timeline_items[0]["task_links"][0]["target_id"] == "#V#task_alpha"


def test_renderer_selection_emits_calendar_elements_for_calendar_renderer(monkeypatch):
    """Calendar renderer selection should emit calendar display elements only."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#calendar_renderer,#V#workflow_renderer,#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#calendar_renderer",
                        "renderer_type": "calendar",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Calendar events.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_list",
                                "status": "ok",
                                "duration_ms": 2.8,
                                "payload": {
                                    "tasks": [
                                        {
                                            "task_concept_id": "#V#task_alpha",
                                            "title": "Alpha task",
                                            "status": "pending",
                                            "due_date": "2026-03-01",
                                            "jira_issue_key": "JVNAUTOSCI-1177",
                                        }
                                    ]
                                },
                            }
                        ),
                    }
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])
    result = orchestrator.run(
        prompt="Show calendar",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_mapping_mode") == "selected_renderer_types"
    assert result.render_plan.get("screen_element_targets") == {
        "table": False,
        "workflow_view": False,
        "task_view": False,
        "calendar_view": True,
        "chart_view": False,
        "location_view": False,
        "document_view": False,
        "kanban_view": False,
        "timeline": False,
        "hierarchy_view": False,
        "relation_graph_view": False,
    }
    assert "screen_table_record_sets" not in result.render_plan
    assert "screen_workflow_elements" not in result.render_plan
    assert "screen_task_view_elements" not in result.render_plan
    assert "screen_timeline_elements" not in result.render_plan
    assert result.render_plan.get("screen_calendar_element_count") == 1

    calendar_elements = result.render_plan.get("screen_calendar_elements")
    assert isinstance(calendar_elements, list)
    assert len(calendar_elements) == 1
    payload = calendar_elements[0].get("payload", {})
    items = payload.get("items")
    assert isinstance(items, list)
    assert items[0]["item_id"] == "#V#task_alpha"
    assert items[0]["title"] == "Alpha task"
    assert items[0]["all_day"] is True
    assert items[0]["task_links"][0]["target_id"] == "#V#task_alpha"


def test_renderer_selection_emits_chart_elements_for_chart_renderer(monkeypatch):
    """Chart renderer selection should emit chart display elements only."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#chart_renderer,#V#workflow_renderer,#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#chart_renderer",
                        "renderer_type": "chart",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Chart summary.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_list",
                                "status": "ok",
                                "duration_ms": 2.4,
                                "payload": {
                                    "tasks": [
                                        {
                                            "task_concept_id": "#V#task_alpha",
                                            "title": "Alpha task",
                                            "status": "pending",
                                        },
                                        {
                                            "task_concept_id": "#V#task_beta",
                                            "title": "Beta task",
                                            "status": "done",
                                        },
                                        {
                                            "task_concept_id": "#V#task_gamma",
                                            "title": "Gamma task",
                                            "status": "pending",
                                        },
                                    ]
                                },
                            }
                        ),
                    }
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])
    result = orchestrator.run(
        prompt="Show chart",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_mapping_mode") == "selected_renderer_types"
    assert result.render_plan.get("screen_element_targets") == {
        "table": False,
        "workflow_view": False,
        "task_view": False,
        "calendar_view": False,
        "chart_view": True,
        "location_view": False,
        "document_view": False,
        "kanban_view": False,
        "timeline": False,
        "hierarchy_view": False,
        "relation_graph_view": False,
    }
    assert "screen_table_record_sets" not in result.render_plan
    assert "screen_workflow_elements" not in result.render_plan
    assert "screen_task_view_elements" not in result.render_plan
    assert "screen_timeline_elements" not in result.render_plan
    assert result.render_plan.get("screen_chart_element_count") == 1

    chart_elements = result.render_plan.get("screen_chart_elements")
    assert isinstance(chart_elements, list)
    assert len(chart_elements) == 1
    payload = chart_elements[0].get("payload", {})
    assert payload.get("chart_type") == "bar"
    assert payload.get("x_axis") == "Task status"
    series = payload.get("series")
    assert isinstance(series, list)
    assert series[0]["series_id"] == "task_status_counts"
    points = series[0].get("points")
    assert isinstance(points, list)
    point_by_status = {
        str(point.get("x")): point.get("y")
        for point in points
        if isinstance(point, Mapping)
    }
    assert point_by_status["pending"] == 2
    assert point_by_status["done"] == 1


def test_renderer_selection_emits_location_elements_for_location_renderer(monkeypatch):
    """Location renderer selection should emit location display elements only."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#location_renderer,#V#workflow_renderer,#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#location_renderer",
                        "renderer_type": "location",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Location summary.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_list",
                                "status": "ok",
                                "duration_ms": 2.1,
                                "payload": {
                                    "tasks": [
                                        {
                                            "task_concept_id": "#V#task_alpha",
                                            "title": "Alpha task",
                                            "location": {
                                                "lat": -36.8485,
                                                "lng": 174.7633,
                                                "address": "Auckland, New Zealand",
                                            },
                                            "jira_issue_key": "JVNAUTOSCI-1178",
                                        },
                                        {
                                            "task_concept_id": "#V#task_beta",
                                            "title": "Beta task",
                                            "address": "Wellington, New Zealand",
                                        },
                                    ]
                                },
                            }
                        ),
                    }
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])
    result = orchestrator.run(
        prompt="Show locations",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_mapping_mode") == "selected_renderer_types"
    assert result.render_plan.get("screen_element_targets") == {
        "table": False,
        "workflow_view": False,
        "task_view": False,
        "calendar_view": False,
        "chart_view": False,
        "location_view": True,
        "document_view": False,
        "kanban_view": False,
        "timeline": False,
        "hierarchy_view": False,
        "relation_graph_view": False,
    }
    assert "screen_table_record_sets" not in result.render_plan
    assert "screen_workflow_elements" not in result.render_plan
    assert "screen_task_view_elements" not in result.render_plan
    assert "screen_timeline_elements" not in result.render_plan
    assert result.render_plan.get("screen_location_element_count") == 1

    location_elements = result.render_plan.get("screen_location_elements")
    assert isinstance(location_elements, list)
    assert len(location_elements) == 1
    payload = location_elements[0].get("payload", {})
    points = payload.get("points")
    assert isinstance(points, list)
    assert len(points) == 2
    assert points[0]["point_id"] == "#V#task_alpha"
    assert points[0]["latitude"] == -36.8485
    assert points[0]["longitude"] == 174.7633
    assert points[0]["address"] == "Auckland, New Zealand"
    assert points[0]["task_links"][0]["target_id"] == "#V#task_alpha"
    assert payload.get("viewport", {}).get("centre_lat") is not None


def test_renderer_selection_emits_document_elements_for_document_renderer(monkeypatch):
    """Document renderer selection should emit document display elements only."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#document_renderer,#V#workflow_renderer,#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#document_renderer",
                        "renderer_type": "document",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Document summary.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "search_arxiv",
                                "status": "ok",
                                "duration_ms": 4.1,
                                "payload": {
                                    "results": [
                                        {
                                            "id": "arXiv:2501.12345",
                                            "title": "Graph-based Document Reasoning",
                                            "summary": "This paper proposes a retrieval-grounded graph pipeline for document reasoning.",
                                            "pdf_url": "https://arxiv.org/pdf/2501.12345.pdf",
                                            "published": "2026-01-20T00:00:00Z",
                                        }
                                    ]
                                },
                            }
                        ),
                    }
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])
    result = orchestrator.run(
        prompt="Show document excerpts",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_mapping_mode") == "selected_renderer_types"
    assert result.render_plan.get("screen_element_targets") == {
        "table": False,
        "workflow_view": False,
        "task_view": False,
        "calendar_view": False,
        "chart_view": False,
        "location_view": False,
        "document_view": True,
        "kanban_view": False,
        "timeline": False,
        "hierarchy_view": False,
        "relation_graph_view": False,
    }
    assert "screen_table_record_sets" not in result.render_plan
    assert "screen_workflow_elements" not in result.render_plan
    assert "screen_task_view_elements" not in result.render_plan
    assert "screen_timeline_elements" not in result.render_plan
    assert result.render_plan.get("screen_document_element_count") == 1

    document_elements = result.render_plan.get("screen_document_elements")
    assert isinstance(document_elements, list)
    assert len(document_elements) == 1
    payload = document_elements[0].get("payload", {})
    documents = payload.get("documents")
    assert isinstance(documents, list)
    assert documents[0]["document_id"] == "arXiv:2501.12345"
    assert documents[0]["title"] == "Graph-based Document Reasoning"
    assert documents[0]["sections"][0]["heading"] == "Graph-based Document Reasoning"


def test_renderer_selection_emits_task_view_elements_for_task_renderer(monkeypatch):
    """Task-view renderer selection should emit task-view elements only."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#task_view_renderer,#V#workflow_renderer,#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#task_view_renderer",
                        "renderer_type": "task_view",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Task cards.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_list",
                                "status": "ok",
                                "duration_ms": 2.8,
                                "payload": {
                                    "tasks": [
                                        {
                                            "task_concept_id": "#V#task_alpha",
                                            "title": "Alpha task",
                                            "status": "pending",
                                            "priority": "high",
                                            "jira_issue_key": "JVNAUTOSCI-1174",
                                        }
                                    ]
                                },
                            }
                        ),
                    }
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])
    result = orchestrator.run(
        prompt="Show task cards",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_mapping_mode") == "selected_renderer_types"
    assert result.render_plan.get("screen_element_targets") == {
        "table": False,
        "workflow_view": False,
        "task_view": True,
        "calendar_view": False,
        "chart_view": False,
        "location_view": False,
        "document_view": False,
        "kanban_view": False,
        "timeline": False,
        "hierarchy_view": False,
        "relation_graph_view": False,
    }
    assert "screen_table_record_sets" not in result.render_plan
    assert "screen_workflow_elements" not in result.render_plan
    assert "screen_timeline_elements" not in result.render_plan
    assert result.render_plan.get("screen_task_view_element_count") == 1

    task_view_elements = result.render_plan.get("screen_task_view_elements")
    assert isinstance(task_view_elements, list)
    assert len(task_view_elements) == 1
    tasks = task_view_elements[0].get("payload", {}).get("tasks")
    assert isinstance(tasks, list)
    assert tasks[0]["task_id"] == "#V#task_alpha"
    task_link_targets = {
        str(link.get("target_id"))
        for link in tasks[0].get("task_links", [])
        if isinstance(link, Mapping)
    }
    assert "#V#task_alpha" in task_link_targets
    assert "JVNAUTOSCI-1174" in task_link_targets


def test_renderer_selection_emits_kanban_elements_for_kanban_renderer(monkeypatch):
    """Kanban renderer selection should emit kanban display elements only."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#kanban_renderer,#V#workflow_renderer,#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#kanban_renderer",
                        "renderer_type": "kanban",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Kanban cards.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_list",
                                "status": "ok",
                                "duration_ms": 2.8,
                                "payload": {
                                    "tasks": [
                                        {
                                            "task_concept_id": "#V#task_alpha",
                                            "title": "Alpha task",
                                            "status": "pending",
                                            "priority": "high",
                                            "jira_issue_key": "JVNAUTOSCI-1181",
                                        },
                                        {
                                            "task_concept_id": "#V#task_beta",
                                            "title": "Beta task",
                                            "status": "done",
                                        },
                                    ]
                                },
                            }
                        ),
                    }
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])
    result = orchestrator.run(
        prompt="Show task kanban",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_mapping_mode") == "selected_renderer_types"
    assert result.render_plan.get("screen_element_targets") == {
        "table": False,
        "workflow_view": False,
        "task_view": False,
        "calendar_view": False,
        "chart_view": False,
        "location_view": False,
        "document_view": False,
        "kanban_view": True,
        "timeline": False,
        "hierarchy_view": False,
        "relation_graph_view": False,
    }
    assert "screen_table_record_sets" not in result.render_plan
    assert "screen_workflow_elements" not in result.render_plan
    assert "screen_task_view_elements" not in result.render_plan
    assert "screen_timeline_elements" not in result.render_plan
    assert result.render_plan.get("screen_kanban_element_count") == 1

    kanban_elements = result.render_plan.get("screen_kanban_elements")
    assert isinstance(kanban_elements, list)
    assert len(kanban_elements) == 1
    payload = kanban_elements[0].get("payload", {})
    columns = payload.get("columns")
    cards = payload.get("cards")
    assert isinstance(columns, list)
    assert isinstance(cards, list)
    assert {column.get("column_id") for column in columns} == {"pending", "done"}
    assert cards[0]["card_id"] == "#V#task_alpha"
    assert cards[0]["column_id"] == "pending"


def test_renderer_selection_emits_relation_graph_elements_for_graph_renderer(monkeypatch):
    """Relation-graph renderer selection should emit relation graph elements only."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#relation_graph_renderer,#V#workflow_renderer,#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#relation_graph_renderer",
                        "renderer_type": "relation_graph",
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Relation graph.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "get_predicate_extent",
                                "status": "ok",
                                "duration_ms": 2.4,
                                "payload": {
                                    "concept_id": "#V#panelist_in_event",
                                    "extent": [
                                        {
                                            "subject": "#V#michael_witbrock",
                                            "predicate": "#V#panelist_in_event",
                                            "object": "#V#panel_4_ai_for_industry_ai_for_society_iaicgf_2025_melbourne",
                                            "source": "structured",
                                        },
                                        {
                                            "subject": "#V#michael_witbrock",
                                            "predicate": "#V#hasName",
                                            "object": "Michael Witbrock",
                                            "source": "text_relations",
                                        },
                                    ],
                                },
                            }
                        ),
                    }
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])
    result = orchestrator.run(
        prompt="Show relation graph",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_mapping_mode") == "selected_renderer_types"
    assert result.render_plan.get("screen_element_targets") == {
        "table": False,
        "workflow_view": False,
        "task_view": False,
        "calendar_view": False,
        "chart_view": False,
        "location_view": False,
        "document_view": False,
        "kanban_view": False,
        "timeline": False,
        "hierarchy_view": False,
        "relation_graph_view": True,
    }
    assert "screen_table_record_sets" not in result.render_plan
    assert "screen_workflow_elements" not in result.render_plan
    assert "screen_task_view_elements" not in result.render_plan
    assert "screen_timeline_elements" not in result.render_plan
    assert "screen_kanban_elements" not in result.render_plan
    assert result.render_plan.get("screen_relation_graph_element_count") == 1

    relation_graph_elements = result.render_plan.get("screen_relation_graph_elements")
    assert isinstance(relation_graph_elements, list)
    assert len(relation_graph_elements) == 1
    payload = relation_graph_elements[0].get("payload", {})
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    assert isinstance(nodes, list)
    assert isinstance(edges, list)
    assert len(edges) == 2
    node_ids = {str(node.get("node_id")) for node in nodes if isinstance(node, Mapping)}
    assert "#V#michael_witbrock" in node_ids
    assert "#V#panel_4_ai_for_industry_ai_for_society_iaicgf_2025_melbourne" in node_ids


def test_renderer_selection_fallback_when_no_types_selected(monkeypatch):
    """Empty renderer selections should preserve legacy table/workflow extraction behaviour."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#workflow_renderer,#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Task summary.",
                "tool_messages": [
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_list",
                                "status": "ok",
                                "duration_ms": 2.4,
                                "payload": {
                                    "tasks": [
                                        {
                                            "task_concept_id": "#V#task_alpha",
                                            "title": "Alpha task",
                                            "status": "pending",
                                        }
                                    ]
                                },
                            }
                        ),
                    }
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])
    result = orchestrator.run(
        prompt="Show tasks",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_mapping_mode") == (
        "fallback_no_renderer_selection"
    )
    assert result.render_plan.get("screen_element_targets") == {
        "table": True,
        "workflow_view": True,
        "task_view": False,
        "calendar_view": False,
        "chart_view": False,
        "location_view": False,
        "document_view": False,
        "kanban_view": False,
        "timeline": False,
        "hierarchy_view": False,
        "relation_graph_view": False,
    }
    assert result.render_plan.get("screen_table_record_set_count") == 1
    assert (
        "renderer_screen_elements:fallback_no_renderer_selection"
        in result.render_plan.get("screen_element_reason_codes", [])
    )


def test_renderer_render_plan_skips_malformed_tool_messages_for_table_record_sets(
    monkeypatch,
):
    """Malformed/invalid tool payloads should not produce table record sets."""
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

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Here is the answer on screen.",
                "tool_messages": [
                    # Malformed/truncated JSON.
                    {
                        "role": "tool",
                        "content": "{\"tool\":\"task_list\",\"status\":\"ok\",\"payload\":{\"tasks\":[{\"title\":\"A\"}]",
                    },
                    # Explicit tool error should be ignored.
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_list",
                                "status": "error",
                                "duration_ms": 2.0,
                                "error": "simulated failure",
                            }
                        ),
                    },
                    # Non-mapping payload should be ignored.
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "task_search",
                                "status": "ok",
                                "duration_ms": 3.1,
                                "payload": ["unexpected", "payload", "shape"],
                            }
                        ),
                    },
                    # Mapping payload but invalid extent row shape should be ignored.
                    {
                        "role": "tool",
                        "content": json.dumps(
                            {
                                "tool": "get_predicate_extent",
                                "status": "ok",
                                "duration_ms": 1.4,
                                "payload": {
                                    "success": True,
                                    "concept_id": "#V#depends_on",
                                    "extent": ["invalid-row-shape"],
                                },
                            }
                        ),
                    },
                ],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(["tool_seeking"])

    result = orchestrator.run(
        prompt="Show tasks and dependencies",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.response_text == "Here is the answer on screen."
    assert isinstance(result.render_plan, dict)
    assert "screen_table_record_sets" not in result.render_plan
    assert "screen_table_record_set_count" not in result.render_plan


def test_renderer_applicability_missing_definitions_falls_back_screen_only(monkeypatch):
    """Enabled routing with missing renderer definitions should fail closed to screen-only."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.delenv("VON_RENDERER_APPLICABILITY_DEFINITION_IDS", raising=False)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_BOOTSTRAP_DEFAULTS_ENABLE", "0")

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


def test_renderer_applicability_bootstrap_defaults_surface_resolver_error_details(
    monkeypatch,
):
    """When env IDs are missing, canonical defaults should be used and diagnostics preserved."""
    from src.backend.services.renderer_applicability_vontology_service import (
        canonical_renderer_profile_concept_ids,
    )

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.delenv("VON_RENDERER_APPLICABILITY_DEFINITION_IDS", raising=False)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_BOOTSTRAP_DEFAULTS_ENABLE", "1")

    captured_payloads: list[dict[str, Any]] = []

    def _invoke(tool_name: str, payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        captured_payloads.append(dict(payload))
        return _InvokeResult(
            {
                "success": False,
                "error": "No renderer profile metadata available",
                "error_code": "missing_parameter",
                "error_details": {
                    "renderer_definition_loading": {
                        "missing_profile_concept_ids": ["#V#table_renderer"],
                        "malformed_profile_concept_ids": ["#V#workflow_renderer"],
                    }
                },
                "suggestions": [
                    "Use upsert_renderer_profile to persist valid profile JSON",
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    llm = _CapturingLLM(["tool_seeking", "Here is the answer on screen."])

    result = orchestrator.run(
        prompt="Explain this briefly",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.response_text == "Here is the answer on screen."
    assert captured_payloads
    expected_ids = list(canonical_renderer_profile_concept_ids())
    assert (
        captured_payloads[0]["renderer_definition_concept_ids"]
        == expected_ids
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
    assert renderer_entry.get("attempted") is True
    assert renderer_entry.get("success") is False
    assert renderer_entry.get("reason") == "resolver_unsuccessful"
    assert renderer_entry.get("renderer_definition_source") == "canonical_bootstrap_defaults"
    assert renderer_entry.get("resolver_error_code") == "missing_parameter"
    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("resolver_error_code") == "missing_parameter"
    error_details = result.render_plan.get("resolver_error_details") or {}
    loading = error_details.get("renderer_definition_loading") or {}
    assert loading.get("missing_profile_concept_ids") == ["#V#table_renderer"]
    assert loading.get("malformed_profile_concept_ids") == ["#V#workflow_renderer"]


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


def test_custom_tool_pipeline_workflow_dispatches_without_id_special_casing(monkeypatch):
    """Custom discovered workflows that satisfy the tool pipeline contract should
    execute through the tool workflow path without a workflow-ID allowlist.
    """

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    custom_workflow_id = "#V#custom_tool_pipeline_workflow"

    tool_workflow_def = orchestrator._workflow_registry.get(TOOL_CALLING_WORKFLOW_ID)
    assert tool_workflow_def is not None
    orchestrator._workflow_registry.register_if_absent(
        WorkflowRegistration(
            workflow_id=custom_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=custom_workflow_id,
                initial_state=tool_workflow_def.initial_state,
                states=tool_workflow_def.states,
                termination_states=tool_workflow_def.termination_states,
                purpose="Custom tool pipeline workflow for dispatch parity tests.",
            ),
            purpose="Custom tool pipeline workflow for dispatch parity tests.",
            source="test",
        )
    )

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Custom tool pipeline response.",
                "tool_messages": [],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    execute_calls: list[dict[str, Any]] = []

    def _execute_workflow(workflow_id: str, **kwargs: Any):
        call = {
            "workflow_id": workflow_id,
            "episode_stage": kwargs.get("data", {}).get("workflow_episode_stage"),
        }
        execute_calls.append(call)
        if call["episode_stage"] != "tool_calling":
            raise AssertionError(
                "Custom tool-pipeline workflow should dispatch via tool_calling stage."
            )
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM([custom_workflow_id.lower()])
    discovery_result = {
        "matches": [
            {
                "concept_id": custom_workflow_id,
                "name": "Custom Tool Pipeline Workflow",
                "description": "Test workflow mirroring tool-calling actions.",
            }
        ],
        "match_count": 1,
    }

    result = orchestrator.run(
        prompt="Use the custom tool pipeline",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
    )

    assert result.response_text == "Custom tool pipeline response."
    assert len(execute_calls) == 1
    assert execute_calls[0]["workflow_id"] == custom_workflow_id
    assert execute_calls[0]["episode_stage"] == "tool_calling"


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



