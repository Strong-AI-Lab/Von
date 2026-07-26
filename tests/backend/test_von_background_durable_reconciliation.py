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
    def __init__(self, instance: Any, *, hydrated_instance: Any = None) -> None:
        self.instance = instance
        self.hydrated_instance = hydrated_instance or instance
        self.calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []

    def list_instances(self, **kwargs: Any) -> list[Any]:
        self.calls.append(dict(kwargs))
        return [self.instance]

    def get_instance(self, instance_id: str) -> Any:
        self.get_calls.append(instance_id)
        return self.hydrated_instance


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
    assert body["progress"]["workflow_lifecycle_status"] == "completed"
    assert body["progress"]["user_outcome_status"] == "unknown"
    assert body["progress"]["user_outcome_source"] == (
        "missing_terminal_outcome_evidence"
    )
    assert registry.mark_calls[0]["task_id"] == "turn-123"
    assert manager.calls[0]["source_event_id"] == "turn-123"


def test_background_status_does_not_call_lifecycle_completion_user_success(
    monkeypatch,
) -> None:
    registry = _RouteRegistry(
        TaskStatus(
            task_id="turn-2494",
            status="completed",
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            result={"response": ""},
            progress={"status": "completed"},
        )
    )
    instance = SimpleNamespace(
        instance_id="instance-2494",
        status=WorkflowInstanceStatus.COMPLETED,
        current_state="context_adjudication",
        error=None,
        source_event_id="turn-2494",
        inputs={"conversation_session_id": "session-2494"},
        outputs={
            "request_id": "turn-2494",
            "session_id": "session-2494",
            "last_action_failed": True,
            "last_action_error": (
                "subworkflow_failed:#V#turn_context_adjudication_workflow:"
                "workflow_llm_step_timeout:context_adjudication"
            ),
        },
    )
    manager = _TerminalTurnManager(instance)
    app = _make_app(monkeypatch, registry, manager)

    response = app.test_client().get("/von/api/task/status/turn-2494")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "completed"
    assert body["progress"]["workflow_lifecycle_status"] == "completed"
    assert body["progress"]["user_outcome_status"] == "terminal_failure"
    assert body["progress"]["user_outcome_source"] == "workflow_failure_evidence"
    assert body["progress"]["safe_to_claim_completion"] is None
    assert registry.status is not None
    assert registry.status.result["llm_debug"]["workflow_failure_evidence"][
        "last_action_error"
    ].startswith("subworkflow_failed:")


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


def test_background_result_hydrates_compact_terminal_instance_before_projection(
    monkeypatch,
) -> None:
    registry = _RouteRegistry(
        TaskStatus(
            task_id="turn-offloaded",
            status="running",
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
    )
    compact_instance = SimpleNamespace(
        instance_id="instance-offloaded",
        status=WorkflowInstanceStatus.COMPLETED,
        current_state="context_adjudication",
        error=None,
        source_event_id="turn-offloaded",
        inputs={"conversation_session_id": "session-offloaded"},
        outputs={
            "response": {
                "schema_version": "workflow_payload_blob_ref.v1",
                "blob_ref": {"key": "blob-response"},
            },
            "turn_execution_record": {
                "schema_version": "workflow_payload_blob_ref.v1",
                "blob_ref": {"key": "blob-ter"},
            },
        },
    )
    failure = (
        "subworkflow_failed:#V#turn_context_adjudication_workflow:"
        "workflow_llm_step_timeout:context_adjudication"
    )
    hydrated_instance = SimpleNamespace(
        **{
            **compact_instance.__dict__,
            "outputs": {
                "request_id": "turn-offloaded",
                "session_id": "session-offloaded",
                "response": "The context-adjudication model timed out.",
                "final_response": "The context-adjudication model timed out.",
                "response_channels": {
                    "screen": "The context-adjudication model timed out.",
                    "spoken": "The model timed out.",
                },
                "last_action_failed": True,
                "last_action_error": failure,
                "completion_gate": {
                    "decision": "escalation_required",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                },
                "turn_execution_record": {
                    "schema_version": "turn_execution_record.v1",
                    "completion_gate": {
                        "decision": "escalation_required",
                        "safe_to_claim_completion": False,
                    },
                },
            },
        }
    )
    manager = _TerminalTurnManager(
        compact_instance,
        hydrated_instance=hydrated_instance,
    )
    app = _make_app(monkeypatch, registry, manager)

    response = app.test_client().get("/von/api/task/result/turn-offloaded")

    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result["response"] == "The context-adjudication model timed out."
    assert result["response_channels"]["screen"] == (
        "The context-adjudication model timed out."
    )
    assert result["llm_debug"]["turn_execution_record"]["schema_version"] == (
        "turn_execution_record.v1"
    )
    assert (
        result["llm_debug"]["workflow_failure_evidence"]["last_action_error"] == failure
    )
    assert result["workflow_lifecycle_status"] == "completed"
    assert result["user_outcome_status"] == "non_success"
    assert result["safe_to_claim_completion"] is False
    assert result["response_availability"]["status"] == "available"
    assert result["diagnostics_availability"]["status"] == "available"
    assert manager.get_calls == ["instance-offloaded"]


def test_background_result_types_unavailable_compact_payload_when_hydration_fails(
    monkeypatch,
) -> None:
    registry = _RouteRegistry(
        TaskStatus(
            task_id="turn-unavailable",
            status="running",
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
    )
    blob_ref = {
        "schema_version": "workflow_payload_blob_ref.v1",
        "blob_ref": {"key": "blob-unavailable"},
    }
    compact_instance = SimpleNamespace(
        instance_id="instance-unavailable",
        status=WorkflowInstanceStatus.COMPLETED,
        current_state="responded",
        error=None,
        source_event_id="turn-unavailable",
        inputs={"conversation_session_id": "session-unavailable"},
        outputs={"response": blob_ref, "turn_execution_record": blob_ref},
    )
    manager = _TerminalTurnManager(compact_instance)

    def _fail_hydration(_instance_id: str) -> Any:
        raise RuntimeError("blob backend unavailable")

    monkeypatch.setattr(manager, "get_instance", _fail_hydration)
    app = _make_app(monkeypatch, registry, manager)

    response = app.test_client().get("/von/api/task/result/turn-unavailable")

    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result["response"] == ""
    assert result["response_availability"]["status"] == "response_unavailable"
    assert result["diagnostics_availability"]["status"] == ("diagnostics_unavailable")
    assert result["payload_hydration"] == {
        "status": "unresolved_blob_references",
        "unresolved_output_fields": ["response", "turn_execution_record"],
    }
    assert result["workflow_lifecycle_status"] == "completed"
    assert result["user_outcome_status"] == "unknown"


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
