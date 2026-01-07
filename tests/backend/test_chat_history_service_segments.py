"""Unit tests for chat_history_service segment ordering/splitting.

These tests use a fake collection so they don't require Mongo.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def find_one(self, query, projection=None):
        user_id = query.get("user_id")
        session_id = query.get("session_id")
        for doc in self._docs:
            if doc.get("user_id") != user_id:
                continue
            if session_id and doc.get("session_id") != session_id:
                continue
            if isinstance(projection, dict):
                history_proj = projection.get("history")
                if isinstance(history_proj, dict) and "$slice" in history_proj:
                    slice_spec = history_proj["$slice"]
                    history = list(doc.get("history") or [])
                    if isinstance(slice_spec, list) and len(slice_spec) == 2:
                        start, count = slice_spec
                        history = history[start : start + count]
                    elif isinstance(slice_spec, int):
                        history = history[slice_spec:]
                    trimmed = dict(doc)
                    trimmed["history"] = history
                    return trimmed
            return doc
        return None


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

    segments = chat_history_service.get_chat_history_segments("#V#u", "s1")

    # Should skip the leading empty segment and the double-reset empties.
    assert len(segments) == 2
    assert [m["content"] for m in segments[0]] == ["hi"]
    assert [m["content"] for m in segments[1]] == ["ok"]


def test_get_chat_history_segments_scopes_to_requested_session(monkeypatch):
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

    segments = chat_history_service.get_chat_history_segments("#V#u", "s_new")

    assert len(segments) == 1
    assert segments[0][0]["content"] == "new"


def test_get_chat_history_segments_chunks_large_segments(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [
                {"role": "user", "content": "m1"},
                {"role": "assistant", "content": "m2"},
                {"role": "user", "content": "m3"},
                {"role": "assistant", "content": "m4"},
                {"role": "user", "content": "m5"},
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda: _FakeCollection(docs),
    )

    segments = chat_history_service.get_chat_history_segments(
        "#V#u", "s1", segment_size=2
    )

    assert [len(segment) for segment in segments] == [2, 2, 1]
    assert [m["content"] for m in segments[0]] == ["m1", "m2"]
    assert [m["content"] for m in segments[1]] == ["m3", "m4"]
    assert [m["content"] for m in segments[2]] == ["m5"]


def test_get_chat_history_segments_strips_debug_when_requested(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [
                {
                    "role": "assistant",
                    "content": "ok",
                    "llm_debug_data": {"model": "demo"},
                }
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda: _FakeCollection(docs),
    )

    segments = chat_history_service.get_chat_history_segments(
        "#V#u",
        "s1",
        include_locations=True,
        include_debug=False,
    )

    assert len(segments) == 1
    assert len(segments[0]) == 1
    assert "llm_debug_data" not in segments[0][0]


def test_get_chat_history_segments_reports_truncation(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [
                {"role": "user", "content": "m1"},
                {"role": "assistant", "content": "m2"},
                {"role": "user", "content": "m3"},
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda: _FakeCollection(docs),
    )

    segments, meta = chat_history_service.get_chat_history_segments(
        "#V#u",
        "s1",
        history_tail_limit=2,
        return_meta=True,
    )

    assert isinstance(segments, list)
    assert meta["history_truncated"] is True


def test_get_chat_history_debug_entry_returns_payload(monkeypatch):
    from src.backend.services import chat_history_service

    docs = [
        {
            "_id": "1",
            "user_id": "#V#u",
            "session_id": "s1",
            "history": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "ok", "llm_debug_data": {"model": "demo"}},
            ],
        }
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda: _FakeCollection(docs),
    )

    debug_data = chat_history_service.get_chat_history_debug_entry(
        user_id="#V#u",
        session_id="s1",
        history_index=1,
    )

    assert debug_data == {"model": "demo"}
