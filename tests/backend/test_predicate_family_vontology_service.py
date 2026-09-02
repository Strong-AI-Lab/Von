from __future__ import annotations

from typing import Any

from src.backend.services import predicate_family_vontology_service as service


def test_expand_relation_predicate_family_uses_inverse_and_indexed_family_schema(
    monkeypatch,
) -> None:
    queries: list[dict[str, Any]] = []

    def fake_find(query, projection=None):
        queries.append({"query": query, "projection": projection})
        concept_ids = query.get("concept_id", {}).get("$in", [])
        if concept_ids == ["#V#direct_relation"]:
            return [
                {
                    "concept_id": "#V#direct_relation",
                    "relationships": {
                        "#V#predicate_is_inverse_of_predicate": ["#V#inverse_relation"]
                    },
                }
            ]
        assert concept_ids == ["#V#represented_relation_family"]
        return [
            {
                "concept_id": "#V#represented_relation_family",
                "relationships": {
                    "#V#has_member_predicate": [
                        "#V#direct_relation",
                        "#V#inverse_relation",
                        "#V#focal_role",
                        "#V#related_role",
                    ],
                    "#V#has_focal_role_predicate": ["#V#focal_role"],
                    "#V#has_related_role_predicate": ["#V#related_role"],
                    "#V#has_reified_relation_type": ["#V#relation_event"],
                },
            }
        ]

    index_queries: list[dict[str, Any]] = []

    def fake_query_relationship_extent_index(**kwargs):
        captured = dict(kwargs)
        captured["target_values"] = list(kwargs.get("target_values") or [])
        index_queries.append(captured)
        if kwargs["predicate_id"] == service.PREDICATE_SPECIALISATION_PREDICATE_ID:
            return [], 0
        return (
            [
                {
                    "source_concept_id": "#V#represented_relation_family",
                    "predicate_id": "#V#has_member_predicate",
                    "target_value": "#V#inverse_relation",
                }
            ],
            1,
        )

    monkeypatch.setattr(service.ConceptsRepository, "find", staticmethod(fake_find))
    monkeypatch.setattr(
        service,
        "query_relationship_extent_index",
        fake_query_relationship_extent_index,
    )

    result = service.expand_relation_predicate_family(["#V#direct_relation"])

    assert result["status"] == "expanded"
    assert result["predicate_ids"] == [
        "#V#direct_relation",
        "#V#inverse_relation",
        "#V#focal_role",
        "#V#related_role",
    ]
    assert result["family_concept_ids"] == ["#V#represented_relation_family"]
    assert result["focal_role_predicate_ids"] == ["#V#focal_role"]
    assert result["related_role_predicate_ids"] == ["#V#related_role"]
    assert result["reified_relation_type_ids"] == ["#V#relation_event"]
    assert result["relationship_extent_index_status"] == "available"
    assert result["family_discovery_source"] == "relationship_extent_index"
    assert index_queries == [
        {
            "predicate_id": "#V#predicate_specialises_predicate",
            "target_values": ["#V#direct_relation", "#V#inverse_relation"],
            "count_total": False,
            "projection": {
                "_id": 0,
                "source_concept_id": 1,
                "predicate_id": 1,
                "target_value": 1,
            },
            "limit": 128,
        },
        {
            "predicate_id": "#V#has_member_predicate",
            "target_values": ["#V#direct_relation", "#V#inverse_relation"],
            "count_total": False,
            "projection": {
                "_id": 0,
                "source_concept_id": 1,
                "predicate_id": 1,
                "target_value": 1,
            },
            "limit": 128,
        },
    ]
    assert len(queries) == 2


def test_expand_relation_predicate_family_uses_exact_fallback_when_index_unavailable(
    monkeypatch,
) -> None:
    queries: list[dict[str, Any]] = []

    def fake_find(
        query,
        projection=None,
        *,
        limit=0,
        max_time_ms=None,
    ):
        queries.append(
            {
                "query": query,
                "projection": projection,
                "limit": limit,
                "max_time_ms": max_time_ms,
            }
        )
        if "concept_id" in query:
            return [
                {
                    "concept_id": "#V#direct_relation",
                    "relationships": {"#V#inverse_predicates": ["#V#inverse_relation"]},
                }
            ]
        if f"relationships.{service.PREDICATE_SPECIALISATION_PREDICATE_ID}" in query:
            return []
        return [
            {
                "concept_id": "#V#represented_relation_family",
                "relationships": {
                    "#V#has_member_predicate": [
                        "#V#direct_relation",
                        "#V#inverse_relation",
                        "#V#role_relation",
                    ],
                    "#V#has_focal_role_predicate": ["#V#role_relation"],
                },
            }
        ]

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        staticmethod(fake_find),
    )
    monkeypatch.setattr(
        service,
        "query_relationship_extent_index",
        lambda **_kwargs: ([], -1),
    )

    result = service.expand_relation_predicate_family(["#V#direct_relation"])

    assert result["status"] == "expanded"
    assert result["predicate_ids"] == [
        "#V#direct_relation",
        "#V#inverse_relation",
        "#V#role_relation",
    ]
    assert result["family_concept_ids"] == ["#V#represented_relation_family"]
    assert result["relationship_extent_index_status"] == "unavailable"
    assert result["family_discovery_source"] == ("canonical_exact_predicate_query")
    assert queries[2]["query"] == {
        "relationships.#V#has_member_predicate": {
            "$in": ["#V#direct_relation", "#V#inverse_relation"]
        }
    }
    assert queries[2]["limit"] == 32
    assert queries[2]["max_time_ms"] is None


def test_general_predicate_query_expands_to_specialisations_but_not_reverse(
    monkeypatch,
) -> None:
    def fake_find(query, projection=None, *, limit=0):
        concept_ids = query.get("concept_id", {}).get("$in", [])
        if concept_ids:
            return [
                {"concept_id": concept_id, "relationships": {}}
                for concept_id in concept_ids
            ]
        return []

    def fake_index(**kwargs):
        predicate = kwargs["predicate_id"]
        targets = list(kwargs.get("target_values") or [])
        if predicate == service.PREDICATE_SPECIALISATION_PREDICATE_ID and targets == [
            "#V#memberOf"
        ]:
            return (
                [
                    {
                        "source_concept_id": "#V#memberOfVonOrg",
                        "predicate_id": predicate,
                        "target_value": "#V#memberOf",
                    }
                ],
                1,
            )
        return [], 0

    monkeypatch.setattr(service.ConceptsRepository, "find", staticmethod(fake_find))
    monkeypatch.setattr(service, "query_relationship_extent_index", fake_index)

    general = service.expand_relation_predicate_family(["#V#memberOf"])
    narrow = service.expand_relation_predicate_family(["#V#memberOfVonOrg"])

    assert general["predicate_ids"] == ["#V#memberOf", "#V#memberOfVonOrg"]
    assert general["entailing_specialisation_predicate_ids"] == ["#V#memberOfVonOrg"]
    assert narrow["predicate_ids"] == ["#V#memberOfVonOrg"]
    assert narrow["entailing_specialisation_predicate_ids"] == []


def test_expand_relation_predicate_family_fails_soft(monkeypatch) -> None:
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        staticmethod(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("database unavailable")
            )
        ),
    )

    result = service.expand_relation_predicate_family(["#V#direct_relation"])

    assert result["predicate_ids"] == ["#V#direct_relation"]
    assert result["status"] == "schema_read_failed"
    assert result["error_class"] == "RuntimeError"
