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

    for name in (
        "conversation_list",
        "conversation_search",
        "conversation_get",
        "conversation_transcript_page",
        "conversation_inspect_batch",
    ):
        definition = catalogue.get(name)
        assert definition.category == "read"
        assert definition.ordinary_turn_trusted_argument_bindings == {
            "acting_user_concept_id": "actor_user_concept_id",
            "namespace": "turn_namespace",
        }
        assert definition.ordinary_turn_optional_trusted_argument_bindings == {
            "organisation_concept_id": "actor_organisation_concept_id",
        }

    manage = catalogue.get("conversation_manage")
    assert manage.category == "write"
    assert manage.ordinary_turn_effect is True
    assert manage.ordinary_turn_trusted_argument_bindings == {
        "acting_user_concept_id": "actor_user_concept_id",
        "namespace": "turn_namespace",
        "request_id": "turn_id",
    }
    assert manage.ordinary_turn_optional_trusted_argument_bindings == {
        "organisation_concept_id": "actor_organisation_concept_id",
    }
    batch = catalogue.get("conversation_manage_batch")
    assert batch.category == "write"
    assert batch.ordinary_turn_effect is True
    assert batch.ordinary_turn_trusted_argument_bindings == (
        manage.ordinary_turn_trusted_argument_bindings
    )
    assert batch.ordinary_turn_optional_trusted_argument_bindings == (
        manage.ordinary_turn_optional_trusted_argument_bindings
    )


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
        "include_trashed": False,
        "cursor": None,
        "name_present": None,
        "cursor_context": None,
    }
    _assert_success_schema("conversation_list", payload)


def test_conversation_search_uses_trusted_actor_scope_and_returns_cursor(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import conversation_search_service

    captured = {}

    def _search(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "schema_version": "conversation_search_result.v1",
            "query": kwargs["query"],
            "match_mode": kwargs["match_mode"],
            "sort": kwargs["sort"],
            "filters": kwargs["filters"],
            "candidate_session_ids": [],
            "results": [],
            "count": 0,
            "page_size": kwargs["page_size"],
            "has_more": False,
            "continuation_cursor": None,
            "next_cursor": None,
            "index_coverage": {},
            "retrieval": {},
            "trashed_only": False,
        }

    monkeypatch.setattr(
        conversation_search_service, "search_actor_conversations", _search
    )
    with bind_internal_mcp_actor_context_source("trusted_operator_payload_fallback"):
        payload = catalogue._conversation_search(
            acting_user_concept_id="#V#alice",
            organisation_concept_id="#V#org",
            namespace="#V#alice@org",
            query="detector calibration",
            name_present=False,
            access_mode="owner",
            hidden=False,
            date_from="2026-01-01T00:00:00+00:00",
            page_size=25,
        )

    assert captured["actor_user_id"] == "#V#alice"
    assert captured["organisation_concept_id"] == "#V#org"
    assert captured["namespace"] == "#V#alice@org"
    assert captured["query"] == "detector calibration"
    assert captured["filters"] == {
        "name_present": False,
        "access_mode": "owner",
        "hidden": False,
        "date_from": "2026-01-01T00:00:00+00:00",
    }
    definition = build_default_catalogue().get("conversation_search")
    assert definition.input_schema.expect("name_present") == (bool, type(None))
    assert definition.input_schema.expect("access_mode") == (str, type(None))
    assert definition.input_schema.expect("hidden") == (bool, type(None))
    assert "name_present=false" in definition.description
    _assert_success_schema("conversation_search", payload)


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


def test_canonical_conversation_reference_names_invalid_schema_field():
    from src.backend.integrations.internal_mcp import catalogue

    normalised = catalogue._normalise_chat_history_conversation_ref_argument(
        {
            "kind": "von_conversation_ref",
            "schema_version": "conversation_reference.v0",
            "conversation_ref": {
                "schema_version": "conversation_reference.v1",
                "binding_kind": "explicit_session_id",
                "session_id": "session-1",
            },
        }
    )

    assert normalised["validation_errors"] == [
        {
            "field": "conversation_ref.schema_version",
            "value": "conversation_reference.v0",
            "expected": "conversation_reference.v1",
        }
    ]

    flat = catalogue._normalise_chat_history_conversation_ref_argument(
        {
            "schema_version": "conversation_reference.v1",
            "binding_kind": "explicit_session_id",
            "session_id": "session-1",
            "user_concept_id": "#V#alice",
            "namespace": "#V#alice@org",
        }
    )
    assert flat["recognised_public_ref"] is True
    assert flat["validation_errors"] == []
    assert flat["fields"]["session_id"] == "session-1"


def test_conversation_manage_batch_preserves_per_item_receipts(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _manage(**kwargs):
        session_id = kwargs["session_id"]
        if session_id == "denied":
            return {
                "success": False,
                "error_code": "PERMISSION_DENIED",
                "message": "Not accessible",
            }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "action": kwargs["action"],
            "session_id": session_id,
            "canonical_read_back": {"session_id": session_id, "trashed": True},
        }

    monkeypatch.setattr(catalogue, "_conversation_manage", _manage)
    payload = catalogue._conversation_manage_batch(
        action="trash",
        session_ids=["one", "denied", "two"],
        acting_user_concept_id="#V#alice",
        namespace="#V#alice@org",
        request_id="turn-1",
        continuation_cursor="opaque-next-page",
    )

    assert payload["success"] is False
    assert payload["effect_status"] == "partial"
    assert payload["succeeded_count"] == 2
    assert payload["failed_count"] == 1
    assert [result["index"] for result in payload["results"]] == [0, 1, 2]
    assert payload["results"][1]["error_code"] == "PERMISSION_DENIED"
    assert payload["continuation_cursor"] == "opaque-next-page"
    _assert_success_schema("conversation_manage_batch", payload)


def test_transcript_cursor_pages_more_than_100_messages_and_is_actor_bound(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import chat_history_service

    messages = [
        {"role": "user", "content": f"message {index}"} for index in range(205)
    ]
    actor = {"value": "#V#alice"}

    monkeypatch.setattr(
        catalogue,
        "_resolve_chat_history_read_target",
        lambda _payload: {
            "success": True,
            "session_id": "session-long",
            "requested_user_id": actor["value"],
            "read_user_id": "#V#alice",
            "read_namespace": "#V#alice@org",
            "access_mode": "owner",
        },
    )

    def _page(**kwargs):
        offset = kwargs["offset"]
        page = messages[offset : offset + kwargs["page_size"]]
        next_offset = offset + len(page)
        return {
            "found": True,
            "messages": [
                {
                    **message,
                    "source_locator": {"history_index": offset + index},
                }
                for index, message in enumerate(page)
            ],
            "message_count": len(messages),
            "page_count": len(page),
            "offset": offset,
                "next_offset": next_offset if next_offset < len(messages) else None,
                "has_more": next_offset < len(messages),
                "coverage_complete": next_offset >= len(messages),
            }

    monkeypatch.setattr(chat_history_service, "get_chat_history_transcript_page", _page)

    first = catalogue._conversation_transcript_page(
        session_id="session-long", page_size=100
    )
    second = catalogue._conversation_transcript_page(
        session_id="session-long", page_size=100, cursor=first["next_cursor"]
    )
    third = catalogue._conversation_transcript_page(
        session_id="session-long", page_size=100, cursor=second["next_cursor"]
    )

    assert first["messages"][0]["source_locator"]["history_index"] == 0
    assert second["messages"][0]["source_locator"]["history_index"] == 100
    assert third["messages"][-1]["source_locator"]["history_index"] == 204
    assert first["message_count"] == 205
    assert third["coverage_complete"] is True
    assert third["next_cursor"] is None
    assert first["transport_paging"]["distinct_from_transcript_cursor"] is True
    _assert_success_schema("conversation_transcript_page", first)

    actor["value"] = "#V#bob"
    denied = catalogue._conversation_transcript_page(
        session_id="session-long", cursor=first["next_cursor"]
    )
    assert denied["success"] is False
    assert denied["error_code"] == "invalid_transcript_cursor"


def test_title_evidence_batch_drives_idempotent_rename_and_preserves_names(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import (
        chat_history_service,
        conversation_management_service,
    )

    state = {"session_name": None, "rename_calls": 0}
    actor = {"value": "#V#alice"}

    def _access(_payload):
        return {
            "success": True,
            "session_id": "session-1",
            "requested_user_id": actor["value"],
            "read_user_id": "#V#alice",
            "read_namespace": "#V#alice@org",
            "organisation_concept_id": "#V#org",
            "access_mode": "owner",
        }

    monkeypatch.setattr(catalogue, "_resolve_chat_history_read_target", _access)
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_title_evidence",
        lambda **_kwargs: {
            "session_id": "session-1",
            "session_name": state["session_name"],
            "updated_at": "2026-09-02T12:00:00+00:00",
            "message_count": 3,
            "user_message_count": 2,
            "first_user_message": {
                "content": "Relate claims in a scientific paper.",
                "content_sha256": "first-hash",
                "source_locator": {"turn_id": "turn-1"},
            },
            "latest_user_message": {
                "content": "Compare the experimental findings.",
                "content_sha256": "latest-hash",
                "source_locator": {"turn_id": "turn-3"},
            },
            "evidence_sufficient": True,
        },
    )
    monkeypatch.setattr(
        conversation_management_service,
        "apply_conversation_preferences",
        lambda *, actor_user_id, conversations: [dict(conversations[0])],
    )

    def _rename(**kwargs):
        assert kwargs["require_unnamed"] is True
        assert kwargs["expected_updated_at"] == "2026-09-02T12:00:00+00:00"
        state["rename_calls"] += 1
        state["session_name"] = kwargs["session_name"]
        return {"matched": True, "updated": True}

    monkeypatch.setattr(chat_history_service, "rename_chat_session", _rename)
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_session_summary",
        lambda *_args, **_kwargs: {
            "session_id": "session-1",
            "session_name": state["session_name"],
            "namespace": "#V#alice@org",
        },
    )

    inspected = catalogue._conversation_inspect_batch(
        mode="title_evidence",
        session_ids=["session-1"],
        acting_user_concept_id="#V#alice",
        namespace="#V#alice@org",
    )
    evidence = inspected["results"][0]["evidence"]
    assert inspected["results"][0]["status"] == "ready"
    assert inspected["results"][0]["evidence_state"] == "complete"
    assert inspected["results"][0]["evidence_truncated"] is False
    assert evidence["current_display_name"] is None
    assert evidence["evidence_token"]
    assert "llm_debug_data" not in evidence["first_user_message"]
    assert inspected["title_evidence_items"] == [
        {
            "index": 0,
            "session_id": "session-1",
            "status": "ready",
            "evidence_state": "complete",
            "eligible_for_rename": True,
            "current_display_name": None,
            "first_user_text": evidence["first_user_message"]["content"],
            "first_user_text_truncated": None,
            "latest_user_text": evidence["latest_user_message"]["content"],
            "latest_user_text_truncated": None,
            "error_code": None,
        }
    ]
    _assert_success_schema("conversation_inspect_batch", inspected)

    rename_item = {
        "session_id": "session-1",
        "session_name": "Scientific Paper Text Relations",
        "evidence_token": evidence["evidence_token"],
    }
    renamed = catalogue._conversation_manage_batch(
        action="rename",
        rename_items=[rename_item],
        acting_user_concept_id="#V#alice",
        namespace="#V#alice@org",
        request_id="turn-rename",
    )
    assert renamed["success"] is True
    assert renamed["results"][0]["changed"] is True
    assert renamed["results"][0]["canonical_read_back"]["session_name"] == (
        "Scientific Paper Text Relations"
    )
    assert state["rename_calls"] == 1

    retried = catalogue._conversation_manage_batch(
        action="rename",
        rename_items=[rename_item],
        acting_user_concept_id="#V#alice",
        namespace="#V#alice@org",
        request_id="turn-rename",
    )
    assert retried["success"] is True
    assert retried["results"][0]["changed"] is False
    assert state["rename_calls"] == 1

    overwrite = catalogue._conversation_manage_batch(
        action="rename",
        rename_items=[{**rename_item, "session_name": "Different title"}],
        acting_user_concept_id="#V#alice",
        namespace="#V#alice@org",
    )
    assert overwrite["success"] is False
    assert overwrite["results"][0]["error_code"] == (
        "stale_conversation_rename_evidence"
    )

    named = catalogue._conversation_inspect_batch(
        mode="title_evidence", session_ids=["session-1"]
    )
    assert named["results"][0]["status"] == "already_named"
    assert named["results"][0]["evidence"]["evidence_token"] is None

    actor["value"] = "#V#bob"
    cross_actor = catalogue._conversation_manage_batch(
        action="rename",
        rename_items=[rename_item],
        acting_user_concept_id="#V#bob",
        namespace="#V#bob@org",
    )
    assert cross_actor["success"] is False
    assert cross_actor["results"][0]["error_code"] == (
        "invalid_conversation_rename_evidence"
    )


def test_title_evidence_batch_distinguishes_failure_and_truncation_states(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _access(payload):
        session_id = payload["session_id"]
        if session_id == "denied":
            return {
                "success": False,
                "error_code": "PERMISSION_DENIED",
                "message": "not authorised",
            }
        if session_id == "lookup-down":
            return {
                "success": False,
                "error_code": "SHARED_CONVERSATION_LOOKUP_FAILED",
                "message": "invite storage unavailable",
            }
        return {
            "success": True,
            "session_id": session_id,
            "requested_user_id": "#V#alice",
            "read_user_id": "#V#alice",
            "read_namespace": "#V#alice@org",
            "access_mode": "owner",
        }

    def _evidence(access):
        if access["session_id"] == "missing":
            return {
                "success": False,
                "error_code": "conversation_not_found",
                "message": "gone",
            }
        return {
            "success": True,
            "evidence": {
                "eligible_for_rename": True,
                "first_user_message": {
                    "content": "bounded excerpt",
                    "content_truncated": True,
                },
                "latest_user_message": {
                    "content": "latest",
                    "content_truncated": False,
                },
            },
        }

    monkeypatch.setattr(catalogue, "_resolve_chat_history_read_target", _access)
    monkeypatch.setattr(catalogue, "_conversation_title_evidence_for_access", _evidence)

    result = catalogue._conversation_inspect_batch(
        mode="title_evidence",
        session_ids=["truncated", "missing", "denied", "lookup-down"],
        acting_user_concept_id="#V#alice",
    )

    assert [row["status"] for row in result["results"]] == [
        "ready",
        "missing",
        "denied",
        "unavailable",
    ]
    assert [row["evidence_state"] for row in result["results"]] == [
        "truncated",
        "missing",
        "denied",
        "unavailable",
    ]
    assert result["results"][0]["evidence_truncated"] is True


def test_accepted_shared_title_evidence_rename_is_actor_specific_and_idempotent(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.services import (
        chat_history_service,
        conversation_management_service,
    )

    actor_display_name = {"value": None}
    preference_calls = []

    monkeypatch.setattr(
        catalogue,
        "_resolve_chat_history_read_target",
        lambda _payload: {
            "success": True,
            "session_id": "shared-session",
            "requested_user_id": "#V#alice",
            "read_user_id": "#V#owner",
            "read_namespace": "#V#owner@org",
            "organisation_concept_id": "#V#org",
            "access_mode": "invitee",
        },
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_title_evidence",
        lambda **_kwargs: {
            "session_id": "shared-session",
            "session_name": None,
            "updated_at": "2026-09-02T12:00:00+00:00",
            "message_count": 2,
            "user_message_count": 1,
            "first_user_message": {
                "content": "Discuss sparse neural retrieval.",
                "content_sha256": "first-hash",
                "content_truncated": False,
                "source_locator": {"turn_id": "turn-1"},
            },
            "latest_user_message": {
                "content": "Discuss sparse neural retrieval.",
                "content_sha256": "first-hash",
                "content_truncated": False,
                "source_locator": {"turn_id": "turn-1"},
            },
            "evidence_sufficient": True,
        },
    )

    def _apply_preferences(*, actor_user_id, conversations):
        assert actor_user_id == "#V#alice"
        row = dict(conversations[0])
        if actor_display_name["value"] is not None:
            row["session_name"] = actor_display_name["value"]
            row["session_name_source"] = "actor_override"
        return [row]

    def _set_preference(**kwargs):
        preference_calls.append(dict(kwargs))
        new_name = kwargs["session_name_override"]
        changed = actor_display_name["value"] != new_name
        actor_display_name["value"] = new_name
        return {"changed": changed}

    monkeypatch.setattr(
        conversation_management_service,
        "apply_conversation_preferences",
        _apply_preferences,
    )
    monkeypatch.setattr(
        conversation_management_service,
        "set_conversation_preference",
        _set_preference,
    )
    monkeypatch.setattr(
        chat_history_service,
        "rename_chat_session",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a shared rename must not mutate the owner's title")
        ),
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_session_summary",
        lambda *_args, **_kwargs: {
            "session_id": "shared-session",
            "session_name": None,
            "namespace": "#V#owner@org",
        },
    )

    inspected = catalogue._conversation_inspect_batch(
        mode="title_evidence",
        session_ids=["shared-session"],
        acting_user_concept_id="#V#alice",
    )
    token = inspected["results"][0]["evidence"]["evidence_token"]
    rename_item = {
        "session_id": "shared-session",
        "session_name": "Sparse Neural Retrieval",
        "evidence_token": token,
    }

    renamed = catalogue._conversation_manage_batch(
        action="rename",
        rename_items=[rename_item],
        acting_user_concept_id="#V#alice",
    )
    retried = catalogue._conversation_manage_batch(
        action="rename",
        rename_items=[rename_item],
        acting_user_concept_id="#V#alice",
    )

    assert renamed["success"] is True
    assert renamed["results"][0]["access_mode"] == "invitee"
    assert renamed["results"][0]["changed"] is True
    assert renamed["results"][0]["canonical_read_back"]["session_name"] == (
        "Sparse Neural Retrieval"
    )
    assert renamed["results"][0]["canonical_read_back"]["session_name_source"] == (
        "actor_override"
    )
    assert retried["success"] is True
    assert retried["results"][0]["changed"] is False
    assert len(preference_calls) == 1
