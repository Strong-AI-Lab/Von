"""Route-level coverage for task parity fields (start date + epic linkage)."""

from __future__ import annotations

from flask import Flask

from src.backend.server.routes.task_routes import task_bp


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
            "start_date": kwargs["start_date"].isoformat()
            if kwargs.get("start_date")
            else None,
            "due_date": kwargs["due_date"].isoformat() if kwargs.get("due_date") else None,
            "epic_task_concept_id": kwargs.get("epic_task_concept_id"),
            "components": kwargs.get("components"),
            "fix_versions": kwargs.get("fix_versions"),
            "sprint_values": kwargs.get("sprint_values"),
            "backlog_rank": kwargs.get("backlog_rank"),
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


def test_update_task_route_uses_update_task_fields(monkeypatch):
    client = _build_client()
    captured: dict = {}

    def _fake_update_task_fields(task_concept_id: str, *, fields: dict, actor_concept_id=None):
        captured["task_concept_id"] = task_concept_id
        captured["fields"] = fields
        captured["actor_concept_id"] = actor_concept_id
        return {"task": {"task_concept_id": task_concept_id}, "changed_fields": list(fields)}

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
        },
    )

    assert response.status_code == 200
    assert captured["task_concept_id"] == "#V#task_1"
    assert captured["fields"]["epic_task_concept_id"] == "#V#task_epic_1"
    assert captured["fields"]["components"] == ["Workflow Engine"]
    assert captured["fields"]["fix_versions"] == ["R1"]
    assert captured["fields"]["sprint_values"] == ["Sprint 6"]
    assert captured["fields"]["backlog_rank"] == "0|i00123:"
    assert captured["actor_concept_id"] == "#V#user_alice"


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
