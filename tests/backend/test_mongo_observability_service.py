from __future__ import annotations

from flask import Flask

from src.backend.services import chat_history_service
from src.backend.services.mongo_observability_service import (
    build_mongo_operation_comment,
    get_mongo_operation_audit_snapshot,
    record_mongo_operation,
    reset_mongo_operation_audit_snapshot,
)


def test_mongo_operation_comment_includes_safe_route_context(monkeypatch):
    monkeypatch.setenv("VON_MONGO_QUERY_ATTRIBUTION_ENABLED", "1")
    app = Flask(__name__)

    with app.test_request_context("/api/settings/db/info?token=secret", method="GET"):
        comment = build_mongo_operation_comment(
            service="settings_routes",
            collection="$cmd",
            operation="get_db_location_info.ping",
        )

    assert comment is not None
    assert comment["app"] == "von"
    assert comment["jira"] == "JVNAUTOSCI-2391"
    assert comment["service"] == "settings_routes"
    assert comment["collection"] == "$cmd"
    assert comment["operation"] == "get_db_location_info.ping"
    assert comment["route"]["method"] == "GET"
    assert comment["route"]["path"] == "/api/settings/db/info"
    assert "token" not in str(comment)
    assert "secret" not in str(comment)


def test_mongo_operation_audit_snapshot_aggregates_without_payload_content(monkeypatch):
    monkeypatch.setenv("VON_MONGO_OPERATION_AUDIT_ENABLED", "1")
    reset_mongo_operation_audit_snapshot()

    record_mongo_operation(
        service="chat_history_service",
        collection="chat_history",
        operation="get_chat_history_segments.find_session",
        elapsed_ms=12.5,
        success=True,
        detail="safe detail",
    )
    record_mongo_operation(
        service="chat_history_service",
        collection="chat_history",
        operation="get_chat_history_segments.find_session",
        elapsed_ms=900.0,
        success=False,
        error_type="ExecutionTimeout",
    )

    snapshot = get_mongo_operation_audit_snapshot(reset=True)
    rows = snapshot["operations"]

    assert snapshot["schema_version"] == "mongo_operation_audit_snapshot.v1"
    assert len(rows) == 1
    row = rows[0]
    assert row["service"] == "chat_history_service"
    assert row["collection"] == "chat_history"
    assert row["operation"] == "get_chat_history_segments.find_session"
    assert row["count"] == 2
    assert row["failure_count"] == 1
    assert row["slow_count"] == 1
    assert row["last_error_type"] == "ExecutionTimeout"
    assert "payload" not in str(row).lower()


def test_chat_history_read_adds_comment_and_records_operation(monkeypatch):
    monkeypatch.setenv("VON_MONGO_QUERY_ATTRIBUTION_ENABLED", "1")
    monkeypatch.setenv("VON_MONGO_OPERATION_AUDIT_ENABLED", "1")
    reset_mongo_operation_audit_snapshot()

    class FakeCollection:
        def __init__(self):
            self.kwargs = None

        def find_one(self, query, projection=None, **kwargs):
            self.kwargs = kwargs
            return {"_id": "doc"}

    coll = FakeCollection()
    doc = chat_history_service.find_chat_history_document_for_read(
        coll,
        {"session_id": "s1"},
        {"_id": 1},
    )

    assert doc == {"_id": "doc"}
    assert coll.kwargs["comment"]["service"] == "chat_history_service"
    assert coll.kwargs["comment"]["collection"] == "chat_history"
    assert coll.kwargs["comment"]["operation"] == "find_one"
    snapshot = get_mongo_operation_audit_snapshot(reset=True)
    assert snapshot["operations"][0]["count"] == 1


def test_chat_history_read_falls_back_when_comment_kwargs_are_unsupported(monkeypatch):
    monkeypatch.setenv("VON_MONGO_QUERY_ATTRIBUTION_ENABLED", "1")
    monkeypatch.setenv("VON_MONGO_OPERATION_AUDIT_ENABLED", "1")
    reset_mongo_operation_audit_snapshot()

    class LegacyFakeCollection:
        def __init__(self):
            self.calls = 0

        def find_one(self, query, projection=None):
            self.calls += 1
            return {"_id": "legacy"}

    coll = LegacyFakeCollection()
    doc = chat_history_service.find_chat_history_document_for_read(
        coll,
        {"session_id": "s1"},
        {"_id": 1},
    )

    assert doc == {"_id": "legacy"}
    assert coll.calls == 1
    snapshot = get_mongo_operation_audit_snapshot(reset=True)
    assert snapshot["operations"][0]["count"] == 1
    assert snapshot["operations"][0]["failure_count"] == 0
