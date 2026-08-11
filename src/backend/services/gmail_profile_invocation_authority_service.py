"""Bind Gmail profile use to trusted invocation provenance and actor authority.

Runtime Gmail aliases identify configured capabilities; they are not authority
grants.  This module is the reusable boundary shared by every InternalMCP
Gmail handler.  It deliberately distinguishes authenticated/workflow actors
from the narrow trusted-local operator route and never promotes identity
claims that arrived only in a tool payload.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

GmailPrincipalKind = Literal["authenticated_actor", "trusted_local_operator"]


@dataclass(frozen=True)
class GmailInvocationPrincipal:
    """Trusted principal established before a profile is selected."""

    kind: GmailPrincipalKind
    user_concept_id: str | None = None
    organisation_concept_id: str | None = None


@dataclass(frozen=True)
class GmailProfileInvocationAuthority:
    """Canonical profile and audit scope authorised for one invocation."""

    principal: GmailInvocationPrincipal
    profile_id: str
    profile_resource_concept_id: str | None
    audit_namespace: str


@dataclass(frozen=True)
class GmailInvocationAuthorityError(PermissionError):
    """Safe typed denial produced before Gmail or token state is touched."""

    reason_code: str
    safe_message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    suggestions: tuple[str, ...] = ()

    def __str__(self) -> str:
        return self.safe_message


def _clean_concept_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned.startswith("#V#") else None


def resolve_gmail_invocation_principal() -> GmailInvocationPrincipal:
    """Resolve only provenance that can authorise private Gmail access.

    A default gateway may install user/organisation values derived from the
    tool payload for compatibility.  Those values are useful routing hints but
    are explicitly not authentication, even when a forged ``namespace`` made
    them appear in the ambient access-control context.
    """

    from ..integrations.internal_mcp.gateway import (
        get_internal_mcp_actor_context_source,
        get_internal_mcp_preexisting_actor_context,
        internal_mcp_actor_context_is_trusted_local_operator,
        internal_mcp_actor_context_is_untrusted_payload_fallback,
    )
    from ..security.access_control import (
        get_effective_organisation_concept_id,
        get_effective_user_concept_id,
    )

    source = get_internal_mcp_actor_context_source()
    preexisting_actor = get_internal_mcp_preexisting_actor_context()

    if internal_mcp_actor_context_is_trusted_local_operator():
        return GmailInvocationPrincipal(kind="trusted_local_operator")

    if internal_mcp_actor_context_is_untrusted_payload_fallback():
        raise GmailInvocationAuthorityError(
            reason_code="authenticated_actor_context_required",
            safe_message=(
                "Gmail access requires an authenticated actor or the trusted "
                "local operator route; tool-payload identity is not authority."
            ),
            suggestions=(
                "Run the operation from an authenticated Von conversation",
            ),
        )

    if preexisting_actor is not None:
        user_concept_id = _clean_concept_id(preexisting_actor[0])
        organisation_concept_id = _clean_concept_id(preexisting_actor[1])
    elif source is None:
        # Deliberate in-process callers may already be inside a server-bound
        # actor context without crossing the gateway.  An unscoped direct call
        # is not silently upgraded to operator authority.
        user_concept_id = _clean_concept_id(get_effective_user_concept_id())
        organisation_concept_id = _clean_concept_id(
            get_effective_organisation_concept_id()
        )
    else:
        user_concept_id = None
        organisation_concept_id = None

    if user_concept_id is None:
        raise GmailInvocationAuthorityError(
            reason_code="authenticated_actor_context_required",
            safe_message=(
                "Gmail access requires an authenticated actor or the trusted "
                "local operator route."
            ),
            suggestions=(
                "Run the operation from an authenticated Von conversation",
            ),
        )

    return GmailInvocationPrincipal(
        kind="authenticated_actor",
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
    )


def require_trusted_gmail_operator() -> GmailInvocationPrincipal:
    """Require the explicit local-operator provenance for global Gmail data."""

    principal = resolve_gmail_invocation_principal()
    if principal.kind != "trusted_local_operator":
        raise GmailInvocationAuthorityError(
            reason_code="gmail_operator_authority_required",
            safe_message=(
                "Deployment-global Gmail profile access requires the trusted "
                "local operator route."
            ),
        )
    return principal


def _actor_profile_resolution(
    *,
    principal: GmailInvocationPrincipal,
    requested_profile: str,
) -> tuple[str, str]:
    from ..integrations.google import gmail_service
    from .mail_profile_resource_vontology_service import (
        list_authorised_gmail_profiles_for_user,
        resolve_authorised_gmail_profile_for_user,
    )

    assert principal.user_concept_id is not None
    authority = resolve_authorised_gmail_profile_for_user(
        user_concept_id=principal.user_concept_id,
        requested_profile_id=requested_profile,
    )

    # Compatibility: callers may name the mailbox address rather than its
    # runtime alias.  Resolve that address only among aliases already
    # represented as authorised for this actor; never search the deployment-
    # global profile set on an actor-scoped path.
    if (
        authority.get("success") is not True
        and authority.get("reason_code") == "mail_profile_not_authorised_for_actor"
        and "@" in requested_profile
    ):
        represented = list_authorised_gmail_profiles_for_user(
            user_concept_id=principal.user_concept_id,
        )
        represented_profiles = [
            dict(item)
            for item in represented.get("profiles") or []
            if isinstance(item, Mapping)
        ]
        try:
            configured_profiles = gmail_service.load_profiles_from_env()
        except Exception as exc:  # typed fail-closed boundary
            raise GmailInvocationAuthorityError(
                reason_code="gmail_profile_configuration_unavailable",
                safe_message=(
                    "Gmail profile configuration could not be checked before access."
                ),
                details={"exception_type": type(exc).__name__},
            ) from exc
        authorised_aliases = {
            str(item.get("profile_id") or "").strip()
            for item in represented_profiles
            if str(item.get("profile_id") or "").strip()
        }
        actor_profiles = {
            alias: profile
            for alias, profile in configured_profiles.items()
            if alias in authorised_aliases
        }
        if actor_profiles:
            try:
                resolved = gmail_service.get_profile(
                    requested_profile,
                    actor_profiles,
                )
            except Exception:  # noqa: BLE001 - return one non-enumerating denial
                resolved = None
            if resolved is not None:
                authority = resolve_authorised_gmail_profile_for_user(
                    user_concept_id=principal.user_concept_id,
                    requested_profile_id=resolved.profile_id,
                )

    profile_id = authority.get("profile_id")
    profile_resource_id = authority.get("profile_resource_concept_id")
    if authority.get("success") is not True or not isinstance(profile_id, str):
        raise GmailInvocationAuthorityError(
            reason_code="gmail_profile_not_authorised",
            safe_message=(
                "The requested Gmail profile is not represented as authorised "
                "for the authenticated actor."
            ),
            details={
                "authority_reason": authority.get("reason_code")
                or "mail_profile_not_authorised_for_actor"
            },
            suggestions=(
                "Use a Gmail profile represented as authorised for this actor",
            ),
        )

    cleaned_resource_id = (
        profile_resource_id.strip()
        if isinstance(profile_resource_id, str) and profile_resource_id.strip()
        else None
    )
    if cleaned_resource_id is None:
        raise GmailInvocationAuthorityError(
            reason_code="gmail_profile_not_authorised",
            safe_message=(
                "The requested Gmail profile lacks a represented actor-authority resource."
            ),
        )
    return profile_id.strip(), cleaned_resource_id


def _operator_profile_resolution(requested_profile: str) -> tuple[str, str | None]:
    from ..integrations.google import gmail_service
    from .mail_profile_resource_vontology_service import (
        gmail_profile_resource_concept_id,
    )

    try:
        configured_profiles = gmail_service.load_profiles_from_env()
    except Exception as exc:  # typed fail-closed boundary
        raise GmailInvocationAuthorityError(
            reason_code="gmail_profile_configuration_unavailable",
            safe_message=(
                "Gmail profile configuration could not be checked before access."
            ),
            details={"exception_type": type(exc).__name__},
        ) from exc

    runtime_request = requested_profile
    if requested_profile.startswith("#V#"):
        matching_aliases = [
            alias
            for alias in configured_profiles
            if gmail_profile_resource_concept_id(alias) == requested_profile
        ]
        if len(matching_aliases) == 1:
            runtime_request = matching_aliases[0]

    try:
        resolved = gmail_service.get_profile(runtime_request, configured_profiles)
    except Exception as exc:  # do not expose global aliases/emails
        raise GmailInvocationAuthorityError(
            reason_code="gmail_profile_not_configured",
            safe_message="The requested Gmail profile is not configured.",
        ) from exc

    canonical_profile_id = resolved.profile_id.strip()
    return (
        canonical_profile_id,
        gmail_profile_resource_concept_id(canonical_profile_id),
    )


def authorise_gmail_profile_for_invocation(
    requested_profile: Any,
) -> GmailProfileInvocationAuthority:
    """Return the one canonical runtime profile this invocation may use."""

    requested = (
        requested_profile.strip()
        if isinstance(requested_profile, str) and requested_profile.strip()
        else None
    )
    if requested is None:
        raise GmailInvocationAuthorityError(
            reason_code="missing_parameter",
            safe_message="Missing required parameter: profile",
            details={"missing": ["profile"]},
            suggestions=("Provide an actor-authorised Gmail profile resource",),
        )

    principal = resolve_gmail_invocation_principal()
    if principal.kind == "trusted_local_operator":
        profile_id, profile_resource_id = _operator_profile_resolution(requested)
        audit_namespace = "trusted_local_operator"
    else:
        profile_id, profile_resource_id = _actor_profile_resolution(
            principal=principal,
            requested_profile=requested,
        )
        from ..integrations.google import gmail_service
        from .namespace_service import derive_namespace_for_actor

        try:
            configured_profiles = gmail_service.load_profiles_from_env()
        except Exception as exc:  # typed fail-closed boundary
            raise GmailInvocationAuthorityError(
                reason_code="gmail_profile_configuration_unavailable",
                safe_message=(
                    "Gmail profile configuration could not be checked before access."
                ),
                details={"exception_type": type(exc).__name__},
            ) from exc
        if profile_id not in configured_profiles:
            raise GmailInvocationAuthorityError(
                reason_code="authorised_gmail_profile_unavailable",
                safe_message=(
                    "The actor-authorised Gmail profile is not configured in this runtime."
                ),
            )
        audit_namespace = derive_namespace_for_actor(
            principal.user_concept_id,
            principal.organisation_concept_id,
        )
        if audit_namespace is None:
            raise GmailInvocationAuthorityError(
                reason_code="actor_namespace_required",
                safe_message=(
                    "Could not derive an actor namespace for Gmail access."
                ),
            )

    return GmailProfileInvocationAuthority(
        principal=principal,
        profile_id=profile_id,
        profile_resource_concept_id=profile_resource_id,
        audit_namespace=audit_namespace,
    )


__all__ = [
    "GmailInvocationAuthorityError",
    "GmailInvocationPrincipal",
    "GmailProfileInvocationAuthority",
    "authorise_gmail_profile_for_invocation",
    "require_trusted_gmail_operator",
    "resolve_gmail_invocation_principal",
]
