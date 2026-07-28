from __future__ import annotations

from typing import Any

from flask import Flask

from src.backend.services.adaptive_turn_service import AdaptiveTurnResult


def _make_app(monkeypatch, history_calls: list[dict[str, object]]) -> Flask:
    from src.backend.workflows.durable import registry_factory

    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_generate_session_override")
    monkeypatch.setattr(registry_factory, "discover_workflow_ids", list)
    monkeypatch.setattr(
        registry_factory, "_launch_deferred_registry_work", lambda **_kwargs: None
    )

    from src.backend.server.routes import von_routes

    adaptive_calls: list[dict[str, Any]] = []

    def _execute_adaptive_turn(**kwargs: Any) -> AdaptiveTurnResult:
        adaptive_calls.append(dict(kwargs))
        return AdaptiveTurnResult(
            response_text="ok",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
        )

    monkeypatch.setattr(von_routes, "execute_adaptive_turn", _execute_adaptive_turn)

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
        lambda **_kwargs: object(),
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
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    app.config["CONTEXT"] = []
    app.config["_ADAPTIVE_TURN_CALLS"] = adaptive_calls
    app.config["INTERNAL_MCP_GATEWAY"] = None
    return app


def test_generate_honours_explicit_conversation_session_id(monkeypatch):
    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)

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
    assert app.config["_ADAPTIVE_TURN_CALLS"]
    assert history_calls, "expected chat history writes"
    assert all(
        call.get("session_id") == "session-override" for call in history_calls
    )


def test_generate_rejects_non_string_conversation_session_id(monkeypatch):
    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "conversation_session_id": {"bad": "value"}},
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert isinstance(body, dict)
    assert body["error"] == "invalid_conversation_session_id"


def test_generate_creates_and_binds_chat_session_when_window_scope_has_no_active_session(
    monkeypatch,
):
    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda window_session_id, session_snapshot, user_concept_id: {
            "organisation_id": "#V#org",
            "chat_session_id": None,
            "role": "member",
            "namespace": "#V#user@org",
            "source": "window_session",
        },
    )

    created_calls: list[dict[str, object]] = []
    bound_sessions: list[tuple[str, str, str | None]] = []

    def _fake_create_chat_session(**kwargs):
        created_calls.append(dict(kwargs))
        return {
            "session_id": str(kwargs["session_id"]),
            "session_name": "Chat 2026-04-13 18:30",
            "namespace": kwargs.get("namespace"),
        }

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.create_chat_session",
        _fake_create_chat_session,
    )
    monkeypatch.setattr(
        "src.backend.services.window_session_context_service.set_window_chat_session",
        lambda window_session_id, chat_session_id, user_id=None: bound_sessions.append(
            (str(window_session_id), str(chat_session_id), user_id)
        ),
    )

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello from a fresh chat tab"},
        headers={"X-Von-Window-Session": "window-identity"},
    )

    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert isinstance(body, dict)
    assert isinstance(body.get("conversation_session_id"), str)
    assert body["conversation_session_id"]
    assert body["session_id"] == body["conversation_session_id"]
    assert body["conversation_session_created"] is True
    assert body["conversation_session_name"] == "Chat 2026-04-13 18:30"

    assert app.config["_ADAPTIVE_TURN_CALLS"]
    assert history_calls, "expected chat history writes"
    assert all(
        call.get("session_id") == body["conversation_session_id"]
        for call in history_calls
    )

    assert created_calls == [
        {
            "user_id": "#V#user",
            "session_id": body["conversation_session_id"],
            "session_name": None,
            "namespace": "#V#user@org",
            "organisation_concept_id": "#V#org",
            "role_in_org": "member",
        }
    ]
    assert bound_sessions == [
        ("window-identity", body["conversation_session_id"], "#V#user")
    ]
