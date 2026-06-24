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
            user_id=kwargs.get("user_id"),
        )
        return self.status


class _TerminalTurnManager:
    def __init__(self, instance: Any) -> None:
        self.instance = instance
        self.calls: list[dict[str, Any]] = []

    def list_instances(self, **kwargs: Any) -> list[Any]:
        self.calls.append(dict(kwargs))
        return [self.instance]


def _make_app(
    monkeypatch, registry: _RouteRegistry, manager: _TerminalTurnManager
) -> Flask:
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


def test_background_result_prefers_user_answer_over_machine_json_response(
    monkeypatch,
) -> None:
    registry = _RouteRegistry(
        TaskStatus(
            task_id="turn-json",
            status="running",
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
    )
    instance = SimpleNamespace(
        instance_id="instance-json",
        status=WorkflowInstanceStatus.COMPLETED,
        current_state="responded",
        error=None,
        source_event_id="turn-json",
        inputs={"conversation_session_id": "session-json"},
        outputs={
            "request_id": "turn-json",
            "session_id": "session-json",
            "response": (
                '{"confidence": 1.0, "workflow_id": '
                '"#V#turn_completion_gate_workflow"}'
            ),
            "selected_workflow_user_response": "Grounded Jira answer.",
            "final_response": "Grounded Jira answer.",
            "completion_gate_decision": "completed",
            "completion_gate_requires_follow_up": False,
            "completion_gate_safe_to_claim_completion": True,
        },
    )
    manager = _TerminalTurnManager(instance)
    app = _make_app(monkeypatch, registry, manager)

    response = app.test_client().get("/von/api/task/result/turn-json")

    assert response.status_code == 200
    body = response.get_json()
    assert body["result"]["response"] == "Grounded Jira answer."
    assert body["result"]["llm_debug"]["response"] == "Grounded Jira answer."
    assert body["result"]["llm_debug"]["completion_gate_verdict"] == {
        "decision": "completed",
        "decision_reason": None,
        "requires_follow_up": False,
        "safe_to_claim_completion": True,
        "terminal_outcome": None,
        "evidence_payload": None,
    }


def test_background_result_uses_completion_report_before_machine_json(
    monkeypatch,
) -> None:
    registry = _RouteRegistry(
        TaskStatus(
            task_id="turn-report",
            status="running",
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
    )
    instance = SimpleNamespace(
        instance_id="instance-report",
        status=WorkflowInstanceStatus.COMPLETED,
        current_state="responded",
        error=None,
        source_event_id="turn-report",
        inputs={"conversation_session_id": "session-report"},
        outputs={
            "request_id": "turn-report",
            "session_id": "session-report",
            "response": (
                '{"confidence": 1.0, "workflow_id": '
                '"#V#turn_completion_gate_workflow"}'
            ),
            "final_response": (
                '{"confidence": 1.0, "workflow_id": '
                '"#V#turn_completion_gate_workflow"}'
            ),
            "completion_report": {
                "response_text": "Here is the grounded completion report answer."
            },
        },
    )
    manager = _TerminalTurnManager(instance)
    app = _make_app(monkeypatch, registry, manager)

    response = app.test_client().get("/von/api/task/result/turn-report")

    assert response.status_code == 200
    body = response.get_json()
    assert (
        body["result"]["response"] == "Here is the grounded completion report answer."
    )
    assert body["result"]["llm_debug"]["completion_report"] == {
        "response_text": "Here is the grounded completion report answer."
    }


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


def test_background_generate_success_body_marks_task_completed_before_persistence(
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes

    registry = _RouteRegistry(
        TaskStatus(
            task_id="turn-ready",
            status="running",
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
    )
    monkeypatch.setattr(von_routes, "background_task_registry", registry)

    von_routes._mark_background_generate_completed_if_ready(
        background_task_id="turn-ready",
        result_body={"response": "Ready answer", "request_id": "turn-ready"},
        request_id="turn-ready",
        session_id="session-ready",
        user_id="#V#michael_witbrock",
        response_text="Ready answer",
    )

    assert registry.status is not None
    assert registry.status.status == "completed"
    assert registry.status.session_id == "session-ready"
    assert registry.status.user_id == "#V#michael_witbrock"
    assert registry.status.result == {
        "response": "Ready answer",
        "request_id": "turn-ready",
    }
    assert registry.status.progress["source"] == "background_generate_success_body"
    assert registry.status.progress["phase"] == "response_finalising"


def test_orchestrator_ready_progress_marks_background_generate_completed(
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes

    registry = _RouteRegistry(
        TaskStatus(
            task_id="turn-orchestrator-ready",
            status="running",
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
    )
    monkeypatch.setattr(von_routes, "background_task_registry", registry)

    von_routes._mark_background_generate_completed_from_orchestrator_ready_progress(
        background_task_id="turn-orchestrator-ready",
        progress_payload={
            "status": "orchestrator_result_ready",
            "response_text": "<spoken>Ready.</spoken><screen>Ready answer</screen>",
            "model": "qwen3:8b",
            "workflow_routing": {"selected_workflow_id": "#V#general_mail_review"},
            "tool_invocations": [{"tool": "gmail_list_messages"}],
            "aux_llm_calls": [{"type": "workflow_use_episode"}],
        },
        request_id="turn-orchestrator-ready",
        session_id="session-ready",
        created_conversation_session_name=None,
        created_conversation_session=False,
        user_id="#V#michael_witbrock",
        rag_trace={"tools_invoked": ["gmail_list_messages"]},
    )

    assert registry.status is not None
    assert registry.status.status == "completed"
    assert registry.status.session_id == "session-ready"
    assert registry.status.user_id == "#V#michael_witbrock"
    assert registry.status.result["response"] == (
        "<spoken>Ready.</spoken><screen>Ready answer</screen>"
    )
    assert registry.status.result["response_channels"]["screen"] == "Ready answer"
    assert registry.status.result["llm_debug"]["background_result_source"] == (
        "orchestrator_result_ready_progress"
    )
    assert registry.status.result["llm_debug"]["workflow_routing"] == {
        "selected_workflow_id": "#V#general_mail_review"
    }
