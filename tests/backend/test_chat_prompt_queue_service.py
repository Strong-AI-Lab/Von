from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
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


def _create_server_queue_record(
    *,
    scope: dict[str, str | None],
    submission_id: str,
    prompt: str = "Queued server task",
    conversation_key: str = "conversation-server",
    session_id: str = "session-server",
    execution_envelope: dict[str, object] | None = None,
    task_concept_id: str | None = None,
    task_execution_concept_id: str | None = None,
    dispatch_ready: bool | None = None,
    handoff_source_queue_id: str | None = None,
) -> dict[str, object]:
    return queue_service.create_queue_record(
        scope=scope,
        prompt_raw=prompt,
        session_id=session_id,
        session_name="Server queue",
        conversation_key=conversation_key,
        enqueue_submission_id=submission_id,
        dispatch_mode=queue_service.DISPATCH_MODE_SERVER,
        execution_envelope=(
            execution_envelope
            if execution_envelope is not None
            else {"language": "en-NZ", "skip_buttonify": True}
        ),
        execution_envelope_version=queue_service.EXECUTION_ENVELOPE_VERSION,
        task_concept_id=task_concept_id,
        task_execution_concept_id=task_execution_concept_id,
        server_dispatch_ready=dispatch_ready,
        handoff_source_queue_id=handoff_source_queue_id,
    )


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
    earlier, later = records
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


def test_active_queue_projection_matches_fifo_when_timestamps_tie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(queue_service, "_now", lambda: fixed_now)
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    created = [
        queue_service.create_queue_record(
            scope=scope,
            prompt_raw=f"Prompt {index}",
            session_id="session-tied",
            conversation_key="conversation-tied",
        )
        for index in range(3)
    ]

    projected = queue_service.list_active_queue_records(scope=scope)
    assert [record["queue_id"] for record in projected] == [
        record["queue_id"] for record in created
    ]


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


def test_durable_cancellation_survives_restart_and_prevents_replay() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    queued = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Stop this exact turn",
        session_id="session-a",
    )
    bound = queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=queued["queue_id"],
        client_request_id="request-a",
        attempt_id="attempt-a",
        conversation_key="conversation-key-a",
        server_instance_id="server-a",
    )

    requested = queue_service.request_prompt_cancellation(
        scope=scope,
        queue_id=queued["queue_id"],
        attempt_id="attempt-a",
    )
    replay = queue_service.request_prompt_cancellation(
        scope=scope,
        queue_id=queued["queue_id"],
        attempt_id="attempt-a",
    )

    assert requested["status"] == queue_service.STATUS_IN_PROGRESS
    assert requested["cancellation_requested"] is True
    assert replay["cancellation_requested_at"] == requested["cancellation_requested_at"]
    assert queue_service.is_prompt_cancellation_requested(
        scope=scope,
        queue_id=queued["queue_id"],
        attempt_id="attempt-a",
    )
    with pytest.raises(queue_service.ChatPromptQueueReconciliationRequired):
        queue_service.requeue_prompt_record(
            scope=scope,
            queue_id=queued["queue_id"],
            current_server_instance_id="server-b",
        )
    assert (
        queue_service.terminalise_one_interrupted_cancellation(
            current_server_instance_id="server-a"
        )
        is None
    )

    terminal = queue_service.terminalise_one_interrupted_cancellation(
        current_server_instance_id="server-b"
    )

    assert terminal is not None
    assert terminal["status"] == queue_service.STATUS_CANCELLED
    assert terminal["attempt_id"] == bound["attempt_id"]
    assert queue_service.list_active_queue_records(scope=scope) == []


def test_durable_cancellation_wins_over_late_provider_completion() -> None:
    scope = queue_service.build_queue_scope(user_concept_id="#V#user")
    queued = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Provider is still running",
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
    queue_service.request_prompt_cancellation(
        scope=scope,
        queue_id=queued["queue_id"],
        attempt_id="attempt-a",
    )

    terminal = queue_service.finish_prompt_record(
        scope=scope,
        queue_id=queued["queue_id"],
        status=queue_service.STATUS_COMPLETED,
        attempt_id="attempt-a",
    )

    assert terminal["status"] == queue_service.STATUS_CANCELLED
    assert terminal["last_error"] is None


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


def test_active_legacy_submission_reuses_same_half_and_joins_after_terminal() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    create_kwargs = {
        "scope": scope,
        "prompt_raw": "Run the same active prompt",
        "session_id": "session-a",
        "conversation_key": "canonical-conversation-a",
        "window_session_id": "window-a",
    }

    queue_first = queue_service.create_queue_record(
        **create_kwargs,
        legacy_submission_role=queue_service.LEGACY_SUBMISSION_ROLE_QUEUE,
    )
    queue_retry = queue_service.create_queue_record(
        **create_kwargs,
        legacy_submission_role=queue_service.LEGACY_SUBMISSION_ROLE_QUEUE,
    )

    assert queue_retry["queue_id"] == queue_first["queue_id"]
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    assert coll.count_documents({}) == 1
    persisted = coll.find_one({"queue_id": queue_first["queue_id"]})
    assert persisted is not None
    active_key = persisted["active_legacy_submission_key"]
    assert len(active_key) == 64
    assert "Run the same active prompt" not in active_key

    queue_service.finish_prompt_record(
        scope=scope,
        queue_id=queue_first["queue_id"],
        status=queue_service.STATUS_COMPLETED,
    )
    terminal = coll.find_one({"queue_id": queue_first["queue_id"]})
    assert terminal is not None
    assert "active_legacy_submission_key" in terminal
    assert terminal["legacy_submission_queue_seen"] is True
    assert terminal["legacy_submission_generate_seen"] is False

    generate_second = queue_service.create_queue_record(
        **create_kwargs,
        client_request_id="request-a",
        legacy_submission_role=queue_service.LEGACY_SUBMISSION_ROLE_GENERATE,
    )
    assert generate_second["queue_id"] == queue_first["queue_id"]
    joined = coll.find_one({"queue_id": queue_first["queue_id"]})
    assert joined is not None
    assert "active_legacy_submission_key" not in joined
    assert "legacy_submission_expires_at" not in joined
    assert joined["legacy_submission_queue_seen"] is True
    assert joined["legacy_submission_generate_seen"] is True

    repeated_finish = queue_service.finish_prompt_record(
        scope=scope,
        queue_id=queue_first["queue_id"],
        status=queue_service.STATUS_COMPLETED,
    )
    assert repeated_finish["queue_id"] == queue_first["queue_id"]
    with pytest.raises(queue_service.ChatPromptQueueRecordNotFound):
        queue_service.finish_prompt_record(
            scope=scope,
            queue_id=queue_first["queue_id"],
            status=queue_service.STATUS_FAILED,
        )

    next_submission = queue_service.create_queue_record(
        **create_kwargs,
        legacy_submission_role=queue_service.LEGACY_SUBMISSION_ROLE_QUEUE,
    )
    assert next_submission["queue_id"] != queue_first["queue_id"]
    assert coll.count_documents({}) == 2


def test_legacy_queue_and_generate_duplicate_key_race_converges_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    original_insert_one = coll.insert_one
    insert_barrier = threading.Barrier(2)

    def racing_insert(document, *args, **kwargs):
        insert_barrier.wait(timeout=2)
        return original_insert_one(document, *args, **kwargs)

    monkeypatch.setattr(coll, "insert_one", racing_insert)
    common = {
        "scope": scope,
        "session_id": "legacy-race-session",
        "conversation_key": "canonical-legacy-race-conversation",
        "window_session_id": "legacy-race-window",
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        queue_future = executor.submit(
            queue_service.create_queue_record,
            **common,
            prompt_raw="  Race #V\u200b#prompt  ",
            legacy_submission_role=queue_service.LEGACY_SUBMISSION_ROLE_QUEUE,
        )
        generate_future = executor.submit(
            queue_service.create_queue_record,
            **common,
            prompt_raw="Race #V#prompt",
            status=queue_service.STATUS_IN_PROGRESS,
            client_request_id="legacy-race-request",
            legacy_submission_role=queue_service.LEGACY_SUBMISSION_ROLE_GENERATE,
        )
        queue_record = queue_future.result(timeout=3)
        generate_record = generate_future.result(timeout=3)

    assert queue_record["queue_id"] == generate_record["queue_id"]
    assert coll.count_documents({}) == 1
    persisted = coll.find_one({"queue_id": queue_record["queue_id"]})
    assert persisted is not None
    assert persisted["legacy_submission_queue_seen"] is True
    assert persisted["legacy_submission_generate_seen"] is True
    assert "active_legacy_submission_key" not in persisted


def test_unpaired_legacy_submission_rendezvous_expires_without_deleting_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    clock = [datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(queue_service, "_now", lambda: clock[0])
    create_kwargs = {
        "scope": scope,
        "prompt_raw": "Retry after a lost generate half",
        "session_id": "session-expiry",
        "conversation_key": "canonical-conversation-expiry",
        "legacy_submission_role": queue_service.LEGACY_SUBMISSION_ROLE_QUEUE,
        "window_session_id": "window-expiry",
    }

    expired = queue_service.create_queue_record(**create_kwargs)
    clock[0] += timedelta(
        seconds=queue_service.LEGACY_SUBMISSION_RENDEZVOUS_SECONDS + 1
    )
    replacement = queue_service.create_queue_record(**create_kwargs)

    assert replacement["queue_id"] != expired["queue_id"]
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    expired_doc = coll.find_one({"queue_id": expired["queue_id"]})
    replacement_doc = coll.find_one({"queue_id": replacement["queue_id"]})
    assert expired_doc is not None
    assert expired_doc["status"] == queue_service.STATUS_QUEUED
    assert "active_legacy_submission_key" not in expired_doc
    assert "legacy_submission_queue_seen" not in expired_doc
    assert "legacy_submission_generate_seen" not in expired_doc
    assert "legacy_submission_expires_at" not in expired_doc
    assert replacement_doc is not None
    assert "active_legacy_submission_key" in replacement_doc


def test_terminal_unpaired_generate_allows_new_client_request() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    create_kwargs = {
        "scope": scope,
        "prompt_raw": "Legitimately send this prompt again",
        "session_id": "session-new-generate",
        "conversation_key": "canonical-conversation-new-generate",
        "legacy_submission_role": queue_service.LEGACY_SUBMISSION_ROLE_GENERATE,
        "window_session_id": "window-new-generate",
    }
    first = queue_service.create_queue_record(
        **create_kwargs,
        client_request_id="request-a",
    )

    with pytest.raises(queue_service.ConversationTurnAlreadyActive):
        queue_service.create_queue_record(
            **create_kwargs,
            client_request_id="request-b",
        )

    queue_service.finish_prompt_record(
        scope=scope,
        queue_id=first["queue_id"],
        status=queue_service.STATUS_COMPLETED,
    )
    second = queue_service.create_queue_record(
        **create_kwargs,
        client_request_id="request-b",
    )

    assert second["queue_id"] != first["queue_id"]
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    old_doc = coll.find_one({"queue_id": first["queue_id"]})
    new_doc = coll.find_one({"queue_id": second["queue_id"]})
    assert old_doc is not None
    assert "active_legacy_submission_key" not in old_doc
    assert "legacy_submission_queue_seen" not in old_doc
    assert "legacy_submission_generate_seen" not in old_doc
    assert new_doc is not None
    assert new_doc["legacy_submission_generate_seen"] is True
    assert new_doc["legacy_submission_queue_seen"] is False
    assert "active_legacy_submission_key" in new_doc


def test_active_legacy_submission_key_isolates_actor_org_conversation_and_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_MAX_QUEUED_TURNS_PER_USER", "20")
    cases = [
        (
            queue_service.build_queue_scope(
                user_concept_id="#V#actor_a",
                organisation_concept_id="#V#org_a",
                namespace="#V#actor_a@org_a",
            ),
            "conversation-a",
            "same prompt",
            "window-a",
        ),
        (
            queue_service.build_queue_scope(
                user_concept_id="#V#actor_b",
                organisation_concept_id="#V#org_a",
                namespace="#V#actor_b@org_a",
            ),
            "conversation-a",
            "same prompt",
            "window-a",
        ),
        (
            queue_service.build_queue_scope(
                user_concept_id="#V#actor_a",
                organisation_concept_id="#V#org_b",
                namespace="#V#actor_a@org_b",
            ),
            "conversation-a",
            "same prompt",
            "window-a",
        ),
        (
            queue_service.build_queue_scope(
                user_concept_id="#V#actor_a",
                organisation_concept_id="#V#org_a",
                namespace="#V#actor_a@org_a",
            ),
            "conversation-b",
            "same prompt",
            "window-a",
        ),
        (
            queue_service.build_queue_scope(
                user_concept_id="#V#actor_a",
                organisation_concept_id="#V#org_a",
                namespace="#V#actor_a@org_a",
            ),
            "conversation-a",
            "different prompt",
            "window-a",
        ),
        (
            queue_service.build_queue_scope(
                user_concept_id="#V#actor_a",
                organisation_concept_id="#V#org_a",
                namespace="#V#actor_a@org_a",
            ),
            "conversation-a",
            "same prompt",
            "window-b",
        ),
    ]

    records = [
        queue_service.create_queue_record(
            scope=scope,
            prompt_raw=prompt,
            session_id=conversation_key,
            conversation_key=conversation_key,
            legacy_submission_role=queue_service.LEGACY_SUBMISSION_ROLE_QUEUE,
            window_session_id=window_session_id,
        )
        for scope, conversation_key, prompt, window_session_id in cases
    ]

    assert len({record["queue_id"] for record in records}) == len(cases)


def test_explicit_attempts_do_not_use_legacy_submission_correlation() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    records = [
        queue_service.create_queue_record(
            scope=scope,
            prompt_raw="Repeat explicitly",
            session_id="session-a",
            conversation_key="canonical-conversation-a",
            client_request_id=f"request-{index}",
            attempt_id=f"attempt-{index}",
        )
        for index in range(2)
    ]

    assert records[0]["queue_id"] != records[1]["queue_id"]
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    assert (
        coll.count_documents({"active_legacy_submission_key": {"$exists": True}}) == 0
    )


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


def test_visibility_projection_prefers_terminal_observation_for_same_queue_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = queue_service.build_queue_scope(user_concept_id="#V#user")
    monkeypatch.setattr(
        queue_service,
        "list_active_queue_records",
        lambda **_kwargs: [{"queue_id": "queue-race", "status": "queued"}],
    )
    monkeypatch.setattr(
        queue_service,
        "list_recent_failed_queue_records",
        lambda **_kwargs: [{"queue_id": "queue-race", "status": "failed"}],
    )

    assert queue_service.list_queue_visibility_records(scope=scope) == {
        "items": [],
        "recent_failed_items": [
            {"queue_id": "queue-race", "status": "failed"}
        ],
    }


def test_server_enqueue_is_idempotent_by_canonical_submission_fingerprint() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    envelope = {
        "workflow_inputs": {"file_copy_concept_id": "#V#file-copy"},
        "model_parameters": {"temperature": 0.2, "max_tokens": 400},
        "model_provider": "gemini",
        "model": "gemini-3.7-flash",
    }
    created = _create_server_queue_record(
        scope=scope,
        submission_id="submission-1",
        execution_envelope=envelope,
        task_concept_id="#V#task-1",
        task_execution_concept_id="#V#task-execution-1",
    )
    replayed = _create_server_queue_record(
        scope=scope,
        submission_id="submission-1",
        execution_envelope={
            "model": "gemini-3.7-flash",
            "model_provider": "gemini",
            "model_parameters": {"max_tokens": 400, "temperature": 0.2},
            "workflow_inputs": {"file_copy_concept_id": "#V#file-copy"},
        },
        task_concept_id="#V#task-1",
        task_execution_concept_id="#V#task-execution-1",
    )

    assert created["queue_id"] == replayed["queue_id"]
    assert created["idempotent_replay"] is False
    assert replayed["idempotent_replay"] is True
    assert created["task_concept_id"] == "#V#task-1"
    assert created["task_execution_concept_id"] == "#V#task-execution-1"
    assert "execution_envelope" not in created

    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    persisted = coll.find_one({"queue_id": created["queue_id"]})
    assert persisted is not None
    assert persisted["execution_envelope"] == envelope
    assert coll.count_documents({"enqueue_submission_id": "submission-1"}) == 1

    with pytest.raises(queue_service.ChatPromptQueueSubmissionConflict) as exc_info:
        _create_server_queue_record(
            scope=scope,
            submission_id="submission-1",
            prompt="Different work",
            execution_envelope=envelope,
            task_concept_id="#V#task-1",
            task_execution_concept_id="#V#task-execution-1",
        )
    assert exc_info.value.error_code == "enqueue_submission_conflict"
    assert exc_info.value.status_code == 409
    assert exc_info.value.queue_id == created["queue_id"]

    with pytest.raises(queue_service.ChatPromptQueueTaskAlreadyActive) as active:
        _create_server_queue_record(
            scope=scope,
            submission_id="submission-2",
            execution_envelope=envelope,
            task_concept_id="#V#task-1",
            task_execution_concept_id="#V#task-execution-2",
        )
    assert active.value.error_code == "task_execution_already_active"
    assert active.value.queue_id == created["queue_id"]
    assert active.value.task_execution_concept_id == "#V#task-execution-1"


def test_server_handoff_enqueue_replays_exact_replacement() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    source = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Retry this exact prompt",
        session_id="session-server",
        conversation_key="conversation-server",
    )

    created = _create_server_queue_record(
        scope=scope,
        submission_id="submission-handoff-exact",
        prompt="Retry this exact prompt",
        handoff_source_queue_id=str(source["queue_id"]),
    )
    replayed = _create_server_queue_record(
        scope=scope,
        submission_id="submission-handoff-exact",
        prompt="Retry this exact prompt",
        handoff_source_queue_id=str(source["queue_id"]),
    )

    assert created["queue_id"] == replayed["queue_id"]
    assert created["idempotent_replay"] is False
    assert replayed["idempotent_replay"] is True
    assert created["dispatch_ready"] is False
    assert created["handoff_source_queue_id"] == source["queue_id"]


def test_concurrent_distinct_handoffs_bind_one_replacement_to_source() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    source = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Retry this exact prompt",
        session_id="session-server",
        conversation_key="conversation-server",
    )
    source_queue_id = str(source["queue_id"])
    barrier = threading.Barrier(2)

    def create_replacement(index: int) -> tuple[str, str | None]:
        barrier.wait()
        try:
            record = _create_server_queue_record(
                scope=scope,
                submission_id=f"submission-handoff-race-{index}",
                prompt="Retry this exact prompt",
                handoff_source_queue_id=source_queue_id,
            )
            return "created", str(record["queue_id"])
        except queue_service.ChatPromptQueueHandoffConflict as exc:
            return "handoff_conflict", exc.queue_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(create_replacement, range(2)))

    assert sorted(outcome[0] for outcome in outcomes) == [
        "created",
        "handoff_conflict",
    ]
    assert len({outcome[1] for outcome in outcomes}) == 1
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    assert coll.count_documents({"handoff_source_queue_id": source_queue_id}) == 1


def test_cancelling_unready_handoff_releases_source_fence_for_safe_retry() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    source = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Retry this exact prompt",
        session_id="session-server",
        conversation_key="conversation-server",
    )
    first = _create_server_queue_record(
        scope=scope,
        submission_id="submission-handoff-cancelled",
        prompt="Retry this exact prompt",
        handoff_source_queue_id=str(source["queue_id"]),
    )

    cancelled = queue_service.cancel_prompt_record(
        scope=scope,
        queue_id=str(first["queue_id"]),
    )
    replacement = _create_server_queue_record(
        scope=scope,
        submission_id="submission-handoff-retry",
        prompt="Retry this exact prompt",
        handoff_source_queue_id=str(source["queue_id"]),
    )

    assert cancelled["status"] == queue_service.STATUS_CANCELLED
    assert cancelled["handoff_source_queue_id"] is None
    assert cancelled["retired_handoff_source_queue_id"] == source["queue_id"]
    assert replacement["queue_id"] != first["queue_id"]
    assert replacement["handoff_source_queue_id"] == source["queue_id"]


def test_handoff_completion_retires_source_before_dispatch_activation() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    source = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Retry this exact prompt",
        session_id="session-server",
        conversation_key="conversation-server",
    )
    replacement = _create_server_queue_record(
        scope=scope,
        submission_id="submission-handoff-complete",
        prompt="Retry this exact prompt",
        handoff_source_queue_id=str(source["queue_id"]),
    )

    completed = queue_service.complete_server_dispatch_handoff(
        scope=scope,
        queue_id=str(replacement["queue_id"]),
    )

    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    persisted_source = coll.find_one({"queue_id": source["queue_id"]})
    assert persisted_source is not None
    assert persisted_source["status"] == queue_service.STATUS_CANCELLED
    assert completed["dispatch_ready"] is True
    assert completed["handoff_completed_at"] is not None
    assert persisted_source["handoff_replacement_queue_id"] == replacement["queue_id"]

    replayed = queue_service.complete_server_dispatch_handoff(
        scope=scope,
        queue_id=str(replacement["queue_id"]),
    )
    assert replayed["queue_id"] == replacement["queue_id"]
    assert replayed["dispatch_ready"] is True


def test_independently_cancelled_source_cannot_activate_handoff_replacement() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    source = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Do not override cancellation",
        session_id="session-server",
        conversation_key="conversation-server",
    )
    replacement = _create_server_queue_record(
        scope=scope,
        submission_id="submission-handoff-pre-cancelled",
        prompt="Do not override cancellation",
        handoff_source_queue_id=str(source["queue_id"]),
    )
    queue_service.cancel_prompt_record(
        scope=scope,
        queue_id=str(source["queue_id"]),
    )

    with pytest.raises(queue_service.ChatPromptQueueHandoffConflict) as exc_info:
        queue_service.complete_server_dispatch_handoff(
            scope=scope,
            queue_id=str(replacement["queue_id"]),
        )

    assert exc_info.value.error_code == "queue_handoff_conflict"
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    persisted_source = coll.find_one({"queue_id": source["queue_id"]})
    persisted_replacement = coll.find_one({"queue_id": replacement["queue_id"]})
    assert persisted_source is not None
    assert "handoff_replacement_queue_id" not in persisted_source
    assert persisted_replacement is not None
    assert persisted_replacement["status"] == queue_service.STATUS_CANCELLED
    assert persisted_replacement.get("dispatch_ready") is False
    assert "handoff_source_queue_id" not in persisted_replacement


def test_concurrent_user_cancellation_never_coexists_with_handoff_activation() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    source = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Race exact cancellation",
        session_id="session-server",
        conversation_key="conversation-server",
    )
    replacement = _create_server_queue_record(
        scope=scope,
        submission_id="submission-handoff-cancel-race",
        prompt="Race exact cancellation",
        handoff_source_queue_id=str(source["queue_id"]),
    )
    barrier = threading.Barrier(2)

    def user_cancel() -> str:
        barrier.wait()
        try:
            queue_service.cancel_prompt_record(
                scope=scope,
                queue_id=str(source["queue_id"]),
            )
            return "user_cancelled"
        except queue_service.ChatPromptQueueError:
            return "handoff_won"

    def complete_handoff() -> str:
        barrier.wait()
        try:
            queue_service.complete_server_dispatch_handoff(
                scope=scope,
                queue_id=str(replacement["queue_id"]),
            )
            return "activated"
        except queue_service.ChatPromptQueueHandoffConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        user_future = executor.submit(user_cancel)
        handoff_future = executor.submit(complete_handoff)
        user_outcome = user_future.result()
        handoff_outcome = handoff_future.result()

    assert (user_outcome, handoff_outcome) in {
        ("user_cancelled", "conflict"),
        ("handoff_won", "activated"),
    }
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    persisted_source = coll.find_one({"queue_id": source["queue_id"]})
    persisted_replacement = coll.find_one({"queue_id": replacement["queue_id"]})
    assert persisted_source is not None
    assert persisted_replacement is not None
    if handoff_outcome == "activated":
        assert (
            persisted_source.get("handoff_replacement_queue_id")
            == replacement["queue_id"]
        )
        assert persisted_replacement["dispatch_ready"] is True
    else:
        assert "handoff_replacement_queue_id" not in persisted_source
        assert persisted_replacement["status"] == queue_service.STATUS_CANCELLED


def test_blocked_handoff_is_backed_off_and_does_not_hide_ready_work() -> None:
    fixed_now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    source = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Retry this exact prompt",
        session_id="session-server",
        conversation_key="conversation-server",
    )
    queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=str(source["queue_id"]),
        client_request_id="request-active-source",
        attempt_id="attempt-active-source",
        conversation_key="conversation-server",
        server_instance_id="server-active-source",
    )
    replacement = _create_server_queue_record(
        scope=scope,
        submission_id="submission-handoff-blocked",
        prompt="Retry this exact prompt",
        handoff_source_queue_id=str(source["queue_id"]),
    )
    ready = _create_server_queue_record(
        scope=scope,
        submission_id="submission-ready-unrelated",
        prompt="Independent prompt",
        conversation_key="conversation-independent",
        session_id="session-independent",
    )

    with pytest.raises(queue_service.ConversationTurnAlreadyActive):
        queue_service.reconcile_next_server_dispatch_handoff(
            now=fixed_now,
            retry_after_seconds=10,
        )
    assert (
        queue_service.reconcile_next_server_dispatch_handoff(
            now=fixed_now + timedelta(seconds=5),
            retry_after_seconds=10,
        )
        is None
    )
    reservation = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-dispatcher",
        now=fixed_now,
    )

    assert reservation is not None
    assert reservation["queue_id"] == ready["queue_id"]
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    persisted = coll.find_one({"queue_id": replacement["queue_id"]})
    assert persisted is not None
    assert persisted["handoff_reconciliation_attempt_count"] == 1
    assert persisted["handoff_reconciliation_next_at"].replace(
        tzinfo=timezone.utc
    ) == (fixed_now + timedelta(seconds=10))
    assert "ConversationTurnAlreadyActive" in persisted[
        "handoff_reconciliation_last_error"
    ]


def test_task_launch_fence_releases_only_after_queue_terminal_state() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    first = _create_server_queue_record(
        scope=scope,
        submission_id="submission-task-first",
        task_concept_id="#V#task-fenced",
        task_execution_concept_id="#V#task-execution-first",
        dispatch_ready=False,
    )

    with pytest.raises(queue_service.ChatPromptQueueTaskAlreadyActive):
        _create_server_queue_record(
            scope=scope,
            submission_id="submission-task-second",
            task_concept_id="#V#task-fenced",
            task_execution_concept_id="#V#task-execution-second",
            dispatch_ready=False,
        )

    queue_service.cancel_prompt_record(
        scope=scope,
        queue_id=str(first["queue_id"]),
    )
    replacement = _create_server_queue_record(
        scope=scope,
        submission_id="submission-task-second",
        task_concept_id="#V#task-fenced",
        task_execution_concept_id="#V#task-execution-second",
        dispatch_ready=False,
    )
    assert replacement["queue_id"] != first["queue_id"]


def test_concurrent_distinct_launches_create_one_active_task_attempt() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    barrier = threading.Barrier(2)

    def launch(index: int) -> tuple[str, str | None]:
        barrier.wait()
        try:
            record = _create_server_queue_record(
                scope=scope,
                submission_id=f"submission-race-{index}",
                task_concept_id="#V#task-race",
                task_execution_concept_id=f"#V#task-execution-race-{index}",
                dispatch_ready=False,
            )
            return "created", str(record["queue_id"])
        except queue_service.ChatPromptQueueTaskAlreadyActive as exc:
            return "already_active", exc.queue_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(launch, range(2)))

    assert sorted(outcome[0] for outcome in outcomes) == [
        "already_active",
        "created",
    ]
    assert len({outcome[1] for outcome in outcomes}) == 1
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    assert coll.count_documents({"task_concept_id": "#V#task-race"}) == 1


def test_concurrent_server_enqueue_replays_one_durable_record() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    barrier = threading.Barrier(8)

    def create_once(_index: int) -> dict[str, object]:
        barrier.wait()
        return _create_server_queue_record(
            scope=scope,
            submission_id="submission-concurrent",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(create_once, range(8)))

    assert len({record["queue_id"] for record in records}) == 1
    assert sum(record["idempotent_replay"] is False for record in records) == 1
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    assert coll.count_documents({"enqueue_submission_id": "submission-concurrent"}) == 1


def test_server_enqueue_rejects_non_allowlisted_or_legacy_mixed_payloads() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )

    with pytest.raises(queue_service.InvalidChatPromptQueueInput, match="invalid keys"):
        _create_server_queue_record(
            scope=scope,
            submission_id="submission-invalid",
            execution_envelope={"user_id": "#V#other"},
        )
    with pytest.raises(
        queue_service.InvalidChatPromptQueueInput,
        match="require server dispatch",
    ):
        queue_service.create_queue_record(
            scope=scope,
            prompt_raw="Legacy must remain browser owned",
            enqueue_submission_id="submission-without-mode",
        )
    with pytest.raises(
        queue_service.InvalidChatPromptQueueInput,
        match="assigns request and attempt",
    ):
        queue_service.create_queue_record(
            scope=scope,
            prompt_raw="Server assigns execution identities",
            session_id="session-server",
            conversation_key="conversation-server",
            enqueue_submission_id="submission-client-request",
            dispatch_mode=queue_service.DISPATCH_MODE_SERVER,
            execution_envelope={},
            execution_envelope_version=queue_service.EXECUTION_ENVELOPE_VERSION,
            client_request_id="client-request-must-not-be-reused",
        )
    with pytest.raises(
        queue_service.InvalidChatPromptQueueInput,
        match="cannot be combined with task execution links",
    ):
        _create_server_queue_record(
            scope=scope,
            submission_id="submission-mixed-handoff-task",
            task_concept_id="#V#task-mixed",
            task_execution_concept_id="#V#task-execution-mixed",
            handoff_source_queue_id="queue-legacy-source",
        )

    immutable = _create_server_queue_record(
        scope=scope,
        submission_id="submission-immutable",
    )
    with pytest.raises(
        queue_service.InvalidChatPromptQueueInput,
        match="immutable",
    ):
        queue_service.update_queued_record(
            scope=scope,
            queue_id=str(immutable["queue_id"]),
            prompt_raw="Mutated after idempotent enqueue",
        )
    with pytest.raises(
        queue_service.InvalidChatPromptQueueInput,
        match="server dispatch reservation",
    ):
        queue_service.claim_queue_record(
            scope=scope,
            queue_id=str(immutable["queue_id"]),
        )


def test_server_dispatch_rows_reject_legacy_finish_and_ambiguous_replay() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    queued = _create_server_queue_record(
        scope=scope,
        submission_id="submission-exact-terminal-only",
    )

    with pytest.raises(
        queue_service.ChatPromptQueueReconciliationRequired,
        match="exact admitted attempt",
    ) as finish_error:
        queue_service.finish_prompt_record(
            scope=scope,
            queue_id=str(queued["queue_id"]),
            status=queue_service.STATUS_COMPLETED,
        )
    assert finish_error.value.status_code == 409

    reservation = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-old",
    )
    assert reservation is not None
    queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=str(queued["queue_id"]),
        client_request_id=str(reservation["client_request_id"]),
        attempt_id=str(reservation["attempt_id"]),
        conversation_key="conversation-server",
        server_instance_id="server-old",
        dispatch_reservation_token=str(
            reservation["dispatch_reservation_token"]
        ),
    )
    with pytest.raises(
        queue_service.ChatPromptQueueReconciliationRequired,
        match="cannot be replayed",
    ):
        queue_service.requeue_prompt_record(
            scope=scope,
            queue_id=str(queued["queue_id"]),
            current_server_instance_id="server-new",
        )


def test_task_linked_server_row_dispatches_only_after_exact_activation() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    queued = _create_server_queue_record(
        scope=scope,
        submission_id="submission-awaiting-task-link",
        task_concept_id="#V#task-1",
        task_execution_concept_id="#V#task-execution-1",
        dispatch_ready=False,
    )
    assert queued["dispatch_ready"] is False
    assert (
        queue_service.reserve_next_server_dispatch(server_instance_id="server-a")
        is None
    )

    activated = queue_service.activate_server_dispatch_record(
        scope=scope,
        queue_id=str(queued["queue_id"]),
        enqueue_submission_id="submission-awaiting-task-link",
        task_execution_concept_id="#V#task-execution-1",
    )
    replayed_activation = queue_service.activate_server_dispatch_record(
        scope=scope,
        queue_id=str(queued["queue_id"]),
        enqueue_submission_id="submission-awaiting-task-link",
        task_execution_concept_id="#V#task-execution-1",
    )
    assert activated["dispatch_ready"] is True
    assert replayed_activation["queue_id"] == queued["queue_id"]
    assert (
        queue_service.reserve_next_server_dispatch(server_instance_id="server-a")
        is not None
    )


def test_task_launch_outbox_lease_blocks_route_activation_and_reclaims_exactly() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    queued = _create_server_queue_record(
        scope=scope,
        submission_id="submission-launch-reconcile",
        task_concept_id="#V#task-launch-reconcile",
        task_execution_concept_id="#V#task-execution-launch-reconcile",
        dispatch_ready=False,
    )
    observed_now = datetime.now(timezone.utc)
    first = queue_service.claim_task_execution_launch_reconciliation(
        server_instance_id="reconciler-a",
        now=observed_now,
        lease_seconds=10,
    )
    assert first is not None
    assert first["queue_id"] == queued["queue_id"]
    assert first["execution_envelope"] == {
        "language": "en-NZ",
        "skip_buttonify": True,
    }
    assert (
        queue_service.claim_task_execution_launch_reconciliation(
            server_instance_id="reconciler-b",
            now=observed_now + timedelta(seconds=5),
        )
        is None
    )
    with pytest.raises(queue_service.ChatPromptQueueRecordNotFound):
        queue_service.activate_server_dispatch_record(
            scope=scope,
            queue_id=str(queued["queue_id"]),
            enqueue_submission_id="submission-launch-reconcile",
            task_execution_concept_id="#V#task-execution-launch-reconcile",
        )

    reclaimed = queue_service.claim_task_execution_launch_reconciliation(
        server_instance_id="reconciler-b",
        now=observed_now + timedelta(seconds=11),
        lease_seconds=10,
    )
    assert reclaimed is not None
    assert reclaimed["queue_id"] == queued["queue_id"]
    assert (
        reclaimed["task_launch_reconciliation_token"]
        != first["task_launch_reconciliation_token"]
    )
    assert not queue_service.acknowledge_task_execution_launch_reconciliation(
        queue_id=str(queued["queue_id"]),
        reconciliation_token=str(first["task_launch_reconciliation_token"]),
        server_instance_id="reconciler-a",
        task_execution_concept_id="#V#task-execution-launch-reconcile",
        enqueue_submission_id="submission-launch-reconcile",
        now=observed_now + timedelta(seconds=12),
    )
    assert queue_service.acknowledge_task_execution_launch_reconciliation(
        queue_id=str(queued["queue_id"]),
        reconciliation_token=str(reclaimed["task_launch_reconciliation_token"]),
        server_instance_id="reconciler-b",
        task_execution_concept_id="#V#task-execution-launch-reconcile",
        enqueue_submission_id="submission-launch-reconcile",
        now=observed_now + timedelta(seconds=12),
    )
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    persisted = coll.find_one({"queue_id": queued["queue_id"]})
    assert persisted is not None
    assert persisted["dispatch_ready"] is True
    assert "task_launch_reconciliation_token" not in persisted


def test_task_launch_outbox_release_then_permanent_failure_is_terminal() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    queued = _create_server_queue_record(
        scope=scope,
        submission_id="submission-launch-failure",
        task_concept_id="#V#task-launch-failure",
        task_execution_concept_id="#V#task-execution-launch-failure",
        dispatch_ready=False,
    )
    observed_now = datetime.now(timezone.utc)
    first = queue_service.claim_task_execution_launch_reconciliation(
        server_instance_id="reconciler-a",
        now=observed_now,
    )
    assert first is not None
    assert queue_service.release_task_execution_launch_reconciliation(
        queue_id=str(queued["queue_id"]),
        reconciliation_token=str(first["task_launch_reconciliation_token"]),
        server_instance_id="reconciler-a",
        error="temporary projection failure",
        retry_after_seconds=3,
        now=observed_now,
    )
    assert (
        queue_service.claim_task_execution_launch_reconciliation(
            server_instance_id="reconciler-b",
            now=observed_now + timedelta(seconds=2),
        )
        is None
    )
    retry = queue_service.claim_task_execution_launch_reconciliation(
        server_instance_id="reconciler-b",
        now=observed_now + timedelta(seconds=3),
    )
    assert retry is not None
    assert queue_service.fail_task_execution_launch_reconciliation(
        queue_id=str(queued["queue_id"]),
        reconciliation_token=str(retry["task_launch_reconciliation_token"]),
        server_instance_id="reconciler-b",
        error="task authority was revoked",
        now=observed_now + timedelta(seconds=3),
    )
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    persisted = coll.find_one({"queue_id": queued["queue_id"]})
    assert persisted is not None
    assert persisted["status"] == queue_service.STATUS_FAILED
    assert persisted["task_execution_reconciliation_status"] == "pending"
    assert "active_task_execution_key" not in persisted
    assert "purge_after" not in persisted


def test_server_dispatch_reservation_upgrades_exact_token_and_preserves_fifo() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    first = _create_server_queue_record(
        scope=scope,
        submission_id="submission-first",
        prompt="First",
    )
    second = _create_server_queue_record(
        scope=scope,
        submission_id="submission-second",
        prompt="Second",
    )
    observed_now = datetime.now(timezone.utc)
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    coll.update_one(
        {"queue_id": first["queue_id"]},
        {"$set": {"created_at": observed_now - timedelta(seconds=1)}},
    )
    coll.update_one(
        {"queue_id": second["queue_id"]},
        {"$set": {"created_at": observed_now}},
    )

    reservation = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-a",
        now=observed_now,
        lease_seconds=60,
    )
    assert reservation is not None
    assert reservation["queue_id"] == first["queue_id"]
    assert reservation["user_concept_id"] == "#V#user"
    assert reservation["organisation_concept_id"] == "#V#org"
    assert reservation["namespace"] == "#V#user@org"
    assert reservation["execution_envelope"] == {
        "language": "en-NZ",
        "skip_buttonify": True,
    }
    token = str(reservation["dispatch_reservation_token"])
    assert token
    assert (
        queue_service.reserve_next_server_dispatch(
            server_instance_id="server-b",
            now=observed_now,
        )
        is None
    )
    visible = queue_service.list_active_queue_records(scope=scope)
    assert "dispatch_reservation_token" not in visible[0]
    assert "execution_envelope" not in visible[0]

    assert not queue_service.heartbeat_server_dispatch_reservation(
        queue_id=str(first["queue_id"]),
        dispatch_reservation_token="wrong-token",
        server_instance_id="server-a",
        now=observed_now + timedelta(seconds=10),
    )
    assert queue_service.heartbeat_server_dispatch_reservation(
        queue_id=str(first["queue_id"]),
        dispatch_reservation_token=token,
        server_instance_id="server-a",
        now=observed_now + timedelta(seconds=10),
        lease_seconds=60,
    )

    with pytest.raises(queue_service.ConversationTurnAlreadyActive):
        queue_service.bind_queue_record_to_turn(
            scope=scope,
            queue_id=str(first["queue_id"]),
            client_request_id=str(reservation["client_request_id"]),
            attempt_id=str(reservation["attempt_id"]),
            conversation_key="conversation-server",
            server_instance_id="server-a",
            dispatch_reservation_token="wrong-token",
        )
    bound = queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=str(first["queue_id"]),
        client_request_id=str(reservation["client_request_id"]),
        attempt_id=str(reservation["attempt_id"]),
        conversation_key="conversation-server",
        server_instance_id="server-a",
        dispatch_reservation_token=token,
        lease_seconds=60,
    )
    assert bound["status"] == queue_service.STATUS_IN_PROGRESS
    assert bound["lease_expires_at"] is not None

    persisted = coll.find_one({"queue_id": first["queue_id"]})
    assert persisted is not None
    assert "dispatch_reservation_token" not in persisted
    assert persisted["active_conversation_key"] == "conversation-server"

    queue_service.finish_prompt_record(
        scope=scope,
        queue_id=str(first["queue_id"]),
        attempt_id=str(reservation["attempt_id"]),
        status=queue_service.STATUS_COMPLETED,
    )
    next_reservation = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-b",
        now=observed_now + timedelta(seconds=11),
    )
    assert next_reservation is not None
    assert next_reservation["queue_id"] == second["queue_id"]


def test_blocked_fifo_does_not_starve_an_unrelated_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(queue_service, "_now", lambda: fixed_now)
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    first_conversation = [
        _create_server_queue_record(
            scope=scope,
            submission_id=f"submission-blocked-{index}",
            conversation_key="conversation-blocked",
            session_id="session-blocked",
        )
        for index in range(3)
    ]
    independent = _create_server_queue_record(
        scope=scope,
        submission_id="submission-independent",
        conversation_key="conversation-independent",
        session_id="session-independent",
    )

    first = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-a",
        scan_limit=2,
    )
    assert first is not None
    assert first["queue_id"] == first_conversation[0]["queue_id"]

    second = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-a",
        scan_limit=2,
    )
    assert second is not None
    assert second["queue_id"] == independent["queue_id"]

    assert queue_service.fail_server_dispatch_reservation(
        queue_id=str(first["queue_id"]),
        dispatch_reservation_token=str(first["dispatch_reservation_token"]),
        server_instance_id="server-a",
        error="bounded test completion",
    )
    third = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-b",
        scan_limit=2,
    )
    assert third is not None
    assert third["queue_id"] == first_conversation[1]["queue_id"]


def test_server_dispatch_release_backoff_and_terminal_failure_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_CHAT_PROMPT_QUEUE_TERMINAL_RETENTION_SECONDS", "60")
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    legacy = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Browser-owned legacy task",
        conversation_key="legacy-conversation",
    )
    queued = _create_server_queue_record(
        scope=scope,
        submission_id="submission-release",
        conversation_key="dispatch-conversation",
    )
    observed_now = datetime.now(timezone.utc)

    reservation = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-a",
        now=observed_now,
        lease_seconds=30,
    )
    assert reservation is not None
    assert reservation["queue_id"] == queued["queue_id"]
    assert reservation["queue_id"] != legacy["queue_id"]
    assert queue_service.release_server_dispatch_reservation(
        queue_id=str(queued["queue_id"]),
        dispatch_reservation_token=str(reservation["dispatch_reservation_token"]),
        server_instance_id="server-a",
        retry_after_seconds=10,
        error="background capacity",
        now=observed_now,
    )
    assert not queue_service.heartbeat_server_dispatch_reservation(
        queue_id=str(queued["queue_id"]),
        dispatch_reservation_token=str(reservation["dispatch_reservation_token"]),
        server_instance_id="server-a",
        now=observed_now + timedelta(seconds=1),
    )
    assert (
        queue_service.reserve_next_server_dispatch(
            server_instance_id="server-b",
            now=observed_now + timedelta(seconds=5),
        )
        is None
    )

    retried = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-b",
        now=observed_now + timedelta(seconds=11),
    )
    assert retried is not None
    assert retried["queue_id"] == queued["queue_id"]
    assert (
        retried["dispatch_reservation_token"]
        != reservation["dispatch_reservation_token"]
    )
    assert retried["client_request_id"] != reservation["client_request_id"]
    assert retried["attempt_id"] != reservation["attempt_id"]
    assert queue_service.fail_server_dispatch_reservation(
        queue_id=str(queued["queue_id"]),
        dispatch_reservation_token=str(retried["dispatch_reservation_token"]),
        server_instance_id="server-b",
        error="authority_revoked",
        now=observed_now + timedelta(seconds=11),
    )

    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    persisted = coll.find_one({"queue_id": queued["queue_id"]})
    assert persisted is not None
    assert persisted["status"] == queue_service.STATUS_FAILED
    assert persisted["last_error"] == "authority_revoked"
    assert (persisted["purge_after"] - persisted["completed_at"]).total_seconds() == 60
    assert "active_conversation_key" not in persisted
    assert "dispatch_reservation_token" not in persisted
    assert "purge_after" not in (coll.find_one({"queue_id": legacy["queue_id"]}) or {})


def test_linked_terminal_marker_survives_process_loss_until_exact_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_CHAT_PROMPT_QUEUE_TERMINAL_RETENTION_SECONDS", "60")
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    queued = _create_server_queue_record(
        scope=scope,
        submission_id="submission-terminal-reconciliation",
        task_concept_id="#V#task_1",
        task_execution_concept_id="#V#task_execution_1",
    )
    reservation = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-a"
    )
    assert reservation is not None
    queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=str(queued["queue_id"]),
        client_request_id=str(reservation["client_request_id"]),
        attempt_id=str(reservation["attempt_id"]),
        conversation_key="conversation-server",
        server_instance_id="server-a",
        dispatch_reservation_token=str(reservation["dispatch_reservation_token"]),
    )

    terminal = queue_service.finish_prompt_record(
        scope=scope,
        queue_id=str(queued["queue_id"]),
        attempt_id=str(reservation["attempt_id"]),
        status=queue_service.STATUS_COMPLETED,
    )
    assert terminal["task_execution_reconciliation_status"] == "pending"
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    persisted = coll.find_one({"queue_id": queued["queue_id"]})
    assert persisted is not None
    assert "purge_after" not in persisted

    observed_now = datetime.now(timezone.utc)
    first_claim = queue_service.claim_task_execution_terminal_reconciliation(
        server_instance_id="reconciler-a",
        now=observed_now,
        lease_seconds=10,
    )
    assert first_claim is not None
    assert (
        queue_service.claim_task_execution_terminal_reconciliation(
            server_instance_id="reconciler-b",
            now=observed_now + timedelta(seconds=5),
        )
        is None
    )
    reclaimed = queue_service.claim_task_execution_terminal_reconciliation(
        server_instance_id="reconciler-b",
        now=observed_now + timedelta(seconds=11),
        lease_seconds=10,
    )
    assert reclaimed is not None
    assert reclaimed["queue_id"] == queued["queue_id"]
    assert (
        reclaimed["task_execution_reconciliation_token"]
        != first_claim["task_execution_reconciliation_token"]
    )
    assert not queue_service.acknowledge_task_execution_terminal_reconciliation(
        queue_id=str(queued["queue_id"]),
        reconciliation_token=str(
            first_claim["task_execution_reconciliation_token"]
        ),
        server_instance_id="reconciler-a",
        task_execution_concept_id="#V#task_execution_1",
        enqueue_submission_id="submission-terminal-reconciliation",
        queue_status=queue_service.STATUS_COMPLETED,
        now=observed_now + timedelta(seconds=12),
    )
    assert queue_service.acknowledge_task_execution_terminal_reconciliation(
        queue_id=str(queued["queue_id"]),
        reconciliation_token=str(reclaimed["task_execution_reconciliation_token"]),
        server_instance_id="reconciler-b",
        task_execution_concept_id="#V#task_execution_1",
        enqueue_submission_id="submission-terminal-reconciliation",
        queue_status=queue_service.STATUS_COMPLETED,
        now=observed_now + timedelta(seconds=12),
    )
    acknowledged = coll.find_one({"queue_id": queued["queue_id"]})
    assert acknowledged is not None
    assert acknowledged["task_execution_reconciliation_status"] == "acknowledged"
    assert (
        acknowledged["purge_after"] - acknowledged["task_execution_reconciled_at"]
    ).total_seconds() == 60


def test_linked_pre_admission_failure_and_cancellation_defer_retention() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    failed_row = _create_server_queue_record(
        scope=scope,
        submission_id="submission-linked-failure",
        conversation_key="conversation-linked-failure",
        task_concept_id="#V#task_failure",
        task_execution_concept_id="#V#task_execution_failure",
    )
    reservation = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-a"
    )
    assert reservation is not None
    assert queue_service.fail_server_dispatch_reservation(
        queue_id=str(failed_row["queue_id"]),
        dispatch_reservation_token=str(reservation["dispatch_reservation_token"]),
        server_instance_id="server-a",
        error="authority revoked",
    )
    cancelled_row = _create_server_queue_record(
        scope=scope,
        submission_id="submission-linked-cancel",
        conversation_key="conversation-linked-cancel",
        task_concept_id="#V#task_cancel",
        task_execution_concept_id="#V#task_execution_cancel",
    )
    queue_service.cancel_prompt_record(
        scope=scope,
        queue_id=str(cancelled_row["queue_id"]),
    )

    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    for queue_id, expected_status in (
        (failed_row["queue_id"], queue_service.STATUS_FAILED),
        (cancelled_row["queue_id"], queue_service.STATUS_CANCELLED),
    ):
        persisted = coll.find_one({"queue_id": queue_id})
        assert persisted is not None
        assert persisted["status"] == expected_status
        assert persisted["task_execution_reconciliation_status"] == "pending"
        assert "purge_after" not in persisted


def test_linked_terminal_backfill_removes_ttl_before_general_retention_backfill() -> None:
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    observed_now = datetime.now(timezone.utc)
    coll.insert_one(
        {
            "queue_id": "legacy-linked-terminal",
            "status": queue_service.STATUS_FAILED,
            "task_concept_id": "#V#task_legacy",
            "task_execution_concept_id": "#V#task_execution_legacy",
            "last_error": "legacy failure",
            "completed_at": observed_now - timedelta(days=2),
            "updated_at": observed_now - timedelta(days=2),
            "purge_after": observed_now + timedelta(days=1),
        }
    )

    assert queue_service.backfill_linked_terminal_reconciliation(now=observed_now) == 1
    assert queue_service.backfill_legacy_terminal_purge_after(now=observed_now) == 0
    persisted = coll.find_one({"queue_id": "legacy-linked-terminal"})
    assert persisted is not None
    assert persisted["task_execution_reconciliation_status"] == "pending"
    assert "purge_after" not in persisted


def test_expired_server_dispatch_reservation_is_reclaimed_by_exact_cas() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    queued = _create_server_queue_record(
        scope=scope,
        submission_id="submission-expired-lease",
    )
    observed_now = datetime.now(timezone.utc)
    first = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-old",
        now=observed_now,
        lease_seconds=10,
    )
    assert first is not None
    reclaimed = queue_service.reserve_next_server_dispatch(
        server_instance_id="server-new",
        now=observed_now + timedelta(seconds=11),
        lease_seconds=10,
    )
    assert reclaimed is not None
    assert reclaimed["queue_id"] == queued["queue_id"]
    assert (
        reclaimed["dispatch_reservation_token"] != first["dispatch_reservation_token"]
    )
    assert not queue_service.release_server_dispatch_reservation(
        queue_id=str(queued["queue_id"]),
        dispatch_reservation_token=str(first["dispatch_reservation_token"]),
        server_instance_id="server-old",
    )
    with pytest.raises(queue_service.ChatPromptQueueError):
        queue_service.bind_queue_record_to_turn(
            scope=scope,
            queue_id=str(queued["queue_id"]),
            client_request_id=str(first["client_request_id"]),
            attempt_id=str(first["attempt_id"]),
            conversation_key="conversation-server",
            server_instance_id="server-old",
            dispatch_reservation_token=str(first["dispatch_reservation_token"]),
        )

    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    coll.update_one(
        {"queue_id": queued["queue_id"]},
        {"$set": {"dispatch_lease_expires_at": observed_now - timedelta(seconds=1)}},
    )
    with pytest.raises(queue_service.ConversationTurnAlreadyActive):
        queue_service.bind_queue_record_to_turn(
            scope=scope,
            queue_id=str(queued["queue_id"]),
            client_request_id=str(reclaimed["client_request_id"]),
            attempt_id=str(reclaimed["attempt_id"]),
            conversation_key="conversation-server",
            server_instance_id="server-new",
            dispatch_reservation_token=str(reclaimed["dispatch_reservation_token"]),
        )


def test_admitted_lease_heartbeat_controls_stale_advisory() -> None:
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    queued = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Long-running turn",
        conversation_key="conversation-long",
    )
    bound = queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=str(queued["queue_id"]),
        client_request_id="request-long",
        attempt_id="attempt-long",
        conversation_key="conversation-long",
        server_instance_id="server-a",
        lease_seconds=60,
    )
    assert bound["lease_expires_at"] is not None
    observed_now = datetime.now(timezone.utc)
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    coll.update_one(
        {"queue_id": queued["queue_id"]},
        {
            "$set": {
                "claimed_at": observed_now - timedelta(days=2),
                "lease_heartbeat_at": observed_now,
                "lease_expires_at": observed_now + timedelta(seconds=30),
            }
        },
    )

    assert (
        queue_service.expire_stale_in_progress_records(
            scope=scope,
            now=observed_now,
        )
        == 0
    )
    assert not queue_service.heartbeat_bound_turn(
        scope=scope,
        queue_id=str(queued["queue_id"]),
        attempt_id="attempt-long",
        server_instance_id="server-b",
        now=observed_now + timedelta(seconds=5),
    )
    assert queue_service.heartbeat_bound_turn(
        scope=scope,
        queue_id=str(queued["queue_id"]),
        attempt_id="attempt-long",
        server_instance_id="server-a",
        now=observed_now + timedelta(seconds=5),
        lease_seconds=60,
    )

    coll.update_one(
        {"queue_id": queued["queue_id"]},
        {"$set": {"lease_expires_at": observed_now - timedelta(seconds=1)}},
    )
    assert (
        queue_service.expire_stale_in_progress_records(
            scope=scope,
            now=observed_now,
        )
        == 1
    )
    stale = coll.find_one({"queue_id": queued["queue_id"]})
    assert stale is not None
    assert stale["stale_advisory"] is True
    assert stale["reconciliation_required"] is True


def test_legacy_terminal_purge_backfill_is_bounded_and_has_rollout_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_CHAT_PROMPT_QUEUE_TERMINAL_RETENTION_SECONDS", "60")
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    observed_now = datetime.now(timezone.utc)
    old_terminal_at = observed_now - timedelta(days=60)
    for queue_id, status in (
        ("legacy-completed", queue_service.STATUS_COMPLETED),
        ("legacy-failed", queue_service.STATUS_FAILED),
    ):
        coll.insert_one(
            {
                "queue_id": queue_id,
                **scope,
                "prompt_raw": queue_id,
                "status": status,
                "created_at": old_terminal_at,
                "updated_at": old_terminal_at,
                "completed_at": old_terminal_at,
            }
        )
    coll.insert_one(
        {
            "queue_id": "legacy-active",
            **scope,
            "prompt_raw": "must never get TTL",
            "status": queue_service.STATUS_IN_PROGRESS,
            "created_at": old_terminal_at,
            "updated_at": old_terminal_at,
            "completed_at": None,
        }
    )

    assert (
        queue_service.backfill_legacy_terminal_purge_after(
            now=observed_now,
            batch_size=1,
            rollout_grace_seconds=7 * 24 * 60 * 60,
        )
        == 1
    )
    assert coll.count_documents({"purge_after": {"$exists": True}}) == 1
    assert (
        queue_service.backfill_legacy_terminal_purge_after(
            now=observed_now,
            batch_size=500,
            rollout_grace_seconds=7 * 24 * 60 * 60,
        )
        == 1
    )
    terminals = list(
        coll.find({"status": {"$in": list(queue_service.TERMINAL_STATUSES)}})
    )
    assert len(terminals) == 2
    for terminal in terminals:
        assert terminal["purge_after"] >= terminal["completed_at"] + timedelta(
            seconds=60
        )
        # BSON stores millisecond precision, so allow the sub-millisecond part
        # of the supplied clock to be truncated by mongomock.
        assert terminal["purge_after"] >= (
            observed_now.replace(tzinfo=None)
            + timedelta(days=7)
            - timedelta(milliseconds=1)
        )
    active = coll.find_one({"queue_id": "legacy-active"})
    assert active is not None
    assert "purge_after" not in active


def test_restart_hands_legacy_work_to_one_server_successor_with_receipt_context(
    monkeypatch,
):
    from src.backend.services import turn_execution_record_service as turns

    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    original = queue_service.create_queue_record(
        scope=scope, prompt_raw="Create a task", session_id="speech-restart"
    )
    queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=original["queue_id"],
        client_request_id="original-request",
        attempt_id="original-attempt",
        conversation_key="restart-conversation",
        server_instance_id="old-server",
    )
    monkeypatch.setattr(
        turns,
        "get_turn_execution_record_projection",
        lambda **kw: {
            "user_id": "#V#user",
            "request_id": "original-request",
            "required_effects": [
                {"effect_id": "already-created", "status": "succeeded"}
            ],
        },
    )
    kwargs = dict(
        scope=scope,
        queue_id=original["queue_id"],
        current_server_instance_id="new-server",
        execution_envelope={"language": "en-NZ"},
    )
    successor = queue_service.resume_legacy_prompt_record(**kwargs)
    replay = queue_service.resume_legacy_prompt_record(**kwargs)
    assert replay["queue_id"] == successor["queue_id"]
    assert successor["dispatch_mode"] == "server" and successor["dispatch_ready"]
    row = queue_service._collection().find_one({"queue_id": successor["queue_id"]})
    recovery = row["execution_envelope"]["workflow_inputs"]["interrupted_turn"]
    assert recovery["original_attempt"]["attempt_id"] == "original-attempt"
    assert (
        recovery["receipt_observations"]["required_effects"][0]["effect_id"]
        == "already-created"
    )
    assert (
        queue_service._collection().find_one({"queue_id": original["queue_id"]})[
            "status"
        ]
        == "cancelled"
    )
    claimed = queue_service.reserve_next_server_dispatch(
        server_instance_id="new-server"
    )
    assert claimed["queue_id"] == successor["queue_id"]


def test_resume_previously_requeued_row_without_attempt_identity_keeps_outcome_unknown():
    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    original = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="Is that task available in Tasks?",
        session_id="legacy-requeued",
    )
    queue_service._collection().update_one(
        {"queue_id": original["queue_id"]}, {"$set": {"source": "restart"}}
    )
    result = queue_service.resume_legacy_prompt_record(
        scope=scope,
        queue_id=original["queue_id"],
        current_server_instance_id="new-server",
        execution_envelope={},
        resolve_conversation=lambda session_id: {"conversation_key": "resolved-legacy-conversation"},
    )
    assert result["dispatch_mode"] == "server" and result["dispatch_ready"]
    row = queue_service._collection().find_one({"queue_id": result["queue_id"]})
    recovery = row["execution_envelope"]["workflow_inputs"]["interrupted_turn"]
    assert recovery["unobserved_effect_outcome"] == "unknown_not_failed"
    assert recovery["receipt_observations"] is None
    assert recovery["original_attempt"]["client_request_id"] is None


def test_restart_reconciles_existing_assistant_result_without_replaying_work():
    from src.backend.services.chat_history_service import (
        get_chat_history_collection_service,
    )

    scope = queue_service.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    original = queue_service.create_queue_record(
        scope=scope, prompt_raw="Create a task", session_id="already-completed"
    )
    queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=original["queue_id"],
        client_request_id="completed-request",
        attempt_id="completed-attempt",
        conversation_key="completed-conversation",
        server_instance_id="old-server",
    )
    get_chat_history_collection_service().insert_one(
        {
            "user_id": "#V#user",
            "namespace": "#V#user@org",
            "session_id": "already-completed",
            "history": [
                {
                    "role": "assistant",
                    "content": "Task created",
                    "llm_debug_data": {"request_id": "completed-request"},
                }
            ],
        }
    )
    result = queue_service.resume_legacy_prompt_record(
        scope=scope,
        queue_id=original["queue_id"],
        current_server_instance_id="new-server",
        execution_envelope={},
    )
    assert result["status"] == "completed" and result["recovered_terminal"]
    assert (
        queue_service._collection().count_documents(
            {"handoff_source_queue_id": original["queue_id"]}
        )
        == 0
    )


def test_attachment_only_queue_preserves_exact_descriptor_ids():
    scope = queue_service.build_queue_scope(user_concept_id="#V#alice", organisation_concept_id="#V#org")
    record = _create_server_queue_record(scope=scope, submission_id="attachment-only",
        prompt="", execution_envelope={"image_attachment_ids":["#V#file"]})
    assert record["prompt_raw"] == ""
    persisted = mongo_client.get_chat_prompt_queue_collection().find_one({"queue_id": record["queue_id"]})
    assert persisted["execution_envelope"]["image_attachment_ids"] == ["#V#file"]
