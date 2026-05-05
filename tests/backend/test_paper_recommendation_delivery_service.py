from __future__ import annotations

from typing import Any

from src.backend.services import paper_recommendation_delivery_service as service
from tests.backend.paper_recommendation_policy_test_helpers import (
    make_test_paper_recommendation_policy,
    patch_paper_recommendation_policy,
)


def test_deliver_paper_recommendation_messages_creates_digest_message(monkeypatch):
    patch_paper_recommendation_policy(monkeypatch, service)
    subject_doc = {
        "name": "Michael Witbrock",
        "relationships": {
            "is_an_instance_of": ["#V#artificial_intelligence_researcher", "#V#von_user"],
            "#V#member_of_organisation": ["#V#university_of_auckland_strong_ai_lab"],
        },
    }
    researcher_type_doc = {"relationships": {"is_a_type_of": ["#V#researcher"]}}
    researcher_root_doc = {"relationships": {"is_a_type_of": []}}

    def _fake_load_concept(concept_id: str):
        mapping = {
            "#V#michael_witbrock": subject_doc,
            "#V#artificial_intelligence_researcher": researcher_type_doc,
            "#V#researcher": researcher_root_doc,
            "#V#von_user": researcher_root_doc,
        }
        return mapping.get(concept_id)

    monkeypatch.setattr(service, "load_concept", _fake_load_concept)
    monkeypatch.setattr(
        service,
        "load_materialised_paper_recommendations",
        lambda **_kwargs: {
            "success": True,
            "recommendations": [
                {
                    "assertion_concept_id": "#V#assertion_1",
                    "paper_concept_id": "#V#paper_1",
                    "paper_title": "Neuro-symbolic Planning",
                    "score": 0.89,
                    "active": True,
                    "delivered_message_ids": [],
                    "rationale_summary": "Matches Michael's work on knowledge use.",
                    "evaluation": {
                        "paper_representation": {
                            "author_names": ["A. Author"],
                            "topic_labels": ["knowledge use", "reasoning"],
                            "publication_date": "2026-03-01",
                        }
                    },
                },
                {
                    "assertion_concept_id": "#V#assertion_2",
                    "paper_concept_id": "#V#paper_2",
                    "paper_title": "Agent Memory Systems",
                    "score": 0.83,
                    "active": True,
                    "delivered_message_ids": [],
                    "rationale_summary": "Matches Michael's work on enduring knowledge.",
                    "evaluation": {
                        "paper_representation": {
                            "author_names": ["B. Author"],
                            "topic_labels": ["memory", "agents"],
                            "publication_date": "2026-02-01",
                        }
                    },
                },
            ],
        },
    )
    monkeypatch.setattr(
        service,
        "resolve_linked_prompt_concept_id",
        lambda **_kwargs: "#V#paper_recommendation_delivery_message_prompt",
    )

    class _RenderedPrompt:
        def __init__(self, text: str):
            self.text = text

    rendered_prompt_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service,
        "render_authoritative_prompt",
        lambda **kwargs: (
            rendered_prompt_calls.append(dict(kwargs))
            or
            _RenderedPrompt(
                f"Hello {kwargs['variables']['recipient_display_name']}\n\n"
                f"{kwargs['variables']['recommendation_items']}"
            ),
            {"resolved_prompt_concept_id": "#V#paper_recommendation_delivery_message_prompt"},
        ),
    )

    created_messages: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service,
        "create_message",
        lambda **kwargs: created_messages.append(kwargs)
        or {"concept_id": "#V#message_von_system_1"},
    )

    linked_assertions: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        service,
        "add_relationship",
        lambda source_id, predicate, target: linked_assertions.append(
            (source_id, predicate, target)
        )
        or {"success": True},
    )

    result = service.deliver_paper_recommendation_messages(
        subject_concept_ids=["#V#michael_witbrock"],
        trigger_source="test",
        max_recommendations_per_message=2,
    )

    assert result["success"] is True
    assert result["triggered"] is True
    assert result["delivered_message_ids"] == ["#V#message_von_system_1"]
    assert created_messages and created_messages[0]["recipient_ids"] == [
        "#V#michael_witbrock"
    ]
    assert created_messages[0]["sender_id"] == "#V#von_system"
    assert created_messages[0]["metadata"]["recommendation_assertion_ids"] == [
        "#V#assertion_1",
        "#V#assertion_2",
    ]
    assert created_messages[0]["metadata"]["recommendation_subject_concept_id"] == (
        "#V#michael_witbrock"
    )
    assert "Open this recommendation message to review usefulness" in (
        rendered_prompt_calls[0]["variables"]["review_hint"]
    )
    assert linked_assertions == [
        (
            "#V#assertion_1",
            "#V#paper_recommendation_delivered_via_message",
            "#V#message_von_system_1",
        ),
        (
            "#V#assertion_2",
            "#V#paper_recommendation_delivered_via_message",
            "#V#message_von_system_1",
        ),
    ]


def test_deliver_paper_recommendation_messages_skips_non_new_recommendations(
    monkeypatch,
):
    patch_paper_recommendation_policy(monkeypatch, service)
    subject_doc = {
        "name": "Lu Yunli",
        "relationships": {
            "is_an_instance_of": ["#V#researcher", "#V#von_user"],
        },
    }

    monkeypatch.setattr(
        service,
        "load_concept",
        lambda concept_id: subject_doc
        if concept_id == "#V#lu_yunli"
        else {"relationships": {"is_a_type_of": []}},
    )
    monkeypatch.setattr(
        service,
        "load_materialised_paper_recommendations",
        lambda **_kwargs: {
            "success": True,
            "recommendations": [
                {
                    "assertion_concept_id": "#V#assertion_1",
                    "paper_concept_id": "#V#paper_1",
                    "paper_title": "Already delivered",
                    "score": 0.8,
                    "active": True,
                    "delivered_message_ids": ["#V#message_existing"],
                    "evaluation": {"paper_representation": {}},
                }
            ],
        },
    )

    result = service.deliver_paper_recommendation_messages(
        subject_concept_ids=["#V#lu_yunli"],
        trigger_source="test",
    )

    assert result["success"] is True
    assert result["triggered"] is False
    assert result["delivered_message_count"] == 0
    assert result["subject_reports"][0]["reason"] == "no_new_active_recommendations"


def test_format_recommendation_block_surfaces_missing_authoritative_rationale_transparently():
    policy = make_test_paper_recommendation_policy()
    block = service._format_recommendation_block(
        1,
        {
            "paper_concept_id": "#V#paper_1",
            "paper_title": "Paper 1",
            "score": 0.5,
            "evaluation": {
                "rationale_generation": {"status": "unavailable"},
                "paper_representation": {},
            },
        },
        policy=policy,
    )

    assert "No authoritative relevance explanation was available" in block


def test_list_paper_recommendation_delivery_subject_ids_finds_researcher_users(
    monkeypatch,
):
    patch_paper_recommendation_policy(monkeypatch, service)
    subject_doc = {
        "concept_id": "#V#michael_witbrock",
        "relationships": {
            "is_an_instance_of": ["#V#artificial_intelligence_researcher", "#V#von_user"],
        },
    }
    researcher_type_doc = {"relationships": {"is_a_type_of": ["#V#researcher"]}}
    von_user_type_doc = {"relationships": {"is_a_type_of": []}}
    researcher_root_doc = {"relationships": {"is_a_type_of": []}}

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [subject_doc],
    )
    monkeypatch.setattr(
        service,
        "list_subject_concept_ids_with_paper_matching_profiles",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        service,
        "load_concept",
        lambda concept_id: {
            "#V#michael_witbrock": subject_doc,
            "#V#artificial_intelligence_researcher": researcher_type_doc,
            "#V#researcher": researcher_root_doc,
            "#V#von_user": von_user_type_doc,
        }.get(concept_id),
    )

    assert service.list_paper_recommendation_delivery_subject_ids(limit=10) == [
        "#V#michael_witbrock"
    ]
