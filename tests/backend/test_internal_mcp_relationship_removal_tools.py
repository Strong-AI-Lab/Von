"""Gateway-level tests for relationship removal tools and contracts."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

import mongomock
import pytest

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
    gateway: InternalMCPGateway, method: str, payload: dict[str, Any]
) -> None:
    definition = gateway.get_method_definition(method)
    assert definition is not None
    assert definition.output_schema is not None
    ok, errors = validate_payload(definition.output_schema, payload)
    assert ok, f"{method} output schema mismatch: {errors}"


@dataclass
class _FakeCollection:
    fail_on_insert: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)

    def create_index(self, *_args, **_kwargs):
        return "idx"

    def insert_one(self, payload: dict[str, Any]):
        if self.fail_on_insert:
            raise RuntimeError("insert_failed")
        self.rows.append(dict(payload))
        return {"inserted_id": len(self.rows)}

    def find(
        self, filter_doc: dict[str, Any], projection: dict[str, Any] | None = None
    ):
        undo_token = filter_doc.get("undo_token")
        matches = []
        for row in self.rows:
            if undo_token is not None and row.get("undo_token") != undo_token:
                continue
            if projection:
                projected = {}
                for key, include in projection.items():
                    if include and key in row:
                        projected[key] = row[key]
                matches.append(projected)
            else:
                matches.append(dict(row))
        return matches


@dataclass
class _FakeDB:
    audit: _FakeCollection
    tombstone: _FakeCollection

    def __getitem__(self, name: str):
        if name == "relationship_removal_audit":
            return self.audit
        if name == "relationship_removal_tombstones":
            return self.tombstone
        raise KeyError(name)


def _install_relationship_state(monkeypatch):
    from src.backend.services import relationship_removal_service as removal_service

    state = {
        "#V#source": {"is_a_type_of": ["#V#target"]},
        "#V#target": {"has_subtype": ["#V#source"]},
    }
    inverse_map = {
        "is_a_type_of": "has_subtype",
        "has_subtype": "is_a_type_of",
        "is_an_instance_of": "has_instance",
        "has_instance": "is_an_instance_of",
        "related_to": "related_to",
    }

    def _fake_find_one(filter_doc, projection=None):
        concept_id = filter_doc.get("concept_id")
        relationships = state.get(concept_id)
        if relationships is None:
            return None
        if not projection:
            return {"concept_id": concept_id, "relationships": dict(relationships)}
        projected_rels: dict[str, Any] = {}
        for key in projection:
            if not isinstance(key, str):
                continue
            if key == "concept_id":
                continue
            if key.startswith("relationships."):
                rel_key = key.split(".", 1)[1]
                if rel_key in relationships:
                    projected_rels[rel_key] = list(relationships[rel_key])
        return {"concept_id": concept_id, "relationships": projected_rels}

    mutate_calls: list[dict[str, Any]] = []

    def _fake_mutate(source_id, kind, target_id, *, action, maintain_inverse=True):
        mutate_calls.append(
            {
                "source_id": source_id,
                "kind": kind,
                "target_id": target_id,
                "action": action,
                "maintain_inverse": maintain_inverse,
            }
        )
        source = state.setdefault(source_id, {})
        source_values = source.setdefault(kind, [])
        changed = False
        if action == "remove":
            if target_id in source_values:
                source_values.remove(target_id)
                changed = True
            if maintain_inverse and kind in inverse_map:
                inv_kind = inverse_map[kind]
                target = state.setdefault(target_id, {})
                inv_values = target.setdefault(inv_kind, [])
                if source_id in inv_values:
                    inv_values.remove(source_id)
                    changed = True
        elif action == "add":
            if target_id not in source_values:
                source_values.append(target_id)
                changed = True
            if maintain_inverse and kind in inverse_map:
                inv_kind = inverse_map[kind]
                target = state.setdefault(target_id, {})
                inv_values = target.setdefault(inv_kind, [])
                if source_id not in inv_values:
                    inv_values.append(source_id)
                    changed = True
        return changed

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.mutate_relationship_edge",
        _fake_mutate,
    )
    monkeypatch.setattr(removal_service, "can_access_concept", lambda _cid: True)
    return state, mutate_calls


def _install_fake_db(monkeypatch, *, fail_audit_insert: bool = False):
    from src.backend.services import relationship_removal_service as removal_service

    fake_db = _FakeDB(
        audit=_FakeCollection(fail_on_insert=fail_audit_insert),
        tombstone=_FakeCollection(),
    )
    monkeypatch.setattr(removal_service, "get_db", lambda: fake_db)
    monkeypatch.setattr(removal_service, "_INDEXES_READY", False)
    return fake_db


def test_relationship_removal_methods_registered() -> None:
    methods = set(build_default_catalogue().list_methods())
    assert "remove_relationship" in methods
    assert "preview_remove_relationship" in methods
    assert "remove_relationships_bulk" in methods
    assert "undo_relationship_removal" in methods


def test_ordinary_turn_can_discover_a_reversible_relationship_correction():
    from src.backend.services.adaptive_turn_service import (
        _model_visible_input_schema,
        _trusted_tool_payload,
        ordinary_turn_capability_delegation,
    )

    gateway = _build_gateway()
    delegated = ordinary_turn_capability_delegation(gateway, user_concept_id="#V#alice")
    assert "remove_relationship" in delegated
    assert "remove_relationships_bulk" not in delegated
    assert "remove_relationship" not in ordinary_turn_capability_delegation(
        gateway, user_concept_id=None
    )
    definition = gateway.get_method_definition("remove_relationship")
    schema = _model_visible_input_schema(definition)
    assert {"source_id", "predicate", "target", "reason"} <= set(schema["properties"])
    assert not {"mode", "cascade", "confirmed", "operator_override"}.intersection(
        schema["properties"]
    )

    # A caller cannot turn this ordinary correction into permanent removal or
    # cascade deletion. The general explicitly delegated editor is unchanged.
    payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="remove_relationship",
        model_payload={
            "source_id": "#V#source",
            "predicate": "#V#authored_by",
            "target": "#V#duplicate_occurrence",
            "reason": "Source author list and occurrence provenance reconciled",
            "mode": "hard_delete",
            "cascade": "delete_dependent",
            "confirmed": True,
            "operator_override": True,
        },
        trusted_argument_values={},
    )
    assert payload["mode"] == "soft_delete"
    assert payload["cascade"] == "warn"
    assert payload["confirmed"] is False
    assert payload["operator_override"] is False
    assert payload["source_id"] == "#V#source"
    assert payload["target"] == "#V#duplicate_occurrence"


@pytest.mark.parametrize("has_semantic_role", [True, False])
def test_same_turn_correction_issues_authority_and_reads_back_exact_effect(
    monkeypatch, has_semantic_role
):
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority
    from src.backend.services.adaptive_turn_service import _trusted_tool_payload

    gateway = _build_gateway()
    audit_db = _install_fake_db(monkeypatch)
    state, mutations = _install_relationship_state(monkeypatch)
    database = mongomock.MongoClient()["ordinary_correction"]
    monkeypatch.setattr(
        authority,
        "get_ontology_authority_delegations_collection",
        lambda: database["delegations"],
    )
    monkeypatch.setattr(
        authority,
        "get_ontology_mutation_receipts_collection",
        lambda: database["receipts"],
    )
    monkeypatch.setattr(
        command, "ontology_mutation_resource_lock", lambda _: nullcontext()
    )
    monkeypatch.setattr(command, "can_access_concept", lambda _: True)
    monkeypatch.setattr(authority, "can_access_concept", lambda _: True)
    monkeypatch.setattr(
        command,
        "concept_publication_context",
        lambda _: authority.PublicationContext.global_context(),
    )
    role = authority.AuthorityRoleEvidence(
        role=authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
        actor_concept_id="#V#alice",
        organisation_concept_id=None,
        relation_id="role-alice-global",
        revision="test-revision",
    )
    monkeypatch.setattr(
        authority,
        "resolve_live_semantic_roles",
        lambda _: (role,) if has_semantic_role else (),
    )
    arguments = _trusted_tool_payload(
        gateway=gateway,
        tool_name="remove_relationship",
        model_payload={
            "source_id": "#V#source",
            "predicate": "is_a_type_of",
            "target": "#V#target",
            "reason": "Correct the source-supported relationship",
        },
        trusted_argument_values={},
    )
    with authority.override_current_actor("#V#alice", None):
        # Exercise production same-turn issuance, not a manually injected grant.
        grant = command.issue_same_turn_method_delegation(
            method_name="remove_relationship",
            arguments=arguments,
            actor_concept_id="#V#alice",
            organisation_concept_id=None,
            delegate_concept_id="#V#von_system",
            audience="adaptive_turn",
            effect_id="correction-effect",
            turn_id="correction-turn",
        )
        if not has_semantic_role:
            assert not grant.get("delegation_id")
            assert grant["effect_status"] == "not_started"
            assert mutations == [] and audit_db.audit.rows == []
            return
        assert grant.get("delegation_id"), json.dumps(grant)
        with authority.bind_ontology_invocation(
            surface="adaptive_turn",
            executing_agent_concept_id="#V#von_system",
            audience="adaptive_turn",
            delegation_id=grant["delegation_id"],
            effect_id="correction-effect",
            turn_id="correction-turn",
        ):
            first = gateway.invoke("remove_relationship", arguments).payload
            repeated = gateway.invoke("remove_relationship", arguments).payload
    assert first["authority_receipt"]["status"] == "succeeded", first
    assert first["canonical_read_back"]["relationship_present"] is False
    assert first["canonical_read_back"]["inverse_relationship_present"] is False
    assert first["undo_token"]
    assert state["#V#source"]["is_a_type_of"] == []
    assert state["#V#target"]["has_subtype"] == []
    assert repeated["authority_receipt"]["status"] == "succeeded"
    assert len(mutations) == 1
    assert len(audit_db.tombstone.rows) == 1


def test_sessionless_remove_relationship_gateway_fails_before_audit(monkeypatch):
    gateway = _build_gateway()
    fake_db = _install_fake_db(monkeypatch)
    _state, mutate_calls = _install_relationship_state(monkeypatch)

    payload = gateway.invoke(
        "remove_relationship",
        {"source_id": "#V#source", "predicate": "typeOf", "target": "#V#target"},
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "ontology_agent_delegation_required"
    assert payload.get("effect_status") == "not_started"
    assert payload.get("mutation_outcome") == "not_started"
    assert payload.get("changed") is False
    assert fake_db.audit.rows == []
    assert mutate_calls == []
    _assert_schema_conformance(gateway, "remove_relationship", payload)


def test_sessionless_remove_relationship_does_not_reach_audit_failure(monkeypatch):
    gateway = _build_gateway()
    fake_db = _install_fake_db(monkeypatch, fail_audit_insert=True)
    _state, mutate_calls = _install_relationship_state(monkeypatch)

    payload = gateway.invoke(
        "remove_relationship",
        {"source_id": "#V#source", "predicate": "typeOf", "target": "#V#target"},
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "ontology_agent_delegation_required"
    assert payload.get("effect_status") == "not_started"
    assert payload.get("mutation_outcome") == "not_started"
    assert payload.get("changed") is False
    assert fake_db.audit.rows == []
    assert mutate_calls == []
    _assert_schema_conformance(gateway, "remove_relationship", payload)


def test_preview_remove_relationship_gateway_has_no_mutation_side_effects(monkeypatch):
    gateway = _build_gateway()
    _install_fake_db(monkeypatch)
    _state, mutate_calls = _install_relationship_state(monkeypatch)

    payload = gateway.invoke(
        "preview_remove_relationship",
        {"source_id": "#V#source", "predicate": "typeOf", "target": "#V#target"},
    ).payload

    assert payload.get("success") is True
    assert payload.get("dry_run") is True
    assert payload.get("status") == "ok"
    assert mutate_calls == []
    _assert_schema_conformance(gateway, "preview_remove_relationship", payload)


def test_bulk_remove_gateway_rejects_invalid_batch_before_any_effect(monkeypatch):
    from src.backend.services.relationship_removal_service import (
        build_relationship_relation_id,
    )

    gateway = _build_gateway()
    fake_db = _install_fake_db(monkeypatch)
    _state, mutate_calls = _install_relationship_state(monkeypatch)

    valid_relation_id = build_relationship_relation_id(
        "#V#source", "is_a_type_of", "#V#target"
    )
    payload = gateway.invoke(
        "remove_relationships_bulk",
        {
            "relation_ids": [valid_relation_id, "bad-format"],
            "confirmed": True,
        },
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "invalid_relationship_relation_id"
    assert payload.get("effect_status") == "not_started"
    assert payload.get("mutation_outcome") == "not_started"
    assert payload.get("changed") is False
    assert payload.get("undo_token") is None
    assert fake_db.audit.rows == []
    assert mutate_calls == []
    _assert_schema_conformance(gateway, "remove_relationships_bulk", payload)


def test_sessionless_remove_cannot_obtain_an_undo_token(monkeypatch):
    gateway = _build_gateway()
    fake_db = _install_fake_db(monkeypatch)
    _state, mutate_calls = _install_relationship_state(monkeypatch)

    remove_payload = gateway.invoke(
        "remove_relationship",
        {"source_id": "#V#source", "predicate": "typeOf", "target": "#V#target"},
    ).payload
    assert remove_payload.get("success") is False
    assert remove_payload.get("error_code") == "ontology_agent_delegation_required"
    assert remove_payload.get("effect_status") == "not_started"
    assert remove_payload.get("mutation_outcome") == "not_started"
    assert remove_payload.get("changed") is False
    assert remove_payload.get("undo_token") is None
    assert fake_db.audit.rows == []
    assert mutate_calls == []
    _assert_schema_conformance(gateway, "remove_relationship", remove_payload)
