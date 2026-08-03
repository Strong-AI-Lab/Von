from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.backend.services import (
    chat_history_service,
)
from src.backend.services import (
    conversation_activity_projection_service as projection_service,
)


@pytest.fixture(autouse=True)
def _reset_chat_history_read_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chat_history_service,
        "_CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC",
        0.0,
    )
    monkeypatch.setattr(
        chat_history_service,
        "_CHAT_HISTORY_READ_CIRCUIT_LAST_ERROR",
        None,
    )


def _install_collection(monkeypatch: pytest.MonkeyPatch, document: dict):
    import mongomock

    collection = mongomock.MongoClient().von_test.chat_history
    collection.insert_one(document)
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(
        chat_history_service,
        "build_mongo_operation_comment",
        lambda **_kwargs: None,
    )
    return collection


def _project(**overrides):
    arguments = {
        "actor_user_id": "#V#user",
        "history_owner_user_id": "#V#user",
        "session_id": "session-activity",
        "namespace": "#V#user@org",
        "request_id": "request-activity",
        "objective": "Represent the six papers selected in the mail scan.",
        "workflow_id": "#V#arxiv_paper_representation_workflow",
        "instance_id": "instance-activity",
        "activity_status": "running",
        "milestone": "item_completed",
        "originated_at_utc": "2026-08-01T18:00:00Z",
        "observed_at_utc": "2026-08-01T18:03:00Z",
        "progress_current": 2,
        "progress_total": 6,
        "progress_message": "Representing paper 3 of 6",
        "represented_progress_facts": [
            {
                "schema_version": "workflow_progress_projection.v1",
                "fact_id": "arxiv_id",
                "label": "arXiv id",
                "status": "available",
                "visibility": "default",
                "value": "2607.25308",
            }
        ],
    }
    arguments.update(overrides)
    return projection_service.project_durable_workflow_activity(**arguments)


def test_generic_turn_activity_projects_without_pseudo_workflow_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _install_collection(
        monkeypatch,
        {
            "user_id": "#V#user",
            "session_id": "session-activity",
            "namespace": "#V#user@org",
            "history": [],
        },
    )

    outcome = projection_service.project_conversation_turn_activity(
        actor_user_id="#V#user",
        history_owner_user_id="#V#user",
        session_id="session-activity",
        namespace="#V#user@org",
        request_id="request-direct-turn",
        activity_id="turn-request-direct-turn",
        objective="Work out why the conversation situation is empty.",
        activity_status="running",
        milestone="tool_batch_completed",
        originated_at_utc="2026-08-01T18:00:00Z",
        observed_at_utc="2026-08-01T18:03:00Z",
        progress_current=42,
        progress_message="Inspecting the evidence from completed tool calls",
    )

    assert outcome["updated"] is True
    assert outcome["activity_id"] == "turn-request-direct-turn"
    assert "workflow_id" not in outcome
    assert "instance_id" not in outcome
    stored = collection.find_one({"session_id": "session-activity"})
    assert stored is not None
    observation = stored["conversation_observations"][0]
    assert observation["kind"] == "conversation_turn_activity_milestone"
    assert observation["activity_id"] == "turn-request-direct-turn"
    assert "workflow_id" not in observation
    assert "instance_id" not in observation
    situation_text = stored["conversation_situation"]["text"]
    assert "Turn activity: running (progress 42)." in situation_text
    assert "Activity turn-request-direct-turn." in situation_text
    assert "Inspecting the evidence" in situation_text


def test_activity_milestone_initialises_nonempty_reducer_owned_situation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _install_collection(
        monkeypatch,
        {
            "user_id": "#V#user",
            "session_id": "session-activity",
            "namespace": "#V#user@org",
            "history": [],
        },
    )

    outcome = _project()
    duplicate = _project(observed_at_utc="2026-08-01T18:04:00Z")

    assert outcome["updated"] is True
    assert outcome["observation"]["updated"] is True
    assert outcome["situation"]["updated"] is True
    assert duplicate["observation"]["duplicate"] is True
    assert duplicate["situation"]["reason"] == "conversation_situation_unchanged"

    stored = collection.find_one({"session_id": "session-activity"})
    assert stored is not None
    assert stored["conversation_observation_total"] == 1
    assert len(stored["conversation_observations"]) == 1
    observation = stored["conversation_observations"][0]
    assert observation["kind"] == "durable_workflow_milestone"
    assert observation["progress_current"] == 2
    assert observation["progress_total"] == 6
    assert observation["progress_summary"] == "arXiv id: 2607.25308"

    situation = stored["conversation_situation"]
    assert situation["source"] == "conversation_activity_projection"
    assert situation["revision"] == 1
    assert "Represent the six papers" in situation["text"]
    assert "2 of 6" in situation["text"]
    assert "2607.25308" in situation["text"]


def test_activity_milestones_advance_reducer_owned_situation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _install_collection(
        monkeypatch,
        {
            "user_id": "#V#user",
            "session_id": "session-activity",
            "namespace": "#V#user@org",
            "history": [],
        },
    )
    first = _project(
        milestone="submitted",
        progress_current=0,
        progress_message="Queued six papers",
        represented_progress_facts=[],
    )
    second = _project(
        milestone="item_completed",
        progress_current=1,
        progress_message="Representing paper 2 of 6",
        represented_progress_facts=[
            {
                "fact_id": "paper_concept",
                "label": "Paper concept",
                "status": "available",
                "visibility": "expert",
                "value": "#V#paper_on_arxiv_2607_15776",
            }
        ],
    )

    assert first["situation"]["updated"] is True
    assert second["situation"]["updated"] is True
    stored = collection.find_one({"session_id": "session-activity"})
    assert stored is not None
    assert stored["conversation_situation"]["revision"] == 2
    assert "1 of 6" in stored["conversation_situation"]["text"]
    assert "paper_on_arxiv_2607_15776" in stored["conversation_situation"]["text"]
    assert stored["conversation_observation_total"] == 2


def test_activity_situation_prefers_running_then_newest_terminal_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _install_collection(
        monkeypatch,
        {
            "user_id": "#V#user",
            "session_id": "session-activity",
            "namespace": "#V#user@org",
            "history": [],
        },
    )

    _project(
        request_id="request-a26",
        instance_id="activity-a26",
        objective="Process Supervisors A26:A29",
        activity_status="running",
        milestone="started",
    )
    _project(
        request_id="request-a26",
        instance_id="activity-a26",
        objective="Process Supervisors A26:A29",
        activity_status="failed",
        milestone="failed",
    )
    _project(
        request_id="request-a2",
        instance_id="activity-a2",
        objective="Process Supervisors A2:E14",
        activity_status="running",
        milestone="started",
    )

    stored = collection.find_one({"session_id": "session-activity"})
    assert stored is not None
    running_text = stored["conversation_situation"]["text"]
    assert "Process Supervisors A2:E14" in running_text
    assert "Process Supervisors A26:A29" not in running_text

    _project(
        request_id="request-a2",
        instance_id="activity-a2",
        objective="Process Supervisors A2:E14",
        activity_status="completed",
        milestone="completed",
    )
    stored = collection.find_one({"session_id": "session-activity"})
    assert stored is not None
    terminal_text = stored["conversation_situation"]["text"]
    assert "Process Supervisors A2:E14" in terminal_text
    assert "Process Supervisors A26:A29" not in terminal_text


def test_activity_situation_provenance_follows_remaining_overlapping_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _install_collection(
        monkeypatch,
        {
            "user_id": "#V#user",
            "session_id": "session-activity",
            "namespace": "#V#user@org",
            "history": [],
        },
    )

    _project(
        request_id="request-a",
        instance_id="activity-a",
        objective="Process batch A",
        activity_status="running",
        milestone="started",
    )
    _project(
        request_id="request-b",
        instance_id="activity-b",
        objective="Process batch B",
        activity_status="running",
        milestone="started",
    )
    _project(
        request_id="request-a",
        instance_id="activity-a",
        objective="Process batch A",
        activity_status="completed",
        milestone="completed",
    )

    stored = collection.find_one({"session_id": "session-activity"})
    assert stored is not None
    situation = stored["conversation_situation"]
    assert "Process batch B" in situation["text"]
    assert "Process batch A" not in situation["text"]
    assert situation["source_request_id"] == "request-b"


def test_activity_observation_never_overwrites_model_authored_situation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_text = "The model-authored shared situation remains authoritative here."
    collection = _install_collection(
        monkeypatch,
        {
            "user_id": "#V#user",
            "session_id": "session-activity",
            "namespace": "#V#user@org",
            "history": [],
            "conversation_situation": {
                "text": existing_text,
                "revision": 4,
                "source": "adaptive_turn",
                "updated_by": "#V#user",
                "updated_at": datetime(2026, 8, 1, 17, 0, tzinfo=UTC),
            },
        },
    )

    outcome = _project()

    assert outcome["observation"]["updated"] is True
    assert outcome["situation"] == {
        "updated": False,
        "reason": "conversation_situation_not_reducer_owned",
        "preserved": True,
    }
    stored = collection.find_one({"session_id": "session-activity"})
    assert stored is not None
    assert stored["conversation_situation"]["text"] == existing_text
    assert stored["conversation_situation"]["revision"] == 4
    assert len(stored["conversation_observations"]) == 1


def test_activity_originating_before_reset_cannot_repopulate_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _install_collection(
        monkeypatch,
        {
            "user_id": "#V#user",
            "session_id": "session-activity",
            "namespace": "#V#user@org",
            "history": [],
            "conversation_situation": {
                "revision": 3,
                "source": "conversation_reset",
                "updated_by": "#V#user",
                "updated_at": datetime(2026, 8, 1, 18, 2, tzinfo=UTC),
            },
        },
    )

    outcome = _project(
        originated_at_utc="2026-08-01T18:00:00Z",
        observed_at_utc="2026-08-01T18:05:00Z",
    )

    assert outcome == {
        "updated": False,
        "reason": "conversation_reset_after_activity_origin",
        "reset_guarded": True,
    }
    stored = collection.find_one({"session_id": "session-activity"})
    assert stored is not None
    assert "conversation_observations" not in stored
    assert "text" not in stored["conversation_situation"]


def test_new_post_reset_activity_can_initialise_but_shared_actor_facts_do_not_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _install_collection(
        monkeypatch,
        {
            "user_id": "#V#owner",
            "session_id": "session-activity",
            "namespace": "#V#owner@org",
            "history": [],
            "conversation_situation": {
                "revision": 3,
                "source": "conversation_reset",
                "updated_by": "#V#owner",
                "updated_at": datetime(2026, 8, 1, 18, 2, tzinfo=UTC),
            },
        },
    )

    outcome = _project(
        actor_user_id="#V#invitee",
        history_owner_user_id="#V#owner",
        namespace="#V#owner@org",
        originated_at_utc="2026-08-01T18:03:00Z",
        represented_progress_facts=[
            {
                "fact_id": "private_mail_subject",
                "label": "Mail subject",
                "status": "available",
                "visibility": "default",
                "value": "Private collaborator message",
            }
        ],
    )

    assert outcome["updated"] is True
    stored = collection.find_one({"session_id": "session-activity"})
    assert stored is not None
    assert stored["conversation_situation"]["revision"] == 4
    assert (
        "Private collaborator message" not in stored["conversation_situation"]["text"]
    )
    assert "progress_summary" not in stored["conversation_observations"][0]
