from __future__ import annotations


def test_materialise_company_representation_for_authority_candidate_persists_core_effects(
    monkeypatch,
):
    from src.backend.services.company_file_representation_service import (
        materialise_company_representation_for_file_copy,
    )

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        lambda **_kwargs: {"results": []},
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.create_concept",
        lambda **_kwargs: {"success": True},
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.update_concept",
        lambda *_args, **_kwargs: {"success": True},
    )

    relation_writes: list[dict[str, object]] = []

    def _fake_upsert_text_for_concept(**kwargs):
        relation_writes.append(dict(kwargs))
        return {
            "relation_id": f"rel-{len(relation_writes)}",
            "relation_created": True,
            "predicate": kwargs.get("predicate"),
        }

    relationship_writes: list[dict[str, object]] = []

    def _fake_add_relationship(**kwargs):
        relationship_writes.append(dict(kwargs))
        return {"success": True, "forward_modified": True}

    monkeypatch.setattr(
        "src.backend.services.file_copy_entity_representation_support.upsert_text_for_concept",
        _fake_upsert_text_for_concept,
    )
    monkeypatch.setattr(
        "src.backend.services.company_file_representation_service.add_relationship",
        _fake_add_relationship,
    )
    monkeypatch.setattr(
        "src.backend.services.company_file_representation_service.infer_file_copy_entity_representation_candidates",
        lambda **_kwargs: (
            {
                "schema_version": "file_copy_entity_representation_interpretation.v1",
                "company_candidate": {
                    "applicable": True,
                    "representation_mode": "web_page",
                    "company_name": "Example Labs Ltd",
                    "aliases": ["Example Labs"],
                    "urls": ["https://examplelabs.ai/about"],
                    "descriptors": ["Builds safe AI products."],
                },
            },
            {"status": "ok"},
        ),
    )

    result = materialise_company_representation_for_file_copy(
        user_concept_id="#V#user_test",
        file_copy_concept_id="#V#uploaded_file_copy_company_1",
        extracted_text="Company profile content",
        original_filename="example-company-webpage.txt",
        interpretation={"description": "Extracted from the company about page."},
    )

    assert result["attempted"] is True
    assert result["verified"] is True
    assert result["success"] is True
    assert result["reason"] == "company_representation_verified"
    assert result["representation_mode"] == "web_page"
    assert result["company_name"] == "Example Labs Ltd"
    assert "https://examplelabs.ai/about" in result["urls"]
    assert result["company_concept_id"].startswith("#V#company_example_labs_ltd_")

    predicates = [row["predicate"] for row in relation_writes]
    assert "hasName" in predicates
    assert "#V#has_url" in predicates
    assert "#V#hasNote" in predicates

    assert relationship_writes == [
        {
            "source_id": "#V#uploaded_file_copy_company_1",
            "predicate": "#V#documentary_evidence_for",
            "target": result["company_concept_id"],
        }
    ]


def test_materialise_company_representation_fails_when_url_unresolved(monkeypatch):
    from src.backend.services.company_file_representation_service import (
        materialise_company_representation_for_file_copy,
    )

    monkeypatch.setattr(
        "src.backend.services.company_file_representation_service.infer_file_copy_entity_representation_candidates",
        lambda **_kwargs: (
            {
                "schema_version": "file_copy_entity_representation_interpretation.v1",
                "company_candidate": {
                    "applicable": True,
                    "representation_mode": "web_page",
                    "company_name": "Example Labs Ltd",
                    "urls": [],
                },
            },
            {"status": "ok"},
        ),
    )

    result = materialise_company_representation_for_file_copy(
        user_concept_id="#V#user_test",
        file_copy_concept_id="#V#uploaded_file_copy_company_2",
        extracted_text="Company profile content",
        original_filename="company-website-profile.txt",
    )

    assert result["attempted"] is True
    assert result["verified"] is False
    assert result["success"] is False
    assert result["reason"] == "company_url_unresolved"
    assert result["company_concept_id"] is None


def test_materialise_company_representation_is_not_attempted_for_unrelated_text(
    monkeypatch,
):
    from src.backend.services.company_file_representation_service import (
        materialise_company_representation_for_file_copy,
    )

    monkeypatch.setattr(
        "src.backend.services.company_file_representation_service.infer_file_copy_entity_representation_candidates",
        lambda **_kwargs: (
            {
                "schema_version": "file_copy_entity_representation_interpretation.v1",
                "company_candidate": {"applicable": False},
            },
            {"status": "no_candidates"},
        ),
    )

    result = materialise_company_representation_for_file_copy(
        user_concept_id="#V#user_test",
        file_copy_concept_id="#V#uploaded_file_copy_generic_1",
        extracted_text="This is an ecosystem planning document about transport policy.",
        original_filename="planning-notes.txt",
    )

    assert result["attempted"] is False
    assert result["verified"] is False
    assert result["success"] is False
    assert result["reason"] == "not_company_artefact"
