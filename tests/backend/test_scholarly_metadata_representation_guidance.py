"""Regression guardrails for scholarly metadata representation routing.

JVNAUTOSCI-2185 captured a near miss where Von correctly inferred how pasted
ACM article metadata should be represented, but no Vontology write tools were
used. These assertions pin the represented prompt/seed guidance so future edits
do not turn metadata-only scholarly article representation back into a
plan-only response or a file-copy-only path.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_DIR = _REPO_ROOT / "src" / "backend" / "workflows" / "repo_seed_bundles"


def test_expected_outcome_prompt_recognises_pasted_scholarly_metadata() -> None:
    prompt_text = (
        _SEED_DIR / "prompt_turn_execution_expected_outcome_inference_seed.md"
    ).read_text(encoding="utf-8")

    lowered = prompt_text.lower()
    assert "pasted scholarly bibliographic metadata" in lowered
    assert "how about with this too" in lowered
    assert "do not require an uploaded file" in lowered
    assert "#v#scholarly_article" in lowered
    assert "create it as a predicate concept" in lowered
    assert "do not create title/field concepts as individuals" in lowered
    assert "bare non-arXiv scholarly article URLs" in prompt_text
    assert "workflow_execute" in prompt_text


def test_expected_outcome_prompt_requires_represented_labels_to_write_and_readback() -> (
    None
):
    prompt_text = (
        _SEED_DIR / "prompt_turn_execution_expected_outcome_inference_seed.md"
    ).read_text(encoding="utf-8")

    lowered = prompt_text.lower()
    assert "represented labels, categories, tags, role markers" in lowered
    assert "workflow progress markers" in lowered
    assert "ontology-native mutation plan and read-back" in lowered
    assert "external label-list tool is not evidence" in lowered
    assert "listing existing external-system labels" in lowered
    assert "write-only required tool list is not sufficient" in lowered
    assert "Represented label/marker creation example" in prompt_text
    assert (
        '"required_tools":["search_concepts","create_concepts","add_relationship",'
        '"upsert_singleton_text_relation","fetch_concept",'
        '"get_text_relations_summary"]'
    ) in prompt_text


def test_expected_outcome_prompt_binds_type_targets_for_predicate_schema_turns() -> (
    None
):
    prompt_text = (
        _SEED_DIR / "prompt_turn_execution_expected_outcome_inference_seed.md"
    ).read_text(encoding="utf-8")

    lowered = prompt_text.lower()
    assert "predicates, relation schema, usage, incidence" in lowered
    assert "represented class/type" in lowered
    assert "target_type_ids" in prompt_text
    assert "get_predicate_incidence" in prompt_text
    assert "instance_of" in prompt_text
    assert "authenticated user" in lowered


def test_expected_outcome_prompt_requires_read_only_jira_lookup_evidence() -> None:
    prompt_text = (
        _SEED_DIR / "prompt_turn_execution_expected_outcome_inference_seed.md"
    ).read_text(encoding="utf-8")

    lowered = prompt_text.lower()
    assert "read-only jira retrieval" in lowered
    assert "latest, most recent, newest" in lowered
    assert "jira_search" in prompt_text
    assert "do not use jira import/reconciliation workflows" in lowered
    assert "#V#jira_task_full_reconciliation_workflow" in prompt_text
    assert "#V#jira_task_incremental_import_workflow" in prompt_text
    assert "task_import_jira_issues" in prompt_text
    assert "Jira recency/list lookup example" in prompt_text
    assert '"required_tools":["jira_search"]' in prompt_text
    assert "concrete predicate before `ORDER BY`" in prompt_text
    assert "never use a bare `ORDER BY updated DESC`" in prompt_text
    assert "issuetype = Task ORDER BY created DESC" in prompt_text
    assert "project = JVNAUTOSCI AND issuetype = Task ORDER BY created DESC" in (
        prompt_text
    )
    assert "created DESC" in prompt_text
    assert "updated DESC" in prompt_text
    assert "recency basis" in lowered


def test_missing_tool_retry_prompt_preserves_represented_label_authority() -> None:
    prompt_text = (_SEED_DIR / "missing_tool_call_retry_prompt_seed.md").read_text(
        encoding="utf-8"
    )

    lowered = prompt_text.lower()
    assert "represented labels, categories, tags, workflow markers" in lowered
    assert "vontology mutation plus read-back request" in lowered
    assert "do not substitute external label-listing tools" in lowered
    assert "get_text_relations_summary" in prompt_text


def test_selector_prompt_treats_scholarly_metadata_continuations_as_authoring() -> None:
    prompt_text = (_SEED_DIR / "prompt_chat_turn_classifier_seed.md").read_text(
        encoding="utf-8"
    )

    lowered = prompt_text.lower()
    assert "scholarly article/paper metadata" in lowered
    assert "doi" in lowered
    assert "abstract" in lowered
    assert "how about with this too" in lowered
    assert "authoring intent" in lowered
    assert (
        "specialised executable scholarly-metadata representation workflow" in lowered
    )
    assert "#V#tool_calling_workflow" in prompt_text


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
