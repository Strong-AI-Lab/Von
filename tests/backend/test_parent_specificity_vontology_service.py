"""Tests for parent-specificity Vontology prompt support."""

from __future__ import annotations

from typing import Any, cast


def test_resolve_prompt_concept_prefers_workflow_link(monkeypatch) -> None:
    from src.backend.services import parent_specificity_vontology_service as mod

    monkeypatch.setattr(
        mod,
        "resolve_linked_prompt_concept_id",
        lambda **_kwargs: "#V#linked_prompt",
    )

    resolved = mod.resolve_parent_specificity_prompt_concept_id(
        workflow_id=mod.PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID
    )

    assert resolved == "#V#linked_prompt"


def test_ensure_prompt_support_creates_prompt_and_links_workflow(monkeypatch) -> None:
    from src.backend.services import parent_specificity_vontology_service as mod

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        mod,
        "ensure_prompt_concept_support",
        lambda **kwargs: captured.update(kwargs)
        or {
            "success": True,
            "counts": {
                "created_prompts": 1,
                "validated_prompts": 1,
                "missing_content_prompts": 0,
                "linked_workflows": 1,
                "errors": 0,
            },
        },
    )

    report = mod.ensure_parent_specificity_prompt_support()

    assert report["success"] is True
    prompt_specs = cast(tuple[Any, ...], tuple(cast(Any, captured["prompt_specs"])))
    assert len(prompt_specs) == 1
    assert prompt_specs[0].concept_id == mod.PARENT_SPECIFICITY_PROMPT_CONCEPT_ID
    workflow_links = cast(tuple[Any, ...], tuple(cast(Any, captured["workflow_links"])))
    assert len(workflow_links) == 1
    assert workflow_links[0].workflow_id == mod.PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID
    assert workflow_links[0].predicate == mod.PARENT_SPECIFICITY_PROMPT_LINK_PREDICATE


def test_render_prompt_reports_missing_prompt(monkeypatch) -> None:
    from src.backend.services import parent_specificity_vontology_service as mod

    monkeypatch.setattr(
        mod,
        "resolve_parent_specificity_prompt_concept_id",
        lambda **_kwargs: mod.PARENT_SPECIFICITY_PROMPT_CONCEPT_ID,
    )
    monkeypatch.setattr(
        mod,
        "render_authoritative_prompt",
        lambda **_kwargs: (
            None,
            {"error": "parent_specificity_prompt_missing_or_empty"},
        ),
    )

    rendered, diagnostics = mod.render_parent_specificity_prompt(
        workflow_id=mod.PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
        variables={"analysis_payload_json": "{}"},
    )

    assert rendered is None
    assert diagnostics["error"] == "parent_specificity_prompt_missing_or_empty"
