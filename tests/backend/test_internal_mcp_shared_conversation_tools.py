"""Gateway-level tests for shared-conversation MCP tools.

These tests validate method registration plus success/error payload schema
compatibility through InternalMCPGateway.invoke().
"""

from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.schemas import validate_payload
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _assert_schema_conformance(
    gateway: InternalMCPGateway, method: str, payload: dict
) -> None:
    definition = gateway.get_method_definition(method)
    assert definition is not None
    assert definition.output_schema is not None
    ok, errors = validate_payload(definition.output_schema, payload)
    assert ok, f"{method} output schema mismatch: {errors}"


def test_shared_conversation_methods_registered_in_catalogue():
    methods = set(build_default_catalogue().list_methods())
    expected = {
        "shared_conversation_create_session",
        "shared_conversation_join_session",
        "shared_conversation_invite_create",
        "shared_conversation_list_invites",
        "shared_conversation_respond_invite",
    }
    missing = sorted(expected - methods)
    assert not missing, f"Missing shared conversation methods: {missing}"


def test_shared_conversation_invite_lifecycle_gateway_and_schema(monkeypatch):
    gateway = _build_gateway()
    bootstrap_calls: list[dict] = []

    def _fake_resolve_event_actor_context(*, user_id=None, org_id=None, namespace=None):
        return user_id or "#V#owner", org_id or "#V#org"

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        _fake_resolve_event_actor_context,
    )
    monkeypatch.setattr(
        "src.backend.services.coding_agent_identity_bootstrap_service.ensure_coding_agent_identity_concepts",
        lambda **kwargs: bootstrap_calls.append(dict(kwargs))
        or {"cached": False, "errors": []},
    )
    monkeypatch.setattr(
        "src.backend.services.organisation_membership_service.get_user_memberships",
        lambda user_concept_id: {
            "user_concept_id": user_concept_id,
            "memberships": [{"organisation_concept_id": "#V#org", "role": "member"}],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.organisation_membership_service.get_organisation_members",
        lambda organisation_concept_id: {
            "organisation_concept_id": organisation_concept_id,
            "members": [
                {"user_concept_id": "#V#owner", "role": "member"},
                {"user_concept_id": "#V#invitee", "role": "member"},
            ],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.create_invite",
        lambda **kwargs: {
            "invite": {
                "invite_id": "invite-1",
                "session_id": kwargs["session_id"],
                "inviter_user_id": kwargs["inviter_user_id"],
                "invitee_user_id": kwargs["invitee_user_id"],
                "organisation_concept_id": kwargs["organisation_concept_id"],
                "status": "pending",
            },
            "created": True,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.list_invites_for_user",
        lambda **kwargs: [
            {
                "invite_id": "invite-1",
                "session_id": kwargs.get("session_id") or "session-1",
                "inviter_user_id": "#V#owner",
                "invitee_user_id": "#V#invitee",
                "organisation_concept_id": "#V#org",
                "status": kwargs.get("status") or "pending",
            }
        ],
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.respond_to_invite",
        lambda **kwargs: {
            "invite_id": kwargs["invite_id"],
            "session_id": "session-1",
            "organisation_concept_id": "#V#org",
            "status": "accepted" if kwargs["action"] == "accept" else "declined",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.episode_logging_service.log_episode",
        lambda **kwargs: "episode-1",
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.create_chat_session",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "session_name": kwargs.get("session_name") or "Shared Session",
            "namespace": kwargs.get("namespace") or "#V#invitee@org",
        },
    )

    invite_payload = gateway.invoke(
        "shared_conversation_invite_create",
        {
            "session_id": "session-1",
            "invitee_concept_id": "#V#invitee",
            "user_concept_id": "#V#owner",
            "organisation_concept_id": "#V#org",
            "actor_concept_id": "#V#github_copilot_instance",
        },
    ).payload
    assert invite_payload.get("success") is True
    assert invite_payload.get("created") is True
    assert invite_payload.get("invite", {}).get("invite_id") == "invite-1"
    assert invite_payload.get("actor_concept_id") == "#V#github_copilot_instance"
    _assert_schema_conformance(gateway, "shared_conversation_invite_create", invite_payload)

    list_payload = gateway.invoke(
        "shared_conversation_list_invites",
        {
            "user_concept_id": "#V#invitee",
            "direction": "incoming",
            "status": "pending",
            "organisation_concept_id": "#V#org",
        },
    ).payload
    assert list_payload.get("success") is True
    assert list_payload.get("count") == 1
    _assert_schema_conformance(gateway, "shared_conversation_list_invites", list_payload)

    respond_payload = gateway.invoke(
        "shared_conversation_respond_invite",
        {
            "invite_id": "invite-1",
            "action": "accept",
            "user_concept_id": "#V#invitee",
            "organisation_concept_id": "#V#org",
            "actor_concept_id": "#V#github_copilot_instance",
        },
    ).payload
    assert respond_payload.get("success") is True
    assert respond_payload.get("invite", {}).get("status") == "accepted"
    assert respond_payload.get("created_session") is True
    assert respond_payload.get("actor_concept_id") == "#V#github_copilot_instance"
    assert len(bootstrap_calls) >= 2
    _assert_schema_conformance(
        gateway, "shared_conversation_respond_invite", respond_payload
    )


def test_shared_conversation_join_gateway_success_and_permission_error_schema(
    monkeypatch,
):
    gateway = _build_gateway()

    def _fake_resolve_event_actor_context(*, user_id=None, org_id=None, namespace=None):
        return user_id or "#V#invitee", org_id or "#V#org"

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        _fake_resolve_event_actor_context,
    )
    monkeypatch.setattr(
        "src.backend.services.organisation_membership_service.get_user_memberships",
        lambda user_concept_id: {
            "user_concept_id": user_concept_id,
            "memberships": [{"organisation_concept_id": "#V#org", "role": "member"}],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.get_accepted_invite_for_user_session",
        lambda **kwargs: {
            "invite_id": "invite-join",
            "session_id": kwargs["session_id"],
            "organisation_concept_id": "#V#org",
            "status": "accepted",
            "inviter_user_id": "#V#owner",
        }
        if kwargs["user_concept_id"] == "#V#invitee"
        else None,
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda **kwargs: "#V#owner",
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.create_chat_session",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "session_name": kwargs.get("session_name") or "Joined Session",
            "namespace": kwargs.get("namespace") or "#V#invitee@org",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.episode_logging_service.log_episode",
        lambda **kwargs: "episode-join",
    )

    success_payload = gateway.invoke(
        "shared_conversation_join_session",
        {
            "session_id": "session-join",
            "user_concept_id": "#V#invitee",
            "organisation_concept_id": "#V#org",
            "actor_concept_id": "#V#github_copilot_instance",
        },
    ).payload
    assert success_payload.get("success") is True
    assert success_payload.get("joined") is True
    assert success_payload.get("access_mode") == "invitee"
    assert success_payload.get("actor_concept_id") == "#V#github_copilot_instance"
    _assert_schema_conformance(gateway, "shared_conversation_join_session", success_payload)

    error_payload = gateway.invoke(
        "shared_conversation_join_session",
        {
            "session_id": "session-join",
            "user_concept_id": "#V#other_user",
            "organisation_concept_id": "#V#org",
        },
    ).payload
    assert error_payload.get("success") is False
    assert error_payload.get("error_code") == "PERMISSION_DENIED"
    _assert_schema_conformance(gateway, "shared_conversation_join_session", error_payload)


def test_shared_conversation_list_invites_read_path_does_not_bootstrap_actor_identity(
    monkeypatch,
):
    gateway = _build_gateway()
    bootstrap_calls: list[dict] = []

    def _fake_resolve_event_actor_context(*, user_id=None, org_id=None, namespace=None):
        return user_id or "#V#invitee", org_id or "#V#org"

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        _fake_resolve_event_actor_context,
    )
    monkeypatch.setattr(
        "src.backend.services.coding_agent_identity_bootstrap_service.ensure_coding_agent_identity_concepts",
        lambda **kwargs: bootstrap_calls.append(dict(kwargs))
        or {"cached": False, "errors": []},
    )
    monkeypatch.setattr(
        "src.backend.services.organisation_membership_service.get_user_memberships",
        lambda user_concept_id: {
            "user_concept_id": user_concept_id,
            "memberships": [{"organisation_concept_id": "#V#org", "role": "member"}],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.list_invites_for_user",
        lambda **kwargs: [
            {
                "invite_id": "invite-read-1",
                "session_id": kwargs.get("session_id") or "session-read-1",
                "inviter_user_id": "#V#owner",
                "invitee_user_id": kwargs.get("user_concept_id"),
                "organisation_concept_id": "#V#org",
                "status": kwargs.get("status") or "pending",
            }
        ],
    )

    payload = gateway.invoke(
        "shared_conversation_list_invites",
        {
            "user_concept_id": "#V#invitee",
            "organisation_concept_id": "#V#org",
            "direction": "incoming",
            "status": "pending",
            "actor_concept_id": "#V#github_copilot_instance",
        },
    ).payload

    assert payload.get("success") is True
    assert payload.get("count") == 1
    assert bootstrap_calls == []
    _assert_schema_conformance(gateway, "shared_conversation_list_invites", payload)
