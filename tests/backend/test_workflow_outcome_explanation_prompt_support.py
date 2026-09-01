from __future__ import annotations


def test_prompt_map_normalisation_rejects_unknown_and_non_concept_entries() -> None:
    from src.backend.workflows.outcome_explanation_prompt_support import (
        normalise_workflow_outcome_explanation_prompt_map,
    )

    result = normalise_workflow_outcome_explanation_prompt_map(
        {
            "schema_version": "workflow_outcome_explanation_prompt_map.v1",
            "prompt_concept_ids": {
                "failed": "#V#prompt_failed",
                "partial": "not-a-concept-id",
                "invented_status": "#V#prompt_invented",
            },
        }
    )

    assert result == {"failed": "#V#prompt_failed"}


def test_prompt_selection_uses_exact_status_then_represented_default() -> None:
    from src.backend.workflows.outcome_explanation_prompt_support import (
        select_workflow_outcome_explanation_prompt,
    )

    support = {
        "workflow_id": "#V#workflow",
        "source": "text_relation:#V#hasWorkflowOutcomeExplanationPromptMapJson",
        "prompts": {
            "failed": {
                "prompt_concept_id": "#V#prompt_failed",
                "prompt_text": "Explain this failure.",
            },
            "default": {
                "prompt_concept_id": "#V#prompt_default",
                "prompt_text": "Explain this outcome.",
            },
        },
    }

    failed = select_workflow_outcome_explanation_prompt(
        support,
        outcome_key="failed",
    )
    partial = select_workflow_outcome_explanation_prompt(
        support,
        outcome_key="partial",
    )
    completed = select_workflow_outcome_explanation_prompt(
        support,
        outcome_key="completed",
    )

    assert failed is not None
    assert failed["matched_prompt_key"] == "failed"
    assert failed["prompt_concept_id"] == "#V#prompt_failed"
    assert partial is not None
    assert partial["matched_prompt_key"] == "default"
    assert partial["prompt_concept_id"] == "#V#prompt_default"
    assert completed is None
