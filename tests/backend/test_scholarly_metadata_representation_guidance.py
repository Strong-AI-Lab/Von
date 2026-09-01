"""Retained represented guidance for tool use and workflow discovery."""

from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_DIR = _REPO_ROOT / "src" / "backend" / "workflows" / "repo_seed_bundles"


def test_missing_tool_retry_prompt_preserves_represented_label_authority() -> None:
    prompt_text = (_SEED_DIR / "missing_tool_call_retry_prompt_seed.md").read_text(
        encoding="utf-8"
    )

    lowered = prompt_text.lower()
    assert "represented labels, categories, tags, workflow markers" in lowered
    assert "vontology mutation plus read-back request" in lowered
    assert "do not substitute external label-listing tools" in lowered
    assert "get_text_relations_summary" in prompt_text


def test_scholarly_outcome_explanation_guidance_preserves_execution_truth() -> None:
    prompt_text = (
        _SEED_DIR / "prompt_scholarly_article_outcome_explanation_seed.md"
    ).read_text(encoding="utf-8")

    lowered = prompt_text.lower()
    assert "if the workflow was not invoked, say so" in lowered
    assert "do not describe an internal stage as having stopped" in lowered
    assert "smallest continuation" in lowered
    assert "never propose recreating a confirmed effect" in lowered
    assert "does not establish that the workflow ran" in lowered


def test_scholarly_workflow_links_outcome_explanation_prompt_map() -> None:
    bundle = json.loads(
        (_SEED_DIR / "paper_representation_workflow_seed_bundle.json").read_text(
            encoding="utf-8"
        )
    )
    workflow = next(
        item
        for item in bundle["workflows"]
        if item.get("workflow_id")
        == "#V#scholarly_article_metadata_representation_workflow"
    )
    prompt_map_text = next(
        item["text"]
        for item in workflow["text_relations"]
        if item.get("predicate")
        == "#V#hasWorkflowOutcomeExplanationPromptMapJson"
    )
    prompt_map = json.loads(prompt_map_text)

    assert prompt_map["schema_version"] == (
        "workflow_outcome_explanation_prompt_map.v1"
    )
    assert prompt_map["prompt_concept_ids"]["failed"] == (
        "#V#prompt_scholarly_article_outcome_explanation"
    )
    assert prompt_map["prompt_concept_ids"]["default"] == (
        "#V#prompt_scholarly_article_outcome_explanation"
    )


def test_tool_calling_workflow_has_generic_write_discovery_exemplars() -> None:
    bundle = json.loads(
        (_SEED_DIR / "canonical_workflow_publication_seed_bundle.json").read_text(
            encoding="utf-8"
        )
    )
    workflows = {
        item.get("workflow_id"): item
        for item in bundle.get("workflows", [])
        if isinstance(item, dict)
    }
    tool_calling = workflows["#V#tool_calling_workflow"]
    text_relations = tool_calling.get("text_relations", [])

    exemplar_text = next(
        item["text"]
        for item in text_relations
        if item.get("predicate") == "#V#hasWorkflowDiscoveryExemplarsJson"
    )
    exemplars = json.loads(exemplar_text)
    haystack = json.dumps(exemplars, sort_keys=True).lower()

    assert exemplars["schema_version"] == "workflow_discovery_exemplars.v1"
    assert "pasted scholarly metadata" in haystack
    assert "non-arxiv scholarly article metadata" in haystack
    assert "create missing predicate concept" in haystack
    assert "no more specific executable authoring workflow is eligible" in haystack

    routing_text = next(
        item["text"]
        for item in text_relations
        if item.get("predicate") == "#V#hasWorkflowRoutingProfileJson"
    )
    routing_profile = json.loads(routing_text)
    assert routing_profile["schema_version"] == "workflow_routing_profile.v1"
    assert routing_profile["role"] == "execution"
    assert routing_profile["authoring_intent_required"] is False


def test_jira_incremental_import_workflow_excludes_read_only_lookup_routing() -> None:
    bundle = json.loads(
        (_SEED_DIR / "canonical_workflow_publication_seed_bundle.json").read_text(
            encoding="utf-8"
        )
    )
    workflows = {
        item.get("workflow_id"): item
        for item in bundle.get("workflows", [])
        if isinstance(item, dict)
    }
    workflow = workflows["#V#jira_task_incremental_import_workflow"]
    text_relations = workflow.get("text_relations", [])

    routing_text = next(
        item["text"]
        for item in text_relations
        if item.get("predicate") == "#V#hasWorkflowRoutingProfileJson"
    )
    routing_profile = json.loads(routing_text)
    assert routing_profile["schema_version"] == "workflow_routing_profile.v1"
    assert routing_profile["role"] == "maintenance"
    assert routing_profile["domain"] == "jira_task_import"
    assert routing_profile["excludes_read_only_lookup"] is True
    assert routing_profile["requires_explicit_import_or_reconciliation_intent"] is True

    exemplar_text = next(
        item["text"]
        for item in text_relations
        if item.get("predicate") == "#V#hasWorkflowDiscoveryExemplarsJson"
    )
    exemplars = json.loads(exemplar_text)
    haystack = json.dumps(exemplars, sort_keys=True).lower()
    assert "import, synchronise, migrate, reconcile, or backfill" in haystack
    assert "do not choose this for read-only jira issue/task listing" in haystack
    assert "jira_search before answer synthesis" in haystack
