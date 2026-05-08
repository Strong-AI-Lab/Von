"""Focused workflow selector routing tests split from the shared orchestrator harness."""

from __future__ import annotations

from tests.backend.test_orchestrator_workflow_selector_routing import *  # noqa: F401,F403
from tests.backend.test_orchestrator_workflow_selector_routing import (
    _CapturingLLM,
    _ModelCandidate,
    _build_orchestrator,
    _build_structured_turn_contract_payload,
    _dispatch_surface,
    _register_terminal_custom_workflow,
    _stub_execute_workflow_result,
)


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


