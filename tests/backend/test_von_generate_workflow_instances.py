from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest
from flask import Flask

from src.backend.workflows import CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
from src.backend.workflows.durable.models import (
    WorkflowInstance,
    WorkflowInstanceStatus,
)
from src.backend.workflows.durable.workflow_instance_submission_service import (
    WorkflowInstanceSubmissionResult,
)


class _InMemoryWorkflowInstanceManager:
    def __init__(self) -> None:
        self._counter = 0
        self.instances: dict[str, WorkflowInstance] = {}

    def _next_instance_id(self) -> str:
        self._counter += 1
        return f"wf-inst-{self._counter}"

    def create_instance(
        self,
        workflow_id: str,
        *,
        user_id: str,
        org_id: str | None,
        namespace: str,
        inputs: dict[str, Any] | None = None,
        schedule_id: str | None = None,
        max_retries: int = 3,
        source_event_type: str | None = None,
        source_event_id: str | None = None,
        event_idempotency_key: str | None = None,
    ) -> str:
        instance = WorkflowInstance.create(
            workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            inputs=dict(inputs or {}),
            schedule_id=schedule_id,
            max_retries=max_retries,
            source_event_type=source_event_type,
            source_event_id=source_event_id,
            event_idempotency_key=event_idempotency_key,
        )
        instance.instance_id = self._next_instance_id()
        self.instances[instance.instance_id] = instance
        return instance.instance_id

    def create_instance_for_event(
        self,
        workflow_id: str,
        *,
        user_id: str,
        org_id: str | None,
        namespace: str,
        event_idempotency_key: str,
        source_event_type: str,
        source_event_id: str,
        inputs: dict[str, Any] | None = None,
        schedule_id: str | None = None,
        max_retries: int = 3,
    ) -> tuple[str, bool]:
        for instance in self.instances.values():
            if instance.event_idempotency_key == event_idempotency_key:
                return instance.instance_id, False
        return (
            self.create_instance(
                workflow_id,
                user_id=user_id,
                org_id=org_id,
                namespace=namespace,
                inputs=inputs,
                schedule_id=schedule_id,
                max_retries=max_retries,
                source_event_type=source_event_type,
                source_event_id=source_event_id,
                event_idempotency_key=event_idempotency_key,
            ),
            True,
        )

    def checkpoint(self, instance_id: str, **kwargs: Any) -> bool:
        instance = self.instances[instance_id]
        workflow_data = kwargs.get("workflow_data")
        merged_workflow_data = dict(instance.workflow_data)
        if isinstance(workflow_data, dict):
            merged_workflow_data.update(workflow_data)
        self.instances[instance_id] = replace(
            instance,
            current_state=str(kwargs.get("current_state") or instance.current_state),
            workflow_data=merged_workflow_data,
            progress_message=kwargs.get("progress_message", instance.progress_message),
            progress_updated_at=datetime.now(timezone.utc),
        )
        return True

    def mark_completed(self, instance_id: str, **kwargs: Any) -> bool:
        instance = self.instances[instance_id]
        self.instances[instance_id] = replace(
            instance,
            status=WorkflowInstanceStatus.COMPLETED,
            current_state=str(kwargs.get("final_state") or "completed"),
            completed_at=datetime.now(timezone.utc),
            outputs=kwargs.get("outputs"),
        )
        return True

    def mark_failed(self, instance_id: str, **kwargs: Any) -> bool:
        instance = self.instances[instance_id]
        self.instances[instance_id] = replace(
            instance,
            status=WorkflowInstanceStatus.FAILED,
            current_state=str(kwargs.get("error_step") or "failed"),
            completed_at=datetime.now(timezone.utc),
            error=kwargs.get("error"),
            error_step=kwargs.get("error_step"),
        )
        return True

    def list_instance_status_dicts(
        self,
        *,
        user_id: str | None = None,
        org_id: str | None = None,
        namespace: str | None = None,
        status: str | list[str] | tuple[str, ...] | None = None,
        workflow_id: str | None = None,
        source_event_type: str | None = None,
        source_event_id: str | None = None,
        limit: int = 50,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        if isinstance(status, str):
            status_values = [status]
        elif isinstance(status, (list, tuple)):
            status_values = [str(item) for item in status]
        else:
            status_values = []

        matches: list[WorkflowInstance] = []
        for instance in self.instances.values():
            if user_id and instance.user_id != user_id:
                continue
            if org_id is not None and instance.org_id != org_id:
                continue
            if namespace and instance.namespace != namespace:
                continue
            if workflow_id and instance.workflow_id != workflow_id:
                continue
            if source_event_type and instance.source_event_type != source_event_type:
                continue
            if source_event_id and instance.source_event_id != source_event_id:
                continue
            if status_values and instance.status.value not in status_values:
                continue
            matches.append(instance)

        matches.sort(key=lambda item: item.created_at, reverse=True)
        return [instance.to_status_dict() for instance in matches[:limit]]


class _CapturingOrchestrator:
    def __init__(self, manager: _InMemoryWorkflowInstanceManager) -> None:
        self.manager = manager
        self.calls: list[dict[str, Any]] = []
        self.active_snapshots: list[list[dict[str, Any]]] = []

    def configure_execution_caps(self, **_kwargs: Any) -> None:
        return None

    def run(self, **kwargs: Any):
        self.calls.append(dict(kwargs))
        namespace = kwargs.get("user_namespace")
        self.active_snapshots.append(
            self.manager.list_instance_status_dicts(
                namespace=str(namespace) if isinstance(namespace, str) else None,
                workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
                status=WorkflowInstanceStatus.active_values(),
                limit=10,
            )
        )

        from src.backend.integrations.internal_mcp.orchestrator import (
            OrchestratorResult,
        )

        return OrchestratorResult(
            response_text="Workflow monitor response",
            extra_messages=[],
            tool_invocations=[],
            aux_llm_calls=[],
        )


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch) -> Flask:
    import src.backend.workflows.durable.registry_factory as registry_factory

    monkeypatch.setattr(registry_factory, "discover_workflow_ids", lambda: [])
    monkeypatch.setattr(
        registry_factory,
        "_launch_deferred_registry_work",
        lambda **_kwargs: None,
    )

    from src.backend.server.routes.von_routes import von_bp
    from src.backend.server.routes.workflows_routes import workflows_bp

    manager = _InMemoryWorkflowInstanceManager()
    orchestrator = _CapturingOrchestrator(manager)

    monkeypatch.setenv("VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "0")
    monkeypatch.setenv("VON_WORKFLOW_DISCOVERY_ENABLE", "0")

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#michael_witbrock",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {
            "organisation_id": "#V#sail_lab",
            "chat_session_id": "session-monitor-test",
            "role": "member",
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_buttonify_model_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._add_chat_history_message",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_context_concept_reference_metadata",
        lambda *_args, **_kwargs: {
            "source": "sent_context_user_assistant",
            "metadata_version": 1,
            "message_roles": ["user", "assistant"],
            "messages_scanned": 0,
            "concept_count": 0,
            "concept_count_capped": False,
            "max_concepts": None,
            "include_direct_supertypes": False,
            "max_direct_supertypes": 0,
            "concepts": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._finalise_llm_debug_info",
        lambda **kwargs: kwargs["llm_debug_info"],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_workflow_registry_read_only",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda *_args, **_kwargs: None,
        raising=False,
    )

    def _fake_submit_verified_workflow_instance(**kwargs: Any):
        manager_arg = kwargs["manager"]
        instance_id, created_new = manager_arg.create_instance_for_event(
            workflow_id=str(kwargs["workflow_id"]),
            user_id=str(kwargs["user_id"]),
            org_id=kwargs.get("org_id"),
            namespace=str(kwargs["namespace"]),
            event_idempotency_key=str(kwargs["event_idempotency_key"]),
            source_event_type=str(kwargs["source_event_type"]),
            source_event_id=str(kwargs["source_event_id"]),
            inputs=dict(kwargs.get("inputs") or {}),
            max_retries=int(kwargs.get("max_retries", 0) or 0),
        )
        return WorkflowInstanceSubmissionResult(
            success=True,
            workflow_id=str(kwargs["workflow_id"]),
            status="pending" if created_new else "reused",
            instance_id=instance_id,
            verification={
                "preflight_passed": True,
                "postflight_passed": True,
                "runnable_verification_success": True,
            },
            created_new=created_new,
        )

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_instance_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.submit_verified_workflow_instance",
        _fake_submit_verified_workflow_instance,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.workflows_routes._get_instance_manager",
        lambda: manager,
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.config["TESTING"] = True
    flask_app.config["PROPAGATE_EXCEPTIONS"] = True
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    flask_app.config["INTERNAL_MCP_GATEWAY"] = object()
    flask_app.register_blueprint(von_bp, url_prefix="/von")
    flask_app.register_blueprint(workflows_bp)
    flask_app.config["CONTEXT"] = []
    flask_app.config["_TEST_MANAGER"] = manager
    flask_app.config["_TEST_ORCHESTRATOR"] = orchestrator
    return flask_app


def test_generate_materialises_conversation_turn_instance_in_monitor(app: Flask) -> None:
    client = app.test_client()

    response = client.post(
        "/von/generate",
        json={"prompt": "Show the workflow monitor row for this turn."},
    )
    assert response.status_code == 200

    orchestrator = app.config["_TEST_ORCHESTRATOR"]
    assert orchestrator.calls, "expected orchestrator.run() to be called"
    assert orchestrator.active_snapshots, "expected an active workflow snapshot"
    active_items = orchestrator.active_snapshots[0]
    assert any(
        item.get("workflow_id") == CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
        and item.get("status") == "pending"
        for item in active_items
    )

    monitor_response = client.get(
        "/api/workflows/instances",
        query_string={"namespace": "#V#michael_witbrock@sail_lab"},
    )
    assert monitor_response.status_code == 200
    payload = monitor_response.get_json()
    assert payload["count"] >= 1
    turn_item = next(
        item
        for item in payload["items"]
        if item.get("workflow_id") == CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    assert turn_item["status"] == "completed"
    assert turn_item["current_state"] == "completed"

    manager = app.config["_TEST_MANAGER"]
    stored_instance = next(
        instance
        for instance in manager.instances.values()
        if instance.workflow_id == CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    assert stored_instance.inputs["conversation_session_id"] == "session-monitor-test"
    assert stored_instance.inputs["turn_id"]
