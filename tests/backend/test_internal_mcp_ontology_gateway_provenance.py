"""Gateway-level provenance for canonical ontology mutations."""

from __future__ import annotations

from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _gateway(handler) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            # This exact canonical mutation name is deliberately used so the
            # gateway takes the production governed-write path.
            name="add_relationship",
            handler=handler,
            input_schema=Schema(
                required={},
                optional={
                    "ontology_delegation_id": str,
                    "ontology_effect_id": str,
                    "executing_agent_concept_id": str,
                    "audience": str,
                    "user_concept_id": str,
                },
                allow_unknown=False,
            ),
            category="write",
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_sessionless_internal_mcp_delegation_fails_before_handler_dispatch() -> None:
    captured = {}

    def handler(**kwargs):
        captured["arguments"] = kwargs
        return {"success": True}

    result = (
        _gateway(handler)
        .invoke(
            "add_relationship",
            {
                "ontology_delegation_id": "oag_server_issued",
                "ontology_effect_id": "effect-123",
                # These are a model/client forgery attempt.  They remain ordinary
                # handler data and cannot set execution provenance.
                "executing_agent_concept_id": "#V#forged_administrator_agent",
                "audience": "forged_audience",
                "user_concept_id": "#V#forged_user",
            },
        )
        .payload
    )

    assert result["success"] is False
    assert result["error_code"] == "ontology_sessionless_delegation_not_supported"
    assert captured == {}


def test_unlabelled_internal_mcp_ontology_write_without_delegation_is_denied() -> None:
    from src.backend.services.ontology_publication_authority_service import (
        OntologyMutationIntent,
        PublicationContext,
        authorise_ontology_mutation,
    )

    def handler(**_kwargs):
        decision = authorise_ontology_mutation(
            OntologyMutationIntent(
                operation="relationship.add",
                publication_context=PublicationContext.global_context(),
                target_concept_ids=("#V#source", "#V#target"),
                tool_name="add_relationship",
                predicate="#V#is_a",
                delta={"target_concept_id": "#V#target"},
            )
        )
        return {"success": False, "error_code": decision.reason_code}

    result = _gateway(handler).invoke("add_relationship", {}).payload

    assert result["error_code"] == "ontology_agent_delegation_required"


def test_prebound_trusted_invocation_cannot_be_overridden_by_gateway_payload() -> None:
    from src.backend.services.ontology_publication_authority_service import (
        bind_ontology_invocation,
        current_ontology_invocation,
        override_current_actor,
    )

    def handler(**_kwargs):
        invocation = current_ontology_invocation()
        assert invocation is not None
        return {
            "success": True,
            "surface": invocation.surface,
            "agent": invocation.executing_agent_concept_id,
            "audience": invocation.audience,
            "delegation_id": invocation.delegation_id,
            "effect_id": invocation.effect_id,
        }

    with (
        override_current_actor("#V#trusted_actor", "#V#trusted_org"),
        bind_ontology_invocation(
            surface="ordinary_turn",
            executing_agent_concept_id="#V#von_system",
            audience="adaptive_turn",
            delegation_id="oag_adaptive",
            effect_id="adaptive-effect",
            turn_id="turn-1",
        ),
    ):
        result = (
            _gateway(handler)
            .invoke(
                "add_relationship",
                {
                    "ontology_delegation_id": "oag_forged",
                    "ontology_effect_id": "forged-effect",
                },
            )
            .payload
        )

    assert result == {
        "success": True,
        "surface": "ordinary_turn",
        "agent": "#V#von_system",
        "audience": "adaptive_turn",
        "delegation_id": "oag_adaptive",
        "effect_id": "adaptive-effect",
    }


def test_prebound_direct_human_invocation_is_preserved() -> None:
    from src.backend.services.ontology_publication_authority_service import (
        bind_ontology_invocation,
        current_ontology_invocation,
    )

    def handler(**_kwargs):
        invocation = current_ontology_invocation()
        assert invocation is not None
        return {
            "success": True,
            "surface": invocation.surface,
            "agent": invocation.executing_agent_concept_id,
            "delegation_id": invocation.delegation_id,
        }

    with bind_ontology_invocation(surface="trusted_http_handoff"):
        result = _gateway(handler).invoke("add_relationship", {}).payload

    assert result == {
        "success": True,
        "surface": "trusted_http_handoff",
        "agent": None,
        "delegation_id": None,
    }
