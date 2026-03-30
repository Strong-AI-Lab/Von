import pytest
from flask import Flask

from src.backend.integrations.internal_mcp import ToolCallParsingError


class _DummyLLM:
    def generate(self, *_args, **_kwargs):
        raise AssertionError("generate should not be called when parsing fails")


class _FailingOrchestrator:
    def run(self, *_args, **_kwargs):
        raise ToolCallParsingError(
            "Tool call was not executed: invalid JSON in tool call response.",
            raw_response='{"action":"call_tool"} trailing',
        )


@pytest.fixture()
def app(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: _DummyLLM(),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(von_bp, url_prefix="/von")

    flask_app.config["CONTEXT"] = []
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = _FailingOrchestrator()
    flask_app.config["INTERNAL_MCP_GATEWAY"] = None

    return flask_app


def test_llm_debug_contains_rejected_tool_payload(app):
    client = app.test_client()

    resp = client.post("/von/generate", json={"prompt": "please run tool"})
    assert resp.status_code == 200

    body = resp.get_json()
    assert body["response"].startswith(
        "Tool call was not executed due to an MCP serialisation error."
    )

    llm_debug = body["llm_debug"]
    invocations = llm_debug["tool_invocations"]
    assert invocations, "expected parse error to be recorded in tool invocations"

    invocation = invocations[0]
    assert invocation["method"] == "__tool_call_parse_error__"
    assert (
        invocation["arguments"].get("raw_tool_call")
        == '{"action":"call_tool"} trailing'
    )
    assert "invalid JSON in tool call response" in invocation["error"]

    warnings = llm_debug.get("warnings")
    assert isinstance(warnings, list)
    assert any(
        "Tool call was not executed" in str(w) or "invalid JSON" in str(w)
        for w in warnings
    )
