"""Gateway-level coverage for paper recommendation ranking MCP tool."""

from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_build_paper_recommendations_gateway_invoke(monkeypatch):
    gateway = _build_gateway()

    captured: dict[str, object] = {}

    def _fake_build_paper_recommendations(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "user_concept_id": kwargs["user_concept_id"],
            "profile_concept_id": "#V#paper_recommendation_profile_for_lu_yunli",
            "recommendation_policy_version": "paper_recommendation_policy.v1",
            "generated_at": "2026-04-03T00:00:00+00:00",
            "results": [
                {
                    "paper_concept_id": "#V#paper_causal_science",
                    "paper_title": "Causal Models for Scientific Discovery",
                    "status": "ranked",
                    "score": 0.88,
                    "recommendation_tier": "strong",
                    "rationale_summary": "Matches stated interests: causal reasoning.",
                    "rationale": ["Matches stated interests: causal reasoning."],
                    "evidence": [],
                    "paper_representation": {
                        "paper_title": "Causal Models for Scientific Discovery",
                        "summary_excerpt": "A causal paper.",
                        "author_names": ["Judea Pearl"],
                        "topic_labels": ["causal reasoning"],
                        "venue_names": ["NeurIPS"],
                        "publication_date": "2026-03-14",
                    },
                    "provenance": {
                        "paper_concept_id": "#V#paper_causal_science",
                        "author_concept_ids": ["#V#judea_pearl"],
                    },
                    "positive_match_count": 3,
                    "negative_match_count": 0,
                }
            ],
            "ranked_count": 1,
            "skipped_count": 0,
            "candidate_count_requested": 1,
            "warning_count": 0,
            "warnings": [],
            "profile_signal_summary": {"usable": True},
            "profile_diagnostics": {"profile_exists": True},
        }

    monkeypatch.setattr(
        "src.backend.services.paper_recommendation_ranking_service.build_paper_recommendations",
        _fake_build_paper_recommendations,
    )

    payload = gateway.invoke(
        "build_paper_recommendations",
        {
            "user_concept_id": "#V#lu_yunli",
            "paper_concept_ids": ["#V#paper_causal_science"],
        },
    ).payload

    assert payload["success"] is True
    assert payload["results"][0]["paper_concept_id"] == "#V#paper_causal_science"
    assert payload["namespace"] == "#V#lu_yunli"
    assert captured == {
        "user_concept_id": "#V#lu_yunli",
        "candidate_paper_concept_ids": ["#V#paper_causal_science"],
        "max_results": 10,
        "include_all_candidates": False,
    }


def test_materialise_paper_recommendations_gateway_invoke(monkeypatch):
    gateway = _build_gateway()

    captured: dict[str, object] = {}

    def _fake_materialise(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "triggered": True,
            "refreshed_subject_count": 1,
            "refreshed_paper_count": 1,
            "subject_concept_ids": ["#V#project_alpha"],
            "candidate_paper_concept_ids": ["#V#paper_causal_science"],
            "reports": [{"success": True, "subject_concept_id": "#V#project_alpha"}],
        }

    monkeypatch.setattr(
        "src.backend.services.paper_recommendation_materialisation_service.materialise_paper_recommendations_from_event",
        _fake_materialise,
    )

    payload = gateway.invoke(
        "materialise_paper_recommendations",
        {
            "target_subject_concept_id": "#V#project_alpha",
            "paper_concept_ids": ["#V#paper_causal_science"],
            "event_type": "paper_recommendation.requested",
            "trigger_source": "test",
        },
    ).payload

    assert payload["success"] is True
    assert payload["triggered"] is True
    assert payload["subject_concept_ids"] == ["#V#project_alpha"]
    assert captured == {
        "event_payload": {"event_type": "paper_recommendation.requested"},
        "target_subject_concept_ids": ["#V#project_alpha"],
        "candidate_paper_concept_ids": ["#V#paper_causal_science"],
        "candidate_limit": 24,
        "max_results": 10,
        "trigger_source": "test",
    }
