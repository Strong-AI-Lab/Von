"""Renderer routing test helpers imported from the workflow selector routing harness."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Mapping

from src.backend.workflows.definitions import (
    CHAT_NARRATION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)
from tests.backend.test_orchestrator_workflow_selector_routing import (
    _CapturingLLM,
    _InvokeResult,
    _build_orchestrator,
    _stub_execute_workflow_result,
)

# ---------------------------------------------------------------------------
# Renderer routing cases.
# ---------------------------------------------------------------------------

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
    assert (
        result.render_plan.get("screen_element_mapping_mode")
        == "selected_renderer_types"
    )
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
    assert (
        result.render_plan.get("screen_element_targets", {}).get("hierarchy_view")
        is True
    )
    hierarchy_elements = result.render_plan.get("screen_hierarchy_elements")
    assert isinstance(hierarchy_elements, list)
    assert len(hierarchy_elements) == 1
    payload = hierarchy_elements[0].get("payload", {})
    edges = payload.get("edges")
    assert isinstance(edges, list)
    assert len(edges) == 2



