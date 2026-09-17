"""Faithful discovery replay: real services, gateway and turn evidence; fixture storage."""

from __future__ import annotations

import pytest

from src.backend.integrations.internal_mcp import build_default_catalogue, catalogue
from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
)
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.security.access_control import override_current_actor
from src.backend.services import conversation_management_service as service
from src.backend.services.adaptive_turn_service import (
    _compact_evidence_envelope,
    _resolve_turn_cursor_evidence_argument,
)
from src.backend.services.opaque_cursor_service import decode_opaque_cursor
from src.backend.services.turn_evidence_store import TrustedTurnScope, TurnEvidenceStore


@pytest.mark.parametrize("method", ["conversation_list", "conversation_search"])
def test_gateway_filtered_pages_reach_owned_and_shared_candidates(monkeypatch, method):
    scope = TrustedTurnScope(
        user_concept_id="#V#alice",
        organisation_concept_id="#V#org",
        namespace="#V#alice@org",
    )
    monkeypatch.setattr(
        catalogue, "_register_dynamic_catalogue_methods", lambda **kwargs: None
    )
    owned_positions = []

    def owned_page(actor, **kwargs):
        assert actor == scope.user_concept_id
        assert kwargs["namespace"] == scope.namespace
        position = kwargs["position"]
        owned_positions.append(position)
        assert position in (None, {"session_id": "named"})
        return {
            "sessions": [
                {
                    "session_id": "named" if position is None else "unnamed",
                    "session_name": "Existing title" if position is None else None,
                }
            ],
            "has_more": position is None,
            "next_position": {"session_id": "named" if position is None else "unnamed"},
        }

    def invite_page(**kwargs):
        assert kwargs["user_concept_id"] == scope.user_concept_id
        assert kwargs["position"] is None
        return {
            "available": True,
            "has_more": False,
            "next_position": None,
            "invites": [
                {
                    "session_id": "shared",
                    "invite_id": "invite-1",
                    "conversation_owner_user_id": "#V#owner",
                    "organisation_concept_id": "#V#org",
                }
            ],
        }

    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_page",
        owned_page,
    )
    monkeypatch.setattr(
        service, "list_accepted_invites_for_user_sessions", lambda **kwargs: []
    )
    monkeypatch.setattr(service, "list_accepted_invites_for_user_page", invite_page)
    monkeypatch.setattr(
        service,
        "get_user_memberships",
        lambda actor: {"memberships": [{"organisation_concept_id": "#V#org"}]},
    )
    monkeypatch.setattr(
        service,
        "apply_conversation_preferences",
        lambda **kwargs: list(kwargs["conversations"]),
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "resolve_chat_history_namespace",
        lambda owner: "#V#owner@org",
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_for_owner_sessions",
        lambda pairs, **kwargs: {
            (owner, session): {"session_id": session, "session_name": None}
            for owner, session in pairs
        },
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    store = TurnEvidenceStore(scope, "discovery-turn")
    arguments = {
        "acting_user_concept_id": scope.user_concept_id,
        "organisation_concept_id": scope.organisation_concept_id,
        "namespace": scope.namespace,
        "name_present": False,
    }
    arguments.update(
        {"limit": 1}
        if method == "conversation_list"
        else {"query": "*", "page_size": 1}
    )
    pages = []
    positions = []
    for index in range(3):
        with override_current_actor(
            scope.user_concept_id, scope.organisation_concept_id
        ):
            page = gateway.invoke(method, arguments).payload
        assert page["success"] is True, page
        assert page["ordering"]["tie_breaker"] == "session_id_or_invite_id"
        pages.append(page)
        envelope = store.record(method, f"call-{index}", page)
        compact = _compact_evidence_envelope(envelope.to_mapping(), max_bytes=4000)
        assert compact["source_diagnostics"]["coverage_complete"] == (index == 2)
        assert compact["source_diagnostics"]["has_more"] == (index < 2)
        if index < 2:
            positions.append(
                decode_opaque_cursor(
                    cursor=page["next_cursor"], purpose="conversation_list"
                )
            )
            arguments, error = _resolve_turn_cursor_evidence_argument(
                capability_name=method,
                arguments={
                    **{k: v for k, v in arguments.items() if k != "cursor"},
                    "cursor_evidence_id": compact["evidence_id"],
                },
                evidence_store=store,
                scope=scope,
                turn_id="discovery-turn",
            )
            assert error is None
            assert arguments["cursor"] == page["next_cursor"]
    rows_key = "conversations" if method == "conversation_list" else "results"
    assert [[row["session_id"] for row in page[rows_key]] for page in pages] == [
        [],
        ["unnamed"],
        ["shared"],
    ]
    assert pages[-1]["next_cursor"] is None
    assert [(p["phase"], p["position"]) for p in positions] == [
        ("owned", {"session_id": "named"}),
        ("shared", None),
    ]
    assert owned_positions == [None, {"session_id": "named"}]

    # A cursor cannot be continued in another actor's scope or a later turn.
    with pytest.raises(service.ConversationManagementError, match="actor or filters"):
        service.list_actor_conversations(
            actor_user_id="#V#bob", cursor=pages[0]["next_cursor"]
        )
    _, error = _resolve_turn_cursor_evidence_argument(
        capability_name=method,
        arguments={"cursor_evidence_id": envelope.evidence_id},
        evidence_store=store,
        scope=scope,
        turn_id="different-turn",
    )
    assert error["error_code"] == "cursor_evidence_unavailable"
