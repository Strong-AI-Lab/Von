from __future__ import annotations

from typing import Any

from src.backend.services import concept_search_service as svc


def _concept(concept_id: str, name: str | None = None) -> dict[str, Any]:
    return {
        "concept_id": concept_id,
        "name": name or concept_id.rsplit("#", 1)[-1],
        "relationships": {},
    }


def test_modern_text_relation_hits_skip_legacy_regex_scan(monkeypatch) -> None:
    concept_ids = [f"#V#paper_{index}" for index in range(4)]
    concept_docs = [_concept(concept_id) for concept_id in concept_ids]
    concept_find_calls: list[dict[str, Any]] = []

    def fake_text_values_find(query, *args, **kwargs):
        if query == {"text": {"$regex": "^paper$", "$options": "i"}}:
            return [{"_id": "507f1f77bcf86cd799439011", "text": "paper"}]
        return []

    def fake_text_relations_find(query, *args, **kwargs):
        if query.get("object_text_id"):
            return [
                {
                    "subject_concept_id": concept_id,
                    "object_text_id": "507f1f77bcf86cd799439011",
                    "predicate": "hasName",
                }
                for concept_id in concept_ids
            ]
        return []

    def fake_concepts_find(query, *args, **kwargs):
        concept_find_calls.append(query)
        assert "$or" not in query
        assert "concept_id" in query
        return list(concept_docs)

    monkeypatch.setattr(svc.TextValuesRepository, "find", fake_text_values_find)
    monkeypatch.setattr(svc.TextRelationsRepository, "find", fake_text_relations_find)
    monkeypatch.setattr(svc.ConceptsRepository, "find", fake_concepts_find)
    monkeypatch.setattr(svc, "_determine_concept_kind", lambda _doc: "individual")

    result = svc.search_concepts("paper", match_type="substring", limit=2)

    assert result["total_count"] == 4
    assert result["match_types_used"] == ["text_relations"]
    assert len(concept_find_calls) == 1


def test_legacy_regex_fallback_filters_duplicates_without_mongo_nin(monkeypatch) -> None:
    concept_find_calls: list[dict[str, Any]] = []
    duplicate_doc = _concept("#V#duplicate", "duplicate")
    legacy_doc = _concept("#V#legacy", "legacy")

    def fake_concepts_find(query, *args, **kwargs):
        concept_find_calls.append(query)
        if len(concept_find_calls) == 1:
            return [duplicate_doc]
        return [duplicate_doc, legacy_doc]

    monkeypatch.setattr(svc.TextValuesRepository, "find", lambda *args, **kwargs: [])
    monkeypatch.setattr(svc.TextRelationsRepository, "find", lambda *args, **kwargs: [])
    monkeypatch.setattr(svc.ConceptsRepository, "find", fake_concepts_find)
    monkeypatch.setattr(
        svc,
        "_similarity_match",
        lambda *_args, **_kwargs: [("#V#duplicate", 0.95, duplicate_doc)],
    )
    monkeypatch.setattr(svc, "_determine_concept_kind", lambda _doc: "individual")

    result = svc.search_concepts("legacy", match_type="all", limit=2)

    assert {item["concept_id"] for item in result["results"]} == {
        "#V#duplicate",
        "#V#legacy",
    }
    fallback_query = concept_find_calls[1]
    assert "$or" in fallback_query
    assert fallback_query.get("concept_id", {}).get("$nin") is None
