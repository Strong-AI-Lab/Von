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

def test_renderer_render_plan_includes_table_record_sets_from_tool_messages(
    monkeypatch,
):
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
                        "content": '{"tool":"task_list","status":"ok","payload":{"tasks":[{"title":"A"}]',
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



