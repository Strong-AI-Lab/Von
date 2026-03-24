"""Tests for authored workflow-template profile selection and rendering."""

from __future__ import annotations

from src.backend.workflows.workflow_template_profile_service import (
    WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID,
    WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID,
    WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID,
    WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID,
    resolve_workflow_spec_template,
    select_workflow_template,
)


def test_select_workflow_template_prefers_scholarly_profile() -> None:
    selection = select_workflow_template(
        request_text=(
            "Create a workflow from this description request: represent scholarly "
            "works from uploaded PDFs in Vontology."
        )
    )

    assert selection["template_id"] == WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID
    assert selection["selection_source"] == "automatic"
    assert selection["profile"]["requires_synthesis_policy"] is True


def test_select_workflow_template_prefers_phd_student_profile() -> None:
    selection = select_workflow_template(
        request_text=(
            "Create a workflow from this description request: represent a PhD "
            "student from text description in Vontology."
        )
    )

    assert selection["template_id"] == WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID
    assert selection["selection_source"] == "automatic"
    assert selection["profile"]["requires_synthesis_policy"] is True


def test_select_workflow_template_uses_fallback_for_generic_request() -> None:
    selection = select_workflow_template(
        request_text="Create a workflow from this description request."
    )

    assert selection["template_id"] == WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID
    assert selection["selection_source"] == "fallback"


def test_resolve_workflow_spec_template_renders_gap_candidate_template() -> None:
    rendered_spec, diagnostics = resolve_workflow_spec_template(
        request_text="Recover the missing workflow for this request.",
        explicit_template_id=WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID,
        variables={
            "workflow_id": "#V#candidate_gap_workflow",
            "workflow_name": "Candidate Gap Workflow",
            "workflow_description": "Candidate workflow for gap recovery.",
            "prompt_concept_id": "#V#candidate_gap_prompt",
            "request_text": "Recover the missing workflow for this request.",
            "recent_turns_json": "[]",
            "base_response_text": "Fallback reply.",
            "workflow_guidance_json": '["Prefer reusable surfaces."]',
            "acceptance_requirements_json": '["Produce a better response."]',
            "intent_summary": "workflow gap recovery",
            "gap_summary": "No suitable workflow matched.",
        },
    )

    assert diagnostics["template_id"] == WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID
    assert diagnostics["selection_source"] == "explicit"
    assert rendered_spec["workflow_id"] == "#V#candidate_gap_workflow"
    assert rendered_spec["steps"][0]["action_id"] == "workflow_gap.execute_candidate"
    assert rendered_spec["steps"][0]["inputs"]["prompt_concept_id"] == (
        "#V#candidate_gap_prompt"
    )
