import json

from flask import Flask


def _build_test_client(monkeypatch, tmp_path):
    from src.backend.server.routes import von_routes

    export_path = tmp_path / "diagnostic_latest.json"
    monkeypatch.setattr(von_routes, "_DIAGNOSTIC_EXPORT_FILE_PATH", export_path)

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    return app.test_client(), export_path


def test_diagnostics_export_writes_sanitised_file(monkeypatch, tmp_path):
    client, export_path = _build_test_client(monkeypatch, tmp_path)

    response = client.post(
        "/von/diagnostics/export",
        json={
            "schema_version": "diagnostic_export_request.v1",
            "diagnostics": {
                "active_thinking": {
                    "request_id": "req-1",
                    "prompt_preview": "This should be redacted",
                    "progress_events": [{"status": "thinking", "message": "event body"}],
                },
                "latest_turn_debug": {
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "raw message body"}],
                    "tool_invocations": [
                        {
                            "method": "search_knowledge_base",
                            "arguments": {"query": "very sensitive text", "limit": 5},
                        }
                    ],
                    "search_evidence": [
                        {
                            "tool": "search_concepts",
                            "arguments": {"query": "bathroom closet"},
                            "result": {
                                "results": [
                                    {
                                        "concept_id": "#V#bathroom_closet",
                                        "name": "Bathroom Closet",
                                    }
                                ]
                            },
                        }
                    ],
                    "api_token": "abc123",
                    "llm_interaction": {"token_count": 42},
                },
            },
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("success") is True
    assert body.get("path") == "data/diagnostic_latest.json"
    assert export_path.exists() is True

    file_payload = json.loads(export_path.read_text(encoding="utf-8"))
    diagnostics = file_payload.get("diagnostics")
    assert isinstance(diagnostics, dict)

    active = diagnostics.get("diagnostics", {}).get("active_thinking")
    assert active.get("prompt_preview") == "[redacted]"

    latest = diagnostics.get("diagnostics", {}).get("latest_turn_debug")
    assert latest.get("messages") == "[redacted]"
    assert latest.get("api_token") == "[redacted]"
    assert latest.get("llm_interaction", {}).get("token_count") == 42

    tool_invocations = latest.get("tool_invocations")
    assert isinstance(tool_invocations, list)
    arguments_summary = tool_invocations[0].get("arguments")
    assert arguments_summary == {
        "summary": "[summarised]",
        "type": "object",
        "key_count": 2,
        "keys": ["limit", "query"],
    }
    assert latest.get("search_evidence") == {
        "summary": "[summarised]",
        "type": "array",
        "item_count": 1,
    }


def test_diagnostics_export_rejects_non_object_payload(monkeypatch, tmp_path):
    client, export_path = _build_test_client(monkeypatch, tmp_path)

    response = client.post("/von/diagnostics/export", json=["not", "an", "object"])

    assert response.status_code == 400
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("success") is False
    assert body.get("error") == "invalid_payload"
    assert export_path.exists() is False
