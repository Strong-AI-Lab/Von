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
        svc.concept_service,
        "update_concept",
        lambda **kwargs: writes.append(kwargs),
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

    monkeypatch.setattr(svc.ConceptsRepository, "find", lambda *args, **kwargs: iter(concepts))

    def _update_concept(**kwargs):
        writes.append(kwargs)
        stored[kwargs["concept_id"]]["relationships"] = kwargs["update_data"][
            "relationships"
        ]
        return stored[kwargs["concept_id"]]

    monkeypatch.setattr(svc.concept_service, "update_concept", _update_concept)
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
    assert writes[0]["update_data"]["relationships"] == {
        "#V#specific_to_user": ["#V#user_a"]
    }
