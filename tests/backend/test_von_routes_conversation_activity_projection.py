from __future__ import annotations

from typing import Any

from src.backend.server.routes import von_routes


def test_material_activity_projection_ignores_non_material_progress() -> None:
    for payload in (
        {"status": "thinking", "phase": "context_build"},
        {"status": "heartbeat", "tool": "create_concepts"},
        {"status": "phase_transition", "phase": "tool_execute"},
        {"status": "llm_call_start", "phase": "model_call"},
        {
            "status": "tool_started",
            "event_kind": "tool_call_start",
            "tool": "create_concepts",
        },
    ):
        assert von_routes._material_conversation_activity_from_progress(payload) is None


def test_material_activity_projection_selects_tool_milestone_without_workflow() -> None:
    projection = von_routes._material_conversation_activity_from_progress(
        {
            "status": "tool_invoked",
            "tool": "create_concepts",
            "call_id": "call-42",
            "tool_calls_done": 42,
            "result_summary": "Concept creation result was recorded.",
        }
    )

    assert projection == {
        "activity_status": "running",
        "milestone": "tool_call_end",
        "progress_current": 42,
        "progress_total": None,
        "progress_message": "Concept creation result was recorded.",
        "represented_progress_facts": None,
        "workflow_id": None,
        "instance_id": None,
    }


def test_material_activity_projection_selects_real_workflow_metadata() -> None:
    projection = von_routes._material_conversation_activity_from_progress(
        {
            "status": "tool_running",
            "event_kind": "workflow_instance_status",
            "stage": "workflow_execution",
            "tool": "workflow_execute",
            "workflow_id": "#V#paper_representation_workflow",
            "workflow_instance_id": "instance-123",
            "workflow_progress": {
                "current": 2,
                "total": 6,
                "message": "Working on paper 3 of 6",
            },
            "progress_facts": [
                {
                    "fact_id": "paper_id",
                    "label": "Paper",
                    "status": "available",
                    "value": "2607.25308",
                }
            ],
        }
    )

    assert projection is not None
    assert projection["milestone"] == "workflow_instance_status"
    assert projection["progress_current"] == 2
    assert projection["progress_total"] == 6
    assert projection["workflow_id"] == "#V#paper_representation_workflow"
    assert projection["instance_id"] == "instance-123"
    assert projection["represented_progress_facts"][0]["value"] == "2607.25308"


def test_42_tool_turn_uses_logarithmic_conversation_carrier_checkpoints() -> None:
    seen: set[tuple[str, str, int, int | None]] = set()
    projected_counts: list[int] = []

    for completed_count in range(42):
        assert (
            von_routes._material_conversation_activity_from_progress(
                {
                    "status": "tool_started",
                    "event_kind": "tool_call_start",
                    "tool": "create_concepts",
                    "tool_calls_done": completed_count,
                }
            )
            is None
        )
        activity = von_routes._material_conversation_activity_from_progress(
            {
                "status": "tool_completed",
                "event_kind": "tool_call_end",
                "tool": "create_concepts",
                "tool_calls_done": completed_count + 1,
                "result_summary": "Concept creation result was recorded.",
            }
        )
        assert activity is not None
        checkpoint_key = von_routes._coarse_conversation_activity_checkpoint_key(
            activity
        )
        if checkpoint_key is not None and checkpoint_key not in seen:
            seen.add(checkpoint_key)
            projected_counts.append(completed_count + 1)

    assert projected_counts == [1, 2, 4, 8, 16, 32]
    # Together with the explicit start and terminal observations, a 42-tool turn
    # performs eight carrier projections rather than 86 start/end projections.
    assert len(projected_counts) + 2 == 8


def test_work_item_start_is_never_a_conversation_carrier_checkpoint() -> None:
    assert (
        von_routes._coarse_conversation_activity_checkpoint_key(
            {
                "milestone": "workflow_for_each_item_start",
                "progress_current": 32,
                "progress_total": 42,
                "workflow_id": "#V#mail_scan",
                "instance_id": "instance-42",
            }
        )
        is None
    )


def test_model_situation_retries_over_same_request_reducer_state(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_set(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return {
                "updated": False,
                "conflict": True,
                "current_revision": 7,
                "conversation_situation": {
                    "text": "Background turn is running.",
                    "revision": 7,
                    "source": "conversation_activity_projection",
                    "source_request_id": "request-123",
                },
            }
        return {"updated": True, "conflict": False, "current_revision": 8}

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "set_chat_history_conversation_situation",
        fake_set,
    )

    von_routes._persist_conversation_situation_fail_soft(
        user_id="#V#user",
        session_id="session-123",
        namespace="#V#user@org",
        previous_text="Prior situation.",
        updated_text="Model-authored updated situation.",
        expected_revision=3,
        updated_by="#V#user",
        request_id="request-123",
    )

    assert [call["expected_revision"] for call in calls] == [3, 7]
    assert calls[1]["source"] == "adaptive_turn"
    assert calls[1]["source_request_id"] == "request-123"


def test_model_situation_never_retries_over_another_request(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_set(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "updated": False,
            "conflict": True,
            "current_revision": 7,
            "conversation_situation": {
                "text": "Another turn is running.",
                "revision": 7,
                "source": "conversation_activity_projection",
                "source_request_id": "request-other",
            },
        }

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "set_chat_history_conversation_situation",
        fake_set,
    )

    von_routes._persist_conversation_situation_fail_soft(
        user_id="#V#user",
        session_id="session-123",
        namespace="#V#user@org",
        previous_text="Prior situation.",
        updated_text="Model-authored updated situation.",
        expected_revision=3,
        updated_by="#V#user",
        request_id="request-123",
    )

    assert len(calls) == 1


def test_current_activity_projection_replaces_stale_failed_objective() -> None:
    prior_situation = {
        "text": "Current conversation activity:\n- Objective: Process A26",
        "revision": 4,
        "source": "conversation_activity_projection",
        "source_request_id": "request-a26",
    }
    prior_observations = [
        {
            "kind": "conversation_turn_activity_milestone",
            "observation_id": "a26-started",
            "activity_id": "activity-a26",
            "activity_status": "running",
            "milestone": "started",
            "objective": "Process A26",
        },
        {
            "kind": "conversation_turn_activity_milestone",
            "observation_id": "a26-failed",
            "activity_id": "activity-a26",
            "activity_status": "failed",
            "milestone": "failed",
            "objective": "Process A26",
        },
    ]
    current_observation = {
        "kind": "conversation_turn_activity_milestone",
        "observation_id": "a2-started",
        "activity_id": "activity-a2",
        "activity_status": "running",
        "milestone": "started",
        "objective": "Process A2",
    }
    current_situation = {
        "text": "Current conversation activity:\n- Objective: Process A2",
        "revision": 5,
        "source": "conversation_activity_projection",
        "source_request_id": "request-a2",
    }

    descriptor, text, revision, observations = (
        von_routes._prepare_current_activity_model_snapshot(
            prior_situation=prior_situation,
            prior_observations=prior_observations,
            current_projection={
                "projected_observation": current_observation,
                "situation": {"conversation_situation": current_situation},
            },
            request_id="request-a2",
        )
    )

    assert descriptor == current_situation
    assert text == current_situation["text"]
    assert revision == 5
    assert observations == [
        {
            "kind": "conversation_turn_activity_milestone",
            "observation_id": "a26-failed",
            "activity_id": "activity-a26",
            "activity_status": "failed",
            "milestone": "failed",
        },
        current_observation,
    ]
