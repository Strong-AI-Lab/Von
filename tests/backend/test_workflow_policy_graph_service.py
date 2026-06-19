"""Tests for workflow policy graph resolution service.

JVNAUTOSCI-998 introduced the graph resolver; JVNAUTOSCI-2496 made it
fail-closed: the resolver applies represented policy, reports provenance,
and reports an explicitly incomplete payload instead of supplying missing
policy values (no silent ``active_llm`` primary, no silent fallback-hop
default, no Python stage table).
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.backend.services.workflow_policy_graph_service import (
    GRAPH_POLICY_COMPLETE,
    GRAPH_POLICY_INCOMPLETE,
    compare_policy_json_vs_graph,
    resolve_policy_from_graph,
)

_SERVICE = "src.backend.services.workflow_policy_graph_service"


def _policy_concept(config_ids):
    return {
        "concept_id": "#V#test_policy",
        "relationships": {"#V#has_stage_configuration": list(config_ids)},
    }


def _config_concept(config_id, stage_concept_id):
    return {
        "concept_id": config_id,
        "relationships": {"#V#applies_to_workflow_stage": [stage_concept_id]},
    }


def _batch_text_lookup_side_effect(single_lookup):
    def _side_effect(
        subject_concept_ids,
        *,
        predicate=None,
        predicates=None,
        limit_per_concept=50,
        **_kwargs,
    ):
        if predicate:
            predicate_values = [predicate]
        else:
            predicate_values = [
                item
                for item in (predicates or [])
                if isinstance(item, str) and item
            ]
        rows_by_concept = {}
        for concept_id in subject_concept_ids:
            rows = []
            for pred in predicate_values:
                for row in single_lookup(
                    concept_id,
                    predicate=pred,
                    limit=limit_per_concept,
                ):
                    payload = dict(row)
                    payload.setdefault("subject_concept_id", concept_id)
                    payload.setdefault("predicate", pred)
                    rows.append(payload)
            rows_by_concept[concept_id] = rows[:limit_per_concept]
        return rows_by_concept

    return _side_effect


def test_resolve_policy_from_graph_returns_none_when_policy_not_found():
    """Graph resolver returns None only when the policy concept is absent."""
    with patch(f"{_SERVICE}.ConceptsRepository") as mock_repo:
        mock_repo.find_one.return_value = None
        result = resolve_policy_from_graph("#V#nonexistent_policy")
        assert result is None


def test_resolve_policy_from_graph_reports_incomplete_when_no_configs():
    """A policy concept with no stage configurations is explicitly incomplete."""
    with patch(f"{_SERVICE}.ConceptsRepository") as mock_repo, patch(
        f"{_SERVICE}.get_texts_for_concept", return_value=[]
    ), patch(
        f"{_SERVICE}.get_texts_for_concepts",
        return_value={"#V#test_policy": []},
    ):
        mock_repo.find_one.return_value = {
            "concept_id": "#V#test_policy",
            "relationships": {},
        }
        result = resolve_policy_from_graph("#V#test_policy")
        assert result is not None
        assert result["completeness"] == GRAPH_POLICY_INCOMPLETE
        assert "no_stage_configurations" in result["incomplete_reasons"]


def test_resolve_policy_from_graph_resolves_fully_represented_policy():
    """Every value comes from represented fixtures; provenance names sources."""

    def mock_find_one(query):
        cid = query.get("concept_id")
        if cid == "#V#test_policy":
            return _policy_concept(["#V#test_planner_config"])
        if cid == "#V#test_planner_config":
            return _config_concept("#V#test_planner_config", "#V#planner_stage")
        return None

    def mock_get_texts(concept_id, predicate=None, limit=1):
        if concept_id == "#V#test_planner_config":
            if predicate == "#V#has_primary_model":
                return [{"text": "ollama:gemma4:26b"}]
            if predicate == "#V#has_fallback_model":
                return [{"text": "ollama:granite3.3:2b"}]
            if predicate == "#V#has_local_only_constraint":
                return [{"text": "true"}]
        if concept_id == "#V#test_policy":
            if predicate == "#V#has_max_fallback_hops":
                return [{"text": "2"}]
        if concept_id == "#V#planner_stage":
            if predicate == "#V#has_runtime_stage_name":
                return [{"text": "planner"}]
        return []

    with patch(f"{_SERVICE}.ConceptsRepository") as mock_repo, patch(
        f"{_SERVICE}.get_texts_for_concept", side_effect=mock_get_texts
    ), patch(
        f"{_SERVICE}.get_texts_for_concepts",
        side_effect=_batch_text_lookup_side_effect(mock_get_texts),
    ):
        mock_repo.find_one.side_effect = mock_find_one

        result = resolve_policy_from_graph("#V#test_policy")

        assert result is not None
        assert result["completeness"] == GRAPH_POLICY_COMPLETE
        assert result["incomplete_reasons"] == []
        assert result["policy_id"] == "#V#test_policy"
        assert result["stages"]["planner"]["primary"] == "ollama:gemma4:26b"
        assert result["stages"]["planner"]["fallback"] == ["ollama:granite3.3:2b"]
        assert result["stages"]["planner"]["constraints"]["local_only"] is True
        assert result["constraints"]["max_fallback_hops"] == 2
        assert result["constraints"]["local_only_stages"] == ["planner"]

        provenance = result["provenance"]["stages"]["planner"]
        assert provenance["config_concept_id"] == "#V#test_planner_config"
        assert provenance["stage_concept_id"] == "#V#planner_stage"
        assert provenance["stage_name_source"] == "represented_runtime_stage_name"
        assert (
            result["provenance"]["max_fallback_hops_source"] == "#V#test_policy"
        )


def test_resolve_policy_from_graph_derives_stage_name_structurally():
    """Without a represented runtime name, the stage name derives from the
    stage concept's own identifier, not a Python stage table."""

    def mock_find_one(query):
        cid = query.get("concept_id")
        if cid == "#V#test_policy":
            return _policy_concept(["#V#cfg"])
        if cid == "#V#cfg":
            return _config_concept("#V#cfg", "#V#evidence_review_stage")
        return None

    def mock_get_texts(concept_id, predicate=None, limit=1):
        if concept_id == "#V#cfg" and predicate == "#V#has_primary_model":
            return [{"text": "ollama:gemma4:26b"}]
        if concept_id == "#V#test_policy" and predicate == "#V#has_max_fallback_hops":
            return [{"text": "1"}]
        return []

    with patch(f"{_SERVICE}.ConceptsRepository") as mock_repo, patch(
        f"{_SERVICE}.get_texts_for_concept", side_effect=mock_get_texts
    ), patch(
        f"{_SERVICE}.get_texts_for_concepts",
        side_effect=_batch_text_lookup_side_effect(mock_get_texts),
    ):
        mock_repo.find_one.side_effect = mock_find_one

        result = resolve_policy_from_graph("#V#test_policy")

        assert result is not None
        assert result["completeness"] == GRAPH_POLICY_COMPLETE
        assert "evidence_review" in result["stages"]
        provenance = result["provenance"]["stages"]["evidence_review"]
        assert provenance["stage_name_source"] == "derived_from_concept_id"


def test_resolve_policy_missing_primary_model_is_incomplete_not_active_llm():
    """Absent #V#has_primary_model must not silently become active_llm."""

    def mock_find_one(query):
        cid = query.get("concept_id")
        if cid == "#V#test_policy":
            return _policy_concept(["#V#cfg"])
        if cid == "#V#cfg":
            return _config_concept("#V#cfg", "#V#planner_stage")
        return None

    def mock_get_texts(concept_id, predicate=None, limit=1):
        if concept_id == "#V#test_policy" and predicate == "#V#has_max_fallback_hops":
            return [{"text": "2"}]
        return []

    with patch(f"{_SERVICE}.ConceptsRepository") as mock_repo, patch(
        f"{_SERVICE}.get_texts_for_concept", side_effect=mock_get_texts
    ), patch(
        f"{_SERVICE}.get_texts_for_concepts",
        side_effect=_batch_text_lookup_side_effect(mock_get_texts),
    ):
        mock_repo.find_one.side_effect = mock_find_one

        result = resolve_policy_from_graph("#V#test_policy")

        assert result is not None
        assert result["completeness"] == GRAPH_POLICY_INCOMPLETE
        assert "missing_primary_model:#V#cfg" in result["incomplete_reasons"]
        assert "planner" not in result["stages"]
        flat = json.dumps(result)
        assert "active_llm" not in flat


def test_resolve_policy_missing_fallback_hops_is_incomplete_not_two():
    """Absent or invalid #V#has_max_fallback_hops must not silently become 2."""

    def mock_find_one(query):
        cid = query.get("concept_id")
        if cid == "#V#test_policy":
            return _policy_concept(["#V#cfg"])
        if cid == "#V#cfg":
            return _config_concept("#V#cfg", "#V#planner_stage")
        return None

    def _texts_missing_hops(concept_id, predicate=None, limit=1):
        if concept_id == "#V#cfg" and predicate == "#V#has_primary_model":
            return [{"text": "ollama:gemma4:26b"}]
        return []

    def _texts_invalid_hops(concept_id, predicate=None, limit=1):
        if concept_id == "#V#cfg" and predicate == "#V#has_primary_model":
            return [{"text": "ollama:gemma4:26b"}]
        if concept_id == "#V#test_policy" and predicate == "#V#has_max_fallback_hops":
            return [{"text": "not-a-number"}]
        return []

    for side_effect, reason in (
        (_texts_missing_hops, "missing_max_fallback_hops"),
        (_texts_invalid_hops, "invalid_max_fallback_hops"),
    ):
        with patch(f"{_SERVICE}.ConceptsRepository") as mock_repo, patch(
            f"{_SERVICE}.get_texts_for_concept", side_effect=side_effect
        ), patch(
            f"{_SERVICE}.get_texts_for_concepts",
            side_effect=_batch_text_lookup_side_effect(side_effect),
        ):
            mock_repo.find_one.side_effect = mock_find_one

            result = resolve_policy_from_graph("#V#test_policy")

            assert result is not None
            assert result["completeness"] == GRAPH_POLICY_INCOMPLETE
            assert reason in result["incomplete_reasons"]
            assert "max_fallback_hops" not in result["constraints"]


def test_resolve_policy_new_stage_needs_no_python_change():
    """Adding a stage through represented metadata requires no Python edit."""

    def mock_find_one(query):
        cid = query.get("concept_id")
        if cid == "#V#test_policy":
            return _policy_concept(["#V#cfg_novel"])
        if cid == "#V#cfg_novel":
            return _config_concept("#V#cfg_novel", "#V#brand_new_concept")
        return None

    def mock_get_texts(concept_id, predicate=None, limit=1):
        if concept_id == "#V#brand_new_concept":
            if predicate == "#V#has_runtime_stage_name":
                return [{"text": "novel_stage_name"}]
        if concept_id == "#V#cfg_novel" and predicate == "#V#has_primary_model":
            return [{"text": "openai:gpt-5.4-mini"}]
        if concept_id == "#V#test_policy" and predicate == "#V#has_max_fallback_hops":
            return [{"text": "3"}]
        return []

    with patch(f"{_SERVICE}.ConceptsRepository") as mock_repo, patch(
        f"{_SERVICE}.get_texts_for_concept", side_effect=mock_get_texts
    ), patch(
        f"{_SERVICE}.get_texts_for_concepts",
        side_effect=_batch_text_lookup_side_effect(mock_get_texts),
    ):
        mock_repo.find_one.side_effect = mock_find_one

        result = resolve_policy_from_graph("#V#test_policy")

        assert result is not None
        assert result["completeness"] == GRAPH_POLICY_COMPLETE
        assert "novel_stage_name" in result["stages"]


def test_resolve_policy_invalid_local_only_is_incomplete():
    """An unparseable local-only value is recorded, not silently false."""

    def mock_find_one(query):
        cid = query.get("concept_id")
        if cid == "#V#test_policy":
            return _policy_concept(["#V#cfg"])
        if cid == "#V#cfg":
            return _config_concept("#V#cfg", "#V#planner_stage")
        return None

    def mock_get_texts(concept_id, predicate=None, limit=1):
        if concept_id == "#V#cfg":
            if predicate == "#V#has_primary_model":
                return [{"text": "ollama:gemma4:26b"}]
            if predicate == "#V#has_local_only_constraint":
                return [{"text": "perhaps"}]
        if concept_id == "#V#test_policy" and predicate == "#V#has_max_fallback_hops":
            return [{"text": "2"}]
        return []

    with patch(f"{_SERVICE}.ConceptsRepository") as mock_repo, patch(
        f"{_SERVICE}.get_texts_for_concept", side_effect=mock_get_texts
    ), patch(
        f"{_SERVICE}.get_texts_for_concepts",
        side_effect=_batch_text_lookup_side_effect(mock_get_texts),
    ):
        mock_repo.find_one.side_effect = mock_find_one

        result = resolve_policy_from_graph("#V#test_policy")

        assert result is not None
        assert result["completeness"] == GRAPH_POLICY_INCOMPLETE
        assert "invalid_local_only_value:#V#cfg" in result["incomplete_reasons"]


def _complete_graph_policy():
    return {
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
        "completeness": GRAPH_POLICY_COMPLETE,
        "incomplete_reasons": [],
    }


def test_compare_policy_json_vs_graph_reports_match():
    """Comparison reports match when JSON and graph are identical."""
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

    with patch(f"{_SERVICE}._get_text_value") as mock_get_text, patch(
        f"{_SERVICE}.resolve_policy_from_graph"
    ) as mock_resolve:
        mock_get_text.return_value = json.dumps(json_policy)
        mock_resolve.return_value = _complete_graph_policy()

        report = compare_policy_json_vs_graph("#V#test_policy")

        assert report["status"] == "match"
        assert report["json_available"]
        assert report["graph_available"]
        assert not report["mismatches"]
        assert "planner" in report["matching_stages"]


def test_compare_policy_json_vs_graph_reports_mismatch():
    """Comparison reports mismatch when JSON and graph differ."""
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

    graph_policy = _complete_graph_policy()
    graph_policy["stages"]["planner"]["primary"] = "ollama:granite3.3:2b"
    graph_policy["stages"]["planner"]["fallback"] = []

    with patch(f"{_SERVICE}._get_text_value") as mock_get_text, patch(
        f"{_SERVICE}.resolve_policy_from_graph"
    ) as mock_resolve:
        mock_get_text.return_value = json.dumps(json_policy)
        mock_resolve.return_value = graph_policy

        report = compare_policy_json_vs_graph("#V#test_policy")

        assert report["status"] == "mismatch"
        assert len(report["mismatches"]) > 0


def test_compare_policy_json_vs_graph_reports_incomplete_graph():
    """An incomplete graph payload is reported as graph_incomplete."""
    json_policy = {
        "stages": {},
        "constraints": {},
    }

    incomplete = _complete_graph_policy()
    incomplete["completeness"] = GRAPH_POLICY_INCOMPLETE
    incomplete["incomplete_reasons"] = ["missing_max_fallback_hops"]

    with patch(f"{_SERVICE}._get_text_value") as mock_get_text, patch(
        f"{_SERVICE}.resolve_policy_from_graph"
    ) as mock_resolve:
        mock_get_text.return_value = json.dumps(json_policy)
        mock_resolve.return_value = incomplete

        report = compare_policy_json_vs_graph("#V#test_policy")

        assert report["status"] == "graph_incomplete"
        assert report["graph_available"] is False
        assert report["graph_incomplete_reasons"] == [
            "missing_max_fallback_hops"
        ]
