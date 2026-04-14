from __future__ import annotations

from src.backend.services import task_ontology_service
from src.backend.utils.concept_id_utils import canonicalise_vontology_concept_id


def test_ensure_task_ontology_uses_canonical_predicate_ids(monkeypatch) -> None:
    existing_ids = {
        "#V#thing",
        "#V#predicate",
        "#V#binary_predicate",
        "#V#task_specification",
    }

    def _fake_get_concept(concept_id: str):
        return {"concept_id": concept_id} if concept_id in existing_ids else None

    def _fake_create_concept(**kwargs):
        raw_concept_id = kwargs.get("concept_id")
        canonical_id = canonicalise_vontology_concept_id(raw_concept_id)
        assert canonical_id is not None
        existing_ids.add(canonical_id)
        return {"concept_id": canonical_id}

    def _fake_mutate_relationship_edge(
        *,
        source_id: str,
        kind: str,
        target_id: str,
        action: str,
        maintain_inverse: bool = True,
    ) -> bool:
        del kind, action, maintain_inverse
        if source_id not in existing_ids:
            raise ValueError(
                f"Concept '{source_id}' not found while normalising relationships"
            )
        if target_id not in existing_ids:
            raise ValueError(
                f"Concept '{target_id}' not found while normalising relationships"
            )
        return True

    monkeypatch.setattr(task_ontology_service, "_task_ontology_ensured", False)
    monkeypatch.setattr(
        task_ontology_service.concept_service,
        "get_concept_by_concept_id",
        _fake_get_concept,
    )
    monkeypatch.setattr(
        task_ontology_service.concept_service,
        "create_concept",
        _fake_create_concept,
    )
    monkeypatch.setattr(
        task_ontology_service.ConceptsRepository,
        "mutate_relationship_edge",
        _fake_mutate_relationship_edge,
    )

    result = task_ontology_service.ensure_task_ontology(force=True)

    assert result["success"] is True
    assert result["errors"] == []
    assert task_ontology_service.PREDICATE_HAS_TASK_SOURCE in result["created_concept_ids"]
    assert task_ontology_service.PREDICATE_REPORTS_TO in result["created_concept_ids"]
