from __future__ import annotations

import json

import src.backend.services.paper_recommendation_profile_vontology_service as service


def test_load_paper_recommendation_profile_returns_profile_and_derived_context(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "resolve_or_create_paper_recommendation_profile_concept_id",
        lambda **_kwargs: "#V#paper_recommendation_profile_for_lu_yunli",
    )

    def _fake_get_concept_by_concept_id(concept_id: str):
        if concept_id == "#V#lu_yunli":
            return {
                "concept_id": concept_id,
                "name": "Lu Yunli",
                "relationships": {
                    service.RESEARCH_INTEREST_PREDICATE_ID: [
                        "#V#knowledge_graph",
                        "#V#causal_reasoning",
                    ],
                },
            }
        if concept_id == "#V#knowledge_graph":
            return {"concept_id": concept_id, "name": "Knowledge Graph"}
        if concept_id == "#V#causal_reasoning":
            return {"concept_id": concept_id, "name": "Causal Reasoning"}
        return None

    monkeypatch.setattr(
        service, "get_concept_by_concept_id", _fake_get_concept_by_concept_id
    )

    def _fake_get_texts_for_concept(subject_concept_id: str, predicate=None, limit=50):
        if subject_concept_id != "#V#paper_recommendation_profile_for_lu_yunli":
            return []
        if predicate != service.PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID:
            return []
        return [
            {
                "text": json.dumps(
                    {
                        "project_description": "Researching graph-grounded reasoning for science.",
                        "stated_interest_terms": [
                            "knowledge graphs",
                            "causal reasoning",
                        ],
                        "negative_interest_terms": ["pure leaderboard work"],
                        "preferred_authors": ["Pearl"],
                        "preferred_venues": ["NeurIPS"],
                        "notes": "Prioritise strong explanations.",
                    }
                )
            }
        ]

    monkeypatch.setattr(service, "get_texts_for_concept", _fake_get_texts_for_concept)

    payload = service.load_paper_recommendation_profile(
        user_concept_id="#V#lu_yunli",
        create_if_missing=False,
    )

    assert payload["success"] is True
    assert payload["profile_concept_id"] == "#V#paper_recommendation_profile_for_lu_yunli"
    assert payload["profile"]["project_description"] == (
        "Researching graph-grounded reasoning for science."
    )
    assert payload["profile"]["stated_interest_terms"] == [
        "knowledge graphs",
        "causal reasoning",
    ]
    assert payload["derived_context"]["research_interest_concepts"] == [
        {"concept_id": "#V#knowledge_graph", "name": "Knowledge Graph"},
        {"concept_id": "#V#causal_reasoning", "name": "Causal Reasoning"},
    ]
    assert payload["diagnostics"]["source_predicate"] == (
        service.PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID
    )


def test_upsert_paper_recommendation_profile_normalises_fields_before_write(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "resolve_or_create_paper_recommendation_profile_concept_id",
        lambda **_kwargs: "#V#paper_recommendation_profile_for_lu_yunli",
    )

    captured: dict[str, object] = {}
    mirrored: dict[str, object] = {}

    def _fake_upsert_singleton_text_relation(**kwargs):
        captured.update(kwargs)
        return {"success": True, "kept_relation_id": "rel-1"}

    monkeypatch.setattr(
        service, "upsert_singleton_text_relation", _fake_upsert_singleton_text_relation
    )
    monkeypatch.setattr(
        service,
        "persist_subject_paper_matching_profile",
        lambda **kwargs: mirrored.update(kwargs)
        or {"success": True, "subject_concept_id": kwargs["subject_concept_id"]},
    )

    result = service.upsert_paper_recommendation_profile(
        user_concept_id="#V#lu_yunli",
        recommendation_profile={
            "project_description": "  neuro-symbolic discovery  ",
            "stated_interest_terms": [
                "Knowledge Graphs",
                "knowledge graphs",
                " causal reasoning ",
            ],
            "negative_interest_terms": "benchmarking, Benchmarking,  toy tasks ",
            "preferred_authors": " Pearl, pearl, Bengio ",
            "preferred_venues": ["NeurIPS", " neurips ", "Nature Machine Intelligence"],
            "notes": "  favour papers with strong methodological detail ",
        },
    )

    assert result["success"] is True
    assert captured["subject_concept_id"] == "#V#paper_recommendation_profile_for_lu_yunli"
    assert captured["predicate"] == service.PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID

    stored_profile = json.loads(str(captured["text"]))
    assert stored_profile["project_description"] == "neuro-symbolic discovery"
    assert stored_profile["stated_interest_terms"] == [
        "Knowledge Graphs",
        "causal reasoning",
    ]
    assert stored_profile["negative_interest_terms"] == [
        "benchmarking",
        "toy tasks",
    ]
    assert stored_profile["preferred_authors"] == ["Pearl", "Bengio"]
    assert stored_profile["preferred_venues"] == [
        "NeurIPS",
        "Nature Machine Intelligence",
    ]
    assert stored_profile["notes"] == "favour papers with strong methodological detail"
    assert stored_profile["updated_at"]
    assert mirrored["subject_concept_id"] == "#V#lu_yunli"
    assert result["generic_subject_profile"]["success"] is True
