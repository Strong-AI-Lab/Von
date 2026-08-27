from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, g, jsonify

import src.backend.server.routes.von_routes as von_routes
from src.backend.services.request_progress_service import CancellationRequested


@dataclass
class _DebugRecord:
    allowed_write_tools: set[str]
    created_at: datetime
    path: Path


def test_json_safe_response_payload_converts_python_native_debug_values() -> None:
    payload: dict[str, object] = {
        "allowed_write_tools": {"z.tool", "a.tool"},
        "debug_record": _DebugRecord(
            allowed_write_tools={"b.tool", "a.tool"},
            created_at=datetime(2026, 7, 4, tzinfo=timezone.utc),
            path=Path("data/arxiv_cache"),
        ),
    }
    payload["self"] = payload

    safe = von_routes._json_safe_response_payload(payload)

    assert safe["allowed_write_tools"] == ["a.tool", "z.tool"]
    assert safe["debug_record"]["allowed_write_tools"] == ["a.tool", "b.tool"]
    assert safe["debug_record"]["created_at"] == "2026-07-04T00:00:00+00:00"
    assert safe["debug_record"]["path"] == "data/arxiv_cache"
    assert safe["self"] == "<circular_ref>"
    json.dumps(safe)


def test_turn_admission_release_retries_the_original_terminal_disposition(
    monkeypatch,
) -> None:
    app = Flask(__name__)
    attempted_statuses: list[str] = []
    token = SimpleNamespace(
        released=False,
        queue_id="queue-1",
        client_request_id="request-1",
        pending_terminal_status=None,
        pending_terminal_error=None,
    )

    def _flaky_release(_token, *, status, error=None):
        attempted_statuses.append(status)
        if len(attempted_statuses) == 1:
            raise RuntimeError("unknown write acknowledgement")
        _token.released = True

    monkeypatch.setattr(
        von_routes.conversation_turn_admission_service,
        "release",
        _flaky_release,
    )

    with app.test_request_context("/von/generate"):
        setattr(g, von_routes._TURN_ADMISSION_CONTEXT_KEY, token)
        response = jsonify(
            {"success": False, "terminal_status": "cancelled", "error": "stopped"}
        )
        von_routes._release_request_turn_admission(response=response)
        von_routes._release_request_turn_admission(
            exception=RuntimeError("conflicting teardown observation")
        )

    assert attempted_statuses == ["cancelled", "cancelled"]
    assert token.pending_terminal_error == "stopped"


def test_turn_admission_maps_cancellation_exception_to_cancelled(monkeypatch) -> None:
    app = Flask(__name__)
    disposition: dict[str, object] = {}
    token = SimpleNamespace(
        released=False,
        queue_id="queue-1",
        client_request_id="request-1",
        pending_terminal_status=None,
        pending_terminal_error=None,
    )

    def _capture_release(_token, *, status, error=None):
        disposition.update(status=status, error=error)
        _token.released = True

    monkeypatch.setattr(
        von_routes.conversation_turn_admission_service,
        "release",
        _capture_release,
    )

    with app.test_request_context("/von/generate"):
        setattr(g, von_routes._TURN_ADMISSION_CONTEXT_KEY, token)
        von_routes._release_request_turn_admission(
            exception=CancellationRequested(task_id="request-1")
        )

    assert disposition["status"] == "cancelled"


def test_task_status_endpoint_jsonifies_set_payloads(monkeypatch) -> None:
    app = Flask(__name__)
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")

    status = SimpleNamespace(
        status="completed",
        to_dict=lambda: {
            "task_id": "task-json-safe",
            "status": "completed",
            "progress": {"allowed_write_tools": {"workflow.write", "kb.write"}},
        },
    )

    monkeypatch.setattr(
        von_routes.background_task_registry,
        "get_task_status_for_scope",
        lambda task_id, **_kwargs: status,
    )
    monkeypatch.setattr(
        von_routes,
        "_get_current_chat_prompt_queue_scope",
        lambda: {
            "user_concept_id": "#V#test_user",
            "organisation_concept_id": None,
            "namespace": "#V#test_user",
        },
    )

    response = app.test_client().get("/von/api/task/status/task-json-safe")

    assert response.status_code == 200
    body = response.get_json()
    assert body["progress"]["allowed_write_tools"] == ["kb.write", "workflow.write"]


def test_task_result_endpoint_jsonifies_generic_set_payloads(monkeypatch) -> None:
    app = Flask(__name__)
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")

    status = SimpleNamespace(
        status="completed",
        result={"allowed_write_tools": {"workflow.write", "kb.write"}},
    )

    monkeypatch.setattr(
        von_routes.background_task_registry,
        "get_task_status_for_scope",
        lambda task_id, **_kwargs: status,
    )
    monkeypatch.setattr(
        von_routes,
        "_get_current_chat_prompt_queue_scope",
        lambda: {
            "user_concept_id": "#V#test_user",
            "organisation_concept_id": None,
            "namespace": "#V#test_user",
        },
    )

    response = app.test_client().get("/von/api/task/result/task-json-safe")

    assert response.status_code == 200
    body = response.get_json()
    assert body["result"]["allowed_write_tools"] == ["kb.write", "workflow.write"]


def test_task_cancellation_terminalises_its_still_queued_prompt(monkeypatch) -> None:
    app = Flask(__name__)
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    scope = {
        "user_concept_id": "#V#test_user",
        "organisation_concept_id": "#V#test_org",
        "namespace": "#V#test_user@test_org",
    }
    status = SimpleNamespace(status="pending", queue_id="queue-1")
    cancelled: list[tuple[dict[str, object], str]] = []

    monkeypatch.setattr(
        von_routes,
        "_get_current_chat_prompt_queue_scope",
        lambda: scope,
    )
    monkeypatch.setattr(
        von_routes,
        "_get_background_task_status_for_scope",
        lambda _task_id, _scope: status,
    )
    monkeypatch.setattr(
        von_routes.background_task_registry,
        "request_cancellation_for_scope",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        von_routes.chat_prompt_queue_service,
        "cancel_prompt_record",
        lambda *, scope, queue_id: cancelled.append((scope, queue_id)),
    )

    response = app.test_client().post("/von/api/task/cancel/task-1")

    assert response.status_code == 200
    assert cancelled == [(scope, "queue-1")]
