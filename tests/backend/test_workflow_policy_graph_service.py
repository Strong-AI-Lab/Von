"""Tests for workflow policy graph resolution service (JVNAUTOSCI-998)."""

from __future__ import annotations

from unittest.mock import patch


def test_resolve_policy_from_graph_returns_none_when_policy_not_found():
    """Graph resolver returns None when policy concept doesn't exist."""
    from src.backend.services.workflow_policy_graph_service import (
        resolve_policy_from_graph,
    )

    with patch(
        "src.backend.services.workflow_policy_graph_service.ConceptsRepository"
    ) as mock_repo:
        mock_repo.find_one.return_value = None
        result = resolve_policy_from_graph("#V#nonexistent_policy")
        assert result is None


def test_resolve_policy_from_graph_returns_none_when_no_configs():
    """Graph resolver returns None when no stage configs are linked."""
    from src.backend.services.workflow_policy_graph_service import (
        resolve_policy_from_graph,
    )

    with patch(
        "src.backend.services.workflow_policy_graph_service.ConceptsRepository"
    ) as mock_repo:
        mock_repo.find_one.return_value = {
            "concept_id": "#V#test_policy",
            "relationships": {},
        }
        result = resolve_policy_from_graph("#V#test_policy")
        assert result is None


def test_resolve_policy_from_graph_resolves_stages():
    """Graph resolver builds stage dict from linked configs."""
    from src.backend.services.workflow_policy_graph_service import (
        resolve_policy_from_graph,
    )

    # Mock the ConceptsRepository
    def mock_find_one(query):
        cid = query.get("concept_id")
        if cid == "#V#test_policy":
            return {
                "concept_id": "#V#test_policy",
                "relationships": {
                    "#V#has_stage_configuration": ["#V#test_planner_config"],
                },
            }
        if cid == "#V#test_planner_config":
            return {
                "concept_id": "#V#test_planner_config",
                "relationships": {
                    "#V#applies_to_workflow_stage": ["#V#planner_stage"],
                },
            }
        return None

    # Mock text value retrieval
    def mock_get_texts(concept_id, predicate=None, limit=1):
        if concept_id == "#V#test_planner_config":
            if predicate == "#V#has_primary_model":
                return [{"text": "active_llm"}]
            if predicate == "#V#has_fallback_model":
                return [{"text": "ollama:granite3.3:2b"}]
            if predicate == "#V#has_local_only_constraint":
                return []
        if concept_id == "#V#test_policy":
            if predicate == "#V#has_max_fallback_hops":
                return [{"text": "2"}]
        return []

    with patch(
        "src.backend.services.workflow_policy_graph_service.ConceptsRepository"
    ) as mock_repo, patch(
        "src.backend.services.workflow_policy_graph_service.get_texts_for_concept",
        side_effect=mock_get_texts,
    ):
        mock_repo.find_one.side_effect = mock_find_one

        result = resolve_policy_from_graph("#V#test_policy")

        assert result is not None
        assert result["policy_id"] == "#V#test_policy"
        assert "planner" in result["stages"]
        assert result["stages"]["planner"]["primary"] == "active_llm"
        assert result["stages"]["planner"]["fallback"] == ["ollama:granite3.3:2b"]
        assert result["constraints"]["max_fallback_hops"] == 2


def test_compare_policy_json_vs_graph_reports_match():
    """Comparison reports match when JSON and graph are identical."""
    from src.backend.services.workflow_policy_graph_service import (
        compare_policy_json_vs_graph,
    )
    import json

    json_policy = {
        "stages": {
            "planner": {
                "primary": "active_llm",
                "fallback": ["ollama:granite3.3:2b"],
                "constraints": {"local_only": False},
            }
        },
        "constraints": {
            "local_only_stages": [],
            "max_fallback_hops": 2,
        },
    }

    graph_policy = {
        "policy_id": "#V#test_policy",
        "stages": {
            "planner": {
                "primary": "active_llm",
                "fallback": ["ollama:granite3.3:2b"],
                "constraints": {"local_only": False},
            }
        },
        "constraints": {
            "local_only_stages": [],
            "max_fallback_hops": 2,
        },
    }

    with patch(
        "src.backend.services.workflow_policy_graph_service._get_text_value"
    ) as mock_get_text, patch(
        "src.backend.services.workflow_policy_graph_service.resolve_policy_from_graph"
    ) as mock_resolve:
        mock_get_text.return_value = json.dumps(json_policy)
        mock_resolve.return_value = graph_policy

        report = compare_policy_json_vs_graph("#V#test_policy")

        assert report["status"] == "match"
        assert report["json_available"]
        assert report["graph_available"]
        assert not report["mismatches"]
        assert "planner" in report["matching_stages"]


def test_compare_policy_json_vs_graph_reports_mismatch():
    """Comparison reports mismatch when JSON and graph differ."""
    from src.backend.services.workflow_policy_graph_service import (
        compare_policy_json_vs_graph,
    )
    import json

    json_policy = {
        "stages": {
            "planner": {
                "primary": "active_llm",
                "fallback": [],
                "constraints": {"local_only": False},
            }
        },
        "constraints": {"local_only_stages": [], "max_fallback_hops": 2},
    }

    graph_policy = {
        "policy_id": "#V#test_policy",
        "stages": {
            "planner": {
                "primary": "ollama:granite3.3:2b",  # Different!
                "fallback": [],
                "constraints": {"local_only": False},
            }
        },
        "constraints": {"local_only_stages": [], "max_fallback_hops": 2},
    }

    with patch(
        "src.backend.services.workflow_policy_graph_service._get_text_value"
    ) as mock_get_text, patch(
        "src.backend.services.workflow_policy_graph_service.resolve_policy_from_graph"
    ) as mock_resolve:
        mock_get_text.return_value = json.dumps(json_policy)
        mock_resolve.return_value = graph_policy

        report = compare_policy_json_vs_graph("#V#test_policy")

        assert report["status"] == "mismatch"
        assert len(report["mismatches"]) > 0
