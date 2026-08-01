from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest


def _hit(
    predicate_id: str,
    target_id: str,
    *,
    source_id: str = "#V#anchor",
) -> dict[str, Any]:
    return {
        "source_concept_id": source_id,
        "predicate_concept_id": predicate_id,
        "relation_kind": "binary",
        "argument_indexes": [1],
        "target_value": target_id,
        "target_concept_preview": None,
        "relation_metadata": {
            "relation_id": f"struct::{source_id}::{predicate_id}::{target_id}"
        },
        "access_granted": True,
        "follow_up_actions": [],
        "score": 1.0,
        "is_asserted": True,
        "relation_state": "asserted",
    }


def _diagnostics() -> dict[str, Any]:
    return {
        "mode": "asserted_only",
        "include_uncertain": False,
        "statuses": None,
    }


def test_selected_incidence_page_is_hydrated_only_after_skeleton_sort(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    hits = [
        _hit("#V#high", "#V#high_target_one"),
        _hit("#V#high", "#V#high_target_two"),
        _hit("#V#high", "#V#high_target_three"),
        _hit("#V#high", "#V#high_target_four"),
        _hit("#V#high", "#V#high_target_five"),
        _hit("#V#low", "#V#low_target"),
    ]
    monkeypatch.setattr(
        service,
        "_collect_all_argument_hits_for_incidence",
        lambda **_kwargs: (list(hits), _diagnostics()),
    )

    aggregate_calls: list[dict[str, Any]] = []
    original_aggregate = service._aggregate_predicate_incidence_rows

    def recording_aggregate(**kwargs):
        aggregate_calls.append(
            {
                "predicates": {
                    hit.get("predicate_concept_id") for hit in kwargs["hits"]
                },
                "include_concept_preview": kwargs["include_concept_preview"],
                "include_type_counts": kwargs["type_count_options"].include,
            }
        )
        return original_aggregate(**kwargs)

    monkeypatch.setattr(
        service,
        "_aggregate_predicate_incidence_rows",
        recording_aggregate,
    )
    hydrated_predicates: list[set[str]] = []

    def attach_types(grouped_hits):
        selected = {
            hit["predicate_concept_id"]
            for grouped in grouped_hits.values()
            for hit in grouped
        }
        hydrated_predicates.append(selected)
        for grouped in grouped_hits.values():
            for hit in grouped:
                hit["target_type_ids"] = [f"{hit['target_value']}_type"]

    monkeypatch.setattr(
        service,
        "_attach_direct_type_ids_to_fast_incidence_hits",
        attach_types,
    )

    def prime_previews(concept_ids, *, include_preview, preview_cache):
        assert include_preview is True
        pending = list(
            dict.fromkeys(
                concept_id
                for concept_id in concept_ids
                if concept_id not in preview_cache
            )
        )
        if len(pending) < 2:
            return
        for concept_id in pending:
            preview_cache[concept_id] = {
                "concept_id": concept_id,
                "name": concept_id,
                "kind": "individual",
            }

    monkeypatch.setattr(service, "_prime_concept_preview_cache", prime_previews)
    monkeypatch.setattr(
        service,
        "_load_accessible_preview_document",
        lambda concept_id: {
            "concept_id": concept_id,
            "name": concept_id,
            "kind": "individual",
        },
    )

    payload = service.get_predicate_incidence(
        concept_id="#V#anchor",
        relation_kind="binary",
        include_concept_preview=True,
        include_argument_type_counts=True,
        limit=1,
    )

    assert [row["predicate_concept_id"] for row in payload["predicates"]] == ["#V#high"]
    assert aggregate_calls == [
        {
            "predicates": {"#V#high", "#V#low"},
            "include_concept_preview": False,
            "include_type_counts": False,
        }
    ]
    assert hydrated_predicates == [{"#V#high"}]
    assert payload["predicates"][0]["predicate_preview"]["concept_id"] == "#V#high"
    type_counts = {
        row["type_concept_id"]: row
        for row in payload["predicates"][0]["argument_type_counts"]
    }
    fifth_sample = type_counts["#V#high_target_five_type"]["sample_concepts"][0]
    assert fifth_sample["name"] == "#V#high_target_five"


def test_selected_page_hydration_uses_normalised_predicate_ids(monkeypatch) -> None:
    from src.backend.services import concept_relation_service as service

    monkeypatch.setattr(
        service,
        "_collect_all_argument_hits_for_incidence",
        lambda **_kwargs: (
            [_hit("  #V#related_to  ", "#V#target")],
            _diagnostics(),
        ),
    )

    def prime_previews(concept_ids, *, include_preview, preview_cache):
        assert include_preview is True
        for concept_id in concept_ids:
            preview_cache[concept_id] = {
                "concept_id": concept_id,
                "name": concept_id,
                "kind": "predicate",
            }

    monkeypatch.setattr(service, "_prime_concept_preview_cache", prime_previews)

    payload = service.get_predicate_incidence(
        concept_id="#V#anchor",
        relation_kind="binary",
        include_concept_preview=True,
        limit=1,
    )

    row = payload["predicates"][0]
    assert row["predicate_concept_id"] == "#V#related_to"
    assert row["predicate_preview"]["concept_id"] == "#V#related_to"


@pytest.mark.parametrize(
    "sort_by",
    [
        "relation_hit_count",
        "grounding_count",
        "grounded_instance_count",
        "predicate",
    ],
)
def test_incidence_sort_and_offset_use_complete_skeleton(
    monkeypatch,
    sort_by: str,
) -> None:
    from src.backend.services import concept_relation_service as service

    hits = [
        *[_hit("#V#alpha", f"#V#alpha_{index}") for index in range(5)],
        *[_hit("#V#beta", f"#V#beta_{index}") for index in range(3)],
        _hit("#V#gamma", "#V#gamma_0"),
    ]
    monkeypatch.setattr(
        service,
        "_collect_all_argument_hits_for_incidence",
        lambda **_kwargs: (list(hits), _diagnostics()),
    )

    payload = service.get_predicate_incidence(
        concept_id="#V#anchor",
        relation_kind="binary",
        sort_by=sort_by,
        offset=1,
        limit=1,
    )

    assert payload["total_predicates"] == 3
    assert payload["paging"] == {
        "limit": 1,
        "offset": 1,
        "returned": 1,
        "total_available": 3,
    }
    assert payload["predicates"][0]["predicate_concept_id"] == "#V#beta"
    assert payload["predicates"][0]["relation_hit_count"] == 3


def test_type_incidence_preserves_counts_while_hydrating_selected_page(
    monkeypatch,
) -> None:
    from src.backend.services import concept_relation_service as service

    grouped_hits = {
        "#V#instance_one": [
            _hit("#V#high", "#V#shared", source_id="#V#instance_one"),
            _hit("#V#low", "#V#other", source_id="#V#instance_one"),
        ],
        "#V#instance_two": [_hit("#V#high", "#V#shared", source_id="#V#instance_two")],
    }
    monkeypatch.setattr(
        service,
        "_resolve_type_incidence_type_ids",
        lambda *_args, **_kwargs: ["#V#test_type"],
    )
    monkeypatch.setattr(
        service,
        "_resolve_type_incidence_instance_ids",
        lambda _type_ids: list(grouped_hits),
    )
    monkeypatch.setattr(
        service,
        "_collect_type_subject_hits_for_incidence_fast",
        lambda **_kwargs: {key: list(value) for key, value in grouped_hits.items()},
    )
    selected_for_types: list[set[str]] = []

    def attach_types(selected):
        selected_for_types.append(
            {hit["predicate_concept_id"] for hits in selected.values() for hit in hits}
        )
        for hits in selected.values():
            for hit in hits:
                hit["target_type_ids"] = ["#V#shared_type"]

    monkeypatch.setattr(
        service,
        "_attach_direct_type_ids_to_fast_incidence_hits",
        attach_types,
    )
    monkeypatch.setattr(
        service,
        "_prime_concept_preview_cache",
        lambda concept_ids, *, include_preview, preview_cache: preview_cache.update(
            {concept_id: {"concept_id": concept_id} for concept_id in concept_ids}
        ),
    )

    payload = service.get_predicate_incidence(
        instance_of="#V#test_type",
        direct_instances_only=True,
        relation_kind="binary",
        include_argument_type_counts=True,
        limit=1,
    )

    row = payload["predicates"][0]
    assert row["predicate_concept_id"] == "#V#high"
    assert row["relation_hit_count"] == 2
    assert row["grounding_count"] == 1
    assert row["grounded_instance_count"] == 2
    assert selected_for_types == [{"#V#high"}]


def test_role_expansion_reads_only_selected_predicate_frames(monkeypatch) -> None:
    from src.backend.services import concept_relation_service as service

    hits = [
        _hit("#V#high", "#V#high_frame_one"),
        _hit("#V#high", "#V#high_frame_two"),
        _hit("#V#low", "#V#low_frame"),
    ]
    monkeypatch.setattr(
        service,
        "_collect_all_argument_hits_for_incidence",
        lambda **_kwargs: (list(hits), _diagnostics()),
    )
    selected_for_types: list[set[str]] = []

    def attach_types(selected):
        selected_for_types.append(
            {
                hit["predicate_concept_id"]
                for grouped in selected.values()
                for hit in grouped
            }
        )
        for grouped in selected.values():
            for hit in grouped:
                hit["target_type_ids"] = ["#V#frame_type"]

    monkeypatch.setattr(
        service,
        "_attach_direct_type_ids_to_fast_incidence_hits",
        attach_types,
    )
    monkeypatch.setattr(
        service,
        "_prime_concept_preview_cache",
        lambda *_args, **_kwargs: None,
    )
    loaded_frames: list[str] = []

    def load_frame(concept_id, **_kwargs):
        loaded_frames.append(concept_id)
        return {
            "concept_id": concept_id,
            "relationships": {"#V#has_literal_role": ["represented value"]},
        }

    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        load_frame,
    )

    payload = service.get_predicate_incidence(
        concept_id="#V#anchor",
        relation_kind="binary",
        role_expansion_mode="explicit",
        role_node_type_filter=["#V#frame_type"],
        limit=1,
    )

    assert payload["predicates"][0]["predicate_concept_id"] == "#V#high"
    assert selected_for_types == [{"#V#high"}]
    assert set(loaded_frames) == {"#V#high_frame_one", "#V#high_frame_two"}
    assert "#V#low_frame" not in loaded_frames


def test_incoming_incidence_excludes_inaccessible_sources(monkeypatch) -> None:
    from src.backend.services import concept_relation_service as service

    monkeypatch.setattr(
        service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(service, "should_enforce_access_control", lambda: True)
    monkeypatch.setattr(
        service,
        "filter_accessible_concept_ids",
        lambda concept_ids: {
            concept_id
            for concept_id in concept_ids
            if concept_id == "#V#visible_source"
        },
    )
    monkeypatch.setattr(
        service,
        "query_relationship_extent_index",
        lambda **_kwargs: (
            [
                {
                    "source_concept_id": "#V#visible_source",
                    "predicate_id": "#V#related_to",
                    "target_value": "#V#anchor",
                    "target_index": 0,
                    "source_updated_at": None,
                },
                {
                    "source_concept_id": "#V#hidden_source",
                    "predicate_id": "#V#related_to",
                    "target_value": "#V#anchor",
                    "target_index": 0,
                    "source_updated_at": None,
                },
            ],
            2,
        ),
    )

    payload = service.get_predicate_incidence(
        concept_id="#V#anchor",
        argument_index="object",
        predicate_filter=["#V#related_to"],
        relation_kind="binary",
    )

    assert payload["total_predicates"] == 1
    assert payload["predicates"][0]["relation_hit_count"] == 1
    assert payload["predicates"][0]["grounding_count"] == 1
    assert payload["predicates"][0]["sample_groundings"] == [
        {"grounding_kind": "concept", "concept_id": "#V#visible_source"}
    ]


def test_incidence_preview_default_matches_service_internal_mcp_stdio_and_metadata(
    monkeypatch,
) -> None:
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.mcp_server import mcp_stdio_server
    from src.backend.services import concept_relation_service as service
    from src.backend.services import tool_metadata_service

    assert (
        inspect.signature(service.get_predicate_incidence)
        .parameters["include_concept_preview"]
        .default
        is False
    )

    observed: list[bool] = []

    def fake_get_predicate_incidence(**kwargs):
        observed.append(kwargs["include_concept_preview"])
        return {
            "mode": "entity",
            "total_predicates": 0,
            "predicates": [],
            "paging": {},
        }

    monkeypatch.setattr(
        service, "get_predicate_incidence", fake_get_predicate_incidence
    )
    catalogue._get_predicate_incidence(concept_id="#V#anchor")
    catalogue._get_predicate_incidence(
        concept_id="#V#anchor",
        include_concept_preview=True,
    )
    monkeypatch.setattr(
        mcp_stdio_server,
        "get_predicate_incidence",
        fake_get_predicate_incidence,
    )
    asyncio.run(
        mcp_stdio_server._handle_get_predicate_incidence({"concept_id": "#V#anchor"})
    )
    asyncio.run(
        mcp_stdio_server._handle_get_predicate_incidence(
            {"concept_id": "#V#anchor", "include_concept_preview": True}
        )
    )

    monkeypatch.setattr(tool_metadata_service, "_load_from_vontology", dict)
    tool_metadata_service.invalidate_cache()
    try:
        metadata = tool_metadata_service.get_tool_metadata("get_predicate_incidence")
        assert metadata.default_payload is not None
        assert metadata.default_payload["include_concept_preview"] is False
    finally:
        tool_metadata_service.invalidate_cache()

    assert observed == [False, True, False, True]
