from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.backend.db import mongo_client
from src.backend.services import chat_prompt_queue_service as queue_service


@pytest.fixture(autouse=True)
def mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    mongo_client.close_connection()
    yield
    mongo_client.close_connection()


def test_queue_record_lifecycle_restores_active_items_only() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )

    queued = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Second task",
        session_id="session-2",
        session_name="Target",
    )

    assert queued["status"] == queue_service.STATUS_QUEUED
    assert queue_service.list_active_queue_records(scope=scope)[0]["prompt_raw"] == "Second task"

    updated = queue_service.update_queued_record(
        scope=scope,
        queue_id=queued["queue_id"],
        prompt_raw="Second edited",
    )
    assert updated["prompt_raw"] == "Second edited"

    claimed = queue_service.claim_queue_record(scope=scope, queue_id=queued["queue_id"])
    assert claimed["status"] == queue_service.STATUS_IN_PROGRESS
    assert claimed["attempt_count"] == 1

    requeued = queue_service.requeue_prompt_record(scope=scope, queue_id=queued["queue_id"])
    assert requeued["status"] == queue_service.STATUS_QUEUED

    reclaimed = queue_service.claim_queue_record(scope=scope, queue_id=queued["queue_id"])
    assert reclaimed["attempt_count"] == 2

    finished = queue_service.finish_prompt_record(
        scope=scope,
        queue_id=queued["queue_id"],
        status=queue_service.STATUS_COMPLETED,
    )
    assert finished["status"] == queue_service.STATUS_COMPLETED
    assert queue_service.list_active_queue_records(scope=scope) == []


def test_queue_scope_isolation() -> None:
    first_scope = queue_service.build_queue_scope(
        user_concept_id="#V#user_a",
        organisation_concept_id="#V#org",
        namespace="#V#user_a@org",
    )
    second_scope = queue_service.build_queue_scope(
        user_concept_id="#V#user_b",
        organisation_concept_id="#V#org",
        namespace="#V#user_b@org",
    )

    queue_service.create_queue_record(scope=first_scope, prompt_raw="Private task")

    assert len(queue_service.list_active_queue_records(scope=first_scope)) == 1
    assert queue_service.list_active_queue_records(scope=second_scope) == []


def test_queue_scope_canonicalises_org_and_namespace_components() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="user",
        organisation_concept_id="#V#org",
        namespace=None,
    )

    assert scope == {
        "user_concept_id": "#V#user",
        "organisation_concept_id": "#V#org",
        "namespace": "#V#user@org",
    }


def test_queue_transitions_tolerate_legacy_scope_variants() -> None:
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    now = datetime.now(timezone.utc)
    coll.insert_one(
        {
            "queue_id": "legacy-queue-1",
            "user_concept_id": "#V#user",
            "organisation_concept_id": "#V#org",
            "namespace": None,
            "prompt_raw": "Legacy queued task",
            "status": queue_service.STATUS_QUEUED,
            "session_id": "session-1",
            "session_name": "Legacy",
            "source": "queued",
            "attempt_count": 0,
            "created_at": now,
            "updated_at": now,
            "queued_at": now,
            "claimed_at": None,
            "completed_at": None,
            "last_error": None,
        }
    )
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="org",
        namespace="#V#user@org",
    )

    claimed = queue_service.claim_queue_record(scope=scope, queue_id="legacy-queue-1")
    assert claimed["status"] == queue_service.STATUS_IN_PROGRESS

    persisted = coll.find_one({"queue_id": "legacy-queue-1"})
    assert persisted is not None
    assert persisted["organisation_concept_id"] == "#V#org"
    assert persisted["namespace"] == "#V#user@org"


def test_queue_transition_miss_reports_wrong_state_details() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    queued = queue_service.create_queue_record(scope=scope, prompt_raw="Already done")
    queue_service.finish_prompt_record(
        scope=scope,
        queue_id=queued["queue_id"],
        status=queue_service.STATUS_COMPLETED,
    )

    with pytest.raises(queue_service.ChatPromptQueueRecordNotFound) as exc_info:
        queue_service.claim_queue_record(scope=scope, queue_id=queued["queue_id"])

    assert exc_info.value.error_code == "wrong_state"
    assert exc_info.value.details["current_status"] == queue_service.STATUS_COMPLETED


def test_list_active_queue_records_expires_stale_in_progress_records() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    active = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Old active task",
        status=queue_service.STATUS_IN_PROGRESS,
        source="active",
    )
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    old = datetime.now(timezone.utc) - timedelta(days=2)
    coll.update_one(
        {"queue_id": active["queue_id"]},
        {"$set": {"claimed_at": old, "updated_at": old}},
    )

    assert queue_service.list_active_queue_records(scope=scope) == []
    persisted = coll.find_one({"queue_id": active["queue_id"]})
    assert persisted is not None
    assert persisted["status"] == queue_service.STATUS_FAILED
    assert persisted["last_error"] == queue_service.STALE_IN_PROGRESS_LAST_ERROR
