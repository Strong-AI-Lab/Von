from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _fail_if_aggregated(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("canonical aggregation must not run")


def test_relationship_target_visibility_is_batched_without_changing_shape(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    checked: list[list[str]] = []
    monkeypatch.setattr(
        service,
        "should_enforce_access_control",
        lambda: True,
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: (
            checked.append(list(concept_ids)) or {"#V#visible"}
        ),
    )

    filtered = service._filter_accessible_relationships(
        {
            "#V#list_relation": [
                "#V#visible",
                "#V#hidden",
                "literal value",
            ],
            "#V#single_hidden": "#V#hidden",
            "#V#single_literal": "literal value",
            "#V#structured_value": {"key": "value"},
        }
    )

    assert checked == [["#V#visible", "#V#hidden", "#V#hidden"]]
    assert filtered == {
        "#V#list_relation": ["#V#visible", "literal value"],
        "#V#single_hidden": [],
        "#V#single_literal": "literal value",
        "#V#structured_value": {"key": "value"},
    }


def test_relation_preview_default_is_false_only_on_model_facing_tools(
    monkeypatch,
) -> None:
    import asyncio
    import inspect

    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.mcp_server import mcp_stdio_server
    from src.backend.services import concept_relation_service as service
    from src.backend.services import tool_metadata_service

    assert (
        inspect.signature(service.find_relations_with_argument)
        .parameters["include_concept_preview"]
        .default
        is True
    )
    observed: list[bool] = []

    def fake_find_relations_with_argument(concept_id: str, **kwargs: Any):
        observed.append(kwargs["include_concept_preview"])
        return {
            "concept_id": concept_id,
            "total_hits": 0,
            "hits": [],
            "paging": {
                "limit": 20,
                "offset": 0,
                "returned": 0,
                "total_available": 0,
            },
            "relation_query_diagnostics": {},
        }

    monkeypatch.setattr(
        service,
        "find_relations_with_argument",
        fake_find_relations_with_argument,
    )
    catalogue._find_relations_with_argument(concept_id="#V#anchor")
    catalogue._find_relations_with_argument(
        concept_id="#V#anchor",
        include_concept_preview=True,
    )
    monkeypatch.setattr(
        mcp_stdio_server,
        "find_relations_with_argument",
        fake_find_relations_with_argument,
    )
    asyncio.run(
        mcp_stdio_server._handle_find_relations_with_argument(
            {"concept_id": "#V#anchor"}
        )
    )
    asyncio.run(
        mcp_stdio_server._handle_find_relations_with_argument(
            {"concept_id": "#V#anchor", "include_concept_preview": True}
        )
    )

    monkeypatch.setattr(tool_metadata_service, "_load_from_vontology", dict)
    tool_metadata_service.invalidate_cache()
    try:
        metadata = tool_metadata_service.get_tool_metadata(
            "find_relations_with_argument"
        )
        assert metadata.default_payload is not None
        assert metadata.default_payload["include_concept_preview"] is False
    finally:
        tool_metadata_service.invalidate_cache()

    assert observed == [False, True, False, True]


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
        "projection": {
            "_id": 0,
            "source_concept_id": 1,
            "predicate_id": 1,
            "target_value": 1,
            "target_index": 1,
            "source_updated_at": 1,
        },
        "batch_size": 20_000,
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
        "bounded": False,
        "complete": True,
    }


def test_relation_hits_include_source_previews_for_both_directions(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    focal_id = "#V#focal"
    index_queries: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: {
            "concept_id": focal_id,
            "relationships": {"#V#direct_relation": ["#V#outgoing_related"]},
        },
    )
    def query_index(**kwargs: Any):
        index_queries.append(kwargs)
        return (
            [
                {
                    "source_concept_id": "#V#incoming_related",
                    "predicate_id": "#V#inverse_relation",
                    "target_value": focal_id,
                    "target_index": 0,
                    "source_updated_at": None,
                }
            ],
            1,
        )

    monkeypatch.setattr(service, "query_relationship_extent_index", query_index)
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    preview_docs = {
        focal_id: {"concept_id": focal_id, "name": "Focal", "relationships": {}},
        "#V#outgoing_related": {
            "concept_id": "#V#outgoing_related",
            "name": "Outgoing Related",
            "relationships": {"is_an_instance_of": ["#V#requested_type"]},
        },
        "#V#incoming_related": {
            "concept_id": "#V#incoming_related",
            "name": "Incoming Related",
            "relationships": {"is_an_instance_of": ["#V#requested_type"]},
        },
    }
    monkeypatch.setattr(service, "should_enforce_access_control", lambda: False)
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        staticmethod(
            lambda query, _projection: [
                preview_docs[concept_id]
                for concept_id in query["concept_id"]["$in"]
                if concept_id in preview_docs
            ]
        ),
    )
    monkeypatch.setattr(
        service,
        "_load_accessible_preview_document",
        lambda concept_id: preview_docs.get(concept_id),
    )

    payload = service.find_relations_with_argument(
        focal_id,
        argument_index="any",
        predicate_filter=["#V#direct_relation", "#V#inverse_relation"],
        relation_kind="binary",
        include_concept_preview=True,
        limit=10,
    )

    hits_by_source = {hit["source_concept_id"]: hit for hit in payload["hits"]}
    assert hits_by_source[focal_id]["source_concept_preview"]["name"] == "Focal"
    assert (
        hits_by_source["#V#incoming_related"]["source_concept_preview"]["name"]
        == "Incoming Related"
    )
    assert hits_by_source["#V#incoming_related"]["source_concept_preview"][
        "type_ids"
    ] == ["#V#requested_type"]
    assert index_queries[0]["predicate_ids"] == [
        "#V#direct_relation",
        "#V#inverse_relation",
    ]


def test_outgoing_relation_previews_batch_uncached_targets(monkeypatch) -> None:
    from src.backend.services import concept_relation_service as service

    source_id = "#V#source"
    target_ids = ("#V#target_a", "#V#target_b", "#V#target_c")
    preview_queries: list[tuple[dict[str, Any], dict[str, int]]] = []
    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: {
            "concept_id": source_id,
            "relationships": {
                "#V#supervises": list(target_ids),
                "#V#unrelated": ["#V#not_requested"],
            },
        },
    )
    monkeypatch.setattr(service, "should_enforce_access_control", lambda: False)

    def fake_find(query, projection):
        preview_queries.append((query, projection))
        return [
            {
                "concept_id": concept_id,
                "name": concept_id.removeprefix("#V#"),
                "relationships": {},
            }
            for concept_id in (source_id, *target_ids)
        ]

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        staticmethod(fake_find),
    )
    monkeypatch.setattr(
        service,
        "_load_accessible_preview_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("batched target previews must avoid point reads")
        ),
    )

    payload = service.find_relations_with_argument(
        source_id,
        argument_index="subject",
        predicate_filter=["#V#supervises"],
        relation_kind="binary",
        include_concept_preview=True,
        offset=1,
        limit=1,
    )

    assert len(preview_queries) == 1
    assert preview_queries[0][0] == {
        "concept_id": {"$in": [source_id, "#V#target_b"]}
    }
    assert payload["total_hits"] == 3
    assert payload["paging"]["returned"] == 1
    assert payload["hits"][0]["target_concept_preview"]["concept_id"] == (
        "#V#target_b"
    )


def test_incoming_relation_previews_batch_uncached_sources(monkeypatch) -> None:
    from src.backend.services import concept_relation_service as service

    target_id = "#V#target"
    preview_queries: list[tuple[dict[str, Any], dict[str, int]]] = []
    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service,
        "incoming_dynamic_extent_rows_page_for_target",
        lambda *_args, **_kwargs: (
            [
                {
                    "source_concept_id": source_id,
                    "predicate_id": "#V#relation",
                    "target_value": target_id,
                    "arg2_index": 2,
                    "updated_at": None,
                }
                for source_id in ("#V#source_a", "#V#source_b")
            ],
            True,
            {
                "used_extent_index": True,
                "complete": True,
                "bounded": False,
                "has_more": False,
                "index_rows_scanned": 2,
                "rows_filtered_by_access": 0,
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        service,
        "should_enforce_access_control",
        lambda: True,
    )

    def fake_find(query, projection):
        preview_queries.append((query, projection))
        return [
            {
                "concept_id": source_id,
                "name": source_id.removeprefix("#V#"),
                "relationships": {},
            }
            for source_id in (target_id, "#V#source_a", "#V#source_b")
        ]

    monkeypatch.setattr(
        service.ConceptsRepository, "find", staticmethod(fake_find)
    )
    monkeypatch.setattr(
        service,
        "_load_accessible_preview_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("batched source previews must avoid point reads")
        ),
    )

    payload = service.find_relations_with_argument(
        target_id,
        relation_kind="binary",
        include_concept_preview=True,
    )

    assert len(preview_queries) == 1
    assert preview_queries[0][0] == {
        "concept_id": {
            "$in": ["#V#source_a", target_id, "#V#source_b"]
        }
    }
    assert {
        hit["source_concept_preview"]["concept_id"]
        for hit in payload["hits"]
    } == {"#V#source_a", "#V#source_b"}


def test_incoming_relation_previews_only_hydrate_the_returned_page(monkeypatch) -> None:
    from src.backend.services import concept_relation_service as service

    target_id = "#V#target"
    source_ids = ("#V#source_a", "#V#source_b", "#V#source_c")
    preview_queries: list[tuple[dict[str, Any], dict[str, int]]] = []
    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service,
        "incoming_dynamic_extent_rows_page_for_target",
        lambda *_args, **_kwargs: (
            [
                {
                    "source_concept_id": source_id,
                    "predicate_id": "#V#relation",
                    "target_value": target_id,
                    "arg2_index": 2,
                    "updated_at": None,
                }
                for source_id in source_ids
            ],
            True,
            {
                "used_extent_index": True,
                "complete": True,
                "bounded": False,
                "has_more": False,
                "index_rows_scanned": 3,
                "rows_filtered_by_access": 0,
            },
        ),
    )
    monkeypatch.setattr(service, "should_enforce_access_control", lambda: False)

    def fake_find(query, projection):
        preview_queries.append((query, projection))
        return [
            {
                "concept_id": concept_id,
                "name": concept_id.removeprefix("#V#"),
                "relationships": {},
            }
            for concept_id in query["concept_id"]["$in"]
        ]

    monkeypatch.setattr(
        service.ConceptsRepository, "find", staticmethod(fake_find)
    )
    monkeypatch.setattr(
        service,
        "_load_accessible_preview_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paged previews must avoid point reads")
        ),
    )

    payload = service.find_relations_with_argument(
        target_id,
        relation_kind="binary",
        include_concept_preview=True,
        sort_by="predicate",
        offset=1,
        limit=1,
    )

    assert payload["total_hits"] == 3
    assert payload["paging"] == {
        "limit": 1,
        "offset": 1,
        "returned": 1,
        "total_available": 3,
    }
    assert len(preview_queries) == 1
    assert preview_queries[0][0] == {
        "concept_id": {"$in": ["#V#source_b", target_id]}
    }
    assert payload["hits"][0]["source_concept_id"] == "#V#source_b"
    assert payload["hits"][0]["source_concept_preview"]["concept_id"] == (
        "#V#source_b"
    )
    assert payload["hits"][0]["target_concept_preview"]["concept_id"] == target_id
    assert payload["hits"][0]["access_granted"] is True


def test_incoming_asserted_binary_falls_back_only_when_index_is_unavailable(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    target_id = "#V#target"
    updated_at = datetime(2026, 7, 26, 5, 6, tzinfo=UTC)
    exact_queries: list[
        tuple[dict[str, Any], dict[str, Any], int | None]
    ] = []

    def fake_find(query, projection, *, max_time_ms=None):
        exact_queries.append((query, projection, max_time_ms))
        return [
            {
                "concept_id": "#V#source",
                "relationships": {
                    "#V#supervises": ["#V#other", target_id],
                },
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
        service.ConceptsRepository, "find", staticmethod(fake_find)
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "aggregate",
        staticmethod(_fail_if_aggregated),
    )

    payload = service.find_relations_with_argument(
        target_id,
        argument_index=3,
        predicate_filter=["#V#supervises"],
        relation_kind="binary",
        include_concept_preview=False,
    )

    assert exact_queries == [
        (
            {"$or": [{"relationships.#V#supervises": target_id}]},
            {
                "_id": 0,
                "concept_id": 1,
                "updated_at": 1,
                "relationships.#V#supervises": 1,
            },
            8_000,
        )
    ]
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
        "path": "canonical_exact_predicate_query",
        "used_relationship_extent_index": False,
        "fallback_reason": "relationship_extent_index_unavailable",
        "index_rows_examined": 0,
        "canonical_rows_returned": 1,
        "rows_filtered_by_access": 0,
        "bounded": False,
        "complete": True,
    }


def test_unresolved_predicate_label_returns_typed_incomplete_without_full_scan(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    aggregate_pipelines: list[list[dict[str, Any]]] = []

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
        service.ConceptsRepository,
        "find",
        staticmethod(_fail_if_aggregated),
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "aggregate",
        staticmethod(
            lambda pipeline: aggregate_pipelines.append(pipeline) or []
        ),
    )

    payload = service.find_relations_with_argument(
        "#V#target",
        predicate_filter=["supervises"],
        relation_kind="binary",
        include_concept_preview=False,
    )

    assert aggregate_pipelines == []
    assert payload["total_hits"] == 0
    assert payload["total_hits_is_lower_bound"] is True
    assert payload["paging"]["total_available_is_lower_bound"] is True
    diagnostics = payload["relation_query_diagnostics"][
        "incoming_asserted_binary"
    ]
    assert diagnostics["path"] == "relationship_extent_index_unavailable"
    assert diagnostics["fallback_reason"] == (
        "relationship_extent_index_unavailable"
    )
    assert diagnostics["stop_reason"] == "relationship_extent_index_unavailable"
    assert diagnostics["bounded"] is False
    assert diagnostics["complete"] is False


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
        "incoming_dynamic_extent_rows_page_for_target",
        lambda *_args, **_kwargs: (
            [],
            True,
            {
                "used_extent_index": True,
                "complete": True,
                "bounded": False,
                "has_more": False,
                "index_rows_scanned": 0,
                "rows_filtered_by_access": 0,
            },
        ),
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


def test_unfiltered_incoming_extent_is_bounded_and_labelled_as_lower_bound(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    target_id = "#V#target"
    page_query: dict[str, Any] = {}

    def fake_page(concept_id: str, **kwargs: Any):
        page_query["concept_id"] = concept_id
        page_query.update(kwargs)
        return (
            [
                {
                    "source_concept_id": "#V#source",
                    "predicate_id": "#V#related_to",
                    "target_value": target_id,
                    "arg2_index": 2,
                    "updated_at": None,
                }
            ],
            True,
            {
                "used_extent_index": True,
                "complete": False,
                "bounded": True,
                "has_more": True,
                "index_rows_scanned": 128,
                "rows_filtered_by_access": 3,
                "stop_reason": "scan_cap_exhausted",
            },
        )

    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service,
        "incoming_dynamic_extent_rows_page_for_target",
        fake_page,
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "aggregate",
        staticmethod(_fail_if_aggregated),
    )

    payload = service.find_relations_with_argument(
        target_id,
        relation_kind="binary",
        include_concept_preview=False,
        limit=20,
    )

    assert page_query == {
        "concept_id": target_id,
        "exclude_structural_predicates": False,
        "visible_offset": 0,
        "visible_limit": 21,
    }
    assert payload["total_hits"] == 1
    assert payload["total_hits_is_lower_bound"] is True
    assert payload["paging"]["total_available_is_lower_bound"] is True
    diagnostics = payload["relation_query_diagnostics"][
        "incoming_asserted_binary"
    ]
    assert diagnostics["bounded"] is True
    assert diagnostics["complete"] is False
    assert diagnostics["has_more"] is True
    assert diagnostics["index_rows_examined"] == 128
    assert diagnostics["rows_filtered_by_access"] == 3


def test_unfiltered_extent_timeout_does_not_start_canonical_full_scan(
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
        "incoming_dynamic_extent_rows_page_for_target",
        lambda *_args, **_kwargs: (
            [],
            False,
            {
                "used_extent_index": False,
                "complete": False,
                "bounded": False,
                "reason": "extent_index_query_failed",
            },
        ),
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
        limit=20,
    )

    assert payload["total_hits"] == 0
    assert payload["total_hits_is_lower_bound"] is True
    diagnostics = payload["relation_query_diagnostics"][
        "incoming_asserted_binary"
    ]
    assert diagnostics["path"] == "relationship_extent_index_bounded_failure"
    assert diagnostics["used_relationship_extent_index"] is False
    assert diagnostics["bounded"] is True
    assert diagnostics["complete"] is False


def test_predicate_incidence_materialises_large_ready_index_once(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    target_id = "#V#target"
    index_calls: list[dict[str, Any]] = []
    indexed_rows = [
        {
            "source_concept_id": f"#V#source_{index}",
            "predicate_id": "#V#related_to",
            "target_value": target_id,
            "target_index": 0,
            "source_updated_at": None,
        }
        for index in range(12_005)
    ]

    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )

    def query_index(**kwargs: Any):
        index_calls.append(kwargs)
        return indexed_rows, len(indexed_rows)

    monkeypatch.setattr(service, "query_relationship_extent_index", query_index)
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "aggregate",
        staticmethod(_fail_if_aggregated),
    )
    monkeypatch.setattr(
        service,
        "_load_accessible_preview_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unselected incidence rows must not read previews")
        ),
    )
    monkeypatch.setattr(
        service,
        "_attach_direct_type_ids_to_fast_incidence_hits",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("default incidence must not hydrate argument types")
        ),
    )

    payload = service.get_predicate_incidence(
        concept_id=target_id,
        argument_index="object",
        relation_kind="binary",
        include_concept_preview=False,
        limit=20,
    )

    assert len(index_calls) == 1
    assert payload["coverage_complete"] is True
    assert "counts_are_lower_bounds" not in payload
    assert payload["total_predicates"] == 1
    assert payload["predicates"][0]["predicate_concept_id"] == "#V#related_to"
    assert payload["predicates"][0]["relation_hit_count"] == 12_005
    assert payload["predicates"][0]["grounding_count"] == 12_005
    assert payload["predicates"][0]["object_argument_hit_count"] == 12_005


def test_predicate_incidence_reports_incomplete_without_unindexed_full_scan(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    target_id = "#V#target"
    aggregate_calls: list[list[dict[str, Any]]] = []
    incoming_rows = [
        {
            "concept_id": f"#V#source_{index}",
            "predicate": "#V#related_to",
            "targets": [target_id],
            "updated_at": None,
        }
        for index in range(12_005)
    ]

    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        service,
        "incoming_dynamic_extent_rows_page_for_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete incidence must not use a bounded relation page")
        ),
    )
    monkeypatch.setattr(
        service,
        "query_relationship_extent_index",
        lambda **_kwargs: ([], -1),
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "aggregate",
        staticmethod(
            lambda pipeline: aggregate_calls.append(pipeline) or incoming_rows
        ),
    )
    monkeypatch.setattr(
        service,
        "_load_accessible_preview_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unselected incidence rows must not read previews")
        ),
    )
    monkeypatch.setattr(
        service,
        "_attach_direct_type_ids_to_fast_incidence_hits",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("default incidence must not hydrate argument types")
        ),
    )

    payload = service.get_predicate_incidence(
        concept_id=target_id,
        argument_index="object",
        relation_kind="binary",
        include_concept_preview=False,
        limit=20,
    )

    assert aggregate_calls == []
    assert payload["coverage_complete"] is False
    assert payload["counts_are_lower_bounds"] is True
    assert payload["total_predicates"] == 0
    assert payload["predicates"] == []
    assert payload["paging"]["total_available_is_lower_bound"] is True
    diagnostics = payload["predicate_incidence_query_diagnostics"]
    assert diagnostics["coverage_complete"] is False
    assert diagnostics["coverage_gaps"] == [
        {
            "relation_kind": "binary",
            "argument_index": "object",
            "path": "relationship_extent_index_unavailable",
            "reason": "relationship_extent_index_unavailable",
        }
    ]
