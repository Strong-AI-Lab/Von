from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import src.backend.services.paper_recommendation_vontology_service as service


def test_record_paper_recommendation_feedback_persists_structured_feedback(
    monkeypatch,
):
    concepts = {
        "#V#lu_yunli": {"concept_id": "#V#lu_yunli", "name": "Lu Yunli"},
        "#V#paper_causal_science": {
            "concept_id": "#V#paper_causal_science",
            "name": "Causal Models for Scientific Discovery",
        },
        "#V#paper_recommendation_assertion_for_lu_yunli_causal": {
            "concept_id": "#V#paper_recommendation_assertion_for_lu_yunli_causal",
            "relationships": {
                service.PAPER_RECOMMENDATION_ASSERTS_SUBJECT_PREDICATE_ID: [
                    "#V#lu_yunli"
                ],
                service.PAPER_RECOMMENDATION_ASSERTS_PAPER_PREDICATE_ID: [
                    "#V#paper_causal_science"
                ],
            },
        },
        "#V#paper_recommendation_profile_for_lu_yunli": {
            "concept_id": "#V#paper_recommendation_profile_for_lu_yunli",
            "name": "Recommendation profile for Lu Yunli",
        },
    }
    created: dict[str, object] = {}
    relationship_calls: list[dict[str, str]] = []
    text_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        service,
        "ensure_paper_recommendation_primitives",
        lambda: {"success": True},
    )
    monkeypatch.setattr(service, "load_concept", lambda concept_id: concepts.get(concept_id))
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id_exact",
        lambda concept_id: concepts.get(concept_id),
    )
    monkeypatch.setattr(
        service,
        "load_subject_paper_matching_profile",
        lambda subject_concept_id: {
            "success": True,
            "profile_concept_id": "#V#paper_recommendation_profile_for_lu_yunli",
        },
    )
    monkeypatch.setattr(
        service,
        "_load_latest_text",
        lambda subject_concept_id, predicate: (
            json.dumps(
                {
                    "policy_version": "paper_recommendation_policy.v2.semantic",
                    "decision_mode": "embedding_plus_llm",
                    "trigger_source": "build_paper_recommendations",
                    "updated_at": "2026-04-04T00:00:00+00:00",
                    "subject_profile_concept_id": "#V#paper_recommendation_profile_for_lu_yunli",
                }
            )
            if subject_concept_id
            == "#V#paper_recommendation_assertion_for_lu_yunli_causal"
            and predicate == service.PAPER_RECOMMENDATION_EVALUATION_JSON_PREDICATE_ID
            else None
        ),
    )
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **kwargs: created.update(kwargs) or {"concept_id": kwargs["concept_id"]},
    )
    monkeypatch.setattr(
        service,
        "add_relationship",
        lambda **kwargs: relationship_calls.append(kwargs),
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: text_calls.append(kwargs)
        or {"relation_id": f"rel-{len(text_calls)}"},
    )
    monkeypatch.setattr(
        service.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="deadbeef"),
    )

    result = service.record_paper_recommendation_feedback(
        actor_user_concept_id="#V#lu_yunli",
        assertion_concept_id="#V#paper_recommendation_assertion_for_lu_yunli_causal",
        recommendation_usefulness="useful",
        explanation_usefulness="partial",
        feedback_text="Helpful recommendation, but the explanation missed the project context.",
        capture_surface="chat_assistant",
        conversation_session_id="sess-1",
        request_id="req-1",
    )

    assert result["success"] is True
    assert result["feedback_concept_id"] == "#V#paper_recommendation_feedback_deadbeef"
    assert created["concept_id"] == "#V#paper_recommendation_feedback_deadbeef"
    assert created["created_by_concept_id"] == "#V#lu_yunli"
    assert created["parent_concept_ids"] == [service.PAPER_RECOMMENDATION_FEEDBACK_TYPE_ID]

    json_text_call = next(
        call
        for call in text_calls
        if call["predicate"] == service.PAPER_RECOMMENDATION_FEEDBACK_JSON_PREDICATE_ID
    )
    feedback_payload = json.loads(str(json_text_call["text"]))
    assert feedback_payload["recommendation_usefulness_label"] == "useful"
    assert feedback_payload["recommendation_usefulness_score"] == 1.0
    assert feedback_payload["explanation_usefulness_label"] == "partly_useful"
    assert feedback_payload["explanation_usefulness_score"] == 0.0
    assert feedback_payload["conversation_session_id"] == "sess-1"
    assert feedback_payload["request_id"] == "req-1"
    assert (
        feedback_payload["profile_concept_id"]
        == "#V#paper_recommendation_profile_for_lu_yunli"
    )
    assert feedback_payload["recommendation_policy_version"] == (
        "paper_recommendation_policy.v2.semantic"
    )

    predicates = {(call["source_id"], call["predicate"], call["target"]) for call in relationship_calls}
    assert (
        "#V#lu_yunli",
        service.HAS_PAPER_RECOMMENDATION_FEEDBACK_PREDICATE_ID,
        "#V#paper_recommendation_feedback_deadbeef",
    ) in predicates
    assert (
        "#V#paper_recommendation_feedback_deadbeef",
        service.PAPER_RECOMMENDATION_FEEDBACK_ASSERTION_PREDICATE_ID,
        "#V#paper_recommendation_assertion_for_lu_yunli_causal",
    ) in predicates


def test_record_paper_recommendation_feedback_requires_materialised_assertion(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "ensure_paper_recommendation_primitives",
        lambda: {"success": True},
    )
    monkeypatch.setattr(service, "load_concept", lambda _concept_id: None)

    with pytest.raises(ValueError, match="materialised recommendation assertion"):
        service.record_paper_recommendation_feedback(
            actor_user_concept_id="#V#lu_yunli",
            subject_concept_id="#V#lu_yunli",
            paper_concept_id="#V#paper_causal_science",
            recommendation_usefulness="useful",
        )
