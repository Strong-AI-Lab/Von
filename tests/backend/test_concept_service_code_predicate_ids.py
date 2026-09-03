from __future__ import annotations

from copy import deepcopy

from src.backend.services import concept_service


class _InsertResult:
    inserted_id = "created-id"


class _Collection:
    def __init__(self) -> None:
        self.document: dict | None = None

    def insert_one(self, document: dict) -> _InsertResult:
        self.document = deepcopy(document)
        self.document["_id"] = _InsertResult.inserted_id
        return _InsertResult()

    def find_one(self, query: dict) -> dict | None:
        if self.document is None or query.get("_id") != _InsertResult.inserted_id:
            return None
        return deepcopy(self.document)


def test_create_concept_preserves_exact_registered_code_predicate_id(
    monkeypatch,
) -> None:
    collection = _Collection()
    monkeypatch.setattr(
        concept_service.ConceptsRepository, "collection", lambda: collection
    )
    monkeypatch.setattr(
        concept_service,
        "get_event_workflow_integration_enabled",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        concept_service, "_invalidate_concept_mutation_caches", lambda: None
    )

    created = concept_service.create_concept(
        name="Has Von login email",
        concept_id="#V#hasVonLoginEmail",
        parent_concept_ids=["#V#binary_text_predicate"],
        create_as_instance=True,
        visibility_scope_mode="global_general",
        defer_text_relations=True,
        maintain_relationship_inverses=False,
    )

    assert created["concept_id"] == "#V#hasVonLoginEmail"
    assert collection.document is not None
    assert collection.document["concept_id"] == "#V#hasVonLoginEmail"


def test_create_concept_still_canonicalises_unregistered_mixed_case_id(
    monkeypatch,
) -> None:
    collection = _Collection()
    monkeypatch.setattr(
        concept_service.ConceptsRepository, "collection", lambda: collection
    )
    monkeypatch.setattr(
        concept_service,
        "get_event_workflow_integration_enabled",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        concept_service, "_invalidate_concept_mutation_caches", lambda: None
    )

    created = concept_service.create_concept(
        name="Ordinary mixed case concept",
        concept_id="#V#OrdinaryMixedCaseConcept",
        parent_concept_ids=["#V#person"],
        create_as_instance=True,
        visibility_scope_mode="global_general",
        defer_text_relations=True,
        maintain_relationship_inverses=False,
    )

    assert created["concept_id"] == "#V#ordinarymixedcaseconcept"
    assert collection.document is not None
    assert collection.document["concept_id"] == "#V#ordinarymixedcaseconcept"
