from __future__ import annotations

from flask import Flask, jsonify, request, session
from typing import Any, cast


class _DummyLLM:
    def generate(self, *_args, **_kwargs):
        raise AssertionError("LLM generate() should not be called directly in this test")


class _StubOrchestrator:
    def __init__(self, result):
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def configure_execution_caps(self, **_kwargs) -> None:
        return None

    def _extract_json_blob(self, _text: str):
        return None

    def run(self, **_kwargs):
        return self.execute_conversation_turn_supervised(**_kwargs)

    def execute_conversation_turn_supervised(self, **_kwargs):
        self.calls.append(dict(_kwargs))
        return self._result


class _SubmittedTaskStatus:
    def __init__(self, status: str = "running") -> None:
        self.status = status


class _CapturingTaskRegistry:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.progress_updates: list[tuple[str, dict[str, Any]]] = []
        self.cancelled_task_ids: set[str] = set()

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

    def update_progress(self, task_id: str, progress: dict[str, Any]) -> bool:
        self.progress_updates.append((task_id, dict(progress)))
        return True

    def is_cancellation_requested(self, task_id: str) -> bool:
        return task_id in self.cancelled_task_ids


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

    def fail_progress_setting_lookup() -> bool:
        raise AssertionError(
            "background submission should not perform progress-setting lookup"
        )

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        fail_progress_setting_lookup,
    )

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


def test_background_generate_reenters_with_task_progress_metadata(monkeypatch):
    import src.backend.server.routes.von_routes as von_routes

    captured_payload: dict[str, Any] = {}
    task_registry = _CapturingTaskRegistry()
    monkeypatch.setattr(von_routes, "background_task_registry", task_registry)

    def fake_generate():
        payload = request.get_json()
        assert isinstance(payload, dict)
        captured_payload.update(payload)
        return jsonify({"response": "ok"}), 200

    monkeypatch.setattr(von_routes, "generate", fake_generate)

    app = Flask(__name__)
    app.secret_key = "test-secret"
    with app.test_request_context(
        "/von/generate",
        method="POST",
        json={"prompt": "Run this in the background", "background": True},
        headers={"X-Von-Window-Session": "window-123"},
    ):
        session["session_id"] = "session-123"
        response, status_code = von_routes._submit_generate_background_request(
            app=app,
            request_data={"prompt": "Run this in the background", "background": True},
            request_headers=request.headers,
            session_snapshot=dict(session),
            request_id="task-123",
        )

    assert status_code == 202
    assert response.get_json()["task_id"] == "task-123"
    assert len(task_registry.calls) == 1

    submitted = cast(dict[str, Any], task_registry.calls[0])
    task_callable = submitted["callable"]
    assert callable(task_callable)
    task_callable()

    assert captured_payload["background"] is False
    assert captured_payload["client_request_id"] == "task-123"
    assert captured_payload["background_task_id"] == "task-123"
    assert captured_payload["background_progress"] is True


def test_background_generate_reentry_skips_ui_progress_persistence(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    import src.backend.server.routes.von_routes as von_routes

    orchestrator_result = OrchestratorResult(
        response_text="ok",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
    )
    task_registry = _CapturingTaskRegistry()
    app = _make_app(monkeypatch, _StubOrchestrator(orchestrator_result), task_registry)

    monkeypatch.setattr(
        von_routes,
        "get_show_tool_use_during_thinking",
        lambda: True,
    )

    def fail_ui_progress_persistence(*_args, **_kwargs) -> None:
        raise AssertionError(
            "background re-entry should use task-registry progress only"
        )

    monkeypatch.setattr(von_routes, "_set_tool_progress", fail_ui_progress_persistence)

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={
            "prompt": "Run this inside the background worker",
            "background": False,
            "background_task_id": "task-123",
            "client_request_id": "task-123",
            "background_progress": True,
            "conversation_session_id": 123,
            "model": "gpt-5.4-nano",
        },
        headers={"X-Von-Window-Session": "window-123"},
    )

    assert response.status_code == 400
    assert task_registry.progress_updates
    assert task_registry.progress_updates[0][0] == "task-123"
    assert task_registry.progress_updates[0][1]["subtask"] == "request setup"


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


def test_resolve_generate_requested_model_ignores_browser_object_request_model(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        von_routes,
        "get_active_model_name",
        lambda *args, **kwargs: "gpt-5.4-mini",
    )

    model_name, client_type = von_routes._resolve_generate_requested_model(
        {"model": "openai:[object PointerEvent]"},
        user_concept_id="#V#test_user",
        org_concept_id=None,
        configured_model=None,
    )

    assert model_name == "gpt-5.4-mini"
    assert client_type is None


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


def test_resolve_generate_requested_model_honours_explicit_provider_field(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        von_routes,
        "get_active_model_name",
        lambda *args, **kwargs: "gpt-5.4-mini",
    )

    model_name, client_type = von_routes._resolve_generate_requested_model(
        {"model": "gemma4:e4b", "model_provider": "ollama"},
        user_concept_id="#V#test_user",
        org_concept_id=None,
        configured_model=None,
    )

    assert model_name == "gemma4:e4b"
    assert client_type == "ollama"


def test_build_generate_conversation_turn_instance_inputs_preserves_requested_model() -> (
    None
):
    from src.backend.server.routes.generate_route_support import (
        _build_generate_conversation_turn_instance_inputs,
    )

    payload = _build_generate_conversation_turn_instance_inputs(
        session_id="session-1",
        request_id="request-1",
        namespace_source="window_context",
        presenter_mode_requested=False,
        request_gmail_profile="profile-1",
        request_language="en-NZ",
        requested_model="gemma4:26b",
        requested_client_type="ollama",
        prompt_text="What do you know about my current research interests?",
        workflow_discovery_result={"selected_workflow_id": "#V#concept_search"},
        workflow_continuation_context={"applied": False},
    )

    assert payload["requested_model"] == "gemma4:26b"
    assert payload["requested_client_type"] == "ollama"
    assert payload["prompt"] == "What do you know about my current research interests?"
    assert payload["user_prompt"] == (
        "What do you know about my current research interests?"
    )
    assert payload["workflow_discovery"] == {
        "selected_workflow_id": "#V#concept_search"
    }
    assert payload["continuation_context"] == {"applied": False}


def test_submit_generate_conversation_turn_instance_skips_in_agent_test(
    monkeypatch,
) -> None:
    from src.backend.server.routes.generate_route_support import (
        _GenerateConversationTurnInstanceState,
        _submit_generate_conversation_turn_instance,
    )

    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    auxiliary_llm_calls: list[dict[str, Any]] = []

    def fail_get_instance_manager():
        raise AssertionError("AgentTest should not submit durable turn instances")

    def fail_submit_verified_workflow_instance(**_kwargs):
        raise AssertionError("AgentTest should not submit durable turn instances")

    _submit_generate_conversation_turn_instance(
        state=_GenerateConversationTurnInstanceState(),
        auxiliary_llm_calls=auxiliary_llm_calls,
        session_id="session-1",
        request_id="request-1",
        user_namespace="#V#user@#V#org",
        user_concept_id="#V#user",
        org_concept_id="#V#org",
        namespace_source="test",
        presenter_mode_requested=False,
        request_gmail_profile=None,
        request_language="en-NZ",
        requested_model="gemma4:e4b",
        requested_client_type="ollama",
        prompt_text="Prompt",
        workflow_discovery_result=None,
        workflow_continuation_context=None,
        get_instance_manager_fn=fail_get_instance_manager,
        submit_verified_workflow_instance_fn=fail_submit_verified_workflow_instance,
    )

    assert auxiliary_llm_calls
    assert auxiliary_llm_calls[0]["status"] == "submission_skipped"
    assert auxiliary_llm_calls[0]["reason_code"] == "agent_test_instance"


def test_submit_generate_conversation_turn_instance_skips_background_reentry() -> None:
    from src.backend.server.routes.generate_route_support import (
        _GenerateConversationTurnInstanceState,
        _submit_generate_conversation_turn_instance,
    )

    auxiliary_llm_calls: list[dict[str, Any]] = []
    state = _GenerateConversationTurnInstanceState()

    def fail_get_instance_manager():
        raise AssertionError(
            "background re-entry should not submit durable turn instances"
        )

    def fail_submit_verified_workflow_instance(**_kwargs):
        raise AssertionError(
            "background re-entry should not submit durable turn instances"
        )

    _submit_generate_conversation_turn_instance(
        state=state,
        auxiliary_llm_calls=auxiliary_llm_calls,
        session_id="session-1",
        request_id="request-1",
        user_namespace="#V#user@#V#org",
        user_concept_id="#V#user",
        org_concept_id="#V#org",
        namespace_source="test",
        presenter_mode_requested=False,
        request_gmail_profile=None,
        request_language="en-NZ",
        requested_model="gemma4:e4b",
        requested_client_type="ollama",
        prompt_text="Prompt",
        workflow_discovery_result=None,
        workflow_continuation_context=None,
        get_instance_manager_fn=fail_get_instance_manager,
        submit_verified_workflow_instance_fn=fail_submit_verified_workflow_instance,
        background_task_id="task-123",
    )

    assert state.manager is None
    assert state.instance_id is None
    assert auxiliary_llm_calls
    assert auxiliary_llm_calls[0]["status"] == "submission_skipped"
    assert auxiliary_llm_calls[0]["reason_code"] == "background_task_reentry"
