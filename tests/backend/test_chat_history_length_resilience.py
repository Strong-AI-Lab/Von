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


def test_get_chat_history_session_message_count_is_exact_and_metadata_safe(
    monkeypatch,
):
    coll = _AggregateCollection({"message_count": 0})
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: coll,
    )

    total = chat_history_service.get_chat_history_session_message_count(
        "#V#u",
        "empty-session",
        namespace="#V#u@org",
    )

    assert total == 0
    assert len(coll.aggregate_calls) == 1
    pipeline = coll.aggregate_calls[0]["pipeline"]
    assert pipeline[0] == {
        "$match": {
            "user_id": "#V#u",
            "session_id": "empty-session",
            "namespace": "#V#u@org",
        }
    }
    assert pipeline[-1] == {"$limit": 1}
    assert "$size" in pipeline[1]["$project"]["message_count"]


def test_get_chat_history_session_message_count_returns_none_when_absent(
    monkeypatch,
):
    class _EmptyAggregateCollection:
        def aggregate(self, _pipeline, **_kwargs):
            return iter([])

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _EmptyAggregateCollection(),
    )

    assert (
        chat_history_service.get_chat_history_session_message_count(
            "#V#u",
            "missing-session",
            namespace="#V#u@org",
        )
        is None
    )


@pytest.mark.parametrize("raw_count", [True, -1, None, "0"])
def test_get_chat_history_session_message_count_rejects_invalid_counts(
    monkeypatch, raw_count
):
    coll = _AggregateCollection({"message_count": raw_count})
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: coll,
    )

    with pytest.raises(
        chat_history_service.ChatHistoryServiceError,
        match="invalid message count",
    ):
        chat_history_service.get_chat_history_session_message_count(
            "#V#u",
            "session-1",
            namespace="#V#u@org",
        )


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
