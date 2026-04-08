from __future__ import annotations

from flask import Flask


class _CapturingOrchestrator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs):
        return self.execute_conversation_turn_supervised(**kwargs)

    def execute_conversation_turn_supervised(self, **kwargs):
        self.calls.append(kwargs)

        from src.backend.integrations.internal_mcp.orchestrator import (
            OrchestratorResult,
        )

        return OrchestratorResult(
            response_text="ok",
            extra_messages=[],
            tool_invocations=[],
            aux_llm_calls=[],
        )


def _make_app(monkeypatch, history_calls: list[dict[str, object]]) -> Flask:
    import src.backend.workflows.durable.registry_factory as registry_factory

    monkeypatch.setattr(registry_factory, "discover_workflow_ids", lambda: [])
    monkeypatch.setattr(
        registry_factory, "_launch_deferred_registry_work", lambda **_kwargs: None
    )

    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setenv("VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "0")
    monkeypatch.setenv("VON_WORKFLOW_DISCOVERY_ENABLE", "0")

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda window_session_id, session_snapshot, user_concept_id: {
            "organisation_id": "#V#org",
            "chat_session_id": "window-session",
            "role": "member",
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_buttonify_model_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._add_chat_history_message",
        lambda **kwargs: history_calls.append(kwargs),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_context_concept_reference_metadata",
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
        "src.backend.server.routes.von_routes._finalise_llm_debug_info",
        lambda **kwargs: kwargs["llm_debug_info"],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_workflow_registry_read_only",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda *_args, **_kwargs: None,
        raising=False,
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = True
    app.register_blueprint(von_bp, url_prefix="/von")
    app.config["CONTEXT"] = []
    app.config["INTERNAL_MCP_GATEWAY"] = object()
    return app


def test_generate_honours_explicit_conversation_session_id(monkeypatch):
    history_calls: list[dict[str, object]] = []
    orchestrator = _CapturingOrchestrator()
    app = _make_app(monkeypatch, history_calls)
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={
            "prompt": "Hello from the background queue",
            "conversation_session_id": "session-override",
        },
        headers={"X-Von-Window-Session": "window-identity"},
    )

    assert resp.status_code == 200, resp.get_json()
    assert orchestrator.calls, "expected orchestrator.run() to be called"
    assert orchestrator.calls[0]["conversation_session_id"] == "session-override"
    assert history_calls, "expected chat history writes"
    assert all(
        call.get("session_id") == "session-override" for call in history_calls
    )


def test_generate_rejects_non_string_conversation_session_id(monkeypatch):
    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _CapturingOrchestrator()

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "conversation_session_id": {"bad": "value"}},
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert isinstance(body, dict)
    assert body["error"] == "invalid_conversation_session_id"
