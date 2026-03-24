"""Tests for workflow-gap Vontology prompt support."""

from __future__ import annotations

from typing import Any, cast


def test_resolve_analysis_prompt_prefers_workflow_link(monkeypatch) -> None:
    from src.backend.services import workflow_gap_vontology_service as mod

    monkeypatch.setattr(
        mod,
        "resolve_linked_prompt_concept_id",
        lambda **_kwargs: "#V#linked_gap_analysis_prompt",
    )

    resolved = mod.resolve_workflow_gap_analysis_prompt_concept_id(
        workflow_id=mod.WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
    )

    assert resolved == "#V#linked_gap_analysis_prompt"


def test_ensure_prompt_support_creates_prompts_and_links_workflows(monkeypatch) -> None:
    from src.backend.services import workflow_gap_vontology_service as mod

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        mod,
        "ensure_prompt_concept_support",
        lambda **kwargs: captured.update(kwargs)
        or {
            "success": True,
            "counts": {
                "created_prompts": 3,
                "validated_prompts": 3,
                "missing_content_prompts": 0,
                "linked_workflows": 2,
                "errors": 0,
            },
        },
    )

    report = mod.ensure_workflow_gap_prompt_support()

    assert report["success"] is True
    prompt_specs = cast(tuple[Any, ...], tuple(cast(Any, captured["prompt_specs"])))
    assert {item.concept_id for item in prompt_specs} == {
        mod.WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID,
        mod.WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID,
        mod.WORKFLOW_GAP_CANDIDATE_PROMPT_CONCEPT_ID,
    }
    assert report["counts"]["linked_workflows"] == 2
    workflow_links = cast(tuple[Any, ...], tuple(cast(Any, captured["workflow_links"])))
    analysis_link = next(
        item
        for item in workflow_links
        if item.workflow_id == mod.WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
    )
    assert analysis_link.predicate == mod.WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE
    test_link = next(
        item
        for item in workflow_links
        if item.workflow_id == mod.WORKFLOW_GAP_TEST_WORKFLOW_ID
    )
    assert test_link.predicate == mod.WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE


def test_render_analysis_prompt_reports_missing_prompt(monkeypatch) -> None:
    from src.backend.services import workflow_gap_vontology_service as mod

    monkeypatch.setattr(
        mod,
        "resolve_workflow_gap_analysis_prompt_concept_id",
        lambda **_kwargs: mod.WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID,
    )
    monkeypatch.setattr(
        mod,
        "render_authoritative_prompt",
        lambda **_kwargs: (
            None,
            {"error": "workflow_gap_analysis_prompt_missing_or_empty"},
        ),
    )

    rendered, diagnostics = mod.render_workflow_gap_analysis_prompt(
        workflow_id=mod.WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
        variables={"analysis_payload_json": "{}"},
    )

    assert rendered is None
    assert diagnostics["error"] == "workflow_gap_analysis_prompt_missing_or_empty"
