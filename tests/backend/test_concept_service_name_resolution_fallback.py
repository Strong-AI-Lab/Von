from __future__ import annotations


def test_get_concept_by_concept_id_falls_back_to_name_resolution(monkeypatch) -> None:
    from src.backend.services import concept_rename_service
    from src.backend.services import concept_resolution_service
    from src.backend.services import concept_service

    class _FakeCollection:
        def find_one(self, query):
            concept_id = query.get("concept_id")
            if concept_id == "#V#specific_to_organisation":
                return {
                    "_id": "fake-id",
                    "concept_id": "#V#specific_to_organisation",
                    "relationships": {"is_an_instance_of": ["#V#predicate"]},
                }
            return None

    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "collection",
        staticmethod(lambda: _FakeCollection()),
    )
    monkeypatch.setattr(
        concept_rename_service,
        "resolve_concept_by_alias",
        lambda _alias: None,
    )
    monkeypatch.setattr(
        concept_resolution_service,
        "resolve_concept_by_name",
        lambda **_kwargs: {
            "success": True,
            "status": "resolved",
            "resolved_concept_id": "#V#specific_to_organisation",
        },
    )

    result = concept_service.get_concept_by_concept_id("#V#specific_to_org")
    assert result is not None
    assert result["concept_id"] == "#V#specific_to_organisation"

