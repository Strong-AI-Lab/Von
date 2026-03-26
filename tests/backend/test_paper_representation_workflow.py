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


def test_normalise_inputs_extracts_publication_date_from_metadata() -> None:
    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    result = registry.execute(
        SCHOLARLY_PAPER_NORMALISE_INPUTS_ACTION_ID,
        inputs={},
        context={
            "file_copy_concept_id": "#V#file_copy_2603_21702",
            "paper_metadata": {
                "title": "The Geometry of Next-Token Prediction",
                "published": "2026/03/23",
            },
            "source_uri": "https://arxiv.org/abs/2603.21702",
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["publication_date"] == "2026-03-23"


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


def test_verify_representation_requires_publication_date_for_arxiv_profile(
    monkeypatch,
) -> None:
    from src.backend.workflows.durable import paper_representation_workflow as mod

    registry = ActionRegistry()
    register_paper_representation_actions(registry)

    monkeypatch.setattr(
        mod,
        "_get_concept",
        lambda _concept_id: {
            "relationships": {
                "is_an_instance_of": ["#V#scholarly_article"],
                "#V#propositional_information_thing_has_computer_file": [
                    "#V#file_copy_2603_21702"
                ],
                "#V#authored_by": ["#V#author_1"],
                "#V#about": ["#V#topic_1"],
            },
            "attributes": {"arxiv_id": "2603.21702"},
        },
    )

    def _fake_get_texts_for_concept(*, predicate: str, **_kwargs):
        if predicate == "hasName":
            return [
                {"text": "The Geometry of Next-Token Prediction"},
                {"text": "2603.21702"},
                {"text": "https://arxiv.org/abs/2603.21702"},
            ]
        if predicate == "hasDescription":
            return [{"text": "Abstract text."}]
        if predicate == "#V#has_publication_date":
            return []
        if predicate == "#V#has_topic_labels":
            return [{"text": "cs.AI"}]
        return []

    monkeypatch.setattr(mod, "get_texts_for_concept", _fake_get_texts_for_concept)

    result = registry.execute(
        SCHOLARLY_PAPER_VERIFY_ACTION_ID,
        inputs={
            "paper_concept_id": "#V#paper_on_arxiv_2603_21702",
            "file_copy_concept_id": "#V#file_copy_2603_21702",
            "arxiv_id": "2603.21702",
            "publication_date": "2026-03-23",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["scholarly_representation_verified"] is False
    assert "publication_date_missing" in list(
        result.outputs["verification_failures"] or []
    )
