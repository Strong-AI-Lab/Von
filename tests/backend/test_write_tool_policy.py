"""Tests for write-tool policy decisions."""

from __future__ import annotations


def test_recent_prompt_allows_vontology_write():
    from src.backend.workflows.write_tool_policy import compute_allowed_write_tools

    decision = compute_allowed_write_tools(
        prompt="These predicates are just instances of #V#nearly_functional_binary_predicate.",
        requested_tools=["create_concepts"],
        recent_user_prompts=[
            "Please create the predicate has_blob_key in the ontology.",
        ],
    )

    assert "create_concepts" in decision.allowed_tools
    assert decision.reason == "recent_vontology_mutation_request"


def test_recent_prompt_allows_artefact_download():
    from src.backend.workflows.write_tool_policy import compute_allowed_write_tools

    decision = compute_allowed_write_tools(
        prompt="What is arxiv 1234.5678 about?",
        requested_tools=["download_paper"],
        recent_user_prompts=["Download and store the arxiv 1234.5678 PDF."],
    )

    assert "download_paper" in decision.allowed_tools
    assert decision.reason == "recent_artefact_download_request"


def test_no_recent_write_intent_blocks():
    from src.backend.workflows.write_tool_policy import compute_allowed_write_tools

    decision = compute_allowed_write_tools(
        prompt="These predicates are just instances of #V#nearly_functional_binary_predicate.",
        requested_tools=["create_concepts"],
        recent_user_prompts=["Explain how predicates relate to instances."],
    )

    assert "create_concepts" not in decision.allowed_tools
    assert decision.reason == "no_explicit_write_intent_detected"
