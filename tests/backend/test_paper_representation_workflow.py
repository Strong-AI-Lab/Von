from __future__ import annotations

from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable.paper_representation_workflow import (
    ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
    ARXIV_NORMALISE_SOURCE_ACTION_ID,
    SCHOLARLY_PAPER_ENRICH_ACTION_ID,
    SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
    SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
    SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
    SCHOLARLY_PAPER_VERIFY_ACTION_ID,
    register_paper_representation_actions,
)


def test_register_paper_representation_actions_registers_expected_ids() -> None:
    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    assert {
        ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
        ARXIV_NORMALISE_SOURCE_ACTION_ID,
        SCHOLARLY_PAPER_ENRICH_ACTION_ID,
        SCHOLARLY_PAPER_MATERIALISE_ACTION_ID,
        SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
        SCHOLARLY_PAPER_RESOLVE_AUTHORS_ACTION_ID,
        SCHOLARLY_PAPER_VERIFY_ACTION_ID,
    }.issubset(set(registry.all_action_ids()))


def test_normalise_inputs_extracts_arxiv_id_from_prompt_context() -> None:
    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    result = registry.execute(
        SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
        inputs={},
        context={
            "prompt": "Please represent https://arxiv.org/abs/2602.20478",
            "file_copy_concept_id": "#V#file_copy_2602_20478",
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["arxiv_id"] == "2602.20478"
    assert result.outputs["verification_profile"] == "arxiv"
    assert result.outputs["file_copy_concept_id"] == "#V#file_copy_2602_20478"


def test_arxiv_decide_acquisition_mode_prefers_existing_file_copy() -> None:
    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    result = registry.execute(
        ARXIV_DECIDE_ACQUISITION_MODE_ACTION_ID,
        inputs={
            "arxiv_id": "2602.20478",
            "file_copy_concept_id": "#V#existing_file_copy",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["result"] is False
    assert result.outputs["acquisition_required"] is False
    assert result.outputs["acquisition_mode"] == "existing_file_copy"
