from __future__ import annotations

from flask import Flask
from typing import Any, cast


class _DummyLLM:
    def generate(self, *_args, **_kwargs):
        raise AssertionError("LLM generate() should not be called directly in this test")


class _StubOrchestrator:
    def __init__(self, result):
        self._result = result

    def configure_execution_caps(self, **_kwargs) -> None:
        return None

    def _extract_json_blob(self, _text: str):
        return None

    def run(self, **_kwargs):
        return self.execute_conversation_turn_supervised(**_kwargs)

    def execute_conversation_turn_supervised(self, **_kwargs):
        return self._result


class _SubmittedTaskStatus:
    def __init__(self, status: str = "running") -> None:
        self.status = status


class _CapturingTaskRegistry:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def submit_task(
        self,
        *,
        task_id: str,
        callable,
        progress_callback,
        session_id,
        user_id,
    ):
        self.calls.append(
            {
                "task_id": task_id,
                "callable": callable,
                "progress_callback": progress_callback,
                "session_id": session_id,
                "user_id": user_id,
            }
        )
        return _SubmittedTaskStatus()


def _make_app(monkeypatch, orchestrator, task_registry) -> Flask:
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: _DummyLLM(),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "gpt-5.4-nano",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.background_task_registry",
        task_registry,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
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
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(von_bp, url_prefix="/von")
    app.config["CONTEXT"] = []
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = object()
    return app


def test_background_generate_submits_immediately(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    orchestrator_result = OrchestratorResult(
        response_text="ok",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
    )
    task_registry = _CapturingTaskRegistry()
    app = _make_app(monkeypatch, _StubOrchestrator(orchestrator_result), task_registry)

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={
            "prompt": "Run this in the background",
            "background": True,
            "model": "gpt-5.4-nano",
        },
        headers={"X-Von-Window-Session": "window-123"},
    )

    assert response.status_code == 202
    body = response.get_json()
    assert isinstance(body, dict)
    assert body["background"] is True
    assert len(task_registry.calls) == 1

    submitted = cast(dict[str, Any], task_registry.calls[0])
    task_callable = submitted["callable"]
    assert callable(task_callable)
    assert submitted["progress_callback"] is None
    assert body["task_id"] == submitted["task_id"]


def test_normalise_background_generate_result_preserves_json_payload() -> None:
    from flask import Flask, jsonify

    import src.backend.server.routes.von_routes as von_routes

    app = Flask(__name__)
    with app.app_context():
        payload = von_routes._normalise_background_generate_result(
            (jsonify({"response": "ok", "llm_debug": {"response": "ok"}}), 200)
        )

    assert payload["response"] == "ok"
    llm_debug = payload.get("llm_debug")
    assert isinstance(llm_debug, dict)
    assert llm_debug.get("response") == "ok"


def test_resolve_generate_requested_model_prefers_explicit_request_model(monkeypatch):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        von_routes,
        "get_active_model_name",
        lambda *args, **kwargs: None,
    )

    model_name, client_type = von_routes._resolve_generate_requested_model(
        {"model": "gpt-5.4-nano"},
        user_concept_id=None,
        org_concept_id=None,
        configured_model=None,
    )

    assert model_name == "gpt-5.4-nano"
    assert client_type == "openai"


def test_resolve_generate_requested_model_overrides_scoped_setting_with_explicit_request(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        von_routes,
        "get_active_model_name",
        lambda *args, **kwargs: "gpt-5.4-mini",
    )

    model_name, client_type = von_routes._resolve_generate_requested_model(
        {"model": "ollama:llama3.1:8b"},
        user_concept_id="#V#test_user",
        org_concept_id=None,
        configured_model=None,
    )

    assert model_name == "llama3.1:8b"
    assert client_type == "ollama"
