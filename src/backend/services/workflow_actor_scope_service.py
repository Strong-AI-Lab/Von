"""Canonical actor authority for workflow discovery and execution support paths.

Workflow IDs, namespaces, and actor hints can arrive through tool arguments or
persisted execution records, but those values are not authentication authority.
This module keeps the support-layer rule explicit: an ambient authenticated or
workflow actor wins, contradictory claims fail closed, and caller claims are
accepted only on a deliberately trusted unscoped/operator path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..security.access_control import (
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
)
from .namespace_service import (
    derive_actor_context_from_namespace,
    resolve_canonical_namespace,
)


WORKFLOW_ACTOR_SCOPE_MISMATCH = "workflow_actor_scope_mismatch"
WORKFLOW_ACTOR_NAMESPACE_INVALID = "workflow_actor_namespace_invalid"
WORKFLOW_ACTOR_AUTHORITY_REQUIRED = "workflow_actor_authority_required"

_AUTO_AMBIENT = object()


def _normalise_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_concept_id(value: Any) -> str | None:
    text = _normalise_text(value)
    if text is None:
        return None
    return text if text.startswith("#") else f"#V#{text}"


@dataclass(frozen=True)
class WorkflowActorScope:
    user_concept_id: str | None
    organisation_concept_id: str | None
    namespace: str | None
    source: str


class WorkflowActorScopeError(ValueError):
    """Typed, metadata-bounded rejection of contradictory actor scope."""

    def __init__(
        self,
        reason: str,
        *,
        mismatch_fields: tuple[str, ...] = (),
    ) -> None:
        self.reason = str(reason or WORKFLOW_ACTOR_SCOPE_MISMATCH)
        self.mismatch_fields = tuple(
            field
            for field in mismatch_fields
            if field in {"user_id", "org_id", "namespace"}
        )
        super().__init__(self.reason)


def resolve_authoritative_workflow_actor_scope(
    *,
    claimed_user_id: Any = None,
    claimed_org_id: Any = None,
    claimed_namespace: Any = None,
    allow_unscoped_claims: bool,
    ambient_user_id: Any = _AUTO_AMBIENT,
    ambient_org_id: Any = _AUTO_AMBIENT,
    ambient_context_supplied: bool | None = None,
) -> WorkflowActorScope:
    """Resolve one workflow actor without promoting claims over ambient authority.

    ``ambient_*`` defaults to the canonical access-control context. Callers such
    as the internal MCP gateway may pass the actor that existed *before* it
    installed tool-payload fallback context, together with
    ``ambient_context_supplied``. This prevents a payload-derived override from
    being mistaken for authenticated authority.
    """

    raw_namespace = _normalise_text(claimed_namespace)
    claimed_canonical_namespace = (
        resolve_canonical_namespace(raw_namespace) if raw_namespace else None
    )
    if raw_namespace and claimed_canonical_namespace is None:
        raise WorkflowActorScopeError(WORKFLOW_ACTOR_NAMESPACE_INVALID)

    namespace_user_id, namespace_org_id = derive_actor_context_from_namespace(
        claimed_canonical_namespace
    )
    explicit_user_id = _normalise_concept_id(claimed_user_id)
    explicit_org_id = _normalise_concept_id(claimed_org_id)

    claim_mismatches: list[str] = []
    if (
        namespace_user_id
        and explicit_user_id
        and namespace_user_id != explicit_user_id
    ):
        claim_mismatches.append("user_id")
    if (
        namespace_org_id
        and explicit_org_id is not None
        and namespace_org_id != explicit_org_id
    ):
        claim_mismatches.append("org_id")
    if claim_mismatches:
        raise WorkflowActorScopeError(
            WORKFLOW_ACTOR_SCOPE_MISMATCH,
            mismatch_fields=tuple(claim_mismatches),
        )

    claimed_user = namespace_user_id or explicit_user_id
    claimed_org = namespace_org_id or explicit_org_id
    if claimed_canonical_namespace is None and claimed_user:
        claimed_canonical_namespace = resolve_canonical_namespace(
            None,
            claimed_user,
            claimed_org,
        )

    resolved_ambient_user = _normalise_concept_id(
        get_effective_user_concept_id()
        if ambient_user_id is _AUTO_AMBIENT
        else ambient_user_id
    )
    resolved_ambient_org = _normalise_concept_id(
        get_effective_organisation_concept_id()
        if ambient_org_id is _AUTO_AMBIENT
        else ambient_org_id
    )
    has_ambient_authority = (
        bool(resolved_ambient_user or resolved_ambient_org)
        if ambient_context_supplied is None
        else bool(ambient_context_supplied)
    )

    if has_ambient_authority:
        ambient_namespace = (
            resolve_canonical_namespace(
                None,
                resolved_ambient_user,
                resolved_ambient_org,
            )
            if resolved_ambient_user
            else None
        )
        ambient_mismatches: list[str] = []
        if claimed_user is not None and claimed_user != resolved_ambient_user:
            ambient_mismatches.append("user_id")
        if claimed_org is not None and claimed_org != resolved_ambient_org:
            ambient_mismatches.append("org_id")
        if namespace_user_id and namespace_user_id != resolved_ambient_user:
            ambient_mismatches.append("namespace")
        if namespace_org_id and namespace_org_id != resolved_ambient_org:
            ambient_mismatches.append("namespace")
        if ambient_mismatches:
            raise WorkflowActorScopeError(
                WORKFLOW_ACTOR_SCOPE_MISMATCH,
                mismatch_fields=tuple(dict.fromkeys(ambient_mismatches)),
            )
        return WorkflowActorScope(
            user_concept_id=resolved_ambient_user,
            organisation_concept_id=resolved_ambient_org,
            namespace=ambient_namespace,
            source="ambient_actor_authority",
        )

    if not allow_unscoped_claims:
        raise WorkflowActorScopeError(WORKFLOW_ACTOR_AUTHORITY_REQUIRED)

    return WorkflowActorScope(
        user_concept_id=claimed_user,
        organisation_concept_id=claimed_org,
        namespace=claimed_canonical_namespace,
        source="trusted_unscoped_claim",
    )


def resolve_provenance_bound_workflow_actor_scope(
    *,
    claimed_user_id: Any = None,
    claimed_org_id: Any = None,
    claimed_namespace: Any = None,
    actor_context_source: str | None = None,
    preexisting_actor_context: tuple[str | None, str | None] | None = None,
) -> WorkflowActorScope:
    """Resolve workflow claims against actor provenance at an execution seam.

    A gateway may temporarily install actor fields copied from a tool payload so
    ordinary tools can consume their existing schemas.  That compatibility
    context is not authentication authority.  Only an actor that existed before
    the gateway invocation, or the deliberately configured trusted-operator
    fallback, may authorise claimed workflow identity.

    An actorless call with no identity claims remains valid so public workflows
    can execute without manufacturing an identity.  Once any actor or namespace
    claim is supplied, an authoritative source is required.
    """

    source = _normalise_text(actor_context_source)
    ambient_kwargs: dict[str, Any] = {}
    trusted_unscoped = source == "trusted_operator_payload_fallback"

    if preexisting_actor_context is not None:
        ambient_kwargs = {
            "ambient_user_id": preexisting_actor_context[0],
            "ambient_org_id": preexisting_actor_context[1],
            "ambient_context_supplied": True,
        }
    elif source in {
        "tool_payload_fallback",
        "trusted_operator_payload_fallback",
    }:
        # The access-control ContextVars may currently contain these same
        # payload values.  Override auto-discovery explicitly so they cannot
        # prove their own authority.
        ambient_kwargs = {
            "ambient_user_id": None,
            "ambient_org_id": None,
            "ambient_context_supplied": False,
        }

    has_identity_claim = any(
        _normalise_text(value) is not None
        for value in (claimed_user_id, claimed_org_id, claimed_namespace)
    )
    return resolve_authoritative_workflow_actor_scope(
        claimed_user_id=claimed_user_id,
        claimed_org_id=claimed_org_id,
        claimed_namespace=claimed_namespace,
        allow_unscoped_claims=trusted_unscoped or not has_identity_claim,
        **ambient_kwargs,
    )


__all__ = [
    "WORKFLOW_ACTOR_AUTHORITY_REQUIRED",
    "WORKFLOW_ACTOR_NAMESPACE_INVALID",
    "WORKFLOW_ACTOR_SCOPE_MISMATCH",
    "WorkflowActorScope",
    "WorkflowActorScopeError",
    "resolve_authoritative_workflow_actor_scope",
    "resolve_provenance_bound_workflow_actor_scope",
]
