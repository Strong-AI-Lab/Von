"""Gateway-level tests for relationship removal tools and contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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

    def find(self, filter_doc: dict[str, Any], projection: dict[str, Any] | None = None):
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
        for key in projection.keys():
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
                    changed = True or changed
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
                    changed = True or changed
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


def test_remove_relationship_gateway_creates_audit_records(monkeypatch):
    from src.backend.services.relationship_removal_service import (
        build_relationship_relation_id,
    )

    gateway = _build_gateway()
    fake_db = _install_fake_db(monkeypatch)
    _state, _mutate_calls = _install_relationship_state(monkeypatch)

    payload = gateway.invoke(
        "remove_relationship",
        {"source_id": "#V#source", "predicate": "typeOf", "target": "#V#target"},
    ).payload

    assert payload.get("success") is True
    assert payload.get("removed") is True
    assert payload.get("relation_id") == build_relationship_relation_id(
        "#V#source", "is_a_type_of", "#V#target"
    )
    assert isinstance(payload.get("audit_record_id"), str)
    phases = [row.get("phase") for row in fake_db.audit.rows]
    assert "attempt" in phases
    assert "result" in phases
    _assert_schema_conformance(gateway, "remove_relationship", payload)


def test_remove_relationship_gateway_fails_closed_when_audit_insert_fails(monkeypatch):
    gateway = _build_gateway()
    _fake_db = _install_fake_db(monkeypatch, fail_audit_insert=True)
    _state, mutate_calls = _install_relationship_state(monkeypatch)

    payload = gateway.invoke(
        "remove_relationship",
        {"source_id": "#V#source", "predicate": "typeOf", "target": "#V#target"},
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "audit_persistence_failed"
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


def test_bulk_remove_gateway_reports_partial_failures(monkeypatch):
    from src.backend.services.relationship_removal_service import (
        build_relationship_relation_id,
    )

    gateway = _build_gateway()
    _install_fake_db(monkeypatch)
    _state, _mutate_calls = _install_relationship_state(monkeypatch)

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

    assert payload.get("success") is True
    assert payload.get("status") == "partial"
    summary = payload.get("summary", {})
    assert summary.get("removed_count") == 1
    assert summary.get("error_count") >= 1
    assert isinstance(payload.get("undo_token"), str)
    _assert_schema_conformance(gateway, "remove_relationships_bulk", payload)


def test_undo_relationship_removal_gateway_restores_soft_deleted_edges(monkeypatch):
    gateway = _build_gateway()
    _install_fake_db(monkeypatch)
    _state, _mutate_calls = _install_relationship_state(monkeypatch)

    remove_payload = gateway.invoke(
        "remove_relationship",
        {"source_id": "#V#source", "predicate": "typeOf", "target": "#V#target"},
    ).payload
    undo_token = remove_payload.get("undo_token")
    assert isinstance(undo_token, str) and undo_token

    undo_payload = gateway.invoke(
        "undo_relationship_removal",
        {"undo_token": undo_token, "confirmed": True},
    ).payload

    assert undo_payload.get("success") is True
    assert undo_payload.get("restored_count") == 1
    _assert_schema_conformance(gateway, "undo_relationship_removal", undo_payload)
