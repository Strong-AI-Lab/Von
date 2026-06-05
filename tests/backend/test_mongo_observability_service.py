from __future__ import annotations

from flask import Flask

from src.backend.services import chat_history_service
from src.backend.services.mongo_observability_service import (
    build_mongo_operation_comment,
    build_mongo_command_shape,
    build_mongo_query_targeting_report_from_rows,
    get_mongo_operation_audit_snapshot,
    get_mongo_query_targeting_report,
    profiler_document_to_query_shape_row,
    record_mongo_query_shape_observation,
    record_mongo_operation,
    reset_mongo_operation_audit_snapshot,
    reset_mongo_query_shape_telemetry,
)
from scripts import mongo_query_targeting_report as query_targeting_script


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


def test_mongo_command_shape_redacts_values_but_keeps_query_structure():
    shape = build_mongo_command_shape(
        "find",
        {
            "find": "workflow_instances",
            "filter": {
                "status": "pending",
                "lock_expires_at": {"$lte": "2026-06-05T00:00:00Z"},
                "secret_field": "do-not-print-me",
            },
            "sort": {"created_at": 1, "instance_id": 1},
            "projection": {"_id": 0, "instance_id": 1},
            "limit": 1,
            "comment": {
                "app": "von",
                "service": "durable_worker",
                "detail": "worker claim",
                "route": {"path": "/api/settings/db/info?token=secret"},
            },
        },
    )

    rendered = str(shape)
    assert shape["collection"] == "workflow_instances"
    assert "status" in shape["filter_shape"]
    assert "lock_expires_at{$lte}" in shape["filter_shape"]
    assert shape["sort_shape"] == "created_at,instance_id"
    assert shape["projection_shape"] == "_id,instance_id"
    assert "pending" not in rendered
    assert "2026-06-05" not in rendered
    assert "do-not-print-me" not in rendered
    assert "token=secret" not in rendered


def test_query_targeting_report_ranks_scanned_per_returned_over_duration_only():
    report = build_mongo_query_targeting_report_from_rows(
        [
            {
                "database": "von_db",
                "collection": "workflow_instances",
                "namespace": "von_db.workflow_instances",
                "command_name": "find",
                "filter_shape": "lock_expires_at{$lte},status",
                "sort_shape": "created_at,instance_id",
                "projection_shape": "_id,instance_id",
                "count": 3,
                "total_duration_ms": 1500,
                "max_duration_ms": 600,
                "total_returned": 3,
                "max_n_returned": 1,
                "total_docs_examined": 389367,
                "total_keys_examined": 389367,
                "max_docs_examined": 129789,
                "max_keys_examined": 129789,
                "sample_request_ids": ["atlas-query-shape"],
                "sources": ["system.profile"],
            },
            {
                "database": "von_db",
                "collection": "workflow_selection_experiences",
                "namespace": "von_db.workflow_selection_experiences",
                "command_name": "find",
                "filter_shape": "outcome",
                "count": 1,
                "total_duration_ms": 4442,
                "max_duration_ms": 4442,
                "total_returned": 120,
                "max_n_returned": 120,
                "total_docs_examined": 120,
                "total_keys_examined": 120,
                "max_docs_examined": 120,
                "max_keys_examined": 120,
                "sample_request_ids": ["slow-but-targeted"],
                "sources": ["system.profile"],
            },
        ],
        limit=5,
        source="unit_test",
    )

    top = report["rows"][0]
    assert top["namespace"] == "von_db.workflow_instances"
    assert top["max_docs_examined_per_returned"] == 129789.0
    assert "High query targeting" in top["recommended_next_step"]


def test_in_process_query_shape_report_keeps_request_ids_and_redacted_shapes(
    monkeypatch,
):
    monkeypatch.setenv("VON_MONGO_QUERY_SHAPE_TELEMETRY_ENABLED", "1")
    reset_mongo_query_shape_telemetry()

    record_mongo_query_shape_observation(
        command_name="find",
        database="von_db",
        collection="workflow_instances",
        filter_shape="lock_expires_at{$lte},status",
        sort_shape="created_at,instance_id",
        projection_shape="_id,instance_id",
        duration_ms=1250.0,
        request_id=12345,
        n_returned=1,
        source="command_listener_slow_success",
    )

    report = get_mongo_query_targeting_report(reset=True)
    row = report["rows"][0]
    assert row["collection"] == "workflow_instances"
    assert row["sample_request_ids"] == ["12345"]
    assert row["max_docs_examined_per_returned"] is None
    assert "Collect profiler" in row["recommended_next_step"]


def test_profiler_document_conversion_and_report_group_without_document_values():
    raw_docs = [
        {
            "ns": "von_db.workflow_instances",
            "op": "query",
            "command": {
                "find": "workflow_instances",
                "filter": {
                    "status": "pending",
                    "lock_expires_at": {"$lte": "secret-date"},
                },
                "sort": {"created_at": 1, "instance_id": 1},
                "projection": {"_id": 0, "instance_id": 1},
            },
            "millis": 80,
            "docsExamined": 129789,
            "keysExamined": 129789,
            "nreturned": 1,
            "planSummary": "COLLSCAN",
            "queryHash": "ABC123",
        },
        {
            "ns": "von_db.workflow_instances",
            "op": "query",
            "command": {
                "find": "workflow_instances",
                "filter": {
                    "status": "running",
                    "lock_expires_at": {"$lte": "other-secret-date"},
                },
                "sort": {"created_at": 1, "instance_id": 1},
                "projection": {"_id": 0, "instance_id": 1},
            },
            "millis": 100,
            "docsExamined": 100000,
            "keysExamined": 100000,
            "nreturned": 1,
            "planSummary": "COLLSCAN",
            "queryHash": "DEF456",
        },
    ]
    rows = [profiler_document_to_query_shape_row(doc) for doc in raw_docs]
    report = build_mongo_query_targeting_report_from_rows(rows, source="system.profile")

    top = report["rows"][0]
    rendered = str(report)
    assert report["observed_shape_count"] == 1
    assert top["count"] == 2
    assert top["total_docs_examined"] == 229789
    assert "status" in top["filter_shape"]
    assert "secret-date" not in rendered
    assert "running" not in rendered


def test_profiler_report_builder_uses_redacted_rows_without_printing_profile_values():
    class FakeCursor(list):
        def sort(self, *_args, **_kwargs):
            return self

        def limit(self, value):
            return FakeCursor(self[:value])

    class FakeCollection:
        def find(self, *_args, **_kwargs):
            return FakeCursor(
                [
                    {
                        "ns": "von_db.workflow_instances",
                        "op": "query",
                        "command": {
                            "find": "workflow_instances",
                            "filter": {"status": "pending"},
                        },
                        "millis": 50,
                        "docsExamined": 5000,
                        "keysExamined": 5000,
                        "nreturned": 1,
                    }
                ]
            )

    class FakeDb(dict):
        def __getitem__(self, key):
            assert key == "system.profile"
            return FakeCollection()

    report = query_targeting_script.build_profiler_query_targeting_report(
        FakeDb(),
        namespace=None,
        min_millis=0,
        sample_limit=10,
        report_limit=5,
        explain_samples=0,
        explain_max_time_ms=100,
    )

    assert report["profile_sample_count"] == 1
    assert report["rows"][0]["namespace"] == "von_db.workflow_instances"
    assert "pending" not in str(report)
