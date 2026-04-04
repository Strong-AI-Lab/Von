"""Tests for workflow-template profile selection and repo-seed affordances."""

from __future__ import annotations

from typing import Any

import pytest

from src.backend.workflows.workflow_template_profile_service import (
    WORKFLOW_CREATION_COMPANY_TEMPLATE_ID,
    DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH,
    WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID,
    WORKFLOW_CREATION_EVENT_TEMPLATE_ID,
    WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID,
    WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
    WORKFLOW_CREATION_PLACE_TEMPLATE_ID,
    WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID,
    WORKFLOW_GAP_CANDIDATE_EXECUTION_TEMPLATE_ID,
    WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
    WORKFLOW_TEMPLATE_SPEC_PREDICATE,
    WORKFLOW_TEMPLATE_TYPE_ID,
    clear_workflow_template_bundle_cache,
    resolve_workflow_spec_template,
    select_workflow_template,
)
from src.backend.services import concept_service
from src.backend.services.text_value_service import get_texts_for_concept


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    clear_workflow_template_bundle_cache()

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    yield

    clear_workflow_template_bundle_cache()


def test_workflow_template_repo_seed_path_is_named_honestly() -> None:
    seed_path = str(DEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH).replace("\\", "/")
    assert "repo_seed_bundles" in seed_path
    assert "workflow_template_seed_bundle.json" in seed_path
    assert "authored_sources" not in seed_path


def test_select_workflow_template_prefers_scholarly_profile(
    _reset_mock_db: Any,
) -> None:
    selection = select_workflow_template(
        request_text=(
            "Create a workflow from this description request: represent scholarly "
            "works from uploaded PDFs in Vontology."
        )
    )

    assert selection["template_id"] == WORKFLOW_CREATION_SCHOLARLY_TEMPLATE_ID
    assert selection["selection_source"] == "automatic"
    assert selection["profile"]["requires_synthesis_policy"] is True
    assert str(selection["template"]["concept_id"]).startswith("#V#workflow_template_")


def test_select_workflow_template_prefers_phd_student_profile(
    _reset_mock_db: Any,
) -> None:
    selection = select_workflow_template(
        request_text=(
            "Create a workflow from this description request: represent a PhD "
            "student from text description in Vontology."
        )
    )

    assert selection["template_id"] == WORKFLOW_CREATION_PHD_STUDENT_TEMPLATE_ID
    assert selection["selection_source"] == "automatic"
    assert selection["profile"]["requires_synthesis_policy"] is True


@pytest.mark.parametrize(
    ("request_text", "expected_template_id"),
    [
        (
            "Create a workflow from this description request: represent a person from text description in Vontology.",
            WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
        ),
        (
            "Create a workflow from this description request: represent a company from text description in Vontology.",
            WORKFLOW_CREATION_COMPANY_TEMPLATE_ID,
        ),
        (
            "Create a workflow from this description request: represent an event from text description in Vontology.",
            WORKFLOW_CREATION_EVENT_TEMPLATE_ID,
        ),
        (
            "Create a workflow from this description request: represent a place from text description in Vontology.",
            WORKFLOW_CREATION_PLACE_TEMPLATE_ID,
        ),
    ],
)
def test_select_workflow_template_prefers_generic_entity_profiles(
    _reset_mock_db: Any,
    request_text: str,
    expected_template_id: str,
) -> None:
    selection = select_workflow_template(request_text=request_text)

    assert selection["template_id"] == expected_template_id
    assert selection["selection_source"] == "automatic"
    assert selection["profile"]["requires_synthesis_policy"] is True


def test_select_workflow_template_uses_fallback_for_generic_request(
    _reset_mock_db: Any,
) -> None:
    selection = select_workflow_template(
        request_text="Create a workflow from this description request."
    )

    assert selection["template_id"] == WORKFLOW_CREATION_DEFAULT_TEMPLATE_ID
    assert selection["selection_source"] == "fallback"


def test_resolve_workflow_spec_template_renders_gap_candidate_template(
    _reset_mock_db: Any,
) -> None:
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


def test_resolve_workflow_spec_template_renders_person_representation_template(
    _reset_mock_db: Any,
) -> None:
    rendered_spec, diagnostics = resolve_workflow_spec_template(
        request_text=(
            "Create a workflow from this description request: represent a person "
            "from text description in Vontology."
        ),
        explicit_template_id=WORKFLOW_CREATION_PERSON_TEMPLATE_ID,
        variables={
            "workflow_id": "#V#person_representation_workflow",
            "workflow_name": "Person Representation Workflow",
            "workflow_description": "Represent people from conversational text.",
            "request_summary": "represent person from text",
        },
    )

    assert diagnostics["template_id"] == WORKFLOW_CREATION_PERSON_TEMPLATE_ID
    assert rendered_spec["workflow_id"] == "#V#person_representation_workflow"
    assert rendered_spec["steps"][2]["action_id"] == "llm.action"
    assert rendered_spec["steps"][4]["action_id"] == (
        "entity_representation.materialise_from_payload"
    )
    text_relations = rendered_spec.get("text_relations") or []
    assert any(
        isinstance(item, dict)
        and item.get("predicate") == "#V#hasWorkflowRoutingProfileJson"
        for item in text_relations
    )
    assert any(
        isinstance(item, dict)
        and item.get("predicate") == "#V#hasWorkflowDiscoveryExemplarsJson"
        for item in text_relations
    )


def test_select_workflow_template_materialises_first_class_template_concepts(
    _reset_mock_db: Any,
) -> None:
    selection = select_workflow_template(
        request_text="Create a workflow from this description request."
    )

    concept_id = str(selection["template"]["concept_id"] or "").strip()
    assert concept_id

    concept_doc = concept_service.get_concept_by_concept_id(concept_id)
    assert concept_doc is not None
    instance_of = (concept_doc.get("relationships") or {}).get("is_an_instance_of") or []
    assert WORKFLOW_TEMPLATE_TYPE_ID in instance_of

    profile_rows = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_PROFILE_PREDICATE,
        limit=5,
    )
    assert profile_rows

    spec_rows = get_texts_for_concept(
        concept_id,
        predicate=WORKFLOW_TEMPLATE_SPEC_PREDICATE,
        limit=5,
    )
    assert spec_rows
