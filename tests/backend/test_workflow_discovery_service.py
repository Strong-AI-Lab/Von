"""Tests for workflow discovery service (JVNAUTOSCI-1076).

Validates workflow discovery during conversation turns including:
- WorkflowMatch and WorkflowDiscoveryResult dataclasses
- Semantic and Vontology search integration
- Relevance filtering and deduplication
- discover_workflows_for_turn convenience wrapper
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# Enable mock DB for tests
os.environ["VON_USE_MOCK_DB"] = "1"

from src.backend.services.workflow_discovery_service import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_RELEVANCE_THRESHOLD,
    WORKFLOW_TYPE_IDS,
    WorkflowDiscoveryResult,
    WorkflowMatch,
    _deduplicate_and_rank,
    _get_workflow_description,
    _get_workflow_name,
    discover_workflows,
    discover_workflows_for_turn,
)


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
            threshold=0.7,
            errors=["minor warning"],
        )
        output = result.to_dict()

        assert len(output["matches"]) == 2
        assert output["search_time_ms"] == 123.46  # Rounded to 2 decimal places
        assert output["query"] == "test query"
        assert output["threshold"] == 0.7
        assert output["match_count"] == 2
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
        assert "#V#llm_workflow" in WORKFLOW_TYPE_IDS
        assert "#V#workflow" in WORKFLOW_TYPE_IDS
        assert "#V#durable_workflow" in WORKFLOW_TYPE_IDS

    def test_defaults_are_reasonable(self) -> None:
        """Default constants should have reasonable values."""
        assert DEFAULT_RELEVANCE_THRESHOLD == 0.70
        assert DEFAULT_MAX_RESULTS == 3
