import pytest
from flask import Flask


class _FailingLLM:
    def __init__(self):
        self.calls = []

    def generate(self, *_args, **_kwargs):
        self.calls.append({"called": True})
        raise AssertionError(
            "LLM should not be called for prompt introspection fast-path"
        )


class _StubGateway:
    enabled = True

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def describe_methods(self):
        # Minimal snapshot shape used by the tool inventory fast-path.
        return {
            "chat_get_prompt_context": {
                "category": "read",
                "description": "Return the effective prompt context for a namespace.",
            },
            "rag_get_status": {
                "category": "read",
                "description": "Return RAG status for a namespace.",
            },
        }

    def invoke(self, method_name, payload=None):
        from src.backend.integrations.internal_mcp.transport import TransportResult

        self.calls.append({"method_name": method_name, "payload": dict(payload or {})})
        return TransportResult(payload=self._payload, duration_ms=1.23)


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("VON_DETERMINISTIC_INTROSPECTION", "1")

    from src.backend.server.routes.von_routes import von_bp

    # Force an authenticated user for the request.
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#michael_witbrock",
    )

    # Keep user prompt loader stable.
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **kwargs: (
            [
                {
                    "concept_id": "#V#general_von_chat_prompt_for_witbrock",
                    "content": "Please be terse.",
                }
            ]
            if kwargs.get("prompt_types")
            == (
                "#V#von_chat_behaviour_prompt",
                "#V#von_chat_behavior_prompt",
                "#V#von_llm_prompt",
            )
            else []
        ),
    )

    llm = _FailingLLM()
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.config["TESTING"] = True
    flask_app.config["PROPAGATE_EXCEPTIONS"] = True
    flask_app.register_blueprint(von_bp, url_prefix="/von")
    flask_app.config["CONTEXT"] = []
    flask_app.config["_TEST_LLM"] = llm
    return flask_app


def test_prompt_introspection_fastpath_returns_prompt_text(app):
    gateway_payload = {
        "success": True,
        "namespace": "#V#michael_witbrock",
        "prompt_concept_ids": ["#V#general_von_chat_prompt_for_witbrock"],
        "prompt_concepts": [
            {
                "concept_id": "#V#general_von_chat_prompt_for_witbrock",
                "content": "Please be terse.",
            }
        ],
        "prompt_text": "Please be terse.",
        "prompt_count": 1,
    }
    gateway = _StubGateway(gateway_payload)
    app.config["INTERNAL_MCP_GATEWAY"] = gateway
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = object()

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Can you tell me what my user prompt is for chat?"},
    )
    if resp.status_code != 200:
        raise AssertionError(
            f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}"
        )

    data = resp.get_json()
    assert isinstance(data, dict)
    assert "Please be terse." in data.get("response", "")
    llm_debug = data.get("llm_debug") or {}
    assert llm_debug.get("tool_invocations"), "expected tool invocations to be recorded"
    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    assert diagnostics.get("request_id")
    assert diagnostics.get("prompt_preview", "").startswith(
        "Can you tell me what my user prompt is for chat?"
    )
    assert isinstance(diagnostics.get("progress_events"), list)
    assert isinstance(diagnostics.get("phase_history"), list)
    assert isinstance(diagnostics.get("tool_history"), list)
    assert isinstance(diagnostics.get("workflow_stage_model"), dict)
    assert isinstance(diagnostics.get("workflow_stage_path"), dict)

    meta = llm_debug.get("prompt_introspection_fastpath") or {}
    assert meta.get("enabled") is True

    assert gateway.calls, "expected gateway.invoke() to be called"
    assert gateway.calls[0]["method_name"] == "chat_get_prompt_context"


def test_prompt_introspection_fastpath_falls_back_when_gateway_disabled(app):
    class _DisabledGateway:
        enabled = False

    app.config["INTERNAL_MCP_GATEWAY"] = _DisabledGateway()
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = object()

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Can you tell me what my user prompt is for chat?"},
    )
    if resp.status_code != 200:
        raise AssertionError(
            f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}"
        )

    data = resp.get_json()
    assert isinstance(data, dict)
    assert "Please be terse." in data.get("response", "")

    llm_debug = data.get("llm_debug") or {}
    meta = llm_debug.get("prompt_introspection_fastpath") or {}
    assert meta.get("used_tool") is False
    assert meta.get("enabled") is True


def test_tool_inventory_fastpath_lists_tools(app):
    gateway = _StubGateway({"success": True})
    app.config["INTERNAL_MCP_GATEWAY"] = gateway
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = object()

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "What tools do you have?"})
    if resp.status_code != 200:
        raise AssertionError(
            f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}"
        )

    data = resp.get_json()
    assert isinstance(data, dict)
    assert "chat_get_prompt_context" in data.get("response", "")
    assert "rag_get_status" in data.get("response", "")
    assert data.get("fastpath", {}).get("bypassed_llm") is True
    assert data.get("fastpath", {}).get("name") == "tool_inventory"

    llm_debug = data.get("llm_debug") or {}
    fp = llm_debug.get("fastpath") or {}
    assert fp.get("bypassed_llm") is True
    assert fp.get("name") == "tool_inventory"


def test_rag_status_fastpath_calls_tool(app):
    gateway_payload = {
        "success": True,
        "namespace": "#V#michael_witbrock",
        "status": "ok",
    }
    gateway = _StubGateway(gateway_payload)
    app.config["INTERNAL_MCP_GATEWAY"] = gateway
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = object()

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "What is my RAG status?"})
    if resp.status_code != 200:
        raise AssertionError(
            f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}"
        )

    data = resp.get_json()
    assert isinstance(data, dict)
    assert "RAG status" in data.get("response", "")

    assert gateway.calls, "expected gateway.invoke() to be called"
    assert gateway.calls[0]["method_name"] == "rag_get_status"


def test_prompt_introspection_fastpath_disabled_by_default_does_not_trigger(
    monkeypatch,
):
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.delenv("VON_DETERMINISTIC_INTROSPECTION", raising=False)
    monkeypatch.setenv("VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "0")

    # Force an authenticated user for the request.
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#michael_witbrock",
    )

    # Keep user prompt loader stable.
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **kwargs: (
            [
                {
                    "concept_id": "#V#general_von_chat_prompt_for_witbrock",
                    "content": "Please be terse.",
                }
            ]
            if kwargs.get("prompt_types")
            == (
                "#V#von_chat_behaviour_prompt",
                "#V#von_chat_behavior_prompt",
                "#V#von_llm_prompt",
            )
            else []
        ),
    )

    class _NonFailingLLM:
        def __init__(self):
            self.calls = []

        def generate(self, *_args, **_kwargs):
            self.calls.append({"called": True})
            return "normal path response"

    llm = _NonFailingLLM()
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )

    class _FailingGateway:
        enabled = True

        def invoke(self, *_args, **_kwargs):
            raise AssertionError("Fast-path should not call gateway when disabled")

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.config["TESTING"] = True
    flask_app.config["PROPAGATE_EXCEPTIONS"] = True
    flask_app.register_blueprint(von_bp, url_prefix="/von")
    flask_app.config["CONTEXT"] = []
    flask_app.config["INTERNAL_MCP_GATEWAY"] = _FailingGateway()
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = None

    client = flask_app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Can you tell me what my user prompt is for chat?"},
    )
    if resp.status_code != 200:
        raise AssertionError(
            f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}"
        )

    data = resp.get_json()
    assert isinstance(data, dict)
    assert "normal path response" in data.get("response", "")

    llm_debug = data.get("llm_debug") or {}
    assert "prompt_introspection_fastpath" not in llm_debug


def test_rag_counts_fastpath_bypasses_llm_and_separates_chat_vs_ka(app, monkeypatch):
    # This fast-path is intentionally NOT gated by VON_DETERMINISTIC_INTROSPECTION,
    # because it prevents KA vs chat-history confusion for factual status questions.

    gateway_payload = {
        "success": True,
        "namespace": "#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        "indexed": 10,  # KA interaction sessions
        "chat_history_sessions": 42,
        "chat_history_sessions_in_namespace": 42,
        "chat_history_session_details": [
            {
                "session_id": "s1",
                "messages_missing_index": 0,
                "rag_indexed_success": 3,
                "rag_indexed_failed": 0,
            },
            {
                "session_id": "s2",
                "messages_missing_index": 0,
                "rag_indexed_success": 5,
                "rag_indexed_failed": 0,
            },
        ],
    }

    # Even if deterministic introspection is disabled, this path should run.
    monkeypatch.delenv("VON_DETERMINISTIC_INTROSPECTION", raising=False)

    gateway = _StubGateway(gateway_payload)
    app.config["INTERNAL_MCP_GATEWAY"] = gateway
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = object()

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["namespace"] = "#V#michael_witbrock@university_of_auckland_strong_ai_lab"

    resp = client.post(
        "/von/generate",
        json={"prompt": "How many indexed chat sessions can you see?"},
    )
    if resp.status_code != 200:
        raise AssertionError(
            f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}"
        )

    data = resp.get_json() or {}
    assert data.get("fastpath", {}).get("name") == "rag_counts"
    assert data.get("fastpath", {}).get("bypassed_llm") is True

    text = data.get("response", "")
    assert "Chat history sessions" in text
    assert "KA interaction sessions" in text
    assert "42" in text
    assert "10" in text

    assert gateway.calls, "expected gateway.invoke() to be called"
    assert gateway.calls[0]["method_name"] == "rag_get_status"
    assert (
        gateway.calls[0]["payload"].get("namespace")
        == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )
    assert gateway.calls[0]["payload"].get("detail") == 1
