import types


def test_add_relationship_adds_inverse_for_structural_predicates(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    calls = {"ensure": [], "find_one": [], "update_one": []}

    def _fake_find_one(filter_doc, projection=None):
        calls["find_one"].append({"filter": filter_doc, "projection": projection})
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            # Existence check
            if projection is None:
                return {"concept_id": "#V#source", "relationships": {}}
            # Relationship-read check
            return {"concept_id": "#V#source", "relationships": {"is_a_type_of": []}}
        if cid == "#V#target":
            if projection is None:
                return {"concept_id": "#V#target", "relationships": {}}
            return {"concept_id": "#V#target", "relationships": {"has_subtype": []}}
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

    # Exercise the relationship handler itself; governed entrypoint authority
    # is covered by the ontology-authority gateway tests.
    result = catalogue._add_relationship.__wrapped__(
        source_id="#V#source", predicate="typeOf", target="#V#target"
    )

    assert result["success"] is True
    assert result["predicate"] == "is_a_type_of"
    assert result.get("inverse", {}).get("predicate") == "has_subtype"
    assert result.get("inverse", {}).get("added") is True

    # Verify ensure_relationship_array was called for both forward and inverse predicates
    forward_calls = [c for c in calls["ensure"] if c["rel_kind"] == "is_a_type_of"]
    inverse_calls = [c for c in calls["ensure"] if c["rel_kind"] == "has_subtype"]
    assert len(forward_calls) >= 1
    assert len(inverse_calls) >= 1


def test_add_relationship_when_forward_exists_still_ensures_inverse(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    calls = {"ensure": [], "update_one": []}

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            if projection is None:
                return {"concept_id": "#V#source", "relationships": {}}
            # Forward already exists
            return {
                "concept_id": "#V#source",
                "relationships": {"is_a_type_of": ["#V#target"]},
            }
        if cid == "#V#target":
            if projection is None:
                return {"concept_id": "#V#target", "relationships": {}}
            return {"concept_id": "#V#target", "relationships": {"has_subtype": []}}
        return None

    def _fake_ensure_relationship_array(concept_id, rel_kind):
        calls["ensure"].append({"concept_id": concept_id, "rel_kind": rel_kind})
        return False

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        calls["update_one"].append(
            {"filter": filter_doc, "update": update_doc, "upsert": upsert}
        )
        # Forward update is idempotent (already had it), inverse update adds it.
        if filter_doc.get("concept_id") == "#V#source":
            return types.SimpleNamespace(modified_count=0, matched_count=1)
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

    result = catalogue._add_relationship.__wrapped__(
        source_id="#V#source", predicate="typeOf", target="#V#target"
    )

    assert result["success"] is True
    # Forward relationship already existed, but inverse may have been added
    assert result.get("inverse", {}).get("added") is True


def test_add_relationship_surfaces_inverse_failure_as_partial(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: {"concept_id": "#V#source"},
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: {
            "success": True,
            "predicate": "is_a_type_of",
            "target_id": "#V#target",
            "forward_modified": True,
            "inverse_predicate": "has_subtype",
            "inverse_error": "inverse update unavailable",
        },
    )

    result = catalogue._add_relationship.__wrapped__(
        source_id="#V#source",
        predicate="typeOf",
        target="#V#target",
    )

    assert result["success"] is True
    assert result["effect_status"] == "partial"
    assert result["changed"] is True
    assert result["partial_failures"] == [
        {
            "stage": "inverse_relationship",
            "error": "inverse update unavailable",
        }
    ]
