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
    for tool_name in (
        "vontology_concept_search",
        "create_concepts",
        "add_relationship",
        "upsert_singleton_text_relation",
        "fetch_concept",
    ):
        assert tool_name in prompt_text


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
