from __future__ import annotations

import importlib

import pytest

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.services import concept_service
from src.backend.services.workflow_episode_service import (
    build_workflow_episode_stable_key,
    finalise_workflow_use_episode,
    get_workflow_episode_counts_for_workflows,
    get_workflow_usage_aggregates_for_workflows,
    list_workflow_use_episodes,
    start_workflow_use_episode,
)


@pytest.fixture(autouse=True)
def reset_episode_state(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    import src.backend.db.mongo_client as mongo_client
    from src.backend.services import workflow_episode_service

    importlib.reload(mongo_client)
    importlib.reload(workflow_episode_service)

    db = mongo_client.get_db()
    if db is not None:
        try:
            db.drop_collection("workflow_use_episodes")
            db.drop_collection("concepts")
            db.drop_collection("text_relations")
            db.drop_collection("text_values")
        except Exception:
            pass
    yield


def _ensure_workflow_concept(workflow_id: str) -> None:
    concept_service.create_concept(
        concept_id=workflow_id,
        name="Workflow Episode Test",
        instance_of_type="#V#type",
    )


def test_workflow_episode_idempotent_start_for_stable_key():
    workflow_id = "#V#workflow_episode_idempotent"
    stable_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-1",
        session_id="session-1",
        stage="tool_calling",
    )

    first = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=stable_key,
        turn_id="turn-1",
        session_id="session-1",
    )
    second = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=stable_key,
        turn_id="turn-1",
        session_id="session-1",
    )

    assert first is not None
    assert second is not None
    assert first["episode_id"] == second["episode_id"]

    episodes = list_workflow_use_episodes(workflow_id=workflow_id, limit=20)
    assert len(episodes) == 1
    assert episodes[0]["episode_id"] == first["episode_id"]

    aggregate = get_workflow_usage_aggregates_for_workflows([workflow_id])[workflow_id]
    assert aggregate["attempts"] == 1
    assert aggregate["completions"] == 0


def test_workflow_episode_aggregates_sync_to_workflow_concept():
    workflow_id = "#V#workflow_episode_aggregate_sync"
    _ensure_workflow_concept(workflow_id)

    first_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-a",
        session_id="session-a",
        stage="tool_calling",
    )
    second_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-b",
        session_id="session-a",
        stage="tool_calling",
    )

    start_first = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=first_key,
        turn_id="turn-a",
        session_id="session-a",
    )
    assert start_first is not None
    finalise_workflow_use_episode(
        workflow_id=workflow_id,
        stable_key=first_key,
        completed=False,
        terminal_stage="tool_execution",
        termination_code="tool_timeout",
        termination_detail="Tool invocation timed out",
    )

    start_second = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=second_key,
        turn_id="turn-b",
        session_id="session-a",
    )
    assert start_second is not None
    finalise_workflow_use_episode(
        workflow_id=workflow_id,
        stable_key=second_key,
        completed=True,
        terminal_stage="completed",
        termination_code="completed",
        termination_detail=None,
    )

    aggregate = get_workflow_usage_aggregates_for_workflows([workflow_id])[workflow_id]
    assert aggregate["attempts"] == 2
    assert aggregate["completions"] == 1
    assert aggregate["completion_rate"] == pytest.approx(0.5)

    concept_doc = ConceptsRepository.find_one(
        {"concept_id": workflow_id},
        projection={"concept_data.workflow_use_aggregates": 1},
    )
    assert concept_doc is not None
    usage = (
        (concept_doc.get("concept_data") or {}).get("workflow_use_aggregates") or {}
    )
    assert usage.get("attempts") == 2
    assert usage.get("completions") == 1
    assert usage.get("completion_rate") == pytest.approx(0.5)


def test_list_workflow_use_episodes_matches_equivalent_namespace_forms():
    workflow_id = "#V#workflow_episode_namespace_equivalence"
    stable_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-ns",
        session_id="session-ns",
        stage="tool_calling",
    )

    created = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=stable_key,
        namespace="#V#michael_witbrock/#V#university_of_auckland_strong_ai_lab",
        turn_id="turn-ns",
        session_id="session-ns",
    )
    assert created is not None

    # Query using the modern user@org form should still match the stored slash form.
    episodes = list_workflow_use_episodes(
        workflow_id=workflow_id,
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        limit=20,
    )
    assert len(episodes) == 1
    assert episodes[0]["episode_id"] == created["episode_id"]


def test_get_workflow_episode_counts_for_workflows_matches_namespace_equivalents():
    workflow_id = "#V#workflow_episode_counts_namespace_equivalence"
    stable_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-counts",
        session_id="session-counts",
        stage="tool_calling",
    )

    created = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=stable_key,
        namespace="#V#michael_witbrock/#V#university_of_auckland_strong_ai_lab",
        turn_id="turn-counts",
        session_id="session-counts",
    )
    assert created is not None

    counts = get_workflow_episode_counts_for_workflows(
        [workflow_id, "#V#missing_workflow_id"],
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
    )
    assert counts[workflow_id] == 1
    assert counts["#V#missing_workflow_id"] == 0
