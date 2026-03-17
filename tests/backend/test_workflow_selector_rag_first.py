"""Tests for the RAG-first workflow selector (JVNAUTOSCI-1424 Phase 2).

Tests cover:
- RAG-first mode: candidate-based selection without static verdicts.
- Legacy mode: backward-compatible verdict mapping.
- Prompt construction in both modes.
- Label extraction from LLM responses.
- Fallback behaviour when no candidates match.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.backend.services.prompt_template_service import PromptTemplateService
from src.backend.workflows import WorkflowRegistry
from src.backend.workflows.workflow_selector import (
    WorkflowSelection,
    WorkflowSelectionPrompt,
    WorkflowSelector,
)
from workflow_test_support import build_test_conversation_turn_registry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_registry() -> WorkflowRegistry:
    return build_test_conversation_turn_registry()


def _build_selector(*, verdict_mapping=None, default_workflow_id=None) -> WorkflowSelector:
    kwargs: dict = {
        "registry": _build_registry(),
        "prompt_service": PromptTemplateService(),
    }
    if verdict_mapping is not None:
        kwargs["verdict_mapping"] = verdict_mapping
    if default_workflow_id is not None:
        kwargs["default_workflow_id"] = default_workflow_id
    return WorkflowSelector(**kwargs)


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
        assert "#V#tool_calling_workflow" in prompt.prompt_text
        assert "#V#chat_assistant_workflow" in prompt.prompt_text
        assert "Tool Calling" in prompt.prompt_text
        assert "Chat Assistant" in prompt.prompt_text
        assert len(prompt.discovered_workflow_ids) == 2

    def test_prompt_with_no_candidates_uses_default(self):
        selector = _build_selector(
            default_workflow_id="#V#chat_assistant_workflow",
        )
        prompt = selector.prepare_selection_prompt(
            turn_text="hello",
            discovered_workflows=[],
        )
        assert "#V#chat_assistant_workflow" in prompt.prompt_text
        assert len(prompt.discovered_workflow_ids) == 1

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
        assert "What is the meaning of life?" in prompt.prompt_text


# ---------------------------------------------------------------------------
# Legacy mode (backward compatibility)
# ---------------------------------------------------------------------------


class TestLegacyMode:
    def test_legacy_mode_enabled_with_verdict_mapping(self):
        selector = _build_selector(
            verdict_mapping={
                "plain_response": "#V#chat_assistant_workflow",
                "tool_seeking": "#V#tool_calling_workflow",
            },
        )
        assert selector.rag_first is False

    def test_legacy_resolves_static_verdicts(self):
        selector = _build_selector(
            verdict_mapping={
                "plain_response": "#V#chat_assistant_workflow",
                "tool_seeking": "#V#tool_calling_workflow",
                "narration": "#V#chat_narration_workflow",
            },
        )
        result = selector.resolve_selection(
            raw_response="tool_seeking",
            prompt_id=None,
            prompt_used="test",
        )
        assert result.workflow_id == "#V#tool_calling_workflow"
        assert result.verdict == "tool_seeking"

    def test_legacy_resolves_discovered_workflow(self):
        selector = _build_selector(
            verdict_mapping={
                "plain_response": "#V#chat_assistant_workflow",
                "tool_seeking": "#V#tool_calling_workflow",
            },
        )
        result = selector.resolve_selection(
            raw_response="#V#todo_refresh_workflow",
            prompt_id=None,
            prompt_used="test",
            discovered_workflow_ids=["#V#todo_refresh_workflow"],
        )
        assert result.workflow_id == "#V#todo_refresh_workflow"

    def test_legacy_fallback_to_plain_response(self):
        selector = _build_selector(
            verdict_mapping={
                "plain_response": "#V#chat_assistant_workflow",
                "tool_seeking": "#V#tool_calling_workflow",
            },
        )
        result = selector.resolve_selection(
            raw_response="gibberish_unknown",
            prompt_id=None,
            prompt_used="test",
        )
        assert result.workflow_id == "#V#chat_assistant_workflow"
        assert result.verdict == "plain_response"


# ---------------------------------------------------------------------------
# Legacy prompt construction
# ---------------------------------------------------------------------------


class TestLegacyPrompt:
    def test_legacy_prompt_contains_verdicts(self):
        selector = _build_selector(
            verdict_mapping={
                "plain_response": "#V#chat_assistant_workflow",
                "tool_seeking": "#V#tool_calling_workflow",
            },
        )
        prompt = selector.prepare_selection_prompt(
            turn_text="hi",
        )
        assert "plain_response" in prompt.prompt_text
        assert "tool_seeking" in prompt.prompt_text

    def test_legacy_prompt_appends_discovered_workflows(self):
        selector = _build_selector(
            verdict_mapping={
                "plain_response": "#V#chat_assistant_workflow",
            },
        )
        prompt = selector.prepare_selection_prompt(
            turn_text="hi",
            discovered_workflows=[
                {
                    "concept_id": "#V#special_workflow",
                    "name": "Special",
                    "description": "Does special things",
                },
            ],
        )
        assert "#V#special_workflow" in prompt.prompt_text
        assert "Special" in prompt.prompt_text
        assert len(prompt.discovered_workflow_ids) == 1


# ---------------------------------------------------------------------------
# Label extraction
# ---------------------------------------------------------------------------


class TestLabelExtraction:
    def test_extract_plain_text_verdict(self):
        label = WorkflowSelector._extract_candidate_label(
            raw_response="tool_seeking",
        )
        assert label == "tool_seeking"

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

    def test_extract_verdict_from_verbose_text(self):
        label = WorkflowSelector._extract_candidate_label(
            raw_response="Based on the request, I recommend tool_seeking.",
        )
        assert label == "tool_seeking"

    def test_normalise_strips_whitespace_and_quotes(self):
        label = WorkflowSelector._normalise_candidate("  'tool_seeking'  ")
        assert label == "tool_seeking"


# ---------------------------------------------------------------------------
# Enabled flag
# ---------------------------------------------------------------------------


class TestEnabled:
    def test_enabled_default(self, monkeypatch):
        monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "1")
        selector = _build_selector()
        assert selector.enabled() is True

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "0")
        selector = _build_selector()
        assert selector.enabled() is False

    def test_disabled_by_false(self, monkeypatch):
        monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "false")
        selector = _build_selector()
        assert selector.enabled() is False


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
