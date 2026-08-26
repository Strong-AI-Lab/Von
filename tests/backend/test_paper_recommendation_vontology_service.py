from __future__ import annotations

import json

import src.backend.services.paper_recommendation_vontology_service as service


def test_load_profile_uses_actor_effective_scoped_overlay(monkeypatch):
    requested_views = []
    monkeypatch.setattr(service, "load_concept", lambda _concept_id: {"relationships": {}})
    monkeypatch.setattr(
        service,
        "_normalise_profile_overlay",
        lambda profile, *, subject_concept_id: {
            **dict(profile or {}),
            "subject_concept_id": subject_concept_id,
        },
    )

    def _texts(*, subject_concept_id, predicate, limit, context_view):
        requested_views.append(context_view)
        assert subject_concept_id == "#V#bin"
        assert predicate == service.GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID
        assert limit == 8
        return [
            {
                "row_kind": "scoped_assertion",
                "assertion_id": "ska_actor_profile",
                "assertion_scope": {"mode": "user"},
                "text": json.dumps(
                    {
                        "schema_version": "paper_matching_profile.v1",
                        "subject_concept_id": "#V#bin",
                        "project_description": "Actor-scoped research summary",
                        "stated_interest_terms": ["causal reasoning"],
                    }
                ),
            },
            {
                "row_kind": "base_text_relation",
                "relation_id": "base_relation",
                "text": json.dumps(
                    {
                        "schema_version": "paper_matching_profile.v1",
                        "subject_concept_id": "#V#bin",
                        "project_description": "Over-broad public profile",
                    }
                ),
            },
        ]

    monkeypatch.setattr(service, "get_texts_for_concept", _texts)

    payload = service.load_subject_paper_matching_profile("#V#bin")

    assert payload["success"] is True
    assert requested_views == ["actor_effective"]
    assert payload["profile_present"] is True
    assert payload["profile_conflict"] is False
    assert payload["profile"]["project_description"] == (
        "Actor-scoped research summary"
    )
    assert payload["profile"]["stated_interest_terms"] == ["causal reasoning"]
    assert payload["profile_diagnostics"]["scoped_candidate_count"] == 1
    assert payload["profile_diagnostics"]["base_candidate_count"] == 1


def test_load_profile_surfaces_multiple_distinct_scoped_profiles_as_conflict(
    monkeypatch,
):
    monkeypatch.setattr(service, "load_concept", lambda _concept_id: {"relationships": {}})
    monkeypatch.setattr(
        service,
        "_normalise_profile_overlay",
        lambda profile, *, subject_concept_id: {
            **dict(profile or {}),
            "subject_concept_id": subject_concept_id,
        },
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda **_kwargs: [
            {
                "row_kind": "scoped_assertion",
                "assertion_id": "ska_one",
                "text": json.dumps(
                    {
                        "schema_version": "paper_matching_profile.v1",
                        "subject_concept_id": "#V#bin",
                        "project_description": "First active revision",
                    }
                ),
            },
            {
                "row_kind": "scoped_assertion",
                "assertion_id": "ska_two",
                "text": json.dumps(
                    {
                        "schema_version": "paper_matching_profile.v1",
                        "subject_concept_id": "#V#bin",
                        "project_description": "Second active revision",
                    }
                ),
            },
        ],
    )

    payload = service.load_subject_paper_matching_profile("#V#bin")

    assert payload["success"] is True
    assert payload["profile_present"] is False
    assert payload["profile_conflict"] is True
    assert payload["profile_diagnostics"]["candidate_count"] == 2
    assert payload["profile_diagnostics"]["scoped_candidate_count"] == 2


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
