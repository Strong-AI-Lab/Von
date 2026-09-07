"""Route tests for the explicit actor-bound task execution launch."""

from __future__ import annotations

from unittest.mock import MagicMock

from flask import Flask

from src.backend.db import mongo_client
from src.backend.server.routes.task_routes import task_bp
from src.backend.services import task_execution_service


def _build_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    app.register_blueprint(task_bp, url_prefix="/api/tasks")
    return app.test_client()


def _task(
    *,
    creator: str = "#V#alice",
    assignee: str = "#V#von_system",
    status: str = "pending",
) -> dict:
    return {
        "task_concept_id": "#V#task_prepare_brief",
        "title": "Prepare brief",
        "description": "Prepare and read back the research brief.",
        "status": status,
        "assignee_concept_id": assignee,
        "created_by_concept_id": creator,
        "organisation_concept_id": "#V#research_lab",
        "originating_conversation_id": "#V#conversation_1",
        "conversation_session_id": "session-1",
        "conversation_name": "Research planning",
    }


def _conversation() -> dict:
    return {
        "conversation_concept_id": "#V#conversation_1",
        "session_id": "session-1",
        "name": "Research planning",
        "owner_concept_id": "#V#alice",
        "organisation_concept_id": "#V#research_lab",
        "namespace": "#V#alice@research_lab",
    }


def _set_actor(client, *, include_org: bool = True) -> None:
    with client.session_transaction() as session:
        session["user_concept_id"] = "#V#alice"
        if include_org:
            session["org_id"] = "#V#research_lab"


def test_execute_with_von_creates_one_linked_server_dispatch(monkeypatch) -> None:
    client = _build_client()
    _set_actor(client)
    queue_call: dict = {}
    reconciliation_call: dict = {}

    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_task",
        lambda _task_id: {
            **_task(),
            "current_work_product": {
                "status": "ready",
                "concept_id": "#V#brief_selected",
            },
        },
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_conversation_concept",
        lambda _conversation_id: _conversation(),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )

    def _create_queue_record(**kwargs):
        queue_call.update(kwargs)
        return {
            "queue_id": "queue-1",
            "status": "queued",
            "session_id": kwargs["session_id"],
            "session_name": kwargs["session_name"],
            "enqueue_submission_id": kwargs["enqueue_submission_id"],
            "task_concept_id": kwargs["task_concept_id"],
            "task_execution_concept_id": kwargs["task_execution_concept_id"],
            "idempotent_replay": False,
            "dispatch_ready": kwargs["server_dispatch_ready"],
        }

    def _reconcile_execution(record):
        reconciliation_call.update(record)
        return {
            "task_execution_concept_id": record["task_execution_concept_id"],
            "task_concept_id": "#V#task_prepare_brief",
            "status": "pending",
            "queue_id": record["queue_id"],
            "enqueue_submission_id": record["enqueue_submission_id"],
        }

    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_prompt_queue_service.create_queue_record",
        _create_queue_record,
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.reconcile_task_execution_queue_record",
        _reconcile_execution,
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_prompt_queue_service.activate_server_dispatch_record",
        lambda **_kwargs: {
            **_create_queue_record(**queue_call),
            "dispatch_ready": True,
        },
    )
    wake_dispatcher = MagicMock()
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.wake_chat_prompt_queue_dispatcher",
        wake_dispatcher,
    )

    response = client.post(
        "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
        json={
            "launch_request_id": "launch-1",
            "continuation_instruction": "Check the new evidence",
            "current_work_product": {"concept_id": "#V#wrong_brief"},
        },
    )

    assert "Check the new evidence" in queue_call["prompt_raw"]
    assert "#V#brief_selected" in queue_call["prompt_raw"]
    assert "#V#wrong_brief" not in queue_call["prompt_raw"]
    assert response.status_code == 201
    assert response.get_json()["task_execution"]["queue_id"] == "queue-1"
    assert reconciliation_call["user_concept_id"] == "#V#alice"
    assert reconciliation_call["organisation_concept_id"] == "#V#research_lab"
    assert reconciliation_call["session_id"] == "session-1"
    assert queue_call["source"] == "von_task"
    assert queue_call["dispatch_mode"] == "server"
    assert queue_call["server_dispatch_ready"] is False
    assert queue_call["execution_envelope"]["turn_kind"] == "user_message"
    assert (
        queue_call["execution_envelope"]["workflow_inputs"]["authority_namespace"]
        == "#V#alice@research_lab"
    )
    assert queue_call["task_concept_id"] == "#V#task_prepare_brief"
    assert "client_request_id" not in queue_call
    wake_dispatcher.assert_called_once_with()


def test_execute_with_von_replays_an_already_linked_launch_without_rebinding(
    monkeypatch,
) -> None:
    client = _build_client()
    _set_actor(client)
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_task",
        lambda _task_id: _task(),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_conversation_concept",
        lambda _conversation_id: _conversation(),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )
    execution_id = "#V#task_execution_replayed"
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_prompt_queue_service.create_queue_record",
        lambda **kwargs: {
            "queue_id": "queue-original",
            "status": "in_progress",
            "dispatch_mode": "server",
            "dispatch_ready": True,
            "enqueue_submission_id": kwargs["enqueue_submission_id"],
            "task_concept_id": "#V#task_prepare_brief",
            "task_execution_concept_id": kwargs["task_execution_concept_id"],
            "idempotent_replay": True,
        },
    )
    reconcile = MagicMock(
        return_value={
            "task_execution_concept_id": execution_id,
            "task_concept_id": "#V#task_prepare_brief",
            "status": "in_progress",
            "queue_id": "queue-original",
        }
    )
    activate = MagicMock()
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.reconcile_task_execution_queue_record",
        reconcile,
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_prompt_queue_service.activate_server_dispatch_record",
        activate,
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.wake_chat_prompt_queue_dispatcher",
        MagicMock(),
    )

    response = client.post(
        "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
        json={"launch_request_id": "launch-replayed"},
    )

    assert response.status_code == 200
    assert response.get_json()["idempotent_replay"] is True
    reconcile.assert_called_once()
    activate.assert_not_called()


def test_execute_with_von_requires_authenticated_actor(monkeypatch) -> None:
    client = _build_client()
    get_task = MagicMock()
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_task",
        get_task,
    )

    response = client.post(
        "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
        json={"launch_request_id": "launch-1"},
    )

    assert response.status_code == 401
    assert response.get_json()["reason_code"] == "not_authenticated"
    get_task.assert_not_called()


def test_execute_with_von_rejects_legacy_identity_header_actor(monkeypatch) -> None:
    client = _build_client()
    get_task = MagicMock()
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id_with_source",
        lambda: ("#V#alice", "legacy_identity_header"),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_task",
        get_task,
    )

    response = client.post(
        "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
        json={"launch_request_id": "launch-1"},
        headers={"X-User-Concept-ID": "#V#alice"},
    )

    assert response.status_code == 401
    assert response.get_json()["reason_code"] == "not_authenticated"
    get_task.assert_not_called()


def test_execute_with_von_deliberately_requires_active_org_scope(monkeypatch) -> None:
    client = _build_client()
    _set_actor(client, include_org=False)
    get_task = MagicMock()
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_task",
        get_task,
    )

    response = client.post(
        "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
        json={"launch_request_id": "launch-1"},
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "organisation_scope_required"
    get_task.assert_not_called()


def test_execute_with_von_rejects_non_creator_even_when_task_is_visible(
    monkeypatch,
) -> None:
    client = _build_client()
    _set_actor(client)
    create_queue = MagicMock()
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_task",
        lambda _task_id: _task(creator="#V#bob"),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_prompt_queue_service.create_queue_record",
        create_queue,
    )

    response = client.post(
        "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
        json={"launch_request_id": "launch-1"},
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "task_creator_mismatch"
    create_queue.assert_not_called()


def test_execute_with_von_rejects_terminal_task(monkeypatch) -> None:
    client = _build_client()
    _set_actor(client)
    create_queue = MagicMock()
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_task",
        lambda _task_id: _task(status="completed"),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_prompt_queue_service.create_queue_record",
        create_queue,
    )

    response = client.post(
        "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
        json={"launch_request_id": "launch-1"},
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "task_not_executable"
    create_queue.assert_not_called()


def test_execute_with_von_requires_client_launch_identity(monkeypatch) -> None:
    client = _build_client()
    _set_actor(client)
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_task",
        lambda _task_id: _task(),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_conversation_concept",
        lambda _conversation_id: _conversation(),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )

    response = client.post(
        "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
        json={},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "launch_request_id is required"


def test_task_execution_lifecycle_is_not_publicly_browser_mutable() -> None:
    client = _build_client()
    _set_actor(client)

    for suffix in ("in-progress", "terminal", "terminalise-task"):
        response = client.post(
            f"/api/tasks/executions/%23V%23task_execution_1/{suffix}",
            json={},
        )
        assert response.status_code == 404


def test_durable_queue_is_not_relabelled_failed_when_execution_binding_needs_reconciliation(
    monkeypatch,
) -> None:
    client = _build_client()
    _set_actor(client)
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_task",
        lambda _task_id: _task(),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_conversation_concept",
        lambda _conversation_id: _conversation(),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )

    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_prompt_queue_service.create_queue_record",
        lambda **kwargs: {
            "queue_id": "queue-durable",
            "status": "queued",
            "dispatch_ready": False,
            "task_concept_id": kwargs["task_concept_id"],
            "task_execution_concept_id": kwargs["task_execution_concept_id"],
            "enqueue_submission_id": kwargs["enqueue_submission_id"],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.reconcile_task_execution_queue_record",
        MagicMock(side_effect=RuntimeError("execution binding must be reconciled")),
    )
    wake = MagicMock()
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.wake_chat_prompt_queue_dispatcher",
        wake,
    )

    response = client.post(
        "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
        json={"launch_request_id": "launch-bind-reconcile"},
    )

    assert response.status_code == 202
    assert response.get_json()["reconciliation_pending"] is True
    assert response.get_json()["queue_item"]["status"] == "queued"
    wake.assert_called_once_with()


def test_execute_with_von_end_to_end_persists_one_queue_and_execution_attempt(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_task_execute_with_von_end_to_end")
    mongo_client.close_connection()
    client = _build_client()
    _set_actor(client)
    task = _task()
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_task",
        lambda _task_id: dict(task),
    )
    monkeypatch.setattr(
        "src.backend.services.task_management_service.get_task",
        lambda _task_id: dict(task),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.get_conversation_concept",
        lambda _conversation_id: _conversation(),
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        task_execution_service,
        "_persist_singleton_text",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.task_execution_submission_service.wake_chat_prompt_queue_dispatcher",
        lambda: None,
    )

    try:
        first = client.post(
            "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
            json={"launch_request_id": "launch-end-to-end"},
        )
        replay = client.post(
            "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
            json={"launch_request_id": "launch-end-to-end"},
        )
        duplicate = client.post(
            "/api/tasks/%23V%23task_prepare_brief/execute-with-von",
            json={"launch_request_id": "launch-distinct"},
        )

        assert first.status_code == 201
        assert replay.status_code == 200
        assert replay.get_json()["idempotent_replay"] is True
        assert duplicate.status_code == 409
        assert duplicate.get_json()["reason_code"] == ("task_execution_already_active")
        first_payload = first.get_json()
        assert first_payload["queue_item"]["dispatch_ready"] is True
        assert first_payload["task_execution"]["queue_id"] == (
            first_payload["queue_item"]["queue_id"]
        )

        # A schedule uses the same atomic active-task key as the UI, even
        # though its short launch workflow has a different instance identity.
        from types import SimpleNamespace
        from src.backend.security.access_control import override_current_actor
        from src.backend.workflows.action_registry import (
            WorkflowActionRequest,
            WorkflowEnvironment,
        )
        from src.backend.workflows.durable.task_execution_actions import (
            submit_task_execution_action,
        )

        monkeypatch.setattr(
            "src.backend.languagemodels.llm_interface.assert_model_execution_allowed",
            lambda **_kwargs: {"allowed": True},
        )
        with override_current_actor("#V#alice", "#V#research_lab"):
            scheduled = submit_task_execution_action(
                WorkflowActionRequest(
                    action_id="task.submit_execution",
                    inputs={
                        "task_concept_id": task["task_concept_id"],
                        "model": "gpt-5.6-luna",
                        "model_provider": "openai",
                    },
                    data={},
                    environment=WorkflowEnvironment(llm_client=None),
                    trace=SimpleNamespace(instance_id="scheduled-encounter-1"),
                )
            )
        assert scheduled.status == "success"
        assert scheduled.outputs["task_launch_status"] == "already_active"
        assert scheduled.outputs["queue_id"] == first_payload["queue_item"]["queue_id"]
        assert scheduled.outputs["domain_completion_claim"] is False

        queue = mongo_client.get_chat_prompt_queue_collection()
        assert queue is not None
        assert queue.count_documents({"task_concept_id": task["task_concept_id"]}) == 1
        executions = mongo_client.get_db()["concepts"]
        assert (
            executions.count_documents(
                {
                    "relationships.is_an_instance_of": (
                        task_execution_service.TASK_EXECUTION_TYPE_ID
                    )
                }
            )
            == 1
        )
    finally:
        mongo_client.close_connection()


def test_continuation_projects_selected_task_product_and_new_instruction():
    from src.backend.services.task_execution_submission_service import (
        _build_task_execution_prompt,
    )

    selected = _task()
    selected.update(
        current_work_product={"status": "ready", "concept_id": "#V#selected_brief"},
        continuation_instruction="Resolve the remaining discrepancy.",
        next_checkpoint="Check the missing evidence",
        evidence="Canonical receipt",
    )
    prompt = _build_task_execution_prompt(selected)
    assert "#V#task_prepare_brief" in prompt
    assert "#V#selected_brief" in prompt
    assert "Resolve the remaining discrepancy." in prompt
    assert "Check the missing evidence" in prompt
    assert "Canonical receipt" in prompt
