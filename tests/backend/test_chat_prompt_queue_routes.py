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
