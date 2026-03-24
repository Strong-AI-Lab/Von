"""Tests for workflow-description prompt support and quality diagnostics."""

from __future__ import annotations

from typing import Any, cast


def test_resolve_workflow_description_prompt_prefers_workflow_link(
    monkeypatch,
) -> None:
    from src.backend.services import workflow_description_vontology_service as mod

    monkeypatch.setattr(
        mod,
        "resolve_linked_prompt_concept_id",
        lambda **_kwargs: "#V#linked_workflow_description_prompt",
    )

    resolved = mod.resolve_workflow_description_prompt_concept_id(
        workflow_id="#V#enrichment_workflow"
    )

    assert resolved == "#V#linked_workflow_description_prompt"


def test_build_workflow_description_prompt_context_summarises_graph(monkeypatch) -> None:
    from src.backend.services import workflow_description_vontology_service as mod

    monkeypatch.setattr(
        mod,
        "build_workflow_process_graph",
        lambda workflow_id: (
            {
                "initial_step": "#V#step_start",
                "steps": [
                    {
                        "step_id": "#V#step_start",
                        "invokes_action_target": "workflow.list",
                        "invokes_workflow": None,
                        "writes_context_keys": ["workflow_ids"],
                        "control_flow": {
                            "conditions": [
                                {
                                    "reason": "workflow_ids_ready",
                                    "to": "#V#step_describe",
                                }
                            ]
                        },
                    },
                    {
                        "step_id": "#V#step_describe",
                        "invokes_action_target": "enrichment.process_batch",
                        "invokes_workflow": "#V#enrichment_workflow",
                        "writes_context_keys": ["descriptions_updated"],
                        "control_flow": {"conditions": []},
                    },
                ],
            },
            [],
        ),
    )

    context = mod.build_workflow_description_prompt_context("#V#demo_workflow")

    assert context["workflow_initial_step"] == "#V#step_start"
    assert context["workflow_step_count"] == "2"
    assert "workflow.list" in context["workflow_action_ids"]
    assert "#V#enrichment_workflow" in context["workflow_subworkflow_ids"]
    assert "workflow_ids_ready" in context["workflow_transition_reasons"]
    assert "descriptions_updated" in context["workflow_output_context_keys"]


def test_ensure_workflow_description_prompt_support_bootstraps_prompts_and_link(
    monkeypatch,
) -> None:
    from src.backend.services import workflow_description_vontology_service as mod

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        mod,
        "ensure_prompt_concept_support",
        lambda **kwargs: captured.update(kwargs)
        or {
            "success": True,
            "counts": {
                "created_prompts": 2,
                "validated_prompts": 2,
                "missing_content_prompts": 0,
                "linked_workflows": 1,
                "errors": 0,
            },
        },
    )

    report = mod.ensure_workflow_description_prompt_support()

    assert report["success"] is True
    prompt_specs = cast(tuple[Any, ...], tuple(cast(Any, captured["prompt_specs"])))
    created_ids = {item.concept_id for item in prompt_specs}
    assert mod.DESCRIPTION_PROMPT_CONCEPT_ID in created_ids
    assert mod.WORKFLOW_DESCRIPTION_PROMPT_CONCEPT_ID in created_ids
    workflow_links = cast(tuple[Any, ...], tuple(cast(Any, captured["workflow_links"])))
    assert len(workflow_links) == 1
    assert workflow_links[0].workflow_id == "#V#enrichment_workflow"
    assert workflow_links[0].predicate == mod.WORKFLOW_DESCRIPTION_PROMPT_LINK_PREDICATE


def test_build_deterministic_workflow_description_returns_retrieval_ready_text() -> None:
    from src.backend.services import workflow_description_vontology_service as mod
    from src.backend.workflows.workflow_description_quality_service import (
        assess_workflow_description_quality,
    )

    description = mod.build_deterministic_workflow_description(
        concept_id="#V#planning_workflow",
        concept_name="Planning Workflow",
        existing_description="Forward inference workflow that proposes concrete next actions.",
        workflow_context={
            "workflow_step_count": "6",
            "workflow_action_ids": "planning.collect_context, planning.rank_actions",
            "workflow_subworkflow_ids": "#V#tool_calling_workflow",
            "workflow_transition_reasons": "context_ready, actions_ranked",
            "workflow_output_context_keys": "planned_actions, rationale",
            "workflow_graph_warnings": "None",
        },
    )

    assert isinstance(description, str)
    assert "Domain:" in description
    assert "Input types:" in description
    assert "Output types:" in description
    assert "Prerequisite capabilities:" in description
    assert "Cost class:" in description
    assert "Maturity:" in description
    assert "Estimated success likelihood:" in description

    quality = assess_workflow_description_quality(
        description=description,
        description_source="deterministic_workflow_graph",
    )
    assert quality["quality_label"] == "retrieval_ready"
