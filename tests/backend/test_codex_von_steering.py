"""Scoped canonical inbox selection and durable ambiguity; no external stores."""

import copy
import json
from types import SimpleNamespace

import mongomock
import pytest

pytest.importorskip("fcntl", reason="DGX controller uses a Unix lock")

from scripts import codex_von_inbox as inbox
from scripts import codex_von_steering as steering


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    config = {
        "agent_id": "#V#agent",
        "delegator_id": "#V#owner",
        "organisation_id": "#V#org",
        "inbox_enabled": True,
        "state_root": str(tmp_path),
        "inbox_since": "2026-01-01T00:00:00Z",
    }
    state = {
        "task_id": "#V#task",
        "attempt": "attempt-1",
        "run_dir": str(tmp_path / "run"),
        "started_at": "2026-09-17T00:00:00Z",
    }
    rows = []
    task = {
        "task_concept_id": "#V#task",
        "status": "in_progress",
        "created_by_concept_id": "#V#owner",
        "assignee_concept_id": "#V#agent",
        "organisation_concept_id": "#V#org",
    }
    db = mongomock.MongoClient().steering_tests
    monkeypatch.setattr(
        "src.backend.services.task_dispatch_authority_service.get_db", lambda: db
    )
    api = SimpleNamespace(
        task=lambda _: task,
        native_writer=lambda _: True,
        messages=SimpleNamespace(
            get_messages_for_user=lambda *a, **kw: copy.deepcopy(rows),
            project_direct_message=lambda row: row,
            get_message_for_user=lambda mid, _: next(
                row for row in rows if row["message_id"] == mid
            ),
            authorise_direct_message_participants=lambda **kw: (True, []),
        ),
    )
    active = steering.ActiveInbox(config, api, state, lambda: None)
    active.active("thread-1", "turn-1")
    message = {
        "message_id": "#V#source",
        "sender_id": "#V#owner",
        "recipient_ids": ["#V#agent"],
        "organisation_concept_id": "#V#org",
        "content": "Please use the existing tests.",
        "sent_at": "2026-09-17T00:01:00Z",
        "submit_mode": "steer",
        "submit_target": copy.deepcopy(active.binding),
    }
    rows.append(message)
    return active, rows, task


def test_exact_reservation_survives_restart_without_redelivery_and_queue_stays_separate(
    receiver,
):
    active, rows, _ = receiver
    queued = {**rows[0], "message_id": "#V#queue", "submit_mode": "queue"}
    rows.append(queued)
    selected = list(active.pending())
    assert len(selected) == 1
    assert selected[0]["message_id"] == "#V#source"
    path = inbox.state_path(active.config, "#V#source")
    retained = json.loads(path.read_text())
    assert retained["steering_delivery"]["status"] == "reserved"
    assert not inbox.state_path(active.config, "#V#queue").exists()
    assert not list(active.pending())
    restarted = steering.ActiveInbox(
        active.config, active.api, active.state, lambda: None
    )
    restarted.active("thread-2", "turn-2")
    assert not list(restarted.pending())
    steering.recover_delivery(retained)
    assert retained["steering_delivery"]["status"] == "uncertain"
    assert retained["result"]["action"] == "reply"
    assert "not been retried or queued" in retained["result"]["answer"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("sender_id", "#V#stranger"),
        ("recipient_ids", ["#V#other_agent"]),
        ("organisation_concept_id", "#V#other_org"),
        ("submit_target", None),
        ("sent_at", "2026-09-16T23:59:00Z"),
    ],
)
def test_wrong_scope_missing_binding_or_old_source_cannot_steer(receiver, field, value):
    active, rows, _ = receiver
    rows[0][field] = value
    assert not list(active.pending())
    assert not inbox.state_path(active.config, "#V#source").exists()


@pytest.mark.parametrize(
    "field",
    ["task_id", "attempt", "thread_id", "turn_id", "agent_id", "organisation_id"],
)
def test_each_target_component_is_checked(receiver, field):
    active, rows, _ = receiver
    rows[0]["submit_target"][field] = "replacement"
    assert not list(active.pending())


@pytest.mark.parametrize(
    "field,value", [("status", "cancelled"), ("assignee_concept_id", "#V#other")]
)
def test_task_cancellation_or_reassignment_prevents_dispatch(receiver, field, value):
    active, _, task = receiver
    task[field] = value
    assert not list(active.pending())


def test_source_target_is_read_back_before_reservation(receiver):
    active, rows, _ = receiver
    original = active.api.messages.get_message_for_user
    active.api.messages.get_message_for_user = lambda *a: {
        **original(*a),
        "submit_target": None,
    }
    assert not list(active.pending())
    assert active.state["steering_poll_error"]["type"] == "PermissionError"
    assert not inbox.state_path(active.config, rows[0]["message_id"]).exists()


def test_ack_is_truthful_and_receipt_recovery_does_not_start_a_model(
    receiver, monkeypatch
):
    active, rows, _ = receiver
    message = list(active.pending())[0]
    active.delivered(message, "accepted")
    path = inbox.state_path(active.config, rows[0]["message_id"])
    retained = json.loads(path.read_text())
    assert "consumption has not been verified" in retained["result"]["answer"]
    monkeypatch.setattr(inbox, "launch", lambda *a: pytest.fail("guidance relaunched"))
    finished = []
    monkeypatch.setattr(
        inbox,
        "finish_or_retain",
        lambda *args: (finished.append(args[2]), args[2].update(phase="done")),
    )
    assert inbox.tick(active.config, active.api, 0)
    assert finished[0]["steering_delivery"]["status"] == "accepted"


def test_observation_failure_does_not_end_the_owned_turn(receiver):
    active, _, _ = receiver

    def unavailable(*a, **kw):
        raise ConnectionError("private details must not be retained")

    active.api.messages.get_messages_for_user = unavailable
    assert not list(active.pending())
    assert active.state["active_turn"] == active.binding
    assert active.state["steering_poll_error"]["type"] == "ConnectionError"
    assert "private details" not in json.dumps(active.state)
