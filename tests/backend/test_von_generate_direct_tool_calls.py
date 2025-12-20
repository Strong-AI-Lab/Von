import json
from datetime import datetime

import pytest
from flask import Flask


class _DummyLLM:
    def generate(self, *_args, **_kwargs):
        raise AssertionError(
            "LLM generate() should not be called for direct tool calls"
        )


class _StubResult:
    def __init__(self, payload, duration_ms=12):
        self.payload = payload
        self.duration_ms = duration_ms


class _StubGateway:
    def __init__(self):
        self.invoked = []

    def describe_methods(self):
        return {
            "jira_get_myself": {"category": "read"},
            "jira_get_issue": {"category": "read"},
            "jira_create_issue": {"category": "write"},
        }

    def invoke(self, tool_name, payload):
        self.invoked.append((tool_name, dict(payload)))
        if tool_name == "jira_get_myself":
            return _StubResult({"accountId": "abc123"}, duration_ms=7)
        raise RuntimeError(f"Unexpected tool: {tool_name}")


class _StubOrchestrator:
    def _extract_json_blob(self, text: str):
        return json.loads(text)

    def _format_tool_result(
        self, tool_name, payload, duration_ms, status, error_message=None
    ):
        result = {
            "tool": tool_name,
            "status": status,
            "duration_ms": duration_ms,
            "payload": payload,
        }
        if error_message:
            result["error"] = error_message
        return json.dumps(result)


@pytest.fixture()
def app(monkeypatch):
    # Import inside fixture so monkeypatch works reliably
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setenv("VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "1")

    # Avoid pulling in real model clients
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: _DummyLLM(),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda: "test-model",
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(von_bp, url_prefix="/von")

    flask_app.config["CONTEXT"] = []
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator()
    flask_app.config["INTERNAL_MCP_GATEWAY"] = _StubGateway()

    return flask_app


def test_generate_executes_direct_read_tool_call(app):
    client = app.test_client()

    tool_call = {
        "action": "call_tool",
        "tool": "jira_get_myself",
        "payload": {},
    }

    resp = client.post("/von/generate", json={"prompt": json.dumps(tool_call)})
    assert resp.status_code == 200

    body = resp.get_json()
    assert "llm_debug" in body
    assert "interaction_timestamp_utc" in body["llm_debug"]
    datetime.fromisoformat(
        body["llm_debug"]["interaction_timestamp_utc"].replace("Z", "+00:00")
    )
    assert body["llm_debug"]["tool_invocations"], "expected a recorded tool invocation"

    invocation = body["llm_debug"]["tool_invocations"][0]
    assert invocation["tool"] == "jira_get_myself"
    assert invocation.get("direct_user_call") is True

    # Response should be the tool-result JSON, not an LLM completion.
    tool_result = json.loads(body["response"])
    assert tool_result["tool"] == "jira_get_myself"
    assert tool_result["status"] == "ok"
    assert tool_result["payload"]["accountId"] == "abc123"


def test_generate_rejects_direct_write_tool_call(app):
    client = app.test_client()

    tool_call = {
        "action": "call_tool",
        "tool": "jira_create_issue",
        "payload": {"summary": "nope"},
    }

    resp = client.post("/von/generate", json={"prompt": json.dumps(tool_call)})
    assert resp.status_code == 200

    body = resp.get_json()
    assert "restricted to read-only tools" in body["response"]
    assert "interaction_timestamp_utc" in body["llm_debug"]
    datetime.fromisoformat(
        body["llm_debug"]["interaction_timestamp_utc"].replace("Z", "+00:00")
    )
    assert body["llm_debug"]["tool_invocations"] == []
