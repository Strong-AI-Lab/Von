"""Tests for the RAG-first workflow selector (JVNAUTOSCI-1424 Phase 2).

Tests cover:
- RAG-first mode: candidate-based selection without static verdicts.
- Prompt construction.
- Label extraction from LLM responses.
- Fallback behaviour when no candidates match.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import src.backend.services.workflow_selection_policy_service as policy_module
from src.backend.workflows import WorkflowRegistry
from src.backend.workflows.workflow_selector import (
    WorkflowSelection,
    WorkflowSelectionPrompt,
    WorkflowSelector,
)
from workflow_test_support import (
    build_test_conversation_turn_registry,
    build_test_prompt_service,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_registry() -> WorkflowRegistry:
    return build_test_conversation_turn_registry()


def _build_selector(*, default_workflow_id=None) -> WorkflowSelector:
    kwargs: dict = {
        "registry": _build_registry(),
        "prompt_service": build_test_prompt_service(),
    }
    if default_workflow_id is not None:
        kwargs["default_workflow_id"] = default_workflow_id
    return WorkflowSelector(**kwargs)


@pytest.fixture(autouse=True)
def _disable_learned_policy(monkeypatch: pytest.MonkeyPatch):
    policy_module.clear_live_selection_policy()
    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)


# ---------------------------------------------------------------------------
# RAG-first mode
# ---------------------------------------------------------------------------


class TestRagFirstMode:
    def test_rag_first_is_default(self):
        selector = _build_selector()
        assert selector.rag_first is True

    def test_rag_first_resolves_matching_candidate(self):
        selector = _build_selector()
        result = selector.resolve_selection(
            raw_response="#V#tool_calling_workflow",
            prompt_id=None,
            prompt_used="test prompt",
            discovered_workflow_ids=[
                "#V#chat_assistant_workflow",
                "#V#tool_calling_workflow",
                "#V#chat_narration_workflow",
            ],
        )
        assert result.workflow_id == "#V#tool_calling_workflow"
        assert result.verdict == "rag_selected"

    def test_rag_first_resolves_case_insensitive(self):
        selector = _build_selector()
        result = selector.resolve_selection(
            raw_response="#v#TOOL_CALLING_WORKFLOW",
            prompt_id=None,
            prompt_used="test",
            discovered_workflow_ids=["#V#tool_calling_workflow"],
        )
        assert result.workflow_id == "#V#tool_calling_workflow"
        assert result.verdict == "rag_selected"

    def test_rag_first_fallback_to_default(self):
        selector = _build_selector(
            default_workflow_id="#V#chat_assistant_workflow",
        )
        result = selector.resolve_selection(
            raw_response="completely_unknown_response",
            prompt_id=None,
            prompt_used="test",
            discovered_workflow_ids=[
                "#V#tool_calling_workflow",
                "#V#chat_narration_workflow",
            ],
        )
        assert result.workflow_id == "#V#chat_assistant_workflow"
        assert result.verdict == "rag_default"

    def test_rag_first_finds_candidate_in_verbose_response(self):
        selector = _build_selector()
        result = selector.resolve_selection(
            raw_response="I recommend #V#tool_calling_workflow for this task",
            prompt_id=None,
            prompt_used="test",
            discovered_workflow_ids=[
                "#V#chat_assistant_workflow",
                "#V#tool_calling_workflow",
            ],
        )
        assert result.workflow_id == "#V#tool_calling_workflow"
        assert result.verdict == "rag_selected"

    def test_rag_first_json_response(self):
        import json

        selector = _build_selector()
        result = selector.resolve_selection(
            raw_response=json.dumps({"workflow_id": "#V#chat_narration_workflow"}),
            prompt_id=None,
            prompt_used="test",
            discovered_workflow_ids=[
                "#V#chat_assistant_workflow",
                "#V#chat_narration_workflow",
            ],
        )
        assert result.workflow_id == "#V#chat_narration_workflow"
        assert result.verdict == "rag_selected"


# ---------------------------------------------------------------------------
# RAG-first prompt construction
# ---------------------------------------------------------------------------


class TestRagFirstPrompt:
    def test_prompt_includes_candidates(self):
        selector = _build_selector()
        prompt = selector.prepare_selection_prompt(
            turn_text="Find arXiv papers about transformers",
            discovered_workflows=[
                {
                    "concept_id": "#V#tool_calling_workflow",
                    "name": "Tool Calling",
                    "description": "General-purpose tool-calling pipeline.",
                },
                {
                    "concept_id": "#V#chat_assistant_workflow",
                    "name": "Chat Assistant",
                    "description": "Simple conversational response.",
                },
            ],
        )
        assert isinstance(prompt, WorkflowSelectionPrompt)
        assert prompt.prompt_text is not None
        assert "#V#tool_calling_workflow" in prompt.prompt_text
        assert "#V#chat_assistant_workflow" in prompt.prompt_text
        assert "Tool Calling" in prompt.prompt_text
        assert "Chat Assistant" in prompt.prompt_text
        assert len(prompt.discovered_workflow_ids) == 2
        assert [entry["concept_id"] for entry in prompt.candidate_entries] == [
            "#V#tool_calling_workflow",
            "#V#chat_assistant_workflow",
        ]

    def test_prompt_with_no_candidates_uses_default(self):
        selector = _build_selector(
            default_workflow_id="#V#chat_assistant_workflow",
        )
        prompt = selector.prepare_selection_prompt(
            turn_text="hello",
            discovered_workflows=[],
        )
        assert prompt.prompt_text is not None
        assert "#V#chat_assistant_workflow" in prompt.prompt_text
        assert len(prompt.discovered_workflow_ids) == 1
        assert prompt.candidate_entries == (
            {
                "concept_id": "#V#chat_assistant_workflow",
                "name": "Default workflow",
                "description": "",
            },
        )

    def test_prompt_includes_turn_text(self):
        selector = _build_selector()
        prompt = selector.prepare_selection_prompt(
            turn_text="What is the meaning of life?",
            discovered_workflows=[
                {
                    "concept_id": "#V#chat_assistant_workflow",
                    "name": "Chat",
                },
            ],
        )
        assert prompt.prompt_text is not None
        assert "What is the meaning of life?" in prompt.prompt_text

    def test_prompt_missing_candidate_list_fails_closed(self):
        selector = WorkflowSelector(
            registry=_build_registry(),
            prompt_service=build_test_prompt_service(
                prompt_text="Route this turn:\n{turn_text}\n",
            ),
        )

        prompt = selector.prepare_selection_prompt(
            turn_text="Find arXiv papers about transformers",
            discovered_workflows=[
                {
                    "concept_id": "#V#tool_calling_workflow",
                    "name": "Tool Calling",
                    "description": "General-purpose tool-calling pipeline.",
                }
            ],
        )

        assert prompt.prompt_failure_reason == "selector_prompt_missing_candidate_list"
        assert prompt.prompt_text is not None

    def test_select_workflow_prompt_unavailable_fails_closed_without_selector_llm(self):
        selector = WorkflowSelector(
            registry=_build_registry(),
            prompt_service=build_test_prompt_service(prompt_text=None),
            default_workflow_id="#V#chat_assistant_workflow",
        )
        mock_llm = MagicMock()

        result = selector.select_workflow(
            llm_client=mock_llm,
            model="test-model",
            turn_text="Find papers about AI",
            discovered_workflows=[
                {
                    "concept_id": "#V#tool_calling_workflow",
                    "name": "Tool Calling",
                    "description": "General-purpose tool pipeline.",
                }
            ],
        )

        assert isinstance(result, WorkflowSelection)
        assert result.workflow_id == "#V#chat_assistant_workflow"
        assert result.verdict == "selector_prompt_unavailable"
        assert result.selection_source == "selector_fail_closed"
        mock_llm.generate.assert_not_called()


# ---------------------------------------------------------------------------
# Label extraction
# ---------------------------------------------------------------------------


class TestLabelExtraction:
    def test_extract_plain_text_workflow_id(self):
        label = WorkflowSelector._extract_candidate_label(
            raw_response="#V#tool_calling_workflow",
            discovered_workflow_ids=["#V#tool_calling_workflow"],
        )
        assert label == "#V#tool_calling_workflow"

    def test_extract_discovered_workflow_id(self):
        label = WorkflowSelector._extract_candidate_label(
            raw_response="#V#custom_workflow",
            discovered_workflow_ids=["#V#custom_workflow"],
        )
        assert label == "#V#custom_workflow"

    def test_extract_from_json(self):
        import json

        label = WorkflowSelector._extract_candidate_label(
            raw_response=json.dumps({"workflow_id": "#V#test_wf"}),
            discovered_workflow_ids=["#V#test_wf"],
        )
        assert label == "#V#test_wf"

    def test_extract_workflow_id_from_verbose_text(self):
        label = WorkflowSelector._extract_candidate_label(
            raw_response="Based on the request, I recommend #V#tool_calling_workflow.",
            discovered_workflow_ids=["#V#tool_calling_workflow"],
        )
        assert label == "#V#tool_calling_workflow"

    def test_normalise_strips_whitespace_and_quotes(self):
        label = WorkflowSelector._normalise_candidate("  '#V#tool_calling_workflow'  ")
        assert label == "#v#tool_calling_workflow"


# ---------------------------------------------------------------------------
# Enabled flag
# ---------------------------------------------------------------------------


class TestEnabled:
    def test_enabled_default(self):
        selector = _build_selector()
        assert selector.enabled() is True

    def test_enabled_ignores_legacy_zero_toggle(self, monkeypatch):
        monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "0")
        selector = _build_selector()
        assert selector.enabled() is True

    def test_enabled_ignores_legacy_false_toggle(self, monkeypatch):
        monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "false")
        selector = _build_selector()
        assert selector.enabled() is True


# ---------------------------------------------------------------------------
# select_workflow integration
# ---------------------------------------------------------------------------


class TestSelectWorkflow:
    def test_select_workflow_calls_llm_and_resolves(self):
        selector = _build_selector()
        mock_llm = MagicMock()
        mock_llm.generate.return_value = "#V#tool_calling_workflow"

        candidates = [
            {
                "concept_id": "#V#tool_calling_workflow",
                "name": "Tool Calling",
                "description": "General-purpose tool pipeline.",
            },
            {
                "concept_id": "#V#chat_assistant_workflow",
                "name": "Chat Assistant",
                "description": "Plain response.",
            },
        ]

        result = selector.select_workflow(
            llm_client=mock_llm,
            model="test-model",
            turn_text="Find papers about AI",
            discovered_workflows=candidates,
        )
        assert isinstance(result, WorkflowSelection)
        assert result.workflow_id == "#V#tool_calling_workflow"
        assert result.verdict == "rag_selected"
        mock_llm.generate.assert_called_once()
