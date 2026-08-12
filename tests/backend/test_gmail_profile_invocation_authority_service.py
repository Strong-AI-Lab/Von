from __future__ import annotations

import pytest


def _runtime_profiles(*profile_ids: str):
    from src.backend.integrations.google.gmail_service import GmailProfile

    return {
        profile_id: GmailProfile(
            profile_id=profile_id,
            token_path=f"/nonexistent/{profile_id}.json",
        )
        for profile_id in profile_ids
    }


def test_payload_fallback_namespace_cannot_forge_gmail_actor(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
    )
    from src.backend.security.access_control import override_current_actor
    from src.backend.services.gmail_profile_invocation_authority_service import (
        GmailInvocationAuthorityError,
        authorise_gmail_profile_for_invocation,
    )

    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: pytest.fail("forged payload must be rejected before profile lookup"),
    )
    with override_current_actor("#V#victim", "#V#org"), (
        bind_internal_mcp_actor_context_source("tool_payload_fallback")
    ), pytest.raises(GmailInvocationAuthorityError) as exc_info:
        authorise_gmail_profile_for_invocation("victim-profile")

    assert exc_info.value.reason_code == "authenticated_actor_context_required"


def test_authenticated_actor_profile_is_canonical_and_audited_to_actor(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import mail_profile_resource_vontology_service
    from src.backend.services.gmail_profile_invocation_authority_service import (
        authorise_gmail_profile_for_invocation,
    )

    monkeypatch.setattr(
        mail_profile_resource_vontology_service,
        "resolve_authorised_gmail_profile_for_user",
        lambda **kwargs: {
            "success": kwargs == {
                "user_concept_id": "#V#actor",
                "requested_profile_id": "#V#gmail_profile_actor_mail",
            },
            "reason_code": "authorised_mail_profile_resolved",
            "profile_id": "actor-mail",
            "profile_resource_concept_id": "#V#gmail_profile_actor_mail",
        },
    )
    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: _runtime_profiles("actor-mail", "foreign-mail"),
    )

    with override_current_actor("#V#actor", "#V#org"):
        authority = authorise_gmail_profile_for_invocation(
            "#V#gmail_profile_actor_mail"
        )

    assert authority.profile_id == "actor-mail"
    assert authority.audit_namespace == "#V#actor@org"
    assert authority.principal.kind == "authenticated_actor"


def test_authenticated_actor_cannot_select_foreign_configured_profile(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import mail_profile_resource_vontology_service
    from src.backend.services.gmail_profile_invocation_authority_service import (
        GmailInvocationAuthorityError,
        authorise_gmail_profile_for_invocation,
    )

    monkeypatch.setattr(
        mail_profile_resource_vontology_service,
        "resolve_authorised_gmail_profile_for_user",
        lambda **_kwargs: {
            "success": False,
            "reason_code": "mail_profile_not_authorised_for_actor",
            "profile_id": None,
        },
    )
    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: _runtime_profiles("actor-mail", "foreign-mail"),
    )

    with override_current_actor("#V#actor", "#V#org"), pytest.raises(
        GmailInvocationAuthorityError
    ) as exc_info:
        authorise_gmail_profile_for_invocation("foreign-mail")

    assert exc_info.value.reason_code == "gmail_profile_not_authorised"
    assert exc_info.value.details == {
        "authority_reason": "mail_profile_not_authorised_for_actor"
    }


def test_trusted_local_operator_can_select_configured_profile(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
    )
    from src.backend.services.gmail_profile_invocation_authority_service import (
        authorise_gmail_profile_for_invocation,
    )

    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: _runtime_profiles("operator-profile"),
    )
    with bind_internal_mcp_actor_context_source(
        "trusted_operator_payload_fallback"
    ):
        authority = authorise_gmail_profile_for_invocation("operator-profile")

    assert authority.profile_id == "operator-profile"
    assert authority.audit_namespace == "trusted_local_operator"
    assert authority.principal.kind == "trusted_local_operator"


def test_actor_send_requires_selected_profile_to_represent_von_system(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import mail_profile_resource_vontology_service

    monkeypatch.setattr(
        mail_profile_resource_vontology_service,
        "resolve_authorised_gmail_profile_for_user",
        lambda **_kwargs: {
            "success": True,
            "reason_code": "authorised_mail_profile_resolved",
            "profile_id": "actor-mail",
            "profile_resource_concept_id": "#V#gmail_profile_actor_mail",
        },
    )
    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: _runtime_profiles("actor-mail"),
    )
    monkeypatch.setattr(
        mail_profile_resource_vontology_service,
        "mail_profile_resource_represents_identity",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        gmail_service,
        "send_message",
        lambda **_kwargs: pytest.fail("identity denial must precede Gmail send"),
    )

    with override_current_actor("#V#actor", "#V#org"):
        result = catalogue._gmail_send_message(
            profile="#V#gmail_profile_actor_mail",
            to="recipient@example.test",
            subject="Identity boundary",
            body_text="This must not be sent.",
            allow_send=True,
            request_id="actor-identity-denial-1",
        )

    assert result["error_code"] == "gmail_agent_identity_not_represented"


def test_trusted_operator_send_requires_configured_profile_to_represent_von_system(
    monkeypatch,
):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
    )
    from src.backend.services import mail_profile_resource_vontology_service

    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: _runtime_profiles("operator-profile"),
    )
    monkeypatch.setattr(
        mail_profile_resource_vontology_service,
        "mail_profile_resource_represents_identity",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        gmail_service,
        "send_message",
        lambda **_kwargs: pytest.fail("identity denial must precede Gmail send"),
    )

    with bind_internal_mcp_actor_context_source(
        "trusted_operator_payload_fallback"
    ):
        result = catalogue._gmail_send_message(
            profile="operator-profile",
            to="recipient@example.test",
            subject="Identity boundary",
            body_text="This must not be sent.",
            allow_send=True,
            request_id="operator-identity-denial-1",
        )

    assert result["error_code"] == "gmail_agent_identity_not_represented"


def test_unscoped_direct_call_is_not_implicitly_operator():
    from src.backend.services.gmail_profile_invocation_authority_service import (
        GmailInvocationAuthorityError,
        require_trusted_gmail_operator,
    )

    with pytest.raises(GmailInvocationAuthorityError) as exc_info:
        require_trusted_gmail_operator()

    assert exc_info.value.reason_code == "authenticated_actor_context_required"


def test_catalogue_global_profile_listing_is_operator_only(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
    )
    from src.backend.security.access_control import override_current_actor

    calls = 0

    def _list_profile_summaries():
        nonlocal calls
        calls += 1
        return [
            {
                "profile_id": "operator-profile",
                "authorised_email": "operator@example.test",
            }
        ]

    monkeypatch.setattr(
        gmail_service,
        "list_profile_summaries",
        _list_profile_summaries,
    )

    with override_current_actor("#V#actor", "#V#org"):
        actor_result = catalogue._gmail_list_profiles()
    with bind_internal_mcp_actor_context_source(
        "trusted_operator_payload_fallback"
    ):
        operator_result = catalogue._gmail_list_profiles()

    assert actor_result["error_code"] == "gmail_operator_authority_required"
    assert operator_result == {
        "profiles": [
            {
                "profile_id": "operator-profile",
                "authorised_email": "operator@example.test",
            }
        ],
        "count": 1,
    }
    assert calls == 1


@pytest.mark.parametrize(
    ("tool_name", "payload"),
    (
        (
            "gmail_list_messages",
            {
                "profile": "victim-profile",
            },
        ),
        (
            "gmail_get_auth_config",
            {
                "profile_id": "victim-profile",
                "namespace": "#V#victim@victim_org",
            },
        ),
        (
            "gmail_send_message",
            {
                "profile": "victim-profile",
                "to": "recipient@example.test",
                "subject": "Forged actor check",
                    "body_text": "This must not be sent.",
                    "allow_send": True,
                    "request_id": "forged-actor-send-1",
                    "namespace": "#V#victim@victim_org",
            },
        ),
    ),
)
def test_default_gateway_cannot_promote_payload_identity_for_gmail(
    monkeypatch,
    tool_name,
    payload,
):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: pytest.fail("payload identity must be rejected before profile lookup"),
    )
    monkeypatch.setattr(
        gmail_service,
        "list_messages",
        lambda **_kwargs: pytest.fail("payload identity must not read Gmail"),
    )
    monkeypatch.setattr(
        gmail_service,
        "send_message",
        lambda **_kwargs: pytest.fail("payload identity must not send Gmail"),
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(tool_name, dict(payload)).payload

    assert result["error_code"] == "authenticated_actor_context_required"


def test_foreign_profile_is_denied_for_auth_read_and_scope_write(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import mail_profile_resource_vontology_service

    monkeypatch.setattr(
        mail_profile_resource_vontology_service,
        "resolve_authorised_gmail_profile_for_user",
        lambda **_kwargs: {
            "success": False,
            "reason_code": "mail_profile_not_authorised_for_actor",
            "profile_id": None,
        },
    )

    with override_current_actor("#V#actor", "#V#org"):
        auth_result = catalogue._gmail_get_auth_config(
            profile_id="foreign-profile"
        )
        scope_result = catalogue._gmail_set_profile_scope(
            profile_id="foreign-profile",
            scopes=["https://www.googleapis.com/auth/gmail.modify"],
        )

    assert auth_result["error_code"] == "gmail_profile_not_authorised"
    assert scope_result["error_code"] == "gmail_profile_not_authorised"
