from __future__ import annotations

import json
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
    EPISODE_SELF_IMPROVEMENT_PROMOTION_PROMPT_CONCEPT_ID,
    EPISODE_SELF_IMPROVEMENT_PROMOTION_WORKFLOW_ID,
    EPISODE_SELF_IMPROVEMENT_PROPOSAL_PROMPT_CONCEPT_ID,
    EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID,
    EPISODE_SELF_IMPROVEMENT_SUBMIT_PROPOSAL_ACTION_ID,
    EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
    EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
)
from src.backend.services.episode_evaluation_workflow_vontology_service import (
    _EPISODE_REPO_SEED_ASSET_PATH,
    _SELF_IMPROVEMENT_REPO_SEED_ASSET_PATH,
    _ensure_episode_evaluation_prompt_support,
    bootstrap_canonical_episode_evaluation_workflow,
)
from src.backend.services.episode_self_improvement_profile_vontology_service import (
    DEFAULT_EPISODE_SELF_IMPROVEMENT_PROFILE_CONCEPT_ID,
    EPISODE_SELF_IMPROVEMENT_PROFILE_LINK_PREDICATE,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.workflows.durable.startup import get_instance_manager
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
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


def test_episode_bundle_declares_reviewed_seed_3_migration() -> None:
    bundle = json.loads(_EPISODE_REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))

    assert bundle["seed_version"] == "4"
    assert bundle["known_legacy_authority_payload_sha256_by_seed_version"] == {
        "#V#episode_evaluation_workflow": {
            "3": [
                "4a793a8207aa3345fde93a0315ab6cfae12c3fe0b709f6801795a9cc282a6117"
            ]
        },
        "#V#episode_grounded_helpfulness_critic_workflow": {
            "3": [
                "e2c09afc711c675a7238e02f1537e38ae59fb33a2fe2b6e37aaffb312567f8e3"
            ]
        },
    }


def test_episode_workflow_bundle_source_tags_preserve_originating_jira_tasks() -> None:
    episode_bundle = json.loads(
        _EPISODE_REPO_SEED_ASSET_PATH.read_text(encoding="utf-8")
    )
    self_improvement_bundle = json.loads(
        _SELF_IMPROVEMENT_REPO_SEED_ASSET_PATH.read_text(encoding="utf-8")
    )

    assert episode_bundle["source_tag"] == "JVNAUTOSCI-1605"
    assert self_improvement_bundle["source_tag"] == "JVNAUTOSCI-1987"


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.delenv("VON_EPISODE_EVALUATION_AUTOTRIGGER_ENABLE", raising=False)
    monkeypatch.delenv(
        "VON_WORKFLOW_INTROSPECTION_AUTOTRIGGER_ENABLE",
        raising=False,
    )

    from src.backend.db.mongo_client import get_db
    from src.backend.workflows import (
        workflow_concept_authority_service as authority_service,
    )
    from src.backend.workflows.durable import instance_manager

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
    persist_state = _state_with_suffix(definition, "persist_assessment")
    assert persist_state is not None
    assert any(
        str(transition.to_state).endswith("_decide_maintenance_follow_up")
        for transition in persist_state.transitions
    )
    for retired_state in (
        "prepare_workflow_experience_profile",
        "induce_workflow_experience_guidance",
        "write_exploration_guidance_relation",
    ):
        assert _state_with_suffix(definition, retired_state) is None
    for state in (definition, grounded_definition):
        context_keys = {
            str(field.get("context_key") or "")
            for workflow_state in state.states.values()
            for action in workflow_state.actions
            for field in ((action.llm_policy or {}).get("context_fields") or [])
        }
        assert not {
            "workflow_success_guidance_history",
            "workflow_failure_avoidance_history",
            "workflow_low_imposition_exploration_history",
        } & context_keys
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
    enabled_turn_bindings = manager.list_event_bindings(
        event_type=EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
        enabled_only=True,
        limit=10,
    )
    all_turn_bindings = manager.list_event_bindings(
        event_type=EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
        limit=10,
    )
    enabled_terminal_bindings = manager.list_event_bindings(
        event_type=EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
        enabled_only=True,
        limit=10,
    )
    all_terminal_bindings = manager.list_event_bindings(
        event_type=EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
        limit=10,
    )
    assert not any(
        binding.workflow_id == EPISODE_EVALUATION_WORKFLOW_ID
        for binding in enabled_turn_bindings
    )
    assert not any(
        binding.workflow_id == EPISODE_EVALUATION_WORKFLOW_ID
        for binding in enabled_terminal_bindings
    )
    episode_terminal_bindings = [
        binding
        for binding in all_terminal_bindings
        if binding.workflow_id == EPISODE_EVALUATION_WORKFLOW_ID
    ]
    assert episode_terminal_bindings
    assert episode_terminal_bindings[0].enabled is False
    episode_turn_bindings = [
        binding
        for binding in all_turn_bindings
        if binding.workflow_id == EPISODE_EVALUATION_WORKFLOW_ID
    ]
    assert episode_turn_bindings
    assert episode_turn_bindings[0].enabled is False


def test_completion_gate_binding_honours_explicit_autotrigger_opt_in(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_EPISODE_EVALUATION_AUTOTRIGGER_ENABLE", "1")

    report = bootstrap_canonical_episode_evaluation_workflow()

    assert report.get("success") is True
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
        binding.workflow_id == EPISODE_EVALUATION_WORKFLOW_ID
        for binding in turn_bindings
    )
    assert not any(
        binding.workflow_id == EPISODE_EVALUATION_WORKFLOW_ID
        for binding in terminal_bindings
    )


def test_episode_prompt_support_seeds_content_from_repo_asset(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_episode_evaluation_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 4
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
