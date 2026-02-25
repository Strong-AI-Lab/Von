from __future__ import annotations

from datetime import datetime, timezone

from pymongo.errors import PyMongoError

from src.backend.services import chat_history_service


def _utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


class _BaseFakeCollection:
    def __init__(self, docs):
        self._docs = [dict(doc) for doc in docs]

    def _matches(self, doc, query):
        for key, value in query.items():
            if key == "$or":
                if not any(self._matches(doc, clause) for clause in value):
                    return False
                continue
            if doc.get(key) != value:
                return False
        return True

    def _project(self, doc, projection):
        if not isinstance(projection, dict):
            return dict(doc)
        out = {}
        for key, include in projection.items():
            if include and key in doc:
                out[key] = doc[key]
        return out

    def find(self, query, projection=None):
        rows = [doc for doc in self._docs if self._matches(doc, query)]
        return [self._project(doc, projection) for doc in rows]


class _NoHistoryReadCollection(_BaseFakeCollection):
    def find(self, query, projection=None):
        if isinstance(projection, dict) and (
            "history" in projection or "history_tail" in projection
        ):
            raise AssertionError("history field should not be read in metadata-only mode")
        return super().find(query, projection)


class _TimeoutOnHistoryCollection(_BaseFakeCollection):
    def find(self, query, projection=None):
        if isinstance(projection, dict) and "history" in projection:
            raise PyMongoError("simulated history read timeout")
        return super().find(query, projection)


def test_light_session_summaries_use_metadata_only_projection(monkeypatch):
    docs = [
        {
            "user_id": "#V#u",
            "session_id": "s-2",
            "session_name": "Session Two",
            "namespace": "#V#u@org",
            "created_at": _utc("2026-02-22T00:00:00Z"),
            "updated_at": _utc("2026-02-22T00:02:00Z"),
            "history": [{"role": "user", "content": "x"}],
        },
        {
            "user_id": "#V#u",
            "session_id": "s-1",
            "session_name": "Session One",
            "namespace": "#V#u@org",
            "created_at": _utc("2026-02-21T00:00:00Z"),
            "updated_at": _utc("2026-02-21T00:01:00Z"),
            "history": [{"role": "user", "content": "y"}],
        },
    ]
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _NoHistoryReadCollection(docs),
    )

    summaries = chat_history_service.get_chat_history_session_summaries(
        "#V#u",
        limit=50,
        namespace="#V#u@org",
        include_legacy=False,
        summary_mode="light",
    )

    assert [s["session_id"] for s in summaries] == ["s-2", "s-1"]
    assert all(s["message_count"] is None for s in summaries)
    assert all(s["preview"] is None for s in summaries)
    assert all(s["is_completed"] is False for s in summaries)


def test_full_session_summaries_fall_back_when_history_read_times_out(monkeypatch):
    docs = [
        {
            "user_id": "#V#u",
            "session_id": "s-1",
            "session_name": "Session One",
            "namespace": "#V#u@org",
            "created_at": _utc("2026-02-21T00:00:00Z"),
            "updated_at": _utc("2026-02-21T00:01:00Z"),
            "history": [{"role": "user", "content": "message"}],
        }
    ]
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _TimeoutOnHistoryCollection(docs),
    )

    summaries = chat_history_service.get_chat_history_session_summaries(
        "#V#u",
        limit=50,
        namespace="#V#u@org",
        include_legacy=False,
        summary_mode="full",
    )

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["session_id"] == "s-1"
    assert summary["message_count"] is None
    assert summary["preview"] is None
    assert summary["is_completed"] is False
