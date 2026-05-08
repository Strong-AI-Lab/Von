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
    assert captured_payloads[0]["renderer_definition_concept_ids"] == expected_ids

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
    assert (
        renderer_entry.get("renderer_definition_source")
        == "canonical_bootstrap_defaults"
    )
    assert renderer_entry.get("resolver_error_code") == "missing_parameter"
    assert isinstance(result.render_plan, dict)
    assert result.render_plan.get("resolver_error_code") == "missing_parameter"
    error_details = result.render_plan.get("resolver_error_details") or {}
    loading = error_details.get("renderer_definition_loading") or {}
    assert loading.get("missing_profile_concept_ids") == ["#V#table_renderer"]
    assert loading.get("malformed_profile_concept_ids") == ["#V#workflow_renderer"]



def test_renderer_applicability_multimodal_selection_sets_spoken_plus_screen(
    monkeypatch,
):
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

