from __future__ import annotations

import pytest
from flask import Flask

from src.backend.server.routes import von_routes


@pytest.fixture
def app_client():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user_concept_id"] = "#V#alice"
        yield client


def _effective_context(*_args, **_kwargs):
    return {
        "user_id": "#V#alice",
        "organisation_id": "#V#org",
        "namespace": "#V#alice@org",
    }


def test_conversation_preference_route_persists_authorised_effect(
    monkeypatch, app_client
):
    captured: dict = {}
    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "has_chat_history_session",
        lambda *args, **kwargs: True,
    )

    def _set_preference(**kwargs):
        captured.update(kwargs)
        return {
            "changed": True,
            "preference": {
                "session_id": kwargs["session_id"],
                "preference_present": True,
                "hidden": False,
                "pinned": True,
            },
        }

    monkeypatch.setattr(
        von_routes.conversation_management_service,
        "set_conversation_preference",
        _set_preference,
    )

    response = app_client.post(
        "/von/api/session/conversation_preference",
        json={"session_id": "session-1", "action": "pin"},
        headers={"X-Von-Window-Session": "window-1"},
    )

    assert response.status_code == 200
    assert response.get_json()["conversation_preference"]["pinned"] is True
    assert captured == {
        "actor_user_id": "#V#alice",
        "session_id": "session-1",
        "pinned": True,
    }


def test_conversation_preference_route_rejects_unowned_unshared_session(
    monkeypatch, app_client
):
    from src.backend.services import shared_conversation_service

    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "has_chat_history_session",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        shared_conversation_service,
        "get_accepted_invite_for_user_session",
        lambda **kwargs: None,
    )

    response = app_client.post(
        "/von/api/session/conversation_preference",
        json={"session_id": "foreign-session", "action": "hide"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Conversation not found"


def test_shared_conversation_rename_is_an_actor_specific_display_name(
    monkeypatch, app_client
):
    from src.backend.services import shared_conversation_service

    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "rename_chat_session",
        lambda **kwargs: {"matched": False, "updated": False},
    )
    monkeypatch.setattr(
        shared_conversation_service,
        "get_accepted_invite_for_user_session",
        lambda **kwargs: {"session_id": kwargs["session_id"], "status": "accepted"},
    )
    monkeypatch.setattr(
        von_routes.conversation_management_service,
        "set_conversation_preference",
        lambda **kwargs: {
            "changed": True,
            "preference": {"session_name_override": kwargs["session_name_override"]},
        },
    )

    response = app_client.post(
        "/von/api/session/rename_chat_session",
        json={"session_id": "shared-1", "session_name": "My useful label"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["access_mode"] == "invitee"
    assert payload["session_name"] == "My useful label"
