from __future__ import annotations

from typing import Any


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def limit(self, n: int):
        self._docs = self._docs[: int(n)]
        return self

    def __iter__(self):
        return iter(self._docs)


class _ChatHistoryCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def find(self, query: dict[str, Any], _projection: dict[str, Any] | None = None):
        namespace = query.get("namespace")
        if isinstance(namespace, str):
            docs = [doc for doc in self._docs if doc.get("namespace") == namespace]
        else:
            docs = list(self._docs)
        return _Cursor(docs)


class _DB:
    def __init__(self, chat_history_docs: list[dict[str, Any]]):
        self._chat = _ChatHistoryCollection(chat_history_docs)

    def __getitem__(self, key: str):
        if key == "chat_history":
            return self._chat
        raise KeyError(key)


def test_backfill_turn_execution_records_from_chat_history_dry_run(monkeypatch):
    from src.backend.services import turn_execution_record_service as service

    docs = [
        {
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "session_id": "chat-1",
            "organisation_concept_id": "#V#org",
            "history": [
                {
                    "role": "assistant",
                    "llm_debug_data": {
                        "turn_execution_record": {
                            "request_id": "req-1",
                            "schema_version": "turn_execution_record.v1",
                        }
                    },
                }
            ],
        }
    ]

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.get_db",
        lambda: _DB(docs),
    )

    upsert_calls: list[dict[str, Any]] = []

    def _capture_upsert(**kwargs: Any) -> dict[str, Any]:
        upsert_calls.append(dict(kwargs))
        return {"updated": True, "inserted": True}

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.upsert_turn_execution_record_projection",
        _capture_upsert,
    )

    result = service.backfill_turn_execution_records_from_chat_history(
        namespace="#V#user@org",
        limit_sessions=10,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["sessions_scanned"] == 1
    assert result["records_found"] == 1
    assert result["candidate_records"] == 1
    assert result["upserted_count"] == 0
    assert upsert_calls == []


def test_backfill_turn_execution_records_from_chat_history_upserts_records(monkeypatch):
    from src.backend.services import turn_execution_record_service as service

    docs = [
        {
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "session_id": "chat-2",
            "organisation_concept_id": "#V#org",
            "history": [
                {
                    "role": "assistant",
                    "llm_debug_data": {
                        "turn_execution_record": {
                            "request_id": "req-2",
                            "schema_version": "turn_execution_record.v1",
                        }
                    },
                }
            ],
        }
    ]

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.get_db",
        lambda: _DB(docs),
    )

    upsert_calls: list[dict[str, Any]] = []

    def _capture_upsert(**kwargs: Any) -> dict[str, Any]:
        upsert_calls.append(dict(kwargs))
        return {
            "updated": True,
            "inserted": True,
            "request_id": kwargs.get("record", {}).get("request_id"),
        }

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.upsert_turn_execution_record_projection",
        _capture_upsert,
    )

    result = service.backfill_turn_execution_records_from_chat_history(
        namespace="#V#user@org",
        limit_sessions=10,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["dry_run"] is False
    assert result["candidate_records"] == 1
    assert result["upserted_count"] == 1
    assert result["inserted_count"] == 1
    assert len(upsert_calls) == 1
    call = upsert_calls[0]
    assert call["namespace"] == "#V#user@org"
    assert call["session_id"] == "chat-2"


def test_backfill_turn_execution_records_reports_gap_when_history_has_no_records(
    monkeypatch,
):
    from src.backend.services import turn_execution_record_service as service

    docs = [
        {
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "session_id": "chat-3",
            "organisation_concept_id": "#V#org",
            "history": [
                {"role": "assistant", "llm_debug_data": {"request_id": "legacy"}},
                {"role": "assistant", "content": "Legacy response without debug payload"},
            ],
        }
    ]

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.get_db",
        lambda: _DB(docs),
    )

    result = service.backfill_turn_execution_records_from_chat_history(
        namespace="#V#user@org",
        limit_sessions=10,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["assistant_messages_scanned"] == 2
    assert result["records_found"] == 0
    gap_signals = result.get("gap_signals")
    assert isinstance(gap_signals, list)
    assert any(
        gap.get("gap_id") == "no_embedded_turn_execution_record_in_history"
        for gap in gap_signals
    )
