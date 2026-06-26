"""Tests for workflow discovery service (JVNAUTOSCI-1076).

Validates workflow discovery during conversation turns including:
- WorkflowMatch and WorkflowDiscoveryResult dataclasses
- Semantic and Vontology search integration
- Relevance filtering and deduplication
- discover_workflows_for_turn convenience wrapper
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.backend.services.workflow_discovery_service import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_RELEVANCE_THRESHOLD,
    DISCOVERY_CAPABILITY_INDEX_WAIT_TIMEOUT_FRACTION,
    DISCOVERY_BLOCKER_BUDGET_EXHAUSTED_NO_CANDIDATES,
    EXECUTABILITY_DRAFT_NOT_PUBLISHED,
    EXECUTABILITY_EXECUTABLE_NOW,
    EXECUTABILITY_GRAPH_INCOMPLETE,
    EXECUTABILITY_WORKFLOW_STEP_COMPLETELY_VACUOUS,
    EXECUTABILITY_WORKFLOW_STEP_PARTIALLY_VACUOUS,
    EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT,
    ROUTING_EXCLUSION_MISSING_AUTHORITATIVE_PURPOSE,
    ROUTING_EXCLUSION_EXPLICITLY_DISABLED,
    ROUTING_READINESS_WORKFLOW_PRESENT_MISSING_AUTHORITATIVE_ROUTING_TEXT,
    WORKFLOW_TYPE_IDS,
    WorkflowDiscoveryResult,
    WorkflowMatch,
    _agent_test_registry_capability_search_should_block,
    _annotate_and_rank_candidates,
    _classify_workflow_concept_executability,
    _deduplicate_and_rank,
    _enrich_workflow_matches,
    _build_expected_outcome_contract_projection,
    _get_workflow_description,
    _get_workflow_name,
    _is_executable_workflow_concept,
    _lifecycle_allows_routing,
    discover_workflows,
    discover_workflows_for_turn,
    invalidate_workflow_discovery_executability_caches,
)
from src.backend.workflows.turn_expected_outcome_contract import (
    TurnExpectedOutcomeContract,
)


@pytest.fixture(autouse=True)
def _use_mock_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable mock DB for workflow discovery tests, scoped per-test to avoid env leakage."""
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")


@pytest.fixture(autouse=True)
def _stub_capability_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit discovery tests focused on discovery logic, not registry rebuilds."""
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.get_workflow_capability_index_runtime_state",
        lambda: {
            "ready": True,
            "build_in_progress": False,
            "last_error": None,
        },
    )


def test_agent_test_registry_capability_search_blocks_for_local_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")

    assert _agent_test_registry_capability_search_should_block(
        workflow_registry=object()
    )
    assert not _agent_test_registry_capability_search_should_block(
        workflow_registry=None
    )

    monkeypatch.delenv("VON_AGENT_TEST_INSTANCE", raising=False)

    assert not _agent_test_registry_capability_search_should_block(
        workflow_registry=object()
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


def test_lifecycle_allows_published_workflow_with_pending_review_proposal() -> None:
    allowed, reason = _lifecycle_allows_routing(
        {
            "schema_version": "workflow_publication_lifecycle.v1",
            "phase": "published",
            "published": True,
            "routing_eligible": True,
            "review_state": "pending_review",
            "approval_required": True,
            "rollout_state": "proposal_pending_review",
            "proposal_id": "proposal-123",
        }
    )

    assert allowed is True
    assert reason is None


@pytest.mark.parametrize(
    "lifecycle",
    [
        {
            "schema_version": "workflow_publication_lifecycle.v1",
            "phase": "published",
            "published": True,
            "routing_eligible": False,
            "review_state": "pending_review",
        },
        {
            "schema_version": "workflow_publication_lifecycle.v1",
            "phase": "draft",
            "published": False,
            "routing_eligible": True,
            "review_state": "pending_review",
        },
        {
            "schema_version": "workflow_publication_lifecycle.v1",
            "phase": "published",
            "published": True,
            "routing_eligible": True,
            "rollout_state": "superseded",
        },
    ],
)
def test_lifecycle_blocks_explicitly_disabled_or_non_current_workflow(
    lifecycle: dict[str, object],
) -> None:
    allowed, reason = _lifecycle_allows_routing(lifecycle)

    assert allowed is False
    assert reason == ROUTING_EXCLUSION_EXPLICITLY_DISABLED


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
        assert result.match_absence_reason is None

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
            allow_non_executable=True,
            match_absence_reason="capability_index_build_in_progress",
            timeout_budget_seconds=2.5,
            budget_exhausted=True,
            budget_exhaustion_stage="semantic_search",
            budget_exhaustion_detail="semantic_search timed out after 2.500s",
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
        assert output["allow_non_executable"] is True
        assert output["match_absence_reason"] == "capability_index_build_in_progress"
        assert output["timeout_budget_seconds"] == 2.5
        assert output["budget_exhausted"] is True
        assert output["budget_exhaustion_stage"] == "semantic_search"
        assert (
            output["budget_exhaustion_detail"]
            == "semantic_search timed out after 2.500s"
        )
        assert output["errors"] == ["minor warning"]

    def test_to_dict_errors_none_when_empty(self) -> None:
        """to_dict should return None for errors when list is empty."""
        result = WorkflowDiscoveryResult(errors=[])
        assert result.to_dict()["errors"] is None


class TestGetWorkflowDescription:
    """Unit tests for _get_workflow_description helper."""

    def test_prefers_text_relations_over_top_level_description(self) -> None:
        """Canonical relation text should outrank convenience fallback fields."""
        concept_doc = {
            "text_relations": [
                {"predicate": "hasDescription", "text": "Authoritative description"}
            ],
            "description": "Fallback description",
        }
        assert _get_workflow_description(concept_doc) == "Authoritative description"

    def test_extracts_from_top_level_description(self) -> None:
        """Should extract description from the convenience description field."""
        concept_doc = {"description": "Workflow description"}
        assert _get_workflow_description(concept_doc) == "Workflow description"

    def test_extracts_from_attributes_description(self) -> None:
        """Should extract from attributes.description when needed."""
        concept_doc = {"attributes": {"description": "Workflow attributes description"}}
        assert (
            _get_workflow_description(concept_doc) == "Workflow attributes description"
        )

    def test_returns_none_when_no_description(self) -> None:
        """Should return None when no description found."""
        concept_doc = {"concept_id": "#V#test"}
        assert _get_workflow_description(concept_doc) is None

    def test_ignores_metadata_description_only_payload(self) -> None:
        """Deprecated metadata.description should not drive workflow discovery."""
        concept_doc = {"metadata": {"description": "Deprecated description"}}
        assert _get_workflow_description(concept_doc) is None

    def test_strips_whitespace(self) -> None:
        """Should strip whitespace from description."""
        concept_doc = {"description": "  trimmed  "}
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


def test_enrich_workflow_matches_prefers_authoritative_vontology_description() -> None:
    matches = [WorkflowMatch("#V#demo_workflow", "Unknown", relevance_score=0.8)]

    with (
        patch(
            "src.backend.workflows.vontology_loader.batch_fetch_workflow_purposes",
            return_value={"#V#demo_workflow": "Authoritative workflow description"},
        ),
        patch(
            "src.backend.db.repositories.concepts_repository.ConceptsRepository.find",
            return_value=[
                {
                    "concept_id": "#V#demo_workflow",
                    "name": "Demo Workflow",
                    "names": [],
                }
            ],
        ),
    ):
        enriched = _enrich_workflow_matches(matches)

    assert enriched[0].description == "Authoritative workflow description"
    assert enriched[0].name == "Demo Workflow"


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
        mock_has_authoritative_text.side_effect = lambda concept_id: (
            concept_id == "#V#wf_exec"
        )

        with patch(
            "src.backend.services.workflow_discovery_service._enrich_workflow_matches",
            side_effect=lambda x: x,
        ):
            result = discover_workflows("test query", max_results=3)

        assert len(result.matches) == 3
        reasons = {m.concept_id: m.executability_reason for m in result.matches}
        assert reasons["#V#wf_exec"] == EXECUTABILITY_EXECUTABLE_NOW
        assert reasons["#V#wf_graph"] == EXECUTABILITY_GRAPH_INCOMPLETE
        assert reasons["#V#wf_design"] == EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT
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

    def test_contract_projection_uses_structured_fields_without_domain_policy(
        self,
    ) -> None:
        projection = _build_expected_outcome_contract_projection(
            {
                "schema_version": "turn_expected_outcome_contract.v1",
                "fields": {
                    "summary": "Create a represented metadata record.",
                    "grounding_requirement": "Use authoritative represented evidence.",
                    "selector_guidance": (
                        "Prefer the exact workflow named in the contract."
                    ),
                },
                "required_tools": ["workflow_execute"],
                "required_actions": ["metadata.verify_representation"],
                "target_workflow_id": "#V#generic_metadata_representation_workflow",
                "target_type_ids": ["#V#metadata_record"],
            }
        )

        assert projection["schema_version"].endswith(".v1")
        assert "summary" in projection["fields_used"]
        assert "required_tools" in projection["fields_used"]
        assert "required_actions" in projection["fields_used"]
        assert projection["workflow_concept_ids"] == [
            "#V#generic_metadata_representation_workflow"
        ]
        assert "metadata.verify_representation" in projection["text"]

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
        return_value=True,
    )
    @patch(
        "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability",
        return_value=(True, EXECUTABILITY_EXECUTABLE_NOW, None),
    )
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities"
    )
    def test_structured_contract_exact_workflow_id_promotes_candidate_before_slow_search(
        self,
        mock_capability: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_classify: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_capability.side_effect = AssertionError(
            "exact represented workflow contract should resolve before retrieval"
        )
        mock_semantic.side_effect = AssertionError(
            "semantic search should not run for exact represented workflow contract"
        )
        mock_vontology.side_effect = AssertionError(
            "vontology search should not run for exact represented workflow contract"
        )
        mock_enrich.side_effect = lambda matches: matches
        registry = SimpleNamespace(
            all_workflow_ids=lambda: ["#V#generic_metadata_representation_workflow"],
            peek_registration=lambda workflow_id: SimpleNamespace(
                workflow_id=workflow_id,
                purpose="Represent metadata records through an authored workflow.",
                source="vontology",
            ),
        )

        result = discover_workflows(
            "Represent metadata for #V#benchmark_report.",
            max_results=1,
            timeout_seconds=0.01,
            workflow_registry=registry,
            expected_outcome_contract={
                "schema_version": "turn_expected_outcome_contract.v1",
                "fields": {
                    "summary": "Represent metadata for the target concept.",
                },
                "required_tools": ["workflow_execute"],
                "target_workflow_id": "#V#generic_metadata_representation_workflow",
                "target_type_ids": ["#V#metadata_record"],
            },
        )

        assert result.search_sources == ["contract_direct_workflow_resolution"]
        assert [match.concept_id for match in result.matches] == [
            "#V#generic_metadata_representation_workflow"
        ]
        assert [match.concept_id for match in result.routing_matches or []] == [
            "#V#generic_metadata_representation_workflow"
        ]
        assert result.contract_projection is not None
        assert result.contract_projection["workflow_concept_ids"] == [
            "#V#generic_metadata_representation_workflow"
        ]
        assert result.routing_readiness_diagnostics[0]["status"] == (
            "workflow_present_routing_ready"
        )
        assert any(
            timing.get("stage") == "contract_direct_workflow_resolution"
            and timing.get("match_count") == 1
            for timing in result.stage_timings
        )


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
    def test_returns_structured_empty_payload_when_no_matches(
        self, mock_discover: MagicMock
    ) -> None:
        """Wrapper should preserve an attempted zero-match discovery result."""
        mock_discover.return_value = WorkflowDiscoveryResult(
            matches=[],
            match_absence_reason="capability_index_build_in_progress",
            timeout_budget_seconds=2.0,
        )
        result = discover_workflows_for_turn("test query input")
        assert result is not None
        assert result["match_count"] == 0
        assert result["candidate_count"] == 0
        assert result["matches"] == []
        assert result["candidates"] == []
        assert result["match_absence_reason"] == "capability_index_build_in_progress"
        assert result["timeout_budget_seconds"] == 2.0

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
            timeout_budget_seconds=1.75,
        )
        result = discover_workflows_for_turn("test query input")

        assert result is not None
        assert result["match_count"] == 1
        assert len(result["matches"]) == 1
        assert result["requested_query"] == "test query input"
        assert result["timeout_budget_seconds"] == 1.75

    @patch("src.backend.services.workflow_discovery_service.discover_workflows")
    def test_passes_timeout_override_through_to_discovery(
        self,
        mock_discover: MagicMock,
    ) -> None:
        mock_discover.return_value = WorkflowDiscoveryResult()

        discover_workflows_for_turn(
            "test query input",
            timeout_seconds=2.25,
        )

        assert mock_discover.call_args is not None
        assert mock_discover.call_args.kwargs["timeout_seconds"] == 2.25

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service.build_file_copy_typing_context"
    )
    def test_augments_discovery_query_with_typed_file_copy_context(
        self,
        mock_typing_context: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
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
        mock_enrich.side_effect = lambda matches: matches

        result = discover_workflows(
            "Fully represent #V#uploaded_file_copy_abc123 now.",
            max_results=3,
        )

        semantic_query = mock_semantic.call_args.args[0]
        assert "Artefact typing context:" in semantic_query
        assert "route_hint=scholarly" in semantic_query
        assert "types=Scholarly paper file copy, PDF file copy" in semantic_query
        assert mock_vontology.call_args.args[0] == semantic_query
        assert result.search_sources == ["capability_index", "semantic", "vontology"]
        assert "route_hint=scholarly" in result.query

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._has_authoritative_routing_text"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    def test_textless_semantic_match_is_visible_but_not_routing_eligible(
        self,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_classify: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_semantic.return_value = [
            WorkflowMatch(
                "#V#textless_candidate", "Textless candidate", relevance_score=0.86
            )
        ]
        mock_vontology.return_value = []
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
        assert result.matches[0].routing_readiness_status == (
            ROUTING_READINESS_WORKFLOW_PRESENT_MISSING_AUTHORITATIVE_ROUTING_TEXT
        )
        assert result.routing_readiness_diagnostics[0]["status"] == (
            ROUTING_READINESS_WORKFLOW_PRESENT_MISSING_AUTHORITATIVE_ROUTING_TEXT
        )
        assert result.routing_matches == []

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
        return_value=True,
    )
    @patch(
        "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability",
        return_value=(True, EXECUTABILITY_EXECUTABLE_NOW, None),
    )
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service.get_workflow_capability_index_runtime_state"
    )
    def test_capability_index_build_in_progress_falls_back_to_secondary_search(
        self,
        mock_capability_state: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_classify: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_capability_state.return_value = {
            "ready": False,
            "build_in_progress": True,
            "last_error": None,
        }
        mock_semantic.return_value = [
            WorkflowMatch(
                "#V#semantic_candidate", "Semantic candidate", relevance_score=0.81
            )
        ]
        mock_vontology.return_value = []
        mock_enrich.side_effect = lambda matches: matches

        result = discover_workflows("Represent the uploaded paper now", max_results=1)

        assert result.search_sources == ["capability_index", "semantic", "vontology"]
        assert [match.concept_id for match in result.matches] == [
            "#V#semantic_candidate"
        ]
        assert [match.concept_id for match in result.routing_matches or []] == [
            "#V#semantic_candidate"
        ]
        assert result.match_absence_reason is None
        assert "capability_index_build_in_progress" in result.errors
        assert mock_semantic.called is True
        assert mock_vontology.called is True
        assert any(
            timing.get("stage") == "contract_direct_workflow_resolution"
            and timing.get("match_count") == 0
            for timing in result.stage_timings
        )

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities"
    )
    @patch(
        "src.backend.services.workflow_discovery_service.get_workflow_capability_index_runtime_state"
    )
    def test_records_capability_index_timeout_cause_and_uses_bounded_wait(
        self,
        mock_capability_state: MagicMock,
        mock_capability: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_capability.return_value = []
        mock_capability_state.return_value = {
            "ready": False,
            "build_in_progress": True,
            "last_error": None,
        }
        mock_semantic.return_value = []
        mock_vontology.return_value = []
        mock_enrich.side_effect = lambda matches: matches

        result = discover_workflows(
            "Represent the uploaded paper now",
            max_results=1,
            timeout_seconds=1.0,
        )

        assert mock_capability.call_args.kwargs["max_wait_seconds"] == pytest.approx(
            1.0 * DISCOVERY_CAPABILITY_INDEX_WAIT_TIMEOUT_FRACTION
        )
        assert "capability_index_wait_timed_out" in result.errors
        assert "capability_index_build_in_progress" in result.errors
        assert (
            result.match_absence_reason
            == "capability_index_wait_timed_out_build_in_progress"
        )
        assert result.budget_exhausted is True
        assert result.budget_exhaustion_stage == "semantic_search"
        assert mock_semantic.called is False
        assert mock_vontology.called is False
        assert any(
            timing.get("stage") == "contract_direct_workflow_resolution"
            and timing.get("match_count") == 0
            for timing in result.stage_timings
        )
        assert any(
            timing.get("stage") == "semantic_search"
            and timing.get("status") == "skipped_budget_insufficient"
            for timing in result.stage_timings
        )
        assert any(
            timing.get("stage") == "vontology_search"
            and timing.get("status") == "skipped_budget_insufficient"
            for timing in result.stage_timings
        )

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service.resolve_workflow_capabilities_for_contract"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities"
    )
    @patch(
        "src.backend.services.workflow_discovery_service.get_workflow_capability_index_runtime_state"
    )
    def test_contract_capability_metadata_resolves_before_slow_secondary_search(
        self,
        mock_capability_state: MagicMock,
        mock_capability: MagicMock,
        mock_contract_capabilities: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_capability.return_value = []
        mock_capability_state.return_value = {
            "ready": False,
            "build_in_progress": True,
            "last_error": None,
        }
        mock_contract_capabilities.return_value = [
            SimpleNamespace(
                workflow_id="#V#general_mail_review_workflow",
                name="General Mail Review Workflow",
                description="Review mailbox messages through represented workflow actions.",
                relevance_score=0.83,
                source="contract_capability_metadata",
                metadata={
                    "routing_index_schema_version": (
                        "workflow_routing_index_entry.v1"
                    ),
                    "has_authoritative_routing_text": True,
                    "required_tools": ["gmail_list_messages"],
                    "workflow_action_ids": ["gmail_list_messages"],
                    "compact_executability": {
                        "schema_version": "workflow_compact_executability.v1",
                        "source": "vontology_workflow_graph_shape",
                        "is_executable": True,
                        "reason": "compact_graph_present",
                        "has_initial_step": True,
                        "step_count": 4,
                    },
                    "publication_lifecycle": {
                        "schema_version": "workflow_publication_lifecycle.v1",
                        "phase": "published",
                        "published": True,
                        "routing_eligible": True,
                    },
                    "contract_capability_match": {
                        "schema_version": "workflow_contract_capability_match.v1",
                        "entry_source": "process_capability_entries",
                        "tool_surface_family_overlap": ["gmail"],
                    },
                },
            )
        ]
        mock_semantic.side_effect = AssertionError(
            "structural contract capability match should avoid slow secondary search"
        )
        mock_vontology.side_effect = AssertionError(
            "structural contract capability match should avoid slow secondary search"
        )
        mock_enrich.side_effect = lambda matches: matches

        result = discover_workflows(
            "List recent mailbox messages.",
            max_results=1,
            expected_outcome_contract={
                "schema_version": "turn_expected_outcome_contract.v1",
                "summary": "List recent mailbox messages.",
                "required_tools": ["gmail_list_profiles", "gmail_list_messages"],
            },
        )

        assert result.search_sources == [
            "capability_index",
            "contract_capability_metadata",
        ]
        assert [match.concept_id for match in result.matches] == [
            "#V#general_mail_review_workflow"
        ]
        assert [match.concept_id for match in result.routing_matches or []] == [
            "#V#general_mail_review_workflow"
        ]
        assert mock_semantic.called is False
        assert mock_vontology.called is False
        assert any(
            timing.get("stage") == "contract_capability_metadata_resolution"
            and timing.get("match_count") == 1
            and timing.get("sufficient") is True
            and timing.get("entry_source") == "process_capability_entries"
            for timing in result.stage_timings
        )

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
        return_value=True,
    )
    @patch(
        "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability",
        return_value=(True, EXECUTABILITY_EXECUTABLE_NOW, None),
    )
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities"
    )
    @patch(
        "src.backend.services.workflow_discovery_service.get_workflow_capability_index_runtime_state"
    )
    def test_workflow_execute_contract_resolves_from_registry_before_capability_index(
        self,
        mock_capability_state: MagicMock,
        mock_capability: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_classify: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_capability.return_value = []
        mock_capability_state.return_value = {
            "ready": False,
            "build_in_progress": True,
            "last_error": None,
        }
        mock_semantic.return_value = []
        mock_vontology.return_value = []
        mock_enrich.side_effect = lambda matches: matches
        registry = SimpleNamespace(
            all_workflow_ids=lambda: ["#V#arxiv_paper_representation_workflow"],
            peek_registration=lambda workflow_id: SimpleNamespace(
                workflow_id=workflow_id,
                purpose="Canonical arXiv wrapper workflow.",
                source="vontology",
            ),
        )

        result = discover_workflows(
            "https://arxiv.org/abs/2406.15341\n\n"
            "Turn-intent routing guidance:\n"
            "- Routing guidance: Route to the executable arXiv paper "
            "representation workflow.\n"
            "- Required tools: workflow_execute",
            max_results=1,
            workflow_registry=registry,
        )

        assert result.search_sources == ["contract_direct_workflow_resolution"]
        assert [match.concept_id for match in result.matches] == [
            "#V#arxiv_paper_representation_workflow"
        ]
        assert [match.concept_id for match in result.routing_matches or []] == [
            "#V#arxiv_paper_representation_workflow"
        ]
        assert mock_capability.called is False
        assert mock_semantic.called is False
        assert mock_vontology.called is False
        assert any(
            timing.get("stage") == "contract_direct_workflow_resolution"
            and timing.get("match_count") == 1
            for timing in result.stage_timings
        )

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
        return_value=True,
    )
    @patch(
        "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability",
        return_value=(True, EXECUTABILITY_EXECUTABLE_NOW, None),
    )
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities"
    )
    def test_contract_workflow_concept_ids_resolve_when_capability_index_cold(
        self,
        mock_capability: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_classify: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_capability.side_effect = AssertionError(
            "contract-named workflow should resolve before capability index"
        )
        mock_semantic.side_effect = AssertionError(
            "semantic search should not run when direct contract resolution succeeds"
        )
        mock_vontology.side_effect = AssertionError(
            "vontology search should not run when direct contract resolution succeeds"
        )
        mock_enrich.side_effect = lambda matches: matches
        registry = SimpleNamespace(
            all_workflow_ids=lambda: ["#V#arxiv_paper_representation_workflow"],
            peek_registration=lambda workflow_id: SimpleNamespace(
                workflow_id=workflow_id,
                purpose="Canonical arXiv wrapper workflow.",
                source="vontology",
            ),
        )
        contract = TurnExpectedOutcomeContract.from_mapping(
            {
                "expected_outcome_summary": "Represent the arXiv paper.",
                "required_tools": ["workflow_execute"],
                "target_workflow_id": "#V#arxiv_paper_representation_workflow",
            }
        ).to_state_payload()

        result = discover_workflows(
            "Yes, represent it.",
            max_results=1,
            workflow_registry=registry,
            expected_outcome_contract=contract,
            timeout_seconds=0.75,
        )

        assert result.search_sources == ["contract_direct_workflow_resolution"]
        assert [match.concept_id for match in result.routing_matches or []] == [
            "#V#arxiv_paper_representation_workflow"
        ]
        assert result.contract_projection is not None
        assert result.contract_projection["workflow_concept_ids"] == [
            "#V#arxiv_paper_representation_workflow"
        ]
        assert any(
            timing.get("stage") == "contract_direct_workflow_resolution"
            and timing.get("match_count") == 1
            and timing.get("workflow_concept_id_count") == 1
            and timing.get("sufficient") is True
            for timing in result.stage_timings
        )

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
        return_value=True,
    )
    @patch(
        "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability",
        return_value=(True, EXECUTABILITY_EXECUTABLE_NOW, None),
    )
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities"
    )
    def test_exact_workflow_display_name_resolves_before_cold_capability_index(
        self,
        mock_capability: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_classify: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_capability.side_effect = AssertionError(
            "exact workflow label should resolve before cold retrieval"
        )
        mock_semantic.side_effect = AssertionError(
            "semantic search should not run for an exact workflow label"
        )
        mock_vontology.side_effect = AssertionError(
            "vontology search should not run for an exact workflow label"
        )
        mock_enrich.side_effect = lambda matches: matches
        registry = SimpleNamespace(
            all_workflow_ids=lambda: [
                "#V#paper_ingestion_testing_workflow",
                "#V#tool_calling_workflow",
            ],
            peek_registration=lambda workflow_id: SimpleNamespace(
                workflow_id=workflow_id,
                purpose=(
                    "Run a represented paper-ingestion testing workflow."
                    if workflow_id == "#V#paper_ingestion_testing_workflow"
                    else "Generic tool-calling workflow."
                ),
                source="vontology",
            ),
        )

        result = discover_workflows(
            "Run the paper ingestion testing workflow on https://example.test/paper.",
            max_results=1,
            timeout_seconds=0.01,
            workflow_registry=registry,
            expected_outcome_contract={
                "selector_guidance": (
                    "Prefer the represented workflow; use the generic "
                    "tool-calling workflow only when no specialised workflow "
                    "is eligible."
                )
            },
        )

        assert result.search_sources == ["contract_direct_workflow_resolution"]
        assert [match.concept_id for match in result.matches] == [
            "#V#paper_ingestion_testing_workflow"
        ]
        assert [match.concept_id for match in result.routing_matches or []] == [
            "#V#paper_ingestion_testing_workflow"
        ]
        assert mock_capability.called is False
        assert mock_semantic.called is False
        assert mock_vontology.called is False
        assert any(
            timing.get("stage") == "contract_direct_workflow_resolution"
            and timing.get("match_count") == 1
            for timing in result.stage_timings
        )

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities"
    )
    @patch(
        "src.backend.services.workflow_discovery_service.get_workflow_capability_index_runtime_state"
    )
    def test_registry_workflow_discovery_uses_secondary_authoritative_searches_only(
        self,
        mock_capability_state: MagicMock,
        mock_capability: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_capability.return_value = []
        mock_capability_state.return_value = {
            "ready": True,
            "build_in_progress": False,
            "last_error": None,
        }
        mock_semantic.return_value = [
            WorkflowMatch(
                "#V#uploaded_file_representation_workflow",
                "Uploaded file representation workflow",
                relevance_score=0.81,
            )
        ]
        mock_vontology.return_value = []
        mock_enrich.side_effect = lambda matches: matches

        result = discover_workflows(
            "Run the uploaded file representation workflow",
            max_results=1,
            workflow_registry=SimpleNamespace(),
        )

        assert result.search_sources == [
            "capability_index",
            "semantic",
            "vontology",
        ]
        assert "registry_keyword_fallback" not in result.search_sources
        assert [match.concept_id for match in result.matches] == [
            "#V#uploaded_file_representation_workflow"
        ]
        assert mock_semantic.called is True
        assert mock_vontology.called is True

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities"
    )
    def test_capability_index_short_circuits_secondary_search_when_sufficient(
        self,
        mock_capability: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_capability.return_value = [
            WorkflowMatch("#V#wf1", "Workflow 1", relevance_score=0.95),
            WorkflowMatch("#V#wf2", "Workflow 2", relevance_score=0.92),
            WorkflowMatch("#V#wf3", "Workflow 3", relevance_score=0.9),
        ]
        mock_semantic.return_value = []
        mock_vontology.return_value = []
        mock_enrich.side_effect = lambda matches: matches

        with (
            patch(
                "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability",
                return_value=(True, EXECUTABILITY_EXECUTABLE_NOW, None),
            ),
            patch(
                "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
                return_value=True,
            ),
        ):
            result = discover_workflows(
                "Represent the uploaded paper now", max_results=3
            )

        assert len(result.matches) == 3
        assert result.search_sources == ["capability_index"]
        assert mock_semantic.called is False
        assert mock_vontology.called is False

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities"
    )
    @patch(
        "src.backend.services.workflow_discovery_service.get_workflow_capability_index_runtime_state"
    )
    def test_records_cooperative_budget_overrun_in_capability_index_search(
        self,
        mock_capability_state: MagicMock,
        mock_capability: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        def _blocking_capability(*_args, **_kwargs):
            time.sleep(0.2)
            return []

        mock_capability.side_effect = _blocking_capability
        mock_capability_state.return_value = {
            "ready": False,
            "build_in_progress": True,
            "last_error": None,
        }
        mock_semantic.return_value = []
        mock_vontology.return_value = []
        mock_enrich.side_effect = lambda matches: matches

        result = discover_workflows(
            "Represent the uploaded paper now",
            max_results=1,
            timeout_seconds=0.05,
        )

        assert result.timeout_budget_seconds == 0.05
        assert result.budget_exhausted is True
        assert result.budget_exhaustion_stage == "capability_index_search"
        assert "capability_index_search exceeded" in str(
            result.budget_exhaustion_detail
        )
        assert result.match_absence_reason == (
            "capability_index_wait_timed_out_build_in_progress"
        )
        assert mock_semantic.called is False
        assert mock_vontology.called is False

    @patch("src.backend.services.workflow_discovery_service._enrich_workflow_matches")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflows_vontology"
    )
    @patch("src.backend.services.workflow_discovery_service._search_workflows_semantic")
    @patch(
        "src.backend.services.workflow_discovery_service._search_workflow_capabilities"
    )
    @patch(
        "src.backend.services.workflow_discovery_service.get_workflow_capability_index_runtime_state"
    )
    def test_skips_blocking_semantic_search_when_budget_insufficient(
        self,
        mock_capability_state: MagicMock,
        mock_capability: MagicMock,
        mock_semantic: MagicMock,
        mock_vontology: MagicMock,
        mock_enrich: MagicMock,
    ) -> None:
        mock_capability.return_value = []
        mock_capability_state.return_value = {
            "ready": True,
            "build_in_progress": False,
            "last_error": None,
        }
        mock_semantic.side_effect = AssertionError(
            "semantic search should not run when too little budget remains"
        )
        mock_vontology.return_value = []
        mock_enrich.side_effect = lambda matches: matches

        result = discover_workflows(
            "Represent the uploaded paper now",
            max_results=1,
            timeout_seconds=0.06,
        )

        assert result.budget_exhausted is True
        assert result.budget_exhaustion_stage == "semantic_search"
        assert "skipped semantic_search" in str(result.budget_exhaustion_detail)
        assert result.blocker_reason == DISCOVERY_BLOCKER_BUDGET_EXHAUSTED_NO_CANDIDATES
        assert result.search_time_ms < 100.0
        assert result.timeout_budget_seconds == 0.06
        assert mock_semantic.called is False
        assert mock_vontology.called is False

    @patch(
        "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._has_authoritative_routing_text"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._resolve_workflow_routing_profile_data"
    )
    def test_annotation_stops_after_enough_routing_candidates(
        self,
        mock_routing_profile: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        matches = [
            WorkflowMatch("#V#wf1", "Workflow 1", relevance_score=0.95),
            WorkflowMatch("#V#wf2", "Workflow 2", relevance_score=0.93),
            WorkflowMatch("#V#wf3", "Workflow 3", relevance_score=0.91),
            WorkflowMatch("#V#wf4", "Workflow 4", relevance_score=0.89),
        ]
        mock_classify.return_value = (True, EXECUTABILITY_EXECUTABLE_NOW, None)
        mock_has_authoritative_text.return_value = True
        mock_routing_profile.return_value = (None, None)

        annotated = _annotate_and_rank_candidates(matches, max_results=2)

        assert len(annotated) == 2
        assert mock_classify.call_count == 2
        assert mock_has_authoritative_text.call_count == 2

    @patch(
        "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._has_authoritative_routing_text"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._resolve_workflow_routing_profile_data"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._resolve_workflow_publication_lifecycle_data"
    )
    def test_annotation_carries_routing_profile_into_discovery_payload(
        self,
        mock_lifecycle: MagicMock,
        mock_routing_profile: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        match = WorkflowMatch(
            "#V#authoring_workflow", "Authoring Workflow", relevance_score=0.95
        )
        mock_classify.return_value = (True, EXECUTABILITY_EXECUTABLE_NOW, None)
        mock_has_authoritative_text.return_value = True
        mock_lifecycle.return_value = (None, None)
        mock_routing_profile.return_value = (
            {
                "role": "authoring",
                "authoring_intent_required": True,
                "explicit_workflow_context_required": False,
                "prefer_existing_capability": True,
            },
            "text_relation:#V#hasWorkflowRoutingProfileJson",
        )

        annotated = _annotate_and_rank_candidates([match], max_results=1)

        assert len(annotated) == 1
        assert annotated[0].routing_profile == {
            "role": "authoring",
            "authoring_intent_required": True,
            "explicit_workflow_context_required": False,
            "prefer_existing_capability": True,
        }
        assert annotated[0].to_dict()["routing_profile"]["role"] == "authoring"

    def test_annotation_uses_routing_index_metadata_without_live_hydration(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        match = WorkflowMatch(
            "#V#compact_indexed_workflow",
            "Compact Indexed Workflow",
            description="Authoritative compact routing description.",
            relevance_score=0.96,
            match_source="capability_index",
            routing_index_metadata={
                "routing_index_schema_version": "workflow_routing_index_entry.v1",
                "has_authoritative_routing_text": True,
                "routing_profile": {
                    "role": "execution",
                    "authoring_intent_required": False,
                    "explicit_workflow_context_required": False,
                    "prefer_existing_capability": False,
                },
                "routing_profile_source": (
                    "text_relation:#V#hasWorkflowRoutingProfileJson"
                ),
                "publication_lifecycle": {
                    "schema_version": "workflow_publication_lifecycle.v1",
                    "phase": "published",
                    "published": True,
                    "routing_eligible": True,
                },
                "publication_lifecycle_source": (
                    "text_relation:#V#hasWorkflowLifecycleJson"
                ),
                "compact_executability": {
                    "schema_version": "workflow_compact_executability.v1",
                    "source": "vontology_workflow_graph_shape",
                    "is_executable": True,
                    "reason": "compact_graph_present",
                    "has_initial_step": True,
                    "step_count": 4,
                },
            },
        )

        monkeypatch.setattr(
            "src.backend.services.workflow_discovery_service._classify_workflow_candidate_executability",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError(
                    "compact routing index should avoid definition hydration"
                )
            ),
        )
        monkeypatch.setattr(
            "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("compact routing index should carry routing text status")
            ),
        )
        monkeypatch.setattr(
            "src.backend.services.workflow_discovery_service._resolve_workflow_routing_profile_data",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("compact routing index should carry routing profile")
            ),
        )
        monkeypatch.setattr(
            "src.backend.services.workflow_discovery_service._resolve_workflow_publication_lifecycle_data",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("compact routing index should carry lifecycle")
            ),
        )

        annotated = _annotate_and_rank_candidates([match], max_results=1)

        assert len(annotated) == 1
        assert annotated[0].is_executable is True
        assert annotated[0].executability_reason == EXECUTABILITY_EXECUTABLE_NOW
        assert annotated[0].routing_eligible is True
        assert annotated[0].routing_profile["role"] == "execution"
        assert "compact_routing_index" in str(annotated[0].executability_detail)

    @patch(
        "src.backend.services.workflow_discovery_service._classify_workflow_concept_executability"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._has_authoritative_routing_text"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._resolve_workflow_routing_profile_data"
    )
    @patch(
        "src.backend.services.workflow_discovery_service._resolve_workflow_publication_lifecycle_data"
    )
    def test_annotation_keeps_published_workflow_routable_while_proposal_pending_review(
        self,
        mock_lifecycle: MagicMock,
        mock_routing_profile: MagicMock,
        mock_has_authoritative_text: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        match = WorkflowMatch(
            "#V#arxiv_paper_representation_workflow",
            "Arxiv Paper Representation Workflow",
            relevance_score=0.95,
        )
        mock_classify.return_value = (True, EXECUTABILITY_EXECUTABLE_NOW, None)
        mock_has_authoritative_text.return_value = True
        mock_routing_profile.return_value = (
            {
                "role": "execution",
                "authoring_intent_required": False,
                "explicit_workflow_context_required": False,
                "prefer_existing_capability": False,
            },
            "text_relation:#V#hasWorkflowRoutingProfileJson",
        )
        mock_lifecycle.return_value = (
            {
                "schema_version": "workflow_publication_lifecycle.v1",
                "phase": "published",
                "published": True,
                "routing_eligible": True,
                "review_state": "pending_review",
                "approval_required": True,
                "rollout_state": "proposal_pending_review",
                "proposal_id": "c3c8f0d7-4888-4bce-85e1-89466d3694b0",
            },
            "text_relation:#V#hasWorkflowLifecycleJson",
        )

        annotated = _annotate_and_rank_candidates([match], max_results=1)

        assert len(annotated) == 1
        assert annotated[0].is_policy_safe is True
        assert annotated[0].routing_eligible is True
        assert annotated[0].routing_exclusion_reason is None

    @patch("src.backend.services.workflow_discovery_service.discover_workflows")
    def test_catches_exceptions(self, mock_discover: MagicMock) -> None:
        """Should catch exceptions and return an explicit failed-closed payload."""
        mock_discover.side_effect = Exception("Unexpected error")
        result = discover_workflows_for_turn("test query input")
        assert isinstance(result, dict)
        assert result["matches"] == []
        assert "workflow_discovery_for_turn_error: Unexpected error" in result["errors"]
        assert result["match_absence_reason"] == "workflow_discovery_for_turn_error"
        assert result["budget_exhausted"] is False

    @patch("src.backend.services.workflow_discovery_service.discover_workflows")
    def test_turn_wrapper_does_not_spawn_outer_timeout_worker(
        self,
        mock_discover: MagicMock,
    ) -> None:
        def _blocking_discovery(*_args, **_kwargs):
            time.sleep(0.05)
            return WorkflowDiscoveryResult(
                budget_exhausted=True,
                budget_exhaustion_stage="semantic_search",
                budget_exhaustion_detail="semantic_search exceeded remaining budget",
            )

        mock_discover.side_effect = _blocking_discovery

        result = discover_workflows_for_turn(
            "test query input",
            timeout_seconds=0.01,
        )

        assert isinstance(result, dict)
        assert result["matches"] == []
        assert result["budget_exhausted"] is True
        assert result["budget_exhaustion_stage"] == "semantic_search"
        assert mock_discover.call_count == 1


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

    with (
        patch(
            "src.backend.workflows.vontology_loader.build_workflow_process_graph",
            return_value=(None, ["workflow_has_no_steps"]),
        ),
        patch(
            "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology",
            return_value=None,
        ),
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

    with (
        patch(
            "src.backend.workflows.vontology_loader.build_workflow_process_graph",
            return_value=(graph, []),
        ),
        patch(
            "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology",
            return_value=object(),
        ),
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


def test_classify_workflow_treats_prompt_sentence_like_id_as_non_executable() -> None:
    _classify_workflow_concept_executability.cache_clear()

    is_executable, reason, detail = _classify_workflow_concept_executability(
        (
            "#V#the_enrichment_workflow_isn_t_the_right_one_we_need_a_new_"
            "vontology_search_workflow_i_suspect_manually_retrieve_the_"
            "v_timothy_pistotti_concept_and_then_look_at_its_types_and_"
            "relations_in_particular_ones_about_supervision_workflow"
        )
    )

    assert is_executable is False
    assert reason == EXECUTABILITY_NON_EXECUTABLE_DESIGN_ARTIFACT
    assert "workflow_id_invalid" in str(detail)
    assert "workflow_id_slug_too_long" in str(detail)


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

    with (
        patch(
            "src.backend.workflows.vontology_loader.build_workflow_process_graph",
            return_value=(graph, []),
        ),
        patch(
            "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology",
            return_value=object(),
        ),
    ):
        is_executable, reason, detail = _classify_workflow_concept_executability(
            "#V#wf_vacancy_full"
        )

    assert is_executable is False
    assert reason == EXECUTABILITY_WORKFLOW_STEP_COMPLETELY_VACUOUS
    assert "count=2" in str(detail)
    assert "total=2" in str(detail)
    assert "first_step=#V#step_one" in str(detail)


def test_classify_workflow_rejects_actionless_output_only_contract() -> None:
    _classify_workflow_concept_executability.cache_clear()
    graph = {
        "workflow_id": "#V#wf_output_only",
        "initial_step": "#V#step_one",
        "steps": [
            {
                "step_id": "#V#step_one",
                "name": "step one",
                "invokes_action": None,
                "preconditions": [],
                "effects": [],
                "reads_variables": [],
                "reads_context_keys": [],
                "writes_variables": [],
                "writes_context_keys": ["#V#workflow_context_key_validated_type_name"],
            }
        ],
        "edges": [],
        "warnings": [],
    }

    with (
        patch(
            "src.backend.workflows.vontology_loader.build_workflow_process_graph",
            return_value=(graph, []),
        ),
        patch(
            "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology",
            return_value=object(),
        ),
    ):
        is_executable, reason, detail = _classify_workflow_concept_executability(
            "#V#wf_output_only"
        )

    assert is_executable is False
    assert reason == EXECUTABILITY_WORKFLOW_STEP_COMPLETELY_VACUOUS
    assert "count=1" in str(detail)
    assert "total=1" in str(detail)
    assert "first_step=#V#step_one" in str(detail)


def test_classify_workflow_treats_student_lookup_subworkflow_as_executable() -> None:
    _classify_workflow_concept_executability.cache_clear()
    graph = {
        "workflow_id": "#V#student_supervision_lookup_workflow",
        "initial_step": "#V#find_student",
        "steps": [
            {
                "step_id": "#V#find_student",
                "name": "find student",
                "invokes_workflow": "#V#find_concepts_workflow",
                "preconditions": [],
                "effects": [],
                "reads_variables": [],
                "writes_variables": [],
                "reads_context_keys": ["student_name"],
                "writes_context_keys": ["student_concept_id"],
            },
            {
                "step_id": "#V#verify_supervision",
                "name": "verify supervision",
                "invokes_action": "tool.verify_supervision",
                "preconditions": [],
                "effects": [],
                "reads_variables": [],
                "writes_variables": [],
                "reads_context_keys": ["student_concept_id"],
                "writes_context_keys": ["student_supervision_verified"],
            },
        ],
        "edges": [
            {
                "from": "#V#find_student",
                "to": "#V#verify_supervision",
                "predicate": "next_step",
            }
        ],
        "warnings": [],
    }

    with (
        patch(
            "src.backend.workflows.vontology_loader.build_workflow_process_graph",
            return_value=(graph, []),
        ),
        patch(
            "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology",
            return_value=object(),
        ),
    ):
        is_executable, reason, detail = _classify_workflow_concept_executability(
            "#V#student_supervision_lookup_workflow"
        )

    assert is_executable is True
    assert reason == EXECUTABILITY_EXECUTABLE_NOW
    assert detail is None


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

    with (
        patch(
            "src.backend.workflows.vontology_loader.build_workflow_process_graph"
        ) as mock_build_graph,
        patch(
            "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology"
        ) as mock_load_definition,
        patch(
            "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
            return_value=fake_registry,
        ),
    ):
        is_executable, reason, detail = _classify_workflow_concept_executability(
            "#V#chat_assistant_workflow"
        )

    assert is_executable is True
    assert reason == EXECUTABILITY_EXECUTABLE_NOW
    assert detail is None
    mock_build_graph.assert_not_called()
    mock_load_definition.assert_not_called()


def test_annotation_reuses_provided_registry_for_built_in_workflow() -> None:
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
    match = WorkflowMatch(
        "#V#chat_assistant_workflow",
        "Chat Assistant Workflow",
        relevance_score=0.95,
    )

    with (
        patch(
            "src.backend.workflows.durable.registry_factory.build_durable_workflow_registry_read_only"
        ) as mock_build_registry,
        patch(
            "src.backend.services.workflow_discovery_service._has_authoritative_routing_text",
            return_value=True,
        ),
        patch(
            "src.backend.services.workflow_discovery_service._resolve_workflow_routing_profile_data",
            return_value=(None, None),
        ),
        patch(
            "src.backend.services.workflow_discovery_service._resolve_workflow_publication_lifecycle_data",
            return_value=(None, None),
        ),
    ):
        annotated = _annotate_and_rank_candidates(
            [match],
            max_results=1,
            workflow_registry=fake_registry,
        )

    assert len(annotated) == 1
    assert annotated[0].is_executable is True
    assert annotated[0].executability_reason == EXECUTABILITY_EXECUTABLE_NOW
    mock_build_registry.assert_not_called()


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

    with (
        patch(
            "src.backend.workflows.vontology_loader.build_workflow_process_graph",
            return_value=(None, ["workflow_concept_not_found"]),
        ),
        patch(
            "src.backend.workflows.vontology_loader.load_workflow_definition_from_vontology",
            return_value=None,
        ),
        patch(
            "src.backend.workflows.durable.registry_factory.build_durable_workflow_registry_read_only",
            return_value=fake_registry,
        ),
    ):
        is_executable, reason, detail = _classify_workflow_concept_executability(
            "#V#vontology_workflow_without_graph"
        )

    assert is_executable is False
    assert reason == EXECUTABILITY_GRAPH_INCOMPLETE
    assert detail == "workflow_concept_not_found"
