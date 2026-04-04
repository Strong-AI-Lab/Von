

def test_add_relationship_rejects_non_vontology_predicate_keys(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    calls = {"find_one": [], "update_one": []}

    def _fake_find_one(filter_doc, projection=None):
        calls["find_one"].append({"filter": filter_doc, "projection": projection})
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        if cid == "#V#target":
            return {"concept_id": "#V#target", "relationships": {}}
        return None

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        calls["update_one"].append(
            {"filter": filter_doc, "update": update_doc, "upsert": upsert}
        )
        raise AssertionError("update_one should not be called for invalid predicates")

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        _fake_update_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="has_item", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "invalid_predicate_format"
    assert "invalid_predicate_format" in (result.get("error") or "")


def test_add_relationship_rejects_missing_dynamic_predicate_concepts(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        if cid == "#V#target":
            return {"concept_id": "#V#target", "relationships": {}}
        # Predicate concept does not exist
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="#V#has_item", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "predicate_concept_not_found"


def test_add_relationship_rejects_dynamic_concepts_that_are_not_predicates(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        if cid == "#V#target":
            return {"concept_id": "#V#target", "relationships": {}}
        if cid == "#V#has_todo_item":
            # Exists, but is not typed as a predicate
            return {
                "concept_id": "#V#has_todo_item",
                "relationships": {"is_an_instance_of": []},
            }
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="#V#has_todo_item", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "predicate_concept_not_typed"


def test_add_relationship_returns_structured_error_for_missing_source(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        # Source is missing
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="typeOf", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "source_concept_not_found"
    assert result.get("error_details", {}).get("concept_id") == "#V#source"


def test_add_relationship_returns_structured_error_for_missing_target(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        # Target is missing
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="typeOf", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "target_not_found"
