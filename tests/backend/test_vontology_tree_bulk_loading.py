from __future__ import annotations

from typing import Any

from src.backend.db.repositories import concepts_repository
from src.backend.security import access_control
from src.backend.vontology import code_concepts_registry, utils_vontology


def _flatten_tree(node: dict[str, Any]) -> dict[str, dict[str, Any]]:
    flattened: dict[str, dict[str, Any]] = {}

    def visit(current: dict[str, Any]) -> None:
        flattened[current["id"]] = current
        for child in current.get("children", []):
            visit(child)

    visit(node)
    return flattened


def test_tree_loader_bulk_sanitises_structure_before_fetching_display_fields(
    monkeypatch,
) -> None:
    structural_docs = [
        {"concept_id": "#V#thing", "relationships": {}},
        {
            "concept_id": "#V#parent",
            "relationships": {"is_a_type_of": ["#V#thing"]},
        },
        {
            "concept_id": "#V#child",
            "relationships": {
                "is_a_type_of": ["#V#thing", "#V#parent"],
                "most_salient_type": "#V#parent",
            },
        },
        {
            "concept_id": "#V#pure_individual",
            "relationships": {"is_an_instance_of": ["#V#person"]},
        },
        {
            "concept_id": "#V#hidden_target_erased",
            # This is the exact document shape after relationship sanitisation
            # removes an inaccessible sole instance target.  Classification
            # must continue to happen after that sanitisation.
            "relationships": {},
        },
    ]
    display_docs = {
        document["concept_id"]: {
            "concept_id": document["concept_id"],
            "name": document["concept_id"].removeprefix("#V#").replace("_", " "),
            "path": document["concept_id"],
            "_id": document["concept_id"],
        }
        for document in structural_docs
    }
    calls: list[tuple[dict, dict, dict]] = []

    def fake_find(query: dict, projection: dict, **kwargs: Any):
        calls.append((query, projection, kwargs))
        if query == {}:
            return list(structural_docs)
        requested = set(query["concept_id"]["$in"])
        return [display_docs[concept_id] for concept_id in requested]

    monkeypatch.setattr(utils_vontology.ConceptsRepository, "find", fake_find)
    monkeypatch.setattr(code_concepts_registry, "iter_code_concepts", lambda: ())

    payload = utils_vontology.get_vontology_tree()

    assert "error" not in payload
    nodes = _flatten_tree(payload["tree"][0])
    assert set(nodes) == {
        "#V#thing",
        "#V#parent",
        "#V#child",
        "#V#hidden_target_erased",
    }
    assert nodes["#V#child"] in nodes["#V#parent"]["children"]
    assert nodes["#V#hidden_target_erased"] in nodes["#V#thing"]["children"]

    assert len(calls) == 2
    structural_query, structural_projection, structural_options = calls[0]
    assert structural_query == {}
    assert "name" not in structural_projection
    assert structural_projection["relationships.is_an_instance_of"] == 1
    assert structural_options["sanitisation_batch_size"] == 20_000
    assert structural_options["cursor_batch_size"] == 10_000

    detail_query, detail_projection, detail_options = calls[1]
    assert "#V#pure_individual" not in detail_query["concept_id"]["$in"]
    assert "#V#hidden_target_erased" in detail_query["concept_id"]["$in"]
    assert detail_projection == {
        "concept_id": 1,
        "name": 1,
        "names": 1,
        "path": 1,
        "_id": 1,
    }
    assert detail_options["sanitisation_batch_size"] == 20_000
    assert detail_options["cursor_batch_size"] == 10_000


def test_access_controlled_cursor_supports_a_tree_only_bulk_batch(
    monkeypatch,
) -> None:
    documents = [
        {"concept_id": f"#V#source_{index}", "relationships": {}}
        for index in range(130)
    ]
    prewarm_sizes: list[int] = []
    monkeypatch.setattr(
        concepts_repository,
        "prewarm_concept_relationship_access",
        lambda batch: prewarm_sizes.append(len(batch)),
    )
    monkeypatch.setattr(
        concepts_repository,
        "sanitize_concept_document",
        lambda document: document,
    )

    cursor = concepts_repository._AccessControlledCursor(
        iter(documents),
        sanitisation_batch_size=100,
    )

    assert list(cursor) == documents
    assert prewarm_sizes == [100, 30]


def test_relationship_visibility_ids_are_bounded_by_count_and_bson_size(
    monkeypatch,
) -> None:
    count_batches = list(
        access_control._bounded_access_id_query_batches(
            f"#V#target_{index}" for index in range(10_001)
        )
    )
    assert [len(batch) for batch in count_batches] == [10_000, 1]

    monkeypatch.setattr(access_control, "_ACCESS_ID_QUERY_MAX_BSON_BYTES", 100)
    byte_batches = list(
        access_control._bounded_access_id_query_batches(
            ["#V#" + ("a" * 50), "#V#" + ("b" * 50)]
        )
    )
    assert [len(batch) for batch in byte_batches] == [1, 1]


def test_bulk_cursor_bounds_integrated_relationship_visibility_fanout(
    monkeypatch,
) -> None:
    document_count = 40_001

    class FakeCursor(list):
        def batch_size(self, _size: int):
            return self

    class CountingCollection:
        def __init__(self) -> None:
            self.find_calls = 0

        def find(self, query: dict, _projection: dict):
            self.find_calls += 1
            return FakeCursor(
                {
                    "concept_id": concept_id,
                    "relationships": {},
                }
                for concept_id in query["concept_id"]["$in"]
            )

    collection = CountingCollection()
    monkeypatch.setattr(
        access_control,
        "get_concepts_collection",
        lambda: collection,
    )
    sources = [
        {
            "concept_id": f"#V#source_{index}",
            "relationships": {"related_to": [f"#V#target_{index}"]},
        }
        for index in range(document_count)
    ]

    with (
        access_control.force_access_control_enforcement(),
        access_control.override_current_actor("#V#reader", None),
    ):
        result = list(
            concepts_repository._AccessControlledCursor(
                iter(sources),
                sanitisation_batch_size=20_000,
            )
        )

    assert result == sources
    # Three 20,000-source sanitisation waves become 2 + 2 + 1 bounded target
    # lookups, instead of at least 626 source waves at the generic size of 64.
    assert collection.find_calls == 5
