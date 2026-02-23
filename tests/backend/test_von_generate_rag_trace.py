import json

import pytest
from flask import Flask


class _DummyLLM:
    def generate(self, *_args, **_kwargs):
        raise AssertionError("LLM generate() should not be called in these tests")


class _StubOrchestratorResult:
    def __init__(
        self, response_text, extra_messages, tool_invocations, aux_llm_calls=()
    ):
        self.response_text = response_text
        self.extra_messages = tuple(extra_messages)
        self.tool_invocations = tuple(tool_invocations)
        self.aux_llm_calls = tuple(aux_llm_calls)


class _StubOrchestrator:
    def _extract_json_blob(self, text: str):
        try:
            return json.loads(text)
        except Exception:
            return None

    def run(self, **_kwargs):
        return _StubOrchestratorResult(
            response_text="ok",
            extra_messages=[
                {
                    "role": "tool",
                    "content": json.dumps({"tool": "search_knowledge_base"}),
                }
            ],
            tool_invocations=[
                {"tool": "search_knowledge_base", "payload": {"query": "x"}}
            ],
        )


class _StubOrchestratorNoTools:
    def _extract_json_blob(self, text: str):
        try:
            return json.loads(text)
        except Exception:
            return None

    def run(self, **_kwargs):
        return _StubOrchestratorResult(
            response_text="ok",
            extra_messages=[],
            tool_invocations=[],
        )


class _StubGateway:
    enabled = True

    def describe_methods(self):
        return {"search_knowledge_base": {"category": "read"}}


@pytest.fixture()
def app(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

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


def test_generate_includes_rag_trace_when_authenticated(app):
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user"
        sess["namespace"] = "#V#user@org"
        sess["session_id"] = "test-session"

    resp = client.post("/von/generate", json={"prompt": "What is in my RAG store?"})
    assert resp.status_code == 200

    body = resp.get_json()
    assert body["response"] == "ok"

    assert "rag_trace" in body
    rag_trace = body["rag_trace"]
    assert rag_trace["authenticated"] is True
    assert rag_trace["namespace"] == "#V#user@org"
    assert rag_trace["namespace_source"] == "effective_context.namespace"
    assert rag_trace["retrieval_attempted"] is True
    assert "search_knowledge_base" in rag_trace["tools_invoked"]
    assert rag_trace["tool_results_included_in_prompt"] is True

    assert "llm_debug" in body
    assert body["llm_debug"]["namespace_report"]["namespace"] == "#V#user@org"
    assert (
        body["llm_debug"]["namespace_report"]["namespace_source"]
        == "effective_context.namespace"
    )
    assert body["llm_debug"]["namespace_report"]["mismatch_detected"] is False


def test_generate_prefers_window_effective_namespace_and_reports_mismatch(
    app, monkeypatch
):
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#user"
        sess["namespace"] = "#V#user@flask_org"
        sess["session_id"] = "test-session"

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {
            "user_id": "#V#user",
            "organisation_id": "#V#window_org",
            "role": "member",
            "namespace": "#V#user@window_org",
            "chat_session_id": "test-session",
            "source": "window_session",
        },
    )

    captured_namespaces = []

    def _capture_add_message(_user_id, _session_id, _message, llm_debug_data=None, **kwargs):
        captured_namespaces.append(kwargs.get("namespace"))
        return None

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        _capture_add_message,
    )

    resp = client.post(
        "/von/generate",
        json={"prompt": "What is in my RAG store?"},
        headers={"X-Von-Window-Session": "ws-test"},
    )
    assert resp.status_code == 200

    body = resp.get_json()
    rag_trace = body["rag_trace"]
    assert rag_trace["namespace"] == "#V#user@window_org"
    assert rag_trace["namespace_source"] == "effective_context.namespace"

    namespace_report = body["llm_debug"]["namespace_report"]
    assert namespace_report["mismatch_detected"] is True
    assert namespace_report["effective_context_source"] == "window_session"
    assert namespace_report["session_namespace"] == "#V#user@flask_org"
    assert namespace_report["namespace"] == "#V#user@window_org"

    assert captured_namespaces
    assert all(ns == "#V#user@window_org" for ns in captured_namespaces if ns is not None)


def test_generate_rag_trace_marks_unauthenticated(app):
    client = app.test_client()

    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestratorNoTools()

    with client.session_transaction() as sess:
        sess["session_id"] = "test-session"

    resp = client.post("/von/generate", json={"prompt": "What is in my RAG store?"})
    assert resp.status_code == 200

    body = resp.get_json()
    assert "rag_trace" in body
    rag_trace = body["rag_trace"]
    assert rag_trace["authenticated"] is False
    assert rag_trace["namespace"] is None
    assert rag_trace["retrieval_attempted"] is False
    assert rag_trace["retrieval_attempt_reason"] == "not_authenticated"
