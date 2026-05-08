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
                        "selected_preview": [{"renderer_id": "#V#narration_renderer"}],
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
    assert renderer_invocations[0]["payload"]["renderer_definition_concept_ids"] == [
        "#V#narration_renderer"
    ]

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
        raise AssertionError(
            "Renderer applicability tool must not be invoked when disabled"
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
    assert result.render_plan is None
    assert len(llm.calls) == 1
    assert all(
        not (
            isinstance(entry, dict)
            and entry.get("type") == "renderer_applicability_routing"
        )
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
    assert (
        result.render_plan.get("request_payload_selected_concept_id") == "#V#task_123"
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
    assert renderer_entry.get("request_payload_object_kind") == "concept"
    assert renderer_entry.get("request_payload_selected_concept_id") == "#V#task_123"



