"""Tests for workflow discovery service (JVNAUTOSCI-1076).

Validates workflow discovery during conversation turns including:
- WorkflowMatch and WorkflowDiscoveryResult dataclasses
- Semantic and Vontology search integration
- Relevance filtering and deduplication
- discover_workflows_for_turn convenience wrapper
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.backend.services.workflow_discovery_service import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_RELEVANCE_THRESHOLD,
    EXECUTABILITY_DRAFT_NOT_PUBLISHED,
    EXECUTABILITY_EXECUTABLE_NOW,
    EXECUTABILITY_GRAPH_INCOMPLETE,
    EXECUTABILITY_WORKFLOW_STEP_COMPLETELY_VACUOUS,
    EXECUTABILITY_WORKFLOW_STEP_PARTIALLY_VACUOUS,
    EXECUTABILITY_WORKFLOW_STEP_INTEGRITY,
    EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT,
    ROUTING_EXCLUSION_MISSING_AUTHORITATIVE_PURPOSE,
    WORKFLOW_TYPE_IDS,
    WorkflowDiscoveryResult,
    WorkflowMatch,
    _build_keyword_fallback_queries,
    _classify_workflow_concept_executability,
    _deduplicate_and_rank,
    _get_workflow_description,
    _get_workflow_name,
    _is_executable_workflow_concept,
    discover_workflows,
    discover_workflows_for_turn,
    invalidate_workflow_discovery_executability_caches,
)


@pytest.fixture(autouse=True)
def _use_mock_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable mock DB for workflow discovery tests, scoped per-test to avoid env leakage."""
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")


class TestWorkflowMatch:
    """Unit tests for WorkflowMatch dataclass."""

    def test_create_with_defaults(self) -> None:
        """WorkflowMatch should have sensible defaults."""
        match = WorkflowMatch(concept_id="#V#test", name="Test Workflow")
        assert match.concept_id == "#V#test"
        assert match.name == "Test Workflow"
        assert match.description is None
        assert match.relevance_score == 0.0
        assert match.match_source == "unknown"

    def test_to_dict_returns_correct_structure(self) -> None:
        """to_dict should return JSON-serialisable dict."""
        match = WorkflowMatch(
            concept_id="#V#test_workflow",
            name="Test Workflow",
            description="A test workflow",
            relevance_score=0.8567,
            match_source="semantic",
        )
        result = match.to_dict()

        assert result["concept_id"] == "#V#test_workflow"
        assert result["name"] == "Test Workflow"
        assert result["description"] == "A test workflow"
        assert result["relevance_score"] == 0.857  # Rounded to 3 decimal places
        assert result["match_source"] == "semantic"

    def test_to_dict_rounds_relevance_score(self) -> None:
        """to_dict should round relevance_score to 3 decimal places."""
        match = WorkflowMatch(
            concept_id="#V#test",
            name="Test",
            relevance_score=0.123456789,
        )
        assert match.to_dict()["relevance_score"] == 0.123


class TestWorkflowDiscoveryResult:
    """Unit tests for WorkflowDiscoveryResult dataclass."""

    def test_create_with_defaults(self) -> None:
        """WorkflowDiscoveryResult should have sensible defaults."""
        result = WorkflowDiscoveryResult()
        assert result.matches == []
        assert result.search_time_ms == 0.0
        assert result.query == ""
        assert result.threshold == DEFAULT_RELEVANCE_THRESHOLD
        assert result.errors == []

    def test_to_dict_returns_correct_structure(self) -> None:
        """to_dict should return JSON-serialisable dict."""
        matches = [
            WorkflowMatch(
                concept_id="#V#wf1",
                name="Workflow 1",
                relevance_score=0.9,
            ),
            WorkflowMatch(
                concept_id="#V#wf2",
                name="Workflow 2",
                relevance_score=0.8,
            ),
        ]
        result = WorkflowDiscoveryResult(
            matches=matches,
            search_time_ms=123.456,
            query="test query",
            requested_query="requested test query",
            threshold=0.7,
            errors=["minor warning"],
            search_sources=["semantic", "vontology"],
            keyword_fallback_queries=["workflow fallback"],
            allow_non_executable=True,
        )
        output = result.to_dict()

        assert len(output["matches"]) == 2
        assert output["search_time_ms"] == 123.46  # Rounded to 2 decimal places
        assert output["query"] == "test query"
        assert output["requested_query"] == "requested test query"
        assert output["threshold"] == 0.7
        assert output["match_count"] == 2
        assert output["candidate_count"] == 2
        assert output["search_sources"] == ["semantic", "vontology"]
        assert output["keyword_fallback_queries"] == ["workflow fallback"]
        assert output["allow_non_executable"] is True
        assert output["errors"] == ["minor warning"]

    def test_to_dict_errors_none_when_empty(self) -> None:
        """to_dict should return None for errors when list is empty."""
        result = WorkflowDiscoveryResult(errors=[])
        assert result.to_dict()["errors"] is None


class TestGetWorkflowDescription:
    """Unit tests for _get_workflow_description helper."""

    def test_extracts_from_metadata(self) -> None:
        """Should extract description from metadata.description field."""
        concept_doc = {"metadata": {"description": "Workflow description"}}
        assert _get_workflow_description(concept_doc) == "Workflow description"

    def test_extracts_from_names_text(self) -> None:
        """Should extract from names array text field as fallback."""
        concept_doc = {"names": [{"text": "Workflow text description"}]}
        assert _get_workflow_description(concept_doc) == "Workflow text description"

    def test_returns_none_when_no_description(self) -> None:
        """Should return None when no description found."""
        concept_doc = {"concept_id": "#V#test"}
        assert _get_workflow_description(concept_doc) is None

    def test_strips_whitespace(self) -> None:
        """Should strip whitespace from description."""
        concept_doc = {"metadata": {"description": "  trimmed  "}}
        assert _get_workflow_description(concept_doc) == "trimmed"


class TestGetWorkflowName:
    """Unit tests for _get_workflow_name helper."""

    @patch(
        "src.backend.vontology.utils_vontology.get_concept_display_name_with_names_fallback",
        side_effect=Exception("Import failed"),
    )
    def test_extracts_from_name_field(self, mock_display: MagicMock) -> None:
        """Should extract from top-level name field."""
        concept_doc = {"name": "Test Workflow"}
        assert _get_workflow_name(concept_doc) == "Test Workflow"

    @patch(
        "src.backend.vontology.utils_vontology.get_concept_display_name_with_names_fallback",
        side_effect=Exception("Import failed"),
    )
    def test_extracts_from_names_array(self, mock_display: MagicMock) -> None:
        """Should extract from names array as fallback."""
        concept_doc = {"names": [{"name": "Workflow Name"}]}
        assert _get_workflow_name(concept_doc) == "Workflow Name"

    @patch(
        "src.backend.vontology.utils_vontology.get_concept_display_name_with_names_fallback",
        side_effect=Exception("Import failed"),
    )
    def test_falls_back_to_concept_id(self, mock_display: MagicMock) -> None:
        """Should fall back to concept_id when no name found."""
        concept_doc = {"concept_id": "#V#my_workflow"}
        assert _get_workflow_name(concept_doc) == "#V#my_workflow"


class TestDeduplicateAndRank:
    """Unit tests for _deduplicate_and_rank helper."""

    def test_removes_duplicates_by_concept_id(self) -> None:
        """Should keep only first occurrence of each concept_id."""
        matches = [
            WorkflowMatch("#V#wf1", "Workflow 1", relevance_score=0.9),
            WorkflowMatch("#V#wf1", "Workflow 1 (dup)", relevance_score=0.8),
            WorkflowMatch("#V#wf2", "Workflow 2", relevance_score=0.85),
        ]
        result = _deduplicate_and_rank(matches, threshold=0.5, max_results=10)

        assert len(result) == 2
        assert result[0].concept_id == "#V#wf1"
        assert result[1].concept_id == "#V#wf2"

    def test_filters_below_threshold(self) -> None:
        """Should filter matches below relevance threshold."""
        matches = [
            WorkflowMatch("#V#wf1", "High", relevance_score=0.9),
            WorkflowMatch("#V#wf2", "Low", relevance_score=0.3),
        ]
        result = _deduplicate_and_rank(matches, threshold=0.5, max_results=10)

        assert len(result) == 1
        assert result[0].concept_id == "#V#wf1"

    def test_respects_max_results(self) -> None:
        """Should limit results to max_results."""
        matches = [
            WorkflowMatch(f"#V#wf{i}", f"Workflow {i}", relevance_score=0.9 - i * 0.01)
            for i in range(10)
        ]
        result = _deduplicate_and_rank(matches, threshold=0.5, max_results=3)

        assert len(result) == 3

    def test_sorts_by_score_descending(self) -> None:
        """Should sort matches by relevance score descending."""
        matches = [
            WorkflowMatch("#V#wf1", "Low", relevance_score=0.6),
            WorkflowMatch("#V#wf2", "High", relevance_score=0.95),
            WorkflowMatch("#V#wf3", "Medium", relevance_score=0.8),
        ]
        result = _deduplicate_and_rank(matches, threshold=0.5, max_results=10)

        assert result[0].concept_id == "#V#wf2"  # Highest score first
        assert result[1].concept_id == "#V#wf3"
        assert result[2].concept_id == "#V#wf1"


class TestKeywordFallbackQueries:
    """Unit tests for keyword fallback query generation."""

    def test_adds_arxiv_specific_queries_for_bare_arxiv_url(self) -> None:
        queries = _build_keyword_fallback_queries(
            "https://arxiv.org/abs/2602.20478",
            [],
        )

        assert "arxiv paper representation workflow" in queries
        assert "scholarly paper representation workflow" in queries
        assert "arxiv workflow" in queries
        assert "arxiv 2602.20478" in queries

    def test_adds_talk_and_seminar_queries_for_presentation_prompt(self) -> None:
        queries = _build_keyword_fallback_queries(
            "Can you make the workflow for adding academic talks now?",
            [],
        )

        assert "talk representation workflow" in queries
        assert "technical scientific talk representation workflow" in queries
        assert "academic presentation workflow" in queries
        assert "seminar representation workflow" in queries


class TestDiscoverWorkflows:
    """Unit tests for discover_workflows function."""

    def test_returns_empty_for_invalid_query(self) -> None:
        """Should return error result for invalid query."""
        result = discover_workflows("")
        assert result.matches == []
        assert "empty query" in result.errors[0].lower()

    def test_returns_empty_for_none_query(self) -> None:
        """Should return error result for None query."""
        result = discover_workflows(None)  # type: ignore
        assert result.matches == []
        assert "Invalid or empty query" in result.errors[0]

    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    def test_combines_semantic_and_vontology_results(
        self, mock_vontology: MagicMock, mock_semantic: MagicMock
    ) -> None:
        """Should combine results from both search sources."""
        mock_semantic.return_value = [
            WorkflowMatch("#V#wf1", "Semantic Workflow", relevance_score=0.9)
        ]
        mock_vontology.return_value = [
            WorkflowMatch("#V#wf2", "Vontology Workflow", relevance_score=0.85)
        ]

        with patch(
            "src.backend.services.workflow_discovery_service._enrich_workflow_matches",
            side_effect=lambda x: x,
        ):
            result = discover_workflows("test query")

        assert len(result.matches) == 2
        assert result.matches[0].concept_id == "#V#wf1"  # Higher score first
        assert result.matches[1].concept_id == "#V#wf2"

    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    def test_deduplicates_across_sources(
        self, mock_vontology: MagicMock, mock_semantic: MagicMock
    ) -> None:
        """Should deduplicate workflows found in both sources."""
        mock_semantic.return_value = [
            WorkflowMatch("#V#wf1", "Workflow 1", relevance_score=0.9)
        ]
        mock_vontology.return_value = [
            WorkflowMatch("#V#wf1", "Workflow 1 (vontology)", relevance_score=0.8)
        ]

        with patch(
            "src.backend.services.workflow_discovery_service._enrich_workflow_matches",
            side_effect=lambda x: x,
        ):
            result = discover_workflows("test query")

        assert len(result.matches) == 1
        assert result.matches[0].concept_id == "#V#wf1"
        assert result.matches[0].relevance_score == 0.9  # Keep higher score

    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    def test_continues_on_semantic_search_error(self, mock_semantic: MagicMock) -> None:
        """Should continue if semantic search fails and try vontology."""
        mock_semantic.side_effect = Exception("Semantic search failed")

        with patch(
            "src.backend.services.workflow_discovery_service._search_workflows_vontology"
        ) as mock_vontology:
            mock_vontology.return_value = [
                WorkflowMatch("#V#wf1", "Workflow", relevance_score=0.9)
            ]
            with patch(
                "src.backend.services.workflow_discovery_service._enrich_workflow_matches",
                side_effect=lambda x: x,
            ):
                result = discover_workflows("test query")

        assert len(result.matches) == 1
        assert "semantic_search_error" in result.errors[0]

    def test_records_search_time(self) -> None:
        """Should record search time in result."""
        with patch(
            "src.backend.services.workflow_discovery_service._search_workflows_semantic",
            return_value=[],
        ):
            with patch(
                "src.backend.services.workflow_discovery_service._search_workflows_vontology",
                return_value=[],
            ):
                result = discover_workflows("test query")

        assert result.search_time_ms > 0

    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._has_authoritative_routing_text"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability"
    )
    def test_mixed_candidates_expose_reason_codes_and_routing_subset(
        self,
        mock_classify: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_vontology: MagicMock,
        mock_semantic: MagicMock,
    ) -> None:
        """Discovery should retain mixed candidates while routing only executable ones."""
        mock_semantic.return_value = [
            WorkflowMatch("#V#wf_exec", "Executable", relevance_score=0.8),
            WorkflowMatch("#V#wf_graph", "Graph Incomplete", relevance_score=0.9),
        ]
        mock_vontology.return_value = [
            WorkflowMatch("#V#wf_design", "Design Artefact", relevance_score=0.85),
        ]

        def _classify(concept_id: str):
            if concept_id == "#V#wf_exec":
                return (True, EXECUTABILITY_EXECUTABLE_NOW, None)
            if concept_id == "#V#wf_graph":
                return (False, EXECUTABILITY_GRAPH_INCOMPLETE, "missing_step_concepts")
            return (
                False,
                EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT,
                "workflow_has_no_steps",
            )

        mock_classify.side_effect = _classify
        mock_has_authoritative_text.side_effect = lambda concept_id: concept_id == "#V#wf_exec"

        with patch(
            "src.backend.services.workflow_discovery_service._enrich_workflow_matches",
            side_effect=lambda x: x,
        ):
            result = discover_workflows("test query", max_results=3)

        assert len(result.matches) == 3
        reasons = {m.concept_id: m.executability_reason for m in result.matches}
        assert reasons["#V#wf_exec"] == EXECUTABILITY_EXECUTABLE_NOW
        assert reasons["#V#wf_graph"] == EXECUTABILITY_GRAPH_INCOMPLETE
        assert (
            reasons["#V#wf_design"]
            == EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT
        )
        assert len(result.routing_matches or []) == 1
        assert (result.routing_matches or [])[0].concept_id == "#V#wf_exec"

    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
        return_value=True,
    )
    @patch(
        "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability"
    )
    def test_allow_non_executable_override_keeps_mixed_routing_candidates(
        self,
        mock_classify: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_vontology: MagicMock,
        mock_semantic: MagicMock,
    ) -> None:
        """Explicit override should keep non-executable candidates in routing matches."""
        mock_semantic.return_value = [
            WorkflowMatch("#V#wf_exec", "Executable", relevance_score=0.8),
        ]
        mock_vontology.return_value = [
            WorkflowMatch("#V#wf_graph", "Graph Incomplete", relevance_score=0.79),
        ]
        mock_classify.side_effect = [
            (True, EXECUTABILITY_EXECUTABLE_NOW, None),
            (False, EXECUTABILITY_GRAPH_INCOMPLETE, "missing_step_concepts"),
        ]

        with patch(
            "src.backend.services.workflow_discovery_service._enrich_workflow_matches",
            side_effect=lambda x: x,
        ):
            result = discover_workflows(
                "test query",
                max_results=3,
                allow_non_executable=True,
            )

        assert len(result.matches) == 2
        assert len(result.routing_matches or []) == 2


class TestDiscoverWorkflowsForTurn:
    """Unit tests for discover_workflows_for_turn convenience wrapper."""

    def test_returns_none_for_short_input(self) -> None:
        """Should return None for very short inputs."""
        result = discover_workflows_for_turn("hi")
        assert result is None

    def test_returns_none_for_empty_input(self) -> None:
        """Should return None for empty input."""
        result = discover_workflows_for_turn("")
        assert result is None

    @patch("src.backend.services.workflow_discovery_service.discover_workflows")
    def test_returns_none_when_no_matches(self, mock_discover: MagicMock) -> None:
        """Should return None when discover_workflows finds no matches."""
        mock_discover.return_value = WorkflowDiscoveryResult(matches=[])
        result = discover_workflows_for_turn("test query input")
        assert result is None

    @patch("src.backend.services.workflow_discovery_service.discover_workflows")
    def test_returns_candidate_payload_when_only_non_routing_candidates_exist(
        self, mock_discover: MagicMock
    ) -> None:
        """Wrapper should surface non-routing candidates for telemetry and UX."""
        mock_discover.return_value = WorkflowDiscoveryResult(
            matches=[
                WorkflowMatch(
                    "#V#wf_graph",
                    "Graph Incomplete",
                    relevance_score=0.92,
                    is_executable=False,
                    executability_reason=EXECUTABILITY_GRAPH_INCOMPLETE,
                )
            ],
            routing_matches=[],
        )

        result = discover_workflows_for_turn("test query input")
        assert result is not None
        assert result["match_count"] == 0
        assert result["candidate_count"] == 1
        assert len(result["candidates"]) == 1
        assert result["matches"] == []

    @patch("src.backend.services.workflow_discovery_service.discover_workflows")
    def test_returns_dict_when_matches_found(self, mock_discover: MagicMock) -> None:
        """Should return dict with matches when workflows found."""
        mock_discover.return_value = WorkflowDiscoveryResult(
            matches=[WorkflowMatch("#V#wf1", "Workflow 1", relevance_score=0.9)],
            search_time_ms=50.0,
            query="test query input",
        )
        result = discover_workflows_for_turn("test query input")

        assert result is not None
        assert result["match_count"] == 1
        assert len(result["matches"]) == 1
        assert result["requested_query"] == "test query input"

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch("src.backend.services.workflow_discovery_service._search_workflows_name_fallback")
    @patch("src.backend.services.workflow_discovery_service._search_workflows_vontology")
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch("src.backend.services.workflow_discovery_service.build_file_copy_typing_context")
    def test_augments_discovery_query_with_typed_file_copy_context(
        self,
        mock_typing_context: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_name_fallback: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_typing_context.return_value = {
            "file_copy_concept_id": "#V#uploaded_file_copy_abc123",
            "route_hint": "scholarly",
            "type_display_names": ["Scholarly paper file copy", "PDF file copy"],
            "original_filename": "2502.14996.pdf",
            "content_type": "application/pdf",
        }
        mock_semantic.return_value = []
        mock_vontology.return_value = []
        mock_name_fallback.return_value = []
        mock_enrich.side_effect = lambda matches: matches

        result = discover_workflows(
            "Fully represent #V#uploaded_file_copy_abc123 now.",
            max_results=3,
        )

        semantic_query = mock_semantic.call_args.args[0]
        assert "Artefact typing context:" in semantic_query
        assert "route_hint=scholarly" in semantic_query
        assert "types=Scholarly paper file copy, PDF file copy" in semantic_query
        fallback_queries = mock_name_fallback.call_args.args[0]
        assert "scholarly workflow" in fallback_queries
        assert "scholarly representation workflow" in fallback_queries
        assert "Scholarly paper file copy" in fallback_queries
        assert "PDF file copy" in fallback_queries
        assert "route_hint=scholarly" in result.query

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch("src.backend.services.workflow_discovery_service._has_authoritative_routing_text")
    @patch("src.backend.services.workflow_discovery_service._classify_workflow_concept_executability")
    @patch("src.backend.services.workflow_discovery_service._search_workflows_name_fallback")
    @patch("src.backend.services.workflow_discovery_service._search_workflows_vontology")
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    def test_textless_fallback_match_is_visible_but_not_routing_eligible(
        self,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_name_fallback: MagicMock,
        mock_classify: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_semantic.return_value = [
            WorkflowMatch("#V#textless_candidate", "Textless candidate", relevance_score=0.86)
        ]
        mock_vontology.return_value = []
        mock_name_fallback.return_value = []
        mock_classify.return_value = (True, EXECUTABILITY_EXECUTABLE_NOW, None)
        mock_has_authoritative_text.return_value = False
        mock_enrich.side_effect = lambda matches: matches

        result = discover_workflows("Represent the uploaded paper now", max_results=1)

        assert len(result.matches) == 1
        assert result.matches[0].routing_eligible is False
        assert (
            result.matches[0].routing_exclusion_reason
            == ROUTING_EXCLUSION_MISSING_AUTHORITATIVE_PURPOSE
        )
        assert result.routing_matches == []

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch("src.backend.services.workflow_discovery_service._search_workflows_name_fallback")
    @patch("src.backend.services.workflow_discovery_service._search_workflows_vontology")
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    def test_name_fallback_runs_even_when_semantic_search_returns_candidates(
        self,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_name_fallback: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_semantic.return_value = [
            WorkflowMatch("#V#semantic_candidate", "Semantic candidate", relevance_score=0.81)
        ]
        mock_vontology.return_value = []
        mock_name_fallback.return_value = []
        mock_enrich.side_effect = lambda matches: matches

        discover_workflows("Represent the uploaded paper now", max_results=1)

        assert mock_name_fallback.called is True

    @patch("src.backend.services.workflow_discovery_service.discover_workflows")
    def test_catches_exceptions(self, mock_discover: MagicMock) -> None:
        """Should catch exceptions and return None."""
        mock_discover.side_effect = Exception("Unexpected error")
        result = discover_workflows_for_turn("test query input")
        assert result is None


class TestWorkflowTypeIds:
    """Validate workflow type constants."""

    def test_contains_expected_types(self) -> None:
        """WORKFLOW_TYPE_IDS should contain the expected workflow types."""
        assert "#V#ai_workflow" in WORKFLOW_TYPE_IDS
        assert "#V#llm_workflow" in WORKFLOW_TYPE_IDS
        assert "#V#workflow" in WORKFLOW_TYPE_IDS
        assert "#V#durable_workflow" in WORKFLOW_TYPE_IDS

    def test_defaults_are_reasonable(self) -> None:
        """Default constants should have reasonable values."""
        assert DEFAULT_RELEVANCE_THRESHOLD == 0.70
        assert DEFAULT_MAX_RESULTS == 3


def test_invalidate_workflow_discovery_executability_caches_clears_lru_state() -> None:
    _classify_workflow_concept_executability.cache_clear()
    _is_executable_workflow_concept.cache_clear()

    with patch(
        "src.backend.workflows.vontology_loader.build_workflow_process_graph",
        return_value=(None, ["workflow_has_no_steps"]),
    ), patch(
        "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology",
        return_value=None,
    ):
        _classify_workflow_concept_executability("#V#wf_cache_probe")
        _is_executable_workflow_concept("#V#wf_cache_probe")

    assert _classify_workflow_concept_executability.cache_info().currsize > 0
    assert _is_executable_workflow_concept.cache_info().currsize > 0

    invalidate_workflow_discovery_executability_caches()

    assert _classify_workflow_concept_executability.cache_info().currsize == 0
    assert _is_executable_workflow_concept.cache_info().currsize == 0


def test_classify_workflow_treats_partial_vacuous_steps_as_non_executable() -> None:
    graph = {
        "workflow_id": "#V#wf_vacancy",
        "initial_step": "#V#identify",
        "steps": [
            {
                "step_id": "#V#identify",
                "name": "identify",
                "invokes_action": None,
                "preconditions": [],
                "effects": [],
                "reads_variables": [],
                "writes_variables": [],
            },
            {
                "step_id": "#V#final",
                "name": "final",
                "invokes_action": "tool.finished",
            },
        ],
        "edges": [],
        "warnings": [],
    }

    with patch(
        "src.backend.workflows.vontology_loader.build_workflow_process_graph",
        return_value=(graph, []),
    ), patch(
        "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology",
        return_value=object(),
    ):
        is_executable, reason, detail = _classify_workflow_concept_executability(
            "#V#wf_vacancy"
        )

    assert is_executable is False
    assert reason == EXECUTABILITY_WORKFLOW_STEP_PARTIALLY_VACUOUS
    assert "count=1" in str(detail)
    assert "total=2" in str(detail)
    assert "first_step=#V#identify" in str(detail)


def test_classify_workflow_treats_unpublished_draft_as_non_executable() -> None:
    _classify_workflow_concept_executability.cache_clear()

    with patch(
        "src.backend.workflows.vontology_loader.resolve_workflow_publication_lifecycle",
        return_value=(
            {"phase": "validated", "published": False},
            "concept_data",
        ),
    ):
        is_executable, reason, detail = _classify_workflow_concept_executability(
            "#V#wf_draft"
        )

    assert is_executable is False
    assert reason == EXECUTABILITY_DRAFT_NOT_PUBLISHED
    assert detail == "workflow_not_published:phase=validated:source=concept_data"


def test_classify_workflow_treats_completely_vacuous_steps_as_non_executable() -> None:
    graph = {
        "workflow_id": "#V#wf_vacancy_full",
        "initial_step": "#V#step_one",
        "steps": [
            {
                "step_id": "#V#step_one",
                "name": "step one",
                "invokes_action": None,
                "preconditions": [],
                "effects": [],
                "reads_variables": [],
                "writes_variables": [],
            },
            {
                "step_id": "#V#step_two",
                "name": "step two",
                "invokes_action": None,
                "preconditions": [],
                "effects": [],
                "reads_variables": [],
                "writes_variables": [],
            },
        ],
        "edges": [
            {"from": "#V#step_one", "to": "#V#step_two", "predicate": "next_step"},
            {"from": "#V#step_two", "to": "#V#step_one", "predicate": "retry_step"},
        ],
        "warnings": [],
    }

    with patch(
        "src.backend.workflows.vontology_loader.build_workflow_process_graph",
        return_value=(graph, []),
    ), patch(
        "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology",
        return_value=object(),
    ):
        is_executable, reason, detail = _classify_workflow_concept_executability(
            "#V#wf_vacancy_full"
        )

    assert is_executable is False
    assert reason == EXECUTABILITY_WORKFLOW_STEP_COMPLETELY_VACUOUS
    assert "count=2" in str(detail)
    assert "total=2" in str(detail)
    assert "first_step=#V#step_one" in str(detail)


def test_classify_workflow_uses_registry_fallback_for_built_in_workflow() -> None:
    _classify_workflow_concept_executability.cache_clear()
    fake_definition = SimpleNamespace(
        initial_state="start",
        states={"start": object()},
    )
    fake_registration = SimpleNamespace(
        source="built_in",
        definition=fake_definition,
    )
    fake_registry = SimpleNamespace(
        get_registration=lambda workflow_id: fake_registration,
        get=lambda workflow_id: None,
    )

    with patch(
        "src.backend.workflows.vontology_loader.build_workflow_process_graph",
        return_value=(None, ["workflow_concept_not_found"]),
    ), patch(
        "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology",
        return_value=None,
    ), patch(
        "src.backend.workflows.durable.registry_factory.build_durable_workflow_registry_read_only",
        return_value=fake_registry,
    ):
        is_executable, reason, detail = _classify_workflow_concept_executability(
            "#V#chat_assistant_workflow"
        )

    assert is_executable is True
    assert reason == EXECUTABILITY_EXECUTABLE_NOW
    assert detail is None


def test_classify_workflow_keeps_vontology_source_graph_authoritative() -> None:
    _classify_workflow_concept_executability.cache_clear()
    fake_definition = SimpleNamespace(
        initial_state="start",
        states={"start": object()},
    )
    fake_registration = SimpleNamespace(
        source="vontology",
        definition=fake_definition,
    )
    fake_registry = SimpleNamespace(
        get_registration=lambda workflow_id: fake_registration,
        get=lambda workflow_id: None,
    )

    with patch(
        "src.backend.workflows.vontology_loader.build_workflow_process_graph",
        return_value=(None, ["workflow_concept_not_found"]),
    ), patch(
        "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology",
        return_value=None,
    ), patch(
        "src.backend.workflows.durable.registry_factory.build_durable_workflow_registry_read_only",
        return_value=fake_registry,
    ):
        is_executable, reason, detail = _classify_workflow_concept_executability(
            "#V#vontology_workflow_without_graph"
        )

    assert is_executable is False
    assert reason == EXECUTABILITY_GRAPH_INCOMPLETE
    assert detail == "workflow_concept_not_found"
