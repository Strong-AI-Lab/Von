"""Route-level coverage for task parity fields (start date + epic linkage)."""

from __future__ import annotations

from flask import Flask

from src.backend.server.routes.task_routes import task_bp
from src.backend.services.task_management_service import (
    TASK_ORGANISATION_SCOPE_CURRENT_PLUS_UNSCOPED,
    TASK_ORGANISATION_SCOPE_UNSCOPED_ONLY,
    TaskOrganisationScopeAccessError,
)


def _build_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    app.register_blueprint(task_bp, url_prefix="/api/tasks")
    return app.test_client()


def test_create_task_route_parses_start_and_due_dates(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_create_task(**kwargs):
        captured.update(kwargs)
        return {
            "task_concept_id": "#V#task_1",
            "title": kwargs["title"],
            "description": kwargs["description"],
            "start_date": (
                kwargs["start_date"].isoformat() if kwargs.get("start_date") else None
            ),
            "due_date": (
                kwargs["due_date"].isoformat() if kwargs.get("due_date") else None
            ),
            "epic_task_concept_id": kwargs.get("epic_task_concept_id"),
            "components": kwargs.get("components"),
            "fix_versions": kwargs.get("fix_versions"),
            "sprint_values": kwargs.get("sprint_values"),
            "backlog_rank": kwargs.get("backlog_rank"),
            "task_type_ids": kwargs.get("task_type_ids"),
            "task_source_id": kwargs.get("task_source_id"),
            "report_to_concept_id": kwargs.get("report_to_concept_id"),
            "task_role": kwargs.get("task_role"),
            "next_checkpoint": kwargs.get("next_checkpoint"),
            "progress_signal": kwargs.get("progress_signal"),
            "evidence": kwargs.get("evidence"),
            "notes": kwargs.get("notes"),
            "reference_code": kwargs.get("reference_code"),
        }

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.create_task",
        _fake_create_task,
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user_alice"
        sess["org_id"] = "#V#org_1"

    response = client.post(
        "/api/tasks/",
        json={
            "title": "Task",
            "description": "Desc",
            "start_date": "2026-03-01T10:00:00Z",
            "due_date": "2026-03-05T10:00:00Z",
            "epic_task_concept_id": "#V#task_epic_1",
            "components": ["Workflow Engine"],
            "fix_versions": ["R1"],
            "sprint_values": ["Sprint 6"],
            "backlog_rank": "0|i00123:",
            "task_type_ids": ["#V#delegated_task_specification"],
            "task_source_id": "#V#jira_imported_task_source",
            "report_to_concept_id": "#V#user_manager",
            "task_role": "Communicator",
            "next_checkpoint": "Tomorrow morning",
            "progress_signal": "Confirmed by chat",
            "evidence": "Printed document",
            "notes": "Needs a coloured copy",
            "reference_code": "TASK-001",
        },
    )

    assert response.status_code == 201
    assert captured["start_date"].isoformat() == "2026-03-01T10:00:00+00:00"
    assert captured["due_date"].isoformat() == "2026-03-05T10:00:00+00:00"
    assert captured["epic_task_concept_id"] == "#V#task_epic_1"
    assert captured["components"] == ["Workflow Engine"]
    assert captured["fix_versions"] == ["R1"]
    assert captured["sprint_values"] == ["Sprint 6"]
    assert captured["backlog_rank"] == "0|i00123:"
    assert captured["task_type_ids"] == ["#V#delegated_task_specification"]
    assert captured["task_source_id"] == "#V#jira_imported_task_source"
    assert captured["report_to_concept_id"] == "#V#user_manager"
    assert captured["task_role"] == "Communicator"
    assert captured["next_checkpoint"] == "Tomorrow morning"
    assert captured["progress_signal"] == "Confirmed by chat"
    assert captured["evidence"] == "Printed document"
    assert captured["notes"] == "Needs a coloured copy"
    assert captured["reference_code"] == "TASK-001"


def test_update_task_route_uses_update_task_fields(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_update_task_fields(
        task_concept_id: str, *, fields: dict, actor_concept_id=None
    ):
        captured["task_concept_id"] = task_concept_id
        captured["fields"] = fields
        captured["actor_concept_id"] = actor_concept_id
        return {
            "task": {"task_concept_id": task_concept_id},
            "changed_fields": list(fields),
        }

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.update_task_fields",
        _fake_update_task_fields,
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user_alice"

    response = client.patch(
        "/api/tasks/%23V%23task_1",
        json={
            "start_date": "2026-03-01T10:00:00Z",
            "epic_task_concept_id": "#V#task_epic_1",
            "components": ["Workflow Engine"],
            "fix_versions": ["R1"],
            "sprint_values": ["Sprint 6"],
            "backlog_rank": "0|i00123:",
            "task_type_ids": ["#V#delegated_task_specification"],
            "task_source_id": "#V#jira_imported_task_source",
            "report_to_concept_id": "#V#user_manager",
            "task_role": "Communicator",
            "next_checkpoint": "Tomorrow morning",
            "progress_signal": "Confirmed by chat",
            "evidence": "Printed document",
            "notes": "Needs a coloured copy",
            "reference_code": "TASK-001",
        },
    )

    assert response.status_code == 200
    assert captured["task_concept_id"] == "#V#task_1"
    assert captured["fields"]["epic_task_concept_id"] == "#V#task_epic_1"
    assert captured["fields"]["components"] == ["Workflow Engine"]
    assert captured["fields"]["fix_versions"] == ["R1"]
    assert captured["fields"]["sprint_values"] == ["Sprint 6"]
    assert captured["fields"]["backlog_rank"] == "0|i00123:"
    assert captured["fields"]["task_type_ids"] == ["#V#delegated_task_specification"]
    assert captured["fields"]["task_source_id"] == "#V#jira_imported_task_source"
    assert captured["fields"]["report_to_concept_id"] == "#V#user_manager"
    assert captured["fields"]["task_role"] == "Communicator"
    assert captured["fields"]["next_checkpoint"] == "Tomorrow morning"
    assert captured["fields"]["progress_signal"] == "Confirmed by chat"
    assert captured["fields"]["evidence"] == "Printed document"
    assert captured["fields"]["notes"] == "Needs a coloured copy"
    assert captured["fields"]["reference_code"] == "TASK-001"
    assert captured["actor_concept_id"] == "#V#user_alice"


def test_update_task_route_returns_typed_scope_denial(monkeypatch):
    client = _build_client()

    def _deny_scope_change(*args, **kwargs):
        raise TaskOrganisationScopeAccessError(
            "organisation_membership_required",
            "You must be a represented member of both the source and destination organisations",
        )

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.update_task_fields",
        _deny_scope_change,
    )

    response = client.patch(
        "/api/tasks/%23V%23task_1",
        json={"organisation_concept_id": "#V#other_org"},
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "organisation_membership_required"


def test_search_tasks_route_supports_start_and_epic_filters(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_search_tasks(**kwargs):
        captured.update(kwargs)
        return {"tasks": [], "total": 0, "count": 0, "offset": 0, "limit": 50}

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.search_tasks",
        _fake_search_tasks,
    )

    response = client.get(
        "/api/tasks/search?epic_task_concept_id=%23V%23task_epic_1&has_epic=true"
        "&start_from=2026-03-01T00:00:00Z&start_to=2026-03-05T00:00:00Z"
        "&components=Workflow%20Engine&fix_versions=R1&sprints=Sprint%206"
        "&backlog_rank=0%7Ci00123%3A&has_backlog_rank=true"
        "&task_type_ids=%23V%23delegated_task_specification"
        "&task_source_id=%23V%23jira_imported_task_source"
        "&created_by_concept_id=%23V%23user_creator"
        "&report_to_concept_id=%23V%23user_manager"
    )

    assert response.status_code == 200
    assert captured["epic_task_concept_id"] == "#V#task_epic_1"
    assert captured["has_epic"] is True
    assert captured["start_from"] == "2026-03-01T00:00:00Z"
    assert captured["start_to"] == "2026-03-05T00:00:00Z"
    assert captured["components"] == ["Workflow Engine"]
    assert captured["fix_versions"] == ["R1"]
    assert captured["sprint_values"] == ["Sprint 6"]
    assert captured["backlog_rank"] == "0|i00123:"
    assert captured["has_backlog_rank"] is True
    assert captured["task_type_ids"] == ["#V#delegated_task_specification"]
    assert captured["task_source_id"] == "#V#jira_imported_task_source"
    assert captured["created_by_concept_id"] == "#V#user_creator"
    assert captured["report_to_concept_id"] == "#V#user_manager"


def test_list_tasks_route_forwards_bulk_visibility_and_user_scope(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_list_tasks_with_visibility(**kwargs):
        captured.update(kwargs)
        return {
            "tasks": [{"task_concept_id": "#V#task_native", "title": "Native"}],
            "count": 1,
            "total": 1,
            "offset": kwargs["offset"],
            "limit": kwargs["limit"],
            "bulk_visibility": kwargs["bulk_visibility"],
            "bulk_collection_ids": ["#V#jira_task_migration_bulk_collection"],
            "hidden_bulk_task_total": 42,
            "hidden_bulk_task_collections": [
                {
                    "collection_id": "#V#jira_task_migration_bulk_collection",
                    "label": "Jira migration backlog",
                    "count": 42,
                }
            ],
        }

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.list_tasks_with_visibility",
        _fake_list_tasks_with_visibility,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.task_routes._get_current_org_concept_id",
        lambda: "#V#household_org",
    )

    response = client.get(
        "/api/tasks/?bulk_visibility=exclude&limit=10&offset=5"
        "&include_total=false&include_bulk_summary=false"
        "&assignee_concept_id=%23V%23user_alice"
        "&bulk_collection_id=%23V%23jira_task_migration_bulk_collection"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert [task["task_concept_id"] for task in payload["tasks"]] == ["#V#task_native"]
    assert payload["hidden_bulk_task_total"] == 42
    assert payload["total_matching_count"] == 1
    assert payload["hidden_bulk_task_collections"][0]["label"] == (
        "Jira migration backlog"
    )
    assert captured["assignee_concept_id"] == "#V#user_alice"
    assert captured["bulk_visibility"] == "exclude"
    assert captured["bulk_collection_ids"] == ["#V#jira_task_migration_bulk_collection"]
    assert captured["limit"] == 10
    assert captured["offset"] == 5
    assert captured["include_total"] is False
    assert captured["include_bulk_summary"] is False
    assert captured["organisation_concept_id"] == "#V#household_org"
    assert (
        captured["organisation_scope_mode"]
        == TASK_ORGANISATION_SCOPE_CURRENT_PLUS_UNSCOPED
    )
    telemetry = payload["load_telemetry"]
    assert telemetry["schema_version"] == "task_list_load_telemetry.v1"
    assert telemetry["source"] == "task_routes.list_tasks_route"
    assert telemetry["mode"] == "task_listing"
    assert telemetry["request"]["bulk_visibility"] == "exclude"
    assert telemetry["request"]["bulk_collection_count"] == 1
    assert any(
        stage["stage"] == "list_tasks_with_visibility"
        for stage in telemetry["stages"]
    )


def test_list_tasks_route_telemetry_handles_missing_bulk_collection_ids(monkeypatch):
    client = _build_client()

    def _fake_list_tasks_with_visibility(**kwargs):
        return {
            "tasks": [],
            "count": 0,
            "total": 0,
            "offset": kwargs["offset"],
            "limit": kwargs["limit"],
            "bulk_visibility": kwargs["bulk_visibility"],
            "bulk_collection_ids": [],
            "hidden_bulk_task_total": 0,
            "hidden_bulk_task_collections": [],
        }

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.list_tasks_with_visibility",
        _fake_list_tasks_with_visibility,
    )

    response = client.get("/api/tasks/?limit=10&bulk_visibility=exclude")

    assert response.status_code == 200
    telemetry = response.get_json()["load_telemetry"]
    assert telemetry["request"]["bulk_collection_count"] == 0
    assert (
        telemetry["request"]["organisation_scope_mode"]
        == TASK_ORGANISATION_SCOPE_UNSCOPED_ONLY
    )


def test_my_tasks_route_uses_visibility_listing(monkeypatch):
    client = _build_client()
    captured: dict = {}

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes._get_current_user_concept_id",
        lambda: "#V#user_alice",
    )

    def _fake_list_tasks_with_visibility(**kwargs):
        captured.update(kwargs)
        return {
            "tasks": [{"task_concept_id": "#V#task_visible", "title": "Visible"}],
            "count": 1,
            "total": 7,
            "offset": kwargs["offset"],
            "limit": kwargs["limit"],
            "bulk_visibility": kwargs["bulk_visibility"],
            "bulk_collection_ids": ["#V#jira_task_migration_bulk_collection"],
            "hidden_bulk_task_total": 42,
            "hidden_bulk_task_collections": [
                {
                    "collection_id": "#V#jira_task_migration_bulk_collection",
                    "label": "Jira migration backlog",
                    "count": 42,
                }
            ],
        }

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.list_tasks_with_visibility",
        _fake_list_tasks_with_visibility,
    )

    response = client.get(
        "/api/tasks/my?include_created=true&status=pending&priority=high"
        "&task_type_ids=%23V%23delegated_task_specification"
        "&task_source_id=%23V%23jira_imported_task_source"
        "&bulk_visibility=exclude&limit=25&offset=10"
        "&bulk_collection_id=%23V%23jira_task_migration_bulk_collection"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["count"] == 1
    assert payload["total"] == 7
    assert payload["total_matching_count"] == 7
    assert payload["hidden_bulk_task_total"] == 42
    assert captured["user_concept_id"] == "#V#user_alice"
    assert captured["include_created"] is True
    assert captured["status_filter"] == "pending"
    assert captured["priority_filter"] == "high"
    assert captured["task_type_ids"] == ["#V#delegated_task_specification"]
    assert captured["task_source_ids"] == ["#V#jira_imported_task_source"]
    assert captured["bulk_visibility"] == "exclude"
    assert captured["bulk_collection_ids"] == ["#V#jira_task_migration_bulk_collection"]
    assert captured["limit"] == 25
    assert captured["offset"] == 10


def test_jira_migration_bulk_backfill_route_defaults_to_dry_run(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_backfill(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "dry_run": kwargs["dry_run"],
            "candidate_count": 3,
            "updated_count": 0,
        }

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.backfill_jira_migration_bulk_task_collections",
        _fake_backfill,
    )

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user_alice"
        sess["org_id"] = "#V#org_1"

    response = client.post(
        "/api/tasks/bulk-collections/jira-migration/backfill", json={}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["dry_run"] is True
    assert captured["actor_concept_id"] == "#V#user_alice"
    assert captured["organisation_concept_id"] == "#V#org_1"


def test_get_task_taxonomy_route_returns_service_result(monkeypatch):
    client = _build_client()

    monkeypatch.setattr(
        "src.backend.server.routes.task_routes.get_task_taxonomy",
        lambda: {
            "task_types": [
                {"concept_id": "#V#one_off_task_specification", "label": "One-off"}
            ],
            "task_sources": [
                {"concept_id": "#V#von_native_task_source", "label": "Von native"}
            ],
            "defaults": {
                "task_type_id": "#V#one_off_task_specification",
                "task_source_id": "#V#von_native_task_source",
            },
        },
    )

    response = client.get("/api/tasks/taxonomy")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["defaults"]["task_type_id"] == "#V#one_off_task_specification"
    assert payload["task_sources"][0]["concept_id"] == "#V#von_native_task_source"
