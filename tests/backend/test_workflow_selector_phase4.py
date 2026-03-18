from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

import src.backend.db.mongo_client as mongo_client_module
import src.backend.services.workflow_selection_experience as experience_module
import src.backend.services.workflow_selection_policy_service as policy_module
from src.backend.workflows import WorkflowRegistry
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)
from src.backend.workflows.workflow_selector import WorkflowSelector
from test_orchestrator_workflow_selector_routing import (
    _CapturingLLM,
    _build_orchestrator,
)
from workflow_test_support import (
    build_test_conversation_turn_registry,
    build_test_prompt_service,
)


@pytest.fixture(autouse=True)
def _reset_phase4_selection_state(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    mongo_client_module.close_connection()
    db = mongo_client_module.get_db()
    if db is not None:
        try:
            db.drop_collection("workflow_selection_experiences")
        except Exception:
            pass
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
    selection_source: str = "selector",
) -> None:
    entry = experience_module.record_selection_experience(
        turn_id=f"turn::{query}::{workflow_id}::{outcome}::{confidence_score}",
        query=query,
        candidate_workflow_ids=[CHAT_ASSISTANT_WORKFLOW_ID, TODO_REFRESH_WORKFLOW_ID],
        selected_workflow_id=workflow_id,
        verdict="rag_selected" if workflow_id else "fallback",
        selection_source=selection_source,
        selection_metadata={"selected_exploration_bonus": 0.05},
        confidence_score=confidence_score,
        reasoning="Phase 4 training example.",
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
    registry = build_test_conversation_turn_registry()
    return WorkflowSelector(
        registry=registry,
        prompt_service=build_test_prompt_service(),
    )


def _build_rag_first_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    orchestrator._workflow_selector = WorkflowSelector(
        registry=orchestrator._workflow_registry,
        prompt_service=build_test_prompt_service(),
        classifier_prompt_ids=orchestrator._TURN_SELECTOR_PROMPTS,
        default_workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
    )
    return orchestrator


def test_selection_experience_finalisation_persists_outcome_and_reward() -> None:
    entry = experience_module.record_selection_experience(
        turn_id="turn-phase4-persist",
        query="Refresh my Jira todo list",
        candidate_workflow_ids=[CHAT_ASSISTANT_WORKFLOW_ID, TODO_REFRESH_WORKFLOW_ID],
        selected_workflow_id=TODO_REFRESH_WORKFLOW_ID,
        verdict="policy_selected",
        selection_source="policy_direct",
        selection_metadata={"selected_exploration_bonus": 0.12},
        confidence_score=0.91,
        reasoning="Historical reward prior strongly favours the Jira refresh workflow.",
        model_name="policy::unit-test",
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
    assert snapshot["aggregates"]["policy_direct"] == 1


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
                "turn_text": "Refresh my Jira todo list",
                "candidate_workflows": [
                    {"concept_id": CHAT_ASSISTANT_WORKFLOW_ID, "name": "Chat assistant"},
                    {"concept_id": TODO_REFRESH_WORKFLOW_ID, "name": "Todo refresh"},
                ],
                "baseline_workflow_id": CHAT_ASSISTANT_WORKFLOW_ID,
                "expected_workflow_id": TODO_REFRESH_WORKFLOW_ID,
            },
            {
                "turn_text": "Refresh my Jira tasks",
                "candidate_workflows": [
                    {"concept_id": CHAT_ASSISTANT_WORKFLOW_ID, "name": "Chat assistant"},
                    {"concept_id": TODO_REFRESH_WORKFLOW_ID, "name": "Todo refresh"},
                ],
                "baseline_workflow_id": CHAT_ASSISTANT_WORKFLOW_ID,
                "expected_workflow_id": TODO_REFRESH_WORKFLOW_ID,
            },
            {
                "turn_text": "Hello there",
                "candidate_workflows": [
                    {"concept_id": TODO_REFRESH_WORKFLOW_ID, "name": "Todo refresh"},
                    {"concept_id": CHAT_ASSISTANT_WORKFLOW_ID, "name": "Chat assistant"},
                ],
                "baseline_workflow_id": TODO_REFRESH_WORKFLOW_ID,
                "expected_workflow_id": CHAT_ASSISTANT_WORKFLOW_ID,
            },
            {
                "turn_text": "Say hello to the user",
                "candidate_workflows": [
                    {"concept_id": TODO_REFRESH_WORKFLOW_ID, "name": "Todo refresh"},
                    {"concept_id": CHAT_ASSISTANT_WORKFLOW_ID, "name": "Chat assistant"},
                ],
                "baseline_workflow_id": TODO_REFRESH_WORKFLOW_ID,
                "expected_workflow_id": CHAT_ASSISTANT_WORKFLOW_ID,
            },
        ],
    )

    assert evaluation["case_count"] == 4
    assert evaluation["baseline_accuracy"] == 0.0
    assert evaluation["policy_accuracy"] == 1.0
    assert evaluation["accuracy_improvement"] > 0.0


def test_selector_uses_policy_guidance_for_candidate_ordering_and_direct_selection() -> None:
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

    selector = _build_selector()
    prompt = selector.prepare_selection_prompt(
        turn_text="Refresh my Jira todo list",
        discovered_workflows=[
            {"concept_id": CHAT_ASSISTANT_WORKFLOW_ID, "name": "Chat assistant"},
            {"concept_id": TODO_REFRESH_WORKFLOW_ID, "name": "Todo refresh"},
        ],
    )

    assert prompt.discovered_workflow_ids[0] == TODO_REFRESH_WORKFLOW_ID
    assert prompt.policy_recommendation["policy_active"] is True
    assert prompt.policy_recommendation["guidance_mode"] == "direct"
    assert (
        prompt.policy_recommendation["recommended_workflow_id"]
        == TODO_REFRESH_WORKFLOW_ID
    )

    resolved = selector.resolve_policy_selection(
        workflow_id=prompt.policy_recommendation["recommended_workflow_id"],
        prompt_id=prompt.prompt_id,
        prompt_used=prompt.prompt_text,
        discovered_workflow_ids=prompt.discovered_workflow_ids,
        confidence_score=prompt.policy_recommendation["confidence_score"],
        reasoning=prompt.policy_recommendation["reasoning"],
        selection_metadata=prompt.policy_recommendation,
    )
    assert resolved.workflow_id == TODO_REFRESH_WORKFLOW_ID
    assert resolved.verdict == "policy_selected"
    assert resolved.selection_source == "policy_direct"


def test_orchestrator_can_route_via_direct_policy_without_selector_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _ in range(4):
        _record_completed_example(
            query="Refresh my Jira todo list",
            workflow_id=TODO_REFRESH_WORKFLOW_ID,
            outcome="completed",
            confidence_score=0.95,
            duration_ms=780,
            total_tokens=620,
        )
        _record_completed_example(
            query="Refresh my Jira todo list",
            workflow_id=TOOL_CALLING_WORKFLOW_ID,
            outcome="failed",
            confidence_score=0.86,
            duration_ms=4200,
            total_tokens=2400,
        )

    snapshot = policy_module.refresh_live_selection_policy()
    assert snapshot is not None

    orchestrator = _build_rag_first_orchestrator(monkeypatch)
    llm = _CapturingLLM([])
    discovery_result = {
        "matches": [
            {
                "concept_id": TOOL_CALLING_WORKFLOW_ID,
                "name": "Tool calling workflow",
                "description": "General tool pipeline.",
                "is_executable": True,
                "executability_reason": "executable_now",
            },
            {
                "concept_id": TODO_REFRESH_WORKFLOW_ID,
                "name": "Todo refresh workflow",
                "description": "Refreshes the user's Jira todo list.",
                "is_executable": True,
                "executability_reason": "executable_now",
            },
        ],
        "match_count": 2,
    }

    result = orchestrator.run(
        prompt="Refresh my Jira todo list",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
    )

    assert llm.calls == []
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TODO_REFRESH_WORKFLOW_ID
    assert result.workflow_routing.source == "policy_direct"

    selector_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
        ),
        None,
    )
    assert selector_entry is not None
    assert selector_entry.get("selection_source") == "policy_direct"
    assert selector_entry.get("policy_guidance_mode") == "direct"

    recent = experience_module.get_recent_experiences(limit=1)
    assert recent[0]["selected_workflow_id"] == TODO_REFRESH_WORKFLOW_ID
    assert recent[0]["outcome"] == "completed"
