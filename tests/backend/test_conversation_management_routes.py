from __future__ import annotations

import types

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


def test_delete_conversation_route_deletes_empty_exact_session(monkeypatch, app_client):
    count_call = {}
    delete_call = {}
    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)

    def _message_count(*args, **kwargs):
        count_call.update({"args": args, "kwargs": kwargs})
        return 0

    def _delete(*args, **kwargs):
        delete_call.update({"args": args, "kwargs": kwargs})
        return {
            "acknowledged": True,
            "deleted_count": 1,
            "canonical_absent": True,
        }

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_message_count",
        _message_count,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "delete_chat_history",
        _delete,
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "empty-session"},
        headers={"X-Von-Window-Session": "window-1"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "deleted",
        "session_id": "empty-session",
        "acknowledged": True,
        "deleted_count": 1,
        "canonical_absent": True,
    }
    assert count_call == {
        "args": (),
        "kwargs": {
            "user_id": "#V#alice",
            "session_id": "empty-session",
            "namespace": "#V#alice@org",
        },
    }
    assert delete_call == {
        "args": ("#V#alice", "empty-session"),
        "kwargs": {"namespace": "#V#alice@org"},
    }


def test_delete_conversation_route_returns_not_found_without_delete(
    monkeypatch, app_client
):
    delete = pytest.fail
    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_message_count",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "delete_chat_history",
        delete,
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "missing-session"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Session not found"


def test_delete_conversation_route_denies_four_turns_without_delete(
    monkeypatch, app_client
):
    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_message_count",
        lambda **_kwargs: 8,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "delete_chat_history",
        pytest.fail,
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "substantial-session"},
    )

    assert response.status_code == 403
    assert response.get_json()["turn_count"] == 4


@pytest.mark.parametrize(
    "receipt",
    [
        {
            "acknowledged": True,
            "deleted_count": 0,
            "canonical_absent": True,
        },
        {
            "acknowledged": True,
            "deleted_count": 1,
            "canonical_absent": False,
        },
        {
            "acknowledged": False,
            "deleted_count": 1,
            "canonical_absent": True,
        },
    ],
)
def test_delete_conversation_route_never_claims_unverified_success(
    monkeypatch, app_client, receipt
):
    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_message_count",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "delete_chat_history",
        lambda *_args, **_kwargs: receipt,
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "empty-session"},
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["status"] == "delete_unverified"
    assert payload["error_code"] == "conversation_delete_unverified"


def test_delete_conversation_route_fails_closed_without_namespace(
    monkeypatch, app_client
):
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": None},
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "resolve_chat_history_namespace",
        lambda _user_id: None,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_message_count",
        pytest.fail,
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "empty-session"},
    )

    assert response.status_code == 503
    assert response.get_json()["error_code"] == "conversation_namespace_unavailable"


def test_delete_conversation_route_rejects_legacy_identity_header(
    monkeypatch, app_client
):
    from src.backend.security import access_control

    with app_client.session_transaction() as flask_session:
        flask_session.clear()
    monkeypatch.setattr(
        access_control,
        "_validate_person_concept",
        lambda concept_id: concept_id,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_message_count",
        pytest.fail,
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "empty-session"},
        headers={"X-User-Concept-ID": "#V#alice"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "Not authenticated"


def test_delete_conversation_route_and_service_remove_disposable_exact_session(
    monkeypatch, app_client
):
    docs = [
        {
            "_id": "disposable",
            "user_id": "#V#alice",
            "session_id": "disposable-empty-session",
            "namespace": "#V#alice@org",
            "history": [],
        },
        {
            "_id": "other-scope",
            "user_id": "#V#alice",
            "session_id": "disposable-empty-session",
            "namespace": "#V#alice@other-org",
            "history": [],
        },
    ]

    def _matches(doc, query):
        return all(doc.get(key) == value for key, value in query.items())

    class _DisposableCollection:
        def aggregate(self, pipeline, **_kwargs):
            query = pipeline[0]["$match"]
            doc = next((item for item in docs if _matches(item, query)), None)
            if doc is None:
                return iter([])
            count = sum(
                1
                for message in doc.get("history", [])
                if not (
                    message.get("role") == "system"
                    and message.get("content") == "__RESET__"
                )
            )
            return iter([{"message_count": count}])

        def delete_one(self, query):
            for index, doc in enumerate(docs):
                if _matches(doc, query):
                    docs.pop(index)
                    return types.SimpleNamespace(acknowledged=True, deleted_count=1)
            return types.SimpleNamespace(acknowledged=True, deleted_count=0)

        def find_one(self, query, _projection=None, **_kwargs):
            return next((dict(doc) for doc in docs if _matches(doc, query)), None)

    collection = _DisposableCollection()
    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "disposable-empty-session"},
    )

    assert response.status_code == 200
    assert response.get_json()["canonical_absent"] is True
    assert {doc["_id"] for doc in docs} == {"other-scope"}
