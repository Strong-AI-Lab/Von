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
