from __future__ import annotations

import pytest

from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.security.access_control import (
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
    override_current_actor,
)
from src.backend.services.workflow_actor_scope_service import (
    WORKFLOW_ACTOR_AUTHORITY_REQUIRED,
    WORKFLOW_ACTOR_SCOPE_MISMATCH,
    WorkflowActorScopeError,
    resolve_authoritative_workflow_actor_scope,
    resolve_provenance_bound_workflow_actor_scope,
)


def test_user_only_namespace_is_compatible_with_ambient_organisation() -> None:
    scope = resolve_authoritative_workflow_actor_scope(
        claimed_user_id="#V#member",
        claimed_org_id="#V#trusted_org",
        claimed_namespace="#V#member",
        allow_unscoped_claims=False,
        ambient_user_id="#V#member",
        ambient_org_id="#V#trusted_org",
        ambient_context_supplied=True,
    )

    assert scope.user_concept_id == "#V#member"
    assert scope.organisation_concept_id == "#V#trusted_org"
    assert scope.namespace == "#V#member@trusted_org"
    assert scope.source == "ambient_actor_authority"


def test_ambient_actor_rejects_contradictory_full_namespace() -> None:
    with pytest.raises(WorkflowActorScopeError) as exc_info:
        resolve_authoritative_workflow_actor_scope(
            claimed_user_id="#V#owner",
            claimed_org_id="#V#trusted_org",
            claimed_namespace="#V#owner@trusted_org",
            allow_unscoped_claims=False,
            ambient_user_id="#V#outsider",
            ambient_org_id="#V#other_org",
            ambient_context_supplied=True,
        )

    assert exc_info.value.reason == WORKFLOW_ACTOR_SCOPE_MISMATCH
    assert set(exc_info.value.mismatch_fields) == {
        "user_id",
        "org_id",
        "namespace",
    }


def test_untrusted_unscoped_actor_claim_fails_closed() -> None:
    with pytest.raises(WorkflowActorScopeError) as exc_info:
        resolve_authoritative_workflow_actor_scope(
            claimed_user_id="#V#owner",
            claimed_org_id="#V#trusted_org",
            claimed_namespace="#V#owner@trusted_org",
            allow_unscoped_claims=False,
            ambient_user_id=None,
            ambient_org_id=None,
            ambient_context_supplied=False,
        )

    assert exc_info.value.reason == WORKFLOW_ACTOR_AUTHORITY_REQUIRED


def test_explicit_trusted_operator_scope_accepts_consistent_claims() -> None:
    scope = resolve_authoritative_workflow_actor_scope(
        claimed_user_id="#V#operator",
        claimed_org_id="#V#trusted_org",
        claimed_namespace="#V#operator@trusted_org",
        allow_unscoped_claims=True,
        ambient_user_id=None,
        ambient_org_id=None,
        ambient_context_supplied=False,
    )

    assert scope.user_concept_id == "#V#operator"
    assert scope.organisation_concept_id == "#V#trusted_org"
    assert scope.namespace == "#V#operator@trusted_org"
    assert scope.source == "trusted_unscoped_claim"


def test_payload_fallback_provenance_cannot_authorise_its_own_claims() -> None:
    with pytest.raises(WorkflowActorScopeError) as exc_info:
        resolve_provenance_bound_workflow_actor_scope(
            claimed_user_id="#V#owner",
            claimed_org_id="#V#trusted_org",
            claimed_namespace="#V#owner@trusted_org",
            actor_context_source="tool_payload_fallback",
        )

    assert exc_info.value.reason == WORKFLOW_ACTOR_AUTHORITY_REQUIRED


def test_trusted_operator_provenance_accepts_payload_only_claims() -> None:
    scope = resolve_provenance_bound_workflow_actor_scope(
        claimed_user_id="#V#operator",
        claimed_org_id="#V#trusted_org",
        claimed_namespace="#V#operator@trusted_org",
        actor_context_source="trusted_operator_payload_fallback",
    )

    assert scope.user_concept_id == "#V#operator"
    assert scope.organisation_concept_id == "#V#trusted_org"
    assert scope.namespace == "#V#operator@trusted_org"
    assert scope.source == "trusted_unscoped_claim"


def test_actorless_execution_remains_available_for_public_workflows() -> None:
    scope = resolve_provenance_bound_workflow_actor_scope(
        actor_context_source="tool_payload_fallback",
    )

    assert scope.user_concept_id is None
    assert scope.organisation_concept_id is None
    assert scope.namespace is None


def test_gateway_does_not_augment_partial_ambient_actor_from_payload() -> None:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="actor_scope_probe",
            handler=lambda **_kwargs: {
                "user_id": get_effective_user_concept_id(),
                "org_id": get_effective_organisation_concept_id(),
            },
            input_schema=Schema(
                optional={
                    "user_id": (str,),
                    "org_id": (str,),
                },
                allow_unknown=True,
            ),
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=True,
    )

    with override_current_actor("#V#ambient_user", None):
        payload = gateway.invoke(
            "actor_scope_probe",
            {
                "user_id": "#V#forged_user",
                "org_id": "#V#forged_trusted_org",
            },
        ).payload

    assert payload == {
        "user_id": "#V#ambient_user",
        "org_id": None,
    }
