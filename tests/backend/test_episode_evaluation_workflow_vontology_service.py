from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_BUILD_EVIDENCE_ACTION_ID,
    EPISODE_EVALUATION_PERSIST_MEMORY_ACTION_ID,
    EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
    EPISODE_EVALUATION_PROMPT_LINK_PREDICATE,
    EPISODE_EVALUATION_WORKFLOW_ID,
    EPISODE_GROUNDED_HELPFULNESS_PROMPT_CONCEPT_ID,
    EPISODE_GROUNDED_HELPFULNESS_WORKFLOW_ID,
    EPISODE_WORKFLOW_EXPERIENCE_GUIDANCE_PROMPT_CONCEPT_ID,
    EPISODE_SELF_IMPROVEMENT_PROMOTION_PROMPT_CONCEPT_ID,
    EPISODE_SELF_IMPROVEMENT_PROMOTION_WORKFLOW_ID,
    EPISODE_SELF_IMPROVEMENT_PROPOSAL_PROMPT_CONCEPT_ID,
    EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID,
    EPISODE_SELF_IMPROVEMENT_SUBMIT_PROPOSAL_ACTION_ID,
    EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
    EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
)
from src.backend.services.episode_evaluation_workflow_vontology_service import (
    _ensure_episode_evaluation_prompt_support,
    bootstrap_canonical_episode_evaluation_workflow,
)
from src.backend.services import concept_service
from src.backend.services.episode_self_improvement_profile_vontology_service import (
    DEFAULT_EPISODE_SELF_IMPROVEMENT_PROFILE_CONCEPT_ID,
    EPISODE_SELF_IMPROVEMENT_PROFILE_LINK_PREDICATE,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.workflows.durable.startup import get_instance_manager
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology
from src.backend.integrations.internal_mcp import (
    InternalMCPGateway,
    InternalMCPTransport,
    build_default_catalogue,
)


_WORKFLOW_EXPERIENCE_GUIDANCE_PREDICATES = {
    "#V#hasWorkflowSuccessfulRunGuidanceText",
    "#V#hasWorkflowFailureAvoidanceGuidanceText",
    "#V#hasWorkflowNextRunExplorationGuidanceText",
}


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _state_with_suffix(definition: Any, suffix: str) -> Any:
    return next(
        (
            state
            for state_id, state in definition.states.items()
            if str(state_id).endswith(f"_{suffix}")
        ),
        None,
    )


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    from src.backend.db.mongo_client import get_db
    from src.backend.workflows.durable import instance_manager
    from src.backend.workflows import workflow_concept_authority_service as authority_service

    db = get_db()
    if db is not None:
        for collection_name in (
            "concepts",
            "text_relations",
            "text_values",
            "workflow_event_bindings",
            "workflow_instances",
        ):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    instance_manager._indexes_ensured = False
    authority_service.clear_workflow_type_resolution_cache()
    yield
    authority_service.clear_workflow_type_resolution_cache()


def test_bootstrap_materialises_episode_evaluation_workflow_family(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_episode_evaluation_workflow()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert report.get("success") is True
    assert counts.get("errors") == 0
    assert counts.get("workflows_published") == 4
    assert report.get("self_improvement_profile_support", {}).get("success") is True

    definition = load_workflow_definition_from_vontology(EPISODE_EVALUATION_WORKFLOW_ID)
    assert definition is not None
    grounded_definition = load_workflow_definition_from_vontology(
        EPISODE_GROUNDED_HELPFULNESS_WORKFLOW_ID
    )
    assert grounded_definition is not None
    proposal_definition = load_workflow_definition_from_vontology(
        EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID
    )
    assert proposal_definition is not None
    promotion_definition = load_workflow_definition_from_vontology(
        EPISODE_SELF_IMPROVEMENT_PROMOTION_WORKFLOW_ID
    )
    assert promotion_definition is not None

    action_ids = sorted(
        {
            action.action_id
            for state in definition.states.values()
            for action in state.actions
            if action.action_id
        }
    )
    assert EPISODE_EVALUATION_BUILD_EVIDENCE_ACTION_ID in action_ids
    assert EPISODE_EVALUATION_PERSIST_MEMORY_ACTION_ID in action_ids
    assert "workflow_create_instance" in action_ids
    assert "workflow_invoke_subworkflow" in action_ids
    grounded_action_ids = sorted(
        {
            action.action_id
            for state in grounded_definition.states.values()
            for action in state.actions
            if action.action_id
        }
    )
    assert grounded_action_ids == ["llm.action"]
    assert _state_with_suffix(definition, "prepare_workflow_experience_profile") is not None
    guidance_state = _state_with_suffix(definition, "induce_workflow_experience_guidance")
    assert guidance_state is not None
    assert _state_with_suffix(definition, "write_exploration_guidance_relation") is not None
    guidance_action = guidance_state.actions[0]
    assert guidance_action.action_id == "llm.action"
    assert guidance_action.prompt_contract is not None
    assert guidance_action.prompt_contract.get("requested_prompt_concept_ids") == [
        EPISODE_WORKFLOW_EXPERIENCE_GUIDANCE_PROMPT_CONCEPT_ID
    ]
    guidance_policy = guidance_action.llm_policy or {}
    assert "no more than 280 characters" in str(
        guidance_policy.get("response_contract_text") or ""
    )
    proposal_action_ids = sorted(
        {
            action.action_id
            for state in proposal_definition.states.values()
            for action in state.actions
            if action.action_id
        }
    )
    assert EPISODE_SELF_IMPROVEMENT_SUBMIT_PROPOSAL_ACTION_ID in proposal_action_ids
    assert "workflow_create_instance" in proposal_action_ids

    prompt_rows = get_texts_for_concept(
        EPISODE_EVALUATION_WORKFLOW_ID,
        predicate=EPISODE_EVALUATION_PROMPT_LINK_PREDICATE,
        limit=5,
    )
    assert any(
        (row or {}).get("text") == EPISODE_EVALUATION_PROMPT_CONCEPT_ID
        for row in prompt_rows
    )
    prompt_content_rows = get_texts_for_concept(
        EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert any((row or {}).get("text") for row in prompt_content_rows)
    prompt_text = next(
        ((row or {}).get("text") for row in prompt_content_rows if (row or {}).get("text")),
        "",
    )
    assert isinstance(prompt_text, str)
    assert "routing_quality_signals" in prompt_text
    assert "workflow/routing selection defects" in prompt_text
    assert "improvement_suggestions" in prompt_text
    proposal_prompt_rows = get_texts_for_concept(
        EPISODE_SELF_IMPROVEMENT_PROPOSAL_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert any((row or {}).get("text") for row in proposal_prompt_rows)
    grounded_prompt_rows = get_texts_for_concept(
        EPISODE_GROUNDED_HELPFULNESS_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert any((row or {}).get("text") for row in grounded_prompt_rows)
    grounded_prompt_text = next(
        ((row or {}).get("text") for row in grounded_prompt_rows if (row or {}).get("text")),
        "",
    )
    assert isinstance(grounded_prompt_text, str)
    assert "grounded helpfulness" in grounded_prompt_text.lower()
    assert "answer-support evidence" in grounded_prompt_text.lower()
    guidance_prompt_rows = get_texts_for_concept(
        EPISODE_WORKFLOW_EXPERIENCE_GUIDANCE_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    guidance_prompt_text = next(
        ((row or {}).get("text") for row in guidance_prompt_rows if (row or {}).get("text")),
        "",
    )
    assert "low-imposition probe" in guidance_prompt_text
    assert "workflow_experience_guidance_induction.v1" in guidance_prompt_text
    assert "no more than 280 characters" in guidance_prompt_text
    assert "Do not quote, copy, or summarise the full historical guidance entries" in (
        guidance_prompt_text
    )
    promotion_prompt_rows = get_texts_for_concept(
        EPISODE_SELF_IMPROVEMENT_PROMOTION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert any((row or {}).get("text") for row in promotion_prompt_rows)
    profile_link_rows = get_texts_for_concept(
        EPISODE_EVALUATION_WORKFLOW_ID,
        predicate=EPISODE_SELF_IMPROVEMENT_PROFILE_LINK_PREDICATE,
        limit=5,
    )
    assert any(
        (row or {}).get("text") == DEFAULT_EPISODE_SELF_IMPROVEMENT_PROFILE_CONCEPT_ID
        for row in profile_link_rows
    )

    manager = get_instance_manager()
    turn_bindings = manager.list_event_bindings(
        event_type=EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
        enabled_only=True,
        limit=10,
    )
    terminal_bindings = manager.list_event_bindings(
        event_type=EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
        enabled_only=True,
        limit=10,
    )
    assert any(
        binding.workflow_id == EPISODE_EVALUATION_WORKFLOW_ID for binding in turn_bindings
    )
    assert any(
        binding.workflow_id == EPISODE_EVALUATION_WORKFLOW_ID
        for binding in terminal_bindings
    )

    for predicate_id in _WORKFLOW_EXPERIENCE_GUIDANCE_PREDICATES:
        doc = concept_service.get_concept_by_concept_id(predicate_id)
        assert doc is not None
        assert not (doc.get("metadata") or {}).get("virtual")
        relationships = doc.get("relationships") or {}
        assert "#V#predicate" in relationships.get("is_an_instance_of", [])


def test_episode_prompt_support_seeds_content_from_repo_asset(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_episode_evaluation_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 5
    prompt_content_rows = get_texts_for_concept(
        EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    prompt_text = next(
        ((row or {}).get("text") for row in prompt_content_rows if (row or {}).get("text")),
        "",
    )
    assert isinstance(prompt_text, str)
    assert "routing_quality_signals" in prompt_text
    assert "workflow/routing selection defects" in prompt_text
    assert "improvement_suggestions" in prompt_text
    proposal_prompt_rows = get_texts_for_concept(
        EPISODE_SELF_IMPROVEMENT_PROPOSAL_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert any((row or {}).get("text") for row in proposal_prompt_rows)
    grounded_prompt_rows = get_texts_for_concept(
        EPISODE_GROUNDED_HELPFULNESS_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert any((row or {}).get("text") for row in grounded_prompt_rows)
    promotion_prompt_rows = get_texts_for_concept(
        EPISODE_SELF_IMPROVEMENT_PROMOTION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert any((row or {}).get("text") for row in promotion_prompt_rows)


def test_episode_prompt_support_force_prompt_seed_refreshes_existing_content(
    _reset_mock_db: Any,
) -> None:
    _ensure_episode_evaluation_prompt_support()
    from src.backend.services.text_value_service import upsert_singleton_text_relation

    upsert_singleton_text_relation(
        subject_concept_id=EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        text="stale prompt text",
        lang="en-NZ",
        garbage_collect=True,
    )

    report = _ensure_episode_evaluation_prompt_support(force_prompt_seed=True)

    assert report.get("success") is True
    prompt_content_rows = get_texts_for_concept(
        EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    prompt_text = next(
        ((row or {}).get("text") for row in prompt_content_rows if (row or {}).get("text")),
        "",
    )
    assert isinstance(prompt_text, str)
    assert prompt_text != "stale prompt text"
    assert "improvement_suggestions" in prompt_text


def test_workflow_experience_guidance_predicates_write_via_gateway(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_episode_evaluation_workflow()
    concept_service.create_concept(
        name="Workflow experience profile test fixture",
        concept_id="#V#workflow_experience_profile_test_fixture",
        parent_concept_ids=["#V#workflow_llm_experience_profile"],
        create_as_instance=True,
    )
    gateway = _build_gateway()

    for index, predicate_id in enumerate(
        sorted(_WORKFLOW_EXPERIENCE_GUIDANCE_PREDICATES),
        start=1,
    ):
        payload = gateway.invoke(
            "upsert_text_relation",
            {
                "concept_id": "#V#workflow_experience_profile_test_fixture",
                "predicate": predicate_id,
                "text": (
                    "workflow_experience_guidance.v1\n"
                    f"request_id: request-{index}\n"
                    "body:\n"
                    "Keep the guidance body stable."
                ),
                "language": "en-NZ",
                "context": {"schema_version": "workflow_experience_guidance_write.v1"},
            },
        ).payload

        assert payload["success"] is True
        assert payload["predicate"] == predicate_id
        assert payload["relation_created"] is True

    repeat_payload = gateway.invoke(
        "upsert_text_relation",
        {
            "concept_id": "#V#workflow_experience_profile_test_fixture",
            "predicate": "#V#hasWorkflowSuccessfulRunGuidanceText",
            "text": (
                "workflow_experience_guidance.v1\n"
                "request_id: request-repeat\n"
                "body:\n"
                "Keep the guidance body stable."
            ),
            "language": "en-NZ",
        },
    ).payload

    assert repeat_payload["success"] is True
    assert repeat_payload["relation_created"] is True
    rows = get_texts_for_concept(
        "#V#workflow_experience_profile_test_fixture",
        predicate="#V#hasWorkflowSuccessfulRunGuidanceText",
        limit=5,
    )
    assert len(rows) == 2
