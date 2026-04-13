from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_BUILD_EVIDENCE_ACTION_ID,
    EPISODE_EVALUATION_PERSIST_MEMORY_ACTION_ID,
    EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
    EPISODE_EVALUATION_PROMPT_LINK_PREDICATE,
    EPISODE_EVALUATION_WORKFLOW_ID,
    EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
    EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
)
from src.backend.services.episode_evaluation_workflow_vontology_service import (
    _ensure_episode_evaluation_prompt_support,
    bootstrap_canonical_episode_evaluation_workflow,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.workflows.durable.startup import get_instance_manager
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology


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
    assert counts.get("workflows_published") == 1

    definition = load_workflow_definition_from_vontology(EPISODE_EVALUATION_WORKFLOW_ID)
    assert definition is not None

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


def test_episode_prompt_support_seeds_content_from_repo_asset(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_episode_evaluation_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 1
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
