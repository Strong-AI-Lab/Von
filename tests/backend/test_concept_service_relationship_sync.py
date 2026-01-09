from __future__ import annotations

import src.backend.services.concept_service as concept_service


def test_update_concept_reconciles_relationships(monkeypatch):
    class FakeCollection:
        def find_one(self, *_args, **_kwargs):
            return {
                "concept_id": "#V#parent",
                "relationships": {"has_subtype": []},
            }

        def find_one_and_update(self, *_args, **_kwargs):
            return {
                "concept_id": "#V#parent",
                "relationships": {"has_subtype": ["#V#child"]},
            }

    fake_collection = FakeCollection()
    reconcile_calls = []

    monkeypatch.setattr(
        concept_service.ConceptsRepository, "collection", lambda: fake_collection
    )

    def fake_reconcile(concept_id, relationships, *, previous_relationships=None):
        reconcile_calls.append(
            (concept_id, relationships, previous_relationships or {})
        )

    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "reconcile_relationships",
        staticmethod(fake_reconcile),
    )

    concept_service.update_concept(
        "#V#parent", {"relationships": {"has_subtype": ["#V#child"]}}
    )

    assert reconcile_calls == [
        ("#V#parent", {"has_subtype": ["#V#child"]}, {"has_subtype": []})
    ]
