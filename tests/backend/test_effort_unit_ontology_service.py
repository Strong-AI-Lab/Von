from __future__ import annotations

from typing import Any

from src.backend.services import effort_unit_ontology_service as service


def test_ensure_effort_unit_ontology_creates_and_aligns(monkeypatch) -> None:
    service._effort_unit_ontology_ensured = False

    existing: dict[str, dict[str, Any]] = {
        "#V#practice": {"concept_id": "#V#practice"},
        "#V#predicate": {"concept_id": "#V#predicate"},
        "#V#binary_predicate": {"concept_id": "#V#binary_predicate"},
        "#V#project": {"concept_id": "#V#project"},
        "#V#programme": {"concept_id": "#V#programme"},
        "#V#task_specification": {"concept_id": "#V#task_specification"},
        "#V#ai_workflow": {"concept_id": "#V#ai_workflow"},
        "#V#durable_workflow": {"concept_id": "#V#durable_workflow"},
    }
    created: list[str] = []
    seen_edges: set[tuple[str, str, str, str]] = set()

    def _fake_get(concept_id: str) -> dict[str, Any] | None:
        return existing.get(concept_id)

    def _fake_create(**kwargs):
        concept_id = kwargs["concept_id"]
        existing[concept_id] = {"concept_id": concept_id, "relationships": {}}
        created.append(concept_id)
        return existing[concept_id]

    def _fake_mutate(
        source_id: str,
        kind: str,
        target_id: str,
        *,
        action: str,
        maintain_inverse: bool = True,
    ) -> bool:
        key = (source_id, kind, target_id, action)
        if key in seen_edges:
            return False
        seen_edges.add(key)
        return True

    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        _fake_get,
    )
    monkeypatch.setattr(service.concept_service, "create_concept", _fake_create)
    monkeypatch.setattr(
        service.ConceptsRepository,
        "mutate_relationship_edge",
        _fake_mutate,
    )

    report = service.ensure_effort_unit_ontology(force=True)

    assert report["success"] is True
    assert service.EFFORT_UNIT_TYPE_ID in created
    assert (
        service.PREDICATE_COMPLETION_TRIGGERS_SUCCESSOR_EFFORT_UNIT_TYPE
        in report["created_concept_ids"]
    )
    assert "#V#project" in report["alignment"]["updated_type_ids"]
    assert "#V#durable_workflow" in report["alignment"]["updated_type_ids"]


def test_resolve_successor_effort_unit_type_ids_uses_instance_and_type_links(
    monkeypatch,
) -> None:
    task_doc = {
        "concept_id": "#V#task_a",
        "relationships": {
            "is_an_instance_of": ["#V#task_specification"],
            service.PREDICATE_COMPLETION_TRIGGERS_SUCCESSOR_EFFORT_UNIT_TYPE: [
                "#V#direct_successor"
            ],
        },
    }

    def _fake_get(concept_id: str) -> dict[str, Any] | None:
        if concept_id != "#V#task_specification":
            return None
        return {
            "concept_id": concept_id,
            "relationships": {
                service.PREDICATE_COMPLETION_TRIGGERS_SUCCESSOR_EFFORT_UNIT_TYPE: [
                    "#V#direct_successor",
                    "#V#inherited_successor",
                ]
            },
        }

    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        _fake_get,
    )

    resolved = service.resolve_successor_effort_unit_type_ids(
        effort_unit_doc=task_doc,
    )

    assert resolved == ["#V#direct_successor", "#V#inherited_successor"]


def test_persist_successor_effort_unit_type_links_dedupes_and_skips_missing(
    monkeypatch,
) -> None:
    def _fake_get(concept_id: str) -> dict[str, Any] | None:
        if concept_id == "#V#successor_a":
            return {"concept_id": concept_id}
        return None

    calls: list[tuple[str, str, str, str]] = []

    def _fake_mutate(
        source_id: str,
        kind: str,
        target_id: str,
        *,
        action: str,
        maintain_inverse: bool = True,
    ) -> bool:
        calls.append((source_id, kind, target_id, action))
        return len(calls) == 1

    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        _fake_get,
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "mutate_relationship_edge",
        _fake_mutate,
    )

    report = service.persist_successor_effort_unit_type_links(
        source_effort_unit_id="#V#task_x",
        successor_type_ids=[
            "#V#successor_a",
            "#V#successor_a",
            "#V#missing_successor",
        ],
    )

    assert report["success"] is True
    assert report["linked_type_ids"] == ["#V#successor_a"]
    assert report["already_linked_type_ids"] == []
    assert report["skipped_missing_target_type_ids"] == ["#V#missing_successor"]
    assert calls == [
        (
            "#V#task_x",
            service.PREDICATE_HAS_SUCCESSOR_EFFORT_UNIT_TYPE,
            "#V#successor_a",
            "add",
        )
    ]
