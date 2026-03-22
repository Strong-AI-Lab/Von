from __future__ import annotations

import src.backend.services.concept_service as concept_service


def test_import_concepts_rejects_title_only_payload(monkeypatch):
    monkeypatch.setattr(concept_service.ConceptsRepository, "collection", lambda: object())
    monkeypatch.setattr(concept_service, "get_db", lambda: None)

    payload = {
        "concept_id": "#V#person",
        "metadata": {"title": "Legacy title only"},
    }

    result = concept_service.import_concepts(
        payload,
        validate_concepts=False,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["created"] == 0
    assert result["skipped"] == 1
    assert result["errors"] == ["Item 0: missing required 'name'"]


def test_import_concepts_accepts_names_array_without_top_level_name(monkeypatch):
    inserted_docs: list[dict] = []

    monkeypatch.setattr(concept_service.ConceptsRepository, "collection", lambda: object())
    monkeypatch.setattr(concept_service.ConceptsRepository, "find_one", lambda *_a, **_k: None)
    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "insert_one",
        lambda doc: inserted_docs.append(doc),
    )
    monkeypatch.setattr(concept_service, "get_db", lambda: None)

    payload = {
        "concept_id": "#V#person",
        "names": [{"name": "Alice Example", "language": "en-NZ", "type": "NL"}],
    }

    result = concept_service.import_concepts(
        payload,
        validate_concepts=False,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["created"] == 1
    assert result["skipped"] == 0
    assert inserted_docs[0]["names"] == payload["names"]
