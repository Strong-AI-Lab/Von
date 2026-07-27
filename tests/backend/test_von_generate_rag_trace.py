import pytest
from flask import Flask

from src.backend.services.adaptive_turn_service import AdaptiveTurnResult


class _StubGateway:
    enabled = True

    def describe_methods(self):
        return {"search_knowledge_base": {"category": "read"}}


@pytest.fixture()
def app(monkeypatch):
    from src.backend.server.routes import von_routes

    evidence = {
        "schema_version": "turn_evidence_envelope.v1",
        "evidence_id": "ev_rag_trace",
        "tool_name": "search_knowledge_base",
        "call_id": "call-rag-trace",
        "turn_id": "turn-rag-trace",
        "status": "ok",
        "trust_boundary": "untrusted_tool_output",
        "sha256": "0" * 64,
        "size_bytes": 100,
        "char_count": 100,
        "content_type": "application/json",
        "value_kind": "object",
        "preview": '{"results":[{"concept_id":"#V#search_trace_example"}]}',
        "preview_format": "json",
        "preview_truncated": False,
        "available_selectors": ["json_pointer", "query", "offset"],
    }
    adaptive_state = {
        "extra_messages": [
            {
                "role": "tool",
                "name": "turn_invoke_capability",
                "tool_call_id": "call-rag-trace",
                "content": '{"evidence_id":"ev_rag_trace"}',
            }
        ],
        "tool_invocations": [
            {
                "tool": "search_knowledge_base",
                "via": "turn_invoke_capability",
                "call_id": "call-rag-trace",
                "payload": {
                    "name": "search_knowledge_base",
                    "arguments": {"query": "x", "top_k": 5},
                },
                "effective_arguments": {"query": "x", "top_k": 5},
                "evidence": evidence,
                "status": "ok",
            }
        ],
        "aux_llm_calls": [
            {
                "type": "adaptive_turn_evidence_index",
                "schema_version": "adaptive_turn_evidence_index.v1",
                "turn_id": "turn-rag-trace",
                "terminal_status": "completed",
                "evidence": [evidence],
            }
        ],
    }

    def _execute_adaptive_turn(**_kwargs):
        return AdaptiveTurnResult(
            response_text="ok",
            extra_messages=tuple(adaptive_state["extra_messages"]),
            tool_invocations=tuple(adaptive_state["tool_invocations"]),
            aux_llm_calls=tuple(adaptive_state["aux_llm_calls"]),
        )

    monkeypatch.setattr(von_routes, "execute_adaptive_turn", _execute_adaptive_turn)
    monkeypatch.setattr(
        von_routes,
        "get_llm_client",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        von_routes,
        "get_active_model_name",
        lambda *_args, **_kwargs: "test-model",
    )
    monkeypatch.setattr(von_routes, "get_show_tool_use_during_thinking", lambda: False)
    monkeypatch.setattr(von_routes, "get_buttonify_model_enabled", lambda: False)
    monkeypatch.setattr(
        von_routes,
        "_ensure_generate_conversation_session",
        lambda **_kwargs: ("test-session", None, False),
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        von_routes,
        "_add_chat_history_message",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        von_routes,
        "build_context_concept_reference_metadata",
        lambda *_args, **_kwargs: {
            "source": "sent_context_user_assistant",
            "metadata_version": 1,
            "message_roles": ["user", "assistant"],
            "messages_scanned": 0,
            "concept_count": 0,
            "concept_count_capped": False,
            "max_concepts": None,
            "include_direct_supertypes": False,
            "max_direct_supertypes": 0,
            "concepts": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda *_args, **_kwargs: [],
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.config["TESTING"] = True
    flask_app.config["PROPAGATE_EXCEPTIONS"] = True
    flask_app.register_blueprint(von_routes.von_bp, url_prefix="/von")

    flask_app.config["CONTEXT"] = []
    flask_app.config["_ADAPTIVE_STATE"] = adaptive_state
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
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
    assert rag_trace["namespace_source"] in {
        "effective_context.namespace",
        "session.namespace",
    }
    assert rag_trace["user_concept_id"] == "#V#user"
    assert rag_trace["organisation_concept_id"] is None
    assert rag_trace["retrieval_attempted"] is True
    assert "search_knowledge_base" in rag_trace["tools_invoked"]
    assert rag_trace["tool_results_included_in_prompt"] is True

    assert "llm_debug" in body
    assert body["llm_debug"]["namespace_report"]["namespace"] == "#V#user@org"
    assert body["llm_debug"]["namespace_report"]["user_concept_id"] == "#V#user"
    assert body["llm_debug"]["namespace_report"]["organisation_concept_id"] is None
    assert (
        body["llm_debug"]["namespace_report"]["namespace_source"]
        == "effective_context.namespace"
    )
    assert body["llm_debug"]["namespace_report"]["mismatch_detected"] is False
    tool_invocations = body["llm_debug"]["tool_invocations"]
    assert tool_invocations[0]["tool"] == "search_knowledge_base"
    assert tool_invocations[0]["arguments"] == {"query": "x", "top_k": 5}
    turn_record = body["llm_debug"]["turn_execution_record"]
    assert turn_record["evidence_index"][0]["evidence_id"] == "ev_rag_trace"


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

    def _capture_add_message(**kwargs):
        captured_namespaces.append(kwargs.get("namespace"))

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._add_chat_history_message",
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
    assert rag_trace["user_concept_id"] == "#V#user"
    assert rag_trace["organisation_concept_id"] == "#V#window_org"

    namespace_report = body["llm_debug"]["namespace_report"]
    assert namespace_report["mismatch_detected"] is True
    assert namespace_report["effective_context_source"] == "window_session"
    assert namespace_report["session_namespace"] == "#V#user@flask_org"
    assert namespace_report["namespace"] == "#V#user@window_org"

    assert captured_namespaces
    assert all(ns == "#V#user@window_org" for ns in captured_namespaces if ns is not None)


def test_generate_rag_trace_marks_unauthenticated(app):
    client = app.test_client()

    app.config["_ADAPTIVE_STATE"]["extra_messages"] = []
    app.config["_ADAPTIVE_STATE"]["tool_invocations"] = []
    app.config["_ADAPTIVE_STATE"]["aux_llm_calls"] = []

    with client.session_transaction() as sess:
        sess["session_id"] = "test-session"

    resp = client.post("/von/generate", json={"prompt": "What is in my RAG store?"})
    assert resp.status_code == 200

    body = resp.get_json()
    assert "rag_trace" in body
    rag_trace = body["rag_trace"]
    assert rag_trace["authenticated"] is False
    assert rag_trace["namespace"] is None
    assert rag_trace["user_concept_id"] is None
    assert rag_trace["organisation_concept_id"] is None
    assert rag_trace["retrieval_attempted"] is False
    assert rag_trace["retrieval_attempt_reason"] == "not_authenticated"
