from __future__ import annotations

from typing import Any

import pytest

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
        "src.backend.security.access_control.get_effective_user_concept_id_with_source",
        lambda: ("#V#user", "session"),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda window_session_id, session_snapshot, user_concept_id, **_kwargs: {
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
    monkeypatch.setattr(
        "src.backend.services.mail_profile_turn_scope_service.build_gmail_profile_turn_scope",
        lambda **_kwargs: {
            "success": False,
            "reason_code": "mail_profile_actor_not_represented",
            "profile_id": None,
            "choices": [],
        },
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


def test_generate_assistant_opening_uses_empty_prompt_and_persists_only_assistant(
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    completed: list[dict[str, object]] = []

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_session_focus",
        lambda **_kwargs: {"focal_concept_ids": []},
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "claim_chat_session_assistant_opening",
        lambda **_kwargs: {"status": "claimed", "initiation_id": "opening-1"},
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "complete_chat_session_assistant_opening",
        lambda **kwargs: completed.append(dict(kwargs)) or True,
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "",
            "conversation_session_id": "opening-session",
            "turn_kind": "assistant_opening",
            "initiation_id": "opening-1",
        },
    )

    assert response.status_code == 200, response.get_json()
    adaptive_call = app.config["_ADAPTIVE_TURN_CALLS"][0]
    assert adaptive_call["prompt"] == ""
    persisted_messages: list[dict[str, object]] = []
    for call in history_calls:
        message = call.get("message")
        assert isinstance(message, dict)
        persisted_messages.append(message)
    assert not [
        message for message in persisted_messages if message.get("role") == "user"
    ]
    assistant_messages = [
        message
        for message in persisted_messages
        if message.get("role") == "assistant"
    ]
    assert assistant_messages == [
        {
            "role": "assistant",
            "content": "ok",
            "image_attachments": [],
            "turn_kind": "assistant_opening",
            "initiation_id": "opening-1",
            "turn_id": "a-" + app.config["_ADAPTIVE_TURN_CALLS"][0]["turn_id"],
        }
    ]
    assert completed == [
        {
            "user_id": "#V#user",
            "session_id": "opening-session",
            "initiation_id": "opening-1",
            "response_text": "ok",
            "namespace": "#V#user@org",
        }
    ]


def test_generate_uses_stored_focus_and_ignores_client_focus(monkeypatch) -> None:
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    projected: list[object] = []

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "history": [],
            "conversation_situation": None,
            "focal_concept_ids": ["#V#stored_task"],
        },
    )

    def _project_focus(focal_ids):  # noqa: ANN001
        projected.append(focal_ids)
        return [], []

    monkeypatch.setattr(
        von_routes,
        "_build_focal_conversation_runtime_envelope",
        _project_focus,
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "What should I do next?",
            "conversation_session_id": "focused-session",
            "focal_concept_ids": ["#V#client_injected_object"],
        },
    )

    assert response.status_code == 200, response.get_json()
    assert projected == [["#V#stored_task"]]


def test_generate_shared_focus_access_change_is_generic_and_blocks_model(
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: (
            "#V#owner",
            {"session_id": "shared-focused", "inviter_user_id": "#V#owner"},
        ),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "history": [],
            "conversation_situation": None,
            "focal_concept_ids": ["#V#private_owner_task"],
        },
    )
    monkeypatch.setattr(
        von_routes,
        "_build_focal_conversation_runtime_envelope",
        lambda _focal_ids: ([], ["#V#private_owner_task"]),
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Continue",
            "conversation_session_id": "shared-focused",
        },
    )

    assert response.status_code == 403
    assert response.get_json() == {"error": "focused_conversation_access_changed"}
    assert "private_owner_task" not in response.get_data(as_text=True)
    assert app.config["_ADAPTIVE_TURN_CALLS"] == []
    assert history_calls == []


def test_focal_runtime_envelope_uses_actor_effective_text_view(monkeypatch) -> None:
    from src.backend.server.routes import von_routes

    monkeypatch.setattr(
        von_routes,
        "_authorised_focal_concepts",
        lambda _ids: (
            ["#V#task"],
            [{"concept_id": "#V#task", "name": "Task", "type_ids": []}],
            [],
        ),
    )
    calls: list[dict[str, Any]] = []

    def _get_texts(concept_id: str, **kwargs: Any) -> list[dict[str, str]]:
        calls.append({"concept_id": concept_id, **kwargs})
        if kwargs.get("context_view") == "actor_effective":
            return [{"text": "Visible task note"}]
        return [{"text": "PRIVATE OTHER ACTOR NOTE"}]

    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        _get_texts,
    )

    messages, unavailable = von_routes._build_focal_conversation_runtime_envelope(
        ["#V#task"]
    )

    assert unavailable == []
    assert calls == [
        {
            "concept_id": "#V#task",
            "limit": 8,
            "context_view": "actor_effective",
        }
    ]
    assert "Visible task note" in messages[0]["content"]
    assert "PRIVATE OTHER ACTOR NOTE" not in messages[0]["content"]


def test_failed_assistant_opening_is_made_retryable(monkeypatch) -> None:
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    failures: list[dict[str, object]] = []
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_session_focus",
        lambda **_kwargs: {"focal_concept_ids": []},
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "claim_chat_session_assistant_opening",
        lambda **_kwargs: {"status": "claimed", "initiation_id": "opening-fail"},
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "fail_chat_session_assistant_opening",
        lambda **kwargs: failures.append(dict(kwargs)) or True,
    )
    monkeypatch.setattr(
        von_routes,
        "execute_adaptive_turn",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("model unavailable")),
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "",
            "conversation_session_id": "opening-session",
            "turn_kind": "assistant_opening",
            "initiation_id": "opening-fail",
        },
    )

    assert response.status_code == 500
    assert failures == [
        {
            "user_id": "#V#user",
            "session_id": "opening-session",
            "initiation_id": "opening-fail",
            "failure_kind": "RuntimeError",
            "namespace": "#V#user@org",
        }
    ]


def test_generate_reauthorises_shared_conversation_mailbox_memory_for_actor(
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes
    from src.backend.services import mail_profile_turn_scope_service
    from src.backend.services.conversation_turn_memory_context_service import (
        merge_conversation_situation_turn_projection,
    )

    real_build_gmail_profile_turn_scope = (
        mail_profile_turn_scope_service.build_gmail_profile_turn_scope
    )
    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    scope_calls: list[dict[str, Any]] = []

    shared_situation = merge_conversation_situation_turn_projection(
        current_situation=None,
        model_situation=None,
        projection={
            "request_id": "owner-mail-turn",
            "terminal_status": "completed",
            "provenance": "canonical_turn_tool_records",
            "selected_referents": [
                {
                    "schema_version": "selected_referent_capsule.v1",
                    "stable_id": "owner-message-id",
                    "source_kind": "#V#gmail_message_result_entity_type",
                    "capability_kind": "mcp_tool",
                    "capability_name": "gmail_get_message",
                    "display_label": "Owner's recent message",
                    "resource_scope": {
                        "source_family": "gmail",
                        "resource_id": "#V#gmail_profile_owner_personal",
                        "runtime_alias": "owner-personal",
                        "display_label": "owner@example.test",
                        "selection_source": "request",
                    },
                }
            ],
        },
    )
    assert shared_situation is not None

    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: (
            "#V#owner",
            {"organisation_concept_id": "#V#org"},
        ),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "history": [],
            "conversation_situation": {
                "text": shared_situation,
                "revision": 2,
                "source": "adaptive_turn",
                "updated_by": "#V#owner",
            },
            "conversation_observations": [],
        },
    )

    monkeypatch.setattr(
        mail_profile_turn_scope_service,
        "list_authorised_gmail_profiles_for_user",
        lambda **_kwargs: {
            "success": True,
            "reason_code": "authorised_mail_profiles_resolved",
            "profiles": [
                {
                    "profile_id": "actor-personal",
                    "profile_resource_concept_id": (
                        "#V#gmail_profile_actor_personal"
                    ),
                    "is_default": True,
                    "represented_identity_concept_ids": ["#V#user"],
                }
            ],
        },
    )
    monkeypatch.setattr(
        mail_profile_turn_scope_service,
        "load_profiles_from_env",
        lambda: {"actor-personal": object(), "owner-personal": object()},
    )
    monkeypatch.setattr(
        mail_profile_turn_scope_service,
        "list_profile_summaries",
        lambda _profiles: [
            {
                "profile_id": "actor-personal",
                "authorised_email": "actor@example.test",
            },
            {
                "profile_id": "owner-personal",
                "authorised_email": "owner@example.test",
            },
        ],
    )

    def _build_scope(**kwargs: Any) -> dict[str, Any]:
        scope_calls.append(dict(kwargs))
        return real_build_gmail_profile_turn_scope(**kwargs)

    monkeypatch.setattr(
        "src.backend.services.mail_profile_turn_scope_service.build_gmail_profile_turn_scope",
        _build_scope,
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "What else should I do about that email?",
            "conversation_session_id": "shared-mail-session",
        },
    )

    assert response.status_code == 200, response.get_json()
    assert scope_calls == [
        {
            "user_concept_id": "#V#user",
            "requested_profile_id": None,
            "remembered_resource_scope": {
                "source_family": "gmail",
                "resource_id": "#V#gmail_profile_owner_personal",
                "runtime_alias": "owner-personal",
                "display_label": "owner@example.test",
                "selection_source": "request",
            },
        }
    ]
    adaptive_call = app.config["_ADAPTIVE_TURN_CALLS"][0]
    trusted = adaptive_call["trusted_argument_values"]["gmail_profile"]
    assert trusted["default_selector"] == "#V#gmail_profile_actor_personal"
    assert trusted["default_selection_source"] == "represented_default"
    assert [choice["value"] for choice in trusted["choices"]] == [
        "actor-personal"
    ]


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


def test_generate_preserves_explicit_gmail_profile_authority_denial(
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    session_calls: list[dict[str, Any]] = []
    profile_calls: list[dict[str, Any]] = []

    def _deny_profile(**kwargs: Any) -> dict[str, Any]:
        profile_calls.append(dict(kwargs))
        return {
            "success": False,
            "reason_code": "mail_profile_not_authorised_for_actor",
            "profile_id": None,
            "choices": [],
        }

    monkeypatch.setattr(
        "src.backend.services.mail_profile_turn_scope_service.build_gmail_profile_turn_scope",
        _deny_profile,
    )
    monkeypatch.setattr(
        von_routes,
        "_ensure_generate_conversation_session",
        lambda **kwargs: (
            session_calls.append(dict(kwargs))
            or ("must-not-be-created", None, True)
        ),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("history must not load before explicit profile denial")
        ),
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Check recent mail",
            "conversation_session_id": "explicit-profile-denial",
            "gmail_profile": "somebody-elses-mail",
        },
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "error": "gmail_profile_not_authorised",
        "detail": (
            "The requested Gmail profile is not represented as authorised "
            "for the authenticated actor."
        ),
    }
    assert profile_calls == [
        {
            "user_concept_id": "#V#user",
            "requested_profile_id": "somebody-elses-mail",
            "remembered_resource_scope": None,
        }
    ]
    assert session_calls == []


def test_generate_resolves_explicit_gmail_profile_once_before_situation_load(
    monkeypatch,
) -> None:
    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    profile_calls: list[dict[str, Any]] = []

    def _allow_profile(**kwargs: Any) -> dict[str, Any]:
        profile_calls.append(dict(kwargs))
        return {
            "success": True,
            "reason_code": "authorised_mail_profile_choices_resolved",
            "profile_id": "personal",
            "trusted_argument_choice": {
                "schema_version": "trusted_argument_choice.v1",
                "default_selector": "#V#gmail_profile_personal",
                "default_selection_source": "request",
                "choices": [
                    {
                        "selector": "#V#gmail_profile_personal",
                        "value": "personal",
                        "source_family": "gmail",
                        "resource_id": "#V#gmail_profile_personal",
                        "runtime_alias": "personal",
                        "display_label": "me@example.test",
                    }
                ],
            },
        }

    monkeypatch.setattr(
        "src.backend.services.mail_profile_turn_scope_service.build_gmail_profile_turn_scope",
        _allow_profile,
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Check recent mail",
            "conversation_session_id": "explicit-profile-success",
            "gmail_profile": "personal",
        },
    )

    assert response.status_code == 200, response.get_json()
    assert profile_calls == [
        {
            "user_concept_id": "#V#user",
            "requested_profile_id": "personal",
            "remembered_resource_scope": None,
        }
    ]
    trusted = app.config["_ADAPTIVE_TURN_CALLS"][0]["trusted_argument_values"]
    assert trusted["gmail_profile"]["default_selection_source"] == "request"


def test_generate_creates_and_binds_chat_session_when_window_scope_has_no_active_session(
    monkeypatch,
):
    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda window_session_id, session_snapshot, user_concept_id, **_kwargs: {
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
            "session_name": None,
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
    assert body["conversation_session_name"] is None

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


@pytest.mark.parametrize("checkpoint_revision", [None, 6])
def test_generate_carries_and_updates_inspectable_conversation_situation(
    monkeypatch,
    checkpoint_revision,
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
        "revision": (checkpoint_revision if checkpoint_revision is not None else 4) + 1,
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
            conversation_situation_revision=checkpoint_revision,
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
    assert adaptive_calls[0]["conversation_situation_revision"] == 4
    assert adaptive_calls[0]["conversation_history_owner_user_id"] == "#V#user"
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
            "expected_revision": (
                checkpoint_revision if checkpoint_revision is not None else 4
            ),
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


def test_generate_persists_runtime_situation_when_model_sidecar_is_omitted(
    monkeypatch,
):
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    set_calls: list[dict[str, Any]] = []
    persisted_text: dict[str, str] = {}

    def _session_state(**kwargs: Any) -> dict[str, Any]:
        text = persisted_text.get("text")
        return {
            "session_id": kwargs["session_id"],
            "history": [],
            "conversation_situation": (
                {
                    "text": text,
                    "revision": 1,
                    "source": "adaptive_turn",
                    "updated_by": "#V#user",
                    "updated_at": "2026-08-10T22:30:00+00:00",
                    "source_request_id": set_calls[0]["source_request_id"],
                }
                if text
                else None
            ),
            "conversation_observations": [],
        }

    def _set_situation(**kwargs: Any) -> dict[str, Any]:
        set_calls.append(dict(kwargs))
        persisted_text["text"] = kwargs["text"]
        return {
            "updated": True,
            "matched": True,
            "conflict": False,
            "expected_revision": kwargs["expected_revision"],
            "current_revision": kwargs["expected_revision"] + 1,
            "session_id": kwargs["session_id"],
        }

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        _session_state,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "set_chat_history_conversation_situation",
        _set_situation,
    )
    monkeypatch.setattr(
        von_routes,
        "execute_adaptive_turn",
        lambda **_kwargs: AdaptiveTurnResult(
            response_text="The design uses #V#trip and #V#has_trip_component.",
            extra_messages=(),
            tool_invocations=(
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "payload": {
                        "name": "search_concepts",
                        "arguments": {"query": "trip"},
                    },
                    "evidence": {
                        "schema_version": "turn_evidence_envelope.v1",
                        "status": "ok",
                        "projected_payload": {
                            "results": [
                                {"concept_id": "#V#trip"},
                                {"concept_id": "#V#has_trip_component"},
                                {"concept_id": "#V#unselected"},
                            ],
                        },
                    },
                },
            ),
            aux_llm_calls=(),
            conversation_situation=None,
        ),
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "What is the smallest existing trip design?",
            "conversation_session_id": "session-runtime-situation",
        },
    )

    assert response.status_code == 200, response.get_json()
    assert len(set_calls) == 1
    assert set_calls[0]["expected_revision"] == 0
    assert set_calls[0]["source"] == "adaptive_turn"
    assert set_calls[0]["source_request_id"]
    assert (
        "verified concept ids: #V#trip, #V#has_trip_component" in set_calls[0]["text"]
    )
    assert "#V#unselected" not in set_calls[0]["text"]
    assert "turn request id: " in set_calls[0]["text"]
    body = response.get_json()
    assert body["conversation_situation"]["text"] == set_calls[0]["text"]
    assert (
        body["conversation_situation"]["source_request_id"]
        == set_calls[0]["source_request_id"]
    )


def test_generate_carries_selected_referent_capsule_to_natural_follow_up(
    monkeypatch,
):
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    adaptive_calls: list[dict[str, Any]] = []
    stored: dict[str, Any] = {"text": None, "revision": 0, "request_id": None}

    def _session_state(**kwargs: Any) -> dict[str, Any]:
        situation = (
            {
                "text": stored["text"],
                "revision": stored["revision"],
                "source": "adaptive_turn",
                "updated_by": "#V#user",
                "updated_at": "2026-08-11T09:00:00+00:00",
                "source_request_id": stored["request_id"],
            }
            if stored["text"]
            else None
        )
        return {
            "session_id": kwargs["session_id"],
            "history": [],
            "conversation_situation": situation,
            "conversation_observations": [],
        }

    def _set_situation(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["expected_revision"] == stored["revision"]
        stored["text"] = kwargs["text"]
        stored["revision"] += 1
        stored["request_id"] = kwargs["source_request_id"]
        return {
            "updated": True,
            "matched": True,
            "conflict": False,
            "expected_revision": kwargs["expected_revision"],
            "current_revision": stored["revision"],
            "session_id": kwargs["session_id"],
        }

    def _adaptive(**kwargs: Any) -> AdaptiveTurnResult:
        adaptive_calls.append(dict(kwargs))
        if len(adaptive_calls) > 1:
            return AdaptiveTurnResult(
                response_text="Open it in the official app and check in.",
                extra_messages=(),
                tool_invocations=(),
                aux_llm_calls=(),
            )
        return AdaptiveTurnResult(
            response_text=(
                "Pay attention to the easyJet booking KD5BJTT first; the "
                "flight is imminent."
            ),
            extra_messages=(),
            tool_invocations=(
                {
                    "tool": "mail_get_item",
                    "capability_kind": "mcp_tool",
                    "call_id": "call-easyjet",
                    "status": "ok",
                    "resource_scope": {
                        "source_family": "mail",
                        "resource_id": "#V#mail_profile_michael_personal",
                        "runtime_alias": "michael-personal",
                        "display_label": "Michael's personal email",
                        "selection_source": "conversation_default",
                    },
                    "evidence": {
                        "evidence_id": "evidence-easyjet",
                        "status": "ok",
                        "projected_payload": {
                            "body": "Private body must not be persisted.",
                            "_tool_evidence_projection": {
                                "tool_concept_id": "#V#mail_get_item_tool",
                                "referent_candidates": [
                                    {
                                        "stable_id": "19fece69a5839e69",
                                        "display_label": (
                                            "easyJet booking KD5BJTT"
                                        ),
                                        "source_kind": (
                                            "#V#mail_message_result_entity_type"
                                        ),
                                        "identity_field_concept_id": (
                                            "#V#mail_message_id_field"
                                        ),
                                    }
                                ],
                            },
                        },
                    },
                },
            ),
            aux_llm_calls=(),
            conversation_situation=None,
        )

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        _session_state,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "set_chat_history_conversation_situation",
        _set_situation,
    )
    monkeypatch.setattr(von_routes, "execute_adaptive_turn", _adaptive)

    first = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Which recent email should I pay attention to first?",
            "conversation_session_id": "session-selected-referent",
        },
    )
    second = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "What should I do about that one?",
            "conversation_session_id": "session-selected-referent",
        },
    )

    assert first.status_code == 200, first.get_json()
    assert second.status_code == 200, second.get_json()
    assert len(adaptive_calls) == 2
    follow_up_situation = adaptive_calls[1]["conversation_situation"]
    assert "selected referent capsule:" in follow_up_situation
    assert "19fece69a5839e69" in follow_up_situation
    assert "easyJet booking KD5BJTT" in follow_up_situation
    assert "michael-personal" in follow_up_situation
    assert "Private body" not in follow_up_situation


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


def test_generate_rejects_new_turn_in_imported_read_only_conversation(monkeypatch):
    from src.backend.server.routes import von_routes

    history_calls: list[dict[str, object]] = []
    app = _make_app(monkeypatch, history_calls)
    monkeypatch.setattr(
        von_routes,
        "_get_effective_context_with_owned_conversation_recovery",
        lambda **_kwargs: {
            "organisation_id": "#V#org",
            "namespace": "#V#user@org",
            "chat_session_id": "external-session",
            "role": "member",
            "source": "window_session",
        },
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "is_external_conversation_read_only",
        lambda **_kwargs: True,
    )

    response = app.test_client().post(
        "/von/generate",
        json={
            "prompt": "Continue here",
            "conversation_session_id": "external-session",
        },
        headers={"X-Von-Window-Session": "window-identity"},
    )

    assert response.status_code == 409
    assert response.get_json()["error_code"] == "external_conversation_read_only"
    assert app.config["_ADAPTIVE_TURN_CALLS"] == []
    assert history_calls == []
