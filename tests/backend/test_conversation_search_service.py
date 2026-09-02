from __future__ import annotations

import types
from datetime import UTC, datetime

import pytest


def _accessible_result(**_kwargs):
    return {
        "success": True,
        "conversations": [
            {
                "session_id": "owned-title",
                "session_name": "Neutrino planning",
                "last_message_at": "2026-08-23T12:00:00+00:00",
                "conversation_search_index_version": 1,
                "trashed": False,
            },
            {
                "session_id": "shared-override",
                "session_name": "Alice's collider notes",
                "last_message_at": "2026-08-22T12:00:00+00:00",
                "conversation_search_index_version": 1,
                "shared_with_me": True,
                "shared_owner_user_id": "#V#owner",
                "trashed": False,
            },
        ],
        "has_more": False,
        "coverage_complete": True,
    }


def test_star_search_filters_unnamed_conversations_structurally(monkeypatch):
    from src.backend.services import conversation_search_service as service

    monkeypatch.setattr(
        service,
        "list_actor_conversations",
        lambda **_kwargs: {
            "success": True,
            "conversations": [
                {
                    "session_id": "unnamed",
                    "session_name": None,
                    "last_message_at": "2026-09-02T12:00:00+00:00",
                    "conversation_search_index_version": 1,
                    "trashed": False,
                },
                {
                    "session_id": "named",
                    "session_name": "Named conversation",
                    "last_message_at": "2026-09-02T11:00:00+00:00",
                    "conversation_search_index_version": 1,
                    "trashed": False,
                },
            ],
            "has_more": False,
            "coverage_complete": True,
        },
    )
    monkeypatch.setattr(
        service,
        "search_conversation_preference_session_ids",
        lambda **_kwargs: [],
    )

    result = service.search_actor_conversations(
        actor_user_id="#V#alice",
        organisation_concept_id="#V#org",
        namespace="#V#alice@org",
        query="*",
        match_mode="lexical",
        filters={"name_present": False},
    )

    assert [row["session_id"] for row in result["results"]] == ["unnamed"]
    assert result["filters"] == {"name_present": False}
    assert result["coverage_complete"] is True


def test_star_search_pages_the_exhaustive_source_cursor(monkeypatch):
    from src.backend.services import conversation_search_service as service

    calls = []

    def _list(**kwargs):
        calls.append(kwargs)
        if kwargs.get("cursor") is None:
            return {
                "success": True,
                "conversations": [
                    {
                        "session_id": "unnamed-1",
                        "session_name": None,
                        "trashed": False,
                    }
                ],
                "has_more": True,
                "next_cursor": "source-page-2",
                "coverage_complete": False,
                "ordering": {"consistency": "stateless_keyset_under_unchanged_corpus"},
            }
        assert kwargs["cursor"] == "source-page-2"
        return {
            "success": True,
            "conversations": [
                {
                    "session_id": "unnamed-2",
                    "session_name": None,
                    "trashed": False,
                }
            ],
            "has_more": False,
            "next_cursor": None,
            "coverage_complete": True,
            "ordering": {"consistency": "stateless_keyset_under_unchanged_corpus"},
        }

    monkeypatch.setattr(service, "list_actor_conversations", _list)

    first = service.search_actor_conversations(
        actor_user_id="#V#alice",
        namespace="#V#alice@org",
        query="*",
        match_mode="lexical",
        filters={"name_present": False, "access_mode": "owner"},
        page_size=1,
    )
    second = service.search_actor_conversations(
        actor_user_id="#V#alice",
        namespace="#V#alice@org",
        query="*",
        match_mode="lexical",
        filters={"name_present": False, "access_mode": "owner"},
        page_size=1,
        cursor=first["next_cursor"],
    )

    assert [row["session_id"] for row in first["results"]] == ["unnamed-1"]
    assert [row["session_id"] for row in second["results"]] == ["unnamed-2"]
    assert first["candidate_session_ids"] == ["unnamed-1"]
    assert second["candidate_session_ids"] == ["unnamed-2"]
    assert first["continuation_cursor"] == "source-page-2"
    assert first["next_cursor"] == "source-page-2"
    assert first["coverage_complete"] is False
    assert second["coverage_complete"] is True
    assert calls[0]["name_present"] is False
    assert calls[1]["cursor"] == "source-page-2"
    assert isinstance(calls[0]["cursor_context"], str)
    assert calls[1]["cursor_context"] == calls[0]["cursor_context"]

    # The exhaustive search reuses the source cursor directly. The real source
    # validates its signed actor/filter binding; this mock asserts only that the
    # exact continuation reaches that boundary unchanged.


def test_search_combines_indexed_title_content_and_override_with_stable_cursor(
    monkeypatch,
):
    from src.backend.services import conversation_search_service as service

    monkeypatch.setattr(service, "list_actor_conversations", _accessible_result)
    monkeypatch.setattr(
        service,
        "_lexical_candidates",
        lambda **_kwargs: (
            [
                {
                    "session_id": "owned-title",
                    "conversation_search_title": "Neutrino planning",
                    "conversation_search_text": ["old exact detector phrase"],
                    "conversation_search_segments": [
                        {
                            "text": "old exact detector phrase",
                            "role": "user",
                            "source_locator": {
                                "message_id": "message-1",
                                "role": "user",
                            },
                        }
                    ],
                    "score": 7.0,
                },
                {"session_id": "foreign", "score": 100.0},
            ],
            {
                "status": "results_available",
                "result_count": 2,
                "coverage_complete": True,
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "_semantic_candidates",
        lambda **_kwargs: (
            [
                {
                    "score": 0.8,
                    "text": "detector calibration topic",
                    "metadata": {
                        "session_id": "shared-override",
                        "role": "assistant",
                    },
                },
                {
                    "score": 99,
                    "text": "debug secret",
                    "metadata": {"session_id": "owned-title", "role": "tool"},
                },
            ],
            {
                "status": "results_available",
                "result_count": 2,
                "coverage_complete": True,
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "search_conversation_preference_session_ids",
        lambda **_kwargs: ["shared-override"],
    )

    first = service.search_actor_conversations(
        actor_user_id="#V#alice",
        organisation_concept_id="#V#org",
        namespace="#V#alice@org",
        query="detector",
        page_size=1,
    )
    second = service.search_actor_conversations(
        actor_user_id="#V#alice",
        organisation_concept_id="#V#org",
        namespace="#V#alice@org",
        query="detector",
        page_size=1,
        cursor=first["next_cursor"],
    )

    assert first["has_more"] is True
    assert first["results"][0]["session_id"] != second["results"][0]["session_id"]
    assert {first["results"][0]["session_id"], second["results"][0]["session_id"]} == {
        "owned-title",
        "shared-override",
    }
    assert all(
        row["session_id"] != "foreign"
        for row in [*first["results"], *second["results"]]
    )
    assert "debug secret" not in str(first) + str(second)
    assert first["index_coverage"]["complete_for_accessible_window"] is True
    assert first["index_coverage"]["shared_conversation_count"] == 1
    assert first["index_coverage"]["limitations"] == []
    all_results = [*first["results"], *second["results"]]
    owned = next(row for row in all_results if row["session_id"] == "owned-title")
    assert owned["match"]["source_locator"]["message_id"] == "message-1"


def test_search_pages_more_than_250_conversations_without_duplicates(monkeypatch):
    from src.backend.services import conversation_search_service as service

    rows = [
        {
            "session_id": f"session-{index:03d}",
            "session_name": f"Indexed topic {index}",
            "last_message_at": "2026-08-23T12:00:00+00:00",
            "conversation_search_index_version": 1,
            "trashed": False,
            **(
                {
                    "shared_with_me": True,
                    "shared_owner_user_id": "#V#owner",
                    "namespace": "#V#owner@org",
                }
                if index % 10 == 0
                else {}
            ),
        }
        for index in range(300)
    ]
    monkeypatch.setattr(
        service,
        "list_actor_conversations",
        lambda **_kwargs: {
            "success": True,
            "conversations": rows,
            "has_more": False,
            "coverage_complete": True,
        },
    )
    monkeypatch.setattr(
        service,
        "_lexical_candidates",
        lambda **_kwargs: (
            [
                {
                    "session_id": row["session_id"],
                    "conversation_search_title": row["session_name"],
                    "score": 1.0,
                }
                for row in rows
            ],
            {"status": "results_available", "result_count": len(rows)},
        ),
    )
    monkeypatch.setattr(service, "_semantic_candidates", lambda **_kwargs: ([], {}))
    monkeypatch.setattr(
        service,
        "search_conversation_preference_session_ids",
        lambda **_kwargs: [],
    )

    session_ids: list[str] = []
    cursor = None
    while True:
        page = service.search_actor_conversations(
            actor_user_id="#V#alice",
            organisation_concept_id="#V#org",
            namespace="#V#alice@org",
            query="Indexed topic",
            match_mode="lexical",
            page_size=100,
            cursor=cursor,
        )
        session_ids.extend(row["session_id"] for row in page["results"])
        assert page["coverage_complete"] is True
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break

    assert len(session_ids) == 300
    assert len(set(session_ids)) == 300


def test_search_cursor_is_actor_query_and_signature_bound(monkeypatch):
    from src.backend.services import conversation_search_service as service

    monkeypatch.setattr(service, "list_actor_conversations", _accessible_result)
    monkeypatch.setattr(service, "_lexical_candidates", lambda **_kwargs: ([], {}))
    monkeypatch.setattr(service, "_semantic_candidates", lambda **_kwargs: ([], {}))
    monkeypatch.setattr(
        service,
        "search_conversation_preference_session_ids",
        lambda **_kwargs: ["owned-title", "shared-override"],
    )
    first = service.search_actor_conversations(
        actor_user_id="#V#alice",
        organisation_concept_id="#V#org",
        namespace="#V#alice@org",
        query="neutrino",
        page_size=1,
    )
    cursor = first["next_cursor"]
    assert cursor

    with pytest.raises(service.ConversationSearchError, match="signature"):
        service.search_actor_conversations(
            actor_user_id="#V#alice",
            organisation_concept_id="#V#org",
            namespace="#V#alice@org",
            query="neutrino",
            page_size=1,
            cursor=f"{cursor[:-1]}x",
        )
    with pytest.raises(service.ConversationSearchError, match="actor"):
        service.search_actor_conversations(
            actor_user_id="#V#bob",
            organisation_concept_id="#V#org",
            namespace="#V#alice@org",
            query="neutrino",
            page_size=1,
            cursor=cursor,
        )
    with pytest.raises(service.ConversationSearchError, match="filters"):
        service.search_actor_conversations(
            actor_user_id="#V#alice",
            organisation_concept_id="#V#other-org",
            namespace="#V#alice@org",
            query="neutrino",
            page_size=1,
            cursor=cursor,
        )


def test_owner_trash_and_restore_are_idempotent_and_canonically_read_back(
    monkeypatch,
):
    from src.backend.services import chat_history_service as service

    doc = {
        "_id": "doc-1",
        "user_id": "#V#alice",
        "session_id": "session-1",
        "namespace": "#V#alice@org",
        "session_name": "Research",
        "created_at": datetime(2026, 8, 20, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 23, tzinfo=UTC),
    }

    class _Collection:
        def find_one(self, query, _projection=None):
            if all(doc.get(key) == value for key, value in query.items()):
                return dict(doc)
            return None

        def update_one(self, query, update):
            assert all(doc.get(key) == value for key, value in query.items())
            doc.update(update.get("$set", {}))
            for key in update.get("$unset", {}):
                doc.pop(key, None)
            return types.SimpleNamespace(
                acknowledged=True, matched_count=1, modified_count=1
            )

    monkeypatch.setattr(
        service, "get_chat_history_collection_service", lambda **_kwargs: _Collection()
    )

    first = service.set_chat_session_trashed(
        user_id="#V#alice",
        actor_user_id="#V#alice",
        session_id="session-1",
        namespace="#V#alice@org",
        trashed=True,
        verify_active_work=False,
    )
    second = service.set_chat_session_trashed(
        user_id="#V#alice",
        actor_user_id="#V#alice",
        session_id="session-1",
        namespace="#V#alice@org",
        trashed=True,
        verify_active_work=False,
    )
    restored = service.set_chat_session_trashed(
        user_id="#V#alice",
        actor_user_id="#V#alice",
        session_id="session-1",
        namespace="#V#alice@org",
        trashed=False,
        verify_active_work=False,
    )

    assert first["changed"] is True and first["canonical_read_back"]["trashed"] is True
    assert first["effect_id"].startswith("conversation_lifecycle:")
    assert first["index_reconciliation"]["lexical_visibility"] == "trash_only"
    assert second["changed"] is False
    assert restored["changed"] is True
    assert restored["canonical_read_back"]["trashed"] is False
    assert restored["index_reconciliation"]["lexical_visibility"] == "active"


def test_invitee_cannot_trash_owner_conversation():
    from src.backend.services import chat_history_service as service

    with pytest.raises(service.ChatHistoryServiceError, match="owner"):
        service.set_chat_session_trashed(
            user_id="#V#owner",
            actor_user_id="#V#invitee",
            session_id="shared-1",
            namespace="#V#owner@org",
            trashed=True,
            verify_active_work=False,
        )


def test_trash_is_blocked_before_mutation_when_prompt_work_is_active(monkeypatch):
    from src.backend.services import chat_history_service as service
    from src.backend.services import chat_prompt_queue_service

    doc = {
        "_id": "doc-1",
        "user_id": "#V#alice",
        "session_id": "session-1",
        "namespace": "#V#alice@org",
    }

    class _Collection:
        def find_one(self, _query, _projection=None):
            return dict(doc)

        def update_one(self, _query, _update):
            raise AssertionError("Trash mutation must not run while work is active")

    monkeypatch.setattr(
        service, "get_chat_history_collection_service", lambda **_kwargs: _Collection()
    )
    monkeypatch.setattr(
        chat_prompt_queue_service,
        "list_active_queue_records",
        lambda **_kwargs: [
            {"queue_id": "queue-1", "session_id": "session-1", "status": "queued"}
        ],
    )

    with pytest.raises(service.ConversationActiveWorkError, match="active_work"):
        service.set_chat_session_trashed(
            user_id="#V#alice",
            actor_user_id="#V#alice",
            session_id="session-1",
            namespace="#V#alice@org",
            organisation_concept_id="#V#org",
            trashed=True,
        )


def test_search_projection_excludes_system_tool_and_debug_content():
    from src.backend.services import chat_history_service as service

    assert (
        service._conversation_search_message_text(
            {"role": "user", "content": "visible question"}
        )
        == "visible question"
    )
    assert (
        service._conversation_search_message_text(
            {
                "role": "assistant",
                "content": "visible answer",
                "llm_debug_data": {"secret": 1},
            }
        )
        == "visible answer"
    )
    assert (
        service._conversation_search_message_text(
            {"role": "system", "content": "hidden system prompt"}
        )
        is None
    )
    assert (
        service._conversation_search_message_text(
            {"role": "tool", "content": "hidden tool result"}
        )
        is None
    )


def test_backfill_indexes_old_visible_phrase_and_title_only(monkeypatch):
    from src.backend.services import chat_history_service as service

    updates = []

    class _Cursor(list):
        def limit(self, _limit):
            return self

        def batch_size(self, _size):
            return self

    class _Collection:
        def find(self, _query, _projection):
            return _Cursor(
                [
                    {
                        "_id": "old-1",
                        "session_name": "Old detector work",
                        "history": [
                            {"role": "system", "content": "private system text"},
                            {"role": "user", "content": "old exact detector phrase"},
                            {"role": "tool", "content": "private tool output"},
                            {"role": "assistant", "content": "visible conclusion"},
                        ],
                    }
                ]
            )

        def update_one(self, query, update):
            updates.append((query, update))
            return types.SimpleNamespace(acknowledged=True)

    monkeypatch.setattr(
        service, "get_chat_history_collection_service", lambda **_kwargs: _Collection()
    )
    result = service.backfill_conversation_search_index(
        user_concept_id="#V#alice",
        namespace="#V#alice@org",
        dry_run=False,
    )

    assert result["sessions_updated"] == 1
    projection = updates[0][1]["$set"]
    assert projection["conversation_search_title"] == "Old detector work"
    assert projection["conversation_search_text"] == [
        "old exact detector phrase",
        "visible conclusion",
    ]
    assert [
        row["source_locator"]["history_index"]
        for row in projection["conversation_search_segments"]
    ] == [1, 3]
    assert "private system text" not in str(projection)
    assert "private tool output" not in str(projection)
