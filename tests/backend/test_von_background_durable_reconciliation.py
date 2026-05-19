from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from flask import Flask

from src.backend.services.background_task_service import TaskStatus
from src.backend.workflows.durable.models import WorkflowInstanceStatus


class _RouteRegistry:
    def __init__(self, status: TaskStatus | None) -> None:
        self.status = status
        self.mark_calls: list[dict[str, Any]] = []

    def get_task_status(self, _task_id: str) -> TaskStatus | None:
        return self.status

    def mark_terminal_external(self, task_id: str, **kwargs: Any) -> TaskStatus:
        self.mark_calls.append({"task_id": task_id, **kwargs})
        self.status = TaskStatus(
            task_id=task_id,
            status=kwargs["status"],
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            result=kwargs.get("result"),
            error=kwargs.get("error"),
            progress=dict(kwargs.get("progress") or {}),
            session_id=kwargs.get("session_id"),
        )
        return self.status


class _TerminalTurnManager:
    def __init__(self, instance: Any) -> None:
        self.instance = instance
        self.calls: list[dict[str, Any]] = []

    def list_instances(self, **kwargs: Any) -> list[Any]:
        self.calls.append(dict(kwargs))
        return [self.instance]


def _make_app(monkeypatch, registry: _RouteRegistry, manager: _TerminalTurnManager) -> Flask:
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.background_task_registry",
        registry,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_instance_manager",
        lambda: manager,
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(von_bp, url_prefix="/von")
    return app


def test_background_status_reconciles_terminal_durable_turn(monkeypatch) -> None:
    task_status = TaskStatus(
        task_id="turn-123",
        status="running",
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
    )
    registry = _RouteRegistry(task_status)
    instance = SimpleNamespace(
        instance_id="instance-123",
        status=WorkflowInstanceStatus.COMPLETED,
        current_state="responded",
        error=None,
        source_event_id="turn-123",
        inputs={"conversation_session_id": "session-123"},
        outputs={
            "request_id": "turn-123",
            "session_id": "session-123",
            "response": "Durable answer",
            "display_elements": {"elements": []},
        },
    )
    manager = _TerminalTurnManager(instance)
    app = _make_app(monkeypatch, registry, manager)

    response = app.test_client().get("/von/api/task/status/turn-123")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "completed"
    assert body["has_result"] is True
    assert body["progress"]["workflow_instance_id"] == "instance-123"
    assert registry.mark_calls[0]["task_id"] == "turn-123"
    assert manager.calls[0]["source_event_id"] == "turn-123"


def test_background_result_returns_reconciled_durable_payload(monkeypatch) -> None:
    registry = _RouteRegistry(
        TaskStatus(
            task_id="turn-456",
            status="running",
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
    )
    instance = SimpleNamespace(
        instance_id="instance-456",
        status=WorkflowInstanceStatus.COMPLETED,
        current_state="responded",
        error=None,
        source_event_id="turn-456",
        inputs={"conversation_session_id": "session-456"},
        outputs={
            "request_id": "turn-456",
            "session_id": "session-456",
            "response": "Durable result text",
            "display_elements": {
                "elements": [
                    {
                        "element_type": "text",
                        "payload": {"text": "Durable result text"},
                    }
                ]
            },
        },
    )
    manager = _TerminalTurnManager(instance)
    app = _make_app(monkeypatch, registry, manager)

    response = app.test_client().get("/von/api/task/result/turn-456")

    assert response.status_code == 200
    body = response.get_json()
    assert body["task_id"] == "turn-456"
    assert body["result"]["response"] == "Durable result text"
    assert body["result"]["conversation_session_id"] == "session-456"
    assert body["result"]["workflow_instance_id"] == "instance-456"
    assert body["result"]["llm_debug"]["background_result_source"] == (
        "durable_conversation_turn_instance"
    )


def test_background_result_restores_failed_task_from_completed_durable_turn(
    monkeypatch,
) -> None:
    registry = _RouteRegistry(
        TaskStatus(
            task_id="turn-789",
            status="failed",
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            error="post-processing failed",
        )
    )
    instance = SimpleNamespace(
        instance_id="instance-789",
        status=WorkflowInstanceStatus.COMPLETED,
        current_state="responded",
        error=None,
        source_event_id="turn-789",
        inputs={"conversation_session_id": "session-789"},
        outputs={
            "request_id": "turn-789",
            "session_id": "session-789",
            "response": "Restored durable result",
        },
    )
    manager = _TerminalTurnManager(instance)
    app = _make_app(monkeypatch, registry, manager)

    response = app.test_client().get("/von/api/task/result/turn-789")

    assert response.status_code == 200
    body = response.get_json()
    assert body["result"]["response"] == "Restored durable result"
    assert registry.status is not None
    assert registry.status.status == "completed"
    assert registry.status.error is None
