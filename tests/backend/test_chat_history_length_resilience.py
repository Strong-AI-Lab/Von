from __future__ import annotations

import pytest
from pymongo.errors import PyMongoError

from src.backend.services import chat_history_service


class _AggregateCollection:
    def __init__(self, response: dict):
        self.response = response
        self.aggregate_calls = []

    def aggregate(self, pipeline, **kwargs):
        self.aggregate_calls.append({"pipeline": pipeline, "kwargs": kwargs})
        return iter([self.response])


def test_get_chat_history_length_uses_aggregate_pipeline(monkeypatch):
    coll = _AggregateCollection({"total_turns": 9})
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: coll,
    )

    total = chat_history_service.get_chat_history_length(
        "#V#u", namespace="#V#u@org", include_legacy=False
    )

    assert total == 9
    assert len(coll.aggregate_calls) == 1
    pipeline = coll.aggregate_calls[0]["pipeline"]
    assert pipeline[0]["$match"]["user_id"] == "#V#u"
    assert pipeline[0]["$match"]["namespace"] == "#V#u@org"
    assert "maxTimeMS" not in coll.aggregate_calls[0]["kwargs"]


def test_get_chat_history_session_count_uses_aggregate_pipeline(monkeypatch):
    coll = _AggregateCollection({"session_count": 4})
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: coll,
    )

    total = chat_history_service.get_chat_history_session_count(
        "#V#u", namespace="#V#u@org", include_legacy=False
    )

    assert total == 4
    assert len(coll.aggregate_calls) == 1
    assert "maxTimeMS" not in coll.aggregate_calls[0]["kwargs"]
    pipeline = coll.aggregate_calls[0]["pipeline"]
    assert pipeline[0]["$match"]["user_id"] == "#V#u"
    assert pipeline[0]["$match"]["namespace"] == "#V#u@org"


def test_chat_history_length_retries_despite_recent_transient_timeout(monkeypatch):
    class _FailingAggregateCollection:
        def __init__(self):
            self.calls = 0

        def aggregate(self, pipeline, **kwargs):
            self.calls += 1
            raise PyMongoError("timed out while reading from replica set")

    coll = _FailingAggregateCollection()
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: coll,
    )
    monkeypatch.setenv("VON_CHAT_HISTORY_READ_CIRCUIT_SECONDS", "5")
    monkeypatch.setattr(
        chat_history_service,
        "_CHAT_HISTORY_READ_CIRCUIT_UNTIL_MONOTONIC",
        0.0,
    )
    monkeypatch.setattr(chat_history_service, "_CHAT_HISTORY_READ_CIRCUIT_LAST_ERROR", None)

    with pytest.raises(chat_history_service.ChatHistoryServiceError, match="Could not retrieve chat history length"):
        chat_history_service.get_chat_history_length("#V#u")

    with pytest.raises(
        chat_history_service.ChatHistoryServiceError,
        match="Could not retrieve chat history length",
    ):
        chat_history_service.get_chat_history_length("#V#u")

    assert coll.calls == 2
