from __future__ import annotations

from typing import Any

from src.backend.db.repositories import concepts_repository as repository


class _FakeCursor:
    def __init__(self) -> None:
        self.operations: list[tuple[str, Any]] = []

    def max_time_ms(self, value: int) -> _FakeCursor:
        self.operations.append(("max_time_ms", value))
        return self

    def sort(self, value: list[tuple[str, int]]) -> _FakeCursor:
        self.operations.append(("sort", value))
        return self

    def skip(self, value: int) -> _FakeCursor:
        self.operations.append(("skip", value))
        return self

    def limit(self, value: int) -> _FakeCursor:
        self.operations.append(("limit", value))
        return self


class _FakeCollection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self.cursor = cursor
        self.find_calls: list[tuple[dict[str, Any], dict[str, int] | None]] = []

    def find(
        self,
        query: dict[str, Any],
        projection: dict[str, int] | None,
    ) -> _FakeCursor:
        self.find_calls.append((query, projection))
        return self.cursor


def test_find_applies_server_deadline_before_cursor_options(monkeypatch) -> None:
    cursor = _FakeCursor()
    collection = _FakeCollection(cursor)
    monkeypatch.setattr(
        repository.ConceptsRepository,
        "collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        repository,
        "apply_concept_query_filter",
        lambda query: {**query, "access_scope": "test"},
    )

    result = repository.ConceptsRepository.find(
        {"concept_id": {"$in": ["#V#person"]}},
        projection={"concept_id": 1},
        sort=[("concept_id", 1)],
        skip=2,
        limit=3,
        max_time_ms=5_000,
    )

    assert collection.find_calls == [
        (
            {
                "concept_id": {"$in": ["#V#person"]},
                "access_scope": "test",
            },
            {"concept_id": 1},
        )
    ]
    assert cursor.operations == [
        ("max_time_ms", 5_000),
        ("sort", [("concept_id", 1)]),
        ("skip", 2),
        ("limit", 3),
    ]
    assert result._cursor is cursor
