from concurrent.futures import ThreadPoolExecutor

import pytest

from src.backend.db import mongo_client
from src.backend.services import chat_steering_service as steering
from src.backend.services import chat_prompt_queue_service as queue


@pytest.fixture()
def active(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_steering")
    mongo_client.close_connection()
    scope = queue.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    record = queue.create_queue_record(
        scope=scope, prompt_raw="Original job", session_id="session"
    )
    queue._collection().update_one(
        {"queue_id": record["queue_id"]},
        {
            "$set": {
                "status": "in_progress",
                "active_conversation_key": "conversation",
                "attempt_id": "attempt",
            }
        },
    )
    yield {"scope": scope, "queue_id": record["queue_id"], "attempt_id": "attempt"}
    mongo_client.close_connection()


def statuses(active):
    return [
        i["status"]
        for i in steering.read(scope=active["scope"], queue_id=active["queue_id"])[
            "items"
        ]
    ]


def test_fifo_steering_idempotency_and_queue_independence(active):
    later = queue.create_queue_record(
        scope=active["scope"], prompt_raw="Later job", session_id="session"
    )
    for id in ["a", "b", "a"]:
        steering.submit(**active, submission_id=id, text=id)
    assert statuses(active) == ["pending", "pending"]
    assert [m["text"] for m in steering.take(**active)] == ["a", "b"]
    assert steering.take(**active) == []
    assert statuses(active) == ["delivered", "delivered"]
    assert (
        queue._collection().find_one({"queue_id": later["queue_id"]})["status"]
        == "queued"
    )
    with pytest.raises(queue.ChatPromptQueueRecordNotFound):
        steering.submit(**active, submission_id="a", text="different")


def test_withdrawal_and_terminal_readback(active):
    steering.submit(**active, submission_id="a", text="Withdraw me")
    steering.submit(**active, submission_id="b", text="Keep me")
    steering.cancel(
        scope=active["scope"], queue_id=active["queue_id"], submission_id="a"
    )
    assert [m["id"] for m in steering.take(**active)] == ["b"]
    with pytest.raises(queue.ChatPromptQueueRecordNotFound):
        steering.cancel(
            scope=active["scope"], queue_id=active["queue_id"], submission_id="b"
        )
    steering.submit(**active, submission_id="c", text="Unseen")
    queue._collection().update_one(
        {"queue_id": active["queue_id"]}, {"$set": {"status": "cancelled"}}
    )
    assert statuses(active) == ["cancelled", "delivered", "not_applied"]
    assert steering.take(**active) == []


def test_final_boundary_and_submission_are_atomic(active):
    def submit():
        try:
            steering.submit(**active, submission_id="race", text="Correct this")
            return True
        except queue.ChatPromptQueueRecordNotFound:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = pool.submit(submit)
        taken = pool.submit(steering.take, **active, close_if_empty=True)
    assert accepted.result() == bool(taken.result())
    steering.take(**active, close_if_empty=True)
    with pytest.raises(queue.ChatPromptQueueRecordNotFound):
        steering.submit(**active, submission_id="late", text="Too late")


def test_scope_attempt_and_stopping_boundaries(active):
    for changes in [
        {"attempt_id": "other"},
        {"scope": {**active["scope"], "user_concept_id": "#V#other"}},
    ]:
        with pytest.raises(queue.ChatPromptQueueRecordNotFound):
            steering.submit(
                **{**active, **changes}, submission_id="a", text="Wrong scope"
            )
    steering.submit(**active, submission_id="a", text="Old attempt")
    queue._collection().update_one(
        {"queue_id": active["queue_id"]}, {"$set": {"attempt_id": "new"}}
    )
    assert steering.take(**{**active, "attempt_id": "new"}) == []
    assert statuses(active) == ["not_applied"]
    from datetime import datetime, timezone

    queue._collection().update_one(
        {"queue_id": active["queue_id"]},
        {"$set": {"cancellation_requested_at": datetime.now(timezone.utc)}},
    )
    with pytest.raises(queue.ChatPromptQueueRecordNotFound):
        steering.submit(
            **{**active, "attempt_id": "new"}, submission_id="b", text="Stopping"
        )


def test_new_attempt_accepts_steering_without_rewriting_old_delivery(active):
    steering.submit(**active, submission_id='old', text='Delivered earlier')
    steering.take(**active)
    steering.take(**active, close_if_empty=True)
    queue._collection().update_one({'queue_id': active['queue_id']}, {'$set': {'attempt_id': 'next'}})
    steering.submit(**{**active, 'attempt_id': 'next'}, submission_id='new', text='New guidance')
    assert statuses(active) == ['delivered', 'pending']
    assert [m['id'] for m in steering.take(**{**active, 'attempt_id': 'next'})] == ['new']


def test_mailbox_size_is_bounded_without_discarding_accepted_guidance(active):
    steering.submit(**active, submission_id='full', text='x' * queue.MAX_PROMPT_RAW_CHARS)
    with pytest.raises(queue.ChatPromptQueueRecordNotFound):
        steering.submit(**active, submission_id='overflow', text='y')
    assert statuses(active) == ['pending']
