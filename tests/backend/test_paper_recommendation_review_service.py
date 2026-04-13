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


def test_build_message_linked_paper_recommendation_review_uses_materialised_rows(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "load_materialised_paper_recommendations",
        lambda **_kwargs: {
            "success": True,
            "recommendations": [
                {
                    "assertion_concept_id": "#V#assertion_1",
                    "paper_concept_id": "#V#paper_causal_science",
                    "paper_title": "Causal Models for Scientific Discovery",
                    "score": 0.88,
                    "active": True,
                },
                {
                    "assertion_concept_id": "#V#assertion_2",
                    "paper_concept_id": "#V#paper_graph_methods",
                    "paper_title": "Graph Methods for Discovery",
                    "score": 0.71,
                    "active": True,
                },
            ],
        },
    )
    monkeypatch.setattr(
        service,
        "list_paper_recommendation_feedback",
        lambda **kwargs: {
            "success": True,
            "feedback": [
                {
                    "feedback_concept_id": "#V#feedback_1",
                    "feedback_payload": {
                        "assertion_concept_id": kwargs["assertion_concept_id"],
                        "recommendation_usefulness": "useful",
                    },
                }
            ]
            if kwargs["assertion_concept_id"] == "#V#assertion_1"
            else [],
        },
    )

    payload = service.build_message_linked_paper_recommendation_review(
        subject_concept_id="#V#lu_yunli",
        assertion_concept_ids=["#V#assertion_2", "#V#assertion_1"],
        message_concept_id="#V#message_1",
        trigger_source="message_opened",
    )

    assert payload["success"] is True
    assert payload["review_surface_id"] == (
        "message_panel.paper_recommendation_review.v1"
    )
    assert payload["candidate_selection"]["assertion_concept_ids"] == [
        "#V#assertion_2",
        "#V#assertion_1",
    ]
    assert [
        row["assertion_concept_id"]
        for row in payload["recommendation_report"]["results"]
    ] == ["#V#assertion_2", "#V#assertion_1"]
    assert payload["recommendation_report"]["results"][1]["feedback_count"] == 1
    assert payload["recommendation_report"]["results"][1]["latest_feedback"] == {
        "assertion_concept_id": "#V#assertion_1",
        "recommendation_usefulness": "useful",
    }
