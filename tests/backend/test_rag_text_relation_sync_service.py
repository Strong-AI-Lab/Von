from __future__ import annotations

from typing import Any, Dict, Optional

import pytest


class _ConceptsCollection:
    def __init__(self, visible_concepts: set[str]):
        self.visible_concepts = set(visible_concepts)

    def find_one(
        self, query: Dict[str, Any], _projection: Optional[Dict[str, Any]] = None
    ):
        # Our service wraps the base filter with apply_concept_query_filter, so concept_id may be
        # nested under $and. Extract it loosely.
        def _extract_concept_id(q: Any) -> Optional[str]:
            if isinstance(q, dict):
                if "concept_id" in q and isinstance(q["concept_id"], str):
                    return q["concept_id"]
                if "$and" in q and isinstance(q["$and"], list):
                    for part in q["$and"]:
                        found = _extract_concept_id(part)
                        if found:
                            return found
                if "$or" in q and isinstance(q["$or"], list):
                    for part in q["$or"]:
                        found = _extract_concept_id(part)
                        if found:
                            return found
            return None

        concept_id = _extract_concept_id(query)
        if concept_id and concept_id in self.visible_concepts:
            return {"_id": "ok"}
        return None


class _StubRAG:
    def __init__(self):
        self.upserts = []

    def upsert_documents(self, docs, *, namespace=None, allow_partial_failures=True):
        self.upserts.extend(docs)
        return (len(docs), 0)


def test_sync_text_relations_indexes_visible_concepts(monkeypatch):
    from src.backend.services import rag_text_relation_sync_service as svc

    # Visible concept set for the namespace.
    monkeypatch.setattr(
        svc, "get_concepts_collection", lambda: _ConceptsCollection({"#V#allowed"})
    )

    # Two relations: one visible, one not.
    monkeypatch.setattr(
        "src.backend.db.repositories.text_value_repository.TextRelationsRepository.find",
        lambda _filter, limit=0: [
            {
                "_id": "r1",
                "subject_concept_id": "#V#allowed",
                "predicate": "hasDescription",
                "object_text_id": "000000000000000000000001",
            },
            {
                "_id": "r2",
                "subject_concept_id": "#V#blocked",
                "predicate": "hasDescription",
                "object_text_id": "000000000000000000000002",
            },
        ],
    )

    def _tv_find_one(filter_doc, _projection=None):
        oid = filter_doc.get("_id")
        if str(oid) == "000000000000000000000001":
            return {"_id": oid, "text": "Hello world", "lang": "en-NZ"}
        if str(oid) == "000000000000000000000002":
            return {"_id": oid, "text": "Should not index", "lang": "en-NZ"}
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.text_value_repository.TextValuesRepository.find_one",
        _tv_find_one,
    )

    rag = _StubRAG()
    monkeypatch.setattr(svc, "get_rag_service", lambda *_args, **_kwargs: rag)

    result = svc.sync_text_relations_to_rag(namespace="#V#user@org", limit=100)
    assert result["success"] is True

    # Only the visible concept relation should be upserted.
    assert len(rag.upserts) == 1
    doc = rag.upserts[0]
    assert doc["id"] == "text_relation:r1"
    assert "Hello world" in doc["text"]

    meta = doc["metadata"]
    assert meta["source"] == "vontology_text_relation"
    assert meta["subject_concept_id"] == "#V#allowed"
    assert meta["predicate"] == "hasDescription"
    assert meta["relation_id"] == "r1"

    # Required for permission filtering.
    assert meta["user_id"] == "#V#user"
    assert meta["organisation_concept_id"] == "#V#org"
