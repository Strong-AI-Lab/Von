from src.backend.services.conversation_scope_binding_service import (
    build_conversation_scope_binding,
    build_history_location_binding,
    verify_conversation_scope_binding,
    verify_history_location_binding,
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
