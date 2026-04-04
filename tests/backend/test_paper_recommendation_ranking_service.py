from __future__ import annotations

import src.backend.services.paper_recommendation_ranking_service as service


def test_build_paper_recommendations_delegates_to_semantic_materialiser(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_materialise(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "subject_concept_id": kwargs["subject_concept_id"],
            "recommendation_policy_version": service.PAPER_RECOMMENDATION_POLICY_VERSION,
            "results": [
                {
                    "paper_concept_id": "#V#paper_causal_science",
                    "status": "ranked",
                    "score": 0.91,
                }
            ],
            "ranked_count": 1,
            "skipped_count": 0,
            "candidate_count_requested": len(kwargs["candidate_paper_concept_ids"]),
            "profile_signal_summary": {"usable": True},
            "warnings": [],
            "warning_count": 0,
        }

    monkeypatch.setattr(
        service,
        "materialise_paper_recommendations_for_subject",
        _fake_materialise,
    )

    payload = service.build_paper_recommendations(
        user_concept_id="#V#lu_yunli",
        candidate_paper_concept_ids=[
            "#V#paper_causal_science",
            "#V#paper_graph_methods",
        ],
        max_results=5,
        include_all_candidates=True,
    )

    assert payload["success"] is True
    assert payload["ranked_count"] == 1
    assert captured == {
        "subject_concept_id": "#V#lu_yunli",
        "candidate_paper_concept_ids": [
            "#V#paper_causal_science",
            "#V#paper_graph_methods",
        ],
        "max_results": 5,
        "include_all_candidates": True,
        "trigger_source": "build_paper_recommendations",
    }


def test_build_paper_recommendations_surfaces_materialiser_errors(monkeypatch):
    monkeypatch.setattr(
        service,
        "materialise_paper_recommendations_for_subject",
        lambda **_kwargs: {
            "success": False,
            "error": "subject_bundle_failed",
            "message": "Could not assemble the recommendation subject context.",
        },
    )

    payload = service.build_paper_recommendations(
        user_concept_id="#V#lu_yunli",
        candidate_paper_concept_ids=["#V#paper_causal_science"],
    )

    assert payload == {
        "success": False,
        "error": "subject_bundle_failed",
        "message": "Could not assemble the recommendation subject context.",
    }
