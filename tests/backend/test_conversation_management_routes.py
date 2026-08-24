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


def test_conversation_search_route_binds_actor_namespace_and_cursor(monkeypatch, app_client):
    captured = {}
    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)

    def _search(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "schema_version": "conversation_search_result.v1",
            "query": kwargs["query"],
            "match_mode": kwargs["match_mode"],
            "sort": kwargs["sort"],
            "filters": kwargs["filters"],
            "results": [],
            "count": 0,
            "page_size": kwargs["page_size"],
            "has_more": False,
            "next_cursor": None,
            "index_coverage": {},
            "retrieval": {},
        }

    monkeypatch.setattr(
        von_routes.conversation_search_service,
        "search_actor_conversations",
        _search,
    )
    response = app_client.get(
        "/von/api/session/conversation_search?q=detector&cursor=opaque&page_size=25",
        headers={"X-Von-Window-Session": "window-1"},
    )

    assert response.status_code == 200
    assert captured["actor_user_id"] == "#V#alice"
    assert captured["namespace"] == "#V#alice@org"
    assert captured["organisation_concept_id"] == "#V#org"
    assert captured["cursor"] == "opaque"
    assert captured["page_size"] == 25


def test_delete_compatibility_route_moves_exact_session_to_trash(
    monkeypatch, app_client
):
    lifecycle_call = {}
    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)

    def _set_trashed(**kwargs):
        lifecycle_call.update(kwargs)
        return {
            "matched": True,
            "changed": True,
            "trashed": True,
            "canonical_read_back": {
                "session_id": kwargs["session_id"],
                "trashed": True,
            },
        }

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "set_chat_session_trashed",
        _set_trashed,
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "empty-session"},
        headers={"X-Von-Window-Session": "window-1"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "trashed"
    assert payload["recoverable"] is True
    assert payload["trashed"] is True
    assert payload["canonical_read_back"]["session_id"] == "empty-session"
    assert lifecycle_call == {
        "user_id": "#V#alice",
        "session_id": "empty-session",
        "trashed": True,
            "actor_user_id": "#V#alice",
            "namespace": "#V#alice@org",
            "organisation_concept_id": "#V#org",
            "include_legacy": False,
    }


def test_delete_conversation_route_returns_not_found_without_claiming_trash(
    monkeypatch, app_client
):
    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "set_chat_session_trashed",
        lambda **_kwargs: {"matched": False, "changed": False, "trashed": True},
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "missing-session"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Session not found"


def test_delete_conversation_route_restores_substantial_conversation(
    monkeypatch, app_client
):
    captured = {}
    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)

    def _set_trashed(**kwargs):
        captured.update(kwargs)
        return {
            "matched": True,
            "changed": True,
            "trashed": False,
            "canonical_read_back": {
                "session_id": kwargs["session_id"],
                "trashed": False,
            },
        }

    monkeypatch.setattr(
        von_routes.chat_history_service, "set_chat_session_trashed", _set_trashed
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "substantial-session", "action": "restore"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "restored"
    assert response.get_json()["trashed"] is False
    assert captured["trashed"] is False


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
        "set_chat_session_trashed",
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
        "set_chat_session_trashed",
        pytest.fail,
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "empty-session"},
        headers={"X-User-Concept-ID": "#V#alice"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "Not authenticated"


def test_delete_conversation_route_and_service_trash_exact_session_recoverably(
    monkeypatch, app_client
):
    from src.backend.services import chat_prompt_queue_service

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
        def find_one(self, query, _projection=None, **_kwargs):
            return next((dict(doc) for doc in docs if _matches(doc, query)), None)

        def update_one(self, query, update):
            doc = next((item for item in docs if _matches(item, query)), None)
            if doc is None:
                return types.SimpleNamespace(
                    acknowledged=True, matched_count=0, modified_count=0
                )
            doc.update(update.get("$set", {}))
            for key in update.get("$unset", {}):
                doc.pop(key, None)
            return types.SimpleNamespace(
                acknowledged=True, matched_count=1, modified_count=1
            )

    collection = _DisposableCollection()
    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(
        chat_prompt_queue_service,
        "list_active_queue_records",
        lambda **_kwargs: [],
    )

    response = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": "disposable-empty-session"},
    )

    assert response.status_code == 200
    assert response.get_json()["trashed"] is True
    assert {doc["_id"] for doc in docs} == {"disposable", "other-scope"}
    assert (
        next(doc for doc in docs if doc["_id"] == "disposable")["trashed_at"]
        is not None
    )
    assert "trashed_at" not in next(doc for doc in docs if doc["_id"] == "other-scope")


def test_authenticated_search_cursor_trash_restore_replay(monkeypatch, app_client):
    from src.backend.services import chat_prompt_queue_service

    docs = [
        {
            "_id": f"doc-{index}",
            "user_id": "#V#alice",
            "session_id": f"search-session-{index}",
            "session_name": f"Search fixture {index}",
            "namespace": "#V#alice@org",
            "organisation_concept_id": "#V#org",
            "updated_at": f"2026-08-{20 + index:02d}T12:00:00+00:00",
            "created_at": f"2026-08-{20 + index:02d}T11:00:00+00:00",
            "conversation_search_index_version": 1,
        }
        for index in range(3)
    ]

    def _matches(doc, query):
        return all(doc.get(key) == value for key, value in query.items())

    class _Collection:
        def find_one(self, query, _projection=None):
            return next((dict(doc) for doc in docs if _matches(doc, query)), None)

        def update_one(self, query, update):
            doc = next((row for row in docs if _matches(row, query)), None)
            if doc is None:
                return types.SimpleNamespace(acknowledged=True, matched_count=0)
            doc.update(update.get("$set", {}))
            for key in update.get("$unset", {}):
                doc.pop(key, None)
            return types.SimpleNamespace(
                acknowledged=True, matched_count=1, modified_count=1
            )

    def _list_actor_conversations(**kwargs):
        include_trashed = kwargs.get("include_trashed") is True
        rows = []
        for doc in docs:
            trashed = doc.get("trashed_at") is not None
            if trashed and not include_trashed:
                continue
            rows.append(
                {
                    "session_id": doc["session_id"],
                    "session_name": doc["session_name"],
                    "last_message_at": doc["updated_at"],
                    "created_at": doc["created_at"],
                    "namespace": doc["namespace"],
                    "organisation_concept_id": doc["organisation_concept_id"],
                    "conversation_search_index_version": 1,
                    "trashed": trashed,
                }
            )
        return {
            "success": True,
            "conversations": rows,
            "has_more": False,
            "coverage_complete": True,
        }

    def _lexical_candidates(**kwargs):
        trashed_only = kwargs.get("trashed_only") is True
        rows = [
            {
                "session_id": doc["session_id"],
                "conversation_search_title": doc["session_name"],
                "score": 1.0,
            }
            for doc in docs
            if (doc.get("trashed_at") is not None) == trashed_only
        ]
        return rows, {
            "status": "results_available" if rows else "valid_empty",
            "result_count": len(rows),
        }

    monkeypatch.setattr(von_routes, "get_effective_context", _effective_context)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: _Collection(),
    )
    monkeypatch.setattr(
        chat_prompt_queue_service, "list_active_queue_records", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        von_routes.conversation_search_service,
        "list_actor_conversations",
        _list_actor_conversations,
    )
    monkeypatch.setattr(
        von_routes.conversation_search_service,
        "_lexical_candidates",
        _lexical_candidates,
    )
    monkeypatch.setattr(
        von_routes.conversation_search_service,
        "_semantic_candidates",
        lambda **_kwargs: (
            [],
            {"status": "valid_empty", "coverage_complete": True},
        ),
    )
    monkeypatch.setattr(
        von_routes.conversation_search_service,
        "search_conversation_preference_session_ids",
        lambda **_kwargs: [],
    )

    first = app_client.get(
        "/von/api/session/conversation_search?q=Search+fixture&page_size=1"
    ).get_json()
    second = app_client.get(
        "/von/api/session/conversation_search",
        query_string={
            "q": "Search fixture",
            "page_size": 1,
            "cursor": first["next_cursor"],
        },
    ).get_json()
    assert first["results"][0]["session_id"] != second["results"][0]["session_id"]
    target = second["results"][0]["session_id"]

    trashed = app_client.post(
        "/von/api/session/delete_chat_session", json={"session_id": target}
    )
    assert trashed.status_code == 200
    assert trashed.get_json()["canonical_read_back"]["trashed"] is True
    active = app_client.get(
        "/von/api/session/conversation_search?q=Search+fixture&page_size=10"
    ).get_json()
    assert target not in {row["session_id"] for row in active["results"]}
    trash = app_client.get(
        "/von/api/session/conversation_search",
        query_string={
            "q": "Search fixture",
            "page_size": 10,
            "trashed_only": "true",
        },
    ).get_json()
    assert {row["session_id"] for row in trash["results"]} == {target}

    restored = app_client.post(
        "/von/api/session/delete_chat_session",
        json={"session_id": target, "action": "restore"},
    )
    assert restored.status_code == 200
    assert restored.get_json()["canonical_read_back"]["trashed"] is False
    rediscovered = app_client.get(
        "/von/api/session/conversation_search?q=Search+fixture&page_size=10"
    ).get_json()
    assert target in {row["session_id"] for row in rediscovered["results"]}
