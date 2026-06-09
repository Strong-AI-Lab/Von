from __future__ import annotations

from src.backend.services import visibility_predicate_migration_service as svc


def test_build_visibility_migration_plan_merges_and_removes_legacy_fields() -> None:
    concept = {
        "concept_id": "#V#example",
        "relationships": {
            "specific_to_user": ["#V#user_a"],
            "#V#specific_to_user": ["#V#user_b"],
            "specific_to_org": ["#V#org_a"],
            "#V#specific_to_organisation": ["#V#org_c"],
            "#V#specific_to_org": ["#V#org_b"],
            "other": ["#V#kept"],
        },
    }

    plan = svc.build_visibility_migration_plan(concept)

    assert plan is not None
    assert plan.changed is True
    assert plan.updated_relationships["#V#specific_to_user"] == [
        "#V#user_b",
        "#V#user_a",
    ]
    assert plan.updated_relationships["#V#specific_to_organisation"] == [
        "#V#org_c",
        "#V#org_a",
        "#V#org_b",
    ]
    assert "specific_to_user" not in plan.updated_relationships
    assert "specific_to_org" not in plan.updated_relationships
    assert "#V#specific_to_org" not in plan.updated_relationships
    assert plan.updated_relationships["other"] == ["#V#kept"]
    assert {item["family"] for item in plan.conflicts} == {
        "specific_to_user",
        "specific_to_organisation",
    }


def test_build_visibility_migration_plan_is_clean_after_canonicalisation() -> None:
    concept = {
        "concept_id": "#V#example",
        "relationships": {
            "#V#specific_to_user": ["#V#user_a"],
            "#V#specific_to_organisation": ["#V#org_a"],
        },
    }

    plan = svc.build_visibility_migration_plan(concept)

    assert plan is not None
    assert plan.changed is False
    assert plan.conflicts == ()


def test_migrate_visibility_predicate_storage_dry_run_does_not_update(monkeypatch) -> None:
    concepts = [
        {
            "concept_id": "#V#legacy",
            "relationships": {"specific_to_user": ["#V#user_a"]},
        }
    ]
    writes = []

    monkeypatch.setattr(svc.ConceptsRepository, "find", lambda *args, **kwargs: iter(concepts))
    monkeypatch.setattr(
        svc.ConceptsRepository,
        "update_one",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    report = svc.migrate_visibility_predicate_storage(dry_run=True)

    assert report["dry_run"] is True
    assert report["would_update_count"] == 1
    assert report["updated_count"] == 0
    assert writes == []


def test_migrate_visibility_predicate_storage_apply_updates_and_verifies(monkeypatch) -> None:
    concepts = [
        {
            "concept_id": "#V#legacy",
            "relationships": {"specific_to_user": ["#V#user_a"]},
        }
    ]
    stored = {
        "#V#legacy": {
            "concept_id": "#V#legacy",
            "relationships": {"specific_to_user": ["#V#user_a"]},
        }
    }
    writes = []
    find_calls = 0

    def _find(*_args, **_kwargs):
        nonlocal find_calls
        find_calls += 1
        if find_calls == 1:
            return iter(concepts)
        return iter([stored["#V#legacy"]])

    class _BulkResult:
        modified_count = 1

    class _FakeCollection:
        def bulk_write(self, operations, ordered=False):
            writes.extend(operations)
            for operation in operations:
                filter_doc = operation._filter
                update_doc = operation._doc
                relationships = stored[filter_doc["concept_id"]]["relationships"]
                for key, value in update_doc.get("$set", {}).items():
                    if key.startswith("relationships."):
                        relationships[key.removeprefix("relationships.")] = value
                for key in update_doc.get("$unset", {}):
                    if key.startswith("relationships."):
                        relationships.pop(key.removeprefix("relationships."), None)
            return _BulkResult()

    monkeypatch.setattr(svc.ConceptsRepository, "find", _find)
    monkeypatch.setattr(svc.ConceptsRepository, "collection", lambda: _FakeCollection())
    monkeypatch.setattr(
        svc.ConceptsRepository,
        "find_one",
        lambda query, projection=None: stored.get(query.get("concept_id")),
    )

    report = svc.migrate_visibility_predicate_storage(dry_run=False)

    assert report["dry_run"] is False
    assert report["would_update_count"] == 1
    assert report["updated_count"] == 1
    assert report["verified_count"] == 1
    assert report["error_count"] == 0
    update_doc = writes[0]._doc
    assert update_doc["$set"]["relationships.#V#specific_to_user"] == ["#V#user_a"]
    assert update_doc["$unset"] == {
        "relationships.specific_to_user": "",
        "relationships.specific_to_org": "",
        "relationships.specific_to_organisation": "",
        "relationships.#V#specific_to_org": "",
        "relationships.#V#specific_to_organisation": "",
    }
