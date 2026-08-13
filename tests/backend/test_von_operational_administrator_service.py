"""Reciprocal authority contracts for the represented operational role."""

from __future__ import annotations

from contextlib import nullcontext

import pytest
from bson import ObjectId

from src.backend.services import von_operational_administrator_service as service


def _stores(monkeypatch):
    relations: list[dict] = []
    texts: dict[str, dict] = {}

    def find_relations(query):
        return [
            dict(row)
            for row in relations
            if all(row.get(key) == value for key, value in query.items())
        ]

    monkeypatch.setattr(service.TextRelationsRepository, "find", find_relations)
    monkeypatch.setattr(
        service.TextValuesRepository,
        "find_one",
        lambda query: texts.get(query.get("_id")),
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(service, "current_ontology_invocation", lambda: None)
    monkeypatch.setattr(
        service,
        "ontology_mutation_resource_lock",
        lambda _resource_key: nullcontext(),
    )
    monkeypatch.setattr(
        service,
        "ontology_authority_membership_mutation_barrier",
        nullcontext,
    )

    def upsert(**kwargs):
        relation_id = f"operator:{kwargs['subject_concept_id']}"
        text_id = f"text:{relation_id}"
        texts[text_id] = {"text": kwargs["text"]}
        existing = next(
            (item for item in relations if item["_id"] == relation_id), None
        )
        row = {
            "_id": relation_id,
            "subject_concept_id": kwargs["subject_concept_id"],
            "predicate": kwargs["predicate"],
            "object_text_id": text_id,
            "context": kwargs["context"],
        }
        if existing is None:
            relations.append(row)
        else:
            existing.update(row)
        return {"relation_id": relation_id, "relation_created": existing is None}

    def delete(**kwargs):
        for index, row in enumerate(relations):
            if row["_id"] == kwargs["relation_id"]:
                relations.pop(index)
                return {"deleted": True}
        return {"deleted": False}

    monkeypatch.setattr(service, "upsert_text_for_concept", upsert)
    monkeypatch.setattr(service, "delete_text_relation", delete)
    return relations


def test_semantic_role_alone_does_not_create_operational_authority(monkeypatch) -> None:
    _stores(monkeypatch)
    monkeypatch.setattr(service, "get_effective_user_concept_id", lambda: "#V#semantic")

    assert service.is_live_von_operational_administrator("#V#semantic") is False
    with pytest.raises(PermissionError, match="von_operational_administrator_required"):
        service.grant_von_operational_administrator(subject_concept_id="#V#other")


def test_bootstrap_grant_and_revoke_have_exact_read_back(monkeypatch) -> None:
    _stores(monkeypatch)
    monkeypatch.setattr(service, "get_effective_user_concept_id", lambda: "#V#michael")

    with service.bind_von_operational_administrator_bootstrap():
        granted = service.bootstrap_first_von_operational_administrator(
            subject_concept_id="#V#michael"
        )
    assert granted["canonical_read_back"]["active"] is True
    assert service.is_live_von_operational_administrator("#V#michael") is True

    added = service.grant_von_operational_administrator(subject_concept_id="#V#other")
    assert added["canonical_read_back"]["active"] is True
    revoked = service.revoke_von_operational_administrator(
        subject_concept_id="#V#other"
    )
    assert revoked["canonical_read_back"]["active"] is False


def test_role_read_back_resolves_string_text_reference_to_object_id(
    monkeypatch,
) -> None:
    relation_id = ObjectId()
    text_id = ObjectId()
    monkeypatch.setattr(
        service.TextRelationsRepository,
        "find",
        lambda _query: [
            {
                "_id": relation_id,
                "subject_concept_id": "#V#michael",
                "predicate": service.VON_OPERATIONAL_ADMINISTRATOR_PREDICATE,
                "object_text_id": str(text_id),
                "context": {},
            }
        ],
    )
    monkeypatch.setattr(
        service.TextValuesRepository,
        "find_one",
        lambda query: (
            {"_id": text_id, "text": service._ROLE_STORAGE_TEXT}
            if query.get("_id") == text_id
            else None
        ),
    )

    read_back = service.von_operational_administrator_read_back(
        subject_concept_id="#V#michael"
    )

    assert read_back["active"] is True
    assert read_back["grants"][0]["relation_id"] == str(relation_id)


def test_last_operational_administrator_cannot_be_revoked(monkeypatch) -> None:
    _stores(monkeypatch)
    monkeypatch.setattr(service, "get_effective_user_concept_id", lambda: "#V#michael")
    with service.bind_von_operational_administrator_bootstrap():
        service.bootstrap_first_von_operational_administrator(
            subject_concept_id="#V#michael"
        )

    with pytest.raises(
        PermissionError, match="last_von_operational_administrator_cannot_be_revoked"
    ):
        service.revoke_von_operational_administrator(subject_concept_id="#V#michael")


def test_agent_invocation_cannot_manage_represented_operational_role(
    monkeypatch,
) -> None:
    _stores(monkeypatch)
    monkeypatch.setattr(service, "get_effective_user_concept_id", lambda: "#V#michael")
    monkeypatch.setattr(
        service,
        "current_ontology_invocation",
        lambda: type("Invocation", (), {"executing_agent_concept_id": "#V#agent"})(),
    )

    with pytest.raises(PermissionError, match="human_required"):
        service.grant_von_operational_administrator(subject_concept_id="#V#other")


def test_represented_operator_opens_only_the_operational_control_plane(
    monkeypatch,
) -> None:
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
    )

    monkeypatch.setattr(
        service,
        "is_live_von_operational_administrator",
        lambda actor: actor == "#V#operator",
    )
    with bind_internal_mcp_actor_context_source(
        "preexisting_authenticated_or_workflow_context",
        preexisting_actor_context=("#V#operator", "#V#organisation"),
    ):
        assert catalogue._internal_mcp_global_workflow_admin_authorised() is True
    with bind_internal_mcp_actor_context_source(
        "preexisting_authenticated_or_workflow_context",
        preexisting_actor_context=("#V#semantic_only", "#V#organisation"),
    ):
        assert catalogue._internal_mcp_global_workflow_admin_authorised() is False
