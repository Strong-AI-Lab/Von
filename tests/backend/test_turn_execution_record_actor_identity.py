from __future__ import annotations

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

