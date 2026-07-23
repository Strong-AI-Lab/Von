from __future__ import annotations

import ast
import inspect
from contextlib import contextmanager
from typing import Any

from src.backend.services import concept_search_service as svc


def _concept(concept_id: str, name: str | None = None) -> dict[str, Any]:
    return {
        "concept_id": concept_id,
        "name": name or concept_id.rsplit("#", 1)[-1],
        "relationships": {},
    }


def test_all_direct_concept_search_repository_reads_have_deadlines() -> None:
    tree = ast.parse(inspect.getsource(svc))
    repositories = {
        "ConceptsRepository",
        "TextRelationsRepository",
        "TextValuesRepository",
    }
    calls_by_repository: dict[str, list[ast.Call]] = {
        repository: [] for repository in repositories
    }

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (
            node.func.attr != "find"
            or not isinstance(owner, ast.Name)
            or owner.id not in repositories
        ):
            continue
        calls_by_repository[owner.id].append(node)

    for repository, calls in calls_by_repository.items():
        assert calls, f"expected at least one {repository}.find call"
        for call in calls:
            deadline = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "max_time_ms"),
                None,
            )
            assert isinstance(deadline, ast.Name), (
                f"{repository}.find at line {call.lineno} has no named deadline"
            )
            assert deadline.id == "CONCEPT_SEARCH_QUERY_MAX_TIME_MS"


def test_fingerprint_search_consumes_cursor_under_total_client_deadline(
    monkeypatch,
) -> None:
    deadline_active = False
    observed_timeouts: list[float] = []

    @contextmanager
    def fake_timeout(seconds: float):
        nonlocal deadline_active
        observed_timeouts.append(seconds)
        deadline_active = True
        try:
            yield
        finally:
            deadline_active = False

    class DeadlineAwareCursor:
        def __init__(self):
            self._rows = iter([{"_id": "507f1f77bcf86cd799439011"}])

        def __iter__(self):
            return self

        def __next__(self):
            assert deadline_active is True
            return next(self._rows)

    monkeypatch.setattr(svc, "timeout", fake_timeout)
    monkeypatch.setattr(
        svc.TextValuesRepository,
        "find",
        lambda *_args, **_kwargs: DeadlineAwareCursor(),
    )

    result = svc._find_text_values_by_fingerprint("bounded")

    assert result == [{"_id": "507f1f77bcf86cd799439011"}]
    assert observed_timeouts == [svc.CONCEPT_SEARCH_OPERATION_TIMEOUT_SECONDS]


def test_modern_text_relation_hits_skip_legacy_regex_scan(monkeypatch) -> None:
    concept_ids = [f"#V#paper_{index}" for index in range(4)]
    concept_docs = [_concept(concept_id) for concept_id in concept_ids]
    concept_find_calls: list[dict[str, Any]] = []

    def fake_text_values_find(query, *args, **kwargs):
        if query.get("fingerprint"):
            return [{"_id": "507f1f77bcf86cd799439011"}]
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


def test_text_relations_substring_uses_bounded_id_only_text_search(
    monkeypatch,
) -> None:
    text_value_id = "507f1f77bcf86cd799439011"
    concept_docs = [_concept(f"#V#workflow_{index}") for index in range(10)]
    text_value_calls: list[dict[str, Any]] = []
    relation_calls: list[dict[str, Any]] = []

    def fake_text_values_find(query, *args, **kwargs):
        text_value_calls.append({"query": query, **kwargs})
        if "$text" in query:
            return [{"_id": text_value_id}]
        return []

    def fake_text_relations_find(query, *args, **kwargs):
        relation_calls.append({"query": query, **kwargs})
        return [
            {
                "subject_concept_id": concept_doc["concept_id"],
                "object_text_id": text_value_id,
                "predicate": "hasName",
            }
            for concept_doc in concept_docs
        ]

    def fake_concepts_find(query, *args, **kwargs):
        assert "$or" not in query
        return list(concept_docs)

    monkeypatch.setattr(svc.TextValuesRepository, "find", fake_text_values_find)
    monkeypatch.setattr(svc.TextRelationsRepository, "find", fake_text_relations_find)
    monkeypatch.setattr(svc.ConceptsRepository, "find", fake_concepts_find)
    monkeypatch.setattr(svc, "_determine_concept_kind", lambda _doc: "individual")

    result = svc.search_concepts("workflow", match_type="substring", limit=5)

    text_queries = [call["query"] for call in text_value_calls]
    assert not any("text" in query and "$regex" in query["text"] for query in text_queries)
    assert text_queries[0]["fingerprint"]["$type"] == "string"
    assert text_queries[1] == {"$text": {"$search": "workflow"}}
    assert text_value_calls[1]["projection"] == {"_id": 1}
    assert text_value_calls[1]["limit"] == 200
    assert (
        text_value_calls[1]["max_time_ms"] == svc.CONCEPT_SEARCH_QUERY_MAX_TIME_MS
    )
    assert relation_calls[0]["projection"] == {"subject_concept_id": 1}
    assert relation_calls[0]["limit"] == 800
    assert relation_calls[0]["max_time_ms"] == svc.CONCEPT_SEARCH_QUERY_MAX_TIME_MS
    assert result["match_types_used"] == ["text_relations"]


def test_text_value_fingerprint_search_uses_partial_index_shape(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_text_values_find(query, *args, **kwargs):
        calls.append({"query": query, **kwargs})
        return [{"_id": "507f1f77bcf86cd799439011"}]

    monkeypatch.setattr(svc.TextValuesRepository, "find", fake_text_values_find)

    result = svc._find_text_values_by_fingerprint("AI Researcher".casefold(), limit=7)

    assert result == [{"_id": "507f1f77bcf86cd799439011"}]
    assert calls == [
        {
            "query": {
                "fingerprint": {
                    "$regex": r"^ai\ researcher\|\|",
                    "$type": "string",
                }
            },
            "projection": {"_id": 1},
            "limit": 7,
            "max_time_ms": svc.CONCEPT_SEARCH_QUERY_MAX_TIME_MS,
        }
    ]
