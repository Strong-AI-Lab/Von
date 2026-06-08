from __future__ import annotations

from flask import Flask
import pytest

from src.backend.db import mongo_client
from src.backend.server.routes.von_routes import von_bp


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
    assert claim_resp.status_code == 200
    assert claim_resp.get_json()["item"]["status"] == "in_progress"

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
    assert claim_resp.status_code == 404
    payload = claim_resp.get_json()
    assert payload["error_code"] == "wrong_state"
    assert payload["details"]["current_status"] == "completed"
