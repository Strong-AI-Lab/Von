"""Tests for the workflow capability index (JVNAUTOSCI-1424 Phase 2).

Tests cover:
- BM25 indexing and search behaviour.
- Built-in workflow capability descriptions.
- Registry integration (eager + lazy workflows).
- Score normalisation and ranking order.
"""

from __future__ import annotations

import pytest

from src.backend.services.workflow_capability_service import (
    BUILTIN_WORKFLOW_CAPABILITIES,
    WorkflowCapabilityIndex,
    WorkflowCapabilityMatch,
    _tokenise,
    _workflow_id_to_name,
    build_workflow_capability_text,
    get_workflow_capability_index,
    reset_workflow_capability_index,
)
from workflow_test_support import build_test_conversation_turn_registry


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------


class TestTokenise:
    def test_basic_words(self):
        tokens = _tokenise("hello world tool calling")
        assert "hello" in tokens
        assert "world" in tokens

    def test_stop_words_removed(self):
        tokens = _tokenise("this is a test of the system")
        assert "test" in tokens
        assert "system" in tokens
        assert "this" not in tokens
        assert "is" not in tokens
        assert "the" not in tokens

    def test_single_char_removed(self):
        tokens = _tokenise("a b c long")
        assert "long" in tokens
        # Single-char tokens should be excluded.
        assert "b" not in tokens
        assert "c" not in tokens


# ---------------------------------------------------------------------------
# Index — build and search
# ---------------------------------------------------------------------------


class TestWorkflowCapabilityIndex:
    def test_empty_index_returns_no_results(self):
        index = WorkflowCapabilityIndex()
        results = index.search("hello")
        assert results == []

    def test_single_entry_matches_query(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow(
            "#V#test_workflow",
            "Tool calling pipeline for external API calls and data retrieval",
            metadata={"name": "Test Workflow", "source": "test"},
        )
        results = index.search("API calls and data retrieval")
        assert len(results) >= 1
        assert results[0].workflow_id == "#V#test_workflow"

    def test_multiple_entries_rank_by_relevance(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow(
            "#V#chat",
            "Simple conversational greetings and acknowledgements without tools",
        )
        index.index_workflow(
            "#V#tools",
            "Tool calling pipeline for external API calls data retrieval file operations",
        )
        index.index_workflow(
            "#V#narration",
            "Narrative generation and storytelling prose composition",
        )
        results = index.search("retrieve data from external API")
        assert len(results) >= 1
        # The tool workflow should be the top result.
        assert results[0].workflow_id == "#V#tools"

    def test_max_results_limits_output(self):
        index = WorkflowCapabilityIndex()
        for i in range(20):
            index.index_workflow(f"#V#wf_{i}", f"workflow {i} capability text")
        results = index.search("workflow capability", max_results=5)
        assert len(results) <= 5

    def test_min_score_filters_low_matches(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow("#V#match", "keyword aligned with query terms")
        index.index_workflow("#V#unrelated", "completely different topic about cooking")
        results = index.search("keyword aligned query", min_score=0.5)
        # The unrelated entry should be excluded or scored very low.
        for r in results:
            assert r.relevance_score >= 0.5

    def test_exclude_ids(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow("#V#a", "same text for testing exclusion")
        index.index_workflow("#V#b", "same text for testing exclusion")
        results = index.search("testing exclusion", exclude_ids={"#V#a"})
        ids = [r.workflow_id for r in results]
        assert "#V#a" not in ids
        assert "#V#b" in ids

    def test_score_normalisation_0_to_1(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow("#V#a", "alpha bravo charlie")
        index.index_workflow("#V#b", "delta echo foxtrot")
        results = index.search("alpha bravo")
        for r in results:
            assert 0.0 <= r.relevance_score <= 1.0

    def test_size_reflects_entries(self):
        index = WorkflowCapabilityIndex()
        assert index.size == 0
        index.index_workflow("#V#one", "first workflow")
        assert index.size == 1
        index.index_workflow("#V#two", "second workflow")
        assert index.size == 2
        # Re-indexing same ID replaces.
        index.index_workflow("#V#one", "updated first workflow")
        assert index.size == 2

    def test_to_discovery_dict_format(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow(
            "#V#test_wf",
            "Description for test workflow",
            metadata={"name": "Test Wf"},
        )
        results = index.search("test workflow")
        assert len(results) >= 1
        d = results[0].to_discovery_dict()
        assert d["concept_id"] == "#V#test_wf"
        assert d["name"] == "Test Wf"
        assert "match_source" in d
        assert d["match_source"] == "capability_index"
        assert isinstance(d["relevance_score"], float)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestIndexFromRegistry:
    def test_indexes_conversation_turn_workflows_from_vontology_purpose(self):
        registry = build_test_conversation_turn_registry()
        index = WorkflowCapabilityIndex()
        count = index.index_from_registry(registry)
        assert count > 0
        results = index.search("tool calling pipeline")
        assert any(r.workflow_id == "#V#tool_calling_workflow" for r in results)

    def test_indexes_lazy_registrations(self):
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import LazyWorkflowRegistration

        registry = WorkflowRegistry()
        lazy = LazyWorkflowRegistration(
            workflow_id="#V#test_lazy_wf",
            purpose="Analyse research papers and extract key insights",
            source="vontology",
        )
        registry.register_lazy(lazy)
        index = WorkflowCapabilityIndex()
        count = index.index_from_registry(registry)
        assert count >= 1
        results = index.search("research papers key insights")
        assert any(r.workflow_id == "#V#test_lazy_wf" for r in results)

    def test_lazy_without_purpose_is_skipped(self):
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import LazyWorkflowRegistration

        registry = WorkflowRegistry()
        lazy = LazyWorkflowRegistration(
            workflow_id="#V#entity_resolution_workflow",
            source="vontology",
        )
        registry.register_lazy(lazy)
        index = WorkflowCapabilityIndex()
        count = index.index_from_registry(registry)
        assert count == 0
        results = index.search("entity resolution")
        assert all(r.workflow_id != "#V#entity_resolution_workflow" for r in results)

    def test_non_vontology_registration_is_skipped(self):
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import WorkflowRegistration
        from src.backend.workflows.engine import WorkflowDefinition, WorkflowStateSpec

        registry = WorkflowRegistry()
        definition = WorkflowDefinition(
            workflow_id="#V#built_in_workflow",
            initial_state="start",
            states={"start": WorkflowStateSpec(state_id="start", terminal=True)},
            termination_states=("start",),
            purpose="Built-in workflow purpose text",
        )
        registry.register(
            WorkflowRegistration(
                workflow_id="#V#built_in_workflow",
                definition=definition,
                purpose="Built-in workflow purpose text",
                source="built_in",
            )
        )

        index = WorkflowCapabilityIndex()
        count = index.index_from_registry(registry)

        assert count == 0
        assert index.search("workflow purpose text") == []


# ---------------------------------------------------------------------------
# Conversation-turn capabilities from authoritative purpose text
# ---------------------------------------------------------------------------


class TestPurposeDrivenCapabilities:
    def test_builtin_override_surface_is_empty(self):
        assert BUILTIN_WORKFLOW_CAPABILITIES == {}

    def test_chat_assistant_matches_greeting_queries(self):
        index = WorkflowCapabilityIndex()
        index.index_from_registry(build_test_conversation_turn_registry())
        results = index.search("hello how are you")
        assert len(results) >= 1
        top = results[0]
        assert top.workflow_id == "#V#chat_assistant_workflow"

    def test_tool_calling_matches_data_queries(self):
        index = WorkflowCapabilityIndex()
        index.index_from_registry(build_test_conversation_turn_registry())
        results = index.search("search arXiv for papers about transformers")
        assert len(results) >= 1
        top = results[0]
        assert top.workflow_id == "#V#tool_calling_workflow"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_workflow_id_to_name(self):
        assert _workflow_id_to_name("#V#tool_calling_workflow") == "Tool Calling Workflow"
        assert _workflow_id_to_name("some_workflow") == "Some Workflow"

    def test_build_capability_text_uses_purpose(self):
        text = build_workflow_capability_text(
            "#V#unknown_wf",
            purpose="Custom purpose text",
        )
        assert "Custom purpose text" in text

    def test_build_capability_text_prefers_description_when_present(self):
        text = build_workflow_capability_text(
            "#V#chat_assistant_workflow",
            description="Direct conversational response workflow.",
        )
        assert "Direct conversational response workflow." in text

    def test_build_capability_text_fallback(self):
        text = build_workflow_capability_text("#V#mystery_workflow")
        assert "Mystery Workflow" in text


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_returns_same_instance(self):
        reset_workflow_capability_index()
        idx1 = get_workflow_capability_index()
        idx2 = get_workflow_capability_index()
        assert idx1 is idx2

    def test_reset_creates_new_instance(self):
        reset_workflow_capability_index()
        idx1 = get_workflow_capability_index()
        reset_workflow_capability_index()
        idx2 = get_workflow_capability_index()
        assert idx1 is not idx2
