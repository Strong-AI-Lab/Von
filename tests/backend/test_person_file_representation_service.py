from __future__ import annotations


def test_materialise_person_representation_for_authority_candidate_persists_core_effects(
    monkeypatch,
):
    from src.backend.services.person_file_representation_service import (
        materialise_person_representation_for_file_copy,
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
        "src.backend.services.person_file_representation_service.add_relationship",
        _fake_add_relationship,
    )
    monkeypatch.setattr(
        "src.backend.services.person_file_representation_service.infer_file_copy_entity_representation_candidates",
        lambda **_kwargs: (
            {
                "schema_version": "file_copy_entity_representation_interpretation.v1",
                "person_candidate": {
                    "applicable": True,
                    "representation_mode": "cv",
                    "person_name": "Jane Doe",
                    "emails": ["jane.doe@example.org"],
                    "phone_numbers": ["+64 21 123 4567"],
                    "affiliations": ["University of Auckland"],
                    "roles": ["Senior Researcher"],
                },
            },
            {"status": "ok"},
        ),
    )

    result = materialise_person_representation_for_file_copy(
        user_concept_id="#V#user_test",
        file_copy_concept_id="#V#uploaded_file_copy_person_1",
        extracted_text="Candidate CV details",
        original_filename="jane-doe-cv.pdf",
        interpretation={"description": "CV extracted from uploaded document."},
    )

    assert result["attempted"] is True
    assert result["verified"] is True
    assert result["success"] is True
    assert result["reason"] == "person_representation_verified"
    assert result["representation_mode"] == "cv"
    assert result["person_name"] == "Jane Doe"
    assert result["emails"] == ["jane.doe@example.org"]
    assert "Senior Researcher" in result["roles"]
    assert "University of Auckland" in result["affiliations"]
    assert result["person_concept_id"].startswith("#V#person_jane_doe_")

    predicates = [row["predicate"] for row in relation_writes]
    assert "hasName" in predicates
    assert "#V#has_email" in predicates
    assert "#V#hasRole" in predicates
    assert "#V#hasNote" in predicates

    assert relationship_writes == [
        {
            "source_id": "#V#uploaded_file_copy_person_1",
            "predicate": "#V#documentary_evidence_for",
            "target": result["person_concept_id"],
        }
    ]


def test_materialise_person_representation_fails_when_identity_unresolved(monkeypatch):
    from src.backend.services.person_file_representation_service import (
        materialise_person_representation_for_file_copy,
    )

    monkeypatch.setattr(
        "src.backend.services.person_file_representation_service.infer_file_copy_entity_representation_candidates",
        lambda **_kwargs: (
            {
                "schema_version": "file_copy_entity_representation_interpretation.v1",
                "person_candidate": {
                    "applicable": True,
                    "representation_mode": "business_card",
                    "person_name": "",
                    "emails": ["info@example.org"],
                },
            },
            {"status": "ok"},
        ),
    )

    result = materialise_person_representation_for_file_copy(
        user_concept_id="#V#user_test",
        file_copy_concept_id="#V#uploaded_file_copy_person_2",
        extracted_text="Business card details",
        original_filename="contact-card.png",
    )

    assert result["attempted"] is True
    assert result["verified"] is False
    assert result["success"] is False
    assert result["reason"] == "person_identity_unresolved"
    assert result["person_concept_id"] is None


def test_materialise_person_representation_is_not_attempted_for_unrelated_text(
    monkeypatch,
):
    from src.backend.services.person_file_representation_service import (
        materialise_person_representation_for_file_copy,
    )

    monkeypatch.setattr(
        "src.backend.services.person_file_representation_service.infer_file_copy_entity_representation_candidates",
        lambda **_kwargs: (
            {
                "schema_version": "file_copy_entity_representation_interpretation.v1",
                "person_candidate": {"applicable": False},
            },
            {"status": "no_candidates"},
        ),
    )

    result = materialise_person_representation_for_file_copy(
        user_concept_id="#V#user_test",
        file_copy_concept_id="#V#uploaded_file_copy_generic_1",
        extracted_text="This is an ecosystem planning document about transport policy.",
        original_filename="planning-notes.txt",
    )

    assert result["attempted"] is False
    assert result["verified"] is False
    assert result["success"] is False
    assert result["reason"] == "not_person_artefact"
