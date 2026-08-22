from __future__ import annotations

import types

import pytest


def test_create_chat_session_preserves_explicit_name_and_leaves_unnamed_session_unset(
    monkeypatch,
):
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

    unnamed_result = chat_history_service.create_chat_session(
        user_id="#V#user",
        session_id="s-2",
        session_name="   ",
    )

    assert unnamed_result["session_name"] is None
    unnamed_doc = stored[("#V#user", "s-2")]
    assert "session_name" not in unnamed_doc
    assert unnamed_doc["history"] == []

    existing_result = chat_history_service.create_chat_session(
        user_id="#V#user",
        session_id="s-2",
        session_name="Not persisted on an existing session",
    )

    assert existing_result["session_name"] is None
    assert "session_name" not in unnamed_doc


def test_implicit_session_creation_does_not_derive_name_from_first_user_message(
    monkeypatch,
):
    from src.backend.services import chat_history_service

    writes = []

    class _FakeColl:
        def update_one(self, query, update, upsert=False):
            writes.append((query, update, upsert))
            return types.SimpleNamespace(modified_count=1, matched_count=0)

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeColl(),
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_session_context",
        lambda: {"namespace": "#V#user@org"},
    )

    chat_history_service.add_message_to_history(
        user_id="#V#user",
        session_id="s-implicit",
        message={"role": "user", "content": "Do not turn this into a title"},
        broadcast_to_shared=False,
        skip_rag_indexing=True,
    )

    assert len(writes) == 1
    _, update, upsert = writes[0]
    assert upsert is True
    assert "session_name" not in update["$setOnInsert"]


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
            "_id": "doc-live-sampler",
            "user_id": "#V#user",
            "session_id": "live-sampler-1",
            "session_name": "JVNAUTOSCI-1894 live prompt sample [arm_1:gemma4:26b]",
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
    assert dry_run["reliable_candidate_count"] == 3
    assert dry_run["sessions_marked"] == 0
    assert "is_agent_created" not in docs[0]

    applied = chat_history_service.backfill_agent_created_chat_session_provenance(
        user_concept_id="#V#user",
        namespace="#V#user@org",
        dry_run=False,
    )

    assert applied["reliable_candidate_count"] == 3
    assert applied["sessions_marked"] == 3
    assert docs[0]["origin_kind"] == "browser_test_fixture"
    assert docs[0]["is_agent_created"] is True
    assert docs[1]["origin_kind"] == "benchmark_harness"
    assert docs[1]["is_agent_created"] is True
    assert docs[2]["origin_kind"] == "coding_agent_test"
    assert docs[2]["is_agent_created"] is True
    assert docs[2]["test_artifact_kind"] == "live_kb_tool_prompt_sampler_chat_session"
    assert "is_agent_created" not in docs[3]


def test_delete_chat_history_targets_exact_namespace_and_reads_back(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "target",
            "user_id": "#V#user",
            "session_id": "same-session",
            "namespace": "#V#user@org-a",
        },
        {
            "_id": "other-namespace",
            "user_id": "#V#user",
            "session_id": "same-session",
            "namespace": "#V#user@org-b",
        },
        {
            "_id": "legacy",
            "user_id": "#V#user",
            "session_id": "same-session",
        },
    ]
    calls = {"delete": [], "find": []}

    def _matches(doc, query):
        return all(doc.get(key) == value for key, value in query.items())

    class _FakeColl:
        def delete_one(self, query):
            calls["delete"].append(dict(query))
            for index, doc in enumerate(docs):
                if _matches(doc, query):
                    docs.pop(index)
                    return types.SimpleNamespace(acknowledged=True, deleted_count=1)
            return types.SimpleNamespace(acknowledged=True, deleted_count=0)

        def find_one(self, query, projection=None, **_kwargs):
            calls["find"].append(dict(query))
            return next((dict(doc) for doc in docs if _matches(doc, query)), None)

    coll = _FakeColl()
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: coll,
    )

    receipt = chat_history_service.delete_chat_history(
        "#V#user",
        "same-session",
        namespace="#V#user@org-a",
    )

    assert receipt == {
        "acknowledged": True,
        "deleted_count": 1,
        "canonical_absent": True,
    }
    assert calls["delete"] == [
        {
            "user_id": "#V#user",
            "session_id": "same-session",
            "namespace": "#V#user@org-a",
        }
    ]
    assert calls["find"] == calls["delete"]
    assert {doc["_id"] for doc in docs} == {"other-namespace", "legacy"}


@pytest.mark.parametrize("namespace", ["", "   ", None])
def test_delete_chat_history_requires_exact_namespace(monkeypatch, namespace):
    from src.backend.services import chat_history_service

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: pytest.fail("collection should not be accessed"),
    )

    with pytest.raises(chat_history_service.ChatHistoryServiceError, match="namespace"):
        chat_history_service.delete_chat_history(
            "#V#user",
            "session-1",
            namespace=namespace,
        )


def test_delete_chat_history_receipt_exposes_zero_delete(monkeypatch):
    from src.backend.services import chat_history_service

    class _FakeColl:
        def delete_one(self, _query):
            return types.SimpleNamespace(acknowledged=True, deleted_count=0)

        def find_one(self, _query, _projection=None, **_kwargs):
            return None

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: _FakeColl(),
    )

    receipt = chat_history_service.delete_chat_history(
        "#V#user",
        "missing-session",
        namespace="#V#user@org",
    )

    assert receipt == {
        "acknowledged": True,
        "deleted_count": 0,
        "canonical_absent": True,
    }


def test_delete_chat_history_receipt_exposes_remaining_duplicate(monkeypatch):
    from src.backend.services import chat_history_service

    class _FakeColl:
        def delete_one(self, _query):
            return types.SimpleNamespace(acknowledged=True, deleted_count=1)

        def find_one(self, _query, _projection=None, **_kwargs):
            return {"_id": "duplicate-remains"}

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: _FakeColl(),
    )

    receipt = chat_history_service.delete_chat_history(
        "#V#user",
        "duplicate-session",
        namespace="#V#user@org",
    )

    assert receipt == {
        "acknowledged": True,
        "deleted_count": 1,
        "canonical_absent": False,
    }
