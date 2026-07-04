from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from flask import Flask

import src.backend.server.routes.von_routes as von_routes


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
        "get_task_status",
        lambda task_id: status,
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
        "get_task_status",
        lambda task_id: status,
    )

    response = app.test_client().get("/von/api/task/result/task-json-safe")

    assert response.status_code == 200
    body = response.get_json()
    assert body["result"]["allowed_write_tools"] == ["kb.write", "workflow.write"]
