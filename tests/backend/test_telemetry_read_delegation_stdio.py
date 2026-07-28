from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from src.backend.services.conversation_scope_binding_service import (
    build_history_location_binding,
    build_turn_telemetry_binding,
)


def _stdio_payload(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from src.backend.mcp_server import mcp_stdio_server

    async def run() -> dict[str, Any]:
        result = await mcp_stdio_server.call_tool(tool_name, arguments)
        first = cast(list[Any], result)[0]
        return cast(
            dict[str, Any],
            json.loads(cast(str, getattr(first, "text"))),
        )

    return asyncio.run(run())


def test_real_stdio_handler_path_accepts_exact_actor_bound_reads(monkeypatch) -> None:
    history_ref = build_history_location_binding(
        chat_session_id="chat-2609-stdio",
        history_index=2,
        history_owner_user_id="#V#actor_a",
        read_namespace="#V#actor_a@org",
        organisation_concept_id="#V#org",
        delegated_actor_user_id="#V#actor_a",
        delegated_actor_namespace="#V#actor_a@org",
    )
    turn_ref = build_turn_telemetry_binding(
        request_id="req-2609-stdio",
        delegated_actor_user_id="#V#actor_a",
        permitted_tool="turn_execution_get_diagnostics",
        chat_session_id="chat-2609-stdio",
        history_index=2,
        history_owner_user_id="#V#actor_a",
        read_namespace="#V#actor_a@org",
        delegated_actor_namespace="#V#actor_a@org",
        organisation_concept_id="#V#org",
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda session_id: "#V#actor_a",
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda user_id, session_id, namespace=None, include_legacy=True: (
            user_id == "#V#actor_a"
            and session_id == "chat-2609-stdio"
            and namespace == "#V#actor_a@org"
        ),
    )
    stored_debug = {
        "request_id": "req-2609-stdio",
        "diagnostic": "exact stored entry",
    }
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_debug_entry",
        lambda **_kwargs: stored_debug,
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_diagnostics_payload",
        lambda **_kwargs: {
            "success": True,
            "schema_version": "turn_execution_diagnostics.v1",
            "request_id": "req-2609-stdio",
            "chat_session_id": "chat-2609-stdio",
            "history_location": {
                "session_id": "chat-2609-stdio",
                "history_index": 2,
            },
            "derived_user_concept_id": "#V#actor_a",
            "derived_organisation_concept_id": "#V#org",
            "namespace": "#V#actor_a@org",
            "diagnostics_source": "chat_history.llm_debug_data.turn_execution_diagnostics",
        },
    )

    debug_result = _stdio_payload(
        "chat_history_get_debug_entry",
        {
            "history_location_ref": history_ref,
            "namespace": "#V#actor_a@org",
            "user_concept_id": "#V#actor_a",
            "organisation_concept_id": "#V#org",
        },
    )
    diagnostics_result = _stdio_payload(
        "turn_execution_get_diagnostics",
        {
            "request_id": "req-2609-stdio",
            "turn_telemetry_ref": turn_ref,
            "namespace": "#V#actor_a@org",
            "user_concept_id": "#V#actor_a",
            "organisation_concept_id": "#V#org",
        },
    )

    assert debug_result["success"] is True
    assert debug_result["llm_debug_data"] == stored_debug
    assert debug_result["read_delegation"]["delegated_actor_user_id"] == "#V#actor_a"
    assert debug_result["provenance"]["source_system"] == "mongo.chat_history"
    assert diagnostics_result["success"] is True
    assert diagnostics_result["request_id"] == "req-2609-stdio"
    assert diagnostics_result["read_delegation"]["delegated_actor_user_id"] == (
        "#V#actor_a"
    )
    assert diagnostics_result["source_system"] == "mongo.chat_history"


def test_stdio_raw_ids_retain_original_denials_and_actor_b_fails_closed() -> None:
    actor_a_ref = build_turn_telemetry_binding(
        request_id="req-2609-a",
        delegated_actor_user_id="#V#actor_a",
        permitted_tool="turn_execution_get_diagnostics",
        read_namespace="#V#actor_a@org",
        delegated_actor_namespace="#V#actor_a@org",
        organisation_concept_id="#V#org",
    )

    raw_debug = _stdio_payload(
        "chat_history_get_debug_entry",
        {
            "session_id": "chat-guessed",
            "history_index": 2,
            "namespace": "#V#actor_a@org",
            "user_concept_id": "#V#actor_a",
        },
    )
    raw_diagnostics = _stdio_payload(
        "turn_execution_get_diagnostics",
        {
            "request_id": "req-guessed",
            "namespace": "#V#actor_a@org",
            "user_concept_id": "#V#actor_a",
        },
    )
    actor_b = _stdio_payload(
        "turn_execution_get_diagnostics",
        {
            "request_id": "req-2609-a",
            "turn_telemetry_ref": actor_a_ref,
            "namespace": "#V#actor_b@org",
            "user_concept_id": "#V#actor_b",
            "organisation_concept_id": "#V#org",
        },
    )

    assert raw_debug["error_code"] == "authenticated_actor_context_required"
    assert raw_diagnostics["error_code"] == (
        "workflow_global_admin_authority_required"
    )
    assert actor_b["error_code"] == "READ_DELEGATION_ACTOR_MISMATCH"


def test_stdio_delegation_tamper_expiry_tool_and_target_mismatches_fail_closed() -> None:
    issued_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    expired_ref = build_turn_telemetry_binding(
        request_id="req-expired-2609",
        delegated_actor_user_id="#V#actor_a",
        permitted_tool="turn_execution_get_diagnostics",
        issued_at=issued_at,
        ttl_seconds=30,
    )
    diagnostics_ref = build_turn_telemetry_binding(
        request_id="req-bound-2609",
        delegated_actor_user_id="#V#actor_a",
        permitted_tool="turn_execution_get_diagnostics",
        chat_session_id="chat-bound-2609",
        history_owner_user_id="#V#actor_a",
        read_namespace="#V#actor_a@org",
        delegated_actor_namespace="#V#actor_a@org",
        organisation_concept_id="#V#org",
    )
    tampered_ref = dict(diagnostics_ref)
    tampered_ref["request_id"] = "req-tampered-2609"

    expired = _stdio_payload(
        "turn_execution_get_diagnostics",
        {
            "request_id": "req-expired-2609",
            "turn_telemetry_ref": expired_ref,
            "user_concept_id": "#V#actor_a",
        },
    )
    tampered = _stdio_payload(
        "turn_execution_get_diagnostics",
        {
            "request_id": "req-tampered-2609",
            "turn_telemetry_ref": tampered_ref,
            "user_concept_id": "#V#actor_a",
        },
    )
    wrong_tool = _stdio_payload(
        "turn_execution_get_live_progress",
        {
            "request_id": "req-bound-2609",
            "turn_telemetry_ref": diagnostics_ref,
            "user_concept_id": "#V#actor_a",
        },
    )
    wrong_session = _stdio_payload(
        "turn_execution_get_diagnostics",
        {
            "request_id": "req-bound-2609",
            "session_id": "chat-other-2609",
            "turn_telemetry_ref": diagnostics_ref,
            "user_concept_id": "#V#actor_a",
        },
    )
    wrong_namespace = _stdio_payload(
        "turn_execution_get_diagnostics",
        {
            "request_id": "req-bound-2609",
            "turn_telemetry_ref": diagnostics_ref,
            "namespace": "#V#actor_a@other_org",
            "user_concept_id": "#V#actor_a",
            "organisation_concept_id": "#V#org",
        },
    )

    assert expired["error_code"] == "READ_DELEGATION_EXPIRED"
    assert tampered["error_code"] == "INVALID_CONTEXT_BINDING"
    assert wrong_tool["error_code"] == "READ_DELEGATION_TOOL_MISMATCH"
    assert wrong_session["error_code"] == "INVALID_CONTEXT_BINDING"
    assert wrong_session["error_details"]["reason_code"] == (
        "READ_DELEGATION_TARGET_MISMATCH"
    )
    assert wrong_namespace["error_code"] == "INVALID_CONTEXT_BINDING"


def test_stdio_large_debug_payload_is_paginated(monkeypatch) -> None:
    history_ref = build_history_location_binding(
        chat_session_id="chat-2609-large",
        history_index=5,
        history_owner_user_id="#V#actor_a",
        read_namespace="#V#actor_a@org",
        delegated_actor_user_id="#V#actor_a",
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda session_id: "#V#actor_a",
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_debug_entry",
        lambda **_kwargs: {"large": "x" * 150_000},
    )

    first_page = _stdio_payload(
        "chat_history_get_debug_entry",
        {
            "history_location_ref": history_ref,
            "namespace": "#V#actor_a@org",
            "user_concept_id": "#V#actor_a",
            "limit": 20_000,
        },
    )

    assert first_page["success"] is True
    assert "llm_debug_data" not in first_page
    assert first_page["bounded_read"]["returned_chars"] == 20_000
    assert first_page["bounded_read"]["has_more"] is True
    assert first_page["bounded_read"]["next_offset"] == 20_000
    assert len(first_page["bounded_read"]["json_chunk"]) == 20_000


def test_stdio_live_progress_delegation_cannot_select_another_scope(
    monkeypatch,
) -> None:
    turn_ref = build_turn_telemetry_binding(
        request_id="req-live-2609",
        delegated_actor_user_id="#V#actor_a",
        permitted_tool="turn_execution_get_live_progress",
        read_namespace="#V#actor_a@org",
        delegated_actor_namespace="#V#actor_a@org",
        organisation_concept_id="#V#org",
    )
    captured: dict[str, Any] = {}

    def get_live_progress(**kwargs):
        captured.update(kwargs)
        return {
            "request_id": "req-live-2609",
            "resolved_scope_key": "user:#V#actor_a",
            "status": "running",
        }

    monkeypatch.setattr(
        "src.backend.services.turn_execution_live_progress_service.get_turn_execution_live_progress_payload",
        get_live_progress,
    )

    result = _stdio_payload(
        "turn_execution_get_live_progress",
        {
            "request_id": "req-live-2609",
            "turn_telemetry_ref": turn_ref,
            "namespace": "#V#actor_a@org",
            "user_concept_id": "#V#actor_a",
            "organisation_concept_id": "#V#org",
            "scope_key": "user:#V#actor_b",
            "window_session_id": "actor-b-window",
            "anonymous_session_id": "actor-b-session",
        },
    )

    assert result["status"] == "running"
    assert result["read_delegation"]["delegated_actor_user_id"] == "#V#actor_a"
    assert captured["scope_key"] is None
    assert captured["window_session_id"] is None
    assert captured["anonymous_session_id"] is None

    monkeypatch.setattr(
        "src.backend.services.turn_execution_live_progress_service.get_turn_execution_live_progress_payload",
        lambda **_kwargs: {
            "request_id": "req-live-2609",
            "resolved_scope_key": "user:#V#actor_b",
            "status": "running",
        },
    )
    mismatched = _stdio_payload(
        "turn_execution_get_live_progress",
        {
            "request_id": "req-live-2609",
            "turn_telemetry_ref": turn_ref,
            "namespace": "#V#actor_a@org",
            "user_concept_id": "#V#actor_a",
            "organisation_concept_id": "#V#org",
        },
    )

    assert mismatched["error_code"] == (
        "READ_DELEGATION_CANONICAL_TARGET_MISMATCH"
    )


def test_stdio_diagnostics_delegation_reads_owner_namespace_for_invitee(
    monkeypatch,
) -> None:
    turn_ref = build_turn_telemetry_binding(
        request_id="req-shared-2609",
        delegated_actor_user_id="#V#invitee",
        permitted_tool="turn_execution_get_diagnostics",
        chat_session_id="chat-shared-2609",
        history_index=9,
        history_owner_user_id="#V#owner",
        read_namespace="#V#owner@org",
        delegated_actor_namespace="#V#invitee@org",
        organisation_concept_id="#V#org",
    )
    captured: dict[str, Any] = {}

    def get_diagnostics(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "request_id": "req-shared-2609",
            "chat_session_id": "chat-shared-2609",
            "history_location": {
                "session_id": "chat-shared-2609",
                "history_index": 9,
            },
            "derived_user_concept_id": "#V#owner",
            "derived_organisation_concept_id": "#V#org",
            "namespace": "#V#owner@org",
            "diagnostics_source": "mongo.turn_execution_records",
        }

    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_diagnostics_payload",
        get_diagnostics,
    )

    result = _stdio_payload(
        "turn_execution_get_diagnostics",
        {
            "request_id": "req-shared-2609",
            "turn_telemetry_ref": turn_ref,
            "namespace": "#V#invitee@org",
            "user_concept_id": "#V#invitee",
            "organisation_concept_id": "#V#org",
        },
    )

    assert result["success"] is True
    assert captured["namespace"] == "#V#owner@org"
    assert captured["delegated_actor_user_id"] == "#V#invitee"
    assert captured["delegated_actor_namespace"] == "#V#invitee@org"
