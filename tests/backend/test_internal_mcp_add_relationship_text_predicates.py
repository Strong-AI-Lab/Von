import pytest


def test_add_relationship_treats_prefixed_hascontent_as_text_relation(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    calls = {"find_one": [], "upsert": []}

    def _fake_find_one(filter_doc, projection=None):
        calls["find_one"].append({"filter": filter_doc, "projection": projection})
        if filter_doc.get("concept_id") == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        return None

    def _fake_upsert_text_for_concept(
        *, subject_concept_id, predicate, text, lang=None, provenance=None
    ):
        calls["upsert"].append(
            {
                "subject_concept_id": subject_concept_id,
                "predicate": predicate,
                "text": text,
                "lang": lang,
                "provenance": provenance,
            }
        )
        return {"text_value_id": "tv1", "relation_id": "rel1"}

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_text_for_concept",
        _fake_upsert_text_for_concept,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="#V#hasContent", target="Hello"
    )

    assert result["success"] is True
    assert result["relationship_type"] == "text_relation"
    assert result["predicate"] == "hasContent"
    assert result["predicate_input"] == "#V#hasContent"

    assert len(calls["upsert"]) == 1
    assert calls["upsert"][0]["predicate"] == "hasContent"
    assert calls["upsert"][0]["text"] == "Hello"


def test_add_relationship_treats_plain_hasdescription_as_text_relation(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        if filter_doc.get("concept_id") == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        return None

    def _fake_upsert_text_for_concept(
        *, subject_concept_id, predicate, text, lang=None, provenance=None
    ):
        return {"text_value_id": "tv2", "relation_id": "rel2"}

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_text_for_concept",
        _fake_upsert_text_for_concept,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="hasDescription", target="Desc"
    )

    assert result["success"] is True
    assert result["relationship_type"] == "text_relation"
    assert result["predicate"] == "hasDescription"
    assert result["predicate_input"] == "hasDescription"
