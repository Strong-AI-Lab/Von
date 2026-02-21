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


def test_recent_prompt_allows_cached_paper_finalise():
    from src.backend.workflows.write_tool_policy import compute_allowed_write_tools

    decision = compute_allowed_write_tools(
        prompt="What is arxiv 1234.5678 about?",
        requested_tools=["finalise_cached_paper"],
        recent_user_prompts=["Finalise and store the cached arxiv 1234.5678 PDF."],
    )

    assert "finalise_cached_paper" in decision.allowed_tools
    assert decision.reason == "recent_artefact_download_request"


def test_explicit_get_arxiv_paper_allows_download():
    from src.backend.workflows.write_tool_policy import compute_allowed_write_tools

    decision = compute_allowed_write_tools(
        prompt="2512.23959 get that arxiv paper",
        requested_tools=["download_paper"],
        recent_user_prompts=[],
    )

    assert "download_paper" in decision.allowed_tools
    assert decision.reason == "explicit_artefact_download_request"


def test_explicit_finalise_cached_arxiv_paper_allows_write():
    from src.backend.workflows.write_tool_policy import compute_allowed_write_tools

    decision = compute_allowed_write_tools(
        prompt="Finalise cached arXiv:2512.23959 and store it.",
        requested_tools=["finalise_cached_paper"],
        recent_user_prompts=[],
    )

    assert "finalise_cached_paper" in decision.allowed_tools
    assert decision.reason == "explicit_artefact_download_request"


def test_minimal_finalise_arxiv_id_allows_write():
    from src.backend.workflows.write_tool_policy import compute_allowed_write_tools

    decision = compute_allowed_write_tools(
        prompt="finalise 2505.12477",
        requested_tools=["finalise_cached_paper"],
        recent_user_prompts=[],
    )

    assert "finalise_cached_paper" in decision.allowed_tools
    assert decision.reason == "explicit_artefact_download_request"


def test_no_recent_write_intent_blocks():
    from src.backend.workflows.write_tool_policy import compute_allowed_write_tools

    decision = compute_allowed_write_tools(
        prompt="These predicates are just instances of #V#nearly_functional_binary_predicate.",
        requested_tools=["create_concepts"],
        recent_user_prompts=["Explain how predicates relate to instances."],
    )

    assert "create_concepts" not in decision.allowed_tools
    assert decision.reason == "no_explicit_write_intent_detected"


def test_explicit_note_request_allows_text_relation_write():
    from src.backend.workflows.write_tool_policy import compute_allowed_write_tools

    decision = compute_allowed_write_tools(
        prompt="Add the note now.",
        requested_tools=["upsert_text_relation"],
        recent_user_prompts=[],
    )

    assert "upsert_text_relation" in decision.allowed_tools
    assert decision.reason == "explicit_vontology_mutation_request"


def test_high_impact_tool_detection():
    from src.backend.workflows.write_tool_policy import (
        is_high_impact_vontology_write_tool,
    )

    assert is_high_impact_vontology_write_tool("create_concepts") is True
    assert is_high_impact_vontology_write_tool("add_relationship") is True
    assert is_high_impact_vontology_write_tool("upsert_text_relation") is False


def test_prompt_grants_high_impact_kb_write_approval():
    from src.backend.workflows.write_tool_policy import (
        prompt_grants_high_impact_kb_write_approval,
    )

    assert (
        prompt_grants_high_impact_kb_write_approval(
            prompt="Approved. Proceed with the Vontology concept write.",
            recent_user_prompts=[],
        )
        is True
    )
    assert (
        prompt_grants_high_impact_kb_write_approval(
            prompt="Please create a concept in the ontology.",
            recent_user_prompts=[],
        )
        is False
    )
