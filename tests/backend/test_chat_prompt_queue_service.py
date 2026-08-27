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
    assert (
        queue_service.list_active_queue_records(scope=scope)[0]["prompt_raw"]
        == "Second task"
    )

    updated = queue_service.update_queued_record(
        scope=scope,
        queue_id=queued["queue_id"],
        prompt_raw="Second edited",
    )
    assert updated["prompt_raw"] == "Second edited"

    claimed = queue_service.claim_queue_record(scope=scope, queue_id=queued["queue_id"])
    assert claimed["status"] == queue_service.STATUS_IN_PROGRESS
    assert claimed["attempt_count"] == 1

    requeued = queue_service.requeue_prompt_record(
        scope=scope, queue_id=queued["queue_id"]
    )
    assert requeued["status"] == queue_service.STATUS_QUEUED

    reclaimed = queue_service.claim_queue_record(
        scope=scope, queue_id=queued["queue_id"]
    )
    assert reclaimed["attempt_count"] == 2

    finished = queue_service.finish_prompt_record(
        scope=scope,
        queue_id=queued["queue_id"],
        status=queue_service.STATUS_COMPLETED,
    )
    assert finished["status"] == queue_service.STATUS_COMPLETED
    assert queue_service.list_active_queue_records(scope=scope) == []


def test_turn_binding_correlates_attempt_and_serialises_one_conversation() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    first = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="First",
        session_id="session-a",
        client_request_id="request-a",
        attempt_id="attempt-a",
    )
    second = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Second",
        session_id="session-b",
    )

    bound = queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=first["queue_id"],
        client_request_id="request-a",
        attempt_id="attempt-a",
        conversation_key="conversation-key-a",
        server_instance_id="server-a",
    )
    parallel = queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=second["queue_id"],
        client_request_id="request-b",
        attempt_id="attempt-b",
        conversation_key="conversation-key-b",
        server_instance_id="server-a",
    )

    assert bound["status"] == queue_service.STATUS_IN_PROGRESS
    assert bound["client_request_id"] == "request-a"
    assert bound["attempt_id"] == "attempt-a"
    assert bound["conversation_key"] == "conversation-key-a"
    assert bound["server_instance_id"] == "server-a"
    assert bound["lease_acquired_at"] is not None
    assert parallel["conversation_key"] == "conversation-key-b"

    with pytest.raises(queue_service.ChatPromptQueueRecordNotFound):
        queue_service.finish_prompt_record(
            scope=scope,
            queue_id=first["queue_id"],
            status=queue_service.STATUS_COMPLETED,
        )
    with pytest.raises(queue_service.ChatPromptQueueRecordNotFound):
        queue_service.finish_prompt_record(
            scope=scope,
            queue_id=first["queue_id"],
            status=queue_service.STATUS_COMPLETED,
            attempt_id="wrong-attempt",
        )

    finished = queue_service.finish_prompt_record(
        scope=scope,
        queue_id=first["queue_id"],
        status=queue_service.STATUS_COMPLETED,
        attempt_id="attempt-a",
    )
    repeated_finish = queue_service.finish_prompt_record(
        scope=scope,
        queue_id=first["queue_id"],
        status=queue_service.STATUS_COMPLETED,
        attempt_id="attempt-a",
    )
    assert repeated_finish["completed_at"] == finished["completed_at"]
    persisted = mongo_client.get_chat_prompt_queue_collection().find_one(
        {"queue_id": first["queue_id"]}
    )
    assert persisted is not None
    assert "active_conversation_key" not in persisted
    assert "lease_heartbeat_at" not in persisted


def test_shared_conversation_fifo_crosses_scopes_and_orders_timestamp_ties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(queue_service, "_now", lambda: fixed_now)
    owner_scope = queue_service.build_queue_scope(
        user_concept_id="#V#owner",
        organisation_concept_id="#V#org",
        namespace="#V#owner@org",
    )
    invitee_scope = queue_service.build_queue_scope(
        user_concept_id="#V#invitee",
        organisation_concept_id="#V#org",
        namespace="#V#invitee@org",
    )
    records = [
        queue_service.create_queue_record(
            scope=owner_scope,
            prompt_raw="Owner prompt",
            session_id="shared-session",
            conversation_key="canonical-shared-key",
        ),
        queue_service.create_queue_record(
            scope=invitee_scope,
            prompt_raw="Invitee prompt",
            session_id="shared-session",
            conversation_key="canonical-shared-key",
        ),
    ]
    earlier, later = sorted(records, key=lambda item: item["queue_id"])
    scope_by_queue_id = {
        records[0]["queue_id"]: owner_scope,
        records[1]["queue_id"]: invitee_scope,
    }

    with pytest.raises(queue_service.ConversationTurnAlreadyActive) as exc_info:
        queue_service.bind_queue_record_to_turn(
            scope=scope_by_queue_id[later["queue_id"]],
            queue_id=later["queue_id"],
            client_request_id="request-later",
            conversation_key="canonical-shared-key",
            server_instance_id="server-a",
        )

    assert exc_info.value.queue_id is None
    bound = queue_service.bind_queue_record_to_turn(
        scope=scope_by_queue_id[earlier["queue_id"]],
        queue_id=earlier["queue_id"],
        client_request_id="request-earlier",
        conversation_key="canonical-shared-key",
        server_instance_id="server-a",
    )
    queue_service.finish_prompt_record(
        scope=scope_by_queue_id[earlier["queue_id"]],
        queue_id=earlier["queue_id"],
        status=queue_service.STATUS_COMPLETED,
        attempt_id=bound["attempt_id"],
    )
    assert (
        queue_service.bind_queue_record_to_turn(
            scope=scope_by_queue_id[later["queue_id"]],
            queue_id=later["queue_id"],
            client_request_id="request-later",
            conversation_key="canonical-shared-key",
            server_instance_id="server-a",
        )["status"]
        == queue_service.STATUS_IN_PROGRESS
    )


def test_queued_backlog_is_bounded_per_user_across_organisations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_MAX_QUEUED_TURNS_GLOBAL", "10")
    monkeypatch.setenv("VON_MAX_QUEUED_TURNS_PER_USER", "2")
    scopes = [
        queue_service.build_queue_scope(
            user_concept_id="#V#user",
            organisation_concept_id=f"#V#org_{index}",
            namespace=f"#V#user@org_{index}",
        )
        for index in range(3)
    ]
    for index in range(2):
        queue_service.create_queue_record(
            scope=scopes[index], prompt_raw=f"Accepted {index}"
        )

    with pytest.raises(queue_service.ChatPromptQueueCapacityReached) as exc_info:
        queue_service.create_queue_record(scope=scopes[2], prompt_raw="Rejected")

    assert exc_info.value.limit_kind == "user"
    assert len(queue_service.list_active_queue_records(scope=scopes[0])) == 1
    assert len(queue_service.list_active_queue_records(scope=scopes[1])) == 1


def test_queued_backlog_is_bounded_globally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VON_MAX_QUEUED_TURNS_GLOBAL", "2")
    monkeypatch.setenv("VON_MAX_QUEUED_TURNS_PER_USER", "2")
    for index in range(2):
        scope = queue_service.build_queue_scope(user_concept_id=f"#V#user_{index}")
        queue_service.create_queue_record(scope=scope, prompt_raw=f"Accepted {index}")

    with pytest.raises(queue_service.ChatPromptQueueCapacityReached) as exc_info:
        queue_service.create_queue_record(
            scope=queue_service.build_queue_scope(user_concept_id="#V#user_3"),
            prompt_raw="Rejected",
        )

    assert exc_info.value.limit_kind == "global"


def test_requeue_releases_conversation_fence_and_request_correlation() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    queued = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Retry me",
        session_id="session-a",
    )

    queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=queued["queue_id"],
        client_request_id="request-a",
        attempt_id="attempt-a",
        conversation_key="conversation-key-a",
        server_instance_id="server-a",
    )

    with pytest.raises(queue_service.ConversationTurnAlreadyActive):
        queue_service.requeue_prompt_record(
            scope=scope,
            queue_id=queued["queue_id"],
            current_server_instance_id="server-a",
        )
    with pytest.raises(queue_service.ConversationTurnAlreadyActive):
        queue_service.cancel_prompt_record(
            scope=scope,
            queue_id=queued["queue_id"],
        )

    requeued = queue_service.requeue_prompt_record(
        scope=scope,
        queue_id=queued["queue_id"],
        current_server_instance_id="server-b",
    )
    assert requeued["status"] == queue_service.STATUS_QUEUED
    assert requeued["client_request_id"] is None
    assert requeued["attempt_id"] is None

    rebound = queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=queued["queue_id"],
        client_request_id="request-b",
        attempt_id="attempt-b",
        conversation_key="conversation-key-a",
        server_instance_id="server-b",
    )
    assert rebound["status"] == queue_service.STATUS_IN_PROGRESS
    assert rebound["client_request_id"] == "request-b"
    assert rebound["attempt_id"] == "attempt-b"
    assert rebound["server_instance_id"] == "server-b"


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


def test_queue_scope_recovers_components_from_namespace() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id=None,
        namespace="#V#user@org",
    )

    assert scope == {
        "user_concept_id": "#V#user",
        "organisation_concept_id": "#V#org",
        "namespace": "#V#user@org",
    }


def test_queue_scope_rebuilds_namespace_when_components_disagree() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org_b",
        namespace="#V#user@org_a",
    )

    assert scope == {
        "user_concept_id": "#V#user",
        "organisation_concept_id": "#V#org_b",
        "namespace": "#V#user@org_b",
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


def test_legacy_scope_terminal_record_reports_wrong_state() -> None:
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    now = datetime.now(timezone.utc)
    coll.insert_one(
        {
            "queue_id": "legacy-terminal-queue-1",
            "user_concept_id": "user",
            "organisation_concept_id": "org",
            "namespace": None,
            "prompt_raw": "Legacy completed task",
            "status": queue_service.STATUS_COMPLETED,
            "session_id": "session-1",
            "session_name": "Legacy",
            "source": "queued",
            "attempt_count": 1,
            "created_at": now,
            "updated_at": now,
            "queued_at": now,
            "claimed_at": now,
            "completed_at": now,
            "last_error": None,
        }
    )
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )

    with pytest.raises(queue_service.ChatPromptQueueRecordNotFound) as exc_info:
        queue_service.claim_queue_record(
            scope=scope, queue_id="legacy-terminal-queue-1"
        )

    assert exc_info.value.error_code == "wrong_state"
    assert exc_info.value.details["scope_match"] is True
    assert exc_info.value.details["current_status"] == queue_service.STATUS_COMPLETED


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


def test_list_active_queue_records_marks_stale_in_progress_as_advisory() -> None:
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

    records = queue_service.list_active_queue_records(scope=scope)
    assert len(records) == 1
    assert records[0]["queue_id"] == active["queue_id"]
    assert records[0]["status"] == queue_service.STATUS_IN_PROGRESS
    assert records[0]["stale_advisory"] is True
    assert records[0]["stale_advisory_reason"] == (
        queue_service.STALE_IN_PROGRESS_ADVISORY_REASON
    )
    assert records[0]["reconciliation_required"] is True
    persisted = coll.find_one({"queue_id": active["queue_id"]})
    assert persisted is not None
    assert persisted["status"] == queue_service.STATUS_IN_PROGRESS
    assert persisted["completed_at"] is None
    assert persisted["last_error"] is None
    assert persisted["stale_advisory"] is True
    assert persisted["stale_advisory_reason"] == (
        queue_service.STALE_IN_PROGRESS_ADVISORY_REASON
    )

    requeued = queue_service.requeue_prompt_record(
        scope=scope,
        queue_id=active["queue_id"],
    )
    assert requeued["status"] == queue_service.STATUS_QUEUED
    assert requeued["stale_advisory"] is False
    assert requeued["stale_advisory_reason"] is None
    assert requeued["reconciliation_required"] is False


def test_list_recent_failed_queue_records_is_bounded_and_scoped() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    other_scope = queue_service.build_queue_scope(
        user_concept_id="#V#other",
        organisation_concept_id="#V#org",
        namespace="#V#other@org",
    )

    failed = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Visible failed task",
        session_id="session-1",
        session_name="Current",
    )
    queue_service.claim_queue_record(scope=scope, queue_id=failed["queue_id"])
    queue_service.finish_prompt_record(
        scope=scope,
        queue_id=failed["queue_id"],
        status=queue_service.STATUS_FAILED,
        error="The final response did not return from Von.",
    )

    completed = queue_service.create_queue_record(
        scope=scope, prompt_raw="Completed task"
    )
    queue_service.finish_prompt_record(
        scope=scope,
        queue_id=completed["queue_id"],
        status=queue_service.STATUS_COMPLETED,
    )

    other_failed = queue_service.create_queue_record(
        scope=other_scope,
        prompt_raw="Private failed task",
    )
    queue_service.finish_prompt_record(
        scope=other_scope,
        queue_id=other_failed["queue_id"],
        status=queue_service.STATUS_FAILED,
        error="Other scope",
    )

    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    old_completed_at = datetime.now(timezone.utc) - timedelta(days=3)
    stale_failed = queue_service.create_queue_record(
        scope=scope, prompt_raw="Old failed task"
    )
    queue_service.finish_prompt_record(
        scope=scope,
        queue_id=stale_failed["queue_id"],
        status=queue_service.STATUS_FAILED,
        error="Too old",
    )
    coll.update_one(
        {"queue_id": stale_failed["queue_id"]},
        {"$set": {"completed_at": old_completed_at, "updated_at": old_completed_at}},
    )

    recent = queue_service.list_recent_failed_queue_records(scope=scope)

    assert [item["queue_id"] for item in recent] == [failed["queue_id"]]
    assert recent[0]["prompt_raw"] == "Visible failed task"
    assert recent[0]["last_error"] == "The final response did not return from Von."


def test_cancel_failed_record_is_scoped_durable_and_preserves_failure_evidence() -> (
    None
):
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    other_scope = queue_service.build_queue_scope(
        user_concept_id="#V#other",
        organisation_concept_id="#V#org",
        namespace="#V#other@org",
    )
    queued = queue_service.create_queue_record(scope=scope, prompt_raw="Expired task")
    queue_service.claim_queue_record(scope=scope, queue_id=queued["queue_id"])
    failed = queue_service.finish_prompt_record(
        scope=scope,
        queue_id=queued["queue_id"],
        status=queue_service.STATUS_FAILED,
        error=queue_service.STALE_IN_PROGRESS_LAST_ERROR,
    )

    with pytest.raises(queue_service.ChatPromptQueueRecordNotFound) as exc_info:
        queue_service.cancel_prompt_record(
            scope=other_scope,
            queue_id=queued["queue_id"],
        )

    assert exc_info.value.error_code == "scope_mismatch"
    recent_failed = queue_service.list_recent_failed_queue_records(scope=scope)
    assert recent_failed[0]["queue_id"] == queued["queue_id"]

    dismissed = queue_service.cancel_prompt_record(
        scope=scope,
        queue_id=queued["queue_id"],
    )

    assert dismissed["status"] == queue_service.STATUS_CANCELLED
    assert dismissed["last_error"] == queue_service.STALE_IN_PROGRESS_LAST_ERROR
    assert dismissed["completed_at"] == failed["completed_at"]
    assert queue_service.list_queue_visibility_records(scope=scope) == {
        "items": [],
        "recent_failed_items": [],
    }

    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    persisted = coll.find_one({"queue_id": queued["queue_id"]})
    assert persisted is not None
    assert persisted["status"] == queue_service.STATUS_CANCELLED
    assert persisted["last_error"] == queue_service.STALE_IN_PROGRESS_LAST_ERROR


def test_cancel_prompt_record_still_rejects_completed_records() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    queued = queue_service.create_queue_record(scope=scope, prompt_raw="Completed task")
    queue_service.finish_prompt_record(
        scope=scope,
        queue_id=queued["queue_id"],
        status=queue_service.STATUS_COMPLETED,
    )

    with pytest.raises(queue_service.ChatPromptQueueRecordNotFound) as exc_info:
        queue_service.cancel_prompt_record(scope=scope, queue_id=queued["queue_id"])

    assert exc_info.value.error_code == "wrong_state"
    assert exc_info.value.details["current_status"] == queue_service.STATUS_COMPLETED
