from __future__ import annotations

from src.backend.integrations.internal_mcp import catalogue
from src.backend.services import (
    concept_rename_service,
    concept_resolution_service,
    concept_service,
)


def _patch_fetch_enrichment(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.backend.security.access_control.describe_concept_access",
        lambda concept_id: {
            "concept_id": concept_id,
            "exists": concept_id == "#V#paper",
            "accessible": True,
        },
    )
    monkeypatch.setattr(
        concept_service,
        "enrich_concept_with_text_relations",
        lambda concept: concept,
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.detect_vacuous_typing",
        lambda _concept: None,
    )


def test_fetch_concept_uses_exact_lookup_without_alias_or_fuzzy_resolution(
    monkeypatch,
) -> None:
    _patch_fetch_enrichment(monkeypatch)
    exact_calls: list[str] = []

    def _exact(concept_id: str):
        exact_calls.append(concept_id)
        return {"concept_id": concept_id, "relationships": {}}

    monkeypatch.setattr(concept_service, "get_concept_by_concept_id_exact", _exact)
    monkeypatch.setattr(
        concept_rename_service,
        "resolve_concept_by_alias",
        lambda _concept_id: (_ for _ in ()).throw(
            AssertionError("alias resolution should not run for an exact hit")
        ),
    )
    monkeypatch.setattr(
        concept_resolution_service,
        "resolve_concept_by_name",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("fetch_concept must not perform fuzzy name resolution")
        ),
    )

    result = catalogue._get_concept_by_concept_id(concept_id="#V#paper")

    assert result["concept_id"] == "#V#paper"
    assert exact_calls == ["#V#paper"]


def test_fetch_concept_resolves_one_exact_code_alias(monkeypatch) -> None:
    _patch_fetch_enrichment(monkeypatch)
    exact_calls: list[str] = []

    def _exact(concept_id: str):
        exact_calls.append(concept_id)
        if concept_id == "#V#paper":
            return {"concept_id": concept_id, "relationships": {}}
        raise concept_service.ConceptNotFoundError(concept_id)

    monkeypatch.setattr(concept_service, "get_concept_by_concept_id_exact", _exact)
    monkeypatch.setattr(
        "src.backend.vontology.code_concepts_registry.build_virtual_concept_doc",
        lambda _concept_id: None,
    )
    monkeypatch.setattr(
        concept_rename_service,
        "resolve_concept_by_alias",
        lambda concept_id: "#V#paper" if concept_id == "#V#old_paper" else None,
    )
    monkeypatch.setattr(
        concept_resolution_service,
        "resolve_concept_by_name",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("fetch_concept must not perform fuzzy name resolution")
        ),
    )

    result = catalogue._get_concept_by_concept_id(concept_id="#V#old_paper")

    assert result["concept_id"] == "#V#paper"
    assert exact_calls == ["#V#old_paper", "#V#paper"]


def test_fetch_concept_returns_typed_not_found_without_fuzzy_scan(monkeypatch) -> None:
    _patch_fetch_enrichment(monkeypatch)

    def _not_found(concept_id: str):
        raise concept_service.ConceptNotFoundError(concept_id)

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id_exact",
        _not_found,
    )
    monkeypatch.setattr(
        "src.backend.vontology.code_concepts_registry.build_virtual_concept_doc",
        lambda _concept_id: None,
    )
    monkeypatch.setattr(
        concept_rename_service,
        "resolve_concept_by_alias",
        lambda _concept_id: None,
    )
    monkeypatch.setattr(
        concept_resolution_service,
        "resolve_concept_by_name",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("fetch_concept must not perform fuzzy name resolution")
        ),
    )

    result = catalogue._get_concept_by_concept_id(concept_id="#V#missing")

    assert result["success"] is False
    assert result["error_code"] == "concept_not_found"
    assert result["error_details"] == {
        "concept_id": "#V#missing",
        "status": "not_found",
        "lookup_modes": ["exact_concept_id", "exact_code_alias"],
    }


def test_fetch_concept_preserves_exact_virtual_code_concepts(monkeypatch) -> None:
    _patch_fetch_enrichment(monkeypatch)

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id_exact",
        lambda concept_id: (_ for _ in ()).throw(
            concept_service.ConceptNotFoundError(concept_id)
        ),
    )
    monkeypatch.setattr(
        "src.backend.vontology.code_concepts_registry.build_virtual_concept_doc",
        lambda concept_id: {
            "concept_id": concept_id,
            "relationships": {},
            "metadata": {"concept_type": "predicate", "virtual": True},
        },
    )
    monkeypatch.setattr(
        concept_rename_service,
        "resolve_concept_by_alias",
        lambda _concept_id: (_ for _ in ()).throw(
            AssertionError("an exact virtual concept must precede alias resolution")
        ),
    )

    result = catalogue._get_concept_by_concept_id(concept_id="#V#has_student")

    assert result["concept_id"] == "#V#has_student"
    assert result["id"] == "virtual:#V#has_student"
    assert result["kind"] == "predicate"
