from __future__ import annotations

from src.backend.server.routes import predicate_routes


def test_generic_predicate_extent_hides_login_identity_without_store_access(
    monkeypatch,
) -> None:
    def unexpected_store_access(*_args, **_kwargs):
        raise AssertionError("hidden predicate extent must not query storage")

    monkeypatch.setattr(
        predicate_routes,
        "_query_text_relations_extent",
        unexpected_store_access,
    )
    monkeypatch.setattr(
        predicate_routes,
        "_query_structured_relations_extent",
        unexpected_store_access,
    )

    result = predicate_routes.get_predicate_extent_data(
        concept_id="#V#hasVonLoginEmail",
        limit=25,
        offset=10,
    )

    assert result == {
        "concept_id": "#V#hasVonLoginEmail",
        "extent": [],
        "total_count": 0,
        "limit": 25,
        "offset": 10,
        "has_more": False,
        "sampled": False,
    }


def test_generic_predicate_extent_hides_sample_and_statistics(monkeypatch) -> None:
    def unexpected_store_access(*_args, **_kwargs):
        raise AssertionError("hidden predicate statistics must not query storage")

    monkeypatch.setattr(
        predicate_routes,
        "get_text_relations_collection",
        unexpected_store_access,
    )
    monkeypatch.setattr(
        predicate_routes,
        "get_concepts_collection",
        unexpected_store_access,
    )

    sampled = predicate_routes.get_predicate_extent_data(
        concept_id="#V#hasVonLoginEmail",
        sample_size=5,
    )

    assert sampled["extent"] == []
    assert sampled["total_count"] == 0
    assert sampled["sampled"] is True
    assert sampled["sample_size"] == 5
    assert predicate_routes._calculate_extent_statistics("#V#hasVonLoginEmail") == {
        "total_uses": 0,
        "unique_subjects": 0,
        "unique_objects": 0,
    }


def test_non_sensitive_predicate_extent_keeps_existing_query_path(monkeypatch) -> None:
    monkeypatch.setattr(
        predicate_routes,
        "_query_text_relations_extent",
        lambda *_args, **_kwargs: ([{"predicate": "#V#hasName"}], 1),
    )
    monkeypatch.setattr(
        predicate_routes,
        "_query_structured_relations_extent",
        lambda *_args, **_kwargs: ([], 0),
    )

    result = predicate_routes.get_predicate_extent_data(
        concept_id="#V#hasName",
        source_filter="text_relations",
    )

    assert result["extent"] == [{"predicate": "#V#hasName"}]
    assert result["total_count"] == 1
