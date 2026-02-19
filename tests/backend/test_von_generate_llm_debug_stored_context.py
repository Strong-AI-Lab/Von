import pytest
from flask import Flask


class _StubLLM:
    def __init__(self):
        self.calls = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        return "ok"


@pytest.fixture()
def app(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    llm = _StubLLM()

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda: "test-model",
    )

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )

    # Avoid touching the database in this test.
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "ok"},
        ],
    )

    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **_kwargs: [],
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(von_bp, url_prefix="/von")

    flask_app.config["CONTEXT"] = []
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    flask_app.config["INTERNAL_MCP_GATEWAY"] = None

    return flask_app


def test_generate_debug_stored_context_uses_persisted_history_for_authenticated_user(
    app,
):
    client = app.test_client()

    resp = client.post("/von/generate", json={"prompt": "Hello"})
    assert resp.status_code == 200

    body = resp.get_json()
    llm_debug = body["llm_debug"]

    # Previously this was empty because it always reported app.config["CONTEXT"],
    # which is only used for unauthenticated sessions.
    stored = llm_debug["context_stats"]["stored_context"]
    assert stored["total_messages"] >= 2
    assert stored["by_role"]["user"] >= 1
    assert stored["by_role"]["assistant"] >= 1

    internal_mcp = llm_debug["internal_mcp"]
    assert internal_mcp["gateway_present"] is False
    assert internal_mcp["gateway_enabled"] is False
    assert internal_mcp["orchestrator_present"] is False

    diagnostics = llm_debug.get("turn_execution_diagnostics")
    assert isinstance(diagnostics, dict)
    assert diagnostics.get("request_id") == body.get("request_id")
    assert diagnostics.get("prompt_preview") == "Hello"
    assert isinstance(diagnostics.get("progress_events"), list)
    assert isinstance(diagnostics.get("phase_history"), list)
    assert isinstance(diagnostics.get("tool_history"), list)

    turn_execution_record = llm_debug.get("turn_execution_record")
    assert isinstance(turn_execution_record, dict)
    assert turn_execution_record.get("schema_version") == "turn_execution_record.v1"
    completion_gate = turn_execution_record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert isinstance(completion_gate.get("decision"), str)
