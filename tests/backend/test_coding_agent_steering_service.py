from copy import deepcopy
from datetime import UTC, datetime, timedelta

import mongomock
import pytest

from src.backend.services import coding_agent_steering_service as steering
from src.backend.services import message_service as messages


@pytest.fixture
def owner(monkeypatch):
    db = mongomock.MongoClient().steering
    actor = ["#V#agent"]
    binding = dict(
        agent_id="#V#agent",
        organisation_id="#V#org",
        task_id="#V#task",
        attempt="attempt-1",
        thread_id="thread-1",
        turn_id="turn-1",
    )
    task = dict(
        task_concept_id="#V#task",
        status="in_progress",
        assignee_concept_id="#V#agent",
        created_by_concept_id="#V#owner",
        organisation_concept_id="#V#org",
        execution_timing={
            "attempts": [
                {
                    "attempt_id": "attempt-1",
                    "actor_concept_id": "#V#agent",
                    "started_at": "2026-09-17T10:00:00Z",
                }
            ]
        },
    )
    monkeypatch.setattr(steering, "get_db", lambda: db)
    monkeypatch.setattr(steering, "get_effective_user_concept_id", lambda: actor[0])
    monkeypatch.setattr(
        "src.backend.services.task_management_service.get_task",
        lambda _: deepcopy(task),
    )
    monkeypatch.setattr(
        "src.backend.services.task_dispatch_authority_service.get_db", lambda: db
    )
    monkeypatch.setattr(
        messages, "authorise_direct_message_participants", lambda **_: (True, [])
    )
    return db, actor, binding, task


def resolve(actor):
    actor[0] = "#V#owner"
    return steering.resolve(
        sender_id="#V#owner", recipient_id="#V#agent", organisation_id="#V#org"
    )


def test_owner_publication_readback_admission_and_late_withdrawal(owner):
    db, actor, binding, _ = owner
    assert steering.publish(binding, delegator_id="#V#owner") == binding
    assert resolve(actor) == binding
    assert (
        steering.validate_submission(
            sender_id="#V#owner",
            recipient_ids=["#V#agent"],
            organisation_id="#V#org",
            target=binding,
        )
        == binding
    )
    actor[0] = "#V#agent"
    replacement = {**binding, "turn_id": "turn-2"}
    steering.publish(replacement, delegator_id="#V#owner")
    steering.withdraw(binding)
    assert resolve(actor) == replacement
    with pytest.raises(messages.DirectMessageSteeringUnavailable):
        steering.validate_submission(
            sender_id="#V#owner",
            recipient_ids=["#V#agent"],
            organisation_id="#V#org",
            target=binding,
        )


@pytest.mark.parametrize(
    "change",
    [
        "cancelled",
        "reassigned",
        "attempt_replaced",
        "ended",
        "terminal_unknown_end",
        "expired",
        "wrong_sender",
        "wrong_org",
    ],
)
def test_no_admission_after_owner_or_scope_changes(owner, change):
    db, actor, binding, task = owner
    steering.publish(binding, delegator_id="#V#owner")
    if change == "cancelled":
        task["status"] = "cancelled"
    elif change == "reassigned":
        task["assignee_concept_id"] = "#V#other"
    elif change == "attempt_replaced":
        task["execution_timing"]["attempts"].append({"attempt_id": "attempt-2"})
    elif change == "ended":
        task["execution_timing"]["attempts"][0]["ended_at"] = "2026-09-17T10:01:00Z"
    elif change == "terminal_unknown_end":
        task["execution_timing"]["attempts"][0]["outcome"] = "needs_input"
    elif change == "expired":
        db[steering.COLLECTION].update_one(
            {}, {"$set": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}}
        )
    elif change == "wrong_sender":
        task["created_by_concept_id"] = "#V#other"
    elif change == "wrong_org":
        task["organisation_concept_id"] = "#V#other"
    with pytest.raises(messages.DirectMessageSteeringUnavailable):
        resolve(actor)


def test_sender_cannot_publish_an_owner_binding(owner):
    _, actor, binding, _ = owner
    actor[0] = "#V#owner"
    with pytest.raises(PermissionError):
        steering.publish(binding, delegator_id="#V#owner")


def test_real_message_service_rechecks_admission_before_persistence(owner, monkeypatch):
    db, actor, binding, _ = owner
    steering.publish(binding, delegator_id="#V#owner")
    resolve(actor)
    monkeypatch.setattr(messages, "get_concepts_collection", lambda: db.concepts)
    created = []
    monkeypatch.setattr(
        messages.ConceptsRepository,
        "insert_one",
        lambda doc: created.append(deepcopy(doc)),
    )
    monkeypatch.setattr(messages, "upsert_text_for_concept", lambda **_: None)
    monkeypatch.setattr(
        messages, "maybe_launch_direct_message_workflow", lambda **_: None
    )
    # Canonical create boundary accepts only the exact current target.
    result = messages.create_message(
        sender_id="#V#owner",
        recipient_ids=["#V#agent"],
        org_id="#V#org",
        content="Use the bounded existing test.",
        metadata={"submit_mode": "steer", "submit_target": binding},
    )
    projection = messages.project_direct_message(result)
    assert projection["submit_mode"] == "steer"
    assert projection["submit_target"] == binding
    assert len(created) == 1
    with pytest.raises(messages.DirectMessageSteeringUnavailable):
        messages.create_message(
            sender_id="#V#owner",
            recipient_ids=["#V#agent"],
            org_id="#V#org",
            content="Never retarget.",
            metadata={
                "submit_mode": "steer",
                "submit_target": {**binding, "turn_id": "other"},
            },
        )
    assert len(created) == 1


def test_normal_rest_target_send_replay_and_controller_receipt(owner, monkeypatch):
    from flask import Flask
    from src.backend.server.routes import message_routes as routes

    db, actor, binding, task = owner
    steering.publish(binding, delegator_id="#V#owner")
    actor[0] = "#V#owner"
    monkeypatch.setattr(routes, "_get_current_user_concept_id", lambda: actor[0])
    monkeypatch.setattr(routes, "_get_current_org_concept_id", lambda *a, **k: "#V#org")
    monkeypatch.setattr(
        routes, "_authorise_sender_and_recipients_for_org", lambda **_: (True, [])
    )
    monkeypatch.setattr(messages, "get_concepts_collection", lambda: db.concepts)
    monkeypatch.setattr(messages, "apply_concept_query_filter", lambda query: query)
    monkeypatch.setattr(
        messages.ConceptsRepository, "insert_one", db.concepts.insert_one
    )
    monkeypatch.setattr(
        messages.ConceptsRepository, "update_one", db.concepts.update_one
    )
    monkeypatch.setattr(messages, "upsert_text_for_concept", lambda **_: None)
    monkeypatch.setattr(
        messages, "maybe_launch_direct_message_workflow", lambda **_: None
    )
    monkeypatch.setattr(
        messages,
        "get_message_for_user",
        lambda mid, uid: db.concepts.find_one({"concept_id": mid}),
    )
    monkeypatch.setattr(
        "src.backend.services.episode_logging_service.log_episode", lambda **_: None
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(routes.message_bp, url_prefix="/api/messages")
    client = app.test_client()
    available = client.get(
        "/api/messages/steering-target?recipient_id=%23V%23agent&organisation_concept_id=%23V%23org"
    ).get_json()
    assert available == {"available": True, "submit_target": binding}
    payload = {
        "recipient_ids": ["#V#agent"],
        "organisation_concept_id": "#V#org",
        "content": "Harmless exact-turn guidance",
        "submit_mode": "steer",
        "submit_target": available["submit_target"],
        "delivery_idempotency_key": "steering-probe",
    }
    first = client.post("/api/messages/", json=payload)
    assert first.status_code == 201, first.get_json()
    message_id = first.get_json()["message_id"]
    assert first.get_json()["steering_delivery"]["status"] == "pending"
    actor[0] = "#V#agent"
    receipt = steering.record_delivery(message_id, binding, "accepted")
    assert receipt["consumption_verified"] is False
    steering.withdraw(binding)
    actor[0] = "#V#owner"
    # An ambiguous HTTP retry reconciles its existing message even after completion.
    task["status"] = "completed"
    replay = client.post("/api/messages/", json=payload)
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["message_id"] == message_id
    assert replay.get_json()["steering_delivery"]["status"] == "accepted"
    assert db.concepts.count_documents({}) == 1


def test_missing_canonical_task_is_unavailable_but_store_failure_is_an_error(
    owner, monkeypatch
):
    from src.backend.services import task_management_service as tasks

    _, actor, binding, _ = owner
    steering.publish(binding, delegator_id="#V#owner")

    def missing(_):
        raise tasks.TaskNotFoundError("missing")

    monkeypatch.setattr(tasks, "get_task", missing)
    with pytest.raises(messages.DirectMessageSteeringUnavailable):
        resolve(actor)

    def unavailable(_):
        raise ConnectionError("unavailable")

    monkeypatch.setattr(tasks, "get_task", unavailable)
    with pytest.raises(ConnectionError):
        resolve(actor)
