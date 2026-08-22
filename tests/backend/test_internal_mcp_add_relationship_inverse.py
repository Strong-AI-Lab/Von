import types

import mongomock


def test_governed_add_relationship_writes_only_structural_source(monkeypatch):
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
    assert "inverse" not in result

    # The target is a visible referent, not another mutation subject.
    forward_calls = [c for c in calls["ensure"] if c["rel_kind"] == "is_a_type_of"]
    inverse_calls = [c for c in calls["ensure"] if c["rel_kind"] == "has_subtype"]
    assert len(forward_calls) >= 1
    assert inverse_calls == []
    assert [call["filter"]["concept_id"] for call in calls["update_one"]] == [
        "#V#source"
    ]


def test_governed_add_relationship_does_not_repair_target_inverse(monkeypatch):
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
    assert result["added"] is False
    assert "inverse" not in result
    assert [call["filter"]["concept_id"] for call in calls["update_one"]] == [
        "#V#source"
    ]
    assert all(call["concept_id"] == "#V#source" for call in calls["ensure"])


def test_governed_add_relationship_binds_source_only_mode(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    captured = {}
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: {"concept_id": "#V#source"},
    )

    def _add_edge(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "predicate": "is_a_type_of",
            "target_id": "#V#target",
            "forward_modified": True,
        }

    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        _add_edge,
    )

    result = catalogue._add_relationship.__wrapped__(
        source_id="#V#source",
        predicate="typeOf",
        target="#V#target",
    )

    assert result["success"] is True
    assert result["effect_status"] == "succeeded"
    assert result["changed"] is True
    assert captured["maintain_inverse"] is False


def test_source_only_structural_write_remains_available_as_incoming_extent(
    monkeypatch,
):
    from src.backend.services import relationship_extent_index_service as extent
    from src.backend.services import relationship_write_service as writes

    class Repo:
        def __init__(self):
            self.docs = {
                "#V#candidature": {
                    "concept_id": "#V#candidature",
                    "relationships": {},
                },
                # No visibility-owner edge means this target is global. The write
                # service must use it as a reference without changing this document.
                "#V#person": {"concept_id": "#V#person", "relationships": {}},
            }

        def find_one(self, query, projection=None):
            del projection
            return self.docs.get(query.get("concept_id"))

        def _ensure_relationship_array(self, concept_id, predicate):
            relationships = self.docs[concept_id].setdefault("relationships", {})
            relationships.setdefault(predicate, [])
            return False

        def update_one(self, query, update, upsert=False):
            del upsert
            concept_id = query["concept_id"]
            field, target = next(iter(update["$addToSet"].items()))
            predicate = field.removeprefix("relationships.")
            values = self.docs[concept_id]["relationships"].setdefault(predicate, [])
            modified = target not in values
            if modified:
                values.append(target)
            return types.SimpleNamespace(
                matched_count=1,
                modified_count=int(modified),
            )

    repo = Repo()
    index = mongomock.MongoClient().db.relationship_extent_index
    synced_source_ids = []

    def _sync_sources(source_ids):
        synced_source_ids.extend(source_ids)
        for source_id in source_ids:
            extent.sync_relationship_extent_index_for_concept_doc(
                repo.docs[source_id],
                collection=index,
            )

    monkeypatch.setattr(writes, "normalise_structural_predicate", lambda value: value)
    monkeypatch.setattr(
        writes,
        "get_relationship_kinds_set",
        lambda: {"is_an_instance_of"},
    )
    monkeypatch.setattr(
        writes,
        "get_structural_inverse_map",
        lambda: {"is_an_instance_of": "has_instance"},
    )
    monkeypatch.setattr(
        writes, "_sync_relationship_extent_index_for_sources", _sync_sources
    )
    monkeypatch.setattr(
        writes,
        "_invalidate_workflow_routing_projection_for_relationship_change",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        writes,
        "_invalidate_vontology_projection_for_relationship_change",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        writes, "_emit_relationship_mutation_event", lambda **_kwargs: None
    )

    result = writes.add_relationship(
        "#V#candidature",
        "is_an_instance_of",
        "#V#person",
        repo=repo,
        maintain_inverse=False,
    )

    assert result["success"] is True
    assert repo.docs["#V#candidature"]["relationships"] == {
        "is_an_instance_of": ["#V#person"]
    }
    assert repo.docs["#V#person"]["relationships"] == {}
    assert synced_source_ids == ["#V#candidature"]

    monkeypatch.setattr(extent, "relationship_extent_index_ready", lambda: True)
    monkeypatch.setattr(
        extent,
        "get_relationship_extent_index_collection",
        lambda: index,
    )
    monkeypatch.setattr(
        extent,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        extent,
        "get_relationship_kinds_set",
        lambda: {"is_an_instance_of"},
    )

    rows, used_index, diagnostics = extent.incoming_dynamic_extent_rows_page_for_target(
        "#V#person",
        requested_predicate="is_an_instance_of",
        exclude_structural_predicates=False,
        visible_limit=10,
        batch_size=10,
        max_index_rows_scanned=10,
    )

    assert used_index is True
    assert diagnostics["rows_filtered_by_access"] == 0
    assert [(row["arg1_value"], row["predicate_id"]) for row in rows] == [
        ("#V#candidature", "is_an_instance_of")
    ]
