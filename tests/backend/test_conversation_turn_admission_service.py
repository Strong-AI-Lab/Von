from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.backend.db import mongo_client
from src.backend.services import chat_prompt_queue_service as queue_service
from src.backend.services.conversation_turn_admission_service import (
    ConversationTurnActive,
    ConversationTurnAdmissionService,
    ConversationTurnCapacityReached,
    build_conversation_key,
)


@pytest.fixture(autouse=True)
def mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_conversation_turn_admission")
    mongo_client.close_connection()
    yield
    mongo_client.close_connection()


def _scope(user: str = "#V#user", org: str = "#V#org") -> dict[str, str]:
    return queue_service.build_queue_scope(
        user_concept_id=user,
        organisation_concept_id=org,
        namespace=f"{user}@{org.removeprefix('#V#')}",
    )


def _key(session_id: str, owner: str = "#V#user") -> str:
    return build_conversation_key(
        owner_user_id=owner,
        history_namespace=f"{owner}@org",
        conversation_session_id=session_id,
    )


def _queued(scope: dict[str, str], session_id: str, prompt: str) -> str:
    record = queue_service.create_queue_record(
        scope=scope,
        prompt_raw=prompt,
        session_id=session_id,
    )
    return str(record["queue_id"])


def test_default_global_limit_preserves_http_thread_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VON_MAX_ACTIVE_TURNS_GLOBAL", raising=False)
    monkeypatch.setenv("VON_WAITRESS_THREADS", "16")
    monkeypatch.setenv("VON_TURN_HTTP_HEADROOM_THREADS", "8")

    service = ConversationTurnAdmissionService()

    assert service.snapshot()["global_limit"] == 8
    assert service.snapshot()["configured_http_threads"] == 16
    assert service.snapshot()["available_http_thread_headroom"] == 8


def test_explicit_global_limit_remains_observable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_WAITRESS_THREADS", "32")
    monkeypatch.setenv("VON_MAX_ACTIVE_TURNS_GLOBAL", "20")

    service = ConversationTurnAdmissionService()

    assert service.snapshot()["global_limit"] == 20
    assert service.snapshot()["available_http_thread_headroom"] == 12


def test_distinct_conversations_run_concurrently_and_release_exactly() -> None:
    scope = _scope()
    service = ConversationTurnAdmissionService(per_user_limit=2, global_limit=4)

    first = service.acquire(
        scope=scope,
        prompt_raw="first",
        session_id="conversation-a",
        session_name=None,
        client_request_id="request-a",
        conversation_key=_key("conversation-a"),
        queue_id=_queued(scope, "conversation-a", "first"),
    )
    second = service.acquire(
        scope=scope,
        prompt_raw="second",
        session_id="conversation-b",
        session_name=None,
        client_request_id="request-b",
        conversation_key=_key("conversation-b"),
        queue_id=_queued(scope, "conversation-b", "second"),
    )

    assert service.snapshot()["active_global"] == 2
    service.release(first, status=queue_service.STATUS_COMPLETED)
    service.release(first, status=queue_service.STATUS_COMPLETED)
    assert service.snapshot()["active_global"] == 1
    service.release(second, status=queue_service.STATUS_COMPLETED)
    assert service.snapshot()["active_global"] == 0


def test_legacy_queue_first_converges_with_generate_admission() -> None:
    scope = _scope()
    conversation_key = _key("legacy-queue-first")
    queue_first = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="queue first",
        session_id="legacy-queue-first",
        conversation_key=conversation_key,
        legacy_submission_role=queue_service.LEGACY_SUBMISSION_ROLE_QUEUE,
        window_session_id="window-queue-first",
    )
    service = ConversationTurnAdmissionService(per_user_limit=2, global_limit=2)

    token = service.acquire(
        scope=scope,
        prompt_raw="queue first",
        session_id="legacy-queue-first",
        session_name=None,
        client_request_id="request-queue-first",
        conversation_key=conversation_key,
        window_session_id="window-queue-first",
    )

    assert token.queue_id == queue_first["queue_id"]
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    assert coll.count_documents({}) == 1
    service.release(token, status=queue_service.STATUS_COMPLETED)
    persisted = coll.find_one({"queue_id": token.queue_id})
    assert persisted is not None
    assert "active_legacy_submission_key" not in persisted


def test_legacy_generate_first_converges_with_queue_create() -> None:
    scope = _scope()
    conversation_key = _key("legacy-generate-first")
    service = ConversationTurnAdmissionService(per_user_limit=2, global_limit=2)

    token = service.acquire(
        scope=scope,
        prompt_raw="generate first",
        session_id="legacy-generate-first",
        session_name=None,
        client_request_id="request-generate-first",
        conversation_key=conversation_key,
        window_session_id="window-generate-first",
    )
    service.release(token, status=queue_service.STATUS_COMPLETED)
    queue_second = queue_service.create_queue_record(
        scope=scope,
        prompt_raw="generate first",
        session_id="legacy-generate-first",
        conversation_key=conversation_key,
        legacy_submission_role=queue_service.LEGACY_SUBMISSION_ROLE_QUEUE,
        window_session_id="window-generate-first",
    )

    assert queue_second["queue_id"] == token.queue_id
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    assert coll.count_documents({}) == 1
    persisted = coll.find_one({"queue_id": token.queue_id})
    assert persisted is not None
    assert persisted["status"] == queue_service.STATUS_COMPLETED
    assert "active_legacy_submission_key" not in persisted


def test_terminal_unpaired_generate_does_not_block_new_request_id() -> None:
    scope = _scope()
    conversation_key = _key("legacy-generate-repeat")
    service = ConversationTurnAdmissionService(per_user_limit=2, global_limit=2)
    acquire_kwargs = {
        "scope": scope,
        "prompt_raw": "send the same prompt again",
        "session_id": "legacy-generate-repeat",
        "session_name": None,
        "conversation_key": conversation_key,
        "window_session_id": "window-generate-repeat",
    }

    first = service.acquire(
        **acquire_kwargs,
        client_request_id="request-generate-first",
    )
    service.release(first, status=queue_service.STATUS_COMPLETED)
    second = service.acquire(
        **acquire_kwargs,
        client_request_id="request-generate-second",
    )

    assert second.queue_id != first.queue_id
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    assert coll.count_documents({}) == 2
    old_record = coll.find_one({"queue_id": first.queue_id})
    assert old_record is not None
    assert old_record["status"] == queue_service.STATUS_COMPLETED
    assert "active_legacy_submission_key" not in old_record
    service.release(second, status=queue_service.STATUS_COMPLETED)


def test_release_retries_durable_terminalisation_before_freeing_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()
    service = ConversationTurnAdmissionService(per_user_limit=1, global_limit=1)
    token = service.acquire(
        scope=scope,
        prompt_raw="retry terminalisation",
        session_id="conversation-a",
        session_name=None,
        client_request_id="request-a",
        conversation_key=_key("conversation-a"),
        queue_id=_queued(scope, "conversation-a", "retry terminalisation"),
    )
    original_finish = queue_service.finish_prompt_record
    attempts = 0

    def _flaky_finish(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary database failure")
        return original_finish(**kwargs)

    monkeypatch.setattr(queue_service, "finish_prompt_record", _flaky_finish)

    with pytest.raises(RuntimeError, match="temporary database failure"):
        service.release(token, status=queue_service.STATUS_COMPLETED)
    assert service.snapshot()["active_global"] == 1
    assert token.released is False

    service.release(token, status=queue_service.STATUS_COMPLETED)
    assert service.snapshot()["active_global"] == 0
    assert token.released is True
    service.shutdown()


def test_release_reconciles_asynchronously_after_both_request_hooks_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()
    service = ConversationTurnAdmissionService(
        per_user_limit=1,
        global_limit=1,
        terminalisation_retry_initial_delay_sec=0.2,
        terminalisation_retry_max_delay_sec=0.2,
    )
    token = service.acquire(
        scope=scope,
        prompt_raw="reconcile terminalisation",
        session_id="conversation-a",
        session_name=None,
        client_request_id="request-a",
        conversation_key=_key("conversation-a"),
        queue_id=_queued(scope, "conversation-a", "reconcile terminalisation"),
    )
    original_finish = queue_service.finish_prompt_record
    terminalised = threading.Event()
    attempts: list[tuple[str, str | None, str | None]] = []

    def _fail_hooks_then_reconcile(**kwargs):
        attempts.append(
            (
                kwargs["status"],
                kwargs.get("error"),
                kwargs.get("attempt_id"),
            )
        )
        if len(attempts) <= 2:
            raise RuntimeError("database unavailable through request teardown")
        result = original_finish(**kwargs)
        terminalised.set()
        return result

    monkeypatch.setattr(
        queue_service,
        "finish_prompt_record",
        _fail_hooks_then_reconcile,
    )

    try:
        for _ in range(2):
            with pytest.raises(
                RuntimeError,
                match="database unavailable through request teardown",
            ):
                service.release(
                    token,
                    status=queue_service.STATUS_CANCELLED,
                    error="cancelled at request boundary",
                )

        assert service.snapshot()["active_global"] == 1
        assert token.released is False
        assert terminalised.wait(timeout=2)
        assert service.snapshot()["active_global"] == 0
        assert token.released is True
        assert len(attempts) == 3
        assert all(
            status == queue_service.STATUS_CANCELLED
            and error == "cancelled at request boundary"
            and attempt_id == token.attempt_id
            for status, error, attempt_id in attempts
        )
    finally:
        service.shutdown()


def test_same_conversation_has_exactly_one_racing_winner() -> None:
    scope = _scope()
    service = ConversationTurnAdmissionService(per_user_limit=4, global_limit=4)
    queue_ids = [
        _queued(scope, "same-conversation", "first"),
        _queued(scope, "same-conversation", "second"),
    ]
    barrier = threading.Barrier(2)

    def _acquire(index: int):
        barrier.wait(timeout=2)
        try:
            return service.acquire(
                scope=scope,
                prompt_raw=f"prompt-{index}",
                session_id="same-conversation",
                session_name=None,
                client_request_id=f"request-{index}",
                conversation_key=_key("same-conversation"),
                queue_id=queue_ids[index],
            )
        except ConversationTurnActive:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_acquire, range(2)))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert service.snapshot()["active_global"] == 1
    service.release(winners[0], status=queue_service.STATUS_COMPLETED)


def test_user_limit_spans_organisations_but_other_actor_can_progress() -> None:
    service = ConversationTurnAdmissionService(per_user_limit=1, global_limit=3)
    org_a = _scope(org="#V#org_a")
    org_b = _scope(org="#V#org_b")
    other = _scope(user="#V#other", org="#V#org_b")

    first = service.acquire(
        scope=org_a,
        prompt_raw="one",
        session_id="a",
        session_name=None,
        client_request_id="request-a",
        conversation_key=_key("a"),
        queue_id=_queued(org_a, "a", "one"),
    )
    with pytest.raises(ConversationTurnCapacityReached):
        service.acquire(
            scope=org_b,
            prompt_raw="two",
            session_id="b",
            session_name=None,
            client_request_id="request-b",
            conversation_key=_key("b"),
            queue_id=_queued(org_b, "b", "two"),
        )

    other_token = service.acquire(
        scope=other,
        prompt_raw="other",
        session_id="other",
        session_name=None,
        client_request_id="request-other",
        conversation_key=_key("other", owner="#V#other"),
        queue_id=_queued(other, "other", "other"),
    )
    assert service.snapshot()["active_global"] == 2
    service.release(first, status=queue_service.STATUS_COMPLETED)
    service.release(other_token, status=queue_service.STATUS_COMPLETED)


def test_shared_owner_identity_builds_one_conversation_key() -> None:
    owner_key = build_conversation_key(
        owner_user_id="#V#owner",
        history_namespace="#V#owner@org",
        conversation_session_id="shared-session",
    )
    invitee_view_key = build_conversation_key(
        owner_user_id="#V#owner",
        history_namespace="#V#owner@org",
        conversation_session_id="shared-session",
    )

    assert owner_key == invitee_view_key


def test_ephemeral_turns_share_global_capacity_and_conversation_single_flight() -> None:
    service = ConversationTurnAdmissionService(per_user_limit=2, global_limit=2)
    first = service.acquire_ephemeral(
        actor_capacity_key="anonymous-browser-a",
        conversation_key="anonymous-conversation-a",
        client_request_id="anonymous-request-a",
    )
    second = service.acquire_ephemeral(
        actor_capacity_key="anonymous-browser-b",
        conversation_key="anonymous-conversation-b",
        client_request_id="anonymous-request-b",
    )

    assert service.snapshot()["active_global"] == 2
    with pytest.raises(ConversationTurnCapacityReached):
        service.acquire_ephemeral(
            actor_capacity_key="anonymous-browser-c",
            conversation_key="anonymous-conversation-c",
            client_request_id="anonymous-request-c",
        )
    # Free global capacity so the next assertion discriminates the
    # conversation fence from the global ceiling.
    service.release(second, status=queue_service.STATUS_COMPLETED)
    with pytest.raises(ConversationTurnActive):
        service.acquire_ephemeral(
            actor_capacity_key="anonymous-browser-a",
            conversation_key="anonymous-conversation-a",
            client_request_id="anonymous-request-a-2",
        )

    service.release(first, status=queue_service.STATUS_COMPLETED)
    assert service.snapshot()["active_global"] == 0


@pytest.mark.parametrize("user_count", [10, 20, 50])
def test_representative_user_load_admits_and_recovers_in_bounded_waves(
    user_count: int,
) -> None:
    """Representative bursts use the production 24-global/4-user bounds."""

    # Prewarm the lazy mock client and indexes before worker threads. Without
    # this, concurrent first access can create separate Mongo-mock clients and
    # test the fixture bootstrap rather than turn admission.
    assert mongo_client.get_chat_prompt_queue_collection() is not None
    service = ConversationTurnAdmissionService(per_user_limit=4, global_limit=24)
    work = []
    for index in range(user_count):
        user = f"#V#load_user_{index}"
        scope = _scope(user=user)
        session_id = f"load-conversation-{index}"
        work.append(
            (
                index,
                user,
                scope,
                session_id,
                _queued(scope, session_id, f"prompt {index}"),
            )
        )

    def _admit_wave(items):
        barrier = threading.Barrier(len(items))

        def _acquire(item):
            index, user, scope, session_id, queue_id = item
            barrier.wait(timeout=5)
            try:
                return item, service.acquire(
                    scope=scope,
                    prompt_raw=f"prompt {index}",
                    session_id=session_id,
                    session_name=None,
                    client_request_id=f"load-request-{index}",
                    conversation_key=_key(session_id, owner=user),
                    queue_id=queue_id,
                )
            except ConversationTurnCapacityReached:
                return item, None

        with ThreadPoolExecutor(max_workers=len(items)) as executor:
            return list(executor.map(_acquire, items))

    first_wave = _admit_wave(work)
    admitted = [token for _item, token in first_wave if token is not None]
    rejected = [item for item, token in first_wave if token is None]
    assert len(admitted) == min(user_count, 24)
    assert len(rejected) == max(0, user_count - 24)
    assert service.snapshot()["active_global"] == len(admitted)

    for token in admitted:
        service.release(token, status=queue_service.STATUS_COMPLETED)
    assert service.snapshot()["active_global"] == 0

    while rejected:
        next_items, rejected = rejected[:24], rejected[24:]
        recovery_wave = _admit_wave(next_items)
        recovery_tokens = [token for _item, token in recovery_wave if token is not None]
        assert len(recovery_tokens) == len(next_items)
        for token in recovery_tokens:
            service.release(token, status=queue_service.STATUS_COMPLETED)

    assert service.snapshot()["active_global"] == 0
    assert service.snapshot()["active_user_count"] == 0


def test_twenty_users_each_keep_one_active_and_one_queued_turn_visible() -> None:
    """Twenty users retain two isolated active-or-queued turns each."""

    assert mongo_client.get_chat_prompt_queue_collection() is not None
    user_count = 20
    service = ConversationTurnAdmissionService(per_user_limit=4, global_limit=24)
    work = []
    for index in range(user_count):
        user = f"#V#two_turn_user_{index}"
        scope = _scope(user=user)
        active_session = f"active-conversation-{index}"
        active_queue_id = _queued(scope, active_session, f"active prompt {index}")
        queued_queue_id = _queued(
            scope,
            f"queued-conversation-{index}",
            f"queued prompt {index}",
        )
        work.append(
            (index, user, scope, active_session, active_queue_id, queued_queue_id)
        )

    barrier = threading.Barrier(user_count)

    def _acquire(item):
        index, user, scope, session_id, queue_id, _queued_queue_id = item
        barrier.wait(timeout=5)
        return service.acquire(
            scope=scope,
            prompt_raw=f"active prompt {index}",
            session_id=session_id,
            session_name=None,
            client_request_id=f"two-turn-request-{index}",
            conversation_key=_key(session_id, owner=user),
            queue_id=queue_id,
        )

    with ThreadPoolExecutor(max_workers=user_count) as executor:
        tokens = list(executor.map(_acquire, work))

    assert service.snapshot()["active_global"] == user_count
    for item in work:
        _index, _user, scope, _session_id, active_queue_id, queued_queue_id = item
        visible = queue_service.list_active_queue_records(scope=scope)
        assert {record["queue_id"] for record in visible} == {
            active_queue_id,
            queued_queue_id,
        }
        assert {record["status"] for record in visible} == {
            queue_service.STATUS_IN_PROGRESS,
            queue_service.STATUS_QUEUED,
        }

    first_scope_ids = {
        record["queue_id"]
        for record in queue_service.list_active_queue_records(scope=work[0][2])
    }
    assert work[1][4] not in first_scope_ids
    assert work[1][5] not in first_scope_ids

    for token in tokens:
        service.release(token, status=queue_service.STATUS_COMPLETED)
    assert service.snapshot()["active_global"] == 0

    for item in work:
        _index, _user, scope, _session_id, active_queue_id, queued_queue_id = item
        visible = queue_service.list_active_queue_records(scope=scope)
        assert [record["queue_id"] for record in visible] == [queued_queue_id]
        queue_service.finish_prompt_record(
            scope=scope,
            queue_id=queued_queue_id,
            status=queue_service.STATUS_CANCELLED,
        )
        assert queue_service.list_active_queue_records(scope=scope) == []
        persisted_active = mongo_client.get_chat_prompt_queue_collection().find_one(
            {"queue_id": active_queue_id}
        )
        assert persisted_active is not None
        assert persisted_active["status"] == queue_service.STATUS_COMPLETED

    assert service.snapshot()["active_user_count"] == 0
