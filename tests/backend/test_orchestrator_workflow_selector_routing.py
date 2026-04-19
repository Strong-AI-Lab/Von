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
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence, cast
from unittest.mock import MagicMock

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    ProgressTracker,
    WorkflowRoutingInfo,
    _ModelCandidate,
)
import src.backend.services.workflow_selection_experience as selection_experience_module
from src.backend.services.paper_representation_workflow_vontology_service import (
    ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
    bootstrap_canonical_paper_representation_workflows,
)
from src.backend.services.workflow_discovery_service import (
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowRegistration,
    WorkflowStateSpec,
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
)
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology
from src.backend.workflows.workflow_gap_workflow_contracts import (
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
)
from src.backend.workflows.workflow_selector import WorkflowSelector
from orchestrator_test_harness import build_db_independent_orchestrator


# ---------------------------------------------------------------------------
# Stubs – avoid DB, gateway, and real LLM calls.
# ---------------------------------------------------------------------------


class _StubGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {
            "renderer_resolve_applicability": {"category": "read"},
        }

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


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    invalidate_workflow_discovery_executability_caches()
    yield
    invalidate_workflow_discovery_executability_caches()
    authority_service.clear_workflow_type_resolution_cache()


def _build_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selector_enabled: bool = False,
) -> InternalMCPChatOrchestrator:
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _StubGateway()),
        selector_enabled=selector_enabled,
        max_tool_invocations=1,
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda **_kwargs: ("You are Von.", "#V#test_base_system_prompt"),
    )
    return orchestrator


def _stub_execute_workflow_result(
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: InternalMCPChatOrchestrator,
    *,
    expected_workflow_id: str,
    data: Mapping[str, Any],
    final_state: str = "completed",
    completed: bool = True,
    passthrough_unmatched: bool = False,
) -> None:
    """Stub direct workflow execution so routing tests stay focused."""

    original_execute_workflow = orchestrator.execute_workflow

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != expected_workflow_id:
            if passthrough_unmatched:
                return original_execute_workflow(workflow_id, **_kwargs)
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data=dict(data),
            final_state=final_state,
            completed=completed,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)


def _register_terminal_custom_workflow(
    orchestrator: InternalMCPChatOrchestrator,
    *,
    workflow_id: str,
    purpose: str = "Custom workflow for routing tests.",
) -> None:
    """Register a minimal launchable custom workflow for selector tests."""

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_custom"),
                        ),
                        terminal=True,
                    )
                },
            ),
            purpose=purpose,
            source="test",
        )
    )


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
            CHAT_NARRATION_WORKFLOW_ID,  # workflow selector verdict
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
    narration_prompt = str(llm.calls[2]["prompt"] or "")
    assert "<spoken>" in narration_prompt
    assert "spoken" in narration_prompt.lower()

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
                "diagnostics": {
                    "filtering_boundary": {
                        "schema_version": "renderer_filtering_boundary_v1",
                        "candidate_count": 1,
                        "applicable_count": 1,
                        "rejected_count": 0,
                        "selected_count": 1,
                        "rejection_reason_counts": {},
                        "applicable_preview": [
                            {"renderer_id": "#V#narration_renderer"}
                        ],
                        "rejected_preview": [],
                        "selected_preview": [
                            {"renderer_id": "#V#narration_renderer"}
                        ],
                    }
                },
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=TOOL_CALLING_WORKFLOW_ID,
        data={
            "final_response": "Here is the answer on screen.",
            "tool_messages": [],
            "invocations": [],
            "iteration_count": 1,
        },
        passthrough_unmatched=True,
    )

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,  # workflow selector verdict
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
    assert isinstance(renderer_entry.get("renderer_filtering_boundary"), dict)
    assert (
        renderer_entry.get("renderer_filtering_boundary", {}).get("schema_version")
        == "renderer_filtering_boundary_v1"
    )


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
            CHAT_NARRATION_WORKFLOW_ID,  # workflow selector verdict
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
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=TOOL_CALLING_WORKFLOW_ID,
        data={
            "final_response": "Here is the answer on screen.",
            "tool_messages": [],
            "invocations": [],
            "iteration_count": 1,
        },
        passthrough_unmatched=True,
    )

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,
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
    assert len(llm.calls) == 1
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
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=TOOL_CALLING_WORKFLOW_ID,
        data={
            "final_response": "Here is the answer on screen.",
            "tool_messages": [],
            "invocations": [],
            "iteration_count": 1,
        },
        passthrough_unmatched=True,
    )

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,
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
    assert len(llm.calls) == 1
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
            TOOL_CALLING_WORKFLOW_ID,
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])

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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])

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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
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


def test_renderer_selection_skips_hierarchy_view_for_custom_relation_edges(monkeypatch):
    """Custom relation edges must not be coerced into ontology hierarchy trees."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#hierarchy_renderer,#V#workflow_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult(
            {
                "success": True,
                "selected_renderers": [
                    {
                        "renderer_id": "#V#hierarchy_renderer",
                        "renderer_type": "hierarchy",
                        "screen_element_families": ["hierarchy_view"],
                        "modalities": ["visual"],
                    }
                ],
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Related concepts only.",
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
                                            "object": (
                                                "#V#panel_4_ai_for_industry_ai_for_society_"
                                                "iaicgf_2025_melbourne"
                                            ),
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
    result = orchestrator.run(
        prompt="Show the hierarchy",
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
        "hierarchy_view": True,
        "relation_graph_view": False,
    }
    assert "screen_hierarchy_elements" not in result.render_plan
    assert result.render_plan.get("screen_hierarchy_element_count") in (None, 0)


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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
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


def test_renderer_selection_emits_hierarchy_from_taxonomy_screen_text(monkeypatch):
    """Taxonomy recommendations in screen text should emit hierarchy_view payloads."""
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
                "final_response": (
                    "```text\n"
                    "#V#document\n"
                    "└── #V#meeting_document\n"
                    "    ├── #V#meeting_notes\n"
                    "    ├── #V#meeting_summary\n"
                    "    └── #V#meeting_transcript\n"
                    "        └── #V#automated_meeting_transcript\n"
                    "```"
                ),
                "tool_messages": [],
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
    result = orchestrator.run(
        prompt="Show the taxonomy recommendation",
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
        "hierarchy_view": True,
        "relation_graph_view": False,
    }
    assert (
        "renderer_screen_elements:taxonomy_hierarchy_from_screen_text"
        in result.render_plan.get("screen_element_reason_codes", [])
    )
    assert result.render_plan.get("screen_hierarchy_element_count") == 1

    hierarchy_elements = result.render_plan.get("screen_hierarchy_elements")
    assert isinstance(hierarchy_elements, list)
    assert len(hierarchy_elements) == 1
    payload = hierarchy_elements[0].get("payload", {})
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    assert isinstance(nodes, list)
    assert isinstance(edges, list)
    assert len(nodes) == 6
    assert len(edges) == 5
    node_ids = {str(node.get("node_id")) for node in nodes if isinstance(node, Mapping)}
    assert "#V#document" in node_ids
    assert "#V#automated_meeting_transcript" in node_ids


def test_renderer_selection_hierarchy_parser_tolerates_indented_root_rows(monkeypatch):
    """Regression for JVNAUTOSCI-1365: avoid index errors on indented first rows."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_RENDERER_APPLICABILITY_ROUTING_ENABLE", "1")
    monkeypatch.setenv(
        "VON_RENDERER_APPLICABILITY_DEFINITION_IDS",
        "#V#workflow_renderer,#V#table_renderer",
    )

    def _invoke(tool_name: str, _payload: Mapping[str, Any]):
        if tool_name != "renderer_resolve_applicability":
            raise AssertionError(f"Unexpected tool invocation: {tool_name}")
        return _InvokeResult({"success": True, "selected_renderers": []})

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": (
                    "```text\n"
                    "    #V#document\n"
                    "    #V#meeting_document\n"
                    "        #V#meeting_notes\n"
                    "```"
                ),
                "tool_messages": [],
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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])
    result = orchestrator.run(
        prompt="Show a taxonomy hierarchy",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("screen_element_targets", {}).get("hierarchy_view") is True
    hierarchy_elements = result.render_plan.get("screen_hierarchy_elements")
    assert isinstance(hierarchy_elements, list)
    assert len(hierarchy_elements) == 1
    payload = hierarchy_elements[0].get("payload", {})
    edges = payload.get("edges")
    assert isinstance(edges, list)
    assert len(edges) == 2


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

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])

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
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=TOOL_CALLING_WORKFLOW_ID,
        data={
            "final_response": "Here is the answer on screen.",
            "tool_messages": [],
            "invocations": [],
            "iteration_count": 1,
        },
        passthrough_unmatched=True,
    )

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,
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
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=TOOL_CALLING_WORKFLOW_ID,
        data={
            "final_response": "Here is the answer on screen.",
            "tool_messages": [],
            "invocations": [],
            "iteration_count": 1,
        },
    )

    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID])

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
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=TOOL_CALLING_WORKFLOW_ID,
        data={
            "final_response": "Here is the answer on screen.",
            "tool_messages": [],
            "invocations": [],
            "iteration_count": 1,
        },
        passthrough_unmatched=True,
    )

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,
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
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=TOOL_CALLING_WORKFLOW_ID,
        data={
            "final_response": "I'll help with that.",
            "tool_messages": [],
            "invocations": [],
            "iteration_count": 1,
        },
    )

    # No presenter mode context — selector should still fire.
    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,  # workflow selector verdict
        ]
    )

    result = orchestrator.run(
        prompt="Search for papers about transformers",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    # The selector still fires even when the final response comes from the
    # tool workflow rather than a separate presenter-mode path.
    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"] == "Select workflow"
    assert result.response_text == "I'll help with that."

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
    assert selector_entry["verdict"] == "rag_selected"


def test_selector_prompt_unavailable_fails_closed_without_selector_llm(monkeypatch):
    """Missing authoritative selector prompt should skip selector LLM dispatch."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    original_render_prompt = orchestrator._prompt_templates.render_prompt

    def _render_without_selector_prompt(
        prompt_ids: Any,
        *,
        fallback: Any = None,
        variables: Any = None,
        max_chars: Any = None,
    ) -> Any:
        requested_prompt_ids = {
            str(item).strip()
            for item in (prompt_ids or ())
            if isinstance(item, str) and str(item).strip()
        }
        if requested_prompt_ids.intersection(orchestrator._TURN_SELECTOR_PROMPTS):
            return None
        return original_render_prompt(
            prompt_ids,
            fallback=fallback,
            variables=variables,
            max_chars=max_chars,
        )

    monkeypatch.setattr(
        orchestrator._prompt_templates,
        "render_prompt",
        _render_without_selector_prompt,
    )

    llm = _CapturingLLM(
        [
            "I'll help with that.",
        ]
    )

    result = orchestrator.run(
        prompt="hello",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert len(llm.calls) == 1
    assert all(call["prompt"] != "Select workflow" for call in llm.calls)
    assert result.response_text == "I'll help with that."

    selector_entry = next(
        e
        for e in result.aux_llm_calls
        if isinstance(e, dict) and e.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == CHAT_ASSISTANT_WORKFLOW_ID
    assert selector_entry["verdict"] == "selector_prompt_unavailable"
    assert selector_entry["selection_source"] == "selector_fail_closed"
    assert selector_entry["prompt_failure_reason"] == "selector_prompt_unavailable"


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 1.3: Discovered workflows in selector prompt.
# ---------------------------------------------------------------------------


def test_selector_receives_discovered_workflows(monkeypatch):
    """Discovered workflows should reach selector context after JIT registration."""

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
    discovered_ids = set(selector_entry.get("discovered_workflow_ids", []))
    assert "#V#custom_analysis_workflow" in discovered_ids
    assert {
        CHAT_ASSISTANT_WORKFLOW_ID,
        TOOL_CALLING_WORKFLOW_ID,
        CHAT_NARRATION_WORKFLOW_ID,
    }.issubset(discovered_ids)
    assert selector_entry.get("discovery_candidate_count") == 4
    assert selector_entry.get("discovery_excluded_count") == 0

    # Since the workflow is not in the registry, execute_workflow returns None
    # and we fall through to tool-calling.  The response should come from the
    # plan handler (second LLM call).
    assert "Falling back" in result.response_text or result.response_text


def test_selector_unmatched_non_default_candidate_uses_safe_general_tool_fallback(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    excluded_workflow_id = "#V#specialised_vontology_search_workflow"
    executed_workflow_ids: list[str] = []
    recovery_requests: list[Mapping[str, Any]] = []

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        executed_workflow_ids.append(workflow_id)
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            return SimpleNamespace(
                data={
                    "final_response": "Executed via safe general tool workflow.",
                    "tool_messages": [],
                    "invocations": [],
                    "iteration_count": 1,
                },
                final_state="complete",
                completed=True,
            )
        if workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID:
            recovery_requests.append(dict(_kwargs))
            return SimpleNamespace(
                data={
                    "workflow_gap_final_response_text": (
                        "Recovered through workflow-gap analysis."
                    ),
                    "workflow_gap_final_extra_messages": [],
                    "workflow_gap_final_tool_invocations": [],
                    "workflow_gap_recovery_outcome": (
                        "candidate_retried_successfully"
                    ),
                    "workflow_gap_candidate_workflow_id": excluded_workflow_id,
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(
        [
            json.dumps(
                {
                    "workflow_id": excluded_workflow_id,
                    "confidence": 0.94,
                    "reasoning": (
                        "The specialised Vontology search workflow is the best fit "
                        "for this request."
                    ),
                }
            )
        ]
    )

    result = orchestrator.run(
        prompt="Find the represented student record and inspect the concept links.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": excluded_workflow_id,
                    "name": "Specialised Vontology Search Workflow",
                    "description": "Inspect represented concepts and relations.",
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.97,
                    "confidence_score": 0.97,
                }
            ],
            "candidates": [
                {
                    "concept_id": excluded_workflow_id,
                    "name": "Specialised Vontology Search Workflow",
                    "description": "Inspect represented concepts and relations.",
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.97,
                    "confidence_score": 0.97,
                }
            ],
            "match_count": 1,
        },
    )

    assert executed_workflow_ids == [
        TOOL_CALLING_WORKFLOW_ID,
        WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    ]
    assert result.response_text == "Recovered through workflow-gap analysis."
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "tool_contract_override"
    assert result.workflow_routing.source == "selector_override"
    assert (
        "safe general tool workflow"
        in (result.workflow_routing.reasoning or "").lower()
    )

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == CHAT_ASSISTANT_WORKFLOW_ID
    assert selector_entry["selection_metadata"]["selection_resolution"] == (
        "default_workflow_fallback_unmatched_candidate"
    )
    assert (
        selector_entry["selection_metadata"]["unmatched_candidate_workflow_id"]
        == excluded_workflow_id
    )

    override_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == "selector_unmatched_candidate_requires_safe_general_fallback"
    )
    assert override_entry["selected_workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert override_entry["requested_candidate_workflow_id"] == excluded_workflow_id

    recovery_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_gap_recovery"
    )
    assert recovery_entry["status"] == "applied"
    assert recovery_entry["candidate_workflow_id"] == excluded_workflow_id
    assert recovery_requests
    assert (
        recovery_requests[0]["data"]["workflow_gap_base_response_text"]
        == "Executed via safe general tool workflow."
    )
    assert recovery_requests[0]["data"]["workflow_gap_selected_execution_mode"] == (
        "tool_pipeline"
    )


def test_selector_safe_general_fallback_finalises_selection_experience_with_override_truth(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    excluded_workflow_id = "#V#specialised_vontology_search_workflow"
    captured_finalise: dict[str, Any] = {}

    monkeypatch.setattr(
        selection_experience_module,
        "record_selection_experience",
        lambda **_kwargs: SimpleNamespace(experience_id="exp-selector-override"),
    )

    def _capture_finalise(**kwargs: Any):
        captured_finalise.update(kwargs)
        return None

    monkeypatch.setattr(
        selection_experience_module,
        "finalise_selection_experience",
        _capture_finalise,
    )

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            return SimpleNamespace(
                data={
                    "final_response": "Executed via safe general tool workflow.",
                    "tool_messages": [],
                    "invocations": [],
                    "iteration_count": 1,
                },
                final_state="complete",
                completed=True,
            )
        if workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID:
            return SimpleNamespace(
                data={
                    "workflow_gap_final_response_text": (
                        "Recovered through workflow-gap analysis."
                    ),
                    "workflow_gap_final_extra_messages": [],
                    "workflow_gap_final_tool_invocations": [],
                    "workflow_gap_recovery_outcome": (
                        "candidate_retried_successfully"
                    ),
                    "workflow_gap_candidate_workflow_id": excluded_workflow_id,
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt="Find the represented student record and inspect the concept links.",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": excluded_workflow_id,
                        "confidence": 0.94,
                        "reasoning": (
                            "The specialised Vontology search workflow is the best fit "
                            "for this request."
                        ),
                    }
                )
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": excluded_workflow_id,
                    "name": "Specialised Vontology Search Workflow",
                    "description": "Inspect represented concepts and relations.",
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.97,
                    "confidence_score": 0.97,
                }
            ],
            "candidates": [
                {
                    "concept_id": excluded_workflow_id,
                    "name": "Specialised Vontology Search Workflow",
                    "description": "Inspect represented concepts and relations.",
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.97,
                    "confidence_score": 0.97,
                }
            ],
            "match_count": 1,
        },
        turn_id="turn-selector-override",
    )

    assert result.response_text == "Recovered through workflow-gap analysis."
    assert captured_finalise["experience_id"] == "exp-selector-override"
    outcome_metadata = captured_finalise["outcome_metadata"]
    assert outcome_metadata["selected_workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert outcome_metadata["effective_dispatch_workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert outcome_metadata["selector_override_applied"] is True
    assert outcome_metadata["workflow_gap_recovery_applied"] is True
    assert outcome_metadata["selector_override"]["reason"] == (
        "selector_unmatched_candidate_requires_safe_general_fallback"
    )
    assert outcome_metadata["selector_override"]["requested_candidate_workflow_id"] == (
        excluded_workflow_id
    )


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
    discovered_ids = set(selector_entry.get("discovered_workflow_ids", []))
    assert TODO_REFRESH_WORKFLOW_ID not in discovered_ids
    assert {
        CHAT_ASSISTANT_WORKFLOW_ID,
        TOOL_CALLING_WORKFLOW_ID,
        CHAT_NARRATION_WORKFLOW_ID,
    }.issubset(discovered_ids)
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


def test_workflow_selector_emits_dispatch_progress_events(monkeypatch):
    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.recommend_workflow_with_policy",
        lambda **_kwargs: {
            "guidance_mode": "none",
            "recommended_workflow_id": None,
            "candidate_scores": [],
        },
    )
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID, "Fallback response."])
    captured_progress: list[dict[str, Any]] = []
    tracker = ProgressTracker(callback=lambda info: captured_progress.append(dict(info)))

    discovery_result = {
        "matches": [
            {
                "concept_id": TOOL_CALLING_WORKFLOW_ID,
                "name": "Tool calling workflow",
                "description": "Default tool-calling route.",
                "is_executable": True,
                "executability_reason": "executable_now",
            }
        ],
        "candidates": [
            {
                "concept_id": TOOL_CALLING_WORKFLOW_ID,
                "name": "Tool calling workflow",
                "description": "Default tool-calling route.",
                "is_executable": True,
                "executability_reason": "executable_now",
            }
        ],
        "match_count": 1,
        "candidate_count": 1,
    }

    result = orchestrator.run(
        prompt="Use the best workflow for this request.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
        progress_tracker=tracker,
    )

    workflow_dispatch_events = [
        entry for entry in captured_progress if entry.get("stage") == "workflow_dispatch"
    ]
    assert workflow_dispatch_events
    assert any(
        entry.get("phase_label") == "Selecting workflow"
        and entry.get("workflow_candidate_count") == 3
        for entry in workflow_dispatch_events
    )
    assert any(
        entry.get("status") == "llm_call_start"
        for entry in workflow_dispatch_events
    )
    assert any(
        entry.get("status") == "llm_call_end"
        and entry.get("success") is True
        for entry in workflow_dispatch_events
    )
    selected_event = next(
        entry
        for entry in workflow_dispatch_events
        if entry.get("workflow_selector_verdict") == "rag_selected"
        and entry.get("status") == "thinking"
    )
    assert selected_event.get("phase_label") == "Workflow selected"
    assert selected_event.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert any(
        entry.get("phase") == "workflow_dispatch"
        and entry.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
        and entry.get("goal_label")
        == f"Execute {selected_event.get('selected_workflow_name')}."
        for entry in captured_progress
    )
    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 2.2: Non-standard workflow routing via execute_workflow.
# ---------------------------------------------------------------------------


def test_non_standard_workflow_routes_via_execute_workflow(monkeypatch):
    """Workflows in the registry but not in the standard set should use execute_workflow."""

    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    execute_calls: list[dict[str, Any]] = []

    def _execute_workflow(workflow_id: str, **kwargs: Any):
        execute_calls.append(
            {
                "workflow_id": workflow_id,
                "data": dict(kwargs.get("data") or {}),
            }
        )
        return SimpleNamespace(
            completed=True,
            final_state="completed",
            data={"response_text": "Todo refresh complete."},
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

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
    assert len(execute_calls) == 1
    assert execute_calls[0]["workflow_id"] == TODO_REFRESH_WORKFLOW_ID
    handoff_data = execute_calls[0]["data"]
    assert isinstance(handoff_data.get("workflow_discovery_result"), dict)
    assert isinstance(handoff_data.get("workflow_routing"), dict)
    assert handoff_data.get("selected_workflow_id") == TODO_REFRESH_WORKFLOW_ID

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


def test_non_standard_workflow_emits_custom_workflow_execution_summary(monkeypatch):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#custom_summary_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="done",
                states={"done": WorkflowStateSpec(state_id="done", terminal=True)},
            ),
            purpose="Workflow execution summary regression test.",
            source="test",
        )
    )

    def _run_workflow(_workflow_def: Any, **_kwargs: Any):
        return SimpleNamespace(
            completed=True,
            final_state="done",
            error=None,
            data={
                "response_text": "Workflow created.",
                "created_workflow_ids": ["#V#wf_new"],
                "alignment": {"updated_type_ids": ["#V#durable_workflow"]},
                "workflow_step_result_envelopes": [
                    {
                        "state_id": "prepare",
                        "action_id": "tool.prepare",
                        "action_outcome": "success",
                    },
                    {
                        "state_id": "persist",
                        "action_id": "tool.persist",
                        "action_outcome": "success",
                    },
                ],
                "workflow_terminal_effect_events": [
                    {
                        "state_id": "done",
                        "symbol": "#V#workflow_effect_custom_summary_done_terminal",
                        "alias": "workflow_effect_custom_summary_done_terminal",
                        "applied": True,
                    }
                ],
                "workflow_control_flow_events": [
                    {"status": "entered_state", "state_id": "prepare"},
                    {"status": "entered_state", "state_id": "persist"},
                ],
            },
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    llm = _CapturingLLM([selected_workflow_id.lower()])
    result = orchestrator.run(
        prompt="Create the workflow definition.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [{"concept_id": selected_workflow_id}],
            "match_count": 1,
        },
    )

    execution_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict) and entry.get("type") == "workflow_execution"
        ),
        None,
    )
    assert execution_entry is not None
    assert execution_entry["workflow_id"] == selected_workflow_id
    assert execution_entry["completed"] is True

    execution_summary = execution_entry.get("execution_summary")
    assert isinstance(execution_summary, dict)
    assert execution_summary["workflow_id"] == selected_workflow_id
    assert execution_summary["step_result_envelope_count"] == 2
    assert execution_summary["action_completed_count"] == 2
    assert execution_summary["action_success_count"] == 2
    assert execution_summary["terminal_effect_count"] == 1
    assert execution_summary["durable_side_effect_count"] == 2
    assert execution_summary["durable_side_effects"] == [
        {
            "mutation_kind": "created",
            "artefact_type": "workflow",
            "source_key": "created_workflow_ids",
            "source_path": "created_workflow_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#wf_new"],
        },
        {
            "mutation_kind": "updated",
            "artefact_type": "type",
            "source_key": "updated_type_ids",
            "source_path": "alignment.updated_type_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#durable_workflow"],
        },
    ]


def test_custom_workflow_run_applies_launch_contract_without_workflow_specific_glue(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#launch_contract_custom_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="prepare",
                states={
                    "prepare": WorkflowStateSpec(
                        state_id="prepare",
                        actions=(WorkflowActionInvocation(action_id="tool.prepare"),),
                        terminal=True,
                    )
                },
                metadata={
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["invitation_text"],
                        "input_mappings": [
                            {
                                "target_context_key": "invitation_text",
                                "source_expression": "inputs.prompt",
                                "extractor": "first_quoted_text",
                                "required": True,
                            },
                            {
                                "target_context_key": "candidate_workflow_ids",
                                "source_expression": "inputs.workflow_discovery_result.matches",
                                "extractor": "workflow_id_list",
                            },
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose="Custom launch-contract workflow for selector dispatch tests.",
            source="test",
        )
    )

    captured_data: dict[str, Any] = {}

    def _run_workflow(_workflow_def: Any, *, data: Mapping[str, Any], **_kwargs: Any):
        captured_data.update(dict(data))
        return SimpleNamespace(
            completed=True,
            final_state="prepare",
            error=None,
            data={"response_text": "Prepared."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    prompt = (
        'Run a meeting-invitation test on this invitation text:\n\n'
        '"Kia ora team, please join us on Tuesday at 2:00pm in Room 4 '
        'for a project planning meeting about the Q2 roadmap."'
    )
    discovery_result = {
        "matches": [
            {
                "concept_id": selected_workflow_id,
                "name": "Launch contract custom workflow",
                "is_executable": True,
                "executability_reason": "executable_now",
            },
            {
                "concept_id": "#V#synthetic_workflow_regression_suite_workflow",
                "name": "Synthetic workflow regression suite workflow",
                "is_executable": True,
                "executability_reason": "executable_now",
            },
        ],
        "candidates": [
            {
                "concept_id": selected_workflow_id,
                "name": "Launch contract custom workflow",
                "is_executable": True,
                "executability_reason": "executable_now",
            }
        ],
        "match_count": 2,
    }

    result = orchestrator.run(
        prompt=prompt,
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result=discovery_result,
        conversation_session_id="chat-launch",
        turn_id="turn-launch",
    )

    assert result.response_text == "Prepared."
    assert captured_data["invitation_text"] == (
        "Kia ora team, please join us on Tuesday at 2:00pm in Room 4 for a "
        "project planning meeting about the Q2 roadmap."
    )
    assert captured_data["candidate_workflow_ids"] == [
        selected_workflow_id,
        "#V#synthetic_workflow_regression_suite_workflow",
    ]
    assert captured_data["selected_workflow_id"] == selected_workflow_id

    launch_resolution = captured_data.get("workflow_launch_input_resolution")
    assert isinstance(launch_resolution, dict)
    assert launch_resolution.get("status") == "resolved"
    assert launch_resolution.get("resolved_inputs") == [
        "candidate_workflow_ids",
        "invitation_text",
    ]


def test_custom_workflow_launchability_promotes_launchable_replacement_candidate(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#non_launchable_custom_workflow"
    launchable_workflow_id = "#V#launchable_custom_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="prepare_spec",
                states={
                    "prepare_spec": WorkflowStateSpec(
                        state_id="prepare_spec",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_spec"),
                        ),
                        terminal=True,
                        metadata={"reads_context_keys": ["target_workflow_ids"]},
                    )
                },
            ),
            purpose="Non-launchable custom workflow for selector override tests.",
            source="test",
        )
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=launchable_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=launchable_workflow_id,
                initial_state="prepare_spec",
                states={
                    "prepare_spec": WorkflowStateSpec(
                        state_id="prepare_spec",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_spec"),
                        ),
                        terminal=True,
                        metadata={"reads_context_keys": ["invitation_text"]},
                    )
                },
                metadata={
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["invitation_text"],
                        "input_mappings": [
                            {
                                "target_context_key": "invitation_text",
                                "source_expression": "inputs.prompt",
                                "extractor": "first_quoted_text",
                                "required": True,
                            },
                            {
                                "target_context_key": "candidate_workflow_ids",
                                "source_expression": "inputs.workflow_discovery_result.matches",
                                "extractor": "workflow_id_list",
                            },
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose="Launchable custom workflow for selector override tests.",
            source="test",
        )
    )

    captured_execution: dict[str, Any] = {}

    def _run_workflow(
        workflow_def: WorkflowDefinition,
        *,
        data: Mapping[str, Any],
        **_kwargs: Any,
    ):
        captured_execution["workflow_id"] = workflow_def.workflow_id
        captured_execution["data"] = dict(data)
        return SimpleNamespace(
            completed=True,
            final_state="complete",
            error=None,
            data={
                "response_text": "Handled via safe tool pipeline fallback.",
                "final_response": "Handled via safe tool pipeline fallback.",
            },
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt=(
            'Run a meeting-invitation test on this invitation text:\n\n'
            '"Kia ora team, please join us on Tuesday at 2:00pm in Room 4 '
            'for a project planning meeting about the Q2 roadmap."'
        ),
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Non-launchable custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                },
                {
                    "concept_id": launchable_workflow_id,
                    "name": "Launchable custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Non-launchable custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                },
                {
                    "concept_id": launchable_workflow_id,
                    "name": "Launchable custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                },
            ],
            "match_count": 2,
        },
        conversation_session_id="chat-launch-override",
        turn_id="turn-launch-override",
    )

    assert result.response_text == "Handled via safe tool pipeline fallback."
    assert captured_execution["workflow_id"] == launchable_workflow_id

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == launchable_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"

    override_policy_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "custom_workflow_override_policy"
        ),
        None,
    )
    assert override_policy_entry is None
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == "selected_custom_workflow_not_launchable_from_turn_inputs"
        for entry in result.aux_llm_calls
    )

    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    assert [entry.get("boundary") for entry in dispatch_boundaries[-3:]] == [
        "execution_mode_selected",
        "workflow_handoff",
        "workflow_terminal",
    ]
    assert dispatch_boundaries[-3].get("selected_workflow_id") == launchable_workflow_id
    assert dispatch_boundaries[-2].get("selected_workflow_id") == launchable_workflow_id
    assert dispatch_boundaries[-1].get("selected_workflow_id") == launchable_workflow_id


def test_custom_workflow_launchability_override_failure_preserves_selector_decision_but_surfaces_truthful_failure(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="normalise_inputs",
                states={
                    "normalise_inputs": WorkflowStateSpec(
                        state_id="normalise_inputs",
                        actions=(WorkflowActionInvocation(action_id="tool.prepare_spec"),),
                        terminal=True,
                        metadata={"reads_context_keys": ["file_copy_concept_id"]},
                    )
                },
            ),
            purpose="Selected workflow whose launchability override path will fail.",
            source="test",
        )
    )

    def _unexpected_execute_workflow(*_args: Any, **_kwargs: Any):
        raise AssertionError(
            "execute_workflow should not run after launchability gate failure"
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _unexpected_execute_workflow)

    def _raise_override_failure(**_kwargs: Any) -> Any:
        raise AttributeError("override policy exploded")

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.choose_custom_workflow_override_candidate",
        _raise_override_failure,
    )

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2402.18144",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "match_count": 1,
        },
        conversation_session_id="chat-launchability-override-failure",
        turn_id="turn-launchability-override-failure",
    )

    assert (
        result.response_text
        == "I attempted to use tools but the tool-pipeline handoff failed before "
        "tool execution could begin. Please try again or report this issue."
    )
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "tool_contract_override"
    assert result.workflow_routing.source == "selector_override"
    assert result.extra_messages == ()
    assert result.tool_invocations == ()

    failure_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_override"
            and entry.get("reason")
            == "selector_unmatched_candidate_requires_safe_general_fallback"
        ),
        None,
    )
    assert failure_entry is not None
    assert failure_entry.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert failure_entry.get("requested_candidate_workflow_id") == selected_workflow_id
    assert failure_entry.get("prior_selected_workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID


def test_custom_workflow_override_prefers_semantically_fit_execution_candidate(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.recommend_workflow_with_policy",
        lambda **_kwargs: {
            "policy_active": False,
            "guidance_mode": "none",
            "candidate_scores": [],
            "ranked_candidate_ids": [],
        },
    )
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#non_launchable_custom_workflow"
    authoring_workflow_id = "#V#launchable_authoring_workflow"
    execution_workflow_id = "#V#meeting_invitation_testing_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="prepare_spec",
                states={
                    "prepare_spec": WorkflowStateSpec(
                        state_id="prepare_spec",
                        actions=(WorkflowActionInvocation(action_id="tool.prepare_spec"),),
                        terminal=True,
                        metadata={"reads_context_keys": ["target_workflow_ids"]},
                    )
                },
            ),
            purpose="Generic non-launchable custom workflow for selector override tests.",
            source="test",
        )
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=authoring_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=authoring_workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="workflow_authoring.identify_need"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "authoring",
                        "authoring_intent_required": True,
                        "prefer_existing_capability": True,
                    }
                },
            ),
            purpose=(
                "Create and verify executable workflows from a workflow "
                "description request."
            ),
            source="test",
        )
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=execution_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=execution_workflow_id,
                initial_state="prepare_spec",
                states={
                    "prepare_spec": WorkflowStateSpec(
                        state_id="prepare_spec",
                        actions=(WorkflowActionInvocation(action_id="tool.prepare_spec"),),
                        terminal=True,
                        metadata={"reads_context_keys": ["invitation_text"]},
                    )
                },
                metadata={
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["invitation_text"],
                        "input_mappings": [
                            {
                                "target_context_key": "invitation_text",
                                "source_expression": "inputs.prompt",
                                "extractor": "first_quoted_text",
                                "required": True,
                            }
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose="Run meeting invitation testing against an invitation specimen.",
            source="test",
        )
    )

    captured_execution: dict[str, Any] = {}

    def _run_workflow(
        workflow_def: WorkflowDefinition,
        *,
        data: Mapping[str, Any],
        **_kwargs: Any,
    ):
        captured_execution["workflow_id"] = workflow_def.workflow_id
        captured_execution["data"] = dict(data)
        return SimpleNamespace(
            completed=True,
            final_state="prepare_spec",
            error=None,
            data={"response_text": "Prepared via meeting invitation workflow."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt=(
            'Run a meeting invitation test on this invitation text:\n\n'
            '"Kia ora team, please join us on Tuesday at 2:00pm in Room 4 '
            'for a project planning meeting about the Q2 roadmap."'
        ),
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Non-launchable custom workflow",
                    "description": "Generic workflow with missing launch inputs.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.41,
                    "confidence_score": 0.41,
                },
                {
                    "concept_id": authoring_workflow_id,
                    "name": "Workflow creation workflow",
                    "description": (
                        "Create and verify executable workflows from a "
                        "workflow description request."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.58,
                    "confidence_score": 0.58,
                },
                {
                    "concept_id": execution_workflow_id,
                    "name": "Meeting invitation testing workflow",
                    "description": "Run meeting invitation testing against an invitation specimen.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.87,
                    "confidence_score": 0.87,
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Non-launchable custom workflow",
                    "description": "Generic workflow with missing launch inputs.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.41,
                    "confidence_score": 0.41,
                },
                {
                    "concept_id": authoring_workflow_id,
                    "name": "Workflow creation workflow",
                    "description": (
                        "Create and verify executable workflows from a "
                        "workflow description request."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.58,
                    "confidence_score": 0.58,
                },
                {
                    "concept_id": execution_workflow_id,
                    "name": "Meeting invitation testing workflow",
                    "description": "Run meeting invitation testing against an invitation specimen.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.87,
                    "confidence_score": 0.87,
                },
            ],
            "match_count": 3,
        },
        conversation_session_id="chat-launch-semantic-override",
        turn_id="turn-launch-semantic-override",
    )

    assert result.response_text == "Prepared via meeting invitation workflow."
    assert captured_execution["workflow_id"] == execution_workflow_id
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == execution_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"

    override_policy_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "custom_workflow_override_policy"
        ),
        None,
    )
    assert override_policy_entry is None


def test_launchability_replacement_declines_testing_workflow_for_conceptual_prompt(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.recommend_workflow_with_policy",
        lambda **_kwargs: {
            "policy_active": False,
            "guidance_mode": "none",
            "candidate_scores": [],
            "ranked_candidate_ids": [],
        },
    )
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#scholarly_paper_representation_workflow"
    testing_workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="normalise_inputs",
                states={
                    "normalise_inputs": WorkflowStateSpec(
                        state_id="normalise_inputs",
                        actions=(WorkflowActionInvocation(action_id="tool.prepare_spec"),),
                        terminal=True,
                        metadata={"reads_context_keys": ["file_copy_concept_id"]},
                    )
                },
            ),
            purpose=(
                "Canonical durable workflow for representing scholarly papers "
                "from file-copy artefacts, metadata, and verification requirements."
            ),
            source="test",
        )
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=testing_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=testing_workflow_id,
                initial_state="prepare_fixture",
                states={
                    "prepare_fixture": WorkflowStateSpec(
                        state_id="prepare_fixture",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="testing.prepare_arxiv_paper_ingestion_fixture"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "testing",
                        "authoring_intent_required": False,
                        "prefer_existing_capability": False,
                    },
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["prompt_text"],
                        "input_mappings": [
                            {
                                "target_context_key": "prompt_text",
                                "source_expression": "inputs.prompt",
                                "required": True,
                            }
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose=(
                "Execute the canonical arXiv ingestion workflow against one live "
                "arXiv paper, verify represented scholarly metadata and provenance, "
                "and clean up transient artefacts afterwards."
            ),
            source="test",
        )
    )

    execute_calls: list[dict[str, Any]] = []

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        execute_calls.append({"workflow_id": workflow_id})
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data={
                "final_response": "Fallback via generic tool pipeline.",
                "tool_messages": [],
                "invocations": [],
                "iteration_count": 1,
            },
            final_state="complete",
            completed=True,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    prompt = (
        "OK, thinking about the way papers are represented at the moment, think "
        "about papers under preparation. How should they be represented. What is "
        "common between them and published (or rejected papers) and what is "
        "unique to the under-preparation status. Are any ontological edits needed"
    )
    result = orchestrator.run(
        prompt=prompt,
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "description": (
                        "Canonical durable workflow for representing scholarly "
                        "papers from file-copy artefacts, metadata, and "
                        "verification requirements."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.914,
                    "confidence_score": 0.984,
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "description": (
                        "Canonical durable workflow for representing scholarly "
                        "papers from file-copy artefacts, metadata, and "
                        "verification requirements."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.914,
                    "confidence_score": 0.984,
                },
            ],
            "match_count": 2,
        },
        conversation_session_id="session-paper-representation-safe-fallback",
        turn_id="turn-paper-representation-safe-fallback",
    )

    assert execute_calls
    assert execute_calls[0]["workflow_id"] == TOOL_CALLING_WORKFLOW_ID

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "tool_contract_override"
    assert result.workflow_routing.source == "selector_override"
    assert result.response_text == "Fallback via generic tool pipeline."

    override_policy_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "custom_workflow_override_policy"
        ),
        None,
    )
    assert override_policy_entry is None

    selector_prompt_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict) and entry.get("type") == "workflow_selector_prompt"
        ),
        None,
    )
    assert selector_prompt_entry is not None
    excluded_candidates = selector_prompt_entry.get("discovery_excluded_candidates")
    assert isinstance(excluded_candidates, list)
    excluded_testing = next(
        (
            item
            for item in excluded_candidates
            if isinstance(item, dict) and item.get("concept_id") == testing_workflow_id
        ),
        None,
    )
    assert isinstance(excluded_testing, dict)
    assert excluded_testing.get("routing_profile_role") == "maintenance"
    assert (
        excluded_testing.get("routing_exclusion_reason")
        == "explicit_workflow_context_required_by_workflow_profile"
    )

    override_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_override"
            and entry.get("reason")
            == "selector_unmatched_candidate_requires_safe_general_fallback"
        ),
        None,
    )
    assert override_entry is not None
    assert override_entry.get("prior_selected_workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert override_entry.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert override_entry.get("requested_candidate_workflow_id") == selected_workflow_id
    assert "launch_viability_probe" not in (override_entry or {})

    override_reasons = {
        entry.get("reason")
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector_override"
    }
    assert (
        "selector_unmatched_candidate_requires_safe_general_fallback"
        in override_reasons
    )


def test_launchability_replacement_allows_testing_workflow_for_explicit_testing_prompt(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.recommend_workflow_with_policy",
        lambda **_kwargs: {
            "policy_active": False,
            "guidance_mode": "none",
            "candidate_scores": [],
            "ranked_candidate_ids": [],
        },
    )
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#scholarly_paper_representation_workflow"
    testing_workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="normalise_inputs",
                states={
                    "normalise_inputs": WorkflowStateSpec(
                        state_id="normalise_inputs",
                        actions=(WorkflowActionInvocation(action_id="tool.prepare_spec"),),
                        terminal=True,
                        metadata={"reads_context_keys": ["file_copy_concept_id"]},
                    )
                },
            ),
            purpose=(
                "Canonical durable workflow for representing scholarly papers "
                "from file-copy artefacts, metadata, and verification requirements."
            ),
            source="test",
        )
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=testing_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=testing_workflow_id,
                initial_state="prepare_fixture",
                states={
                    "prepare_fixture": WorkflowStateSpec(
                        state_id="prepare_fixture",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="testing.prepare_arxiv_paper_ingestion_fixture"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "testing",
                        "authoring_intent_required": False,
                        "prefer_existing_capability": False,
                    },
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["prompt_text"],
                        "input_mappings": [
                            {
                                "target_context_key": "prompt_text",
                                "source_expression": "inputs.prompt",
                                "required": True,
                            }
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose=(
                "Execute the canonical arXiv ingestion workflow against one live "
                "arXiv paper, verify represented scholarly metadata and provenance, "
                "and clean up transient artefacts afterwards."
            ),
            source="test",
        )
    )

    execute_calls: list[dict[str, Any]] = []

    def _run_workflow(
        workflow_def: WorkflowDefinition,
        *,
        data: Mapping[str, Any],
        **_kwargs: Any,
    ):
        execute_calls.append(
            {"workflow_id": workflow_def.workflow_id, "data": dict(data)}
        )
        return SimpleNamespace(
            completed=True,
            final_state="complete",
            error=None,
            data={"response_text": "Executed via arXiv testing workflow."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    prompt = (
        "Run the arXiv paper ingestion testing workflow on "
        "https://arxiv.org/abs/2603.21702 and verify title, authors, abstract, "
        "publication date, provenance, and cleanup."
    )
    result = orchestrator.run(
        prompt=prompt,
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "description": (
                        "Canonical durable workflow for representing scholarly "
                        "papers from file-copy artefacts, metadata, and "
                        "verification requirements."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.71,
                    "confidence_score": 0.71,
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "description": (
                        "Canonical durable workflow for representing scholarly "
                        "papers from file-copy artefacts, metadata, and "
                        "verification requirements."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.71,
                    "confidence_score": 0.71,
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                },
            ],
            "match_count": 2,
        },
        conversation_session_id="session-explicit-arxiv-testing",
        turn_id="turn-explicit-arxiv-testing",
    )

    assert execute_calls
    assert execute_calls[0]["workflow_id"] == testing_workflow_id

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == testing_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"

    override_policy_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "custom_workflow_override_policy"
        ),
        None,
    )
    assert override_policy_entry is None

    override_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_override"
            and entry.get("reason")
            == "selected_custom_workflow_not_launchable_from_turn_inputs"
        ),
        None,
    )
    assert override_entry is None


def test_custom_workflow_first_step_failure_projects_terminal_locality_before_tool_pipeline_fallback(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#launch_contract_failure_custom_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="prepare_spec",
                states={
                    "prepare_spec": WorkflowStateSpec(
                        state_id="prepare_spec",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_spec"),
                        ),
                        terminal=True,
                    )
                },
            ),
            purpose="Custom workflow failure locality projection test.",
            source="test",
        )
    )

    tool_pipeline_payload: dict[str, Any] = {}

    def _execute_workflow(workflow_id: str, **kwargs: Any):
        if workflow_id == selected_workflow_id:
            return SimpleNamespace(
                completed=False,
                final_state="prepare_spec",
                error="workflow_launch_input_resolution_failed:invitation_text",
                data={
                    "response_text": (
                        f"Workflow {selected_workflow_id} could not start because "
                        "required launch inputs were unresolved: invitation_text."
                    ),
                    "workflow_launch_input_resolution": {
                        "status": "failed",
                        "unresolved_required_inputs": ["invitation_text"],
                        "failing_state_id": "prepare_spec",
                        "failing_action_id": "tool.prepare_spec",
                    },
                },
            )
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            tool_pipeline_payload.update(dict(kwargs))
            return SimpleNamespace(
                completed=True,
                final_state="complete",
                error=None,
                data={
                    "final_response": "Recovered through the general tool workflow.",
                    "tool_messages": [],
                    "invocations": [{"tool": "extract_url"}],
                    "iteration_count": 1,
                },
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    progress_events: list[dict[str, Any]] = []
    orchestrator.set_progress_callback(lambda info: progress_events.append(dict(info)))

    result = orchestrator.run(
        prompt=(
            'Run the meeting invitation testing workflow on this invitation:\n\n'
            '"Kia ora team, please join us on Tuesday at 2:00pm in Room 4."'
        ),
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Launch contract failure custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Launch contract failure custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "match_count": 1,
        },
            conversation_session_id="chat-launch-failure",
            turn_id="turn-launch-failure",
        )

    assert result.response_text == "Recovered through the general tool workflow."
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert tool_pipeline_payload["data"]["prior_failed_selected_workflow"] == {
        "workflow_id": selected_workflow_id,
        "completed": False,
        "tool_progress_detected": False,
        "final_state": "prepare_spec",
        "operational_error": "workflow_launch_input_resolution_failed:invitation_text",
        "user_visible_failure_text": (
            f"Workflow {selected_workflow_id} could not start because required "
            "launch inputs were unresolved: invitation_text."
        ),
    }

    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    terminal_boundary = next(
        (
            entry
            for entry in dispatch_boundaries
            if entry.get("boundary") == "workflow_terminal"
            and entry.get("selected_execution_mode") == "custom_workflow"
        ),
        None,
    )
    assert terminal_boundary is not None
    assert terminal_boundary.get("status") == "failed"
    assert terminal_boundary.get("selected_execution_mode") == "custom_workflow"
    assert terminal_boundary.get("selected_workflow_id") == selected_workflow_id
    assert terminal_boundary.get("dispatch_workflow_id") == selected_workflow_id
    assert terminal_boundary.get("final_state") == "prepare_spec"
    assert terminal_boundary.get("reason") == "workflow_launch_input_resolution_failed"
    assert terminal_boundary.get("workflow_launch_input_resolution_status") == "failed"
    assert terminal_boundary.get("unresolved_required_inputs") == ["invitation_text"]
    assert terminal_boundary.get("failing_state_id") == "prepare_spec"
    assert terminal_boundary.get("failing_action_id") == "tool.prepare_spec"
    assert terminal_boundary.get("continued_to_tool_pipeline") is True

    terminal_progress = next(
        (
            entry
            for entry in reversed(progress_events)
            if entry.get("phase_label") == "Workflow terminal state"
        ),
        None,
    )
    assert terminal_progress is not None
    assert terminal_progress.get("selected_workflow_id") == selected_workflow_id
    assert terminal_progress.get("selected_workflow_name") == (
        "Launch contract failure custom workflow"
    )
    assert terminal_progress.get("workflow_selector_verdict") == "rag_selected"
    assert terminal_progress.get("workflow_selector_source") == "selector"
    assert isinstance(terminal_progress.get("workflow_selection_rationale"), str)
    assert terminal_progress.get("workflow_selection_rationale")


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
            CHAT_ASSISTANT_WORKFLOW_ID,  # selector verdict
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
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,  # selector verdict
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
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"

    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    assert dispatch_boundaries[-1].get("boundary") == "workflow_terminal"
    assert dispatch_boundaries[-1].get("selected_execution_mode") == "direct_response"


def test_workflow_selector_uses_provider_aware_classifier_fallback(monkeypatch):
    """Selector classification should honour provider-aware stage candidates.

    The classifier policy may prefer a local Ollama model for cheap routing.
    When that candidate is unreachable, workflow dispatch must fall back to the
    next candidate without trying the Ollama model name through the default
    OpenAI client.
    """

    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.recommend_workflow_with_policy",
        lambda **_kwargs: {
            "guidance_mode": "none",
            "recommended_workflow_id": None,
            "candidate_scores": [],
        },
    )

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    llm = _CapturingLLM(["Hello! How can I help?"])

    ollama_candidate = _ModelCandidate(
        provider="ollama",
        model="granite3.3:2b",
        raw="ollama:granite3.3:2b",
        source="policy",
        host="http://localhost:11434",
    )
    openai_candidate = _ModelCandidate(
        provider="openai",
        model="gpt-5.2-chat-latest",
        raw="openai:gpt-5.2-chat-latest",
        source="policy",
    )

    original_stage_model_candidates = orchestrator._stage_model_candidates
    original_create_client_for_candidate = orchestrator._create_client_for_candidate

    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **kwargs: (
            [ollama_candidate, openai_candidate]
            if kwargs.get("stage") == "classifier"
            else original_stage_model_candidates(**kwargs)
        ),
    )

    class _SelectorFallbackClient:
        def generate(self, *_args: Any, **_kwargs: Any) -> str:
            return CHAT_ASSISTANT_WORKFLOW_ID

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **kwargs: Any,
    ) -> tuple[Any, str | None, Mapping[str, Any]]:
        if candidate.provider == "ollama":
            return (
                object(),
                "granite3.3:2b",
                {
                    "provider": "ollama",
                    "model": "granite3.3:2b",
                    "raw": candidate.raw,
                    "source": candidate.source,
                    "host": "http://localhost:11434",
                },
            )
        if candidate.provider == "openai":
            return (
                _SelectorFallbackClient(),
                "gpt-5.2-chat-latest",
                {
                    "provider": "openai",
                    "model": "gpt-5.2-chat-latest",
                    "raw": candidate.raw,
                    "source": candidate.source,
                    "host": None,
                },
            )
        return original_create_client_for_candidate(candidate, **kwargs)

    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        _create_client_for_candidate,
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda *, telemetry: (
            {
                "provider": "ollama",
                "host": "http://localhost:11434",
                "probe_url": "http://localhost:11434/api/tags",
                "probe_timeout_ms": 1200,
                "duration_ms": 7,
                "reachable": False,
                "error": "connection refused",
                "error_class": "ConnectionError",
            }
            if telemetry.get("provider") == "ollama"
            else None
        ),
    )

    captured_progress: list[dict[str, Any]] = []
    tracker = ProgressTracker(callback=lambda info: captured_progress.append(dict(info)))

    result = orchestrator.run(
        prompt="Hi there",
        context=[],
        llm_client=llm,
        model="gpt-5.2-chat-latest",
        user_namespace="#V#user",
        progress_tracker=tracker,
    )

    assert result.response_text == "Hello! How can I help?"
    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"] == "Hi there"

    workflow_dispatch_events = [
        entry for entry in captured_progress if entry.get("stage") == "workflow_dispatch"
    ]
    failed_attempt = next(
        entry
        for entry in workflow_dispatch_events
        if entry.get("status") == "llm_call_end"
        and entry.get("fallback_attempt_no") == 1
    )
    assert failed_attempt["success"] is False
    assert failed_attempt["failure_kind"] == "provider_unreachable"
    assert failed_attempt["provider"] == "ollama"

    succeeded_attempt = next(
        entry
        for entry in workflow_dispatch_events
        if entry.get("status") == "llm_call_end"
        and entry.get("fallback_attempt_no") == 2
    )
    assert succeeded_attempt["success"] is True
    assert succeeded_attempt["fallback_used"] is True
    assert succeeded_attempt["provider"] == "openai"

    stage_summary = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_model_policy_stage"
        and entry.get("stage") == "workflow_dispatch"
    )
    assert stage_summary["policy_stage"] == "classifier"
    assert stage_summary["selected"]["provider"] == "openai"

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    assert selector_entry["policy_stage"] == "classifier"
    assert selector_entry["candidate"]["provider"] == "openai"
    assert selector_entry["model_name"] == "gpt-5.2-chat-latest"
    assert selector_entry["prompt_provenance"]["prompt_mode"] == (
        "rag_first_candidate_selector"
    )
    assert selector_entry["requested_prompt_ids"] == [
        "#V#chat_turn_classifier_prompt"
    ]
    assert selector_entry["candidate_list"]["text"]
    assert selector_entry["selection_metadata"]["selection_resolution"] == (
        "candidate_label_exact_match"
    )
    assert selector_entry["response"]["text"] == CHAT_ASSISTANT_WORKFLOW_ID

    selector_prompt_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector_prompt"
    )
    assert selector_prompt_entry["prompt"]["text"]
    assert selector_prompt_entry["candidate_entries"]

    assert stage_summary["fallback_attempts"][0]["failure_kind"] == (
        "provider_unreachable"
    )
    assert stage_summary["fallback_attempts"][1]["response"]["text"] == (
        CHAT_ASSISTANT_WORKFLOW_ID
    )


def test_workflow_selector_reuses_augmented_context_and_tracks_context_lineage(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    base_context = [
        {
            "role": "system",
            "content": "CURRENT USER CONTEXT: Test User (#V#test_user)",
        },
        {"role": "assistant", "content": "Earlier context that still matters."},
        {"role": "user", "content": "What is my name?"},
    ]
    monkeypatch.setattr(
        orchestrator,
        "_build_augmented_context",
        lambda *args, **kwargs: list(base_context),
    )

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,
            "Your name is Test User.",
        ]
    )

    result = orchestrator.run(
        prompt="What is my name?",
        context=[],
        llm_client=llm,
        model="test-model",
        user_namespace="#V#user",
    )

    assert result.response_text == "Your name is Test User."
    selector_context = llm.calls[0]["context"]
    assert any(
        isinstance(message, dict)
        and message.get("content") == "CURRENT USER CONTEXT: Test User (#V#test_user)"
        for message in selector_context
    )
    assert any(
        isinstance(message, dict)
        and message.get("role") == "user"
        and message.get("content") == "What is my name?"
        for message in selector_context
    )

    stage_summary = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_model_policy_stage"
        and entry.get("stage") == "workflow_dispatch"
    )
    request = stage_summary["request"]
    assert request["context_lineage"]["base_context_source"] == "augmented_context"
    assert request["context_lineage"]["stage_added_message_count"] == 1
    assert request["context_lineage"]["stage_added_messages"][0]["role"] == "system"
    assert request["context_lineage"]["base_context_summary"]["message_count"] == 3
    assert request["context_summary"]["message_count"] == len(selector_context)
    assert request["context_summary"]["role_counts"]["system"] >= 2

    selector_prompt_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector_prompt"
    )
    assert selector_prompt_entry["context_lineage"]["base_context_source"] == (
        "augmented_context"
    )
    assert selector_prompt_entry["context_lineage"]["stage_added_message_count"] == 1


def test_mutative_wording_does_not_override_plain_response_routing(monkeypatch):
    """Mutative wording alone must not trigger Python-side routing overrides."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    # Write-policy checks in orchestrator should only keep this on the
    # tool-calling path when mutative intent is explicitly requested.
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
    )

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,  # selector verdict
            "Plain response only.",
        ]
    )

    result = orchestrator.run(
        prompt="Create a concept link in Vontology.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Plain response only."
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "workflow_selector_override"
        for entry in result.aux_llm_calls
    )
    gate_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "write_policy_gate"
            and entry.get("stage") == "routing"
        ),
        None,
    )
    assert gate_entry is not None
    assert gate_entry.get("gate_state") == "not_applied"
    assert gate_entry.get("reason") == "workflow_llm_owns_mutation_routing"


def test_launchable_custom_workflow_is_not_python_overridden_from_mutative_wording(
    monkeypatch,
):
    """Discovered custom workflows must not be promoted from mutative wording alone."""

    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#launchable_mutative_custom_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_custom"),
                        ),
                        terminal=True,
                    )
                },
            ),
            purpose=(
                "Create concept links in Vontology via a specialised "
                "relationship editing workflow."
            ),
            source="test",
        )
    )

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
    )

    captured_execution: dict[str, Any] = {}

    def _run_workflow(
        workflow_def: WorkflowDefinition,
        *,
        data: Mapping[str, Any],
        **_kwargs: Any,
    ):
        captured_execution["workflow_id"] = workflow_def.workflow_id
        captured_execution["data"] = dict(data)
        return SimpleNamespace(
            completed=True,
            final_state="complete",
            error=None,
            data={"response_text": "Executed via specialised workflow."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt="Create a concept link in Vontology.",
        context=[],
        llm_client=_CapturingLLM([CHAT_ASSISTANT_WORKFLOW_ID, "Plain response only."]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Launchable mutative custom workflow",
                    "description": (
                        "Create concept links in Vontology via a specialised "
                        "relationship editing workflow."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.82,
                    "confidence_score": 0.82,
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Launchable mutative custom workflow",
                    "description": (
                        "Create concept links in Vontology via a specialised "
                        "relationship editing workflow."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.82,
                    "confidence_score": 0.82,
                }
            ],
            "match_count": 1,
        },
        conversation_session_id="session-mutative-custom",
        turn_id="turn-mutative-custom",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Plain response only."
    assert captured_execution == {}
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") in {"workflow_selector_override", "custom_workflow_override_policy"}
        for entry in result.aux_llm_calls
    )


def test_unrelated_execution_workflow_is_not_python_declined_or_promoted_from_prompt_text(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    unrelated_workflow_id = "#V#sail_phd_student_onboarding_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=unrelated_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=unrelated_workflow_id,
                initial_state="collect_student_info",
                states={
                    "collect_student_info": WorkflowStateSpec(
                        state_id="collect_student_info",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="tool.prepare_student_onboarding"
                            ),
                        ),
                        terminal=True,
                    )
                },
            ),
            purpose=(
                "Onboard new PhD students into SAIL by collecting student "
                "details and setting up onboarding tasks."
            ),
            source="test",
        )
    )

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
    )

    execute_calls: list[dict[str, Any]] = []

    def _run_workflow(
        workflow_def: WorkflowDefinition,
        *,
        data: Mapping[str, Any],
        **_kwargs: Any,
    ):
        execute_calls.append(
            {"workflow_id": workflow_def.workflow_id, "data": dict(data)}
        )
        return SimpleNamespace(
            completed=True,
            final_state="completed",
            error=None,
            data={"response_text": "Tool pipeline executed instead."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt=(
            "OK, taking into account the way papers are represented at the moment, "
            "think about papers under preparation. How should they be represented. "
            "What is common between them and published (or rejected papers) and what "
            "is unique to the under-preparation status. Are any ontological edits "
            "needed. If so, list the new types and relations needed."
        ),
        context=[],
        llm_client=_CapturingLLM([CHAT_ASSISTANT_WORKFLOW_ID, "Plain response only."]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": unrelated_workflow_id,
                    "name": "Sail Phd Student Onboarding Workflow",
                    "description": (
                        "Onboarding workflow for new PhD students joining the "
                        "SAIL research group."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                }
            ],
            "candidates": [
                {
                    "concept_id": unrelated_workflow_id,
                    "name": "Sail Phd Student Onboarding Workflow",
                    "description": (
                        "Onboarding workflow for new PhD students joining the "
                        "SAIL research group."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                }
            ],
            "match_count": 1,
        },
        conversation_session_id="session-paper-representation-routing",
        turn_id="turn-paper-representation-routing",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Plain response only."
    assert execute_calls == []
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") in {"workflow_selector_override", "custom_workflow_override_policy"}
        for entry in result.aux_llm_calls
    )


def test_authoring_workflow_query_is_left_to_selector_without_python_semantic_override(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    authoring_workflow_id = "#V#launchable_authoring_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=authoring_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=authoring_workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="workflow_authoring.identify_need"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "authoring",
                        "authoring_intent_required": True,
                        "prefer_existing_capability": True,
                    }
                },
            ),
            purpose=(
                "Create and verify executable workflows from a workflow "
                "description request."
            ),
            source="test",
        )
    )

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
    )

    execute_calls: list[dict[str, Any]] = []

    def _run_workflow(
        workflow_def: WorkflowDefinition,
        *,
        data: Mapping[str, Any],
        **_kwargs: Any,
    ):
        execute_calls.append(
            {"workflow_id": workflow_def.workflow_id, "data": dict(data)}
        )
        return SimpleNamespace(
            completed=True,
            final_state="complete",
            error=None,
            data={"response_text": "Executed via workflow."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt="Is there already a workflow for creating a meeting instance?",
        context=[],
        llm_client=_CapturingLLM([CHAT_ASSISTANT_WORKFLOW_ID, "Plain response only."]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": authoring_workflow_id,
                    "name": "Workflow creation workflow",
                    "description": (
                        "Create and verify executable workflows from a "
                        "workflow description request."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.93,
                    "confidence_score": 0.93,
                }
            ],
            "candidates": [
                {
                    "concept_id": authoring_workflow_id,
                    "name": "Workflow creation workflow",
                    "description": (
                        "Create and verify executable workflows from a "
                        "workflow description request."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.93,
                    "confidence_score": 0.93,
                }
            ],
            "match_count": 1,
        },
        conversation_session_id="session-mutative-authoring-decline",
        turn_id="turn-mutative-authoring-decline",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Plain response only."
    assert execute_calls == []
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") in {"workflow_selector_override", "custom_workflow_override_policy"}
        for entry in result.aux_llm_calls
    )


def test_prepare_selector_discovered_matches_preserves_discovery_exclusion_reason(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#search_concept_and_instances_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(),
                        terminal=True,
                    )
                },
            ),
            purpose="Inspect concepts and their instances.",
            source="test",
        )
    )

    included, excluded = orchestrator._prepare_selector_discovered_matches(
        {
            "candidates": [
                {
                    "concept_id": workflow_id,
                    "name": "Search Concept And Instances Workflow",
                    "description": "Inspect concepts and their instances.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.91,
                    "confidence_score": 0.91,
                    "is_policy_safe": False,
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                }
            ]
        },
        turn_text="Look up #V#timothy_pistotti and inspect the concept relations.",
    )

    assert included == []
    assert len(excluded) == 1
    assert excluded[0]["routing_eligible"] is False
    assert excluded[0]["routing_exclusion_reason"] == "missing_authoritative_purpose"
    assert excluded[0]["candidate_reason"] == "discovered_workflow_excluded"


def test_prepare_selector_discovered_matches_excludes_authoring_profile_without_explicit_authoring_request(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#launchable_authoring_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="workflow_authoring.identify_need"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "authoring",
                        "authoring_intent_required": True,
                        "prefer_existing_capability": True,
                    }
                },
            ),
            purpose="Create and verify executable workflows from a workflow description request.",
            source="test",
        )
    )

    included, excluded = orchestrator._prepare_selector_discovered_matches(
        {
            "candidates": [
                {
                    "concept_id": workflow_id,
                    "name": "Workflow creation workflow",
                    "description": "Create and verify executable workflows from a workflow description request.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.93,
                    "confidence_score": 0.93,
                }
            ]
        },
        turn_text=(
            "The enrichment workflow isn't the right one. We need a new "
            "search workflow eventually, but manually retrieve "
            "#V#timothy_pistotti first and do not run an existing workflow."
        ),
    )

    assert included == []
    assert len(excluded) == 1
    assert excluded[0]["routing_profile"]["role"] == "authoring"
    assert (
        excluded[0]["routing_exclusion_reason"]
        == "authoring_intent_required_by_workflow_profile"
    )
    assert excluded[0]["routing_profile_role"] == "authoring"
    assert (
        excluded[0]["routing_policy_lexical_signals"]["explicit_authoring_request"]
        is False
    )


def test_prepare_selector_discovered_matches_allows_authoring_profile_for_explicit_authoring_request(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#launchable_authoring_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="workflow_authoring.identify_need"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "authoring",
                        "authoring_intent_required": True,
                        "prefer_existing_capability": True,
                    }
                },
            ),
            purpose="Create and verify executable workflows from a workflow description request.",
            source="test",
        )
    )

    included, excluded = orchestrator._prepare_selector_discovered_matches(
        {
            "candidates": [
                {
                    "concept_id": workflow_id,
                    "name": "Workflow creation workflow",
                    "description": "Create and verify executable workflows from a workflow description request.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.93,
                    "confidence_score": 0.93,
                }
            ]
        },
        turn_text="Create a new workflow to inspect PhD supervision relations.",
    )

    assert excluded == []
    assert len(included) == 1
    assert included[0]["routing_profile"]["role"] == "authoring"
    assert included[0]["routing_eligible"] is True
    assert included[0]["routing_profile_role"] == "authoring"
    assert (
        included[0]["routing_policy_lexical_signals"]["explicit_authoring_request"]
        is True
    )


def test_prepare_selector_discovered_matches_excludes_maintenance_profile_without_explicit_workflow_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="testing.prepare_arxiv_paper_ingestion_fixture"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "testing",
                        "authoring_intent_required": False,
                        "explicit_workflow_context_required": True,
                        "prefer_existing_capability": False,
                    }
                },
            ),
            purpose=(
                "Execute the canonical arXiv ingestion workflow against one live "
                "arXiv paper, verify represented scholarly metadata and provenance, "
                "and clean up transient artefacts afterwards."
            ),
            source="test",
        )
    )

    included, excluded = orchestrator._prepare_selector_discovered_matches(
        {
            "candidates": [
                {
                    "concept_id": workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": "Run the canonical arXiv ingestion workflow as a test.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.77,
                    "confidence_score": 0.84,
                }
            ]
        },
        turn_text="https://arxiv.org/abs/2411.04983",
    )

    assert included == []
    assert len(excluded) == 1
    assert excluded[0]["routing_profile"]["role"] == "testing"
    assert excluded[0]["routing_profile"][
        "explicit_workflow_context_required"
    ] is True
    assert (
        excluded[0]["routing_exclusion_reason"]
        == "explicit_workflow_context_required_by_workflow_profile"
    )
    assert excluded[0]["routing_profile_role"] == "maintenance"
    assert (
        excluded[0]["routing_policy_lexical_signals"]["workflow_query_intent"]
        is False
    )


def test_prepare_selector_discovered_matches_allows_maintenance_profile_for_explicit_workflow_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="testing.prepare_arxiv_paper_ingestion_fixture"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "testing",
                        "authoring_intent_required": False,
                        "explicit_workflow_context_required": True,
                        "prefer_existing_capability": False,
                    }
                },
            ),
            purpose=(
                "Execute the canonical arXiv ingestion workflow against one live "
                "arXiv paper, verify represented scholarly metadata and provenance, "
                "and clean up transient artefacts afterwards."
            ),
            source="test",
        )
    )

    included, excluded = orchestrator._prepare_selector_discovered_matches(
        {
            "candidates": [
                {
                    "concept_id": workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": "Run the canonical arXiv ingestion workflow as a test.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ]
        },
        turn_text=(
            "Run the arXiv paper ingestion testing workflow on "
            "https://arxiv.org/abs/2411.04983 and verify the workflow result."
        ),
    )

    assert excluded == []
    assert len(included) == 1
    assert included[0]["routing_profile"]["role"] == "testing"
    assert included[0]["routing_profile"][
        "explicit_workflow_context_required"
    ] is True
    assert included[0]["routing_eligible"] is True
    assert (
        included[0]["routing_policy_lexical_signals"]["workflow_query_intent"]
        is True
    )


def test_explicit_tool_requirement_is_telemetry_visible_even_without_python_routing_override(
    monkeypatch,
):
    """Explicit tool mentions should be surfaced in telemetry without forcing routing."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"workflow_list_definitions": {"category": "read"}},
    )
    monkeypatch.setattr(
        orchestrator._gateway,
        "invoke",
        lambda _tool_name, _payload: _InvokeResult({"workflows": []}),
    )

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,
            "I inspected workflow definitions.",
        ]
    )

    result = orchestrator.run(
        prompt="Call workflow_list_definitions and confirm what exists.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"

    requirement_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "prompt_tool_requirements_preflight"
            and entry.get("stage") == "workflow_dispatch"
        ),
        None,
    )
    assert requirement_entry is not None
    assert requirement_entry.get("required_tools") == ["workflow_list_definitions"]
    assert requirement_entry.get("missing_tools") == ["workflow_list_definitions"]
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "workflow_selector_override"
        for entry in result.aux_llm_calls
    )


def test_incidental_url_prompt_stays_on_plain_response_path(monkeypatch):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"resilient_extract_url": {"category": "read"}},
    )

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,
            "Meeting noted.",
        ]
    )

    result = orchestrator.run(
        prompt="Meeting notice: Zoom link https://example.com/join/abc for tomorrow's call.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.tool_invocations == ()
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason") == "required_prompt_tools_missing_preselector"
        for entry in result.aux_llm_calls
    )


def test_multi_surface_turn_contract_overrides_selected_custom_workflow_to_tool_pipeline(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#concept_search_instance_retrieval_workflow"

    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Inspect represented concepts and relations for retrieval questions.",
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda **_kwargs: ("You are Von.", "#V#test_base_system_prompt"),
    )

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {
            "search_knowledge_base": {"category": "read"},
            "search_concepts": {"category": "read"},
            "search_arxiv": {"category": "read"},
            "jira_search": {"category": "read"},
        },
    )

    execute_calls: list[str] = []

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        execute_calls.append(workflow_id)
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data={
                "final_response": "Research briefing via tool pipeline.",
                "tool_messages": [],
                "invocations": [],
                "iteration_count": 1,
            },
            final_state="complete",
            completed=True,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt=(
            "Prepare a short research briefing for me: my represented papers, "
            "relevant recent arXiv work, and any linked Jira tasks."
        ),
        context=[
            {
                "role": "system",
                "content": (
                    "Expected answer contract for this turn:\n"
                    "- Success target: A concise research briefing comprising "
                    "represented papers, recent relevant arXiv work, and linked Jira tasks.\n"
                    "- Grounding requirement: Papers must be grounded via authorship "
                    "or ownership relationships.\n"
                    "- Selector guidance: Use KB/concept retrieval, arXiv search, "
                    "and Jira retrieval."
                ),
            }
        ],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 0.96,
                        "reasoning": (
                            "The specialised concept-search workflow is best for "
                            "grounded represented retrieval."
                        ),
                    }
                )
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": "Retrieve represented concepts and relations.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": "Retrieve represented concepts and relations.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ],
            "match_count": 1,
        },
    )

    assert execute_calls == [TOOL_CALLING_WORKFLOW_ID]
    assert result.response_text == "Research briefing via tool pipeline."
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "tool_contract_override"
    assert result.workflow_routing.source == "selector_override"

    preflight_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "prompt_tool_requirements_preflight"
            and entry.get("stage") == "workflow_dispatch"
        ),
        None,
    )
    assert preflight_entry is not None
    assert preflight_entry.get("contract_required_tools") == [
        "search_knowledge_base",
        "search_concepts",
        "search_arxiv",
        "jira_search",
    ]
    assert preflight_entry.get("required_tool_surface_families") == [
        "knowledge_base",
        "arxiv",
        "jira",
    ]

    override_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_override"
            and entry.get("reason")
            == "selected_custom_workflow_cannot_satisfy_multi_surface_turn_contract"
        ),
        None,
    )
    assert override_entry is not None
    assert override_entry.get("prior_selected_workflow_id") == selected_workflow_id
    assert override_entry.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert override_entry.get("turn_contract_required_tools") == [
        "search_knowledge_base",
        "search_concepts",
        "search_arxiv",
        "jira_search",
    ]
    assert override_entry.get("turn_contract_external_surface_families") == [
        "arxiv",
        "jira",
    ]


def test_url_read_prompt_stays_selector_owned_without_python_url_preselection(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    gateway_calls: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"resilient_extract_url": {"category": "read"}},
    )

    def _invoke(tool_name: str, payload: Mapping[str, Any]):
        gateway_calls.append((tool_name, dict(payload)))
        return _InvokeResult(
            {
                "success": True,
                "url": payload.get("url"),
                "title": "Example report",
                "content": "Example report body",
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,
            "I can help with that.",
        ]
    )

    result = orchestrator.run(
        prompt="Please read this: https://example.com/report",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert gateway_calls == []
    assert result.tool_invocations == ()

    preflight_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "prompt_tool_requirements_preflight"
            and entry.get("stage") == "workflow_dispatch"
        ),
        None,
    )
    assert preflight_entry is not None
    assert preflight_entry.get("required_tools") == []
    assert preflight_entry.get("required_url_extraction_tool") is None
    assert preflight_entry.get("required_url_extraction_url") is None
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "workflow_selector_override"
        for entry in result.aux_llm_calls
    )


def test_explicit_workflow_prompt_is_not_python_reinterpreted_into_custom_dispatch(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"download_paper": {"category": "read"}},
    )

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="testing.prepare_arxiv_paper_ingestion_fixture"
                            ),
                        ),
                        terminal=True,
                    )
                },
            ),
            purpose=(
                "Execute the canonical arXiv ingestion workflow against one live "
                "arXiv paper, verify represented scholarly metadata and provenance, "
                "and clean up transient artefacts afterwards."
            ),
            source="test",
        )
    )

    captured_execution: dict[str, Any] = {}

    def _run_workflow(
        workflow_def: WorkflowDefinition,
        *,
        data: Mapping[str, Any],
        **_kwargs: Any,
    ):
        captured_execution["workflow_id"] = workflow_def.workflow_id
        captured_execution["data"] = dict(data)
        return SimpleNamespace(
            completed=True,
            final_state="complete",
            error=None,
            data={"response_text": "Executed via arXiv testing workflow."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    prompt = (
        "Run the arXiv paper ingestion testing workflow on "
        "https://arxiv.org/abs/2603.21702. Use the workflow itself to verify "
        "title, authors, abstract, publication date, provenance, and cleanup."
    )
    result = orchestrator.run(
        prompt=prompt,
        context=[],
        llm_client=_CapturingLLM([CHAT_ASSISTANT_WORKFLOW_ID, "Plain response only."]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against one "
                        "live arXiv paper and verify title, authors, abstract, "
                        "publication date, provenance, and cleanup."
                    ),
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against one "
                        "live arXiv paper and verify title, authors, abstract, "
                        "publication date, provenance, and cleanup."
                    ),
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "match_count": 1,
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Plain response only."
    assert captured_execution == {}
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") in {"workflow_selector_override", "custom_workflow_override_policy"}
        for entry in result.aux_llm_calls
    )


def test_explicit_arxiv_representation_request_selects_representation_workflow_over_generic_tool_calling(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Canonical arXiv wrapper workflow that normalises an arXiv source, "
            "fetches authoritative metadata, and delegates to scholarly-paper "
            "representation."
        ),
    )

    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        final_state="complete",
        data={"response_text": "Executed via arXiv representation workflow."},
    )

    result = orchestrator.run(
        prompt="Download and represent 2603.19312v1 arxiv",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 0.97,
                        "reasoning": (
                            "The request explicitly asks to download and represent "
                            "an arXiv paper, and the specialised arXiv "
                            "representation workflow is executable."
                        ),
                    }
                )
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                }
            ],
            "match_count": 1,
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Executed via arXiv representation workflow."

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    candidate_list_text = str(selector_entry.get("candidate_list", {}).get("text") or "")
    assert selected_workflow_id in candidate_list_text
    assert TOOL_CALLING_WORKFLOW_ID in candidate_list_text
    assert "relevance 100%" in candidate_list_text
    assert "confidence 100%" in candidate_list_text
    assert "routing eligible" in candidate_list_text
    assert "executable" in candidate_list_text
    assert selector_entry.get("workflow_id") == selected_workflow_id


def test_bare_arxiv_url_with_authoritative_definition_stays_launchable(
    _reset_mock_db: Any,
    monkeypatch,
):
    bootstrap_canonical_paper_representation_workflows()
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID

    authoritative_definition = load_workflow_definition_from_vontology(
        selected_workflow_id
    )
    assert authoritative_definition is not None
    assert authoritative_definition.metadata.get("launch_contract") == {
        "schema_version": "launch_contract.v1",
        "preconditions": [
            {
                "type": "context_key_present",
                "key": "prompt",
                "required": True,
            }
        ],
    }
    assert authoritative_definition.metadata.get("launch_contract_source") == (
        "text_relation:#V#has_launch_contract"
    )
    assert authoritative_definition.metadata.get("launch_input_contract", {}).get(
        "required_inputs"
    ) == ["prompt"]

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=authoritative_definition,
            purpose=(
                "Canonical arXiv wrapper workflow that normalises an arXiv source, "
                "fetches authoritative metadata, and delegates to scholarly-paper "
                "representation."
            ),
            source="authoritative_vontology_test",
        )
    )

    executed_workflow_ids: list[str] = []

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        executed_workflow_ids.append(workflow_id)
        if workflow_id != selected_workflow_id:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data={
                "response_text": "Executed via authoritative arXiv representation workflow."
            },
            final_state="complete",
            completed=True,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2411.04983",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 0.99,
                        "reasoning": (
                            "A bare arXiv URL should use the specialised arXiv "
                            "representation workflow, and the authoritative launch "
                            "metadata is satisfied by the prompt."
                        ),
                    }
                )
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, and delegates to "
                        "scholarly-paper representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, and delegates to "
                        "scholarly-paper representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                }
            ],
            "match_count": 1,
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == (
        "Executed via authoritative arXiv representation workflow."
    )
    assert executed_workflow_ids == [selected_workflow_id]
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == "selected_custom_workflow_launchability_requires_safe_general_fallback"
        for entry in result.aux_llm_calls
    )


def test_bare_arxiv_url_excludes_testing_workflow_before_selector_and_routes_to_representation_workflow(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    testing_workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"

    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Canonical arXiv wrapper workflow that normalises an arXiv source, "
            "fetches authoritative metadata, and delegates to scholarly-paper "
            "representation."
        ),
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=testing_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=testing_workflow_id,
                initial_state="prepare_fixture",
                states={
                    "prepare_fixture": WorkflowStateSpec(
                        state_id="prepare_fixture",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="testing.prepare_arxiv_paper_ingestion_fixture"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "testing",
                        "authoring_intent_required": False,
                        "explicit_workflow_context_required": True,
                        "prefer_existing_capability": False,
                    }
                },
            ),
            purpose=(
                "Execute the canonical arXiv ingestion workflow against one live "
                "arXiv paper, verify represented scholarly metadata and provenance, "
                "and clean up transient artefacts afterwards."
            ),
            source="test",
        )
    )

    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        final_state="complete",
        data={"response_text": "Executed via arXiv representation workflow."},
    )

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2411.04983",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 1.0,
                        "reasoning": (
                            "The request is a bare arXiv URL, so the canonical "
                            "execution workflow for arXiv representation is the "
                            "best executable route."
                        ),
                    }
                )
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 0.84,
                    "relevance_score": 0.77,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 0.84,
                    "relevance_score": 0.77,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                },
            ],
            "match_count": 2,
        },
        conversation_session_id="session-bare-arxiv-routing-regression",
        turn_id="turn-bare-arxiv-routing-regression",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Executed via arXiv representation workflow."

    selector_prompt_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector_prompt"
    )
    candidate_list_text = str(
        selector_prompt_entry.get("candidate_list", {}).get("text") or ""
    )
    assert selected_workflow_id in candidate_list_text
    assert testing_workflow_id not in candidate_list_text

    excluded_candidates = selector_prompt_entry.get("discovery_excluded_candidates")
    assert isinstance(excluded_candidates, list)
    excluded_testing = next(
        item
        for item in excluded_candidates
        if isinstance(item, dict) and item.get("concept_id") == testing_workflow_id
    )
    assert excluded_testing.get("routing_profile_role") == "maintenance"
    assert (
        excluded_testing.get("routing_exclusion_reason")
        == "explicit_workflow_context_required_by_workflow_profile"
    )
    assert (
        excluded_testing.get("routing_policy_flags", {}).get(
            "explicit_workflow_context_required"
        )
        is True
    )


def test_bare_arxiv_url_selector_default_recovers_to_single_discovered_execution_workflow(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    testing_workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"

    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Canonical arXiv wrapper workflow that normalises an arXiv source, "
            "fetches authoritative metadata, and delegates to scholarly-paper "
            "representation."
        ),
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=testing_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=testing_workflow_id,
                initial_state="prepare_fixture",
                states={
                    "prepare_fixture": WorkflowStateSpec(
                        state_id="prepare_fixture",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="testing.prepare_arxiv_paper_ingestion_fixture"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "testing",
                        "authoring_intent_required": False,
                        "explicit_workflow_context_required": True,
                        "prefer_existing_capability": False,
                    }
                },
            ),
            purpose=(
                "Execute the canonical arXiv ingestion workflow against one live "
                "arXiv paper, verify represented scholarly metadata and provenance, "
                "and clean up transient artefacts afterwards."
            ),
            source="test",
        )
    )

    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        final_state="complete",
        data={"response_text": "Executed via recovered arXiv representation workflow."},
    )

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2501.00663",
        context=[],
        llm_client=_CapturingLLM(
            [
                (
                    "I'm not sure which workflow you'd like me to select from "
                    "the candidates provided. If you want me to download or "
                    "represent the paper, please say so explicitly."
                )
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "routing_profile": {"role": "execution"},
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 0.84,
                    "relevance_score": 0.77,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "routing_profile": {"role": "execution"},
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 0.84,
                    "relevance_score": 0.77,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                },
            ],
            "match_count": 2,
        },
        conversation_session_id="session-bare-arxiv-selector-default-recovery",
        turn_id="turn-bare-arxiv-selector-default-recovery",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Executed via recovered arXiv representation workflow."
    assert (
        "only eligible specialised candidate already present"
        in (result.workflow_routing.reasoning or "").lower()
    )

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == selected_workflow_id
    assert selector_entry["selection_metadata"]["selection_resolution"] == (
        "single_specialised_candidate_recovery_from_selector_fallback"
    )
    assert selector_entry["selection_metadata"]["selection_resolution_prior"] == (
        "default_workflow_fallback"
    )
    candidate_entries = selector_entry.get("candidate_entries") or []
    candidate_order = [
        str(item.get("concept_id"))
        for item in candidate_entries
        if isinstance(item, dict) and isinstance(item.get("concept_id"), str)
    ]
    assert candidate_order.index(selected_workflow_id) < candidate_order.index(
        CHAT_ASSISTANT_WORKFLOW_ID
    )
    assert selector_entry["selection_metadata"]["recovered_candidate_workflow_id"] == (
        selected_workflow_id
    )
    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector_override" not in aux_types


def test_single_specialised_retrieval_candidate_recovers_inside_selector_boundary(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#concept_search_instance_retrieval_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Retrieve the represented concept facts for the authenticated current "
            "user and answer from those facts."
        ),
    )
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        data={"response_text": "Retrieved current-user concept details."},
        passthrough_unmatched=True,
    )

    result = orchestrator.run(
        prompt="Tell me about myself.",
        context=[],
        llm_client=_CapturingLLM(
            [
                (
                    "I'm not sure which workflow you would like me to select.\n\n"
                    "Are you looking to search for information or manage tasks?"
                ),
                "Retrieved current-user concept details.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": (
                        "Retrieve represented facts about a specific concept or "
                        "instance from the Vontology."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "routing_profile": {"role": "retrieval"},
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": (
                        "Retrieve represented facts about a specific concept or "
                        "instance from the Vontology."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "routing_profile": {"role": "retrieval"},
                }
            ],
            "match_count": 1,
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Retrieved current-user concept details."
    assert (
        "only eligible specialised candidate already present"
        in (result.workflow_routing.reasoning or "").lower()
    )

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == selected_workflow_id
    assert selector_entry["selection_metadata"]["selection_resolution"] == (
        "single_specialised_candidate_recovery_from_selector_fallback"
    )
    assert selector_entry["selection_metadata"]["selection_resolution_prior"] == (
        "default_workflow_fallback"
    )
    assert selector_entry["selection_metadata"]["recovered_candidate_workflow_id"] == (
        selected_workflow_id
    )
    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector_override" not in aux_types


def test_generic_tool_fallback_records_disqualifying_reason_for_specialised_candidate(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"

    result = orchestrator.run(
        prompt="Download and represent 2603.19312v1 arxiv",
        context=[],
        llm_client=_CapturingLLM(
            [TOOL_CALLING_WORKFLOW_ID, "Fallback via generic tool pipeline."]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Canonical arXiv representation workflow.",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": False,
                    "executability_reason": "launch_input_contract_unsatisfied",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Canonical arXiv representation workflow.",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": False,
                    "executability_reason": "launch_input_contract_unsatisfied",
                }
            ],
            "match_count": 1,
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    reasoning = str(selector_entry.get("reasoning") or "")
    assert "#V#arxiv_paper_representation_workflow" in reasoning
    assert "launch input contract unsatisfied" in reasoning


def test_contradictory_structured_selector_reasoning_overrides_selected_workflow_id(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Canonical arXiv wrapper workflow that normalises an arXiv source, "
            "fetches authoritative metadata, and delegates to scholarly-paper "
            "representation."
        ),
    )
    captured_workflow_ids: list[str] = []

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        captured_workflow_ids.append(workflow_id)
        if workflow_id == selected_workflow_id:
            return SimpleNamespace(
                data={"response_text": "Executed via arXiv representation workflow."},
                final_state="complete",
                completed=True,
            )
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            return SimpleNamespace(
                data={
                    "final_response": "Fallback via generic tool pipeline.",
                    "tool_messages": [],
                    "invocations": [],
                    "iteration_count": 0,
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt=(
            "For each of those students, if they are not already represented in "
            "the Vontology, please represent them."
        ),
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 0.97,
                        "reasoning": (
                            "The arXiv-specific workflow is irrelevant here, while "
                            "the best fit is the generic tool-calling workflow for "
                            "KB writes and representation operations."
                        ),
                    }
                ),
                "Fallback via generic tool pipeline.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Canonical arXiv representation workflow.",
                    "candidate_source": "workflow_discovery",
                    "match_source": "capability_index",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Canonical arXiv representation workflow.",
                    "candidate_source": "workflow_discovery",
                    "match_source": "capability_index",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "match_count": 1,
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Fallback via generic tool pipeline."
    assert captured_workflow_ids == [TOOL_CALLING_WORKFLOW_ID]

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert selector_entry["selection_metadata"]["selection_resolution"] == (
        "reasoning_candidate_override"
    )
    assert (
        selector_entry["selection_metadata"]["reasoning_override_from_workflow_id"]
        == selected_workflow_id
    )
    assert (
        selector_entry["selection_metadata"]["reasoning_override_workflow_id"]
        == TOOL_CALLING_WORKFLOW_ID
    )


def test_same_session_follow_up_does_not_rehydrate_python_write_intent_memory(
    monkeypatch,
):
    """Same-session follow-up turns should remain selector-owned."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
    )

    first = orchestrator.run(
        prompt="Create a concept link in Vontology.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-same",
    )
    assert first.workflow_routing is not None
    assert first.workflow_routing.verdict == "rag_selected"
    assert first.workflow_routing.source == "selector"

    second = orchestrator.run(
        prompt="Yes, do it.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-same",
    )
    assert second.workflow_routing is not None
    assert second.workflow_routing.verdict == "rag_selected"
    assert second.workflow_routing.source == "selector"
    gate_entry = next(
        (
            entry
            for entry in second.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "write_policy_gate"
            and entry.get("stage") == "routing"
        ),
        None,
    )
    assert gate_entry is not None
    assert gate_entry.get("gate_state") == "not_applied"
    assert gate_entry.get("continuation_context_reused") is False
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "write_intent_session_memory"
        for entry in second.aux_llm_calls
    )


def test_cross_session_follow_up_has_no_python_write_intent_reuse(monkeypatch):
    """Cross-session follow-up turns should also remain selector-owned."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
    )

    first = orchestrator.run(
        prompt="Create a concept link in Vontology.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-a",
    )
    assert first.workflow_routing is not None
    assert first.workflow_routing.verdict == "rag_selected"
    assert first.workflow_routing.source == "selector"

    second = orchestrator.run(
        prompt="Yes, do it.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-b",
    )
    assert second.workflow_routing is not None
    assert second.workflow_routing.verdict == "rag_selected"
    assert second.workflow_routing.source == "selector"

    aux_types = [
        entry.get("type") for entry in second.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector_override" not in aux_types
    gate_entry = next(
        (
            entry
            for entry in second.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "write_policy_gate"
            and entry.get("stage") == "routing"
        ),
        None,
    )
    assert gate_entry is not None
    assert gate_entry.get("gate_state") == "not_applied"
    assert gate_entry.get("reason") == "workflow_llm_owns_mutation_routing"
    assert gate_entry.get("continuation_context_reused") is False
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "write_intent_session_memory"
        for entry in second.aux_llm_calls
    )


def test_confirm_structure_follow_up_does_not_rehydrate_python_write_intent_memory(
    monkeypatch,
):
    """Short confirmation prompts should not trigger Python write-intent reuse."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
    )

    first = orchestrator.run(
        prompt="Create a concept link in Vontology.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-structure",
    )
    assert first.workflow_routing is not None
    assert first.workflow_routing.verdict == "rag_selected"
    assert first.workflow_routing.source == "selector"

    second = orchestrator.run(
        prompt="Confirm structure.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-structure",
    )
    assert second.workflow_routing is not None
    assert second.workflow_routing.verdict == "rag_selected"
    assert second.workflow_routing.source == "selector"
    gate_entry = next(
        (
            entry
            for entry in second.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "write_policy_gate"
            and entry.get("stage") == "routing"
        ),
        None,
    )
    assert gate_entry is not None
    assert gate_entry.get("gate_state") == "not_applied"
    assert gate_entry.get("continuation_context_reused") is False
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "write_intent_session_memory"
        for entry in second.aux_llm_calls
    )


def test_tool_planner_receives_authoritative_workflow_continuation_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1380",
            "selected_workflow_id": "#V#scholarly_paper_representation_workflow",
            "completion_gate_decision": "escalation_required",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {
                    "effect_id": "effect_paper_representation_1",
                    "effect_type": "scholarly_representation",
                    "description": "Represent the corresponding scholarly paper.",
                    "required_tools": ["interpret_file_copy"],
                    "targets": ["#V#uploaded_file_copy_abc123"],
                }
            ],
            "required_effects_contract": {
                "schema_version": "required_effects_contract.v1",
                "intent_class": "representation",
                "domain_profile_id": "paper",
                "artefact_context": {
                    "file_copy_ids": ["#V#uploaded_file_copy_abc123"],
                    "urls": [],
                },
            },
        },
    )

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,
            "I'll continue the representation work.",
            "Follow-through response.",
            "Final response after tool workflow.",
        ]
    )

    result = orchestrator.run(
        prompt="Please proceed.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1380",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"

    selector_prompt_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_prompt"
        ),
        None,
    )
    assert selector_prompt_entry is not None
    continuation_context = (
        selector_prompt_entry.get("continuation_context", {}).get("text") or ""
    )
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" in continuation_context
    assert "#V#scholarly_paper_representation_workflow" in continuation_context

    planner_context = llm.calls[1]["context"] or []
    planner_prompt_context = "\n".join(
        str(message.get("content") or "")
        for message in planner_context
        if isinstance(message, dict)
    )
    assert llm.calls[1]["prompt"] == "Please proceed."
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" in planner_prompt_context
    assert "#V#scholarly_paper_representation_workflow" in planner_prompt_context
    assert "#V#uploaded_file_copy_abc123" in planner_prompt_context

    continuation_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_continuation_context"
        ),
        None,
    )
    assert continuation_entry is not None
    assert continuation_entry.get("applied") is True
    assert continuation_entry.get("reason") == "workflow_state_authoritative"


def test_custom_workflow_dispatch_projects_launch_inputs_from_applied_continuation_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="normalise_arxiv_source",
                states={
                    "normalise_arxiv_source": WorkflowStateSpec(
                        state_id="normalise_arxiv_source",
                        actions=(
                            WorkflowActionInvocation(action_id="arxiv.normalise_source"),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["prompt"],
                        "input_mappings": [
                            {
                                "target_context_key": "prompt",
                                "source_expression": "inputs.prompt",
                                "required": True,
                            },
                            {
                                "target_context_key": "source_uri",
                                "source_expression": "inputs.source_uri",
                            },
                            {
                                "target_context_key": "arxiv_id",
                                "source_expression": "inputs.arxiv_id",
                            },
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose="Continuation-aware arXiv workflow dispatch test.",
            source="test",
        )
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1858",
            "selected_workflow_id": selected_workflow_id,
            "completion_gate_decision": "follow_up_required",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {
                    "effect_id": "effect_workflow_execution_1",
                    "effect_type": "workflow_execution",
                    "status": "not_executed",
                    "description": (
                        "Obtain the selected workflow result needed for the "
                        "user-facing answer."
                    ),
                }
            ],
            "required_effects_contract": {
                "schema_version": "required_effects_contract.v1",
                "intent_class": "representation",
                "artefact_context": {
                    "urls": ["https://arxiv.org/abs/2501.00663"],
                },
            },
        },
    )

    captured_data: dict[str, Any] = {}

    def _run_workflow(_workflow_def: Any, *, data: Mapping[str, Any], **_kwargs: Any):
        captured_data.update(dict(data))
        return SimpleNamespace(
            completed=True,
            final_state="normalise_arxiv_source",
            error=None,
            data={"response_text": "Prepared from continuation context."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt="Download and represent the paper",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1858",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Represent an arXiv paper from a prior session artefact.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Represent an arXiv paper from a prior session artefact.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "match_count": 1,
        },
    )

    assert result.response_text == "Prepared from continuation context."
    assert captured_data["selected_workflow_id"] == selected_workflow_id
    assert captured_data["source_uri"] == "https://arxiv.org/abs/2501.00663"
    assert captured_data["source_uris"] == ["https://arxiv.org/abs/2501.00663"]
    assert captured_data["arxiv_id"] == "2501.00663"
    assert captured_data["arxiv_ids"] == ["2501.00663"]
    assert captured_data["workflow_continuation_launch_inputs"] == {
        "source_uris": ["https://arxiv.org/abs/2501.00663"],
        "source_uri": "https://arxiv.org/abs/2501.00663",
        "arxiv_ids": ["2501.00663"],
        "arxiv_id": "2501.00663",
    }

    launch_resolution = captured_data.get("workflow_launch_input_resolution")
    assert isinstance(launch_resolution, dict)
    assert launch_resolution.get("status") == "resolved"
    assert launch_resolution.get("resolved_inputs") == [
        "arxiv_id",
        "prompt",
        "source_uri",
    ]


def test_custom_workflow_dispatch_preserves_plural_launch_inputs_from_continuation_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="normalise_arxiv_source",
                states={
                    "normalise_arxiv_source": WorkflowStateSpec(
                        state_id="normalise_arxiv_source",
                        actions=(
                            WorkflowActionInvocation(action_id="arxiv.normalise_source"),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["prompt"],
                        "input_mappings": [
                            {
                                "target_context_key": "prompt",
                                "source_expression": "inputs.prompt",
                                "required": True,
                            }
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose="Continuation-aware plural arXiv workflow dispatch test.",
            source="test",
        )
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1874-multi",
            "selected_workflow_id": selected_workflow_id,
            "completion_gate_decision": "follow_up_required",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {
                    "effect_id": "effect_workflow_execution_1",
                    "effect_type": "workflow_execution",
                    "status": "not_executed",
                    "targets": [
                        "https://arxiv.org/abs/2501.00663",
                        "https://arxiv.org/abs/2501.00664",
                    ],
                }
            ],
            "required_effects_contract": {
                "schema_version": "required_effects_contract.v1",
                "intent_class": "representation",
                "artefact_context": {
                    "urls": [
                        "https://arxiv.org/abs/2501.00663",
                        "https://arxiv.org/abs/2501.00664",
                    ],
                },
            },
        },
    )

    captured_data: dict[str, Any] = {}

    def _run_workflow(_workflow_def: Any, *, data: Mapping[str, Any], **_kwargs: Any):
        captured_data.update(dict(data))
        return SimpleNamespace(
            completed=True,
            final_state="normalise_arxiv_source",
            error=None,
            data={"response_text": "Prepared from plural continuation context."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt="Download and represent the papers",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1874-multi",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Represent arXiv papers from prior session artefacts.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Represent arXiv papers from prior session artefacts.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "match_count": 1,
        },
    )

    assert result.response_text == "Prepared from plural continuation context."
    assert "source_uri" not in captured_data
    assert "arxiv_id" not in captured_data
    assert captured_data["source_uris"] == [
        "https://arxiv.org/abs/2501.00663",
        "https://arxiv.org/abs/2501.00664",
    ]
    assert captured_data["arxiv_ids"] == ["2501.00663", "2501.00664"]
    assert captured_data["workflow_continuation_launch_inputs"] == {
        "source_uris": [
            "https://arxiv.org/abs/2501.00663",
            "https://arxiv.org/abs/2501.00664",
        ],
        "arxiv_ids": ["2501.00663", "2501.00664"],
    }


def test_selector_routes_failure_follow_up_with_episode_aware_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    diagnostic_workflow_id = "#V#missing_tool_call_workflow"

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1793",
            "active_workflow_episode_id": "wfep_1793",
            "active_workflow_source": "conversation_turn",
            "selected_workflow_id": diagnostic_workflow_id,
            "completion_gate_decision": "follow_up_required",
            "completion_gate_decision_reason": "Diagnostic evidence retrieval failed.",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "workflow_required_effects_contract": {
                "contract_id": "conversation_diagnostics_required_evidence",
            },
            "resolved_contract_identifiers": {
                "workflow_required_effects_contract_id": (
                    "conversation_diagnostics_required_evidence"
                ),
                "selected_execution_mode": "tool_pipeline",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
            },
            "unresolved_required_effects": [
                {
                    "effect_id": "conversation_history",
                    "effect_type": "diagnostic_evidence",
                    "status": "not_satisfied",
                    "status_reason": "Conversation evidence retrieval failed.",
                    "required_tools": ["chat_history_get_debug_entry"],
                }
            ],
        },
    )
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=diagnostic_workflow_id,
        purpose="Diagnostic follow-up workflow.",
    )

    class _SelectorContextSensitiveLLM:
        def __init__(self):
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
            if prompt == "Select workflow":
                selector_prompt_text = "\n".join(
                    str(item.get("content") or "")
                    for item in (context or [])
                    if isinstance(item, Mapping)
                )
                if (
                    "ACTIVE WORKFLOW CONTINUATION CONTEXT" in selector_prompt_text
                    and "conversation_diagnostics_required_evidence"
                    in selector_prompt_text
                    and "Active workflow source: conversation_turn"
                    in selector_prompt_text
                ):
                    return diagnostic_workflow_id
                return CHAT_ASSISTANT_WORKFLOW_ID
            return "Diagnostic follow-up response."

    llm = _SelectorContextSensitiveLLM()

    result = orchestrator.run(
        prompt="Explain the failure from the telemetry",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1793",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": diagnostic_workflow_id,
                    "name": "Missing Tool Call Workflow",
                    "description": "Recover when tool emission failed.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "is_policy_safe": True,
                    "relevance_score": 0.95,
                    "confidence_score": 0.95,
                    "candidate_source": "workflow_discovery",
                    "candidate_reason": "discovered_workflow_candidate",
                }
            ]
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == diagnostic_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    selector_prompt_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_prompt"
        ),
        None,
    )
    assert selector_prompt_entry is not None
    continuation_context = (
        selector_prompt_entry.get("continuation_context", {}).get("text") or ""
    )
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" in continuation_context
    assert "conversation_diagnostics_required_evidence" in continuation_context
    assert "Active workflow source: conversation_turn" in continuation_context


def test_tool_planner_skips_continuation_after_explicit_workflow_divergence(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1687",
            "selected_workflow_id": "#V#enrichment_workflow",
            "completion_gate_decision": "escalation_required",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {
                    "effect_id": "effect_concept_verification_1",
                    "effect_type": "concept_verification",
                    "description": "Verify the concept relations.",
                    "required_tools": ["fetch_concept"],
                    "targets": ["#V#timothy_pistotti"],
                }
            ],
        },
    )

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,
            "I will inspect the concept directly instead.",
            "Follow-through response.",
            "Final response after tool workflow.",
        ]
    )

    result = orchestrator.run(
        prompt=(
            "The enrichment workflow isn't the right one. Manually retrieve "
            "#V#timothy_pistotti and inspect the concept. Do not run an "
            "existing workflow."
        ),
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1687",
    )

    assert result.workflow_routing is not None
    planner_context = llm.calls[1]["context"] or []
    planner_prompt_context = "\n".join(
        str(message.get("content") or "")
        for message in planner_context
        if isinstance(message, dict)
    )
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" not in planner_prompt_context
    selector_prompt_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_prompt"
        ),
        None,
    )
    assert selector_prompt_entry is not None
    selector_continuation_context = (
        selector_prompt_entry.get("continuation_context", {}).get("text") or ""
    )
    assert "No active workflow continuation context." in selector_continuation_context

    continuation_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_continuation_context"
        ),
        None,
    )
    assert continuation_entry is not None
    assert continuation_entry.get("applied") is False
    assert (
        continuation_entry.get("reason")
        == "prompt_explicitly_diverges_from_selected_workflow"
    )
    assert "prompt_forbids_workflow_execution" in (
        continuation_entry.get("context", {}).get("apply_signals") or []
    )


def test_tool_planner_skips_continuation_when_selected_workflow_is_not_executable(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1718",
            "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
            "selected_workflow_is_executable": False,
            "selected_workflow_executability_reason": "draft_not_published",
            "selected_workflow_executability_detail": (
                "workflow_not_published:phase=draft"
            ),
            "completion_gate_decision": "follow_up_required",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {
                    "effect_id": "effect_workflow_execution_1",
                    "effect_type": "workflow_execution",
                    "description": (
                        "Obtain the selected workflow result needed for the user-facing answer."
                    ),
                }
            ],
        },
    )

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,
            "I will continue by inspecting the available workflow candidates.",
            "Follow-through response.",
            "Final response after tool workflow.",
        ]
    )

    result = orchestrator.run(
        prompt="Represent this paper https://arxiv.org/abs/2603.01896",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1718",
    )

    assert result.workflow_routing is not None
    planner_context = llm.calls[1]["context"] or []
    planner_prompt_context = "\n".join(
        str(message.get("content") or "")
        for message in planner_context
        if isinstance(message, dict)
    )
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" not in planner_prompt_context
    selector_prompt_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_prompt"
        ),
        None,
    )
    assert selector_prompt_entry is not None
    selector_continuation_context = (
        selector_prompt_entry.get("continuation_context", {}).get("text") or ""
    )
    assert "No active workflow continuation context." in selector_continuation_context

    continuation_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_continuation_context"
        ),
        None,
    )
    assert continuation_entry is not None
    assert continuation_entry.get("applied") is False
    assert continuation_entry.get("reason") == "selected_workflow_not_executable"
    assert continuation_entry.get("context", {}).get(
        "selected_workflow_executability_reason"
    ) == "draft_not_published"


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Routing info on tool-calling path.
# ---------------------------------------------------------------------------


def test_tool_seeking_has_routing_info(monkeypatch):
    """Tool-calling path should also include WorkflowRoutingInfo."""
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,  # selector verdict
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
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"
    assert isinstance(result.workflow_routing.selection_rationale, str)

    selector_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
        ),
        None,
    )
    assert selector_entry is not None
    assert selector_entry.get("prompt", {}).get("text")
    assert TOOL_CALLING_WORKFLOW_ID in str(
        selector_entry.get("response", {}).get("text") or ""
    )
    assert isinstance(selector_entry.get("candidate_entries"), list)
    assert any(
        isinstance(entry, dict) and entry.get("concept_id") == TOOL_CALLING_WORKFLOW_ID
        for entry in selector_entry.get("candidate_entries", [])
    )

    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    assert [entry.get("boundary") for entry in dispatch_boundaries[:3]] == [
        "execution_mode_selected",
        "contract_resolution",
        "workflow_handoff",
    ]
    assert dispatch_boundaries[-1].get("boundary") == "workflow_terminal"
    assert dispatch_boundaries[-1].get("selected_execution_mode") == "tool_pipeline"
    assert dispatch_boundaries[-1].get("dispatch_workflow_id")


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Routing timing telemetry.
# ---------------------------------------------------------------------------


def test_routing_duration_ms_in_aux_llm_calls(monkeypatch):
    """Routing telemetry should include timing in aux_llm_calls."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,
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
    prepare_steps = [
        e
        for e in result.aux_llm_calls
        if isinstance(e, dict) and e.get("type") == "workflow_dispatch_prepare_step"
    ]
    assert prepare_steps
    assert any(
        step.get("step_id") == "selector_candidate_preparation"
        for step in prepare_steps
    )
    assert all(step.get("stage") == "workflow_dispatch_prepare" for step in prepare_steps)
    assert all(isinstance(step.get("duration_ms"), int) for step in prepare_steps)


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Selector default-on behaviour.
# ---------------------------------------------------------------------------


def test_selector_enabled_by_default(monkeypatch):
    """The selector remains enabled without any compatibility toggle."""
    monkeypatch.delenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", raising=False)

    selector = WorkflowSelector(
        registry=MagicMock(),
        prompt_service=MagicMock(),
    )
    assert selector.enabled()

def test_selector_ignores_legacy_disable_env(monkeypatch):
    """Legacy selector env toggles no longer affect runtime routing."""
    monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "0")

    selector = WorkflowSelector(
        registry=MagicMock(),
        prompt_service=MagicMock(),
    )
    assert selector.enabled()


def test_selector_resolve_selection_extracts_workflow_id_from_json_output():
    registry = MagicMock()
    registry.all_workflow_ids.return_value = {
        CHAT_ASSISTANT_WORKFLOW_ID,
        TOOL_CALLING_WORKFLOW_ID,
    }
    selector = WorkflowSelector(
        registry=registry,
        prompt_service=MagicMock(),
    )

    selection = selector.resolve_selection(
        raw_response=f'{{"workflow_id":"{TOOL_CALLING_WORKFLOW_ID}"}}',
        prompt_id=None,
        prompt_used=None,
        discovered_workflow_ids=(TOOL_CALLING_WORKFLOW_ID,),
    )

    assert selection.verdict == "rag_selected"
    assert selection.workflow_id == TOOL_CALLING_WORKFLOW_ID


def test_selector_resolve_selection_extracts_discovered_workflow_from_free_form_output():
    registry = MagicMock()
    registry.all_workflow_ids.return_value = {
        CHAT_ASSISTANT_WORKFLOW_ID,
        TOOL_CALLING_WORKFLOW_ID,
    }
    selector = WorkflowSelector(
        registry=registry,
        prompt_service=MagicMock(),
    )

    selection = selector.resolve_selection(
        raw_response=f"Use {TODO_REFRESH_WORKFLOW_ID} for this request.",
        prompt_id=None,
        prompt_used=None,
        discovered_workflow_ids=(TODO_REFRESH_WORKFLOW_ID,),
    )

    assert selection.verdict == "rag_selected"
    assert selection.workflow_id == TODO_REFRESH_WORKFLOW_ID


def test_selector_resolve_selection_invalid_output_falls_back_to_default_workflow():
    registry = MagicMock()
    registry.all_workflow_ids.return_value = {
        CHAT_ASSISTANT_WORKFLOW_ID,
        TOOL_CALLING_WORKFLOW_ID,
    }
    selector = WorkflowSelector(
        registry=registry,
        prompt_service=MagicMock(),
    )

    selection = selector.resolve_selection(
        raw_response="I am not sure.",
        prompt_id=None,
        prompt_used=None,
        discovered_workflow_ids=(),
    )

    assert selection.verdict == "rag_default"
    assert selection.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Routing info absent when the selector is suppressed in tests.
# ---------------------------------------------------------------------------


def test_no_routing_info_when_selector_suppressed_in_harness(monkeypatch):
    """Harness-level selector suppression should omit workflow_routing data."""
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


def test_discovery_miss_invokes_gap_recovery_after_plain_fallback(monkeypatch):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    recovery_calls: list[tuple[str, Mapping[str, Any]]] = []

    def _fake_execute_workflow(workflow_id: str, **kwargs):
        recovery_calls.append((workflow_id, dict(kwargs)))
        return SimpleNamespace(
            data={
                "workflow_gap_final_response_text": "Recovered through workflow-gap analysis.",
                "workflow_gap_final_extra_messages": [
                    {"role": "tool", "content": "gap recovery tool output"}
                ],
                "workflow_gap_final_tool_invocations": [
                    {"tool": "workflow_gap.execute_candidate"}
                ],
                "workflow_gap_recovery_outcome": "candidate_retried_successfully",
                "workflow_gap_candidate_workflow_id": "#V#candidate_recovery_workflow",
            },
            final_state="complete",
            completed=True,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _fake_execute_workflow)

    llm = _CapturingLLM([CHAT_ASSISTANT_WORKFLOW_ID, "Fallback response."])
    result = orchestrator.run(
        prompt="Handle this missing workflow.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={"matches": [], "candidates": []},
    )

    assert recovery_calls
    workflow_id, payload = recovery_calls[0]
    assert workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
    assert payload["data"]["workflow_gap_base_response_text"] == "Fallback response."
    assert result.response_text == "Recovered through workflow-gap analysis."
    assert result.extra_messages == (
        {"role": "tool", "content": "gap recovery tool output"},
    )
    assert result.tool_invocations == (
        {"tool": "workflow_gap.execute_candidate"},
    )
    recovery_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_gap_recovery"
        ),
        None,
    )
    assert recovery_entry is not None
    assert recovery_entry.get("status") == "applied"
    assert recovery_entry.get("candidate_workflow_id") == "#V#candidate_recovery_workflow"


def test_discovered_custom_workflow_failure_falls_through_to_tool_pipeline_before_gap_recovery(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#misaligned_specialised_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Misaligned specialised workflow for gap-recovery routing tests.",
    )

    executed_workflow_ids: list[str] = []
    tool_pipeline_payload: dict[str, Any] = {}

    def _fake_execute_workflow(workflow_id: str, **kwargs: Any):
        executed_workflow_ids.append(workflow_id)
        if workflow_id == selected_workflow_id:
            return SimpleNamespace(
                data={"response_text": "Selected specialised workflow failed."},
                final_state="#V#workflow_step_misaligned_specialised_workflow_failed",
                completed=True,
                error=None,
            )
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            tool_pipeline_payload.update(dict(kwargs))
            return SimpleNamespace(
                data={
                    "final_response": "Recovered through the general tool workflow.",
                    "tool_messages": [
                        {
                            "role": "tool",
                            "content": "general tool workflow inspected existing context",
                        }
                    ],
                    "invocations": [{"tool": "find_relations_with_argument"}],
                    "iteration_count": 1,
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _fake_execute_workflow)

    result = orchestrator.run(
        prompt="Represent these people in the Vontology if they are not already represented.",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Misaligned specialised workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Misaligned specialised workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
        },
    )

    assert executed_workflow_ids == [
        selected_workflow_id,
        TOOL_CALLING_WORKFLOW_ID,
    ]
    assert tool_pipeline_payload["data"]["prior_failed_selected_workflow"] == {
        "workflow_id": selected_workflow_id,
        "completed": False,
        "tool_progress_detected": False,
        "final_state": "#V#workflow_step_misaligned_specialised_workflow_failed",
        "user_visible_failure_text": "Selected specialised workflow failed.",
    }
    assert result.response_text == "Recovered through the general tool workflow."
    assert result.extra_messages == (
        {
            "role": "tool",
            "content": "general tool workflow inspected existing context",
        },
    )
    assert result.tool_invocations == (
        {"tool": "find_relations_with_argument"},
    )
    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    terminal_boundary = next(
        (
            entry
            for entry in dispatch_boundaries
            if entry.get("boundary") == "workflow_terminal"
            and entry.get("selected_execution_mode") == "custom_workflow"
        ),
        None,
    )
    assert terminal_boundary is not None
    assert terminal_boundary.get("status") == "failed"
    assert terminal_boundary.get("completed") is False
    assert terminal_boundary.get("final_state") == (
        "#V#workflow_step_misaligned_specialised_workflow_failed"
    )
    assert terminal_boundary.get("reason") == "failed_terminal_state"
    assert terminal_boundary.get("detail") == (
        "Selected specialised workflow failed."
    )
    assert terminal_boundary.get("continued_to_tool_pipeline") is True
    assert terminal_boundary.get("fallback_tool_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    handoff_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_recovery_handoff"
        ),
        None,
    )
    assert handoff_entry is not None
    assert handoff_entry.get("to_execution_mode") == "tool_pipeline"
    assert handoff_entry.get("reason") == "failed_custom_workflow_before_tool_progress"
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "workflow_gap_recovery"
        for entry in result.aux_llm_calls
    )


def test_custom_workflow_fallback_handoff_preserves_turn_expected_outcome_contract(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#predicate_schema_wrapper_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Predicate/schema wrapper workflow for tool handoff contract tests.",
    )

    expected_contract = {
        "summary": "Identify predicates salient to SAIL students.",
        "grounding_requirement": (
            "Predicates must be substantiated by represented relationships or text relations."
        ),
        "precision_policy": "Prefer omission over unsupported predicate claims.",
        "selector_guidance": (
            "Use search_concepts and get_text_relations_summary before answering."
        ),
        "answering_guidance": "List only grounded predicates and say when evidence is missing.",
        "reasoning": "Predicate/schema turns need grounded ontology retrieval rather than generic chat.",
    }
    expected_discovery_contract = {
        "summary": expected_contract["summary"],
        "grounding_requirement": expected_contract["grounding_requirement"],
        "selector_guidance": expected_contract["selector_guidance"],
    }
    class _ContractAwareSelectorLLM:
        def __init__(self) -> None:
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
            if "expected-success inference policy" in prompt:
                return "{}"
            if prompt == "Select workflow":
                return selected_workflow_id
            return "Recovered through the general tool workflow."

    llm = _ContractAwareSelectorLLM()
    tool_pipeline_payload: dict[str, Any] = {}

    def _fake_execute_workflow(workflow_id: str, **kwargs: Any):
        if workflow_id == selected_workflow_id:
            return SimpleNamespace(
                data={"response_text": "Selected specialised workflow failed."},
                final_state="#V#workflow_step_predicate_schema_wrapper_failed",
                completed=True,
                error=None,
            )
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            tool_pipeline_payload.update(dict(kwargs))
            return SimpleNamespace(
                data={
                    "final_response": "Recovered through the general tool workflow.",
                    "tool_messages": [],
                    "invocations": [{"tool": "get_text_relations_summary"}],
                    "iteration_count": 1,
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _fake_execute_workflow)

    result = orchestrator.run(
        prompt="What predicates are salient to SAIL students?",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Predicate Schema Wrapper Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Predicate Schema Wrapper Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "query": (
                "What predicates are salient to SAIL students?\n\n"
                "Turn-intent routing guidance:\n"
                f"- Routing guidance: {expected_contract['selector_guidance']}\n"
                f"- Grounding requirement: {expected_contract['grounding_requirement']}\n"
                f"- Success target: {expected_contract['summary']}"
            ),
        },
    )

    assert result.response_text == "Recovered through the general tool workflow."
    handoff_data = tool_pipeline_payload["data"]
    assert handoff_data["turn_expected_outcome_profile"] == expected_discovery_contract
    assert handoff_data["turn_expected_outcome_contract"] == expected_discovery_contract
    assert handoff_data["turn_expected_outcome_summary"] == expected_discovery_contract[
        "summary"
    ]
    assert handoff_data["turn_expected_grounding_requirement"] == (
        expected_discovery_contract["grounding_requirement"]
    )
    assert handoff_data["turn_selector_guidance"] == expected_discovery_contract[
        "selector_guidance"
    ]
    assert "turn_expected_precision_policy" not in handoff_data
    assert "turn_answering_guidance" not in handoff_data
    assert "turn_expected_outcome_reasoning" not in handoff_data


def test_entity_representation_failure_family_replays_with_truthful_gap_recovery_after_tool_pipeline_attempt(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Canonical arXiv wrapper workflow that represents scholarly papers, "
            "not conversational person lists."
        ),
    )

    executed_workflow_ids: list[str] = []
    recovery_calls: list[tuple[str, Mapping[str, Any]]] = []
    tool_pipeline_payload: dict[str, Any] = {}

    def _fake_execute_workflow(workflow_id: str, **kwargs: Any):
        executed_workflow_ids.append(workflow_id)
        if workflow_id == selected_workflow_id:
            return SimpleNamespace(
                data={
                    "response_text": (
                        "ArXiv paper representation could not represent those people."
                    ),
                    "error": "arxiv_identifier_missing",
                },
                final_state="#V#workflow_step_arxiv_paper_representation_workflow_failed",
                completed=True,
                error=None,
            )
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            tool_pipeline_payload.update(dict(kwargs))
            return SimpleNamespace(
                data={
                    "final_response": (
                        "The general tool workflow still could not complete the turn."
                    ),
                    "tool_messages": [],
                    "invocations": [],
                    "iteration_count": 1,
                },
                final_state="#V#tool_calling_workflow_failed",
                completed=True,
                error=None,
            )
        if workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID:
            recovery_calls.append((workflow_id, dict(kwargs)))
            return SimpleNamespace(
                data={
                    "workflow_gap_final_response_text": (
                        "Recovered by escalating into entity-representation workflow authoring."
                    ),
                    "workflow_gap_final_extra_messages": [
                        {
                            "role": "tool",
                            "content": "workflow-gap recovery prepared entity workflow follow-up",
                        }
                    ],
                    "workflow_gap_final_tool_invocations": [
                        {"tool": "workflow_gap.execute_candidate"}
                    ],
                    "workflow_gap_recovery_outcome": "candidate_retried_successfully",
                    "workflow_gap_candidate_workflow_id": (
                        "#V#entity_representation_workflow"
                    ),
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _fake_execute_workflow)

    result = orchestrator.run(
        prompt=(
            "OK, now for each of those students, if they aren't already represented "
            "in the Vontology, please represent them."
        ),
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
        },
    )

    assert executed_workflow_ids == [
        selected_workflow_id,
        TOOL_CALLING_WORKFLOW_ID,
        WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    ]
    assert tool_pipeline_payload["data"]["prior_failed_selected_workflow"] == {
        "workflow_id": selected_workflow_id,
        "completed": False,
        "tool_progress_detected": False,
        "final_state": "#V#workflow_step_arxiv_paper_representation_workflow_failed",
        "operational_error": "arxiv_identifier_missing",
        "user_visible_failure_text": (
            "ArXiv paper representation could not represent those people."
        ),
    }
    assert recovery_calls
    workflow_id, payload = recovery_calls[0]
    assert workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
    assert payload["data"]["workflow_gap_trigger_reason"] == (
        "tool_pipeline_failed_after_custom_workflow_failure"
    )
    assert payload["data"]["workflow_gap_selected_workflow_final_state"] == (
        "#V#tool_calling_workflow_failed"
    )
    assert payload["data"]["workflow_gap_selected_execution_mode"] == "tool_pipeline"
    assert payload["data"]["workflow_gap_prior_failed_selected_workflow"] == {
        "workflow_id": selected_workflow_id,
        "completed": False,
        "tool_progress_detected": False,
        "final_state": "#V#workflow_step_arxiv_paper_representation_workflow_failed",
        "operational_error": "arxiv_identifier_missing",
        "user_visible_failure_text": (
            "ArXiv paper representation could not represent those people."
        ),
    }
    assert result.response_text == (
        "Recovered by escalating into entity-representation workflow authoring."
    )
    assert result.extra_messages == (
        {
            "role": "tool",
            "content": "workflow-gap recovery prepared entity workflow follow-up",
        },
    )
    terminal_boundary = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_dispatch_boundary"
            and entry.get("boundary") == "workflow_terminal"
            and entry.get("selected_execution_mode") == "custom_workflow"
        ),
        None,
    )
    assert terminal_boundary is not None
    assert terminal_boundary.get("status") == "failed"
    assert terminal_boundary.get("completed") is False
    assert terminal_boundary.get("reason") == "failed_terminal_state"
    assert terminal_boundary.get("detail") == "arxiv_identifier_missing"
    assert terminal_boundary.get("continued_to_tool_pipeline") is True


def test_custom_workflow_failure_prefers_explicit_action_error_over_metadata_summary(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Canonical arXiv wrapper workflow for explicit failure-detail tests.",
    )

    actionable_error = (
        "Unexpected error: Failed to store PDF in blob store: Blob store "
        "initialisation failed: OpenStack Swift backend requires 'openstacksdk' "
        "in the active runtime environment."
    )
    masked_summary = (
        "The ability to predict future outcomes given control actions is "
        "fundamental for physical reasoning."
    )

    def _fake_execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != selected_workflow_id:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data={
                "summary": masked_summary,
                "last_action_error": actionable_error,
                "workflow_step_result_envelopes": [
                    {
                        "schema_version": "workflow_step_result_envelope.v1",
                        "workflow_id": selected_workflow_id,
                        "state_id": (
                            "#V#workflow_step_arxiv_paper_representation_workflow_"
                            "download_or_finalise"
                        ),
                        "action_id": "download_paper",
                        "action_status": "failed",
                        "action_outcome": "failure",
                        "diagnostics": {"error": actionable_error},
                        "output_payload": {},
                    }
                ],
            },
            final_state="#V#workflow_step_arxiv_paper_representation_workflow_failed",
            completed=True,
            error=None,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _fake_execute_workflow)

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2411.04983",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        workflow_gap_recovery_enabled=False,
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
        },
    )

    assert result.response_text == actionable_error
    terminal_boundary = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_dispatch_boundary"
            and entry.get("boundary") == "workflow_terminal"
            and entry.get("selected_execution_mode") == "custom_workflow"
        ),
        None,
    )
    assert terminal_boundary is not None
    assert terminal_boundary.get("status") == "failed"
    assert terminal_boundary.get("detail") == actionable_error
    assert terminal_boundary.get("detail") != masked_summary


def test_custom_workflow_result_preserves_messages_and_invocations(monkeypatch):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#custom_gap_analysis_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Custom gap-analysis workflow for selector dispatch tests.",
    )

    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        final_state="complete",
        data={
            "response_text": "Custom workflow response.",
            "extra_messages": [{"role": "tool", "content": "custom output"}],
            "tool_invocations": [{"tool": "search_concepts"}],
        },
    )

    result = orchestrator.run(
        prompt="Use the discovered workflow.",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Custom gap workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Custom gap workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
        },
    )

    assert result.response_text == "Custom workflow response."
    assert result.extra_messages == ({"role": "tool", "content": "custom output"},)
    assert result.tool_invocations == ({"tool": "search_concepts"},)

    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    assert dispatch_boundaries[-1].get("boundary") == "workflow_terminal"
    assert dispatch_boundaries[-1].get("selected_execution_mode") == "custom_workflow"
    assert dispatch_boundaries[-1].get("dispatch_workflow_id") == selected_workflow_id


def test_custom_workflow_structured_result_renders_verdict_evidence_and_promotion(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#custom_gap_analysis_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Structured-result workflow for selector dispatch tests.",
    )

    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        final_state="complete",
        data={
            "response_text": json.dumps(
                [
                    {
                        "label": "meeting_type_classification",
                        "verdict": "pass",
                        "expected_outcome": "project_meeting",
                        "observed_outcome": "project_meeting",
                    }
                ]
            ),
            "run_id": "#V#run_meeting_test",
            "verdict": "pass",
            "verdict_summary": {
                "reason": "all_recorded_observations_passed",
            },
            "promotion_recommendation": {
                "recommended": True,
                "requires_promotion_gate": True,
                "reason": "explicit_promotion_gate_required",
            },
            "candidate_meeting_type": "project_meeting",
            "candidate_safe_downstream_action": "draft_calendar_entry",
            "meeting_candidate_observations": [
                {
                    "label": "meeting_type_classification",
                    "verdict": "pass",
                    "expected_outcome": "project_meeting",
                    "observed_outcome": "project_meeting",
                },
                {
                    "label": "structured_meeting_fields",
                    "verdict": "pass",
                    "expected_outcome": "title,time,participants",
                    "observed_outcome": "all expected fields present",
                },
            ],
        },
    )

    result = orchestrator.run(
        prompt="Use the discovered workflow.",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Custom gap workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Custom gap workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "match_count": 1,
        },
    )

    assert "Workflow verdict: pass." in result.response_text
    assert "Experiment run: #V#run_meeting_test." in result.response_text
    assert (
        "Promotion recommendation: recommended; promotion gate required; "
        "explicit_promotion_gate_required."
    ) in result.response_text
    assert "Candidate meeting type: project_meeting." in result.response_text
    assert (
        "Candidate safe downstream action: draft_calendar_entry."
    ) in result.response_text
    assert "Evidence:" in result.response_text
    assert (
        "- meeting_type_classification: pass (expected: project_meeting; "
        "observed: project_meeting)"
    ) in result.response_text
    assert (
        "- structured_meeting_fields: pass (expected: title,time,participants; "
        "observed: all expected fields present)"
    ) in result.response_text

    execution_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict) and entry.get("type") == "workflow_execution"
        ),
        None,
    )
    assert execution_entry is not None
    result_snapshot = execution_entry.get("result_snapshot")
    assert isinstance(result_snapshot, dict)
    assert result_snapshot.get("run_id") == "#V#run_meeting_test"
    assert result_snapshot.get("verdict") == "pass"
    assert isinstance(result_snapshot.get("verdict_summary"), dict)
    assert isinstance(result_snapshot.get("promotion_recommendation"), dict)
    assert isinstance(result_snapshot.get("observations"), list)
