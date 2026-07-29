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
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_session_state",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "history": [],
            "conversation_situation": None,
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.set_chat_history_conversation_situation",
        lambda **kwargs: {
            "updated": True,
            "matched": True,
            "conflict": False,
            "expected_revision": kwargs["expected_revision"],
            "current_revision": kwargs["expected_revision"] + 1,
            "session_id": kwargs["session_id"],
        },
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


def test_generate_carries_and_updates_inspectable_conversation_situation(
    monkeypatch,
):
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    events: list[tuple[str, object]] = []
    adaptive_calls: list[dict[str, Any]] = []
    set_calls: list[dict[str, Any]] = []
    state_calls: list[dict[str, Any]] = []

    existing_situation = {
        "text": "We are deciding how to represent the paper.",
        "revision": 4,
        "source": "adaptive_turn",
        "updated_by": "#V#user",
        "updated_at": "2026-07-29T08:00:00+00:00",
    }
    observations = [
        {
            "observation_id": "effect:paper-representation",
            "kind": "durable_effect_terminal",
            "summary": "The paper representation completed.",
        }
    ]
    observation_state = {
        "schema_version": "conversation_observation_state.v1",
        "retained_count": 1,
        "total_count": 1,
        "omitted_count": 0,
        "retention_limit": 24,
    }
    canonical_situation = {
        "text": "We are representing the paper; its canonical identity is now known.",
        "revision": 5,
        "source": "adaptive_turn",
        "updated_by": "#V#user",
        "updated_at": "2026-07-29T09:00:00+00:00",
        "source_request_id": "request-from-turn",
    }
    canonical_observations = [
        *observations,
        {
            "observation_id": "identity:paper",
            "kind": "canonical_read_back",
            "summary": "The paper identity is available.",
        },
    ]
    canonical_observation_state = {
        "schema_version": "conversation_observation_state.v1",
        "retained_count": 2,
        "total_count": 2,
        "omitted_count": 0,
        "retention_limit": 24,
    }

    def _session_state(**kwargs: Any) -> dict[str, Any]:
        state_calls.append(dict(kwargs))
        if len(state_calls) == 1:
            return {
                "session_id": kwargs["session_id"],
                "history": [{"role": "user", "content": "Earlier context"}],
                "conversation_situation": existing_situation,
                "conversation_observations": observations,
                "conversation_observation_state": observation_state,
            }
        canonical_situation["source_request_id"] = adaptive_calls[0]["turn_id"]
        return {
            "session_id": kwargs["session_id"],
            "history": [],
            "conversation_situation": canonical_situation,
            "conversation_observations": canonical_observations,
            "conversation_observation_state": canonical_observation_state,
        }

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        _session_state,
    )

    def _add_message(**kwargs: Any) -> None:
        history_calls.append(dict(kwargs))
        events.append(("message", dict(kwargs["message"])))

    def _adaptive(**kwargs: Any) -> AdaptiveTurnResult:
        adaptive_calls.append(dict(kwargs))
        return AdaptiveTurnResult(
            response_text="updated answer",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
            conversation_situation=(
                "We are representing the paper; its canonical identity is now known."
            ),
        )

    def _set_situation(**kwargs: Any) -> dict[str, Any]:
        set_calls.append(dict(kwargs))
        events.append(("situation", kwargs["text"]))
        next_revision = kwargs["expected_revision"] + 1
        return {
            "updated": True,
            "matched": True,
            "conflict": False,
            "expected_revision": kwargs["expected_revision"],
            "current_revision": next_revision,
            "session_id": kwargs["session_id"],
        }

    monkeypatch.setattr(von_routes, "_add_chat_history_message", _add_message)
    monkeypatch.setattr(von_routes, "execute_adaptive_turn", _adaptive)
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "set_chat_history_conversation_situation",
        _set_situation,
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Continue",
            "conversation_session_id": "session-situation",
        },
    )

    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["conversation_situation"] == canonical_situation
    assert body["conversation_observations"] == canonical_observations
    assert (
        body["conversation_observation_state"]
        == canonical_observation_state
    )
    assert adaptive_calls[0]["conversation_id"] == "session-situation"
    assert (
        adaptive_calls[0]["conversation_situation"]
        == existing_situation["text"]
    )
    assert adaptive_calls[0]["conversation_observations"] == observations
    assert any(
        message.get("content") == "Earlier context"
        for message in adaptive_calls[0]["context"]
    )
    assert set_calls == [
        {
            "user_id": "#V#user",
            "session_id": "session-situation",
            "text": (
                "We are representing the paper; its canonical identity is now known."
            ),
            "expected_revision": 4,
            "source": "adaptive_turn",
            "updated_by": "#V#user",
            "namespace": "#V#user@org",
            "source_request_id": adaptive_calls[0]["turn_id"],
        }
    ]
    assert state_calls == [
        {
            "user_id": "#V#user",
            "session_id": "session-situation",
            "namespace": "#V#user@org",
        },
        {
            "user_id": "#V#user",
            "session_id": "session-situation",
            "namespace": "#V#user@org",
            "include_history": False,
        },
    ]
    assistant_event_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "message"
        and isinstance(event[1], dict)
        and event[1].get("role") == "assistant"
    )
    situation_event_index = next(
        index for index, event in enumerate(events) if event[0] == "situation"
    )
    assert assistant_event_index < situation_event_index


def test_generate_returns_answer_when_situation_revision_conflicts(monkeypatch):
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    state_calls: list[dict[str, Any]] = []
    initial_observations = [
        {
            "observation_id": "turn:start",
            "kind": "turn_state",
            "summary": "The turn started from revision 2.",
        }
    ]
    concurrent_observations = [
        {
            "observation_id": "turn:concurrent",
            "kind": "turn_state",
            "summary": "Another turn updated the shared situation.",
        }
    ]
    concurrent_observation_state = {
        "schema_version": "conversation_observation_state.v1",
        "retained_count": 1,
        "total_count": 3,
        "omitted_count": 2,
        "retention_limit": 24,
    }

    def _session_state(**kwargs: Any) -> dict[str, Any]:
        state_calls.append(dict(kwargs))
        if len(state_calls) == 1:
            return {
                "session_id": kwargs["session_id"],
                "history": [],
                "conversation_situation": {
                    "text": "Loaded situation",
                    "revision": 2,
                    "source": "adaptive_turn",
                    "updated_by": "#V#user",
                    "updated_at": "2026-07-29T08:00:00+00:00",
                },
                "conversation_observations": initial_observations,
                "conversation_observation_state": {
                    "schema_version": "conversation_observation_state.v1",
                    "retained_count": 1,
                    "total_count": 1,
                    "omitted_count": 0,
                    "retention_limit": 24,
                },
            }
        return {
            "session_id": kwargs["session_id"],
            "history": [],
            "conversation_situation": {
                "text": "Concurrently updated situation",
                "revision": 3,
                "source": "adaptive_turn",
                "updated_by": "#V#other",
                "updated_at": "2026-07-29T08:01:00+00:00",
            },
            "conversation_observations": concurrent_observations,
            "conversation_observation_state": concurrent_observation_state,
        }

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        _session_state,
    )
    monkeypatch.setattr(
        von_routes,
        "execute_adaptive_turn",
        lambda **_kwargs: AdaptiveTurnResult(
            response_text="useful answer",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
            conversation_situation="Locally updated situation",
        ),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "set_chat_history_conversation_situation",
        lambda **kwargs: {
            "updated": False,
            "matched": True,
            "conflict": True,
            "expected_revision": kwargs["expected_revision"],
            "current_revision": 3,
            "session_id": kwargs["session_id"],
        },
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Continue",
            "conversation_session_id": "session-conflict",
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["response"] == "useful answer"
    assert body["conversation_situation"]["text"] == (
        "Concurrently updated situation"
    )
    assert body["conversation_observations"] == concurrent_observations
    assert (
        body["conversation_observation_state"]
        == concurrent_observation_state
    )
    assert state_calls == [
        {
            "user_id": "#V#user",
            "session_id": "session-conflict",
            "namespace": "#V#user@org",
        },
        {
            "user_id": "#V#user",
            "session_id": "session-conflict",
            "namespace": "#V#user@org",
            "include_history": False,
        },
    ]


def test_generate_preserves_turn_start_carrier_when_read_back_fails(monkeypatch):
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    state_calls: list[dict[str, Any]] = []
    initial_situation = {
        "text": "Loaded situation",
        "revision": 7,
        "source": "adaptive_turn",
        "updated_by": "#V#user",
        "updated_at": "2026-07-29T08:00:00+00:00",
    }
    initial_observations = [
        {
            "observation_id": "turn:start",
            "kind": "turn_state",
            "summary": "This belongs to the loaded situation.",
        }
    ]
    initial_observation_state = {
        "schema_version": "conversation_observation_state.v1",
        "retained_count": 1,
        "total_count": 4,
        "omitted_count": 3,
        "retention_limit": 24,
    }

    def _session_state(**kwargs: Any) -> dict[str, Any]:
        state_calls.append(dict(kwargs))
        if len(state_calls) == 1:
            return {
                "session_id": kwargs["session_id"],
                "history": [],
                "conversation_situation": initial_situation,
                "conversation_observations": initial_observations,
                "conversation_observation_state": initial_observation_state,
            }
        raise RuntimeError("read-back unavailable")

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        _session_state,
    )
    monkeypatch.setattr(
        von_routes,
        "execute_adaptive_turn",
        lambda **_kwargs: AdaptiveTurnResult(
            response_text="useful answer despite read failure",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
            conversation_situation="A newly proposed situation",
        ),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "set_chat_history_conversation_situation",
        lambda **kwargs: {
            "updated": True,
            "matched": True,
            "conflict": False,
            "expected_revision": kwargs["expected_revision"],
            "current_revision": 8,
            "session_id": kwargs["session_id"],
            "conversation_situation": {
                "text": kwargs["text"],
                "revision": 8,
                "source": "adaptive_turn",
                "updated_by": "#V#user",
                "updated_at": "2026-07-29T08:02:00+00:00",
            },
        },
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Continue",
            "conversation_session_id": "session-read-failure",
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["response"] == "useful answer despite read failure"
    assert body["conversation_situation"] == initial_situation
    assert body["conversation_observations"] == initial_observations
    assert body["conversation_observation_state"] == initial_observation_state
    assert state_calls == [
        {
            "user_id": "#V#user",
            "session_id": "session-read-failure",
            "namespace": "#V#user@org",
        },
        {
            "user_id": "#V#user",
            "session_id": "session-read-failure",
            "namespace": "#V#user@org",
            "include_history": False,
        },
    ]


def test_shared_generate_uses_owner_situation_and_owner_storage_copy(monkeypatch):
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    state_calls: list[dict[str, Any]] = []
    invitee_history_calls: list[tuple[Any, ...]] = []

    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: (
            "#V#owner",
            {
                "conversation_owner_user_id": "#V#owner",
                "organisation_concept_id": "#V#org",
            },
        ),
    )

    def _state(**kwargs: Any) -> dict[str, Any]:
        state_calls.append(dict(kwargs))
        return {
            "session_id": kwargs["session_id"],
            "history": [{"role": "assistant", "content": "Owner answer"}],
            "conversation_situation": {
                "text": "Shared situation",
                "revision": 1,
                "source": "adaptive_turn",
                "updated_by": "#V#owner",
                "updated_at": "2026-07-29T08:00:00+00:00",
            },
        }

    def _invitee_history(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        invitee_history_calls.append((*args, kwargs))
        return [{"role": "user", "content": "Invitee contribution"}]

    monkeypatch.setattr(
        von_routes.chat_history_service, "get_chat_history_session_state", _state
    )
    monkeypatch.setattr(
        von_routes.chat_history_service, "get_chat_history", _invitee_history
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Continue the shared work",
            "conversation_session_id": "shared-session",
        },
    )

    assert response.status_code == 200, response.get_json()
    assert state_calls == [
        {
            "user_id": "#V#owner",
            "session_id": "shared-session",
            "namespace": "#V#owner@org",
        },
        {
            "user_id": "#V#owner",
            "session_id": "shared-session",
            "namespace": "#V#owner@org",
            "include_history": False,
        },
    ]
    assert invitee_history_calls == [
        ("#V#user", "shared-session", {"namespace": "#V#user@org"})
    ]
    assert all(call["user_id"] == "#V#owner" for call in history_calls)
    assert all(call["namespace"] == "#V#owner@org" for call in history_calls)
    adaptive_call = app.config["_ADAPTIVE_TURN_CALLS"][0]
    assert adaptive_call["conversation_id"] == "shared-session"
    assert adaptive_call["conversation_situation"] == "Shared situation"
