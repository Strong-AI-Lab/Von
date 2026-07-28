from __future__ import annotations

from collections.abc import Generator

import pytest
from workflow_test_support import (
    build_test_conversation_turn_registry,
    build_test_prompt_service,
)

import src.backend.db.mongo_client as mongo_client_module
import src.backend.services.workflow_selection_experience as experience_module
import src.backend.services.workflow_selection_policy_service as policy_module
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
)
from src.backend.workflows.workflow_selector import WorkflowSelector


@pytest.fixture(autouse=True)
def _reset_phase4_selection_state(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    mongo_client_module.close_connection()
    db = mongo_client_module.get_db()
    if db is not None:
        db.drop_collection("workflow_selection_experiences")
    experience_module.reset_selection_experience()
    policy_module.clear_live_selection_policy()
    yield
    experience_module.reset_selection_experience()
    policy_module.clear_live_selection_policy()
    mongo_client_module.close_connection()


def _record_completed_example(
    *,
    query: str,
    workflow_id: str,
    outcome: str,
    confidence_score: float = 0.9,
    duration_ms: float = 1200.0,
    total_tokens: int = 900,
) -> None:
    entry = experience_module.record_selection_experience(
        turn_id=f"turn::{query}::{workflow_id}::{outcome}::{confidence_score}",
        query=query,
        candidate_workflow_ids=[
            CHAT_ASSISTANT_WORKFLOW_ID,
            TODO_REFRESH_WORKFLOW_ID,
        ],
        selected_workflow_id=workflow_id,
        verdict="rag_selected" if workflow_id else "fallback",
        selection_source="selector",
        selection_metadata={"selected_exploration_bonus": 0.05},
        confidence_score=confidence_score,
        reasoning="Retained selector-policy training example.",
        model_name="phase4-test-model",
        routing_duration_ms=12.0,
    )
    experience_module.finalise_selection_experience(
        experience_id=entry.experience_id,
        outcome=outcome,
        outcome_metadata={
            "orchestrator_duration_ms": duration_ms,
            "total_tokens": total_tokens,
            "retry_attempts": 0,
        },
        retrain_policy=False,
    )


def _build_selector() -> WorkflowSelector:
    return WorkflowSelector(
        registry=build_test_conversation_turn_registry(),
        prompt_service=build_test_prompt_service(),
    )


def test_selection_experience_finalisation_persists_outcome_and_reward() -> None:
    entry = experience_module.record_selection_experience(
        turn_id="turn-phase4-persist",
        query="Refresh my Jira todo list",
        candidate_workflow_ids=[
            CHAT_ASSISTANT_WORKFLOW_ID,
            TODO_REFRESH_WORKFLOW_ID,
        ],
        selected_workflow_id=TODO_REFRESH_WORKFLOW_ID,
        verdict="rag_selected",
        selection_source="selector",
        selection_metadata={"selected_exploration_bonus": 0.12},
        confidence_score=0.91,
        reasoning="Selector chose the Jira refresh workflow.",
        model_name="phase4-test-model",
        routing_duration_ms=10.0,
    )

    finalised = experience_module.finalise_selection_experience(
        experience_id=entry.experience_id,
        outcome="completed",
        outcome_metadata={
            "orchestrator_duration_ms": 900,
            "total_tokens": 750,
            "retry_attempts": 1,
        },
        retrain_policy=False,
    )

    assert finalised is not None
    assert finalised.outcome == "completed"
    assert finalised.reward is not None
    assert finalised.reward > 0.5
    assert finalised.reward_breakdown["completion_signal"] > 0.0

    persisted = experience_module.list_selection_experiences(limit=5)
    assert len(persisted) == 1
    assert persisted[0].experience_id == entry.experience_id
    assert persisted[0].outcome == "completed"

    snapshot = experience_module.get_selection_experience_snapshot()
    assert snapshot["aggregates"]["total_selections"] == 1
    assert snapshot["aggregates"]["completed_count"] == 1
    assert snapshot["aggregates"]["reward_sample_count"] == 1
    assert snapshot["aggregates"]["policy_direct"] == 0


def test_policy_training_improves_held_out_benchmark_cases() -> None:
    for _ in range(4):
        _record_completed_example(
            query="Refresh my Jira todo list",
            workflow_id=TODO_REFRESH_WORKFLOW_ID,
            outcome="completed",
            confidence_score=0.92,
            duration_ms=800,
            total_tokens=650,
        )
        _record_completed_example(
            query="Refresh my Jira todo list",
            workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
            outcome="failed",
            confidence_score=0.88,
            duration_ms=3500,
            total_tokens=2200,
        )
        _record_completed_example(
            query="Hello there",
            workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
            outcome="completed",
            confidence_score=0.86,
            duration_ms=700,
            total_tokens=420,
        )
        _record_completed_example(
            query="Hello there",
            workflow_id=TODO_REFRESH_WORKFLOW_ID,
            outcome="failed",
            confidence_score=0.82,
            duration_ms=2600,
            total_tokens=1800,
        )

    snapshot = policy_module.refresh_live_selection_policy()
    assert snapshot is not None

    evaluation = policy_module.evaluate_selection_policy(
        snapshot=snapshot,
        benchmark_cases=[
            {
                "turn_text": "Refresh my Jira tasks",
                "candidate_workflows": [
                    {
                        "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                        "name": "Chat assistant",
                    },
                    {
                        "concept_id": TODO_REFRESH_WORKFLOW_ID,
                        "name": "Todo refresh",
                    },
                ],
                "baseline_workflow_id": CHAT_ASSISTANT_WORKFLOW_ID,
                "expected_workflow_id": TODO_REFRESH_WORKFLOW_ID,
            },
            {
                "turn_text": "Say hello to the user",
                "candidate_workflows": [
                    {
                        "concept_id": TODO_REFRESH_WORKFLOW_ID,
                        "name": "Todo refresh",
                    },
                    {
                        "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                        "name": "Chat assistant",
                    },
                ],
                "baseline_workflow_id": TODO_REFRESH_WORKFLOW_ID,
                "expected_workflow_id": CHAT_ASSISTANT_WORKFLOW_ID,
            },
        ],
    )

    assert evaluation["case_count"] == 2
    assert evaluation["baseline_accuracy"] == 0.0
    assert evaluation["policy_accuracy"] == 1.0
    assert evaluation["accuracy_improvement"] > 0.0


def test_selector_uses_policy_guidance_without_direct_selection() -> None:
    for _ in range(4):
        _record_completed_example(
            query="Refresh my Jira todo list",
            workflow_id=TODO_REFRESH_WORKFLOW_ID,
            outcome="completed",
            confidence_score=0.94,
            duration_ms=820,
            total_tokens=650,
        )
        _record_completed_example(
            query="Refresh my Jira todo list",
            workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
            outcome="failed",
            confidence_score=0.88,
            duration_ms=3200,
            total_tokens=2100,
        )

    snapshot = policy_module.refresh_live_selection_policy()
    assert snapshot is not None

    prompt = _build_selector().prepare_selection_prompt(
        turn_text="Refresh my Jira todo list",
        discovered_workflows=[
            {
                "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                "name": "Chat assistant",
            },
            {
                "concept_id": TODO_REFRESH_WORKFLOW_ID,
                "name": "Todo refresh",
            },
        ],
    )

    assert prompt.discovered_workflow_ids[0] == TODO_REFRESH_WORKFLOW_ID
    assert prompt.policy_recommendation["policy_active"] is True
    assert prompt.policy_recommendation["guidance_mode"] == "prompt_guidance"
    assert prompt.policy_recommendation["recommended_workflow_id"] == (
        TODO_REFRESH_WORKFLOW_ID
    )
    assert prompt.policy_recommendation["guidance_basis"] == "learned_policy"
