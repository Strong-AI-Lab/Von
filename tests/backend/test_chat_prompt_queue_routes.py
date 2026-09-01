from __future__ import annotations

import pytest
from flask import Flask

from src.backend.db import mongo_client
from src.backend.server.routes.von_routes import von_bp
from src.backend.services import chat_prompt_queue_service
from src.backend.services.conversation_turn_admission_service import SERVER_INSTANCE_ID


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    mongo_client.close_connection()

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(von_bp, url_prefix="/von")

    with app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["user_concept_id"] = "#V#test_user"
            sess["organisation_concept_id"] = "#V#test_org"
            sess["namespace"] = "#V#test_user@test_org"
        yield test_client

    mongo_client.close_connection()


def test_chat_prompt_queue_routes_restore_and_complete_record(client) -> None:
    create_resp = client.post(
        "/von/api/chat_prompt_queue",
        json={
            "prompt_raw": "Continue this later",
            "session_id": "session-1",
            "session_name": "Current",
        },
    )
    assert create_resp.status_code == 201
    created = create_resp.get_json()["item"]
    assert created["status"] == "queued"

    list_resp = client.get("/von/api/chat_prompt_queue")
    assert list_resp.status_code == 200
    assert [item["prompt_raw"] for item in list_resp.get_json()["items"]] == [
        "Continue this later"
    ]

    queue_id = created["queue_id"]
    update_resp = client.patch(
        f"/von/api/chat_prompt_queue/{queue_id}",
        json={"prompt_raw": "Edited queued task"},
    )
    assert update_resp.status_code == 200
    assert update_resp.get_json()["item"]["prompt_raw"] == "Edited queued task"

    claim_resp = client.post(f"/von/api/chat_prompt_queue/{queue_id}/claim")
    assert claim_resp.status_code == 410
    assert claim_resp.get_json()["error_code"] == "queue_claim_route_retired"

    chat_prompt_queue_service.claim_queue_record(
        scope=chat_prompt_queue_service.build_queue_scope(
            user_concept_id="#V#test_user",
            organisation_concept_id="#V#test_org",
            namespace="#V#test_user@test_org",
        ),
        queue_id=queue_id,
    )

    restartable_resp = client.get("/von/api/chat_prompt_queue")
    assert restartable_resp.status_code == 200
    assert restartable_resp.get_json()["items"][0]["status"] == "in_progress"

    requeue_resp = client.post(f"/von/api/chat_prompt_queue/{queue_id}/requeue")
    assert requeue_resp.status_code == 200
    assert requeue_resp.get_json()["item"]["status"] == "queued"

    finish_resp = client.post(
        f"/von/api/chat_prompt_queue/{queue_id}/finish",
        json={"status": "completed"},
    )
    assert finish_resp.status_code == 200

    final_list_resp = client.get("/von/api/chat_prompt_queue")
    assert final_list_resp.status_code == 200
    assert final_list_resp.get_json()["items"] == []


def test_chat_prompt_queue_create_returns_typed_backpressure(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VON_MAX_QUEUED_TURNS_GLOBAL", "10")
    monkeypatch.setenv("VON_MAX_QUEUED_TURNS_PER_USER", "1")
    first = client.post(
        "/von/api/chat_prompt_queue",
        json={"prompt_raw": "Accepted", "session_id": "session-1"},
    )
    rejected = client.post(
        "/von/api/chat_prompt_queue",
        json={"prompt_raw": "Rejected", "session_id": "session-2"},
    )

    assert first.status_code == 201
    assert first.get_json()["item"]["conversation_key"]
    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "1"
    assert rejected.get_json() == {
        "success": False,
        "error": "This user has reached the queued-turn backlog limit",
        "error_code": "foreground_queue_capacity_reached",
        "limit_kind": "user",
        "retryable": True,
        "retry_after_seconds": 1,
    }


def test_legacy_active_create_is_idempotent_but_explicit_attempts_are_not(
    client,
) -> None:
    from src.backend.services.window_session_context_service import (
        set_window_organisation,
    )

    set_window_organisation(
        "legacy-window",
        "#V#test_org",
        "member",
        "#V#test_user@test_org",
        user_id="#V#test_user",
    )
    headers = {"X-Von-Window-Session": "legacy-window"}
    legacy_payload = {
        "prompt_raw": "legacy active prompt",
        "session_id": "legacy-session",
        "status": "in_progress",
    }
    legacy_first = client.post(
        "/von/api/chat_prompt_queue",
        json=legacy_payload,
        headers=headers,
    )
    legacy_second = client.post(
        "/von/api/chat_prompt_queue",
        json=legacy_payload,
        headers=headers,
    )

    assert legacy_first.status_code == 201
    assert legacy_second.status_code == 201
    legacy_item = legacy_first.get_json()["item"]
    legacy_queue_id = legacy_item["queue_id"]
    assert legacy_second.get_json()["item"]["queue_id"] == legacy_queue_id
    assert "active_legacy_submission_key" not in legacy_item
    assert "legacy_submission_queue_seen" not in legacy_item
    assert "legacy_submission_generate_seen" not in legacy_item
    assert "legacy_submission_expires_at" not in legacy_item

    explicit_queue_ids = []
    for index in range(2):
        response = client.post(
            "/von/api/chat_prompt_queue",
            json={
                **legacy_payload,
                "client_request_id": f"request-{index}",
                "attempt_id": f"attempt-{index}",
            },
            headers=headers,
        )
        assert response.status_code == 201
        explicit_queue_ids.append(response.get_json()["item"]["queue_id"])

    assert explicit_queue_ids[0] != explicit_queue_ids[1]
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    assert coll.count_documents({}) == 3
    assert (
        coll.count_documents({"active_legacy_submission_key": {"$exists": True}}) == 1
    )


def test_legacy_active_create_isolated_by_trusted_window_session(client) -> None:
    from src.backend.services.window_session_context_service import (
        set_window_organisation,
    )

    for window_session_id in ("legacy-window-a", "legacy-window-b"):
        set_window_organisation(
            window_session_id,
            "#V#test_org",
            "member",
            "#V#test_user@test_org",
            user_id="#V#test_user",
        )

    payload = {
        "prompt_raw": "same prompt in two tabs",
        "session_id": "same-conversation",
        "status": "in_progress",
    }
    first = client.post(
        "/von/api/chat_prompt_queue",
        json=payload,
        headers={"X-Von-Window-Session": "legacy-window-a"},
    )
    second = client.post(
        "/von/api/chat_prompt_queue",
        json=payload,
        headers={"X-Von-Window-Session": "legacy-window-b"},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.get_json()["item"]["queue_id"] != second.get_json()["item"]["queue_id"]
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    assert coll.count_documents({}) == 2


def test_queue_session_update_recomputes_and_clears_canonical_conversation_key(
    client,
) -> None:
    created = client.post(
        "/von/api/chat_prompt_queue",
        json={"prompt_raw": "Move me", "session_id": "session-a"},
    ).get_json()["item"]

    moved = client.patch(
        f"/von/api/chat_prompt_queue/{created['queue_id']}",
        json={"session_id": "session-b"},
    )
    cleared = client.patch(
        f"/von/api/chat_prompt_queue/{created['queue_id']}",
        json={"session_id": None},
    )

    assert moved.status_code == 200
    assert moved.get_json()["item"]["session_id"] == "session-b"
    assert moved.get_json()["item"]["conversation_key"]
    assert moved.get_json()["item"]["conversation_key"] != created["conversation_key"]
    assert cleared.status_code == 200
    assert cleared.get_json()["item"]["session_id"] is None
    assert cleared.get_json()["item"]["conversation_key"] is None


def test_chat_prompt_queue_requires_authenticated_session() -> None:
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(von_bp, url_prefix="/von")

    with app.test_client() as unauthenticated_client:
        resp = unauthenticated_client.get("/von/api/chat_prompt_queue")

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Not authenticated"
    assert resp.get_json()["error_code"] == "not_authenticated"


def test_chat_prompt_queue_claim_reports_terminal_wrong_state(client) -> None:
    create_resp = client.post(
        "/von/api/chat_prompt_queue",
        json={"prompt_raw": "Run once"},
    )
    assert create_resp.status_code == 201
    queue_id = create_resp.get_json()["item"]["queue_id"]

    finish_resp = client.post(
        f"/von/api/chat_prompt_queue/{queue_id}/finish",
        json={"status": "completed"},
    )
    assert finish_resp.status_code == 200

    claim_resp = client.post(f"/von/api/chat_prompt_queue/{queue_id}/claim")
    assert claim_resp.status_code == 410
    assert claim_resp.get_json()["error_code"] == "queue_claim_route_retired"


def test_chat_prompt_queue_route_lists_recent_failed_items_separately(client) -> None:
    create_resp = client.post(
        "/von/api/chat_prompt_queue",
        json={
            "prompt_raw": "List the most recent 10 gmail messages together with any labels",
            "session_id": "session-1",
            "session_name": "Current",
        },
    )
    assert create_resp.status_code == 201
    queue_id = create_resp.get_json()["item"]["queue_id"]

    finish_resp = client.post(
        f"/von/api/chat_prompt_queue/{queue_id}/finish",
        json={
            "status": "failed",
            "error": "The final response did not return from Von.",
        },
    )
    assert finish_resp.status_code == 200

    list_resp = client.get("/von/api/chat_prompt_queue")
    assert list_resp.status_code == 200
    payload = list_resp.get_json()
    assert payload["items"] == []
    assert [item["queue_id"] for item in payload["recent_failed_items"]] == [queue_id]
    assert (
        payload["recent_failed_items"][0]["last_error"]
        == "The final response did not return from Von."
    )


def test_chat_prompt_queue_route_dismisses_failed_record_durably(client) -> None:
    create_resp = client.post(
        "/von/api/chat_prompt_queue",
        json={"prompt_raw": "Expired task"},
    )
    assert create_resp.status_code == 201
    queue_id = create_resp.get_json()["item"]["queue_id"]
    failure_message = (
        "Prompt queue record expired after being in progress for more than 24 hours."
    )
    finish_resp = client.post(
        f"/von/api/chat_prompt_queue/{queue_id}/finish",
        json={"status": "failed", "error": failure_message},
    )
    assert finish_resp.status_code == 200
    failed = finish_resp.get_json()["item"]

    dismiss_resp = client.delete(f"/von/api/chat_prompt_queue/{queue_id}")

    assert dismiss_resp.status_code == 200
    dismissed = dismiss_resp.get_json()["item"]
    assert dismissed["status"] == "cancelled"
    assert dismissed["last_error"] == failure_message
    assert dismissed["completed_at"] == failed["completed_at"]

    list_resp = client.get("/von/api/chat_prompt_queue")
    assert list_resp.status_code == 200
    payload = list_resp.get_json()
    assert payload["success"] is True
    assert payload["items"] == []
    assert payload["recent_failed_items"] == []
    assert payload["turn_admission"]["active_global"] == 0
    assert payload["turn_admission"]["pending_admission"] == 0


def test_queue_routes_cannot_release_a_live_server_bound_turn(client) -> None:
    created = client.post(
        "/von/api/chat_prompt_queue",
        json={"prompt_raw": "Keep the durable fence", "session_id": "session-1"},
    ).get_json()["item"]
    scope = chat_prompt_queue_service.build_queue_scope(
        user_concept_id="#V#test_user",
        organisation_concept_id="#V#test_org",
        namespace="#V#test_user@test_org",
    )
    chat_prompt_queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=created["queue_id"],
        client_request_id="request-live",
        attempt_id="attempt-live",
        conversation_key=created["conversation_key"],
        server_instance_id=SERVER_INSTANCE_ID,
    )

    requeue = client.post(f"/von/api/chat_prompt_queue/{created['queue_id']}/requeue")
    cancel = client.delete(f"/von/api/chat_prompt_queue/{created['queue_id']}")
    finish = client.post(
        f"/von/api/chat_prompt_queue/{created['queue_id']}/finish",
        json={"status": "completed"},
    )

    assert requeue.status_code == 409
    assert requeue.get_json()["error_code"] == "conversation_turn_active"
    assert cancel.status_code == 409
    assert cancel.get_json()["error_code"] == "conversation_turn_active"
    assert finish.status_code == 404

    chat_prompt_queue_service.finish_prompt_record(
        scope=scope,
        queue_id=created["queue_id"],
        status=chat_prompt_queue_service.STATUS_COMPLETED,
        attempt_id="attempt-live",
    )


def test_legacy_routes_cannot_complete_or_replay_server_dispatch_rows(client) -> None:
    created_response = client.post(
        "/von/api/chat_prompt_queue",
        json={
            "prompt_raw": "Server owns this turn",
            "session_id": "session-server-owned",
            "enqueue_submission_id": "submission-server-owned",
            "dispatch_mode": "server",
            "execution_envelope_version": 1,
            "execution_envelope": {"language": "en-NZ"},
        },
    )
    assert created_response.status_code == 201
    created = created_response.get_json()["item"]

    false_finish = client.post(
        f"/von/api/chat_prompt_queue/{created['queue_id']}/finish",
        json={"status": "completed"},
    )
    assert false_finish.status_code == 409
    assert (
        false_finish.get_json()["error_code"]
        == "server_dispatch_reconciliation_required"
    )

    reservation = chat_prompt_queue_service.reserve_next_server_dispatch(
        server_instance_id=SERVER_INSTANCE_ID,
    )
    assert reservation is not None
    scope = chat_prompt_queue_service.build_queue_scope(
        user_concept_id="#V#test_user",
        organisation_concept_id="#V#test_org",
        namespace="#V#test_user@test_org",
    )
    chat_prompt_queue_service.bind_queue_record_to_turn(
        scope=scope,
        queue_id=created["queue_id"],
        client_request_id=reservation["client_request_id"],
        attempt_id=reservation["attempt_id"],
        conversation_key=created["conversation_key"],
        server_instance_id=SERVER_INSTANCE_ID,
        dispatch_reservation_token=reservation["dispatch_reservation_token"],
    )

    ambiguous_replay = client.post(
        f"/von/api/chat_prompt_queue/{created['queue_id']}/requeue"
    )
    assert ambiguous_replay.status_code == 409
    assert (
        ambiguous_replay.get_json()["error_code"]
        == "server_dispatch_reconciliation_required"
    )


def test_server_handoff_route_links_replays_and_rejects_second_replacement(
    client,
) -> None:
    source_response = client.post(
        "/von/api/chat_prompt_queue",
        json={
            "prompt_raw": "Preserve this exact prompt",
            "session_id": "session-handoff",
            "session_name": "Handoff conversation",
        },
    )
    assert source_response.status_code == 201
    source = source_response.get_json()["item"]
    replacement_payload = {
        "prompt_raw": "Preserve this exact prompt",
        "session_id": "session-handoff",
        "session_name": "Handoff conversation",
        "enqueue_submission_id": "submission-handoff-route",
        "dispatch_mode": "server",
        "execution_envelope_version": 1,
        "execution_envelope": {"language": "en-NZ"},
        "handoff_source_queue_id": source["queue_id"],
    }

    created_response = client.post(
        "/von/api/chat_prompt_queue",
        json=replacement_payload,
    )
    replay_response = client.post(
        "/von/api/chat_prompt_queue",
        json=replacement_payload,
    )
    conflicting_response = client.post(
        "/von/api/chat_prompt_queue",
        json={
            **replacement_payload,
            "enqueue_submission_id": "submission-handoff-route-conflict",
        },
    )

    assert created_response.status_code == 201
    created = created_response.get_json()["item"]
    assert created["dispatch_ready"] is True
    assert created["handoff_source_queue_id"] == source["queue_id"]
    assert created["handoff_completed_at"] is not None
    assert replay_response.status_code == 200
    assert replay_response.get_json()["item"]["queue_id"] == created["queue_id"]
    assert replay_response.get_json()["idempotent_replay"] is True
    assert conflicting_response.status_code == 409
    assert conflicting_response.get_json()["error_code"] == "queue_handoff_conflict"

    scope = chat_prompt_queue_service.build_queue_scope(
        user_concept_id="#V#test_user",
        organisation_concept_id="#V#test_org",
        namespace="#V#test_user@test_org",
    )
    queue_rows = chat_prompt_queue_service.list_active_queue_records(scope=scope)
    assert [row["queue_id"] for row in queue_rows] == [created["queue_id"]]
