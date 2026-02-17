from __future__ import annotations

from typing import Any, Dict, List

from src.backend.services import preserved_fields_decommission_service as service


def test_inventory_preserved_fields_counts_keys_and_non_empty_text(monkeypatch):
    docs = [
        {
            "concept_id": "#V#one",
            "concept_data": {
                "preserved_fields": {
                    "description": "Desc one",
                    "notes": "",
                }
            },
        },
        {
            "concept_id": "#V#two",
            "concept_data": {
                "preserved_fields": {
                    "description": "Desc two",
                    "custom_key": "Custom",
                }
            },
        },
    ]

    monkeypatch.setattr(
        service,
        "_iter_preserved_field_docs",
        lambda **_kwargs: iter(docs),
    )

    report = service.inventory_preserved_fields()

    assert report["success"] is True
    assert report["concepts_scanned"] == 2
    assert report["keys_total"]["description"] == 2
    assert report["keys_total"]["notes"] == 1
    assert report["keys_total"]["custom_key"] == 1
    assert report["keys_with_non_empty_text"]["description"] == 2
    assert "notes" not in report["keys_with_non_empty_text"]


def test_migrate_preserved_fields_dry_run_reports_without_writing(monkeypatch):
    docs = [
        {
            "concept_id": "#V#example",
            "concept_data": {
                "preserved_fields": {
                    "description": "Example description",
                    "content": "Example content",
                    "custom_key": "unmapped",
                }
            },
        }
    ]

    monkeypatch.setattr(
        service,
        "_iter_preserved_field_docs",
        lambda **_kwargs: iter(docs),
    )
    monkeypatch.setattr(
        service,
        "upsert_text_for_concept",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("dry-run should not write")),
    )

    report = service.migrate_preserved_fields_to_text_relations(dry_run=True)

    assert report["success"] is True
    assert report["stats"]["eligible_text_entries"] == 2
    assert report["stats"]["migrated_entries"] == 2
    assert report["stats"]["skipped_unmapped_keys"] == 1
    assert any(item["status"] == "would_migrate" for item in report["details"])


def test_migrate_preserved_fields_apply_upserts_and_purges(monkeypatch):
    docs = [
        {
            "concept_id": "#V#example",
            "concept_data": {
                "preserved_fields": {
                    "description": "Example description",
                    "notes": "Example notes",
                }
            },
        }
    ]
    writes: List[Dict[str, Any]] = []
    update_calls: List[Dict[str, Any]] = []

    monkeypatch.setattr(
        service,
        "_iter_preserved_field_docs",
        lambda **_kwargs: iter(docs),
    )
    monkeypatch.setattr(
        service,
        "upsert_text_for_concept",
        lambda **kwargs: writes.append(kwargs) or {"relation_id": "rel"},
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "update_one",
        lambda filter_query, update_query: update_calls.append(
            {"filter": filter_query, "update": update_query}
        ),
    )

    report = service.migrate_preserved_fields_to_text_relations(
        dry_run=False,
        purge_migrated_keys=True,
    )

    assert report["success"] is True
    assert report["stats"]["migrated_entries"] == 2
    assert report["stats"]["purged_keys"] == 2
    assert len(writes) == 2
    predicates = {entry["predicate"] for entry in writes}
    assert predicates == {"hasDescription", "hasNote"}
    assert len(update_calls) == 1
    unset_payload = update_calls[0]["update"]["$unset"]
    assert "concept_data.preserved_fields.description" in unset_payload
    assert "concept_data.preserved_fields.notes" in unset_payload

