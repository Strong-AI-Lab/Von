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
                    "is_an_instance_of": ["#V#machine_learning_researcher"],
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
    monkeypatch.setattr(
        service,
        "load_concept",
        lambda concept_id: {
            service.PAPER_RECOMMENDATION_PROFILE_TYPE_ID: {
                "concept_id": service.PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
                "relationships": {
                    service.PROFILE_TYPE_SALIENT_TO_PREDICATE_ID: [
                        service.RESEARCHER_TYPE_ID
                    ],
                },
            },
            "#V#machine_learning_researcher": {
                "concept_id": "#V#machine_learning_researcher",
                "relationships": {"is_a_type_of": [service.RESEARCHER_TYPE_ID]},
            },
            service.RESEARCHER_TYPE_ID: {
                "concept_id": service.RESEARCHER_TYPE_ID,
                "relationships": {},
            },
        }.get(concept_id),
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
    monkeypatch.setattr(
        service,
        "load_type_closure",
        lambda _type_ids: {
            "ordered_type_ids": [
                "#V#machine_learning_researcher",
                service.RESEARCHER_TYPE_ID,
            ]
        },
    )

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
    assert payload["profile_applicability"]["is_applicable"] is True
    assert service.RESEARCHER_TYPE_ID in payload["profile_applicability"][
        "inherited_type_ids"
    ]


def test_upsert_paper_recommendation_profile_normalises_fields_before_write(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "resolve_or_create_paper_recommendation_profile_concept_id",
        lambda **_kwargs: "#V#paper_recommendation_profile_for_lu_yunli",
    )
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {
            "concept_id": concept_id,
            "name": "Lu Yunli",
            "relationships": {"is_an_instance_of": ["#V#research_fellow"]},
        },
    )
    monkeypatch.setattr(
        service,
        "load_concept",
        lambda concept_id: {
            service.PAPER_RECOMMENDATION_PROFILE_TYPE_ID: {
                "concept_id": service.PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
                "relationships": {
                    service.PROFILE_TYPE_SALIENT_TO_PREDICATE_ID: [
                        service.RESEARCHER_TYPE_ID
                    ],
                },
            },
            "#V#research_fellow": {
                "concept_id": "#V#research_fellow",
                "relationships": {"is_a_type_of": [service.RESEARCHER_TYPE_ID]},
            },
            service.RESEARCHER_TYPE_ID: {
                "concept_id": service.RESEARCHER_TYPE_ID,
                "relationships": {},
            },
        }.get(concept_id),
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
    monkeypatch.setattr(
        service,
        "load_type_closure",
        lambda _type_ids: {
            "ordered_type_ids": [
                "#V#research_fellow",
                service.RESEARCHER_TYPE_ID,
            ]
        },
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


def test_ensure_primitives_uses_general_profile_has_form_predicate(monkeypatch):
    ensured_types: list[str] = []
    ensured_predicates: list[str] = []
    relationships: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        service,
        "_ensure_type_concept",
        lambda **kwargs: ensured_types.append(kwargs["concept_id"]),
    )
    monkeypatch.setattr(
        service,
        "_ensure_predicate_concept",
        lambda **kwargs: ensured_predicates.append(kwargs["concept_id"]),
    )
    monkeypatch.setattr(
        service,
        "load_concept",
        lambda concept_id: {
            "concept_id": concept_id,
            "relationships": {},
        }
        if concept_id == service.PAPER_RECOMMENDATION_PROFILE_TYPE_ID
        else None,
    )
    monkeypatch.setattr(
        service,
        "add_relationship",
        lambda source_id, predicate, target: relationships.append(
            (source_id, predicate, target)
        ),
    )

    report = service.ensure_paper_recommendation_profile_primitives()

    assert report["success"] is True
    assert service.PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID in ensured_types
    assert service.PROFILE_HAS_FORM_PREDICATE_ID in ensured_predicates
    assert service.PROFILE_TYPE_SALIENT_TO_PREDICATE_ID in ensured_predicates
    assert (
        service.PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
        service.PROFILE_HAS_FORM_PREDICATE_ID,
        service.PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID,
    ) in relationships
    assert (
        service.PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
        service.PROFILE_TYPE_SALIENT_TO_PREDICATE_ID,
        service.RESEARCHER_TYPE_ID,
    ) in relationships


def test_subject_relevant_for_profile_follows_researcher_type_ancestry(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {
            "concept_id": concept_id,
            "relationships": {"is_an_instance_of": ["#V#postdoctoral_researcher"]},
        },
    )
    monkeypatch.setattr(
        service,
        "load_concept",
        lambda concept_id: {
            service.PAPER_RECOMMENDATION_PROFILE_TYPE_ID: {
                "concept_id": service.PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
                "relationships": {
                    service.PROFILE_TYPE_SALIENT_TO_PREDICATE_ID: [
                        service.RESEARCHER_TYPE_ID
                    ],
                },
            },
            "#V#postdoctoral_researcher": {
                "concept_id": "#V#postdoctoral_researcher",
                "relationships": {"is_a_type_of": ["#V#research_staff"]},
            },
            "#V#research_staff": {
                "concept_id": "#V#research_staff",
                "relationships": {"is_a_type_of": [service.RESEARCHER_TYPE_ID]},
            },
            service.RESEARCHER_TYPE_ID: {
                "concept_id": service.RESEARCHER_TYPE_ID,
                "relationships": {},
            },
        }.get(concept_id),
    )
    monkeypatch.setattr(
        service,
        "load_type_closure",
        lambda _type_ids: {
            "ordered_type_ids": [
                "#V#postdoctoral_researcher",
                "#V#research_staff",
                service.RESEARCHER_TYPE_ID,
            ]
        },
    )

    assert (
        service.is_subject_relevant_for_paper_recommendation_profile(
            subject_concept_id="#V#lu_yunli"
        )
        is True
    )


def test_upsert_paper_recommendation_profile_rejects_ineligible_subject(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {
            "concept_id": concept_id,
            "name": "Strong AI Lab",
            "relationships": {"is_an_instance_of": ["#V#organisation"]},
        },
    )
    monkeypatch.setattr(
        service,
        "load_concept",
        lambda concept_id: {
            service.PAPER_RECOMMENDATION_PROFILE_TYPE_ID: {
                "concept_id": service.PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
                "relationships": {
                    service.PROFILE_TYPE_SALIENT_TO_PREDICATE_ID: [
                        service.RESEARCHER_TYPE_ID
                    ],
                },
            },
            "#V#organisation": {
                "concept_id": "#V#organisation",
                "relationships": {"is_a_type_of": ["#V#group"]},
            },
            "#V#group": {
                "concept_id": "#V#group",
                "relationships": {},
            },
        }.get(concept_id),
    )
    monkeypatch.setattr(
        service,
        "load_type_closure",
        lambda _type_ids: {
            "ordered_type_ids": ["#V#organisation", "#V#group"]
        },
    )

    try:
        service.upsert_paper_recommendation_profile(
            subject_concept_id="#V#strong_ai_lab",
            recommendation_profile={"project_description": "Lab-level research"},
        )
    except ValueError as exc:
        assert "researcher-like types" in str(exc)
    else:
        raise AssertionError("Expected ValueError for ineligible profile subject")


def test_load_paper_recommendation_profile_returns_blank_overlay_without_creating_profile(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "resolve_or_create_paper_recommendation_profile_concept_id",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        service,
        "load_concept",
        lambda _concept_id: None,
    )
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {
            "concept_id": concept_id,
            "name": "Strong AI Lab",
            "relationships": {},
        },
    )

    payload = service.load_paper_recommendation_profile(
        subject_concept_id="#V#strong_ai_lab",
        create_if_missing=False,
    )

    assert payload["success"] is True
    assert payload["subject_concept_id"] == "#V#strong_ai_lab"
    assert payload["profile_concept_id"] == (
        "#V#paper_recommendation_profile_for_strong_ai_lab"
    )
    assert payload["profile"]["subject_concept_id"] == "#V#strong_ai_lab"
    assert payload["diagnostics"]["profile_exists"] is False
    assert payload["diagnostics"]["profile_materialised"] is False
    assert payload["profile_applicability"]["is_applicable"] is False
