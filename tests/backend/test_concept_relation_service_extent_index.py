from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _fail_if_aggregated(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("canonical aggregation must not run")


def test_incoming_asserted_binary_uses_extent_index_without_changing_results(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    target_id = "#V#target"
    updated_at = datetime(2026, 7, 26, 4, 5, tzinfo=UTC)
    indexed_rows = [
        {
            "source_concept_id": "#V#source_a",
            "predicate_id": "#V#supervises",
            "target_value": target_id,
            "target_index": 1,
            "source_updated_at": updated_at,
        },
        {
            "source_concept_id": "#V#source_a",
            "predicate_id": "#V#supervises",
            "target_value": target_id,
            "target_index": 1,
            "source_updated_at": updated_at,
        },
        {
            "source_concept_id": "#V#source_c",
            "predicate_id": "#V#co_supervises",
            "target_value": target_id,
            "target_index": 1,
            "source_updated_at": updated_at,
        },
        {
            "source_concept_id": "#V#private_source",
            "predicate_id": "#V#supervises",
            "target_value": target_id,
            "target_index": 1,
            "source_updated_at": updated_at,
        },
        {
            "source_concept_id": "#V#wrong_predicate",
            "predicate_id": "#V#authored",
            "target_value": target_id,
            "target_index": 1,
            "source_updated_at": updated_at,
        },
        {
            "source_concept_id": "#V#wrong_argument",
            "predicate_id": "#V#supervises",
            "target_value": target_id,
            "target_index": 0,
            "source_updated_at": updated_at,
        },
    ]
    index_query: dict[str, Any] = {}

    def fake_query_relationship_extent_index(**kwargs: Any):
        index_query.update(kwargs)
        return indexed_rows, len(indexed_rows)

    access_candidates: set[str] = set()

    def fake_filter_accessible_concept_ids(concept_ids):
        access_candidates.update(concept_ids)
        return access_candidates - {"#V#private_source"}

    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service,
        "query_relationship_extent_index",
        fake_query_relationship_extent_index,
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        fake_filter_accessible_concept_ids,
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "aggregate",
        staticmethod(_fail_if_aggregated),
    )

    payload = service.find_relations_with_argument(
        target_id,
        argument_index=3,
        predicate_filter=["supervises"],
        relation_kind="binary",
        include_concept_preview=False,
        sort_by="predicate",
        offset=1,
        limit=1,
    )

    assert index_query == {
        "target_value": target_id,
        "count_total": False,
    }
    assert "#V#private_source" in access_candidates
    assert payload["total_hits"] == 2
    assert payload["paging"] == {
        "limit": 1,
        "offset": 1,
        "returned": 1,
        "total_available": 2,
    }
    assert payload["hits"] == [
        {
            "source_concept_id": "#V#source_a",
            "predicate_concept_id": "#V#supervises",
            "relation_kind": "binary",
            "argument_indexes": [3],
            "target_value": target_id,
            "target_concept_preview": None,
            "relation_metadata": {
                "relation_id": (
                    "struct::#V#source_a::#V#supervises::incoming::1"
                ),
                "updated_at": "2026-07-26T04:05:00+00:00",
                "match_type": "exact",
            },
            "access_granted": True,
            "follow_up_actions": [
                {
                    "action": "fetch_concept",
                    "args": {"concept_id": "#V#source_a"},
                }
            ],
            "score": 1.0,
            "is_asserted": True,
            "relation_state": "asserted",
        }
    ]
    assert payload["relation_query_diagnostics"]["incoming_asserted_binary"] == {
        "requested": True,
        "path": "relationship_extent_index",
        "used_relationship_extent_index": True,
        "fallback_reason": None,
        "index_rows_examined": 6,
        "canonical_rows_returned": 0,
        "rows_filtered_by_access": 1,
    }


def test_incoming_asserted_binary_falls_back_only_when_index_is_unavailable(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    target_id = "#V#target"
    updated_at = datetime(2026, 7, 26, 5, 6, tzinfo=UTC)
    aggregate_pipelines: list[list[dict[str, Any]]] = []

    def fake_aggregate(pipeline):
        aggregate_pipelines.append(pipeline)
        return [
            {
                "concept_id": "#V#source",
                "predicate": "#V#supervises",
                "targets": ["#V#other", target_id],
                "updated_at": updated_at,
            }
        ]

    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service,
        "query_relationship_extent_index",
        lambda **_kwargs: ([], -1),
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda _concept_ids: (_ for _ in ()).throw(
            AssertionError("fallback rows are already access-controlled")
        ),
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "aggregate",
        staticmethod(fake_aggregate),
    )

    payload = service.find_relations_with_argument(
        target_id,
        argument_index=3,
        predicate_filter=["#V#supervises"],
        relation_kind="binary",
        include_concept_preview=False,
    )

    assert len(aggregate_pipelines) == 1
    assert payload["total_hits"] == 1
    hit = payload["hits"][0]
    assert hit["source_concept_id"] == "#V#source"
    assert hit["predicate_concept_id"] == "#V#supervises"
    assert hit["argument_indexes"] == [3]
    assert hit["target_value"] == target_id
    assert hit["relation_metadata"] == {
        "relation_id": "struct::#V#source::#V#supervises::incoming::1",
        "updated_at": "2026-07-26T05:06:00+00:00",
        "match_type": "exact",
    }
    assert payload["relation_query_diagnostics"]["incoming_asserted_binary"] == {
        "requested": True,
        "path": "canonical_aggregation",
        "used_relationship_extent_index": False,
        "fallback_reason": "relationship_extent_index_unavailable",
        "index_rows_examined": 0,
        "canonical_rows_returned": 1,
        "rows_filtered_by_access": 0,
    }


def test_empty_available_extent_index_does_not_trigger_canonical_fallback(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service,
        "query_relationship_extent_index",
        lambda **_kwargs: ([], 0),
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda _concept_ids: set(),
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "aggregate",
        staticmethod(_fail_if_aggregated),
    )

    payload = service.find_relations_with_argument(
        "#V#target",
        relation_kind="binary",
        include_concept_preview=False,
    )

    assert payload["total_hits"] == 0
    diagnostics = payload["relation_query_diagnostics"][
        "incoming_asserted_binary"
    ]
    assert diagnostics["path"] == "relationship_extent_index"
    assert diagnostics["used_relationship_extent_index"] is True
    assert diagnostics["fallback_reason"] is None
