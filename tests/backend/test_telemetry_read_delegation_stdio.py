from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from src.backend.services.conversation_scope_binding_service import (
    build_conversation_scope_binding,
    build_history_location_binding,
    build_turn_telemetry_binding,
)


def _stdio_text_and_payload(
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    from src.backend.mcp_server import mcp_stdio_server

    async def run() -> str:
        result = await mcp_stdio_server.call_tool(tool_name, arguments)
        first = cast(list[Any], result)[0]
        return cast(str, getattr(first, "text"))

    text = asyncio.run(run())
    return text, cast(dict[str, Any], json.loads(text))


def _stdio_payload(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _stdio_text_and_payload(tool_name, arguments)[1]


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


def test_actor_bound_conversation_carrier_is_paged_and_locator_stays_compact(
    monkeypatch,
) -> None:
    conversation_ref = build_conversation_scope_binding(
        chat_session_id="chat-shared-carrier",
        history_owner_user_id="#V#owner",
        read_namespace="#V#owner@org",
        organisation_concept_id="#V#org",
        delegated_actor_user_id="#V#invitee",
        delegated_actor_namespace="#V#invitee@org",
    )
    situation = {
        "text": "Inspectable shared situation",
        "revision": 5,
        "source": "adaptive_turn",
        "updated_by": "#V#invitee",
        "updated_at": "2026-07-29T10:00:00+00:00",
    }
    observation_state = {
        "schema_version": "conversation_observation_state.v1",
        "retained_count": 1,
        "total_count": 3,
        "omitted_count": 2,
        "retention_limit": 12,
    }
    observations = [
        {
            "schema_version": "conversation_observation.v1",
            "observation_id": "late-1",
            "kind": "late_terminal_effect",
        }
    ]
    message_timestamp = datetime(2026, 7, 29, 9, 59, tzinfo=timezone.utc)
    captured_segments: dict[str, Any] = {}
    captured_locator: dict[str, Any] = {}

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda session_id: "#V#owner",
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.get_accepted_invite_for_user_session",
        lambda **_kwargs: {"organisation_concept_id": "#V#org"},
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._is_user_member_of_organisation",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda user_id, session_id, namespace=None, include_legacy=True: (
            user_id == "#V#owner"
            and session_id == "chat-shared-carrier"
            and namespace == "#V#owner@org"
        ),
    )

    def get_segments(*_args, **kwargs):
        captured_segments.update(kwargs)
        return (
            [
                [
                    {
                        "role": "assistant",
                        "content": "x" * 80_000,
                        "timestamp": message_timestamp,
                    }
                ]
            ],
            {
                "history_truncated": False,
                "conversation_situation": situation,
                "conversation_observations": observations,
                "conversation_observation_state": observation_state,
            },
        )

    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_segments",
        get_segments,
    )

    def build_locator(**kwargs):
        captured_locator.update(kwargs)
        return {
            "schema_version": "conversation_llm_telemetry_locator.v1",
            "generated_at_utc": "2026-07-29T10:01:00Z",
            "session_id": "chat-shared-carrier",
            "namespace_context": {
                "namespace": "#V#owner@org",
                "user_id": "#V#owner",
                "org_id": "#V#org",
            },
            "conversation_situation_state": {
                "available": True,
                "revision": 5,
                "updated_at": "2026-07-29T10:00:00+00:00",
            },
            "conversation_observation_state": observation_state,
            "metadata": {"total_turns": 0},
            "mcp_access": {},
            "turns": [],
        }

    monkeypatch.setattr(
        "src.backend.services.conversation_telemetry_locator_service.build_conversation_llm_telemetry_locator",
        build_locator,
    )

    authority_args = {
        "conversation_ref": conversation_ref,
        "namespace": "#V#invitee@org",
        "user_concept_id": "#V#invitee",
        "organisation_concept_id": "#V#org",
    }
    carrier = _stdio_payload(
        "chat_history_get_segments",
        {**authority_args, "limit": 20_000},
    )
    carrier_pages = [carrier]
    while carrier_pages[-1]["bounded_read"]["has_more"]:
        next_offset = carrier_pages[-1]["bounded_read"]["next_offset"]
        assert isinstance(next_offset, int)
        assert len(carrier_pages) < 20
        carrier_pages.append(
            _stdio_payload(
                "chat_history_get_segments",
                {
                    **authority_args,
                    "offset": next_offset,
                    "limit": 20_000,
                },
            )
        )
    locator = _stdio_payload(
        "conversation_telemetry_get_locator",
        authority_args,
    )

    assert carrier["success"] is True
    assert "segments" not in carrier
    assert carrier["bounded_read"]["returned_chars"] == 20_000
    assert carrier["bounded_read"]["has_more"] is True
    assert carrier["history_owner_user_id"] == "#V#owner"
    assert carrier["requested_user_id"] == "#V#invitee"
    assert carrier["namespace"] == "#V#owner@org"
    assert carrier["identifier_binding"]["validation_status"] == "verified"
    assert carrier["read_delegation"]["delegated_actor_user_id"] == "#V#invitee"
    assert captured_segments["namespace"] == "#V#owner@org"
    assert captured_segments["include_conversation_state"] is True

    page_metadata = [page["bounded_read"] for page in carrier_pages]
    assert len({page["sha256"] for page in page_metadata}) == 1
    assert len({page["total_chars"] for page in page_metadata}) == 1
    assert [page["offset"] for page in page_metadata] == [
        index * 20_000 for index in range(len(page_metadata))
    ]
    canonical_json = "".join(page["json_chunk"] for page in page_metadata)
    assert len(canonical_json) == page_metadata[0]["total_chars"]
    assert hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() == (
        page_metadata[0]["sha256"]
    )
    reconstructed_carrier = json.loads(canonical_json)
    assert reconstructed_carrier["conversation_situation"] == situation
    assert reconstructed_carrier["conversation_observations"] == observations
    assert reconstructed_carrier["conversation_observation_state"] == (
        observation_state
    )
    assert reconstructed_carrier["history_owner_user_id"] == "#V#owner"
    assert reconstructed_carrier["requested_user_id"] == "#V#invitee"
    assert reconstructed_carrier["segments"][0][0]["timestamp"] == (
        message_timestamp.isoformat()
    )
    for page in carrier_pages:
        assert page["history_owner_user_id"] == "#V#owner"
        assert page["requested_user_id"] == "#V#invitee"
        assert page["namespace"] == "#V#owner@org"
        assert page["read_delegation"]["delegated_actor_user_id"] == "#V#invitee"

    assert locator["conversation_situation_state"]["revision"] == 5
    assert locator["conversation_observation_state"] == observation_state
    assert "conversation_situation" not in locator
    assert "conversation_observations" not in locator
    assert locator["history_owner_user_id"] == "#V#owner"
    assert locator["requested_user_id"] == "#V#invitee"
    assert locator["read_delegation"]["delegated_actor_user_id"] == "#V#invitee"
    assert captured_locator["user_id"] == "#V#owner"
    assert captured_locator["requested_user_id"] == "#V#invitee"


def test_escape_heavy_carrier_pages_remain_under_real_stdio_guard(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_MCP_STDIO_MAX_RESPONSE_CHARS", "10000")
    conversation_ref = build_conversation_scope_binding(
        chat_session_id="chat-escape-heavy-carrier",
        history_owner_user_id="#V#actor",
        read_namespace="#V#actor@org",
        organisation_concept_id="#V#org",
        delegated_actor_user_id="#V#actor",
        delegated_actor_namespace="#V#actor@org",
    )
    message_timestamp = datetime(2026, 7, 29, 12, 34, 56, tzinfo=timezone.utc)
    escape_heavy_content = '\\"' * 6_000

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda session_id: "#V#actor",
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._is_user_member_of_organisation",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda user_id, session_id, namespace=None, include_legacy=True: (
            user_id == "#V#actor"
            and session_id == "chat-escape-heavy-carrier"
            and namespace == "#V#actor@org"
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_segments",
        lambda *_args, **_kwargs: (
            [
                [
                    {
                        "role": "assistant",
                        "content": escape_heavy_content,
                        "timestamp": message_timestamp,
                    }
                ]
            ],
            {
                "history_truncated": False,
                "conversation_situation": None,
                "conversation_observations": [],
                "conversation_observation_state": {
                    "schema_version": "conversation_observation_state.v1",
                    "retained_count": 0,
                    "total_count": 0,
                    "omitted_count": 0,
                    "retention_limit": 12,
                },
            },
        ),
    )

    authority_args = {
        "conversation_ref": conversation_ref,
        "namespace": "#V#actor@org",
        "user_concept_id": "#V#actor",
        "organisation_concept_id": "#V#org",
    }
    response_text, first_page = _stdio_text_and_payload(
        "chat_history_get_segments",
        {**authority_args, "limit": 20_000},
    )
    response_texts = [response_text]
    pages = [first_page]
    while pages[-1]["bounded_read"]["has_more"]:
        next_offset = pages[-1]["bounded_read"]["next_offset"]
        assert isinstance(next_offset, int)
        assert next_offset > pages[-1]["bounded_read"]["offset"]
        assert len(pages) < 30
        response_text, page = _stdio_text_and_payload(
            "chat_history_get_segments",
            {
                **authority_args,
                "offset": next_offset,
                "limit": 20_000,
            },
        )
        response_texts.append(response_text)
        pages.append(page)

    assert all(len(text) <= 10_000 for text in response_texts)
    assert all(page.get("error_code") != "payload_too_large" for page in pages)
    assert pages[0]["bounded_read"]["returned_chars"] < 20_000
    page_metadata = [page["bounded_read"] for page in pages]
    assert len({page["sha256"] for page in page_metadata}) == 1
    assert len({page["total_chars"] for page in page_metadata}) == 1
    canonical_json = "".join(page["json_chunk"] for page in page_metadata)
    assert len(canonical_json) == page_metadata[0]["total_chars"]
    assert hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() == (
        page_metadata[0]["sha256"]
    )
    reconstructed = json.loads(canonical_json)
    assert reconstructed["segments"][0][0]["content"] == escape_heavy_content
    assert reconstructed["segments"][0][0]["timestamp"] == (
        message_timestamp.isoformat()
    )
    for page in pages:
        assert page["history_owner_user_id"] == "#V#actor"
        assert page["requested_user_id"] == "#V#actor"
        assert page["namespace"] == "#V#actor@org"
        assert page["identifier_binding"]["validation_status"] == "verified"
        assert page["read_delegation"]["delegated_actor_user_id"] == "#V#actor"


def test_untrusted_context_raw_ids_and_actor_b_fail_closed() -> None:
    actor_a_ref = build_turn_telemetry_binding(
        request_id="req-2609-a",
        delegated_actor_user_id="#V#actor_a",
        permitted_tool="turn_execution_get_diagnostics",
        read_namespace="#V#actor_a@org",
        delegated_actor_namespace="#V#actor_a@org",
        organisation_concept_id="#V#org",
    )

    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
    )

    # Ordinary internal callers do not inherit the standalone local operator.
    with bind_internal_mcp_actor_context_source("tool_payload_fallback"):
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
    assert raw_diagnostics["error_code"] == ("workflow_global_admin_authority_required")
    assert actor_b["error_code"] == "READ_DELEGATION_ACTOR_MISMATCH"


def test_stdio_delegation_tamper_expiry_tool_and_target_mismatches_fail_closed() -> (
    None
):
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

    assert mismatched["error_code"] == ("READ_DELEGATION_CANONICAL_TARGET_MISMATCH")


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
