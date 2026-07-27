"""Focused low-level identity allocation tests for concept creation."""

from __future__ import annotations

from src.backend.services import concept_service
from src.backend.services.concept_service import InvalidConceptDataError
from src.backend.vontology.utils_vontology import create_vontology_concept


def test_no_suffix_mode_uses_unique_insert_and_returns_race_receipt(monkeypatch):
    repository_reads: list[dict[str, object]] = []
    attempted_ids: list[str] = []

    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.ConceptsRepository.find_one",
        lambda query: repository_reads.append(dict(query)),
    )

    def _duplicate_insert(**kwargs):
        attempted_ids.append(str(kwargs["concept_id"]))
        raise InvalidConceptDataError(
            "concept creation failed due to duplicate key: simulated race"
        )

    monkeypatch.setattr(concept_service, "create_concept", _duplicate_insert)

    result = create_vontology_concept(
        parent_id="#V#abstract_object",
        new_concept_name="Race occupied identity",
        create_as_instance=True,
        allow_duplicate_instance_suffix=False,
    )

    assert attempted_ids == ["#V#race_occupied_identity"]
    assert repository_reads == []
    assert result["success"] is False
    assert result["changed"] is False
    assert result["error_code"] == "already_exists"
    assert result["existing_concept_id"] == "#V#race_occupied_identity"
    assert result["canonical_concept_id"] == "#V#race_occupied_identity"


def test_canonical_override_disables_suffixing_and_preserves_atomic_identity(
    monkeypatch,
):
    repository_reads: list[dict[str, object]] = []
    attempted_ids: list[str] = []

    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.ConceptsRepository.find_one",
        lambda query: repository_reads.append(dict(query)),
    )

    def _create(**kwargs):
        attempted_ids.append(str(kwargs["concept_id"]))
        return {"concept_id": kwargs["concept_id"]}

    monkeypatch.setattr(concept_service, "create_concept", _create)

    result = create_vontology_concept(
        parent_id="#V#scholarly_article",
        new_concept_name="A title that may change",
        create_as_instance=True,
        allow_duplicate_instance_suffix=True,
        canonical_concept_id_override="#V#paper_on_arxiv_2506_03346_1234abcd",
    )

    assert attempted_ids == ["#V#paper_on_arxiv_2506_03346_1234abcd"]
    assert repository_reads == []
    assert result["success"] is True
    assert result["canonical_concept_id"] == "#V#paper_on_arxiv_2506_03346_1234abcd"
