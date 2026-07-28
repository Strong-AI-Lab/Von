from datetime import datetime, timedelta, timezone

from src.backend.services.conversation_scope_binding_service import (
    TELEMETRY_READ_DELEGATION_AUDIENCE,
    build_conversation_scope_binding,
    build_history_location_binding,
    build_turn_telemetry_binding,
    verify_conversation_scope_binding,
    verify_history_location_binding,
    verify_turn_telemetry_binding,
)


def test_conversation_scope_binding_round_trips() -> None:
    binding = build_conversation_scope_binding(
        chat_session_id="chat-1797-1",
        history_owner_user_id="#V#owner",
        read_namespace="#V#owner@org",
        organisation_concept_id="#V#org",
    )

    verified = verify_conversation_scope_binding(binding)

    assert verified["success"] is True
    assert verified["chat_session_id"] == "chat-1797-1"
    assert verified["history_owner_user_id"] == "#V#owner"
    assert verified["read_namespace"] == "#V#owner@org"
    assert verified["identifier_binding"]["validation_status"] == "verified"
    assert verified["identifier_binding"]["chat_session_id_source"] == (
        "conversation_ref"
    )


def test_history_location_binding_round_trips() -> None:
    binding = build_history_location_binding(
        chat_session_id="chat-1797-2",
        history_index=6,
        history_owner_user_id="#V#owner",
        read_namespace="#V#owner@org",
        organisation_concept_id="#V#org",
    )

    verified = verify_history_location_binding(binding)

    assert verified["success"] is True
    assert verified["chat_session_id"] == "chat-1797-2"
    assert verified["history_index"] == 6
    assert verified["identifier_binding"]["validation_status"] == "verified"
    assert verified["identifier_binding"]["history_index_source"] == (
        "history_location_ref"
    )


def test_tampered_conversation_scope_binding_fails_closed() -> None:
    binding = build_conversation_scope_binding(
        chat_session_id="chat-1797-3",
        history_owner_user_id="#V#owner",
    )
    tampered = dict(binding)
    tampered["chat_session_id"] = "chat-1797-3-tampered"

    verified = verify_conversation_scope_binding(tampered)

    assert verified["success"] is False
    assert verified["error_code"] == "INVALID_CONTEXT_BINDING"
    assert verified["identifier_binding"]["validation_status"] == "binding_id_mismatch"


def test_external_conversation_read_delegation_binds_audience_tool_and_actor() -> None:
    binding = build_conversation_scope_binding(
        chat_session_id="chat-2609-1",
        history_owner_user_id="#V#owner",
        read_namespace="#V#owner@org",
        delegated_actor_user_id="#V#actor_a",
        delegated_actor_namespace="#V#actor_a@org",
        organisation_concept_id="#V#org",
        permitted_tools=("conversation_telemetry_get_locator",),
    )

    verified = verify_conversation_scope_binding(
        binding,
        require_read_delegation=True,
        expected_audience=TELEMETRY_READ_DELEGATION_AUDIENCE,
        expected_tool="conversation_telemetry_get_locator",
        expected_actor_user_id="#V#actor_a",
    )

    assert verified["success"] is True
    assert verified["delegated_actor_user_id"] == "#V#actor_a"
    assert verified["delegated_actor_namespace"] == "#V#actor_a@org"
    assert verified["read_delegation"]["permitted_tools"] == [
        "conversation_telemetry_get_locator"
    ]
    assert verified["identifier_binding"]["read_delegation_validation_status"] == (
        "verified"
    )

    wrong_tool = verify_conversation_scope_binding(
        binding,
        require_read_delegation=True,
        expected_audience=TELEMETRY_READ_DELEGATION_AUDIENCE,
        expected_tool="chat_history_get_debug_entry",
        expected_actor_user_id="#V#actor_a",
    )
    wrong_actor = verify_conversation_scope_binding(
        binding,
        require_read_delegation=True,
        expected_audience=TELEMETRY_READ_DELEGATION_AUDIENCE,
        expected_tool="conversation_telemetry_get_locator",
        expected_actor_user_id="#V#actor_b",
    )
    wrong_audience = verify_conversation_scope_binding(
        binding,
        require_read_delegation=True,
        expected_audience="another_mcp_surface",
        expected_tool="conversation_telemetry_get_locator",
        expected_actor_user_id="#V#actor_a",
    )

    assert wrong_tool["error_code"] == "READ_DELEGATION_TOOL_MISMATCH"
    assert wrong_actor["error_code"] == "READ_DELEGATION_ACTOR_MISMATCH"
    assert wrong_audience["error_code"] == "READ_DELEGATION_AUDIENCE_MISMATCH"


def test_expired_read_delegation_fails_closed() -> None:
    issued_at = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    binding = build_history_location_binding(
        chat_session_id="chat-2609-expired",
        history_index=4,
        history_owner_user_id="#V#actor_a",
        delegated_actor_user_id="#V#actor_a",
        ttl_seconds=30,
        issued_at=issued_at,
    )

    verified = verify_history_location_binding(
        binding,
        require_read_delegation=True,
        expected_audience=TELEMETRY_READ_DELEGATION_AUDIENCE,
        expected_tool="chat_history_get_debug_entry",
        expected_actor_user_id="#V#actor_a",
        now=issued_at + timedelta(seconds=31),
    )

    assert verified["success"] is False
    assert verified["error_code"] == "READ_DELEGATION_EXPIRED"


def test_turn_telemetry_delegation_binds_exact_request_and_tool() -> None:
    binding = build_turn_telemetry_binding(
        request_id="req-2609-1",
        delegated_actor_user_id="#V#actor_a",
        permitted_tool="turn_execution_get_diagnostics",
        chat_session_id="chat-2609-2",
        history_index=7,
        history_owner_user_id="#V#actor_a",
        read_namespace="#V#actor_a@org",
        organisation_concept_id="#V#org",
    )

    verified = verify_turn_telemetry_binding(
        binding,
        expected_tool="turn_execution_get_diagnostics",
        expected_actor_user_id="#V#actor_a",
    )
    wrong_tool = verify_turn_telemetry_binding(
        binding,
        expected_tool="turn_execution_get_live_progress",
        expected_actor_user_id="#V#actor_a",
    )

    assert verified["success"] is True
    assert verified["request_id"] == "req-2609-1"
    assert verified["chat_session_id"] == "chat-2609-2"
    assert verified["history_index"] == 7
    assert wrong_tool["error_code"] == "READ_DELEGATION_TOOL_MISMATCH"
