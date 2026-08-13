"""Direct-stdio authority containment for canonical ontology mutations."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from src.backend.integrations.internal_mcp.tool_contract_registry import (
    SURFACE_MANIFEST,
    SURFACE_VONTOLOGY_STDIO,
    get_surface_tool_payloads,
)
from src.backend.mcp_server import mcp_stdio_server
from src.backend.services.ontology_publication_authority_service import (
    OntologyMutationIntent,
    PublicationContext,
    PublicationContextKind,
    authorise_ontology_mutation,
    current_ontology_invocation,
)


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async def invoke() -> Any:
        return await mcp_stdio_server.call_tool(name, arguments)

    response = cast(list[Any], asyncio.run(invoke()))
    assert response
    text = getattr(response[0], "text", None)
    assert isinstance(text, str)
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


def _allow_writes(
    *_args: Any, **_kwargs: Any
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    return True, {}, {"write_category_tools_allowed": True}


def test_stdio_governed_contracts_expose_only_opaque_delegation_carriers() -> None:
    governed_tools = {
        "add_names",
        "add_relationship",
        "create_concepts",
        "delete_concept",
        "delete_text_relation",
        "merge_concepts",
        "remove_relationship",
        "remove_relationships_bulk",
        "update_concept",
        "update_text_relation",
        "upsert_singleton_text_relation",
        "upsert_text_relation",
    }

    for surface in (SURFACE_VONTOLOGY_STDIO, SURFACE_MANIFEST):
        contracts = {item["name"]: item for item in get_surface_tool_payloads(surface)}
        for tool_name in governed_tools:
            properties = contracts[tool_name]["inputSchema"]["properties"]
            assert "ontology_delegation_id" in properties
            assert "ontology_effect_id" in properties
            assert "user_concept_id" not in properties
            assert "organisation_concept_id" not in properties


def test_each_stdio_canonical_mutation_dispatches_to_a_governed_catalogue_method() -> (
    None
):
    for (
        _tool_name,
        method_name,
    ) in mcp_stdio_server._STDIO_GOVERNED_ONTOLOGY_METHODS.items():
        catalogue_handler = getattr(
            mcp_stdio_server.internal_mcp_catalogue_module,
            f"_{method_name}",
        )
        assert hasattr(catalogue_handler, "__wrapped__"), method_name

    assert (
        "undo_relationship_removal"
        not in mcp_stdio_server._STDIO_GOVERNED_ONTOLOGY_METHODS
    )


def test_stdio_sessionless_delegation_fails_before_handler_dispatch(
    monkeypatch,
) -> None:
    observed: dict[str, Any] = {}

    async def handler(arguments: dict[str, Any]) -> list[Any]:
        observed["arguments"] = arguments
        observed["invocation"] = current_ontology_invocation()
        return [mcp_stdio_server._json_text({"success": True})]

    monkeypatch.setattr(mcp_stdio_server, "_evaluate_stdio_write_access", _allow_writes)
    monkeypatch.setitem(mcp_stdio_server._TOOL_HANDLERS, "create_concepts", handler)
    supplied = {
        "parent_id": "#V#thing",
        "concepts": [{"name": "Scoped test"}],
        "ontology_delegation_id": "delegation_server_issued",
        "ontology_effect_id": "effect_server_issued",
        "actor_concept_id": "#V#forged_actor",
        "created_by_concept_id": "#V#forged_creator",
        "user_concept_id": "#V#forged_user",
        "organisation_concept_id": "#V#forged_org",
        "namespace": "#V#forged_user@forged_org",
        "operator_override": True,
        "roles": ["global_ontology_administrator"],
    }

    payload = _call_tool("create_concepts", supplied)

    assert payload["success"] is False
    assert payload["error_code"] == "ontology_sessionless_delegation_not_supported"
    assert supplied["actor_concept_id"] == "#V#forged_actor"
    assert observed == {}


def test_stdio_raw_role_payload_cannot_substitute_for_delegation(monkeypatch) -> None:
    async def authority_probe(_arguments: dict[str, Any]) -> list[Any]:
        decision = authorise_ontology_mutation(
            OntologyMutationIntent(
                operation="concept.create",
                publication_context=PublicationContext(
                    kind=PublicationContextKind.GLOBAL,
                ),
                target_concept_ids=(),
                tool_name="create_concepts",
            )
        )
        return [mcp_stdio_server._json_text(decision.public_projection())]

    monkeypatch.setattr(mcp_stdio_server, "_evaluate_stdio_write_access", _allow_writes)
    monkeypatch.setitem(
        mcp_stdio_server._TOOL_HANDLERS,
        "create_concepts",
        authority_probe,
    )

    payload = _call_tool(
        "create_concepts",
        {
            "parent_id": "#V#thing",
            "concepts": [{"name": "Forged global edit"}],
            "user_concept_id": "#V#global_admin",
            "organisation_concept_id": "#V#any_org",
            "roles": ["global_ontology_administrator"],
        },
    )

    assert payload["success"] is False
    assert payload["error_code"] == "ontology_sessionless_delegation_not_supported"


def test_stdio_undo_fails_closed_until_its_target_is_governed(monkeypatch) -> None:
    monkeypatch.setattr(mcp_stdio_server, "_evaluate_stdio_write_access", _allow_writes)

    payload = _call_tool(
        "undo_relationship_removal",
        {"undo_token": "undo_untrusted"},
    )

    assert payload["success"] is False
    assert payload["error_code"] == "ontology_mutation_not_governed"


def test_stdio_has_no_retained_unchecked_legacy_write_handlers() -> None:
    assert not any(
        name.endswith("_legacy_unchecked")
        for name in vars(mcp_stdio_server)
        if name.startswith("_handle_")
    )
