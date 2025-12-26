"""Unit tests for chat_history_service segment ordering/splitting.

These tests use a fake collection so they don't require Mongo.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def find(self, query, projection=None):
        # Minimal filtering on user_id for these tests.
        user_id = query.get("user_id")
        return [d for d in self._docs if d.get("user_id") == user_id]


def test_get_chat_history_segments_skips_empty_segments(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "history": [
                {"role": "system", "content": "__RESET__"},
                {
                    "role": "user",
                    "content": "hi",
                    "timestamp": datetime(2025, 1, 1, tzinfo=timezone.utc),
                },
                {"role": "system", "content": "__RESET__"},
                {"role": "system", "content": "__RESET__"},
                {
                    "role": "assistant",
                    "content": "ok",
                    "timestamp": datetime(2025, 1, 1, tzinfo=timezone.utc),
                },
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda: _FakeCollection(docs),
    )

    segments = chat_history_service.get_chat_history_segments("#V#u", "ignored")

    # Should skip the leading empty segment and the double-reset empties.
    assert len(segments) == 2
    assert [m["content"] for m in segments[0]] == ["hi"]
    assert [m["content"] for m in segments[1]] == ["ok"]


def test_get_chat_history_segments_orders_by_inferred_last_message_timestamp(
    monkeypatch,
):
    from src.backend.services import chat_history_service

    older = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
    newer = datetime(2025, 1, 2, 10, 0, tzinfo=timezone.utc)

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s_old",
            "updated_at": newer,  # misleading: updated_at is newer
            "history": [
                {"role": "user", "content": "old", "timestamp": older},
            ],
        },
        {
            "_id": "2",
            "user_id": "#V#u",
            "session_id": "s_new",
            "updated_at": older,  # misleading: updated_at is older
            "history": [
                {"role": "user", "content": "new", "timestamp": newer},
            ],
        },
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda: _FakeCollection(docs),
    )

    segments = chat_history_service.get_chat_history_segments("#V#u", "ignored")

    # Sorted by inferred last message timestamp (ascending): old first, new last.
    assert segments[0][0]["content"] == "old"
    assert segments[-1][0]["content"] == "new"
