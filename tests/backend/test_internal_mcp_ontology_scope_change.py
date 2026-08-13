"""Exact Internal-MCP delegation for ontology publication-scope changes."""

from __future__ import annotations

from typing import Any

import mongomock
import pytest

from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
)
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


@pytest.fixture(scope="module")
def scope_method_definitions():
    from src.backend.integrations.internal_mcp.catalogue import (
        build_default_catalogue,
    )

    catalogue = build_default_catalogue()
    names = (
        "get_concept_publication_scope",
        "preview_concept_publication_scope_change",
        "change_concept_publication_scope",
    )
    return {name: catalogue.get(name) for name in names}


@pytest.fixture
def gateway(scope_method_definitions) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    for definition in scope_method_definitions.values():
        catalogue.register(definition)
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )


@pytest.fixture
def authority_stores(monkeypatch):
    from src.backend.services import ontology_publication_authority_service as service

    database = mongomock.MongoClient()["internal_mcp_scope_authority"]
    delegations = database["delegations"]
    receipts = database["receipts"]
    delegations.create_index("delegation_id", unique=True)
    receipts.create_index("receipt_id", unique=True)
    receipts.create_index(
        [("actor_concept_id", 1), ("idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"idempotency_key": {"$exists": True}},
    )
    monkeypatch.setattr(
        service,
        "get_ontology_authority_delegations_collection",
        lambda: delegations,
    )
    monkeypatch.setattr(
        service,
        "get_ontology_mutation_receipts_collection",
        lambda: receipts,
    )
    monkeypatch.setattr(service, "_gateway_actor_trust_source", lambda: None)
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)

    global_role = service.AuthorityRoleEvidence(
        role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
        actor_concept_id="#V#semantic_admin",
        organisation_concept_id=None,
        relation_id="role:global",
        revision="global-revision",
    )
    organisation_role = service.AuthorityRoleEvidence(
        role=service.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        actor_concept_id="#V#semantic_admin",
        organisation_concept_id="#V#organisation_a",
        relation_id="role:organisation-a",
        revision="organisation-revision",
    )
    monkeypatch.setattr(
        service,
        "resolve_live_semantic_roles",
        lambda actor: (
            (global_role, organisation_role) if actor == "#V#semantic_admin" else ()
        ),
    )
    return service, delegations, receipts


@pytest.fixture
def scope_state(monkeypatch):
    from src.backend.services import ontology_scope_change_service as scope

    state: dict[str, Any] = {
        "concept_id": "#V#research_concept",
        "publication_context": {
            "kind": "global",
            "concept_id": None,
            "source": "concept_visibility",
            "historical_predicates": [],
        },
        "scope_edges": {},
        "scope_fingerprint": "scope-before",
    }
    mutations: list[tuple[Any, ...]] = []

    def read_back(_concept_id: str) -> dict[str, Any]:
        return {
            **state,
            "publication_context": dict(state["publication_context"]),
            "scope_edges": {
                key: list(values) for key, values in state["scope_edges"].items()
            },
        }

    def add_relationship(source_id: str, predicate: str, target: str):
        mutations.append((source_id, predicate, target))
        state.update(
            {
                "publication_context": {
                    "kind": "organisation",
                    "concept_id": "#V#organisation_a",
                    "source": "concept_visibility",
                    "historical_predicates": [],
                },
                "scope_edges": {predicate: [target]},
                "scope_fingerprint": "scope-after",
            }
        )
        return {"success": True, "forward_modified": True}

    monkeypatch.setattr(scope, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(scope, "scope_read_back", read_back)
    monkeypatch.setattr(scope, "add_relationship", add_relationship)
    monkeypatch.setattr(
        scope,
        "remove_relationship",
        lambda **_kwargs: pytest.fail("global source has no scope edge to remove"),
    )
    return scope, state, mutations


def _arguments() -> dict[str, Any]:
    return {
        "concept_id": "#V#research_concept",
        "destination_kind": "organisation",
        "destination_concept_id": "#V#organisation_a",
        "expected_scope_fingerprint": "scope-before",
        "reason": "publish for the research organisation",
    }


def _issue(
    *,
    service,
    method_name: str,
    arguments: dict[str, Any],
    effect_id: str,
) -> dict[str, Any]:
    from src.backend.services.ontology_mutation_command_service import (
        issue_same_turn_method_delegation,
    )

    with service.override_current_actor(
        "#V#semantic_admin",
        "#V#organisation_a",
    ):
        return issue_same_turn_method_delegation(
            method_name=method_name,
            arguments=arguments,
            actor_concept_id="#V#semantic_admin",
            organisation_concept_id="#V#organisation_a",
            delegate_concept_id="#V#von_system",
            audience="adaptive_turn",
            effect_id=effect_id,
            turn_id="turn-scope-change",
        )


def _invoke_delegated(
    *,
    gateway: InternalMCPGateway,
    service,
    method_name: str,
    arguments: dict[str, Any],
    delegation_id: str,
    effect_id: str,
) -> dict[str, Any]:
    with (
        service.override_current_actor(
            "#V#semantic_admin",
            "#V#organisation_a",
        ),
        service.bind_ontology_invocation(
            surface="ordinary_turn",
            executing_agent_concept_id="#V#von_system",
            audience="adaptive_turn",
            delegation_id=delegation_id,
            effect_id=effect_id,
            turn_id="turn-scope-change",
        ),
    ):
        return gateway.invoke(method_name, arguments).payload


def test_scope_methods_are_bounded_ordinary_turn_contracts(
    scope_method_definitions,
) -> None:
    read = scope_method_definitions["get_concept_publication_scope"]
    preview = scope_method_definitions["preview_concept_publication_scope_change"]
    execute = scope_method_definitions["change_concept_publication_scope"]

    assert read.category == "read"
    assert read.ordinary_turn_effect is False
    assert preview.category == "write"
    assert preview.ordinary_turn_effect is True
    assert execute.category == "write"
    assert execute.ordinary_turn_effect is True
    assert execute.write_guardrail == {"ordinary_turn_explicit_request": True}
    for definition in (preview, execute):
        assert "actor_concept_id" not in definition.input_schema.required
        assert "actor_concept_id" not in definition.input_schema.optional
        assert "organisation_concept_id" not in definition.input_schema.required
        assert "organisation_concept_id" not in definition.input_schema.optional


def test_preview_then_execute_uses_exact_same_turn_delegations_and_read_back(
    authority_stores,
    gateway,
    scope_state,
) -> None:
    service, _delegations, _receipts = authority_stores
    _scope, state, mutations = scope_state
    arguments = _arguments()

    preview_effect = "effect-scope-preview"
    preview_grant = _issue(
        service=service,
        method_name="preview_concept_publication_scope_change",
        arguments=arguments,
        effect_id=preview_effect,
    )
    preview = _invoke_delegated(
        gateway=gateway,
        service=service,
        method_name="preview_concept_publication_scope_change",
        arguments={
            **arguments,
            "actor_concept_id": "#V#forged_actor",
            "organisation_concept_id": "#V#forged_organisation",
            "global_admin": True,
        },
        delegation_id=preview_grant["delegation_id"],
        effect_id=preview_effect,
    )
    assert preview["success"] is True
    assert preview["preview"] is True
    assert preview["changed"] is False
    assert preview["canonical_read_back"]["scope_fingerprint"] == "scope-before"
    assert mutations == []

    execute_effect = "effect-scope-execute"
    execute_grant = _issue(
        service=service,
        method_name="change_concept_publication_scope",
        arguments=arguments,
        effect_id=execute_effect,
    )
    result = _invoke_delegated(
        gateway=gateway,
        service=service,
        method_name="change_concept_publication_scope",
        arguments={
            **arguments,
            "actor_concept_id": "#V#forged_actor",
            "organisation_concept_id": "#V#forged_organisation",
            "is_operator": True,
        },
        delegation_id=execute_grant["delegation_id"],
        effect_id=execute_effect,
    )

    assert result["success"] is True
    assert result["effect_status"] == "succeeded"
    assert result["authority_decision"]["actor_concept_id"] == "#V#semantic_admin"
    assert (
        result["authority_decision"]["delegation_id"] == execute_grant["delegation_id"]
    )
    assert result["canonical_read_back"]["scope_fingerprint"] == "scope-after"
    assert result["canonical_read_back"]["publication_context"] == {
        "kind": "organisation",
        "concept_id": "#V#organisation_a",
        "source": "concept_visibility",
        "historical_predicates": [],
    }
    assert state["scope_fingerprint"] == "scope-after"
    assert len(mutations) == 1


@pytest.mark.parametrize("mode", ("missing", "wrong_effect"))
def test_scope_execute_without_the_exact_delegation_cannot_mutate(
    authority_stores,
    gateway,
    scope_state,
    mode: str,
) -> None:
    service, _delegations, _receipts = authority_stores
    _scope, _state, mutations = scope_state
    arguments = _arguments()

    if mode == "missing":
        with service.override_current_actor(
            "#V#semantic_admin",
            "#V#organisation_a",
        ):
            result = gateway.invoke(
                "change_concept_publication_scope",
                arguments,
            ).payload
        expected_code = "ontology_agent_delegation_required"
    else:
        grant = _issue(
            service=service,
            method_name="change_concept_publication_scope",
            arguments=arguments,
            effect_id="effect-correct",
        )
        result = _invoke_delegated(
            gateway=gateway,
            service=service,
            method_name="change_concept_publication_scope",
            arguments=arguments,
            delegation_id=grant["delegation_id"],
            effect_id="effect-wrong",
        )
        expected_code = "ontology_delegation_effect_id_mismatch"

    assert result["success"] is False
    assert result["effect_status"] == "not_started"
    assert result["error_code"] == expected_code
    assert mutations == []


def test_scope_execute_rejects_a_stale_fingerprint_before_mutation(
    authority_stores,
    gateway,
    scope_state,
) -> None:
    service, _delegations, _receipts = authority_stores
    _scope, state, mutations = scope_state
    arguments = _arguments()
    grant = _issue(
        service=service,
        method_name="change_concept_publication_scope",
        arguments=arguments,
        effect_id="effect-stale",
    )
    state["scope_fingerprint"] = "scope-concurrently-changed"

    result = _invoke_delegated(
        gateway=gateway,
        service=service,
        method_name="change_concept_publication_scope",
        arguments=arguments,
        delegation_id=grant["delegation_id"],
        effect_id="effect-stale",
    )

    assert result["success"] is False
    assert result["effect_status"] == "not_started"
    assert result["error_code"] == "scope_precondition_failed"
    assert result["canonical_read_back"]["scope_fingerprint"] == (
        "scope-concurrently-changed"
    )
    assert mutations == []


def test_scope_read_ignores_payload_actor_and_organisation(
    gateway,
    monkeypatch,
) -> None:
    from src.backend.security.access_control import get_effective_user_concept_id
    from src.backend.services import ontology_scope_change_service as scope

    observed_actors: list[str | None] = []

    def accessible(_concept_id: str) -> bool:
        observed_actors.append(get_effective_user_concept_id())
        return True

    monkeypatch.setattr(scope, "can_access_concept", accessible)
    monkeypatch.setattr(
        scope,
        "scope_read_back",
        lambda concept_id: {
            "concept_id": concept_id,
            "publication_context": {"kind": "global", "concept_id": None},
            "scope_edges": {},
            "scope_fingerprint": "scope-public",
        },
    )

    result = gateway.invoke(
        "get_concept_publication_scope",
        {
            "concept_id": "#V#public_concept",
            "actor_concept_id": "#V#forged_actor",
            "organisation_concept_id": "#V#forged_organisation",
        },
    ).payload

    assert result["success"] is True
    assert observed_actors == [None]
