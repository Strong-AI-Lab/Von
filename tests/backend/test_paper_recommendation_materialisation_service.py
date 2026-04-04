from __future__ import annotations

import src.backend.services.paper_recommendation_materialisation_service as service


def test_materialise_paper_recommendations_for_subject_persists_ranked_and_inactive(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "_build_subject_bundle",
        lambda _subject_id: {
            "success": True,
            "subject_concept_id": "#V#project_alpha",
            "profile_concept_id": "#V#paper_recommendation_profile_for_project_alpha",
            "profile_source_predicate": "#V#has_paper_matching_profile_json",
            "profile_present": True,
            "related_concepts": [],
            "research_interest_concepts": [],
            "organisation_concept_ids": [],
            "profile": {},
        },
    )
    monkeypatch.setattr(
        service,
        "_score_candidates_with_embeddings",
        lambda **_kwargs: (
            [
                {
                    "paper_concept_id": "#V#paper_new",
                    "embedding_score": 0.88,
                    "status": "scored",
                    "paper_bundle": {
                        "paper_title": "New Paper",
                        "summary_excerpt": "new summary",
                        "author_names": ["A. Author"],
                        "topic_labels": ["Causal AI"],
                        "publication_date": "2026-01-01",
                    },
                },
                {
                    "paper_concept_id": "#V#paper_old",
                    "embedding_score": 0.44,
                    "status": "scored",
                    "paper_bundle": {
                        "paper_title": "Old Paper",
                        "summary_excerpt": "old summary",
                        "author_names": ["B. Author"],
                        "topic_labels": ["Legacy Topic"],
                        "publication_date": "2024-01-01",
                    },
                },
            ],
            {"embedding_client": "FakeClient"},
        ),
    )
    monkeypatch.setattr(
        service,
        "_llm_rerank_candidates",
        lambda **_kwargs: (
            [
                {
                    "paper_concept_id": "#V#paper_new",
                    "score": 0.91,
                    "rationale_summary": "Excellent fit.",
                    "rationale": "Matches the project focus.",
                    "evidence": [{"kind": "topic_fit"}],
                }
            ],
            {"decision_mode": "embedding_plus_llm"},
        ),
    )
    monkeypatch.setattr(
        service,
        "load_materialised_paper_recommendations",
        lambda **_kwargs: {
            "success": True,
            "recommendations": [
                {
                    "paper_concept_id": "#V#paper_old",
                    "evaluation": {
                        "active": True,
                        "score": 0.72,
                        "rationale_summary": "Previously active.",
                    },
                }
            ],
        },
    )

    calls: list[dict[str, object]] = []

    def _fake_upsert(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "assertion_concept_id": f"#V#assertion_{kwargs['paper_concept_id']}",
        }

    monkeypatch.setattr(service, "upsert_paper_recommendation_assertion", _fake_upsert)

    payload = service.materialise_paper_recommendations_for_subject(
        subject_concept_id="#V#project_alpha",
        candidate_paper_concept_ids=["#V#paper_new", "#V#paper_old"],
        max_results=5,
    )

    assert payload["success"] is True
    assert payload["ranked_count"] == 1
    assert payload["results"][0]["paper_concept_id"] == "#V#paper_new"
    assert payload["results"][0]["active"] is True
    assert len(calls) == 2
    assert calls[0]["paper_concept_id"] == "#V#paper_new"
    assert calls[0]["evaluation_payload"]["active"] is True
    assert calls[1]["paper_concept_id"] == "#V#paper_old"
    assert calls[1]["evaluation_payload"]["active"] is False


def test_materialise_paper_recommendations_from_event_resolves_legacy_profile_update(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "resolve_subject_ids_for_legacy_profile_concept",
        lambda _profile_id: ["#V#project_alpha", "#V#org_beta"],
    )

    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        service,
        "materialise_paper_recommendations_for_subject",
        lambda **kwargs: captured.append(kwargs)
        or {"success": True, "subject_concept_id": kwargs["subject_concept_id"]},
    )

    payload = service.materialise_paper_recommendations_from_event(
        event_payload={
            "event_type": "text_relation.upserted",
            "subject_concept_id": "#V#paper_recommendation_profile_for_lu_yunli",
            "predicate": "#V#has_paper_recommendation_profile_json",
        },
        trigger_source="test",
    )

    assert payload["success"] is True
    assert payload["refreshed_subject_count"] == 2
    assert [row["subject_concept_id"] for row in captured] == [
        "#V#project_alpha",
        "#V#org_beta",
    ]


def test_materialise_paper_recommendations_from_event_refreshes_generic_subject_profile(
    monkeypatch,
):
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        service,
        "materialise_paper_recommendations_for_subject",
        lambda **kwargs: captured.append(kwargs)
        or {"success": True, "subject_concept_id": kwargs["subject_concept_id"]},
    )

    payload = service.materialise_paper_recommendations_from_event(
        event_payload={
            "event_type": "text_relation.upserted",
            "subject_concept_id": "#V#project_alpha",
            "predicate": "#V#has_paper_matching_profile_json",
        },
        trigger_source="test",
    )

    assert payload["success"] is True
    assert payload["refreshed_subject_count"] == 1
    assert len(captured) == 1
    assert captured[0]["subject_concept_id"] == "#V#project_alpha"
    assert captured[0]["candidate_paper_concept_ids"] is None
    assert captured[0]["candidate_limit"] == service.DEFAULT_CANDIDATE_RECALL_LIMIT
    assert captured[0]["max_results"] == service.DEFAULT_MAX_RESULTS
    assert captured[0]["include_all_candidates"] is False
    assert captured[0]["trigger_source"] == "test"
