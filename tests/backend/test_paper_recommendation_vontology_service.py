from __future__ import annotations

import json

import src.backend.services.paper_recommendation_vontology_service as service


def test_list_subject_concept_ids_with_paper_matching_profiles_queries_profile_sources(
    monkeypatch,
):
    monkeypatch.setattr(
        service.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"subject_concept_id": "#V#lu_yunli"},
            {"subject_concept_id": "#V#lu_yunli"},
        ],
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [{"concept_id": "#V#michael_witbrock"}],
    )

    assert service.list_subject_concept_ids_with_paper_matching_profiles(limit=10) == [
        "#V#lu_yunli",
        "#V#michael_witbrock",
    ]


def test_load_materialised_paper_recommendations_surfaces_review_facing_fields(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "load_concept",
        lambda concept_id: {
            "#V#lu_yunli": {
                "relationships": {
                    service.HAS_PAPER_RECOMMENDATION_ASSERTION_PREDICATE_ID: [
                        "#V#assertion_1"
                    ]
                }
            },
            "#V#assertion_1": {
                "relationships": {
                    service.PAPER_RECOMMENDATION_ASSERTS_PAPER_PREDICATE_ID: [
                        "#V#paper_causal_science"
                    ],
                    service.PAPER_RECOMMENDATION_DELIVERED_VIA_MESSAGE_PREDICATE_ID: [
                        "#V#message_1"
                    ],
                }
            },
        }.get(concept_id),
    )
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id_exact",
        lambda concept_id: {
            "name": "Causal Models for Scientific Discovery"
        }
        if concept_id == "#V#paper_causal_science"
        else {},
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda subject_concept_id, predicate=None, limit=50: [
            {
                "text": json.dumps(
                    {
                        "status": "ranked",
                        "score": 0.88,
                        "active": True,
                        "recommendation_tier": "recommended",
                        "rationale_summary": "Matches stated interests: causal reasoning.",
                        "rationale": "Matches stated interests: causal reasoning.",
                        "evidence": [{"evidence_type": "interest_term_match"}],
                        "paper_representation": {
                            "publication_date": "2026-03-14",
                        },
                        "provenance": {
                            "paper_concept_id": "#V#paper_causal_science",
                        },
                        "subject_profile_concept_id": "#V#paper_recommendation_profile_for_lu_yunli",
                    }
                )
            }
        ]
        if subject_concept_id == "#V#assertion_1"
        else [],
    )

    payload = service.load_materialised_paper_recommendations(
        subject_concept_id="#V#lu_yunli",
    )

    assert payload["success"] is True
    row = payload["recommendations"][0]
    assert row["paper_title"] == "Causal Models for Scientific Discovery"
    assert row["status"] == "ranked"
    assert row["recommendation_tier"] == "recommended"
    assert row["rationale"] == ["Matches stated interests: causal reasoning."]
    assert row["evidence"] == [{"evidence_type": "interest_term_match"}]
    assert row["paper_representation"]["publication_date"] == "2026-03-14"
    assert row["provenance"]["paper_concept_id"] == "#V#paper_causal_science"
    assert row["subject_profile_concept_id"] == (
        "#V#paper_recommendation_profile_for_lu_yunli"
    )
