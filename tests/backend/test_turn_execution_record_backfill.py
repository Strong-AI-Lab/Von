from __future__ import annotations

from typing import Any


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def skip(self, n: int):
        self._docs = self._docs[int(n) :]
        return self

    def limit(self, n: int):
        self._docs = self._docs[: int(n)]
        return self

    def __iter__(self):
        return iter(self._docs)


class _ChatHistoryCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        namespace = query.get("namespace")
        if isinstance(namespace, str) and doc.get("namespace") != namespace:
            return False
        return True

    def find(self, query: dict[str, Any], _projection: dict[str, Any] | None = None):
        docs = [doc for doc in self._docs if self._matches(doc, query)]
        return _Cursor(docs)

    def distinct(self, field: str, query: dict[str, Any] | None = None):
        query = query or {}
        values: list[Any] = []
        seen: set[Any] = set()
        for doc in self.find(query):
            value = doc.get(field)
            if value in seen:
                continue
            seen.add(value)
            values.append(value)
        return values


class _TurnExecutionCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        namespace = query.get("namespace")
        if isinstance(namespace, str) and doc.get("namespace") != namespace:
            return False
        request_id = query.get("request_id")
        if isinstance(request_id, str) and doc.get("request_id") != request_id:
            return False
        if isinstance(request_id, dict):
            candidates = request_id.get("$in")
            if isinstance(candidates, list) and doc.get("request_id") not in candidates:
                return False
        return True

    def find(self, query: dict[str, Any], _projection: dict[str, Any] | None = None):
        docs = [doc for doc in self._docs if self._matches(doc, query)]
        return _Cursor(docs)

    def count_documents(self, query: dict[str, Any]) -> int:
        return len(list(self.find(query)))

    def distinct(self, field: str, query: dict[str, Any] | None = None):
        query = query or {}
        values: list[Any] = []
        seen: set[Any] = set()
        for doc in self.find(query):
            value = doc.get(field)
            if value in seen:
                continue
            seen.add(value)
            values.append(value)
        return values


class _DB:
    def __init__(
        self,
        chat_history_docs: list[dict[str, Any]],
        turn_execution_docs: list[dict[str, Any]] | None = None,
    ):
        self._chat = _ChatHistoryCollection(chat_history_docs)
        self._turn_execution = _TurnExecutionCollection(turn_execution_docs or [])

    def __getitem__(self, key: str):
        if key == "chat_history":
            return self._chat
        if key == "turn_execution_records":
            return self._turn_execution
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
        synthesise_missing_records=False,
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


def test_backfill_turn_execution_records_synthesises_missing_records(monkeypatch):
    from src.backend.services import turn_execution_record_service as service

    docs = [
        {
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "session_id": "chat-4",
            "organisation_concept_id": "#V#org",
            "history": [
                {"role": "user", "content": "Please update the relation"},
                {
                    "role": "assistant",
                    "content": "Done.",
                    "llm_debug_data": {
                        "request_id": "req-synth-1",
                        "tool_invocations": [
                            {"name": "search_concepts", "arguments": {"query": "x"}}
                        ],
                    },
                },
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
        synthesise_missing_records=True,
    )

    assert result["success"] is True
    assert result["assistant_messages_scanned"] == 1
    assert result["assistant_messages_with_llm_debug_data"] == 1
    assert result["assistant_messages_with_request_id"] == 1
    assert result["embedded_records_found"] == 0
    assert result["synthesised_records_found"] == 1
    assert result["records_found"] == 1
    assert result["candidate_records"] == 1
    gap_signals = result.get("gap_signals")
    assert isinstance(gap_signals, list)
    assert any(
        gap.get("gap_id")
        == "embedded_turn_execution_records_missing_recovered_by_synthesis"
        for gap in gap_signals
    )


def test_backfill_synthesis_infers_workflow_selection_for_projection(monkeypatch):
    from src.backend.services import turn_execution_record_service as service

    docs = [
        {
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "session_id": "chat-5",
            "organisation_concept_id": "#V#org",
            "history": [
                {"role": "user", "content": "Find matching concepts"},
                {
                    "role": "assistant",
                    "content": "I searched the concepts.",
                    "llm_debug_data": {
                        "request_id": "req-synth-2",
                        "tool_invocations": [
                            {
                                "name": "search_concepts",
                                "arguments": {"query": "concept"},
                                "status": "success",
                            }
                        ],
                    },
                },
            ],
        }
    ]

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.get_db",
        lambda: _DB(docs),
    )

    captured_record: dict[str, Any] = {}

    def _capture_upsert(**kwargs: Any) -> dict[str, Any]:
        record = kwargs.get("record")
        if isinstance(record, dict):
            captured_record.update(record)
        return {"updated": True, "inserted": False}

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.upsert_turn_execution_record_projection",
        _capture_upsert,
    )

    result = service.backfill_turn_execution_records_from_chat_history(
        namespace="#V#user@org",
        limit_sessions=10,
        dry_run=False,
        synthesise_missing_records=True,
    )

    assert result["success"] is True
    assert result["upserted_count"] == 1
    workflow_selection = captured_record.get("workflow_selection")
    assert isinstance(workflow_selection, dict)
    assert workflow_selection.get("selected_workflow_id") == "#V#tool_calling_workflow"
    assert workflow_selection.get("selector_verdict") == "tool_seeking"


def test_turn_execution_namespace_coverage_report_summarises_metrics(monkeypatch):
    from src.backend.services import turn_execution_record_service as service

    chat_docs = [
        {
            "namespace": "#V#alpha@org",
            "user_id": "#V#user",
            "session_id": "alpha-1",
            "history": [
                {
                    "role": "assistant",
                    "timestamp": "2026-02-19T00:57:25Z",
                    "llm_debug_data": {
                        "request_id": "req-alpha-1",
                        "turn_execution_record": {"request_id": "req-alpha-1"},
                    },
                },
                {
                    "role": "assistant",
                    "timestamp": "2026-02-19T00:58:25Z",
                    "llm_debug_data": {"request_id": "req-alpha-2"},
                },
            ],
        },
        {
            "namespace": "#V#beta@org",
            "user_id": "#V#user",
            "session_id": "beta-1",
            "history": [
                {"role": "assistant", "content": "No debug payload here"},
            ],
        },
    ]
    turn_docs = [
        {
            "namespace": "#V#alpha@org",
            "request_id": "req-alpha-1",
            "created_at_utc": "2026-02-19T00:57:30Z",
            "completion_gate": {"decision": "completed"},
            "workflow_selection": {"selected_workflow_id": "#V#flow"},
        }
    ]

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.get_db",
        lambda: _DB(chat_docs, turn_docs),
    )

    result = service.build_turn_execution_namespace_coverage_report(
        limit_namespaces=10,
        limit_sessions_per_namespace=10,
        limit_projected_records_per_namespace=10,
    )

    assert result["success"] is True
    assert result["namespaces_scanned"] == 2
    by_namespace = {
        item["namespace"]: item for item in result.get("coverage_by_namespace", [])
    }
    assert "#V#alpha@org" in by_namespace
    assert "#V#beta@org" in by_namespace

    alpha = by_namespace["#V#alpha@org"]
    assert alpha["assistant_messages_scanned"] == 2
    assert alpha["assistant_messages_with_turn_execution_record"] == 1
    assert alpha["projected_records_total"] == 1
    assert alpha["request_id_overlap_count"] == 1
    assert alpha["assistant_embedded_record_rate_pct"] == 50.0

    beta = by_namespace["#V#beta@org"]
    assert beta["assistant_messages_scanned"] == 1
    assert beta["assistant_messages_with_turn_execution_record"] == 0
    beta_gaps = beta.get("gap_signals")
    assert isinstance(beta_gaps, list)
    assert any(
        gap.get("gap_id") == "no_embedded_turn_execution_record_in_history"
        for gap in beta_gaps
    )
    assert any(
        gap.get("gap_id") == "no_turn_execution_records_projection"
        for gap in beta_gaps
    )

    aggregate = result.get("aggregate")
    assert isinstance(aggregate, dict)
    assert aggregate["assistant_messages_scanned"] == 3
    assert aggregate["assistant_messages_with_turn_execution_record"] == 1
    assert aggregate["projected_records_total"] == 1


def test_turn_execution_namespace_coverage_report_flags_no_namespaces(monkeypatch):
    from src.backend.services import turn_execution_record_service as service

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.get_db",
        lambda: _DB([], []),
    )

    result = service.build_turn_execution_namespace_coverage_report()

    assert result["success"] is True
    assert result["namespaces_scanned"] == 0
    gaps = result.get("capability_gaps")
    assert isinstance(gaps, list)
    assert any(gap.get("gap_id") == "no_namespaces_found" for gap in gaps)
