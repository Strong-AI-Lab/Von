"""Gateway-backed regressions for conversation locator/read tools.

These tests exercise the real InternalMCPGateway.invoke() path so identifier
binding mistakes fail closed as structured binding errors rather than falling
through to permission checks.
"""

from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.schemas import validate_payload
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.services.conversation_scope_binding_service import (
    build_conversation_scope_binding,
    build_history_location_binding,
)


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _emitted_von_conversation_ref() -> dict:
    return {
        "kind": "von_conversation_ref",
        "conversation_ref": {
            "session_id": "ff9be41d-28f8-4864-ab0b-8c201e3152d0",
            "user_concept_id": "#V#michael_witbrock",
            "namespace": (
                "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
            ),
            "organisation_concept_id": "university_of_auckland_strong_ai_lab",
            "include_legacy": False,
        },
        "chat_history_lookup": {
            "user_id": "#V#michael_witbrock",
            "session_id": "ff9be41d-28f8-4864-ab0b-8c201e3152d0",
            "namespace": (
                "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
            ),
            "include_legacy": False,
        },
    }


def _assert_schema_conformance(
    gateway: InternalMCPGateway,
    method: str,
    payload: dict,
) -> None:
    definition = gateway.get_method_definition(method)
    assert definition is not None
    if definition.output_schema is None:
        return
    ok, errors = validate_payload(definition.output_schema, payload)
    assert ok, f"{method} output schema mismatch: {errors}"


def test_chat_history_get_segments_gateway_accepts_bound_conversation_ref(
    monkeypatch,
) -> None:
    gateway = _build_gateway()
    conversation_ref = build_conversation_scope_binding(
        chat_session_id="chat-bound-gateway-1",
        history_owner_user_id="#V#owner",
        read_namespace="#V#owner@org",
        organisation_concept_id="#V#org",
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda session_id: "#V#owner",
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda user_id, session_id, namespace=None, include_legacy=True: (
            user_id == "#V#owner" and session_id == "chat-bound-gateway-1"
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_segments",
        lambda *args, **kwargs: ([{"segment_index": 0, "history": []}], {}),
    )

    payload = gateway.invoke(
        "chat_history_get_segments",
        {
            "conversation_ref": conversation_ref,
            "namespace": "#V#owner@org",
            "user_concept_id": "#V#owner",
            "organisation_concept_id": "#V#org",
        },
    ).payload

    assert payload.get("success") is True
    assert payload.get("identifier_binding", {}).get("mode") == "server_bound_reference"
    assert payload.get("identifier_binding", {}).get("validation_status") == "verified"
    _assert_schema_conformance(gateway, "chat_history_get_segments", payload)


def test_chat_history_get_segments_gateway_rejects_emitted_von_conversation_ref_as_authority(
    monkeypatch,
) -> None:
    gateway = _build_gateway()
    namespace = "#V#michael_witbrock@university_of_auckland_strong_ai_lab"

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda session_id: "#V#michael_witbrock",
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda user_id, session_id, namespace=None, include_legacy=True: (
            user_id == "#V#michael_witbrock"
            and session_id == "ff9be41d-28f8-4864-ab0b-8c201e3152d0"
            and namespace == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
        ),
    )

    def get_segments(*args, **kwargs):
        assert kwargs["namespace"] == namespace
        assert kwargs["include_legacy"] is False
        return ([{"segment_index": 0, "history": []}], {})

    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_segments",
        get_segments,
    )

    payload = gateway.invoke(
        "chat_history_get_segments",
        {"conversation_ref": _emitted_von_conversation_ref()},
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "authenticated_actor_context_required"
    _assert_schema_conformance(gateway, "chat_history_get_segments", payload)


def test_chat_history_get_segments_gateway_rejects_identifier_misbinding_before_permission_check(
    monkeypatch,
) -> None:
    gateway = _build_gateway()
    conversation_ref = build_conversation_scope_binding(
        chat_session_id="chat-bound-gateway-2",
        history_owner_user_id="#V#owner",
        read_namespace="#V#owner@org",
        organisation_concept_id="#V#org",
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )

    payload = gateway.invoke(
        "chat_history_get_segments",
        {
            "conversation_ref": conversation_ref,
            "session_id": "req-like-id-2",
            "namespace": "#V#owner@org",
            "user_concept_id": "#V#owner",
            "organisation_concept_id": "#V#org",
        },
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "INVALID_CONTEXT_BINDING"
    assert payload.get("error_details", {}).get("identifier_binding", {}).get(
        "validation_status"
    ) == "verified"
    assert payload.get("error_message") != "PERMISSION_DENIED"
    _assert_schema_conformance(gateway, "chat_history_get_segments", payload)


def test_conversation_telemetry_get_locator_gateway_rejects_identifier_misbinding_before_permission_check(
    monkeypatch,
) -> None:
    gateway = _build_gateway()
    conversation_ref = build_conversation_scope_binding(
        chat_session_id="chat-bound-gateway-3",
        history_owner_user_id="#V#owner",
        read_namespace="#V#owner@org",
        organisation_concept_id="#V#org",
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )

    payload = gateway.invoke(
        "conversation_telemetry_get_locator",
        {
            "conversation_ref": conversation_ref,
            "session_id": "req-like-id-3",
            "namespace": "#V#owner@org",
            "user_concept_id": "#V#owner",
            "organisation_concept_id": "#V#org",
        },
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "INVALID_CONTEXT_BINDING"
    assert payload.get("error_details", {}).get("identifier_binding", {}).get(
        "validation_status"
    ) == "verified"
    assert payload.get("error_message") != "PERMISSION_DENIED"
    _assert_schema_conformance(gateway, "conversation_telemetry_get_locator", payload)


def test_chat_history_get_debug_entry_gateway_rejects_history_location_misbinding_before_permission_check(
    monkeypatch,
) -> None:
    gateway = _build_gateway()
    history_location_ref = build_history_location_binding(
        chat_session_id="chat-debug-gateway-1",
        history_index=3,
        history_owner_user_id="#V#owner",
        read_namespace="#V#owner@org",
        organisation_concept_id="#V#org",
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )

    payload = gateway.invoke(
        "chat_history_get_debug_entry",
        {
            "history_location_ref": history_location_ref,
            "history_index": 99,
            "namespace": "#V#owner@org",
            "user_concept_id": "#V#owner",
            "organisation_concept_id": "#V#org",
        },
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "INVALID_CONTEXT_BINDING"
    assert payload.get("error_details", {}).get("identifier_binding", {}).get(
        "validation_status"
    ) == "verified"
    assert payload.get("error_message") != "PERMISSION_DENIED"
    _assert_schema_conformance(gateway, "chat_history_get_debug_entry", payload)
