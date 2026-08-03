from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from flask import Flask

from src.backend.server.routes import von_routes
from src.backend.services.background_task_service import TaskStatus

OWNER_USER_ID = "#V#background_task_owner"
OTHER_USER_ID = "#V#background_task_other_user"


class _PrivateTaskRegistry:
    def __init__(self, status: TaskStatus) -> None:
        self.status = status
        self.cancellation_calls: list[str] = []

    def get_task_status(self, _task_id: str) -> TaskStatus:
        return self.status

    def request_cancellation(self, task_id: str) -> bool:
        self.cancellation_calls.append(task_id)
        return True


def _make_app(monkeypatch, status: TaskStatus) -> tuple[Flask, _PrivateTaskRegistry]:
    registry = _PrivateTaskRegistry(status)
    monkeypatch.setattr(von_routes, "background_task_registry", registry)

    app = Flask(__name__)
    app.secret_key = "background-task-actor-test"
    app.config["TESTING"] = True
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    return app, registry


def _task_status(*, user_id: str | None = OWNER_USER_ID) -> TaskStatus:
    return TaskStatus(
        task_id="private-task",
        status="completed",
        created_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        result={"response": "private result"},
        progress={"status": "completed"},
        user_id=user_id,
    )


def _authenticate(client: Any, user_id: str) -> None:
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = user_id
        flask_session["organisation_concept_id"] = "#V#background_task_org"
        flask_session["namespace"] = f"{user_id}@background_task_org"


@pytest.mark.parametrize(
    ("method", "path", "request_kwargs"),
    [
        (
            "GET",
            "/von/api/task/status/private-task",
            {"query_string": {"user_id": OWNER_USER_ID}},
        ),
        (
            "GET",
            "/von/api/task/result/private-task",
            {"query_string": {"user_id": OWNER_USER_ID}},
        ),
        (
            "POST",
            "/von/api/task/cancel/private-task",
            {"json": {"user_id": OWNER_USER_ID}},
        ),
    ],
)
def test_background_task_routes_hide_cross_user_tasks_and_ignore_supplied_identity(
    monkeypatch,
    method: str,
    path: str,
    request_kwargs: dict[str, Any],
) -> None:
    app, registry = _make_app(monkeypatch, _task_status())
    client = app.test_client()
    _authenticate(client, OTHER_USER_ID)

    response = client.open(path, method=method, **request_kwargs)

    assert response.status_code == 404
    assert response.get_json()["error_code"] == "task_not_found"
    assert registry.cancellation_calls == []


def test_background_task_status_fails_closed_without_authenticated_actor(
    monkeypatch,
) -> None:
    app, _registry = _make_app(monkeypatch, _task_status())

    response = app.test_client().get("/von/api/task/status/private-task")

    assert response.status_code == 401
    assert response.get_json()["error_code"] == "not_authenticated"


def test_background_task_status_hides_ownerless_private_task(monkeypatch) -> None:
    app, _registry = _make_app(monkeypatch, _task_status(user_id=None))
    client = app.test_client()
    _authenticate(client, OWNER_USER_ID)

    response = client.get("/von/api/task/status/private-task")

    assert response.status_code == 404
    assert response.get_json()["error_code"] == "task_not_found"
