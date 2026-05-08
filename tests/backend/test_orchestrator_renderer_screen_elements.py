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
    assert (
        result.render_plan.get("screen_element_mapping_mode")
        == "selected_renderer_types"
    )
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
    assert "renderer_screen_elements:selected_renderer_types" in result.render_plan.get(
        "screen_element_reason_codes", []
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
                                        {
                                            "task_concept_id": "#V#task_alpha",
                                            "status": "todo",
                                        },
                                        {
                                            "task_concept_id": "#V#task_beta",
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
    assert (
        result.render_plan.get("screen_element_mapping_mode")
        == "selected_renderer_types"
    )
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
    assert (
        result.render_plan.get("screen_element_mapping_mode")
        == "selected_renderer_types"
    )
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
    assert (
        result.render_plan.get("screen_element_mapping_mode")
        == "selected_renderer_types"
    )
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



def test_renderer_selection_emits_relation_graph_elements_for_graph_renderer(
    monkeypatch,
):
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



