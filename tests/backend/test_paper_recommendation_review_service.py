from __future__ import annotations

import src.backend.services.paper_recommendation_review_service as service


def test_build_paper_recommendation_review_uses_explicit_candidate_ids(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        service,
        "build_paper_recommendations",
        lambda **kwargs: captured.update(kwargs)
        or {
            "success": True,
            "results": [
                {
                    "paper_concept_id": "#V#paper_causal_science",
                    "paper_title": "Causal Models for Scientific Discovery",
                }
            ],
        },
    )

    payload = service.build_paper_recommendation_review(
        user_concept_id="#V#lu_yunli",
        candidate_paper_concept_ids=(
            "#V#paper_causal_science,\n#V#paper_graph_methods, #V#paper_causal_science"
        ),
        candidate_limit=40,
        include_all_candidates=True,
        trigger_source="candidate_ingestion",
    )

    assert payload["success"] is True
    assert payload["trigger"]["trigger_source"] == "candidate_ingestion"
    assert payload["candidate_selection"]["source"] == "explicit_candidate_ids"
    assert payload["candidate_selection"]["candidate_paper_concept_ids"] == [
        "#V#paper_causal_science",
        "#V#paper_graph_methods",
    ]
    assert captured == {
        "user_concept_id": "#V#lu_yunli",
        "candidate_paper_concept_ids": [
            "#V#paper_causal_science",
            "#V#paper_graph_methods",
        ],
        "max_results": 40,
        "include_all_candidates": True,
    }


def test_build_paper_recommendation_review_discovers_recent_candidates(monkeypatch):
    monkeypatch.setattr(
        service,
        "search_concepts",
        lambda **_kwargs: {
            "results": [
                {
                    "concept_id": "#V#paper_older",
                    "name": "Older Paper",
                },
                {
                    "concept_id": "#V#paper_newer",
                    "name": "Newer Paper",
                },
            ]
        },
    )

    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda subject_concept_id, predicate=None, limit=50: [
            {"text": "2026-03-14"}
            if subject_concept_id == "#V#paper_older"
            else {"text": "2026-03-20"}
        ],
    )

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        service,
        "build_paper_recommendations",
        lambda **kwargs: captured.update(kwargs) or {"success": False, "error": "stub"},
    )

    payload = service.build_paper_recommendation_review(
        user_concept_id="#V#lu_yunli",
        candidate_limit=15,
        include_all_candidates=False,
        trigger_source="manual_review",
    )

    assert payload["success"] is False
    assert payload["candidate_selection"]["source"] == "represented_scholarly_articles"
    assert payload["candidate_selection"]["discovered_candidates"] == [
        {
            "paper_concept_id": "#V#paper_newer",
            "paper_title": "Newer Paper",
            "publication_date": "2026-03-20",
        },
        {
            "paper_concept_id": "#V#paper_older",
            "paper_title": "Older Paper",
            "publication_date": "2026-03-14",
        },
    ]
    assert captured["candidate_paper_concept_ids"] == [
        "#V#paper_newer",
        "#V#paper_older",
    ]
    assert captured["max_results"] == 15
    assert captured["include_all_candidates"] is False
