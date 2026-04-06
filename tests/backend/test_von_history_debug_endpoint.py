from flask import Flask


def test_history_debug_returns_stored_turn_execution_diagnostics(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    debug_payload = {
        "model": "test-model",
        "turn_execution_diagnostics": {
            "generated_at_utc": "2026-02-18T00:00:00Z",
            "request_id": "req-history-1",
            "elapsed_ms": 987,
            "prompt_preview": "Summarise today",
            "latest_progress": {"status": "completed", "stage": "completed"},
            "progress_events": [{"status": "completed", "stage": "completed"}],
            "phase_history": [{"phase": "tool_execute"}],
            "tool_history": [{"tool": "search_knowledge_base", "success": True}],
            "workflow_discovery": {"matches": [{"concept_id": "#V#workflow_demo"}]},
        },
    }

    calls: dict[str, object] = {}

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#test_user"},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.resolve_chat_history_namespace",
        lambda _user_id: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )

    def _get_debug_entry(*, user_id, session_id, history_index, namespace):
        calls["user_id"] = user_id
        calls["session_id"] = session_id
        calls["history_index"] = history_index
        calls["namespace"] = namespace
        return debug_payload

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_debug_entry",
        _get_debug_entry,
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_bp, url_prefix="/von")

    client = app.test_client()
    response = client.get(
        "/von/history/debug",
        query_string={"session_id": "session-1", "history_index": 3},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("success") is True
    assert body.get("history_location") == {"session_id": "session-1", "history_index": 3}

    returned = body.get("llm_debug_data")
    assert isinstance(returned, dict)
    assert returned == debug_payload
    diagnostics = returned.get("turn_execution_diagnostics")
    assert isinstance(diagnostics, dict)
    assert diagnostics.get("request_id") == "req-history-1"
    assert diagnostics.get("prompt_preview") == "Summarise today"

    assert calls == {
        "user_id": "#V#test_user",
        "session_id": "session-1",
        "history_index": 3,
        "namespace": "#V#test_user",
    }


def test_history_debug_uses_org_hint_to_upgrade_bare_namespace(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    calls: dict[str, object] = {}

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#test_user"},
    )

    def _has_chat_history_session(user_id, session_id, namespace=None, **_kwargs):
        calls["checked_namespace"] = namespace
        return namespace == "#V#test_user@org"

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.has_chat_history_session",
        _has_chat_history_session,
    )

    def _get_debug_entry(*, user_id, session_id, history_index, namespace):
        calls["user_id"] = user_id
        calls["session_id"] = session_id
        calls["history_index"] = history_index
        calls["namespace"] = namespace
        return {"request_id": "req-org-1"}

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_debug_entry",
        _get_debug_entry,
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_bp, url_prefix="/von")

    client = app.test_client()
    response = client.get(
        "/von/history/debug",
        query_string={
            "session_id": "session-1",
            "history_index": 3,
            "organisation_concept_id": "#V#org",
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert calls["checked_namespace"] == "#V#test_user@org"
    assert calls["namespace"] == "#V#test_user@org"
