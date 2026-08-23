from __future__ import annotations

from src.backend.services import concept_type_closure_service as service


def test_load_type_closure_batches_each_depth_and_stops_cycles(monkeypatch) -> None:
    documents = {
        "#V#research_fellow": {
            "concept_id": "#V#research_fellow",
            "relationships": {"is_a_type_of": ["#V#researcher", "#V#person"]},
        },
        "#V#researcher": {
            "concept_id": "#V#researcher",
            "relationships": {"is_a_type_of": ["#V#person"]},
        },
        "#V#person": {
            "concept_id": "#V#person",
            "relationships": {"is_a_type_of": ["#V#thing"]},
        },
        "#V#thing": {
            "concept_id": "#V#thing",
            "relationships": {"is_a_type_of": ["#V#research_fellow"]},
        },
    }
    calls: list[list[str]] = []

    def _find(query, *_args, **_kwargs):
        ids = list(query["concept_id"]["$in"])
        calls.append(ids)
        return [documents[concept_id] for concept_id in ids if concept_id in documents]

    monkeypatch.setattr(service.ConceptsRepository, "find", _find)

    result = service.load_type_closure(["#V#research_fellow"])

    assert result["ordered_type_ids"] == [
        "#V#research_fellow",
        "#V#researcher",
        "#V#person",
        "#V#thing",
    ]
    assert calls == [
        ["#V#research_fellow"],
        ["#V#researcher", "#V#person"],
        ["#V#thing"],
    ]
    assert result["truncated"] is False


def test_load_type_closure_reports_bounded_truncation(monkeypatch) -> None:
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda query, *_args, **_kwargs: [
            {
                "concept_id": query["concept_id"]["$in"][0],
                "relationships": {"is_a_type_of": ["#V#next"]},
            }
        ],
    )

    result = service.load_type_closure(["#V#start"], max_depth=1)

    assert result["ordered_type_ids"] == ["#V#start", "#V#next"]
    assert result["truncated"] is True
