from __future__ import annotations

from flask import Flask
from pymongo.errors import OperationFailure

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.db.mongo_client import _safe_mongo_failure_summary
from src.backend.services import chat_history_service
from src.backend.services.mongo_observability_service import (
    build_mongo_operation_comment,
    build_mongo_command_shape,
    build_mongo_cost_guardrail_report,
    get_blob_hydration_snapshot,
    build_mongo_query_targeting_report_from_rows,
    get_mongo_operation_audit_snapshot,
    get_mongo_large_write_snapshot,
    get_mongo_query_targeting_report,
    profiler_document_to_query_shape_row,
    record_blob_hydration_observation,
    record_mongo_large_write_attempt,
    record_mongo_query_shape_observation,
    record_mongo_operation,
    reset_blob_hydration_snapshot,
    reset_mongo_large_write_snapshot,
    reset_mongo_operation_audit_snapshot,
    reset_mongo_query_shape_telemetry,
)
from src.backend.services import mongo_query_diagnostics_service as diagnostics_service


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


def test_mongo_command_failure_summary_omits_raw_command_body():
    failure = {
        "ok": 0.0,
        "errmsg": 'not authorized on von_db to execute command { find: "system.profile", token: "secret" }',
        "code": 13,
        "codeName": "Unauthorized",
        "$clusterTime": {"signature": {"hash": b"secret-hash"}},
    }

    summary = _safe_mongo_failure_summary(failure)

    rendered = str(summary)
    assert summary == {
        "type": "dict",
        "code": 13,
        "code_name": "Unauthorized",
        "message_class": "not_authorized",
    }
    assert "system.profile" not in rendered
    assert "secret" not in rendered


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

    report = diagnostics_service.build_profiler_query_targeting_report(
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


def test_von_mongo_query_diagnostics_requires_explicit_operator_gate():
    report = diagnostics_service.build_von_mongo_query_diagnostics_report(
        source="in_process",
    )

    assert report["success"] is False
    assert report["status"] == "blocked"
    assert report["error_code"] == "operator_diagnostics_not_allowed"
    assert report["direct_index_mutation"] is False


def test_von_mongo_query_diagnostics_bounds_parameters_and_uses_in_process_rows(
    monkeypatch,
):
    monkeypatch.setenv("VON_MONGO_QUERY_SHAPE_TELEMETRY_ENABLED", "1")
    reset_mongo_query_shape_telemetry()
    record_mongo_query_shape_observation(
        command_name="find",
        database="von_db",
        collection="workflow_instances",
        filter_shape="status,lock_expires_at{$lte}",
        duration_ms=200.0,
        request_id="safe-request-id",
        n_returned=1,
    )

    report = diagnostics_service.build_von_mongo_query_diagnostics_report(
        allow_operator_diagnostics=True,
        source="in-process",
        sample_limit=99_999,
        report_limit=99_999,
        explain_samples=99_999,
        explain_max_time_ms=99_999,
    )

    assert report["success"] is True
    assert report["status"] == "ok"
    assert report["options"]["source"] == "in_process"
    assert report["parameter_bounds"]["sample_limit"]["effective"] == 500
    assert report["parameter_bounds"]["report_limit"]["effective"] == 50
    assert report["parameter_bounds"]["explain_samples"]["effective"] == 5
    assert report["parameter_bounds"]["explain_max_time_ms"]["effective"] == 5000
    assert (
        report["reports"]["in_process"]["rows"][0]["collection"] == "workflow_instances"
    )
    assert "safe-request-id" in str(report)
    assert ".env" in str(report["privacy"]["omits"])


def test_von_mongo_query_diagnostics_reports_profiler_unavailable(monkeypatch):
    class FakeCollection:
        def find(self, *_args, **_kwargs):
            raise OperationFailure("not authorised")

    class FakeDb(dict):
        def __getitem__(self, key):
            assert key == "system.profile"
            return FakeCollection()

    monkeypatch.setattr(diagnostics_service, "get_db", lambda: FakeDb())

    report = diagnostics_service.build_von_mongo_query_diagnostics_report(
        allow_operator_diagnostics=True,
        source="profiler",
        sample_limit=10,
        report_limit=5,
    )

    assert report["success"] is True
    assert report["reports"]["profiler"]["status"] == "profiler_unavailable"
    assert report["reports"]["profiler"]["error_type"] == "OperationFailure"
    assert report["reports"]["profiler"]["error"] == "Mongo diagnostic read failed"
    assert "system.profile" not in str(report["reports"]["profiler"])
    assert report["summary"]["rows"] == []


def test_internal_mcp_gateway_exposes_mongo_query_diagnostics_report(monkeypatch):
    monkeypatch.setenv("VON_MONGO_QUERY_SHAPE_TELEMETRY_ENABLED", "1")
    reset_mongo_query_shape_telemetry()
    record_mongo_query_shape_observation(
        command_name="find",
        database="von_db",
        collection="concepts",
        filter_shape="relationships.is_an_instance_of",
        duration_ms=10.0,
        n_returned=2,
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=True,
    )

    result = gateway.invoke(
        "mongo_query_diagnostics_report",
        {
            "allow_operator_diagnostics": True,
            "source": "in_process",
            "report_limit": 3,
        },
    )

    payload = result.payload
    assert payload["success"] is True
    assert payload["summary"]["rows"][0]["collection"] == "concepts"
    assert payload["direct_index_mutation"] is False


def test_internal_mcp_mongo_diagnostics_reject_payload_only_operator_claims():
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    query_report = gateway.invoke(
        "mongo_query_diagnostics_report",
        {
            "allow_operator_diagnostics": True,
            "source": "in_process",
        },
    ).payload
    cost_report = gateway.invoke(
        "mongo_cost_guardrails_report",
        {},
    ).payload

    assert query_report["success"] is False
    assert cost_report["success"] is False
    assert (
        query_report["error_code"]
        == "workflow_global_admin_authority_required"
    )
    assert cost_report["error_code"] == "workflow_global_admin_authority_required"


def test_mongo_cost_guardrail_report_warns_for_remote_local_operation_rate(
    monkeypatch,
):
    monkeypatch.setenv("VON_MONGO_OPERATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("VON_MONGO_GUARDRAIL_WINDOW_SECONDS", "60")
    monkeypatch.setenv("VON_MONGO_GUARDRAIL_REMOTE_OPS_PER_MIN_WARN", "2")
    reset_mongo_operation_audit_snapshot()
    reset_mongo_large_write_snapshot()
    reset_blob_hydration_snapshot()
    reset_mongo_query_shape_telemetry()

    for _ in range(3):
        record_mongo_operation(
            service="mongo_command_listener",
            collection="$cmd",
            operation="ping",
            elapsed_ms=5,
            success=True,
        )

    report = build_mongo_cost_guardrail_report(
        mongo_classification="atlas",
        sanitized_uri="mongodb+srv://cluster.example.invalid",
        local_development=True,
    )

    codes = {warning["code"] for warning in report["warnings"]}
    assert report["schema_version"] == "mongo_cost_guardrail_report.v1"
    assert report["status"] == "warning"
    assert "remote_local_high_operation_rate" in codes
    assert report["recent_operations"]["operation_count"] == 3
    rendered = str(report)
    assert "cluster.example.invalid" in rendered
    assert "mongodb+srv://user:secret" not in rendered
    assert ".env contents" in rendered


def test_mongo_cost_guardrail_report_includes_large_write_and_hydration_without_payloads(
    monkeypatch,
):
    monkeypatch.setenv("VON_MONGO_LARGE_WRITE_WARN_BYTES", "100")
    monkeypatch.setenv("VON_MONGO_LARGE_WRITE_CRITICAL_BYTES", "200")
    reset_mongo_operation_audit_snapshot()
    reset_mongo_large_write_snapshot()
    reset_blob_hydration_snapshot()
    reset_mongo_query_shape_telemetry()

    record_mongo_large_write_attempt(
        command_name="insert",
        database="von_db",
        collection="turn_execution_records",
        estimated_size_bytes=250,
        request_id="request-123",
    )
    record_blob_hydration_observation(
        family="debug_payload",
        status="local_miss_remote_hit",
        hydrated_count=2,
    )

    report = build_mongo_cost_guardrail_report(
        mongo_classification="remote",
        sanitized_uri="mongodb://atlas-host.example.invalid",
        local_development=True,
    )

    assert report["large_write_attempts"]["rows"][0]["max_estimated_bytes"] == 250
    assert report["cache_and_hydration"]["rows"][0]["status"] == "local_miss_remote_hit"
    assert report["cache_and_hydration"]["rows"][0]["hydrated_count"] == 2
    codes = {warning["code"] for warning in report["warnings"]}
    assert "large_mongo_write_attempt" in codes
    rendered = str(report)
    assert "secret-payload" not in rendered
    assert "request-123" in rendered


def test_large_write_snapshot_records_size_only(monkeypatch):
    monkeypatch.setenv("VON_MONGO_LARGE_WRITE_WARN_BYTES", "10")
    monkeypatch.setenv("VON_MONGO_LARGE_WRITE_CRITICAL_BYTES", "100")
    reset_mongo_large_write_snapshot()

    warning = record_mongo_large_write_attempt(
        command_name="update",
        database="von_db",
        collection="chat_history",
        estimated_size_bytes=64,
        request_id="safe-request",
    )
    snapshot = get_mongo_large_write_snapshot(reset=True)

    assert warning is not None
    assert warning["warning_class"] == "warning"
    assert snapshot["rows"][0]["collection"] == "chat_history"
    assert snapshot["rows"][0]["max_estimated_bytes"] == 64
    assert "safe-request" in str(snapshot)
    assert "payload" not in str(snapshot).lower()


def test_blob_hydration_snapshot_aggregates_without_blob_keys():
    reset_blob_hydration_snapshot()
    record_blob_hydration_observation(
        family="workflow_payload",
        status="backend_store_hit",
        hydrated_count=1,
    )
    record_blob_hydration_observation(
        family="workflow_payload",
        status="missing",
        error_count=1,
    )

    snapshot = get_blob_hydration_snapshot(reset=True)

    statuses = {row["status"]: row for row in snapshot["rows"]}
    assert statuses["backend_store_hit"]["hydrated_count"] == 1
    assert statuses["missing"]["error_count"] == 1
    assert "blob/key" not in str(snapshot)


def test_internal_mcp_gateway_exposes_mongo_cost_guardrails_report(monkeypatch):
    monkeypatch.setenv("VON_MONGO_OPERATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("VON_MONGO_GUARDRAIL_REMOTE_OPS_PER_MIN_WARN", "1")
    reset_mongo_operation_audit_snapshot()
    record_mongo_operation(
        service="mongo_command_listener",
        collection="$cmd",
        operation="ping",
        elapsed_ms=2,
        success=True,
    )

    monkeypatch.setattr(
        "src.backend.db.mongo_client.get_effective_mongo_uri",
        lambda: "mongodb+srv://user:secret@example.invalid/von_db",
    )
    monkeypatch.setattr("src.backend.db.mongo_client.is_using_fallback_uri", lambda: False)
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=True,
    )

    result = gateway.invoke(
        "mongo_cost_guardrails_report",
        {"local_development": True},
    )

    payload = result.payload
    assert payload["schema_version"] == "mongo_cost_guardrail_report.v1"
    assert payload["runtime_posture"]["remote_mongo"] is True
    assert "user:secret" not in str(payload)
    assert payload["runtime_posture"]["sanitized_uri"] == "mongodb+srv://example.invalid"


def test_internal_mcp_guardrails_classify_atlas_through_ssh_tunnel(monkeypatch):
    from src.backend.db import mongo_client

    monkeypatch.setenv("VON_MONGO_OPERATION_AUDIT_ENABLED", "1")
    monkeypatch.setattr(
        mongo_client,
        "MONGO_URI",
        "mongodb+srv://user:secret@cluster.mongodb.net/von_db",
    )
    monkeypatch.setattr(
        mongo_client,
        "get_effective_mongo_uri",
        lambda: "mongodb://user:secret@127.0.0.1:27019/?directConnection=true",
    )
    monkeypatch.setattr(mongo_client, "is_using_fallback_uri", lambda: True)
    monkeypatch.setattr(
        mongo_client,
        "get_mongo_fallback_policy_state",
        lambda: {"active_fallback_kind": "ssh_tunnel"},
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=True,
    )

    payload = gateway.invoke(
        "mongo_cost_guardrails_report",
        {"local_development": True},
    ).payload

    assert payload["runtime_posture"]["remote_mongo"] is True
    assert payload["runtime_posture"]["mongo_classification"] == "atlas"
    assert (
        payload["runtime_posture"]["sanitized_uri"]
        == "mongodb+srv://cluster.mongodb.net"
    )
    assert "user:secret" not in str(payload)


def test_settings_db_guardrails_route_returns_redacted_report(monkeypatch):
    from src.backend.server.routes import settings_routes

    reset_mongo_operation_audit_snapshot()
    monkeypatch.setattr(
        settings_routes,
        "get_effective_mongo_uri",
        lambda: "mongodb+srv://user:secret@example.invalid/von_db?retryWrites=true",
    )
    monkeypatch.setattr(settings_routes, "is_using_fallback_uri", lambda: False)

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(settings_routes.settings_bp, url_prefix="/api/settings")

    response = app.test_client().get("/api/settings/db/guardrails")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    report = payload["report"]
    assert report["schema_version"] == "mongo_cost_guardrail_report.v1"
    assert report["runtime_posture"]["mongo_classification"] == "atlas"
    assert report["runtime_posture"]["sanitized_uri"] == "mongodb+srv://example.invalid"
    assert "user:secret" not in str(payload)
