"""Independent task launches retain source authority without its turn fence."""

from unittest.mock import MagicMock

import pytest

from src.backend.services import task_execution_service as execution
from src.backend.services import task_execution_submission_service as submission
from src.backend.services.conversation_turn_admission_service import (
    build_conversation_key,
)

ACTOR = {
    "actor_concept_id": "#V#alice",
    "organisation_concept_id": "#V#research_lab",
    "namespace": "#V#alice@research_lab",
}
TASK = {
    "task_concept_id": "#V#task_inquiry",
    "title": "Investigate a research question",
    "description": "Produce a sourced work product.",
    "status": "pending",
    "assignee_concept_id": "#V#von_system",
    "created_by_concept_id": "#V#alice",
    "organisation_concept_id": "#V#research_lab",
    "originating_conversation_id": "#V#conversation_source",
    "conversation_session_id": "source-session",
}


@pytest.fixture
def launch(monkeypatch):
    monkeypatch.setattr(submission, "get_task", lambda _: dict(TASK))
    monkeypatch.setattr(
        submission,
        "get_conversation_concept",
        lambda _: {
            "session_id": "source-session",
            "owner_concept_id": "#V#alice",
            "organisation_concept_id": "#V#research_lab",
            "namespace": "#V#alice@research_lab",
        },
    )
    monkeypatch.setattr(
        submission.chat_history_service,
        "has_chat_history_session",
        lambda *a, **kw: True,
    )
    create_session = MagicMock()
    monkeypatch.setattr(
        submission.chat_history_service, "create_chat_session", create_session
    )
    rows = {}

    def enqueue(**kwargs):
        key = kwargs["enqueue_submission_id"]
        replayed = key in rows
        if not replayed:
            rows[key] = {
                **kwargs,
                **kwargs["scope"],
                "queue_id": "queue-1",
                "dispatch_ready": True,
            }
        return {**rows[key], "idempotent_replay": replayed}

    monkeypatch.setattr(
        submission.chat_prompt_queue_service, "create_queue_record", enqueue
    )
    monkeypatch.setattr(submission, "wake_chat_prompt_queue_dispatcher", MagicMock())
    # Keep real launch/outbox authority reconciliation; replace persistence only.
    monkeypatch.setattr(
        "src.backend.services.task_management_service.get_task", lambda _: dict(TASK)
    )
    create_execution = MagicMock()
    monkeypatch.setattr(execution, "create_task_execution", create_execution)
    monkeypatch.setattr(
        execution,
        "bind_task_execution_queue_record",
        lambda execution_id, **kw: {"task_execution_concept_id": execution_id},
    )
    return rows, create_session, create_execution


def submit(*, independent=True, **payload):
    return submission.submit_task_execution(
        TASK["task_concept_id"],
        actor_context=ACTOR,
        payload={"launch_request_id": "source-turn-inquiry", **payload},
        independent=independent,
    )


def test_independent_launch_keeps_source_but_uses_own_history_and_fence(launch):
    rows, create_session, create_execution = launch
    result, status = submit()
    assert status == 201
    row = next(iter(rows.values()))
    inputs = row["execution_envelope"]["workflow_inputs"]
    assert row["session_id"] != TASK["conversation_session_id"]
    assert inputs["conversation_session_id"] == row["session_id"]
    assert inputs["source_conversation_session_id"] == TASK["conversation_session_id"]
    assert (
        inputs["originating_conversation_concept_id"]
        == TASK["originating_conversation_id"]
    )
    assert row["conversation_key"] != build_conversation_key(
        owner_user_id=ACTOR["actor_concept_id"],
        history_namespace=ACTOR["namespace"],
        conversation_session_id=TASK["conversation_session_id"],
    )
    assert create_session.call_args.kwargs["session_id"] == row["session_id"]
    assert create_session.call_args.kwargs["namespace"] == ACTOR["namespace"]
    assert create_session.call_args.kwargs["role_in_org"] is None
    assert "history" not in create_session.call_args.kwargs
    assert result["task"]["conversation_session_id"] == TASK["conversation_session_id"]
    assert (
        create_execution.call_args.kwargs["task"]["conversation_session_id"]
        == row["session_id"]
    )


def test_independent_retry_reuses_carrier_queue_and_execution(launch):
    rows, create_session, create_execution = launch
    first, _ = submit()
    second, status = submit()
    assert status == 200
    assert second["idempotent_replay"] is True
    assert len(rows) == 1
    assert first["task_execution"] == second["task_execution"]
    assert create_session.call_args_list[0] == create_session.call_args_list[1]


def test_request_payload_cannot_choose_execution_carrier(launch):
    rows, create_session, _ = launch
    submit(
        independent=False,
        task_execution_context="independent",
        session_id="another-session",
    )
    row = next(iter(rows.values()))
    assert row["session_id"] == TASK["conversation_session_id"]
    create_session.assert_not_called()


def test_independent_launch_rejects_other_source_owner_before_creating_carrier(
    launch, monkeypatch
):
    _, create_session, _ = launch
    monkeypatch.setattr(
        submission, "get_conversation_concept", lambda _: {"owner_concept_id": "#V#bob"}
    )
    with pytest.raises(execution.TaskExecutionAccessError, match="different actor"):
        submit()
    create_session.assert_not_called()


@pytest.mark.parametrize(
    "tamper", ["execution_session", "source_session", "actor", "organisation"]
)
def test_outbox_rechecks_carrier_and_source_binding(launch, monkeypatch, tamper):
    rows, _, _ = launch
    submit()
    row = next(iter(rows.values()))
    inputs = row["execution_envelope"]["workflow_inputs"]
    if tamper == "execution_session":
        row["session_id"] = inputs["conversation_session_id"] = "other-private-session"
    elif tamper == "source_session":
        inputs["source_conversation_session_id"] = "different-source"
    else:
        field = (
            "created_by_concept_id" if tamper == "actor" else "organisation_concept_id"
        )
        monkeypatch.setattr(
            "src.backend.services.task_management_service.get_task",
            lambda _: {**TASK, field: "#V#other"},
        )
    with pytest.raises(execution.TaskExecutionAccessError):
        execution.reconcile_task_execution_queue_record(row)
