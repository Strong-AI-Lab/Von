from __future__ import annotations

import json

import pytest
from flask import Flask, jsonify, request, session
from typing import Any, Mapping, cast

from src.backend.services.adaptive_turn_service import AdaptiveTurnResult
from src.backend.services.tool_progress_store_service import (
    reset_tool_progress_persistence_queue_for_tests,
)


@pytest.fixture(autouse=True)
def _clear_live_progress_state():
    import src.backend.server.routes.von_routes as von_routes

    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()
    reset_tool_progress_persistence_queue_for_tests()
    yield
    with von_routes._TOOL_PROGRESS_LOCK:
        von_routes._TOOL_PROGRESS.clear()
    reset_tool_progress_persistence_queue_for_tests()


class _DummyLLM:
    def generate(self, *_args, **_kwargs):
        raise AssertionError("LLM generate() should not be called directly in this test")


class _StubAdaptiveTurn:
    def __init__(self, result):
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def execute(self, **kwargs: Any) -> AdaptiveTurnResult:
        self.calls.append(dict(kwargs))
        return self._result


class _SubmittedTaskStatus:
    def __init__(self, status: str = "running") -> None:
        self.status = status


class _CapturingTaskRegistry:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.progress_updates: list[tuple[str, dict[str, Any]]] = []
        self.terminal_updates: list[tuple[str, dict[str, Any]]] = []
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

    def mark_terminal_external(self, task_id: str, **kwargs):
        self.terminal_updates.append((task_id, dict(kwargs)))
        return _SubmittedTaskStatus(str(kwargs.get("status") or "completed"))


def _make_app(monkeypatch, adaptive_turn, task_registry) -> Flask:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_generate_background_submission")

    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        von_routes,
        "get_llm_client",
        lambda **_kwargs: _DummyLLM(),
    )
    monkeypatch.setattr(
        von_routes,
        "get_active_model_name",
        lambda *args, **kwargs: "gpt-5.4-nano",
    )
    monkeypatch.setattr(
        von_routes,
        "get_show_tool_use_during_thinking",
        lambda: False,
    )
    monkeypatch.setattr(
        von_routes,
        "execute_adaptive_turn",
        adaptive_turn.execute,
    )
    monkeypatch.setattr(von_routes, "get_buttonify_model_enabled", lambda: False)
    monkeypatch.setattr(
        von_routes,
        "get_display_elements_screen_fence_compat_enabled",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        von_routes.PromptTemplateService,
        "resolve_prompt_text",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        von_routes,
        "background_task_registry",
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
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    app.config["CONTEXT"] = []
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = object()
    app.config["INTERNAL_MCP_GATEWAY"] = None
    return app


def _authorised_turn_inputs(call: Mapping[str, Any]) -> dict[str, Any]:
    context = call.get("context")
    assert isinstance(context, list)
    for message in context:
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except ValueError:
            continue
        if (
            isinstance(payload, dict)
            and payload.get("type") == "authorised_turn_inputs"
            and isinstance(payload.get("inputs"), dict)
        ):
            return dict(payload["inputs"])
    raise AssertionError("adaptive turn did not receive the authorised turn inputs")


def test_background_generate_submits_immediately(monkeypatch):
    adaptive_result = AdaptiveTurnResult(
        response_text="ok",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
    )
    task_registry = _CapturingTaskRegistry()
    app = _make_app(monkeypatch, _StubAdaptiveTurn(adaptive_result), task_registry)

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


def test_background_generate_reentry_passes_authorised_inputs_to_adaptive_turn(
    monkeypatch,
):
    adaptive_result = AdaptiveTurnResult(
        response_text="ok",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
    )
    task_registry = _CapturingTaskRegistry()
    adaptive_turn = _StubAdaptiveTurn(adaptive_result)
    app = _make_app(monkeypatch, adaptive_turn, task_registry)

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={
            "prompt": "Run this in the background",
            "background": True,
            "model": "gpt-5.4-nano",
            "workflow_inputs": {
                "base_gmail_query": "arxiv.org newer_than:365d",
                "gmail_max_results": 1,
            },
        },
        headers={"X-Von-Window-Session": "window-123"},
    )

    assert response.status_code == 202
    submitted = cast(dict[str, Any], task_registry.calls[0])
    task_callable = submitted["callable"]
    assert callable(task_callable)
    task_callable()

    assert adaptive_turn.calls
    assert _authorised_turn_inputs(adaptive_turn.calls[0]) == {
        "base_gmail_query": "arxiv.org newer_than:365d",
        "gmail_max_results": 1,
    }
    assert len(task_registry.terminal_updates) == 1
    terminal_task_id, terminal_update = task_registry.terminal_updates[0]
    assert terminal_task_id == submitted["task_id"]
    terminal_result = terminal_update["result"]
    assert isinstance(terminal_result, dict)
    llm_debug = terminal_result["llm_debug"]
    assert llm_debug.get("background_result_source") is None
    assert isinstance(llm_debug.get("turn_execution_record"), dict)


def test_background_generate_preserves_adaptive_non_success(monkeypatch):
    adaptive_result = AdaptiveTurnResult(
        response_text="The model call failed.",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
        terminal_status="model_error",
    )
    task_registry = _CapturingTaskRegistry()
    app = _make_app(
        monkeypatch,
        _StubAdaptiveTurn(adaptive_result),
        task_registry,
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
    submitted = cast(dict[str, Any], task_registry.calls[0])
    task_callable = submitted["callable"]
    assert callable(task_callable)
    with pytest.raises(RuntimeError, match="non-success model_error"):
        task_callable()
    assert task_registry.terminal_updates == []


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
            request_data={
                "prompt": "Run this in the background",
                "background": True,
                "workflow_inputs": {"gmail_max_results": 1},
            },
            request_headers=cast(Mapping[str, Any], request.headers),
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
    assert captured_payload["workflow_inputs"] == {"gmail_max_results": 1}


def test_background_generate_reentry_projects_progress_for_mcp_live_readback(
    monkeypatch,
):
    from src.backend.services.turn_execution_live_progress_service import (
        get_turn_execution_live_progress_payload,
    )

    import src.backend.server.routes.von_routes as von_routes

    adaptive_result = AdaptiveTurnResult(
        response_text="ok",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
    )
    task_registry = _CapturingTaskRegistry()
    app = _make_app(monkeypatch, _StubAdaptiveTurn(adaptive_result), task_registry)

    monkeypatch.setattr(
        von_routes,
        "get_show_tool_use_during_thinking",
        lambda: True,
    )

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

    live_progress = get_turn_execution_live_progress_payload(
        request_id="task-123",
        window_session_id="window-123",
    )
    assert live_progress is not None
    assert live_progress["success"] is True
    assert live_progress["request_id"] == "task-123"
    assert live_progress["resolved_scope_key"] == "anon:window:window-123"
    assert live_progress["progress_source"] == "tool_progress_state"
    assert live_progress["status"] == "thinking"
    assert live_progress["stage"] == "context_build"


def test_generate_passes_authorised_inputs_to_adaptive_turn(monkeypatch):
    adaptive_result = AdaptiveTurnResult(
        response_text="ok",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
    )
    task_registry = _CapturingTaskRegistry()
    adaptive_turn = _StubAdaptiveTurn(adaptive_result)
    app = _make_app(monkeypatch, adaptive_turn, task_registry)

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={
            "prompt": "Run selected workflow",
            "background": False,
            "model": "gpt-5.4-nano",
            "workflow_inputs": {
                "base_gmail_query": "arxiv.org newer_than:365d",
                "gmail_max_results": 1,
            },
        },
        headers={"X-Von-Window-Session": "window-123"},
    )

    assert response.status_code == 200
    assert adaptive_turn.calls
    assert _authorised_turn_inputs(adaptive_turn.calls[0]) == {
        "base_gmail_query": "arxiv.org newer_than:365d",
        "gmail_max_results": 1,
    }


def test_generate_surfaces_adaptive_terminal_status_without_rejudging_it(
    monkeypatch,
):
    adaptive_result = AdaptiveTurnResult(
        response_text="I could not complete the request before the deadline.",
        extra_messages=(),
        tool_invocations=(),
        aux_llm_calls=(),
        terminal_status="turn_deadline_exceeded",
    )
    task_registry = _CapturingTaskRegistry()
    app = _make_app(
        monkeypatch,
        _StubAdaptiveTurn(adaptive_result),
        task_registry,
    )

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={
            "prompt": "Try this",
            "background": False,
            "model": "gpt-5.4-nano",
        },
        headers={"X-Von-Window-Session": "window-123"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is False
    assert body["terminal_status"] == "turn_deadline_exceeded"
    assert body["response"] == adaptive_result.response_text


def test_orchestrator_projects_namespaced_workflow_launch_inputs_safely() -> None:
    from src.backend.integrations.internal_mcp.orchestrator import (
        _project_namespaced_workflow_launch_inputs,
    )

    payload: dict[str, Any] = {
        "prompt": "original prompt",
        "workflow_launch_inputs": {
            "gmail_max_results": 1,
            "prompt": "override prompt",
            "workflow_id": "#V#wrong_workflow",
            "__private": "drop",
        },
    }

    _project_namespaced_workflow_launch_inputs(payload)

    assert payload["gmail_max_results"] == 1
    assert payload["prompt"] == "original prompt"
    assert "workflow_id" not in payload
    assert "__private" not in payload
    assert payload["namespaced_workflow_launch_inputs_applied"] == [
        "gmail_max_results"
    ]


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


def test_normalise_background_generate_result_rejects_typed_non_success() -> None:
    from flask import Flask, jsonify

    import src.backend.server.routes.von_routes as von_routes

    app = Flask(__name__)
    with app.app_context():
        with pytest.raises(RuntimeError, match="non-success model_error"):
            von_routes._normalise_background_generate_result(
                (
                    jsonify(
                        {
                            "success": False,
                            "terminal_status": "model_error",
                            "response": "Provider failed.",
                        }
                    ),
                    200,
                )
            )


def test_resolve_generate_requested_model_prefers_explicit_request_model(monkeypatch):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        von_routes,
        "get_active_model_name",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(von_routes, "resolve_llm_setting", lambda **_kwargs: None)

    model_name, client_type, model_parameters = von_routes._resolve_generate_requested_model(
        {"model": "gpt-5.4-nano"},
        user_concept_id=None,
        org_concept_id=None,
        configured_model=None,
    )

    assert model_name == "gpt-5.4-nano"
    assert client_type == "openai"
    assert model_parameters == {}


def test_resolve_generate_requested_model_ignores_browser_object_request_model(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        von_routes,
        "get_active_model_name",
        lambda *args, **kwargs: "gpt-5.4-mini",
    )
    monkeypatch.setattr(von_routes, "resolve_llm_setting", lambda **_kwargs: None)

    model_name, client_type, model_parameters = von_routes._resolve_generate_requested_model(
        {"model": "openai:[object PointerEvent]"},
        user_concept_id="#V#test_user",
        org_concept_id=None,
        configured_model=None,
    )

    assert model_name == "gpt-5.4-mini"
    assert client_type is None
    assert model_parameters == {}


def test_resolve_generate_requested_model_overrides_scoped_setting_with_explicit_request(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        von_routes,
        "get_active_model_name",
        lambda *args, **kwargs: "gpt-5.4-mini",
    )
    monkeypatch.setattr(von_routes, "resolve_llm_setting", lambda **_kwargs: None)

    model_name, client_type, model_parameters = von_routes._resolve_generate_requested_model(
        {"model": "ollama:llama3.1:8b"},
        user_concept_id="#V#test_user",
        org_concept_id=None,
        configured_model=None,
    )

    assert model_name == "llama3.1:8b"
    assert client_type == "ollama"
    assert model_parameters == {}


def test_resolve_generate_requested_model_honours_explicit_provider_field(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        von_routes,
        "get_active_model_name",
        lambda *args, **kwargs: "gpt-5.4-mini",
    )
    monkeypatch.setattr(von_routes, "resolve_llm_setting", lambda **_kwargs: None)

    model_name, client_type, model_parameters = von_routes._resolve_generate_requested_model(
        {"model": "gemma4:e4b", "model_provider": "ollama"},
        user_concept_id="#V#test_user",
        org_concept_id=None,
        configured_model=None,
    )

    assert model_name == "gemma4:e4b"
    assert client_type == "ollama"
    assert model_parameters == {}


def test_resolve_generate_requested_model_preserves_scoped_model_parameters(
    monkeypatch,
):
    import src.backend.server.routes.von_routes as von_routes

    monkeypatch.setattr(
        "src.backend.services.model_parameter_service._registry_parameter_policy",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        von_routes,
        "get_active_model_name",
        lambda *args, **kwargs: "gpt-5.5",
    )
    monkeypatch.setattr(
        von_routes,
        "resolve_llm_setting",
        lambda **_kwargs: {
            "provider": "openai",
            "model": "gpt-5.5",
            "model_parameters": {"reasoning_effort": "low"},
        },
    )

    model_name, client_type, model_parameters = von_routes._resolve_generate_requested_model(
        {},
        user_concept_id="#V#test_user",
        org_concept_id=None,
        configured_model=None,
    )

    assert model_name == "gpt-5.5"
    assert client_type == "openai"
    assert model_parameters == {"reasoning_effort": "low"}


def test_normalise_generate_workflow_launch_inputs_validates_shape() -> None:
    from src.backend.server.routes.von_routes import (
        _normalise_generate_workflow_launch_inputs,
    )

    assert _normalise_generate_workflow_launch_inputs(
        {" gmail_max_results ": 1, "base_gmail_query": "arxiv.org"}
    ) == {
        "gmail_max_results": 1,
        "base_gmail_query": "arxiv.org",
    }

    with pytest.raises(ValueError, match="must be an object"):
        _normalise_generate_workflow_launch_inputs(["not", "an", "object"])

    with pytest.raises(ValueError, match="invalid keys"):
        _normalise_generate_workflow_launch_inputs({"__private": True})
