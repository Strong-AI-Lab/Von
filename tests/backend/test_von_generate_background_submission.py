from __future__ import annotations

import json
from typing import Any, Mapping, cast

import pytest
from flask import Flask, jsonify, request, session

from src.backend.db import mongo_client
from src.backend.services import chat_prompt_queue_service
from src.backend.services.adaptive_turn_service import AdaptiveTurnResult
from src.backend.services.background_task_service import BackgroundTaskCapacityReached
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
        raise AssertionError(
            "LLM generate() should not be called directly in this test"
        )


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
        organisation_id,
        namespace,
        queue_id,
        client_request_id,
        attempt_id,
    ):
        self.calls.append(
            {
                "task_id": task_id,
                "callable": callable,
                "progress_callback": progress_callback,
                "session_id": session_id,
                "user_id": user_id,
                "organisation_id": organisation_id,
                "namespace": namespace,
                "queue_id": queue_id,
                "client_request_id": client_request_id,
                "attempt_id": attempt_id,
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
        lambda: "#V#test_user",
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
    monkeypatch.setattr(
        von_routes,
        "_load_conversation_session_state_fail_soft",
        lambda **_kwargs: ([], None, None, 0, [], {}),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    app.config["CONTEXT"] = []
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = object()
    app.config["INTERNAL_MCP_GATEWAY"] = None
    from src.backend.services.window_session_context_service import (
        get_window_session_store,
        set_window_organisation,
    )

    get_window_session_store().delete("window-123")
    set_window_organisation(
        "window-123",
        "#V#test_org",
        "member",
        "#V#test_user@test_org",
        user_id="#V#test_user",
    )
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


def test_background_generate_returns_typed_capacity_backpressure(monkeypatch):
    task_registry = _CapturingTaskRegistry()

    def _capacity_reached(**_kwargs):
        raise BackgroundTaskCapacityReached("user background capacity reached")

    task_registry.submit_task = _capacity_reached
    app = _make_app(
        monkeypatch,
        _StubAdaptiveTurn(
            AdaptiveTurnResult(
                response_text="must not run",
                extra_messages=(),
                tool_invocations=(),
                aux_llm_calls=(),
            )
        ),
        task_registry,
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Run this in the background",
            "background": True,
            "model": "gpt-5.4-nano",
        },
        headers={"X-Von-Window-Session": "window-123"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.get_json()["error"] == "background_task_capacity_reached"
    assert response.get_json()["retryable"] is True


def test_generate_requires_rebind_for_unknown_window_context(monkeypatch):
    adaptive_turn = _StubAdaptiveTurn(
        AdaptiveTurnResult(
            response_text="must not run",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
        )
    )
    app = _make_app(monkeypatch, adaptive_turn, _CapturingTaskRegistry())

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Use this tab's organisation"},
        headers={"X-Von-Window-Session": "missing-after-restart"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "window_context_unavailable"
    assert adaptive_turn.calls == []


def test_foreground_generate_returns_json_when_cancelled_during_context_setup(
    monkeypatch,
):
    adaptive_turn = _StubAdaptiveTurn(
        AdaptiveTurnResult(
            response_text="must not run",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
        )
    )
    task_registry = _CapturingTaskRegistry()
    task_registry.cancelled_task_ids.add("cancel-during-context")
    app = _make_app(monkeypatch, adaptive_turn, task_registry)

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Stop before model work",
            "background": False,
            "background_task_id": "cancel-during-context",
            "client_request_id": "cancel-during-context",
            "model": "gpt-5.4-nano",
        },
        headers={"X-Von-Window-Session": "window-123"},
    )

    assert response.status_code == 409
    assert response.is_json
    assert response.get_json() == {
        "success": False,
        "terminal_status": "cancelled",
        "error": "generate_task_cancelled",
        "detail": "Cancellation was acknowledged by the running turn.",
        "request_id": "cancel-during-context",
    }
    assert adaptive_turn.calls == []


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


def test_background_generate_freezes_org_scope_when_tab_switches(monkeypatch):
    from src.backend.services.window_session_context_service import (
        set_window_organisation,
    )

    adaptive_turn = _StubAdaptiveTurn(
        AdaptiveTurnResult(
            response_text="ok",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
        )
    )
    task_registry = _CapturingTaskRegistry()
    app = _make_app(monkeypatch, adaptive_turn, task_registry)

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Keep this task in its submitting organisation",
            "background": True,
            "model": "gpt-5.4-nano",
        },
        headers={"X-Von-Window-Session": "window-123"},
    )
    assert response.status_code == 202

    submitted = cast(dict[str, Any], task_registry.calls[0])
    assert submitted["organisation_id"] == "#V#test_org"
    assert submitted["namespace"] == "#V#test_user@test_org"

    set_window_organisation(
        "window-123",
        "#V#other_org",
        "member",
        "#V#test_user@other_org",
        user_id="#V#test_user",
    )

    task_callable = submitted["callable"]
    assert callable(task_callable)
    task_callable()

    assert adaptive_turn.calls
    assert adaptive_turn.calls[0]["org_concept_id"] == "#V#test_org"
    assert adaptive_turn.calls[0]["user_namespace"] == "#V#test_user@test_org"


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
        captured_payload["_frozen_role"] = session.get("role_in_org")
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
            actor_scope={
                "user_concept_id": "#V#test_user",
                "organisation_concept_id": "#V#test_org",
                "namespace": "#V#test_user@test_org",
                "role_in_org": "member",
            },
            conversation_session_id="session-123",
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
    assert captured_payload["_frozen_role"] == "member"


def test_background_generate_reentry_does_not_publish_before_exact_scope_resolution(
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
    assert live_progress is None


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


def test_legacy_queue_first_then_generate_joins_normalised_prompt_record(
    monkeypatch,
):
    adaptive_turn = _StubAdaptiveTurn(
        AdaptiveTurnResult(
            response_text="ok",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
        )
    )
    app = _make_app(monkeypatch, adaptive_turn, _CapturingTaskRegistry())
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    coll.delete_many({})
    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#test_user"

    conversation_id = "legacy-route-queue-first"
    headers = {"X-Von-Window-Session": "window-123"}
    queue_response = client.post(
        "/von/api/chat_prompt_queue",
        json={
            "prompt_raw": "  Discuss #V\u200b#concept  ",
            "session_id": conversation_id,
            "status": "in_progress",
        },
        headers=headers,
    )
    generate_response = client.post(
        "/von/generate",
        json={
            "prompt": "Discuss #V#concept",
            "background": False,
            "model": "gpt-5.4-nano",
            "client_request_id": "legacy-route-queue-first-request",
            "conversation_session_id": conversation_id,
        },
        headers=headers,
    )

    assert queue_response.status_code == 201
    assert generate_response.status_code == 200
    queue_id = queue_response.get_json()["item"]["queue_id"]
    assert coll.count_documents({}) == 1
    persisted = coll.find_one({"queue_id": queue_id})
    assert persisted is not None
    assert persisted["status"] == chat_prompt_queue_service.STATUS_COMPLETED
    assert persisted["client_request_id"] == "legacy-route-queue-first-request"
    assert persisted["legacy_submission_queue_seen"] is True
    assert persisted["legacy_submission_generate_seen"] is True
    assert "active_legacy_submission_key" not in persisted
    assert adaptive_turn.calls

    legacy_finish_response = client.post(
        f"/von/api/chat_prompt_queue/{queue_id}/finish",
        json={"status": "completed"},
        headers=headers,
    )
    assert legacy_finish_response.status_code == 200
    assert legacy_finish_response.get_json()["item"]["queue_id"] == queue_id


def test_legacy_generate_first_stays_joinable_after_completion_and_then_reopens(
    monkeypatch,
):
    adaptive_turn = _StubAdaptiveTurn(
        AdaptiveTurnResult(
            response_text="ok",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
        )
    )
    app = _make_app(monkeypatch, adaptive_turn, _CapturingTaskRegistry())
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    coll.delete_many({})
    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#test_user"

    conversation_id = "legacy-route-generate-first"
    prompt = "Generate first and join later"
    headers = {"X-Von-Window-Session": "window-123"}
    generate_response = client.post(
        "/von/generate",
        json={
            "prompt": prompt,
            "background": False,
            "model": "gpt-5.4-nano",
            "client_request_id": "legacy-route-generate-first-request",
            "conversation_session_id": conversation_id,
        },
        headers=headers,
    )

    assert generate_response.status_code == 200
    assert coll.count_documents({}) == 1
    generated = coll.find_one({})
    assert generated is not None
    generated_queue_id = generated["queue_id"]
    assert generated["status"] == chat_prompt_queue_service.STATUS_COMPLETED
    assert generated["legacy_submission_generate_seen"] is True
    assert generated["legacy_submission_queue_seen"] is False
    assert "active_legacy_submission_key" in generated

    legacy_payload = {
        "prompt_raw": prompt,
        "session_id": conversation_id,
        "status": "in_progress",
    }
    joined_response = client.post(
        "/von/api/chat_prompt_queue",
        json=legacy_payload,
        headers=headers,
    )

    assert joined_response.status_code == 201
    assert joined_response.get_json()["item"]["queue_id"] == generated_queue_id
    assert joined_response.get_json()["item"]["status"] == "completed"
    assert coll.count_documents({}) == 1
    joined = coll.find_one({"queue_id": generated_queue_id})
    assert joined is not None
    assert joined["legacy_submission_generate_seen"] is True
    assert joined["legacy_submission_queue_seen"] is True
    assert "active_legacy_submission_key" not in joined

    legacy_finish_response = client.post(
        f"/von/api/chat_prompt_queue/{generated_queue_id}/finish",
        json={"status": "completed"},
        headers=headers,
    )
    assert legacy_finish_response.status_code == 200
    assert legacy_finish_response.get_json()["item"]["queue_id"] == generated_queue_id

    later_identical_response = client.post(
        "/von/api/chat_prompt_queue",
        json=legacy_payload,
        headers=headers,
    )
    assert later_identical_response.status_code == 201
    assert later_identical_response.get_json()["item"]["queue_id"] != generated_queue_id
    assert coll.count_documents({}) == 2


def test_pre_upgrade_stale_tab_recovers_scope_across_cold_process_and_joins_old_wire(
    monkeypatch,
):
    import mongomock

    from src.backend.services import organisation_membership_service
    from src.backend.services import window_session_context_service as window_context
    from src.backend.services.window_session_binding_store_service import (
        MongoWindowSessionBindingRepository,
    )

    adaptive_turn = _StubAdaptiveTurn(
        AdaptiveTurnResult(
            response_text="ok",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
        )
    )
    app = _make_app(monkeypatch, adaptive_turn, _CapturingTaskRegistry())
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    coll.delete_many({})

    binding_collection = mongomock.MongoClient()["legacy_restart"]["bindings"]
    binding_repository = MongoWindowSessionBindingRepository(
        ttl_seconds=3_600,
        collection_getter=lambda: binding_collection,
    )
    first_cold_store = window_context.WindowSessionStore(
        binding_repository=binding_repository
    )
    monkeypatch.setattr(window_context, "_window_session_store", first_cold_store)
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda user_id, org_id: {
            "user_concept_id": user_id,
            "organisation_concept_id": org_id,
            "role": "member",
        },
    )

    actor_user_id = "#V#test_user"
    actor_org_id = "#V#test_org"
    actor_namespace = "#V#test_user@test_org"
    conversation_id = "pre-upgrade-stale-tab-conversation"
    stale_window_id = "pre-upgrade-stale-window"
    summary_calls: list[tuple[str, str]] = []

    def owned_conversation_summary(user_id, session_id, **_kwargs):
        summary_calls.append((user_id, session_id))
        return {
            "session_id": session_id,
            "namespace": actor_namespace,
            "organisation_concept_id": actor_org_id,
        }

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_session_summary",
        owned_conversation_summary,
    )

    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = actor_user_id
        flask_session["organisation_concept_id"] = "#V#wrong_flask_org"
        flask_session["org_id"] = "#V#wrong_flask_org"
        flask_session["namespace"] = "#V#test_user@wrong_flask_org"
        flask_session["role_in_org"] = "admin"

    headers = {"X-Von-Window-Session": stale_window_id}
    generate_response = client.post(
        "/von/generate",
        json={
            "prompt": "Recover #V#this stale tab",
            "client_request_id": "pre-upgrade-stale-tab-request",
            "conversation_session_id": conversation_id,
            "user_id": "#V#untrusted_client_actor",
            "org_id": "#V#wrong_flask_org",
            "language": "en-NZ",
            "model": "gpt-5.4-nano",
            "presenter_mode": True,
            "skip_buttonify": False,
            "thinking_card_mode": "standard",
        },
        headers=headers,
    )

    assert generate_response.status_code == 200
    assert summary_calls == [(actor_user_id, conversation_id)]
    persisted_binding = binding_repository.load_owned(stale_window_id, actor_user_id)
    assert persisted_binding is not None
    assert persisted_binding.organisation_concept_id == actor_org_id
    generated = coll.find_one({})
    assert generated is not None
    generated_queue_id = generated["queue_id"]
    assert generated["organisation_concept_id"] == actor_org_id
    assert generated["namespace"] == actor_namespace
    assert generated["legacy_submission_generate_seen"] is True

    second_cold_store = window_context.WindowSessionStore(
        binding_repository=binding_repository
    )
    monkeypatch.setattr(window_context, "_window_session_store", second_cold_store)
    queue_response = client.post(
        "/von/api/chat_prompt_queue",
        json={
            "prompt_raw": "  Recover #V\u200b#this stale tab  ",
            "session_id": conversation_id,
            "session_name": "Recovered stale conversation",
            "status": "in_progress",
        },
        headers=headers,
    )

    assert queue_response.status_code == 201
    assert queue_response.get_json()["item"]["queue_id"] == generated_queue_id
    assert coll.count_documents({}) == 1
    joined = coll.find_one({"queue_id": generated_queue_id})
    assert joined is not None
    assert joined["organisation_concept_id"] == actor_org_id
    assert joined["namespace"] == actor_namespace
    assert joined["legacy_submission_queue_seen"] is True
    assert joined["legacy_submission_generate_seen"] is True
    assert "active_legacy_submission_key" not in joined
    assert second_cold_store.count() == 1
    recovered_context = second_cold_store.get_owned(stale_window_id, actor_user_id)
    assert recovered_context is not None
    assert recovered_context.organisation_concept_id == actor_org_id
    assert adaptive_turn.calls

    finish_response = client.post(
        f"/von/api/chat_prompt_queue/{generated_queue_id}/finish",
        json={"status": "completed"},
        headers=headers,
    )
    assert finish_response.status_code == 200


def test_durable_window_binding_scopes_old_wire_after_cold_process_restart(
    monkeypatch,
):
    import mongomock

    from src.backend.services import organisation_membership_service
    from src.backend.services import window_session_context_service as window_context
    from src.backend.services.window_session_binding_store_service import (
        MongoWindowSessionBindingRepository,
    )

    adaptive_turn = _StubAdaptiveTurn(
        AdaptiveTurnResult(
            response_text="ok",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
        )
    )
    app = _make_app(monkeypatch, adaptive_turn, _CapturingTaskRegistry())
    coll = mongo_client.get_chat_prompt_queue_collection()
    assert coll is not None
    coll.delete_many({})

    binding_collection = mongomock.MongoClient()["durable_restart"]["bindings"]
    binding_repository = MongoWindowSessionBindingRepository(
        ttl_seconds=3_600,
        collection_getter=lambda: binding_collection,
    )
    first_store = window_context.WindowSessionStore(
        binding_repository=binding_repository
    )
    monkeypatch.setattr(window_context, "_window_session_store", first_store)
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda user_id, org_id: {
            "user_concept_id": user_id,
            "organisation_concept_id": org_id,
            "role": "member",
        },
    )

    actor_user_id = "#V#test_user"
    actor_org_id = "#V#test_org"
    actor_namespace = "#V#test_user@test_org"
    window_session_id = "durably-bound-window"
    conversation_id = "durably-bound-conversation"
    window_context.set_window_organisation(
        window_session_id,
        actor_org_id,
        "member",
        actor_namespace,
        user_id=actor_user_id,
    )
    assert binding_collection.count_documents({}) == 1

    restarted_store = window_context.WindowSessionStore(
        binding_repository=binding_repository
    )
    monkeypatch.setattr(window_context, "_window_session_store", restarted_store)
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_session_summary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("durable binding must resolve before conversation recovery")
        ),
    )

    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = actor_user_id
        flask_session["organisation_concept_id"] = "#V#wrong_flask_org"
        flask_session["namespace"] = "#V#test_user@wrong_flask_org"
        flask_session["role_in_org"] = "admin"

    headers = {"X-Von-Window-Session": window_session_id}
    queue_response = client.post(
        "/von/api/chat_prompt_queue",
        json={
            "prompt_raw": "  Use #v\u200b#durable context  ",
            "session_id": conversation_id,
            "session_name": "Durably bound conversation",
            "status": "in_progress",
        },
        headers=headers,
    )
    generate_response = client.post(
        "/von/generate",
        json={
            "prompt": "Use #v#durable context",
            "client_request_id": "durably-bound-old-wire-request",
            "conversation_session_id": conversation_id,
            "user_id": "#V#untrusted_client_actor",
            "org_id": "#V#wrong_flask_org",
            "language": "en-NZ",
            "model": "gpt-5.4-nano",
            "presenter_mode": True,
            "skip_buttonify": False,
            "thinking_card_mode": "standard",
        },
        headers=headers,
    )

    assert queue_response.status_code == 201
    assert generate_response.status_code == 200
    queue_id = queue_response.get_json()["item"]["queue_id"]
    assert coll.count_documents({}) == 1
    joined = coll.find_one({"queue_id": queue_id})
    assert joined is not None
    assert joined["organisation_concept_id"] == actor_org_id
    assert joined["namespace"] == actor_namespace
    assert joined["legacy_submission_queue_seen"] is True
    assert joined["legacy_submission_generate_seen"] is True
    assert "active_legacy_submission_key" not in joined
    assert restarted_store.count() == 1
    assert adaptive_turn.calls

    finish_response = client.post(
        f"/von/api/chat_prompt_queue/{queue_id}/finish",
        json={"status": "completed"},
        headers=headers,
    )
    assert finish_response.status_code == 200


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
    assert payload["namespaced_workflow_launch_inputs_applied"] == ["gmail_max_results"]


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

    model_name, client_type, model_parameters = (
        von_routes._resolve_generate_requested_model(
            {"model": "gpt-5.4-nano"},
            user_concept_id=None,
            org_concept_id=None,
            configured_model=None,
        )
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

    model_name, client_type, model_parameters = (
        von_routes._resolve_generate_requested_model(
            {"model": "openai:[object PointerEvent]"},
            user_concept_id="#V#test_user",
            org_concept_id=None,
            configured_model=None,
        )
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

    model_name, client_type, model_parameters = (
        von_routes._resolve_generate_requested_model(
            {"model": "ollama:llama3.1:8b"},
            user_concept_id="#V#test_user",
            org_concept_id=None,
            configured_model=None,
        )
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

    model_name, client_type, model_parameters = (
        von_routes._resolve_generate_requested_model(
            {"model": "gemma4:e4b", "model_provider": "ollama"},
            user_concept_id="#V#test_user",
            org_concept_id=None,
            configured_model=None,
        )
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

    model_name, client_type, model_parameters = (
        von_routes._resolve_generate_requested_model(
            {},
            user_concept_id="#V#test_user",
            org_concept_id=None,
            configured_model=None,
        )
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
