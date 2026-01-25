"""Tests for vacuous typing warning (JVNAUTOSCI-1010).

When adding is_a_type_of #V#thing, the operation succeeds but includes a warning
recommending more specific supertypes.
"""

import types


def test_add_structural_is_a_type_of_thing_returns_warning(monkeypatch):
    """Adding is_a_type_of #V#thing succeeds but includes a warning."""
    from src.backend.services import relationship_write_service as rws

    calls = {"find_one": [], "update_one": [], "ensure": []}

    def _fake_find_one(filter_doc, projection=None):
        calls["find_one"].append({"filter": filter_doc, "projection": projection})
        cid = filter_doc.get("concept_id")
        if cid == "#V#my_concept":
            return {"concept_id": "#V#my_concept", "relationships": {}}
        if cid == "#V#thing":
            return {"concept_id": "#V#thing", "relationships": {}}
        return None

    def _fake_ensure_relationship_array(concept_id, rel_kind):
        calls["ensure"].append({"concept_id": concept_id, "rel_kind": rel_kind})
        return False

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        calls["update_one"].append(
            {"filter": filter_doc, "update": update_doc, "upsert": upsert}
        )
        return types.SimpleNamespace(modified_count=1, matched_count=1)

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository._ensure_relationship_array",
        _fake_ensure_relationship_array,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        _fake_update_one,
    )

    result = rws.add_structural_relationship(
        source_id="#V#my_concept",
        predicate="is_a_type_of",
        target_id="#V#thing",
    )

    # Operation succeeds
    assert result["success"] is True
    assert result["predicate"] == "is_a_type_of"
    assert result["target_id"] == "#V#thing"

    # Warning is present
    assert "warning" in result
    assert result["warning"]["code"] == "vacuous_supertype"
    assert "#V#thing" in result["warning"]["message"]
    assert "suggested_alternatives" in result["warning"]
    assert len(result["warning"]["suggested_alternatives"]) > 0


def test_add_structural_is_a_type_of_specific_type_no_warning(monkeypatch):
    """Adding is_a_type_of to a specific type does not produce a warning."""
    from src.backend.services import relationship_write_service as rws

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#my_concept":
            return {"concept_id": "#V#my_concept", "relationships": {}}
        if cid == "#V#physical_object":
            return {"concept_id": "#V#physical_object", "relationships": {}}
        return None

    def _fake_ensure_relationship_array(concept_id, rel_kind):
        return False

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        return types.SimpleNamespace(modified_count=1, matched_count=1)

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository._ensure_relationship_array",
        _fake_ensure_relationship_array,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        _fake_update_one,
    )

    result = rws.add_structural_relationship(
        source_id="#V#my_concept",
        predicate="is_a_type_of",
        target_id="#V#physical_object",
    )

    # Operation succeeds
    assert result["success"] is True
    assert result["predicate"] == "is_a_type_of"

    # No warning for specific supertypes
    assert "warning" not in result


def test_add_structural_instance_of_thing_no_warning(monkeypatch):
    """Adding is_an_instance_of #V#thing does not produce a vacuous warning.
    
    Vacuous typing warning only applies to is_a_type_of, not is_an_instance_of.
    """
    from src.backend.services import relationship_write_service as rws

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#my_instance":
            return {"concept_id": "#V#my_instance", "relationships": {}}
        if cid == "#V#thing":
            return {"concept_id": "#V#thing", "relationships": {}}
        return None

    def _fake_ensure_relationship_array(concept_id, rel_kind):
        return False

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        return types.SimpleNamespace(modified_count=1, matched_count=1)

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository._ensure_relationship_array",
        _fake_ensure_relationship_array,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        _fake_update_one,
    )

    result = rws.add_structural_relationship(
        source_id="#V#my_instance",
        predicate="is_an_instance_of",
        target_id="#V#thing",
    )

    # Operation succeeds
    assert result["success"] is True
    assert result["predicate"] == "is_an_instance_of"

    # No warning for instance_of (only type_of generates vacuous warning)
    assert "warning" not in result


def test_mcp_add_relationship_propagates_warning(monkeypatch):
    """MCP add_relationship tool propagates the warning from the service."""
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            if projection is None:
                return {"concept_id": "#V#source", "relationships": {}}
            return {"concept_id": "#V#source", "relationships": {"is_a_type_of": []}}
        if cid == "#V#thing":
            if projection is None:
                return {"concept_id": "#V#thing", "relationships": {}}
            return {"concept_id": "#V#thing", "relationships": {"has_subtype": []}}
        return None

    def _fake_ensure_relationship_array(concept_id, rel_kind):
        return False

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        return types.SimpleNamespace(modified_count=1, matched_count=1)

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository._ensure_relationship_array",
        _fake_ensure_relationship_array,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        _fake_update_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="typeOf", target="#V#thing"
    )

    # Operation succeeds
    assert result["success"] is True

    # Warning should be propagated from the service layer
    assert "warning" in result
    assert result["warning"]["code"] == "vacuous_supertype"
