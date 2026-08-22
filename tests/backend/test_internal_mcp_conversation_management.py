from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import (
    bind_internal_mcp_actor_context_source,
)
from src.backend.integrations.internal_mcp.schemas import validate_payload


def _assert_success_schema(method: str, payload: dict) -> None:
    definition = build_default_catalogue().get(method)
    assert definition is not None
    assert definition.output_schema is not None
    ok, errors = validate_payload(definition.output_schema, payload)
    assert ok, f"{method} output schema mismatch: {errors}"


def test_conversation_tools_are_actor_bound_and_manage_is_a_bounded_effect():
    catalogue = build_default_catalogue()

    for name in ("conversation_list", "conversation_get"):
        definition = catalogue.get(name)
        assert definition.category == "read"
        assert definition.ordinary_turn_trusted_argument_bindings == {
            "acting_user_concept_id": "actor_user_concept_id",
            "organisation_concept_id": "actor_organisation_concept_id",
            "namespace": "turn_namespace",
        }

    manage = catalogue.get("conversation_manage")
    assert manage.category == "write"
    assert manage.ordinary_turn_effect is True
    assert manage.ordinary_turn_trusted_argument_bindings == {
        "acting_user_concept_id": "actor_user_concept_id",
        "organisation_concept_id": "actor_organisation_concept_id",
        "namespace": "turn_namespace",
        "request_id": "turn_id",
    }


def test_conversation_list_uses_trusted_operator_actor_scope(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import conversation_management_service

    captured: dict = {}

    def _list_actor_conversations(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "conversations": [],
            "count": 0,
            "limit": kwargs["limit"],
            "include_hidden": kwargs["include_hidden"],
            "hidden_count": 0,
            "has_more": False,
            "warnings": [],
        }

    monkeypatch.setattr(
        conversation_management_service,
        "list_actor_conversations",
        _list_actor_conversations,
    )

    with bind_internal_mcp_actor_context_source("trusted_operator_payload_fallback"):
        payload = catalogue._conversation_list(
            acting_user_concept_id="#V#alice",
            organisation_concept_id="#V#org",
            namespace="#V#alice@org",
            limit=7,
            include_hidden=True,
        )

    assert payload["success"] is True
    assert captured == {
        "actor_user_id": "#V#alice",
        "namespace": "#V#alice@org",
        "organisation_concept_id": "#V#org",
        "limit": 7,
        "include_hidden": True,
    }
    _assert_success_schema("conversation_list", payload)


def test_conversation_get_reuses_bounded_carrier_and_names_its_recovery_tool(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    captured: dict = {}

    def _history(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "session_id": kwargs["session_id"],
            "segments": [[{"role": "user", "content": "Hello"}]],
            "history_coverage": {
                "recovery": {"tool_name": "chat_history_get_segments"}
            },
        }

    monkeypatch.setattr(
        catalogue,
        "_chat_history_get_segments",
        _history,
    )

    payload = catalogue._conversation_get(session_id="session-1")

    assert payload["segments"][0][0]["content"] == "Hello"
    assert payload["history_coverage"]["recovery"]["tool_name"] == "conversation_get"
    assert captured["include_debug"] is False
    assert captured["history_tail_limit"] == 24
    assert captured["segment_size"] == 20
    assert payload["bounded_read"]["has_more"] is False
    _assert_success_schema("conversation_get", payload)


def test_conversation_get_pages_oversized_carrier_without_debug_payload(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        catalogue,
        "_chat_history_get_segments",
        lambda **kwargs: {
            "success": True,
            "session_id": kwargs["session_id"],
            "session_name": "Large research conversation",
            "last_message_at": "2026-08-21T10:30:00+00:00",
            "created_at": "2026-08-20T08:00:00+00:00",
            "segments": [[{"role": "user", "content": "x" * 90_000}]],
            "history_coverage": {"history_truncated": False},
        },
    )

    payload = catalogue._conversation_get(
        session_id="session-large",
        history_tail_limit=10_000,
        segment_size=10_000,
    )

    assert "segments" not in payload
    assert payload["artifact_kind"] == "conversation"
    assert payload["bounded_read"]["has_more"] is True
    assert payload["bounded_read"]["next_offset"] is not None
    assert payload["bounded_read"]["total_chars"] > 90_000
    assert payload["session_name"] == "Large research conversation"
    assert payload["last_message_at"] == "2026-08-21T10:30:00+00:00"
    assert payload["created_at"] == "2026-08-20T08:00:00+00:00"


def test_conversation_get_applies_actor_visible_name_to_shared_history(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import (
        chat_history_service,
        conversation_management_service,
    )

    monkeypatch.setattr(
        catalogue,
        "_authorise_internal_mcp_telemetry_read",
        lambda payload, **_kwargs: {
            "success": True,
            "payload": dict(payload),
            "delegated": False,
            "read_delegation": None,
        },
    )
    monkeypatch.setattr(
        catalogue,
        "_resolve_chat_history_read_target",
        lambda _payload: {
            "success": True,
            "session_id": "shared-session",
            "chat_session_id": "shared-session",
            "read_user_id": "#V#owner",
            "requested_user_id": "#V#alice",
            "read_namespace": "#V#owner@org",
            "access_mode": "invitee",
            "identifier_binding": {"mode": "raw_parameters"},
        },
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_segments",
        lambda *args, **kwargs: (
            [[{"role": "assistant", "content": "Shared result"}]],
            {
                "history_truncated": False,
                "session_name": "Owner label",
                "last_message_at": "2026-08-21T10:30:00+00:00",
                "created_at": "2026-08-20T08:00:00+00:00",
            },
        ),
    )
    monkeypatch.setattr(
        conversation_management_service,
        "apply_conversation_preferences",
        lambda *, actor_user_id, conversations: [
            {
                **conversations[0],
                "session_name": "Alice's research label",
                "session_name_source": "actor_preference",
            }
        ],
    )

    payload = catalogue._conversation_get(
        session_id="shared-session",
        user_concept_id="#V#alice",
    )

    assert payload["session_name"] == "Alice's research label"
    assert payload["session_name_source"] == "actor_preference"
    assert payload["last_message_at"] == "2026-08-21T10:30:00+00:00"
    assert payload["created_at"] == "2026-08-20T08:00:00+00:00"
    _assert_success_schema("conversation_get", payload)


def test_conversation_manage_rename_returns_canonical_read_back(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import chat_history_service

    monkeypatch.setattr(
        catalogue,
        "_resolve_chat_history_read_target",
        lambda _payload: {
            "success": True,
            "session_id": "session-1",
            "requested_user_id": "#V#alice",
            "read_user_id": "#V#alice",
            "read_namespace": "#V#alice@org",
            "access_mode": "owner",
        },
    )
    monkeypatch.setattr(
        chat_history_service,
        "rename_chat_session",
        lambda **kwargs: {
            "matched": True,
            "updated": True,
            "session_name": kwargs["session_name"],
        },
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_session_summary",
        lambda *args, **kwargs: {
            "session_id": "session-1",
            "session_name": "Specific label",
            "namespace": "#V#alice@org",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.conversation_management_service.apply_conversation_preferences",
        lambda *, actor_user_id, conversations: [
            {
                **conversations[0],
                "hidden": False,
                "pinned": False,
                "conversation_preference": {
                    "session_id": "session-1",
                    "preference_present": False,
                    "hidden": False,
                    "pinned": False,
                },
            }
        ],
    )

    payload = catalogue._conversation_manage(
        action="rename",
        session_id="session-1",
        session_name="Specific label",
        acting_user_concept_id="#V#alice",
        request_id="turn-1",
    )

    assert payload["success"] is True
    assert payload["effect_status"] == "succeeded"
    assert payload["changed"] is True
    assert payload["canonical_read_back"]["session_name"] == "Specific label"
    assert payload["canonical_read_back"]["hidden"] is False
    assert payload["request_id"] == "turn-1"
    _assert_success_schema("conversation_manage", payload)


def test_conversation_manage_rejects_foreign_conversation_without_invite(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import shared_conversation_service

    monkeypatch.setattr(
        shared_conversation_service,
        "resolve_conversation_owner",
        lambda **_kwargs: "#V#bob",
    )
    monkeypatch.setattr(
        shared_conversation_service,
        "get_accepted_invite_for_user_session",
        lambda **_kwargs: None,
    )

    with bind_internal_mcp_actor_context_source("trusted_operator_payload_fallback"):
        payload = catalogue._conversation_manage(
            action="hide",
            session_id="bob-session",
            acting_user_concept_id="#V#alice",
            organisation_concept_id="#V#org",
            namespace="#V#alice@org",
        )

    assert payload["success"] is False
    assert payload["error_code"] == "PERMISSION_DENIED"
