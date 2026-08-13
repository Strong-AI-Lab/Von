"""Deterministic parent recovery tests for create_concepts (JVNAUTOSCI-1101)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from src.backend.integrations.internal_mcp.catalogue import (
    _create_concepts as _governed_create_concepts,
    build_default_catalogue,
)
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.mcp_server import mcp_stdio_server
from src.backend.security.access_control import is_bypass_enabled

_create_concepts = _governed_create_concepts.__wrapped__


def _patch_create_concept_success(monkeypatch):
    def _fake_create_vontology_concept(**kwargs):
        name = kwargs["new_concept_name"]
        concept_id = f"#V#{name.lower().replace(' ', '_')}"
        return {
            "success": True,
            "message": "created",
            "concept": {"concept_id": concept_id},
            "canonical_concept_id": concept_id,
            "input_name": name,
        }

    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _fake_create_vontology_concept,
    )


def test_create_concepts_recovers_workflow_definition_parent(monkeypatch):
    _patch_create_concept_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service.resolve_available_workflow_type_ids",
        lambda: ("#V#durable_workflow",),
    )

    def _fake_find_one(query, _projection=None):
        concept_id = query.get("concept_id")
        if concept_id == "#V#durable_workflow":
            return {"concept_id": concept_id}
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = _create_concepts(
        parent_id="#V#workflow_definition",
        concepts=[{"name": "Workflow Recovery Type", "kind": "type"}],
    )

    assert "error" not in result
    assert result.get("successful") == 1
    assert result.get("created_concept_ids") == ["#V#workflow_recovery_type"]
    assert (result.get("results") or [{}])[0].get("concept_id") == "#V#workflow_recovery_type"
    assert result.get("parent_id_used") == "#V#durable_workflow"
    resolution = result.get("parent_resolution") or {}
    assert resolution.get("fallback_used") is True
    assert resolution.get("resolved_parent_id") == "#V#durable_workflow"


def test_create_concepts_parent_not_found_includes_recovery_details(monkeypatch):
    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service.resolve_available_workflow_type_ids",
        lambda: ("#V#durable_workflow", "#V#ai_workflow"),
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda _query, _projection=None: None,
    )

    result = _create_concepts(
        parent_id="#V#workflow_definition",
        concepts=[{"name": "Unrecoverable Workflow Type", "kind": "type"}],
    )

    assert result.get("success") is False
    assert result.get("error_code") == "parent_not_found"
    details = result.get("error_details") or {}
    assert details.get("canonical_parent_id") == "#V#workflow_definition"
    assert details.get("fallback_candidates_checked") == [
        "#V#durable_workflow",
        "#V#ai_workflow",
    ]
    suggestions = result.get("suggestions") or []
    assert any("workflow supertype" in str(item) for item in suggestions)


def test_create_concepts_rejects_individual_as_semantic_parent(monkeypatch):
    created: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, _projection=None: (
            {
                "concept_id": "#V#example_research_lab",
                "kind": "individual",
            }
            if query.get("concept_id") == "#V#example_research_lab"
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: created.append(dict(kwargs)) or {"success": True},
    )

    result = _create_concepts(
        parent_id="#V#example_research_lab",
        concepts=[{"name": "Synthetic scenario", "kind": "individual"}],
    )

    assert result.get("success") is False
    assert result.get("error_code") == "parent_is_not_a_type"
    assert result.get("error_details") == {
        "original_parent_id": "#V#example_research_lab",
        "resolved_parent_id": "#V#example_research_lab",
        "resolved_parent_kind": "individual",
        "requested_child_kinds": ["individual"],
    }
    assert created == []


def test_create_concepts_derives_parent_kind_beyond_sanitised_relationships(
    monkeypatch,
):
    created: list[dict[str, Any]] = []
    reads: list[tuple[bool, dict[str, int] | None]] = []
    hidden_type_id = "#V#hidden_research_organisation"

    def _find_one(query, projection=None):
        bypassed = is_bypass_enabled()
        reads.append((bypassed, projection))
        if query.get("concept_id") != "#V#example_research_lab":
            return None
        if not bypassed:
            # The parent itself is visible, but normal relationship sanitisation
            # removes the actor-invisible type target.
            return {
                "concept_id": "#V#example_research_lab",
                "relationships": {
                    "is_a_type_of": [],
                    "is_an_instance_of": [],
                },
            }
        return {
            "concept_id": "#V#example_research_lab",
            "relationships": {
                "is_a_type_of": [],
                "is_an_instance_of": [hidden_type_id],
            },
        }

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _find_one,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: created.append(dict(kwargs)) or {"success": True},
    )

    result = _create_concepts(
        parent_id="#V#example_research_lab",
        concepts=[{"name": "Synthetic scenario", "kind": "instance"}],
    )

    assert result.get("error_code") == "parent_is_not_a_type"
    assert (result.get("error_details") or {}).get("resolved_parent_kind") == (
        "individual"
    )
    assert created == []
    assert hidden_type_id not in json.dumps(result)
    assert reads == [
        (False, {"concept_id": 1}),
        (
            True,
            {
                "concept_id": 1,
                "kind": 1,
                "computed_kind": 1,
                "relationships.is_a_type_of": 1,
                "relationships.#V#is_a_type_of": 1,
                "relationships.is_an_instance_of": 1,
                "relationships.#V#is_an_instance_of": 1,
            },
        ),
    ]


def test_create_concepts_rejects_invisible_parent_without_bypassed_discovery(
    monkeypatch,
):
    reads: list[bool] = []
    hidden_type_id = "#V#hidden_parent_type"

    def _find_one(_query, _projection=None):
        bypassed = is_bypass_enabled()
        reads.append(bypassed)
        if bypassed:
            return {
                "concept_id": "#V#invisible_parent",
                "relationships": {"is_an_instance_of": [hidden_type_id]},
            }
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _find_one,
    )

    result = _create_concepts(
        parent_id="#V#invisible_parent",
        concepts=[{"name": "Disallowed child", "kind": "instance"}],
    )

    assert result.get("error_code") == "parent_not_found"
    assert reads == [False]
    assert hidden_type_id not in json.dumps(result)


def test_create_concepts_rejects_relationship_derived_predicate_parent(monkeypatch):
    created: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, _projection=None: (
            {
                "concept_id": "#V#example_relation",
                "relationships": {
                    "is_a_type_of": ["#V#binary_relation"],
                    "is_an_instance_of": ["#V#predicate"],
                },
            }
            if query.get("concept_id") == "#V#example_relation"
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: created.append(dict(kwargs)) or {"success": True},
    )

    result = _create_concepts(
        parent_id="#V#example_relation",
        concepts=[{"name": "Invalid child type", "kind": "type"}],
    )

    assert result.get("error_code") == "parent_is_not_a_type"
    assert (result.get("error_details") or {}).get("resolved_parent_kind") == (
        "predicate"
    )
    assert created == []


def test_sessionless_gateway_create_fails_before_parent_recovery(monkeypatch):
    _patch_create_concept_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service.resolve_available_workflow_type_ids",
        lambda: ("#V#durable_workflow",),
    )

    def _fake_find_one(query, _projection=None):
        return {"concept_id": "#V#durable_workflow"} if query.get("concept_id") == "#V#durable_workflow" else None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    payload = gateway.invoke(
        "create_concepts",
        {
            "parent_id": "#V#workflow_definition",
            "concepts": [{"name": "Gateway Workflow Recovery Type", "kind": "type"}],
        },
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "authenticated_actor_context_required"
    assert payload.get("effect_status") == "not_started"
    assert payload.get("mutation_outcome") == "not_started"
    assert payload.get("changed") is False


def test_sessionless_stdio_create_fails_before_parent_recovery(monkeypatch):
    _patch_create_concept_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service.resolve_available_workflow_type_ids",
        lambda: ("#V#durable_workflow",),
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, _projection=None: (
            {"concept_id": "#V#durable_workflow"}
            if query.get("concept_id") == "#V#durable_workflow"
            else None
        ),
    )

    async def _invoke() -> Any:
        return await mcp_stdio_server.call_tool(
            "create_concepts",
            {
                "parent_id": "#V#workflow_definition",
                "concepts": [{"name": "Stdio Workflow Recovery Type", "kind": "type"}],
            },
        )

    response = asyncio.run(_invoke())
    assert response
    payload = json.loads(response[0].text)
    assert payload.get("success") is False
    assert payload.get("error_code") == (
        "ontology_sessionless_delegation_not_supported"
    )
    assert payload.get("effect_status") == "not_started"
    assert payload.get("mutation_outcome") == "not_started"
    assert payload.get("changed") is False
