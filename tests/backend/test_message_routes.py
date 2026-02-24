"""Route tests for inter-user messaging safety and attribution."""

from __future__ import annotations

from flask import Flask
import pytest

import src.backend.server.routes.message_routes as message_routes


@pytest.fixture
def app_client():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(message_routes.message_bp, url_prefix="/api/messages")

    with app.test_client() as client:
        yield app, client


def test_send_message_requires_authentication(monkeypatch, app_client):
    _, client = app_client
    monkeypatch.setattr(message_routes, "_get_current_user_concept_id", lambda: None)

    response = client.post(
        "/api/messages/",
        json={"recipient_ids": ["#V#user_bob"], "content": "Kia ora"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_send_message_rejects_recipient_outside_organisation(monkeypatch, app_client):
    _, client = app_client
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#user_alice",
    )
    monkeypatch.setattr(
        message_routes,
        "_get_current_org_concept_id",
        lambda _user_id: "#V#org_test",
    )

    import src.backend.services.organisation_membership_service as membership_service

    monkeypatch.setattr(
        membership_service,
        "get_organisation_members",
        lambda _org_id: {
            "members": [
                {"user_concept_id": "#V#user_alice"},
                {"user_concept_id": "#V#user_charlie"},
            ]
        },
    )
    monkeypatch.setattr(
        message_routes,
        "create_message",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("create_message should not be called")
        ),
    )

    response = client.post(
        "/api/messages/",
        json={"recipient_ids": ["#V#user_bob"], "content": "Review this please"},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error"] == "Sender/recipient must share the current organisation"
    assert "#V#user_bob" in payload["invalid_concept_ids"]


def test_send_message_success_logs_episode_and_returns_attribution(
    monkeypatch, app_client
):
    _, client = app_client
    monkeypatch.setattr(
        message_routes,
        "_get_current_user_concept_id",
        lambda: "#V#user_alice",
    )
    monkeypatch.setattr(
        message_routes,
        "_get_current_org_concept_id",
        lambda _user_id: "#V#org_test",
    )

    import src.backend.services.organisation_membership_service as membership_service

    monkeypatch.setattr(
        membership_service,
        "get_organisation_members",
        lambda _org_id: {
            "members": [
                {"user_concept_id": "#V#user_alice"},
                {"user_concept_id": "#V#user_bob"},
            ]
        },
    )

    captured_create_args: dict[str, object] = {}

    def _fake_create_message(**kwargs):
        captured_create_args.update(kwargs)
        return {
            "concept_id": "#V#message_test_1",
            "concept_data": {"sent_at": "2026-02-24T10:00:00+00:00"},
        }

    monkeypatch.setattr(message_routes, "create_message", _fake_create_message)

    import src.backend.services.episode_logging_service as episode_logging_service

    episode_calls: list[dict] = []

    def _fake_log_episode(**kwargs):
        episode_calls.append(dict(kwargs))
        return "episode-1"

    monkeypatch.setattr(episode_logging_service, "log_episode", _fake_log_episode)

    response = client.post(
        "/api/messages/",
        json={
            "recipient_ids": ["#V#user_bob"],
            "content": "Please review the workflow output.",
            "metadata": {"intent": "review_request"},
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["message_id"] == "#V#message_test_1"
    assert payload["attribution"] == "Sent by Von on behalf of #V#user_alice"

    assert captured_create_args["sender_id"] == "#V#user_alice"
    assert captured_create_args["recipient_ids"] == ["#V#user_bob"]
    assert captured_create_args["org_id"] == "#V#org_test"
    metadata = captured_create_args["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["intent"] == "review_request"
    assert metadata["delivery_channel"] == "interuser_message"
    assert metadata["attribution"] == "Sent by Von on behalf of #V#user_alice"

    assert len(episode_calls) == 1
    assert episode_calls[0]["episode_type"] == "interuser_message_sent"
    assert episode_calls[0]["actor_user_id"] == "#V#user_alice"
    assert episode_calls[0]["organisation_concept_id"] == "#V#org_test"
    assert episode_calls[0]["payload"]["message_id"] == "#V#message_test_1"

