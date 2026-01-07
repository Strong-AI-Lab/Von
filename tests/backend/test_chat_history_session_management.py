from __future__ import annotations

import types


def test_create_chat_session_sets_name_and_namespace(monkeypatch):
    from src.backend.services import chat_history_service

    stored = {}

    class _FakeColl:
        def update_one(self, query, update, upsert=False):
            key = (query.get("user_id"), query.get("session_id"))
            if key not in stored and upsert:
                doc = {"user_id": key[0], "session_id": key[1]}
                doc.update(update.get("$setOnInsert", {}))
                stored[key] = doc
                return types.SimpleNamespace(modified_count=1, matched_count=0)
            return types.SimpleNamespace(modified_count=0, matched_count=1)

        def find_one(self, query, projection=None):
            return stored.get((query.get("user_id"), query.get("session_id")))

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda: _FakeColl(),
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_session_context",
        lambda: {
            "organisation_concept_id": "#V#org",
            "role_in_org": "member",
            "namespace": "#V#user@org",
            "org_id": "#V#org",
        },
    )

    result = chat_history_service.create_chat_session(
        user_id="#V#user",
        session_id="s-1",
        session_name="  My chat  ",
    )

    assert result["session_name"] == "My chat"
    doc = stored[("#V#user", "s-1")]
    assert doc["session_name"] == "My chat"
    assert doc["namespace"] == "#V#user@org"


def test_rename_chat_session_updates_name(monkeypatch):
    from src.backend.services import chat_history_service

    stored = {("#V#user", "s-1"): {"session_name": "Old name"}}

    class _FakeColl:
        def update_one(self, query, update):
            key = (query.get("user_id"), query.get("session_id"))
            doc = stored.get(key)
            if not doc:
                return types.SimpleNamespace(modified_count=0, matched_count=0)
            doc.update(update.get("$set", {}))
            return types.SimpleNamespace(modified_count=1, matched_count=1)

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda: _FakeColl(),
    )

    result = chat_history_service.rename_chat_session(
        user_id="#V#user",
        session_id="s-1",
        session_name="New name",
    )

    assert result["updated"] is True
    assert stored[("#V#user", "s-1")]["session_name"] == "New name"
