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
        "discover_subjects_if_missing": False,
    }


def test_deliver_paper_recommendation_messages_gateway_invoke(monkeypatch):
    gateway = _build_gateway()

    captured: dict[str, object] = {}

    def _fake_deliver(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "triggered": True,
            "delivered_message_count": 1,
            "delivered_assertion_count": 2,
            "delivered_message_ids": ["#V#message_von_system_1"],
            "delivered_subject_concept_ids": ["#V#lu_yunli"],
            "subject_reports": [
                {
                    "subject_concept_id": "#V#lu_yunli",
                    "triggered": True,
                    "message_concept_id": "#V#message_von_system_1",
                }
            ],
            "errors": [],
        }

    monkeypatch.setattr(
        "src.backend.services.paper_recommendation_delivery_service.deliver_paper_recommendation_messages",
        _fake_deliver,
    )

    payload = gateway.invoke(
        "deliver_paper_recommendation_messages",
        {
            "subject_concept_id": "#V#lu_yunli",
            "trigger_source": "test",
            "max_recommendations_per_message": 2,
        },
    ).payload

    assert payload["success"] is True
    assert payload["triggered"] is True
    assert payload["delivered_message_ids"] == ["#V#message_von_system_1"]
    assert captured == {
        "refresh_reports": None,
        "subject_concept_ids": ["#V#lu_yunli"],
        "trigger_source": "test",
        "max_recommendations_per_message": 2,
    }


def test_record_paper_recommendation_feedback_gateway_invoke(monkeypatch):
    gateway = _build_gateway()

    captured: dict[str, object] = {}

    def _fake_record_feedback(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "feedback_concept_id": "#V#paper_recommendation_feedback_deadbeef",
            "actor_user_concept_id": kwargs["actor_user_concept_id"],
            "subject_concept_id": kwargs["subject_concept_id"],
            "assertion_concept_id": kwargs["assertion_concept_id"],
            "paper_concept_id": "#V#paper_causal_science",
            "feedback_payload": {
                "recommendation_usefulness_label": "useful",
                "explanation_usefulness_label": "partly_useful",
            },
        }

    monkeypatch.setattr(
        "src.backend.services.paper_recommendation_vontology_service.record_paper_recommendation_feedback",
        _fake_record_feedback,
    )

    payload = gateway.invoke(
        "record_paper_recommendation_feedback",
        {
            "user_concept_id": "#V#lu_yunli",
            "assertion_concept_id": "#V#paper_recommendation_assertion_for_lu_yunli_causal",
            "recommendation_usefulness": "useful",
            "explanation_usefulness": "partly_useful",
            "feedback_text": "Useful recommendation, but the explanation should be more specific.",
            "session_id": "sess-1",
            "request_id": "req-1",
        },
    ).payload

    assert payload["success"] is True
    assert payload["feedback_concept_id"] == (
        "#V#paper_recommendation_feedback_deadbeef"
    )
    assert payload["namespace"] == "#V#lu_yunli"
    assert captured == {
        "actor_user_concept_id": "#V#lu_yunli",
        "subject_concept_id": "#V#lu_yunli",
        "assertion_concept_id": "#V#paper_recommendation_assertion_for_lu_yunli_causal",
        "paper_concept_id": None,
        "recommendation_usefulness": "useful",
        "explanation_usefulness": "partly_useful",
        "feedback_text": "Useful recommendation, but the explanation should be more specific.",
        "capture_surface": "conversation",
        "conversation_session_id": "sess-1",
        "request_id": "req-1",
        "organisation_concept_id": None,
        "profile_concept_id": None,
    }
