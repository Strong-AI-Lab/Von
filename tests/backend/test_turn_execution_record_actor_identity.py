from __future__ import annotations

from src.backend.services.chat_auxiliary_prompt_service import (
    build_applied_prompt_snapshot,
)
from src.backend.services.turn_execution_record_service import build_turn_execution_record


def _build_record(**overrides):
    payload = {
        "request_id": "req-actor-1",
        "session_id": "session-actor-1",
        "namespace": "#V#alice@test_org",
        "actor_concept_id": None,
        "user_id": "#V#alice",
        "org_id": "#V#test_org",
        "prompt_text": "test prompt",
        "response_text": "test response",
        "interaction_timestamp_utc": "2026-02-21T00:00:00Z",
        "workflow_discovery": None,
        "workflow_routing": None,
        "tool_invocations": [],
        "turn_execution_diagnostics": None,
        "aux_llm_calls": [],
    }
    payload.update(overrides)
    return build_turn_execution_record(**payload)


def test_actor_identity_prefers_explicit_actor_concept_id() -> None:
    record = _build_record(actor_concept_id="#V#github_copilot_instance")
    assert record.get("actor_concept_id") == "#V#github_copilot_instance"
    assert record.get("actor_identity_source") == "actor_concept_id"


def test_actor_identity_rejects_namespace_shaped_actor_and_uses_user_id() -> None:
    record = _build_record(
        actor_concept_id="#V#alice@test_org",
        user_id="#V#alice",
        namespace="#V#alice@test_org",
    )
    assert record.get("actor_concept_id") == "#V#alice"
    assert record.get("actor_identity_source") == "user_id"


def test_actor_identity_falls_back_to_namespace_user_component() -> None:
    record = _build_record(
        actor_concept_id=None,
        user_id=None,
        namespace="#V#fallback_user@test_org",
    )
    assert record.get("actor_concept_id") == "#V#fallback_user"
    assert record.get("actor_identity_source") == "namespace_user_component"


def test_turn_record_retains_integrity_checked_applied_prompt_snapshot() -> None:
    snapshot = build_applied_prompt_snapshot(
        user_concept_id="#V#alice",
        namespace="#V#alice@test_org",
        organisation_concept_id="#V#test_org",
        turn_id="turn-actor-1",
        behaviour_fragments=[
            {"concept_id": "#V#behaviour_prompt", "content": "Be precise."}
        ],
        narration_fragments=[],
        screen_fragments=[],
    )

    record = _build_record(
        actor_concept_id="#V#assistant_instance",
        applied_prompt_snapshot=snapshot,
    )

    assert record["applied_prompt_snapshot"] == snapshot
    assert record["applied_prompt_snapshot"]["prompts"][0]["content"] == (
        "Be precise."
    )


def test_turn_record_rejects_cross_actor_applied_prompt_snapshot() -> None:
    snapshot = build_applied_prompt_snapshot(
        user_concept_id="#V#bob",
        namespace="#V#bob@test_org",
        organisation_concept_id="#V#test_org",
        turn_id="turn-bob-1",
        behaviour_fragments=[
            {"concept_id": "#V#bob_prompt", "content": "Private Bob prompt."}
        ],
        narration_fragments=[],
        screen_fragments=[],
    )

    record = _build_record(applied_prompt_snapshot=snapshot)

    assert record["applied_prompt_snapshot"] is None
