from __future__ import annotations


def test_build_concept_evidence_profile_exposes_structured_evidence_without_scoring(
    monkeypatch,
) -> None:
    from src.backend.services import entity_identity_evidence_service as service

    concept_doc = {
        "concept_id": "#V#person_michael_witbrock_0880532f",
        "name": "Michael Witbrock",
        "relationships": {
            "is_an_instance_of": ["#V#person"],
            "#V#has_affiliation": ["#V#university_of_auckland"],
            "#V#arbitrary_external_identifier": [
                "https://orcid.org/0000-0002-1825-0097"
            ],
        },
    }

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        lambda query: concept_doc
        if query == {"concept_id": "#V#person_michael_witbrock_0880532f"}
        else None,
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [
            {
                "predicate": "#V#any_text_predicate",
                "text": "https://www.cs.auckland.ac.nz/~michael/",
            },
            {
                "predicate": "#V#has_affiliation",
                "text": "University of Auckland",
            },
        ],
    )
    monkeypatch.setattr(
        service,
        "find_relations_with_argument",
        lambda *_args, **_kwargs: {
            "hits": [{"source_concept_id": "#V#paper_on_arxiv_2603_21852"}]
        },
    )

    profile = service.build_concept_evidence_profile(
        "#V#person_michael_witbrock_0880532f"
    )

    assert profile is not None
    assert profile["concept_id"] == "#V#person_michael_witbrock_0880532f"
    assert profile["is_person"] is True
    assert profile["source_refs"] == [
        "www.cs.auckland.ac.nz/~michael",
        "orcid.org/0000-0002-1825-0097",
    ]
    assert profile["authored_papers"] == [
        {"paper_concept_id": "#V#paper_on_arxiv_2603_21852"}
    ]
    assert profile["evidence_counts"]["authored_paper_count"] == 1
    assert profile["evidence_counts"]["source_ref_count"] == 2
    assert "density_signal" not in profile
    assert profile["text_relations_summary"]["text_relation_samples"][1] == {
        "predicate": "#V#has_affiliation",
        "text_preview": "University of Auckland",
    }
