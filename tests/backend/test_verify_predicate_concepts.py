from __future__ import annotations

from src.backend.services.concept_service import ConceptNotFoundError


def test_verify_predicate_concepts_reports_virtual_and_missing(monkeypatch):
    from src.utilities import verify_predicate_concepts as verify

    monkeypatch.setattr(
        verify,
        "list_code_predicate_ids",
        lambda: ["#V#alpha", "#V#beta", "#V#gamma"],
    )

    def _fake_get(concept_id: str):
        if concept_id == "#V#alpha":
            return {"concept_id": concept_id, "metadata": {}}
        if concept_id == "#V#beta":
            return {"concept_id": concept_id, "metadata": {"virtual": True}}
        if concept_id == "#V#gamma":
            raise ConceptNotFoundError("missing")
        return None

    monkeypatch.setattr(verify.concept_service, "get_concept_by_concept_id", _fake_get)

    result = verify.verify_predicate_concepts()

    assert result["present"] == ["#V#alpha"]
    assert "#V#beta" in result["virtual"]
    assert "#V#beta" in result["missing"]
    assert "#V#gamma" in result["missing"]
