from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.backend.db.repositories import concepts_repository as repository_module
from src.backend.db.repositories.concepts_repository import ConceptsRepository


class _WriteResult:
    modified_count = 1
    matched_count = 1


class _FakeConceptsCollection:
    def __init__(self) -> None:
        self.inserted: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def insert_one(self, document: dict[str, Any]) -> _WriteResult:
        self.inserted.append(document)
        return _WriteResult()

    def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> _WriteResult:
        self.updated.append(("one", query, update))
        return _WriteResult()

    def update_many(
        self, query: dict[str, Any], update: dict[str, Any]
    ) -> _WriteResult:
        self.updated.append(("many", query, update))
        return _WriteResult()

    def find_one_and_update(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        *,
        return_document: bool = False,
    ) -> dict[str, Any]:
        self.updated.append(("find_one", query, update))
        return {"concept_id": "#V#example"}


@pytest.fixture
def fake_collection(monkeypatch: pytest.MonkeyPatch) -> _FakeConceptsCollection:
    collection = _FakeConceptsCollection()
    monkeypatch.setattr(ConceptsRepository, "collection", lambda: collection)
    monkeypatch.setattr(
        repository_module,
        "apply_concept_query_filter",
        lambda query: query,
    )
    monkeypatch.setattr(
        repository_module,
        "sanitize_concept_document",
        lambda document: document,
    )
    return collection


def test_insert_materialises_pending_embedding_status(
    fake_collection: _FakeConceptsCollection,
) -> None:
    source = {"concept_id": "#V#new_concept", "relationships": {}}

    ConceptsRepository.insert_one(source)

    assert source == {"concept_id": "#V#new_concept", "relationships": {}}
    assert fake_collection.inserted == [
        {
            "concept_id": "#V#new_concept",
            "relationships": {},
            "embedding_status": "pending",
        }
    ]


def test_insert_preserves_explicit_embedding_status(
    fake_collection: _FakeConceptsCollection,
) -> None:
    ConceptsRepository.insert_one(
        {"concept_id": "#V#imported", "embedding_status": "indexed"}
    )

    assert fake_collection.inserted[0]["embedding_status"] == "indexed"


@pytest.mark.parametrize("method_name", ["update_one", "update_many"])
def test_timestamped_updates_materialise_stale_status(
    fake_collection: _FakeConceptsCollection,
    method_name: str,
) -> None:
    update = {"$set": {"updated_at": datetime(2026, 8, 20, tzinfo=UTC)}}

    getattr(ConceptsRepository, method_name)({"concept_id": "#V#example"}, update)

    assert fake_collection.updated[-1][2]["$set"]["embedding_status"] == "stale"


def test_relationship_mutation_materialises_stale_status(
    fake_collection: _FakeConceptsCollection,
) -> None:
    ConceptsRepository.update_one(
        {"concept_id": "#V#example"},
        {"$addToSet": {"relationships.is_a_type_of": "#V#thing"}},
    )

    assert fake_collection.updated[-1][2] == {
        "$addToSet": {"relationships.is_a_type_of": "#V#thing"},
        "$set": {"embedding_status": "stale"},
    }


def test_operational_update_without_embedding_input_stays_out_of_queue(
    fake_collection: _FakeConceptsCollection,
) -> None:
    ConceptsRepository.update_one(
        {"concept_id": "#V#message"},
        {"$addToSet": {"concept_data.read_by": "#V#reader"}},
    )

    assert fake_collection.updated[-1][2] == {
        "$addToSet": {"concept_data.read_by": "#V#reader"}
    }


def test_worker_embedding_status_write_is_not_overridden(
    fake_collection: _FakeConceptsCollection,
) -> None:
    indexed_at = datetime(2026, 8, 20, tzinfo=UTC)

    ConceptsRepository.update_one(
        {"concept_id": "#V#example"},
        {
            "$set": {
                "embedding_status": "indexed",
                "embedding_updated_at": indexed_at,
            },
            "$unset": {"embedding_error": ""},
        },
    )

    assert fake_collection.updated[-1][2]["$set"] == {
        "embedding_status": "indexed",
        "embedding_updated_at": indexed_at,
    }


def test_find_one_and_update_materialises_stale_status(
    fake_collection: _FakeConceptsCollection,
) -> None:
    ConceptsRepository.find_one_and_update(
        {"concept_id": "#V#example"},
        {"$unset": {"names": ""}},
        return_document=True,
    )

    assert fake_collection.updated[-1][2] == {
        "$unset": {"names": ""},
        "$set": {"embedding_status": "stale"},
    }
