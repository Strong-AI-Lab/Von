from __future__ import annotations

from datetime import UTC, datetime


def test_opaque_cursor_rejects_expiry_and_tampering(monkeypatch):
    from src.backend.services import opaque_cursor_service as service

    monkeypatch.setattr(service.time, "time", lambda: 1_000)
    cursor = service.encode_opaque_cursor(
        purpose="test", payload={"position": 1}, ttl_seconds=60
    )
    assert service.decode_opaque_cursor(cursor=cursor, purpose="test") == {
        "position": 1
    }

    monkeypatch.setattr(service.time, "time", lambda: 1_061)
    try:
        service.decode_opaque_cursor(cursor=cursor, purpose="test")
    except service.OpaqueCursorError as exc:
        assert "expired" in str(exc)
    else:  # pragma: no cover - explicit failure is clearer than pytest.raises here
        raise AssertionError("expired cursor was accepted")

    monkeypatch.setattr(service.time, "time", lambda: 1_000)
    encoded, signature = cursor.split(".", 1)
    tampered = f"{'A' if encoded[0] != 'A' else 'B'}{encoded[1:]}.{signature}"
    try:
        service.decode_opaque_cursor(cursor=tampered, purpose="test")
    except service.OpaqueCursorError as exc:
        assert "signature" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("tampered cursor was accepted")


def test_owned_summary_source_page_uses_timestamp_and_session_tie_breaker(
    monkeypatch,
):
    from src.backend.services import chat_history_service as service

    captured = {}
    timestamp = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        service, "get_chat_history_collection_service", lambda **_kwargs: object()
    )
    monkeypatch.setattr(service, "_guard_chat_history_read", lambda _name: None)

    def _aggregate(_collection, pipeline, **_kwargs):
        captured["pipeline"] = pipeline
        return iter(
            [
                {
                    "session_id": "same-time-b",
                    "session_name": None,
                    "updated_at": timestamp,
                    "created_at": timestamp,
                    "_conversation_page_timestamp": timestamp,
                },
                {
                    "session_id": "same-time-a",
                    "session_name": None,
                    "updated_at": timestamp,
                    "created_at": timestamp,
                    "_conversation_page_timestamp": timestamp,
                },
            ]
        )

    monkeypatch.setattr(service, "_read_aggregate", _aggregate)
    monkeypatch.setattr(service, "_record_chat_history_read_success", lambda: None)

    page = service.get_chat_history_session_summaries_page(
        "#V#alice", page_size=1
    )

    assert page["has_more"] is True
    assert page["sessions"][0]["session_id"] == "same-time-b"
    assert page["next_position"]["session_id"] == "same-time-b"
    assert captured["pipeline"][-3]["$sort"] == {
        "_conversation_page_timestamp": -1,
        "session_id": -1,
    }
    assert captured["pipeline"][-2] == {"$limit": 2}


def test_shared_invite_source_page_uses_effective_timestamp_and_invite_tie_breaker(
    monkeypatch,
):
    from src.backend.services import shared_conversation_service as service

    captured = {}
    timestamp = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    class _Collection:
        def aggregate(self, pipeline):
            captured["pipeline"] = pipeline
            return iter(
                [
                    {
                        "invite_id": "invite-b",
                        "session_id": "shared-b",
                        "_conversation_page_timestamp": timestamp,
                        "_conversation_page_invite_id": "invite-b",
                    },
                    {
                        "invite_id": "invite-a",
                        "session_id": "shared-a",
                        "_conversation_page_timestamp": timestamp,
                        "_conversation_page_invite_id": "invite-a",
                    },
                ]
            )

    monkeypatch.setattr(service, "_get_collection", lambda: _Collection())

    page = service.list_accepted_invites_for_user_page(
        user_concept_id="#V#alice", page_size=1
    )

    assert page["has_more"] is True
    assert page["invites"] == [
        {"invite_id": "invite-b", "session_id": "shared-b"}
    ]
    assert page["next_position"] == {
        "timestamp": timestamp.isoformat(),
        "invite_id": "invite-b",
    }
    assert captured["pipeline"][1]["$set"]["_conversation_page_timestamp"] == {
        "$ifNull": [
            "$updated_at",
            {
                "$ifNull": [
                    "$created_at",
                    datetime(1970, 1, 1, tzinfo=UTC),
                ]
            },
        ]
    }
    assert captured["pipeline"][-3]["$sort"] == {
        "_conversation_page_timestamp": -1,
        "_conversation_page_invite_id": -1,
    }
    assert captured["pipeline"][-2] == {"$limit": 2}


def test_transcript_page_and_title_evidence_are_source_bounded(monkeypatch):
    from src.backend.services import chat_history_service as service

    monkeypatch.setattr(
        service, "get_chat_history_collection_service", lambda **_kwargs: object()
    )
    monkeypatch.setattr(service, "_guard_chat_history_read", lambda _name: None)
    monkeypatch.setattr(
        service,
        "_build_chat_history_session_read_queries",
        lambda **_kwargs: [{"session_id": "session-1"}],
    )
    monkeypatch.setattr(service, "_record_chat_history_read_success", lambda: None)

    def _aggregate(_collection, _pipeline, *, operation):
        if operation == "get_chat_history_transcript_page.aggregate":
            return iter(
                [
                    {
                        "session_id": "session-1",
                        "history_length": 3,
                        "history_page": [
                            {
                                "role": "user",
                                "content": "second",
                                "llm_debug_data": {"secret": True},
                            },
                            {"role": "assistant", "content": "third"},
                        ],
                    }
                ]
            )
        return iter(
            [
                {
                    "session_id": "session-1",
                    "session_name": None,
                    "updated_at": datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
                    "history_length": 3,
                    "user_message_count": 2,
                    "first_user_message": {
                        "role": "user",
                        "content": "a" * 250,
                        "turn_id": "turn-1",
                    },
                    "latest_user_message": {
                        "role": "user",
                        "content": "latest evidence",
                        "turn_id": "turn-3",
                    },
                }
            ]
        )

    monkeypatch.setattr(service, "_read_aggregate", _aggregate)

    transcript = service.get_chat_history_transcript_page(
        user_id="#V#alice",
        session_id="session-1",
        offset=1,
        page_size=2,
    )
    evidence = service.get_chat_history_title_evidence(
        user_id="#V#alice",
        session_id="session-1",
        excerpt_chars=200,
    )

    assert transcript["message_count"] == 3
    assert transcript["coverage_complete"] is True
    assert transcript["messages"][0]["source_locator"]["history_index"] == 1
    assert "llm_debug_data" not in transcript["messages"][0]
    assert evidence is not None
    assert evidence["first_user_message"]["content_truncated"] is True
    assert len(evidence["first_user_message"]["content"]) == 200
    assert evidence["first_user_message"]["source_locator"]["turn_id"] == "turn-1"
    assert evidence["latest_user_message"]["content"] == "latest evidence"
