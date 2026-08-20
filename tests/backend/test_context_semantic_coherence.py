from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json

import mongomock
from bson import ObjectId


def test_actor_effective_text_is_not_base_publication(monkeypatch):
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import scoped_assertion_service
    from src.backend.services import testing_theory_service
    from src.backend.services import text_value_service

    relation_id = ObjectId()
    text_value_id = ObjectId()
    collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    monkeypatch.setattr(
        scoped_assertion_service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        scoped_assertion_service,
        "can_access_concept",
        lambda _concept_id: True,
    )
    monkeypatch.setattr(
        text_value_service.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {
                "_id": relation_id,
                "subject_concept_id": "#V#gillian_dobbie",
                "predicate": "hasDescription",
                "object_text_id": text_value_id,
            }
        ],
    )
    monkeypatch.setattr(
        text_value_service.TextValuesRepository,
        "find",
        lambda *_args, **_kwargs: [
            {
                "_id": text_value_id,
                "text": "Base programme context.",
                "lang": "en-NZ",
            }
        ],
    )
    monkeypatch.setattr(
        text_value_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        testing_theory_service,
        "get_testing_theory_state",
        lambda _theory_id: {
            "local_assertions": [
                {
                    "assertion_id": "theory_assert_1",
                    "source_id": "#V#gillian_dobbie",
                    "predicate": "hasDescription",
                    "target": "Scoped programme context.",
                    "target_kind": "text",
                    "status": "proposed",
                },
                {
                    "assertion_id": "theory_assert_2",
                    "source_id": "#V#gillian_dobbie",
                    "predicate": "hasDescription",
                    "target": "Base programme context.",
                    "target_kind": "text",
                    "status": "proposed",
                },
            ]
        },
    )

    scoped_assertion_service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="Scoped programme context.",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
        namespace="#V#michael_witbrock@trusted_org",
        canonical_publication=False,
    )

    with override_current_actor("#V#michael_witbrock", "#V#trusted_org"):
        effective_rows = text_value_service.get_texts_for_concept(
            "#V#gillian_dobbie",
            predicate="hasDescription",
            limit=1,
            context_view="actor_effective",
        )
        base_rows = text_value_service.get_texts_for_concept(
            "#V#gillian_dobbie",
            predicate="hasDescription",
            limit=1,
            context_view="base_publication",
        )
        default_rows = text_value_service.get_texts_for_concept(
            "#V#gillian_dobbie",
            predicate="hasDescription",
            limit=1,
        )
        diff_result = testing_theory_service.compute_testing_theory_diff(
            theory_id="#V#test_theory",
        )

    assert [row["text"] for row in effective_rows] == [
        "Scoped programme context."
    ]
    assert effective_rows[0]["row_kind"] == "scoped_assertion"
    assert effective_rows[0]["relation_id"] is None
    assert effective_rows[0]["assertion_id"].startswith("ska_")
    assert [row["text"] for row in base_rows] == ["Base programme context."]
    assert base_rows[0]["row_kind"] == "base_text_relation"
    assert default_rows == base_rows
    assert diff_result["success"] is True
    diff = diff_result["diff"]
    assert diff["promotion_ready_assertion_ids"] == ["theory_assert_1"]
    assert diff["already_canonical_assertion_ids"] == ["theory_assert_2"]
    assert diff["assertions"][0]["diff_status"] == "promotion_ready"
    assert diff["assertions"][1]["diff_status"] == "already_canonical"


def test_actor_effective_language_filter_precedes_scoped_limit(monkeypatch):
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import scoped_assertion_service
    from src.backend.services import text_value_service

    collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    monkeypatch.setattr(
        scoped_assertion_service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        scoped_assertion_service,
        "can_access_concept",
        lambda _concept_id: True,
    )
    monkeypatch.setattr(
        text_value_service.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        text_value_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )

    en_receipt = scoped_assertion_service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="English programme context.",
        language="en-NZ",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
        namespace="#V#michael_witbrock@trusted_org",
        canonical_publication=False,
    )
    fr_receipt = scoped_assertion_service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="Contexte du programme.",
        language="fr",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
        namespace="#V#michael_witbrock@trusted_org",
        canonical_publication=False,
    )
    collection.update_one(
        {"assertion_id": en_receipt["assertion_id"]},
        {"$set": {"updated_at": datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)}},
    )
    collection.update_one(
        {"assertion_id": fr_receipt["assertion_id"]},
        {"$set": {"updated_at": datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)}},
    )

    with override_current_actor("#V#michael_witbrock", "#V#trusted_org"):
        rows = text_value_service.get_texts_for_concept(
            "#V#gillian_dobbie",
            predicate="hasDescription",
            lang="en-NZ",
            limit=1,
            context_view="actor_effective",
        )

    assert [row["text"] for row in rows] == ["English programme context."]


def test_actor_effective_multi_concept_read_uses_per_subject_pages(monkeypatch):
    from src.backend.services import scoped_assertion_service
    from src.backend.services import text_value_service

    concept_ids = ["#V#assertion_rich", "#V#otherwise_starved"]
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        text_value_service.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        text_value_service,
        "filter_accessible_concept_ids",
        lambda candidate_ids: set(candidate_ids),
    )

    def _page(**kwargs):
        observed.append(dict(kwargs))
        items = [
            {
                "assertion_id": f"ska_{index}",
                "subject_concept_id": concept_id,
                "predicate": "hasDescription",
                "object_kind": "text",
                "object_text": {
                    "text": f"Scoped text {index}",
                    "language": "en-NZ",
                },
                "canonical_publication": False,
            }
            for index, concept_id in enumerate(concept_ids)
        ]
        return {
            "items": items,
            "returned": 2,
            "truncated": False,
            "visibility_filtered": False,
            "counts_are_lower_bounds": False,
            "per_subject": {
                concept_id: {
                    "returned": 1,
                    "limit": 1,
                    "has_more": False,
                    "visibility_filtered": False,
                    "counts_are_lower_bounds": False,
                }
                for concept_id in concept_ids
            },
        }

    monkeypatch.setattr(
        scoped_assertion_service,
        "list_visible_scoped_assertions_page",
        _page,
    )
    query_metadata: dict[str, object] = {}

    rows = text_value_service.get_texts_for_concepts(
        concept_ids,
        predicate="hasDescription",
        limit_per_concept=1,
        context_view="actor_effective",
        query_metadata=query_metadata,
    )

    assert [row["text"] for row in rows[concept_ids[0]]] == ["Scoped text 0"]
    assert [row["text"] for row in rows[concept_ids[1]]] == ["Scoped text 1"]
    assert observed[0]["subject_concept_ids"] == concept_ids
    assert observed[0]["limit_per_subject"] == 1
    assert query_metadata["scoped_query_truncated"] is False


def test_actor_effective_summary_keeps_relation_and_assertion_ids_distinct(
    monkeypatch,
):
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import scoped_assertion_service
    from src.backend.services import text_value_service

    relation_id = ObjectId()
    text_value_id = ObjectId()
    base_updated_at = datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc)
    scoped_updated_at = "2026-07-29T12:00:00+02:00"
    collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    monkeypatch.setattr(
        scoped_assertion_service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        scoped_assertion_service,
        "can_access_concept",
        lambda _concept_id: True,
    )
    monkeypatch.setattr(
        text_value_service,
        "can_access_concept",
        lambda _concept_id: True,
    )
    monkeypatch.setattr(
        text_value_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        text_value_service.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {
                "_id": relation_id,
                "subject_concept_id": "#V#gillian_dobbie",
                "predicate": "hasDescription",
                "object_text_id": text_value_id,
                "updated_at": base_updated_at,
            }
        ],
    )
    monkeypatch.setattr(
        text_value_service.TextValuesRepository,
        "find",
        lambda *_args, **_kwargs: [
            {
                "_id": text_value_id,
                "lang": "en-NZ",
            }
        ],
    )
    receipt = scoped_assertion_service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="Scoped programme context.",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
        namespace="#V#michael_witbrock@trusted_org",
        canonical_publication=False,
    )
    collection.update_one(
        {"assertion_id": receipt["assertion_id"]},
        {"$set": {"updated_at": scoped_updated_at}},
    )

    with override_current_actor("#V#michael_witbrock", "#V#trusted_org"):
        summary = text_value_service.get_text_relations_summary(
            "#V#gillian_dobbie",
            predicates=["hasDescription"],
            context_view="actor_effective",
        )
        recent_rows = text_value_service.get_texts_for_concept(
            "#V#gillian_dobbie",
            predicate="hasDescription",
            limit=2,
            recent_first=True,
            context_view="actor_effective",
        )

    assert summary["context_view"] == "actor_effective"
    assert summary["scoped_query_truncated"] is False
    assert summary["counts_are_lower_bounds"] is False
    assert summary["groups_found"] == 1
    group = summary["groups"][0]
    assert group["relation_ids"] == [str(relation_id)]
    assert group["assertion_ids"] == [receipt["assertion_id"]]
    assert group["latest_row_kind"] == "base_text_relation"
    assert group["latest_relation_id"] == str(relation_id)
    assert group["latest_assertion_id"] is None
    assert group["latest_updated_at"] == base_updated_at.isoformat()
    assert [row["row_kind"] for row in recent_rows] == [
        "base_text_relation",
        "scoped_assertion",
    ]


def test_scoped_concept_hit_does_not_masquerade_as_relation_id(monkeypatch):
    from src.backend.services import concept_relation_service
    from src.backend.services import scoped_assertion_service

    monkeypatch.setattr(
        concept_relation_service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        concept_relation_service,
        "query_relationship_extent_index",
        lambda **_kwargs: ([], 0),
    )
    monkeypatch.setattr(
        concept_relation_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        scoped_assertion_service,
        "list_visible_scoped_assertions_page",
        lambda **_kwargs: {
            "items": [
                {
                    "assertion_id": "ska_123",
                    "subject_concept_id": "#V#gillian_dobbie",
                    "predicate": "#V#co_directs",
                    "object_kind": "concept",
                    "object_concept_id": "#V#research_programme",
                    "scope": {"mode": "user"},
                    "provenance": {"turn_id": "turn-1"},
                },
                {
                    "assertion_id": "ska_456",
                    "subject_concept_id": "#V#gillian_dobbie",
                    "predicate": "#V#co_directs",
                    "object_kind": "concept",
                    "object_concept_id": "#V#research_programme",
                    "scope": {"mode": "organisation"},
                    "provenance": {"turn_id": "turn-2"},
                },
            ],
            "has_more": False,
            "counts_are_lower_bounds": False,
        },
    )

    base_payload = concept_relation_service.find_relations_with_argument(
        "#V#gillian_dobbie",
        predicate_filter=["#V#co_directs"],
        relation_kind="binary",
        include_concept_preview=False,
    )
    payload = concept_relation_service.find_relations_with_argument(
        "#V#gillian_dobbie",
        predicate_filter=["#V#co_directs"],
        relation_kind="binary",
        include_concept_preview=False,
        context_view="actor_effective",
    )

    assert base_payload["context_view"] == "base_publication"
    assert base_payload["total_hits"] == 0
    assert payload["context_view"] == "actor_effective"
    assert payload["total_hits"] == 2
    metadata = [hit["relation_metadata"] for hit in payload["hits"]]
    assert {item["assertion_id"] for item in metadata} == {"ska_123", "ska_456"}
    assert all(item["relation_id"] is None for item in metadata)
    assert all(item["row_kind"] == "scoped_assertion" for item in metadata)


def test_relation_lookup_uses_actor_effective_text_without_fabricated_id(
    monkeypatch,
):
    from src.backend.services import concept_relation_service

    observed: list[dict[str, object]] = []

    def _get_texts_for_concepts(concept_ids, **kwargs):
        observed.append({"concept_ids": list(concept_ids), **kwargs})
        return {
            "#V#gillian_dobbie": [
                {
                    "subject_concept_id": "#V#gillian_dobbie",
                    "predicate": "hasDescription",
                    "text": "Scoped programme context.",
                    "lang": "en-NZ",
                    "relation_id": None,
                    "assertion_id": "ska_123",
                    "row_kind": "scoped_assertion",
                    "canonical_publication": False,
                    "storage_surface": "scoped_knowledge_assertions",
                }
            ]
        }

    monkeypatch.setattr(
        concept_relation_service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        concept_relation_service,
        "get_texts_for_concepts",
        _get_texts_for_concepts,
    )

    payload = concept_relation_service.find_relations_with_argument(
        "#V#gillian_dobbie",
        argument_index="subject",
        relation_kind="text",
        include_concept_preview=False,
        context_view="actor_effective",
    )

    assert observed == [
        {
            "concept_ids": ["#V#gillian_dobbie"],
            "limit_per_concept": 501,
            "context_view": "actor_effective",
            "query_metadata": {},
        }
    ]
    assert payload["total_hits"] == 1
    assert payload.get("total_hits_is_lower_bound", False) is False
    metadata = payload["hits"][0]["relation_metadata"]
    assert metadata["relation_id"] is None
    assert metadata["assertion_id"] == "ska_123"
    assert metadata["row_kind"] == "scoped_assertion"


def test_relation_lookup_returns_explicitly_linked_standalone_text_alongside_relations(
    monkeypatch,
):
    from src.backend.services import concept_relation_service
    from src.backend.services import scoped_assertion_service

    monkeypatch.setattr(
        concept_relation_service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        concept_relation_service,
        "get_texts_for_concepts",
        lambda concept_ids, **_kwargs: {concept_id: [] for concept_id in concept_ids},
    )
    monkeypatch.setattr(
        concept_relation_service.TextValuesRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )
    observed = []

    def _list_linked(**kwargs):
        observed.append(kwargs)
        return {
            "items": [
                {
                    "assertion_id": "ska_raw",
                    "assertion_revision": 2,
                    "assertion_form": "standalone_text",
                    "object_kind": "text",
                    "object_text": {
                        "text": "Susan hosted gatherings in Svalbard.",
                        "language": "en-NZ",
                    },
                    "concept_links": [
                        {
                            "link_id": "skl_susan",
                            "concept_id": "#V#susan",
                            "role": "about",
                            "status": "active",
                            "method": "explicit_user_link",
                            "spans": [{"start": 0, "end": 5}],
                        }
                    ],
                    "assertion_context": {
                        "context_id": "intake:user:#V#member"
                    },
                    "scope": {"mode": "user"},
                    "provenance": {"turn_id": "turn-1"},
                }
            ],
            "has_more": False,
            "counts_are_lower_bounds": False,
        }

    monkeypatch.setattr(
        scoped_assertion_service,
        "list_visible_scoped_assertions_page",
        _list_linked,
    )
    payload = concept_relation_service.find_relations_with_argument(
        "#V#susan",
        argument_index="any",
        relation_kind="text",
        include_text_snippets=True,
        include_concept_preview=False,
        context_view="actor_effective",
    )

    assert observed == [
        {
            "argument_concept_id": "#V#susan",
            "object_kind": "text",
            "assertion_form": "standalone_text",
            "limit": 501,
        }
    ]
    assert payload["total_hits"] == 1
    hit = payload["hits"][0]
    assert hit["relation_kind"] == "text"
    assert hit["target_value"] == "Susan hosted gatherings in Svalbard."
    assert hit["predicate_concept_id"] is None
    metadata = hit["relation_metadata"]
    assert metadata["relation_id"] is None
    assert metadata["assertion_id"] == "ska_raw"
    assert metadata["row_kind"] == "text_assertion"
    assert metadata["link_id"] == "skl_susan"
    assert metadata["link_role"] == "about"
    assert metadata["match_type"] == "explicit_concept_link"
    assert metadata["canonical_publication"] is False


def test_relation_lookup_propagates_scoped_page_completeness(monkeypatch):
    from src.backend.services import concept_relation_service
    from src.backend.services import scoped_assertion_service

    monkeypatch.setattr(
        concept_relation_service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        concept_relation_service,
        "query_relationship_extent_index",
        lambda **_kwargs: ([], 0),
    )
    monkeypatch.setattr(
        concept_relation_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        scoped_assertion_service,
        "list_visible_scoped_assertions_page",
        lambda **_kwargs: {
            "items": [],
            "has_more": True,
            "counts_are_lower_bounds": True,
        },
    )

    payload = concept_relation_service.find_relations_with_argument(
        "#V#subject",
        relation_kind="binary",
        include_concept_preview=False,
        context_view="actor_effective",
    )

    assert payload["total_hits_is_lower_bound"] is True
    assert (
        payload["relation_query_diagnostics"][
            "scoped_concept_query_truncated"
        ]
        is True
    )


def test_relation_lookup_propagates_actor_text_query_completeness(
    monkeypatch,
):
    from src.backend.services import concept_relation_service

    monkeypatch.setattr(
        concept_relation_service,
        "_load_accessible_relation_subject_document",
        lambda *_args, **_kwargs: None,
    )

    def _get_texts(concept_ids, **kwargs):
        kwargs["query_metadata"]["scoped_counts_are_lower_bounds"] = True
        return {concept_id: [] for concept_id in concept_ids}

    monkeypatch.setattr(
        concept_relation_service,
        "get_texts_for_concepts",
        _get_texts,
    )

    payload = concept_relation_service.find_relations_with_argument(
        "#V#subject",
        argument_index="subject",
        relation_kind="text",
        include_concept_preview=False,
        context_view="actor_effective",
    )

    assert payload["total_hits_is_lower_bound"] is True
    assert (
        payload["relation_query_diagnostics"][
            "actor_effective_text_query_truncated"
        ]
        is True
    )


def test_actor_facing_text_tools_select_the_effective_view(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import text_value_service

    observed: list[tuple[str, str]] = []

    def _get_texts_for_concept(**kwargs):
        observed.append(("relations", kwargs["context_view"]))
        return []

    def _get_text_relations_summary(_concept_id, **kwargs):
        observed.append(("summary", kwargs["context_view"]))
        return {
            "success": True,
            "concept_id": "#V#gillian_dobbie",
            "groups": [],
            "groups_found": 0,
            "total_relations_scanned": 0,
            "max_relation_ids_per_group": 25,
        }

    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concept",
        _get_texts_for_concept,
    )
    monkeypatch.setattr(
        text_value_service,
        "get_text_relations_summary",
        _get_text_relations_summary,
    )

    with override_current_actor("#V#trusted_user", "#V#trusted_org"):
        catalogue._get_text_relations(concept_id="#V#gillian_dobbie")
        catalogue._get_text_relations_summary(concept_id="#V#gillian_dobbie")

    assert observed == [
        ("relations", "actor_effective"),
        ("summary", "actor_effective"),
    ]


def test_anonymous_stdio_text_tools_select_base_publication(monkeypatch):
    from src.backend.mcp_server import process_guard

    monkeypatch.setattr(
        process_guard,
        "activate_mcp_helper_lifecycle",
        lambda *_args, **_kwargs: {},
    )
    from src.backend.mcp_server import mcp_stdio_server
    from src.backend.security.access_control import (
        should_enforce_access_control,
    )

    observed: list[tuple[str, str, bool]] = []

    def _get_texts_for_concept(**kwargs):
        observed.append(
            (
                "relations",
                kwargs["context_view"],
                should_enforce_access_control(),
            )
        )
        return []

    def _get_text_relations_summary(_concept_id, **kwargs):
        observed.append(
            (
                "summary",
                kwargs["context_view"],
                should_enforce_access_control(),
            )
        )
        return {
            "success": True,
            "concept_id": "#V#gillian_dobbie",
            "context_view": kwargs["context_view"],
            "groups": [],
            "groups_found": 0,
            "total_relations_scanned": 0,
            "max_relation_ids_per_group": 25,
        }

    monkeypatch.setattr(
        mcp_stdio_server,
        "get_texts_for_concept",
        _get_texts_for_concept,
    )
    monkeypatch.setattr(
        mcp_stdio_server,
        "get_text_relations_summary",
        _get_text_relations_summary,
    )

    async def _invoke():
        relations = await mcp_stdio_server._handle_get_text_relations(
            {"concept_id": "#V#gillian_dobbie"}
        )
        summary = await mcp_stdio_server._handle_get_text_relations_summary(
            {"concept_id": "#V#gillian_dobbie"}
        )
        return json.loads(relations[0].text), json.loads(summary[0].text)

    relations_payload, summary_payload = asyncio.run(_invoke())

    assert observed == [
        ("relations", "base_publication", True),
        ("summary", "base_publication", True),
    ]
    assert relations_payload["context_view"] == "base_publication"
    assert summary_payload["context_view"] == "base_publication"


def test_anonymous_stdio_concept_reads_enforce_global_visibility(monkeypatch):
    from src.backend.mcp_server import process_guard

    monkeypatch.setattr(
        process_guard,
        "activate_mcp_helper_lifecycle",
        lambda *_args, **_kwargs: {},
    )
    from src.backend.mcp_server import mcp_stdio_server
    from src.backend.security.access_control import (
        should_enforce_access_control,
    )

    observed: list[tuple[str, bool]] = []

    def _get_concept(concept_id):
        observed.append(("fetch", should_enforce_access_control()))
        return {"concept_id": concept_id, "relationships": {}}

    def _find_relations(**_kwargs):
        observed.append(("relations", should_enforce_access_control()))
        return {"success": True, "relations": []}

    monkeypatch.setattr(
        mcp_stdio_server,
        "get_concept_by_concept_id",
        _get_concept,
    )
    monkeypatch.setattr(
        mcp_stdio_server,
        "enrich_concept_with_text_relations",
        lambda concept: concept,
    )
    monkeypatch.setattr(
        mcp_stdio_server,
        "find_relations_with_argument",
        _find_relations,
    )

    async def _invoke():
        fetched = await mcp_stdio_server._handle_fetch_concept(
            {"concept_id": "#V#globally_visible"}
        )
        related = await mcp_stdio_server._handle_find_relations_with_argument(
            {"concept_id": "#V#globally_visible"}
        )
        return json.loads(fetched[0].text), json.loads(related[0].text)

    fetched_payload, related_payload = asyncio.run(_invoke())

    assert observed == [("fetch", True), ("relations", True)]
    assert fetched_payload["concept_id"] == "#V#globally_visible"
    assert related_payload["success"] is True


def test_stdio_rag_search_rejects_untrusted_namespace_before_backend(monkeypatch):
    from src.backend.mcp_server import process_guard

    monkeypatch.setattr(
        process_guard,
        "activate_mcp_helper_lifecycle",
        lambda *_args, **_kwargs: {},
    )
    from src.backend.mcp_server import mcp_stdio_server
    from src.backend.services import rag_service

    def _unexpected_backend_access():
        raise AssertionError("an untrusted namespace must not reach RAG")

    monkeypatch.setattr(
        rag_service,
        "get_rag_service",
        _unexpected_backend_access,
    )

    async def _invoke():
        result = await mcp_stdio_server.call_tool(
            "search_knowledge_base",
            {
                "query": "private programme",
                "namespace": "#V#victim@private_org",
                "user_concept_id": "#V#victim",
                "organisation_concept_id": "#V#private_org",
            }
        )
        return json.loads(result[0].text)

    payload = asyncio.run(_invoke())

    assert payload["success"] is False
    assert payload["error"] == "namespace_required"
    assert payload["namespace_source"] == "untrusted_payload_rejected"
    assert (
        payload["namespace_resolution_note"]
        == "authenticated_actor_context_required"
    )


def test_stdio_rag_search_uses_server_bound_actor(monkeypatch):
    from src.backend.mcp_server import process_guard

    monkeypatch.setattr(
        process_guard,
        "activate_mcp_helper_lifecycle",
        lambda *_args, **_kwargs: {},
    )
    from src.backend.mcp_server import mcp_stdio_server
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import rag_service

    observed: dict[str, object] = {}

    class _StubRAG:
        def query(self, **kwargs):
            observed.update(kwargs)
            return [
                {
                    "id": "doc-1",
                    "text": "Authorised programme context.",
                    "metadata": {},
                    "score": 0.9,
                }
            ]

    monkeypatch.setattr(rag_service, "get_rag_service", lambda: _StubRAG())

    async def _invoke():
        result = await mcp_stdio_server.call_tool(
            "search_knowledge_base",
            {"query": "programme context"}
        )
        return json.loads(result[0].text)

    with override_current_actor("#V#trusted_user", "#V#trusted_org"):
        payload = asyncio.run(_invoke())

    assert payload["success"] is True
    assert payload["namespace"] == "#V#trusted_user@trusted_org"
    assert payload["namespace_source"] == "trusted_actor_context"
    assert observed["namespace"] == "#V#trusted_user@trusted_org"
    assert observed["permissions_context"] == {
        "user_id": "#V#trusted_user",
        "organisation_concept_id": "#V#trusted_org",
    }
