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
                doc.update(update.get("$set", {}))
                stored[key] = doc
                return types.SimpleNamespace(modified_count=1, matched_count=0)
            if key in stored:
                stored[key].update(update.get("$set", {}))
            return types.SimpleNamespace(modified_count=0, matched_count=1)

        def find_one(self, query, projection=None):
            return stored.get((query.get("user_id"), query.get("session_id")))

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeColl(),
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
    assert doc["history"] == []
    assert result["is_agent_created"] is False


def test_create_chat_session_stores_agent_created_provenance(monkeypatch):
    from src.backend.services import chat_history_service

    stored = {}

    class _FakeColl:
        def update_one(self, query, update, upsert=False):
            key = (query.get("user_id"), query.get("session_id"))
            doc = stored.setdefault(
                key,
                {"user_id": key[0], "session_id": key[1]},
            )
            if upsert:
                for field, value in update.get("$setOnInsert", {}).items():
                    doc.setdefault(field, value)
            doc.update(update.get("$set", {}))
            return types.SimpleNamespace(modified_count=1, matched_count=0)

        def find_one(self, query, projection=None):
            return stored.get((query.get("user_id"), query.get("session_id")))

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeColl(),
    )
    monkeypatch.setattr(chat_history_service, "get_session_context", lambda: {})

    result = chat_history_service.create_chat_session(
        user_id="#V#user",
        session_id="browser-fixture-abc",
        session_name="Browser fixture",
        origin_kind=chat_history_service.CHAT_SESSION_ORIGIN_KIND_BROWSER_TEST_FIXTURE,
        created_by_actor_concept_id=chat_history_service.VON_SYSTEM_ID,
        created_by_actor_type=chat_history_service.CODING_AGENT_TYPE_ID,
        is_agent_created=True,
        test_artifact_kind="browser_test_fixture_chat_session",
    )

    assert result["origin_kind"] == "browser_test_fixture"
    assert result["created_by_actor_concept_id"] == chat_history_service.VON_SYSTEM_ID
    assert result["created_by_actor_type"] == chat_history_service.CODING_AGENT_TYPE_ID
    assert result["is_agent_created"] is True
    assert result["test_artifact_kind"] == "browser_test_fixture_chat_session"


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
        lambda **kwargs: _FakeColl(),
    )

    result = chat_history_service.rename_chat_session(
        user_id="#V#user",
        session_id="s-1",
        session_name="New name",
    )

    assert result["updated"] is True
    assert stored[("#V#user", "s-1")]["session_name"] == "New name"


def test_agent_created_provenance_backfill_marks_only_reliable_candidates(
    monkeypatch,
):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "doc-browser",
            "user_id": "#V#user",
            "session_id": "browser-fixture-user-view-state",
            "session_name": "Browser fixture",
            "namespace": "#V#user@org",
        },
        {
            "_id": "doc-benchmark",
            "user_id": "#V#user",
            "session_id": "bench-1",
            "session_name": "Benchmark session abc12345",
            "namespace": "#V#user@org",
        },
        {
            "_id": "doc-normal",
            "user_id": "#V#user",
            "session_id": "normal-1",
            "session_name": "Human chat",
            "namespace": "#V#user@org",
        },
    ]

    class _FakeColl:
        def find(self, query, projection=None):
            namespace = query.get("namespace")
            return [
                dict(doc)
                for doc in docs
                if doc["user_id"] == query["user_id"]
                and (namespace is None or doc.get("namespace") == namespace)
            ]

        def update_one(self, query, update):
            for doc in docs:
                if doc.get("_id") == query.get("_id"):
                    doc.update(update.get("$set", {}))
                    return types.SimpleNamespace(modified_count=1, matched_count=1)
            return types.SimpleNamespace(modified_count=0, matched_count=0)

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeColl(),
    )

    dry_run = chat_history_service.backfill_agent_created_chat_session_provenance(
        user_concept_id="#V#user",
        namespace="#V#user@org",
        dry_run=True,
    )
    assert dry_run["reliable_candidate_count"] == 2
    assert dry_run["sessions_marked"] == 0
    assert "is_agent_created" not in docs[0]

    applied = chat_history_service.backfill_agent_created_chat_session_provenance(
        user_concept_id="#V#user",
        namespace="#V#user@org",
        dry_run=False,
    )

    assert applied["reliable_candidate_count"] == 2
    assert applied["sessions_marked"] == 2
    assert docs[0]["origin_kind"] == "browser_test_fixture"
    assert docs[0]["is_agent_created"] is True
    assert docs[1]["origin_kind"] == "benchmark_harness"
    assert docs[1]["is_agent_created"] is True
    assert "is_agent_created" not in docs[2]
