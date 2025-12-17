import pytest
from flask import Flask


class _CapturingLLM:
    def __init__(self):
        self.calls = []

    def generate(self, prompt, context=None, model=None):
        self.calls.append({"prompt": prompt, "context": context, "model": model})
        return "ok"


class _CapturingOrchestrator:
    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)

        from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

        return OrchestratorResult(response_text="ok", extra_messages=[], tool_invocations=[])


@pytest.fixture()
def app(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setenv("VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "0")

    # Force an authenticated user for the request.
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#michael_witbrock",
    )

    # Stub auxiliary prompt builder.
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.build_user_specific_system_prompt",
        lambda _user_id: "Please be terse.",
    )

    llm = _CapturingLLM()
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda: "test-model",
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.config["TESTING"] = True
    flask_app.config["PROPAGATE_EXCEPTIONS"] = True
    flask_app.register_blueprint(von_bp, url_prefix="/von")

    flask_app.config["CONTEXT"] = []
    flask_app.config["_TEST_LLM"] = llm

    return flask_app


def test_generate_passes_auxiliary_prompt_to_orchestrator(app):
    orchestrator = _CapturingOrchestrator()
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = object()

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})
    if resp.status_code != 200:
        raise AssertionError(f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}")

    assert orchestrator.calls, "expected orchestrator.run() to be called"
    call = orchestrator.calls[0]
    assert call.get("auxiliary_system_prompt") == "Please be terse."


def test_generate_includes_auxiliary_prompt_without_orchestrator(app):
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    app.config["INTERNAL_MCP_GATEWAY"] = None

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})
    if resp.status_code != 200:
        raise AssertionError(f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}")

    llm = app.config["_TEST_LLM"]
    assert llm.calls, "expected llm.generate() to be called"

    context = llm.calls[0]["context"]
    assert context and context[0]["role"] == "system"

    system_texts = [m["content"] for m in context if m.get("role") == "system"]
    assert any("USER-SPECIFIC SYSTEM PROMPT" in text for text in system_texts)
    assert any("Please be terse." in text for text in system_texts)
