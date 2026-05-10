from __future__ import annotations

from src.backend.integrations.internal_mcp import (
    InternalMCPGateway,
    InternalMCPTransport,
    build_default_catalogue,
)


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_concept_exists_returns_db_unavailable_when_concepts_collection_missing(
    monkeypatch,
) -> None:
    gateway = _build_gateway()

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.collection",
        staticmethod(lambda: None),
    )

    result = gateway.invoke("concept_exists", {"concept_id": "#V#thing"}).payload

    assert result["success"] is False
    assert result["error_code"] == "db_unavailable"
    assert result["error_details"]["reason"] == "concepts_collection_unavailable"


class _FakeConceptCollection:
    def __init__(self, doc: dict) -> None:
        self.doc = doc

    def find_one(self, query: dict, projection: dict | None = None):
        if query.get("concept_id") == self.doc.get("concept_id"):
            return self.doc
        return None


def test_concept_exists_binds_namespace_actor_for_access_decision(
    monkeypatch,
) -> None:
    gateway = _build_gateway()
    collection = _FakeConceptCollection(
        {
            "concept_id": "#V#team_only_concept",
            "relationships": {"specific_to_org": ["#V#sail"]},
        }
    )

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.collection",
        staticmethod(lambda: collection),
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_concepts_collection",
        lambda: collection,
    )

    allowed = gateway.invoke(
        "concept_exists",
        {"concept_id": "#V#team_only_concept", "namespace": "#V#member@sail"},
    ).payload
    denied = gateway.invoke(
        "concept_exists",
        {"concept_id": "#V#team_only_concept", "namespace": "#V#member@other_org"},
    ).payload

    assert allowed["exists"] is True
    assert allowed["accessible"] is True
    assert allowed["access"]["organisation_concept_id"] == "#V#sail"
    assert denied["exists"] is True
    assert denied["accessible"] is False
    assert denied["access"]["restriction_families_present"] == ["specific_to_org"]


def test_fetch_concept_returns_access_denied_without_leaking_payload(
    monkeypatch,
) -> None:
    gateway = _build_gateway()
    collection = _FakeConceptCollection(
        {
            "concept_id": "#V#private_person",
            "relationships": {"specific_to_user": ["#V#owner"]},
            "direct_concept_name": "Private Person",
        }
    )

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.collection",
        staticmethod(lambda: collection),
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_concepts_collection",
        lambda: collection,
    )

    result = gateway.invoke(
        "fetch_concept",
        {"concept_id": "#V#private_person", "namespace": "#V#member@sail"},
    ).payload

    assert result["success"] is False
    assert result["error_code"] == "access_denied"
    assert result["error_details"]["exists"] is True
    assert result["error_details"]["accessible"] is False
    assert "direct_concept_name" not in result
