import pytest


def test_remove_relationship_rejects_non_vontology_predicate_keys(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        return None

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        raise AssertionError("update_one should not be called for invalid predicates")

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        _fake_update_one,
    )

    result = catalogue._remove_relationship(
        source_id="#V#source", predicate="has_item", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "invalid_relationship_predicate"


def test_remove_relationship_rejects_missing_dynamic_predicate_concepts(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        # Predicate concept does not exist
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = catalogue._remove_relationship(
        source_id="#V#source", predicate="#V#has_item", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "predicate_concept_not_found"
    assert "#V#has_item" in (result.get("error") or "")


def test_remove_relationship_returns_structured_error_for_missing_source(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        # Source is missing
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = catalogue._remove_relationship(
        source_id="#V#source", predicate="typeOf", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "source_concept_not_found"
    assert result.get("error_details", {}).get("concept_id") == "#V#source"


def test_remove_relationship_rejects_text_relation_predicates(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        if filter_doc.get("concept_id") == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = catalogue._remove_relationship(
        source_id="#V#source", predicate="hasContent", target="hello"
    )

    assert result["success"] is False
    assert result.get("error_code") == "unsupported_text_relation_removal"
