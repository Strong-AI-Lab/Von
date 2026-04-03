from __future__ import annotations

import src.backend.services.paper_recommendation_ranking_service as service


def test_build_paper_recommendations_ranks_grounded_candidates(monkeypatch):
    monkeypatch.setattr(
        service,
        "load_paper_recommendation_profile",
        lambda **_kwargs: {
            "success": True,
            "user_concept_id": "#V#lu_yunli",
            "profile_concept_id": "#V#paper_recommendation_profile_for_lu_yunli",
            "profile": {
                "project_description": "Causal reasoning for scientific discovery.",
                "stated_interest_terms": [
                    "causal reasoning",
                    "scientific discovery",
                ],
                "negative_interest_terms": ["vision benchmark"],
                "preferred_authors": ["Pearl"],
                "preferred_venues": ["NeurIPS"],
                "notes": "Prefer strong explanation.",
            },
            "derived_context": {
                "research_interest_concepts": [
                    {"concept_id": "#V#knowledge_graph", "name": "Knowledge Graph"}
                ]
            },
            "diagnostics": {
                "source_predicate": "#V#has_paper_recommendation_profile_json",
                "profile_exists": True,
            },
        },
    )

    concept_docs = {
        "#V#paper_causal_science": {
            "concept_id": "#V#paper_causal_science",
            "name": "Causal science paper",
            "relationships": {
                "#V#authored_by": ["#V#judea_pearl"],
                "#V#about": ["#V#causal_reasoning", "#V#scientific_discovery"],
            },
        },
        "#V#paper_graph_methods": {
            "concept_id": "#V#paper_graph_methods",
            "name": "Graph methods paper",
            "relationships": {
                "#V#authored_by": ["#V#graph_author"],
                "#V#about": ["#V#knowledge_graph"],
            },
        },
        "#V#paper_sparse_stub": {
            "concept_id": "#V#paper_sparse_stub",
            "name": "Sparse metadata stub",
            "relationships": {},
        },
        "#V#judea_pearl": {"concept_id": "#V#judea_pearl", "name": "Judea Pearl"},
        "#V#graph_author": {"concept_id": "#V#graph_author", "name": "Amara Graph"},
        "#V#causal_reasoning": {
            "concept_id": "#V#causal_reasoning",
            "name": "Causal Reasoning",
        },
        "#V#scientific_discovery": {
            "concept_id": "#V#scientific_discovery",
            "name": "Scientific Discovery",
        },
        "#V#knowledge_graph": {
            "concept_id": "#V#knowledge_graph",
            "name": "Knowledge Graph",
        },
    }

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: concept_docs.get(concept_id),
    )

    paper_rows = {
        "#V#paper_causal_science": [
            {
                "predicate": "hasName",
                "text": "Causal Models for Scientific Discovery",
                "lang": "en-NZ",
                "relation_id": "rel-title-1",
                "context": {"name_type": "NL"},
            },
            {
                "predicate": "hasDescription",
                "text": (
                    "We apply causal reasoning to scientific discovery and "
                    "provide strong explanation for the resulting claims."
                ),
                "lang": "en-NZ",
                "relation_id": "rel-summary-1",
            },
            {
                "predicate": "#V#has_topic_labels",
                "text": "causal reasoning, scientific discovery",
                "lang": "en-NZ",
                "relation_id": "rel-topics-1",
            },
            {
                "predicate": "published_in",
                "text": "NeurIPS",
                "lang": "en-NZ",
                "relation_id": "rel-venue-1",
            },
        ],
        "#V#paper_graph_methods": [
            {
                "predicate": "#V#hasName",
                "text": "Knowledge Graph Tooling for Research Teams",
                "lang": "en-NZ",
                "relation_id": "rel-title-2",
                "context": {"name_type": "NL"},
            },
            {
                "predicate": "#V#hasDescription",
                "text": "A practical survey of knowledge graph tooling and indexing.",
                "lang": "en-NZ",
                "relation_id": "rel-summary-2",
            },
            {
                "predicate": "published_in",
                "text": "Journal of Knowledge Systems",
                "lang": "en-NZ",
                "relation_id": "rel-venue-2",
            },
        ],
        "#V#paper_sparse_stub": [
            {
                "predicate": "hasName",
                "text": "Sparse metadata stub",
                "lang": "en-NZ",
                "relation_id": "rel-title-3",
                "context": {"name_type": "NL"},
            }
        ],
    }

    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda subject_concept_id, predicate=None, limit=200: list(
            paper_rows.get(subject_concept_id, [])
        ),
    )

    payload = service.build_paper_recommendations(
        user_concept_id="#V#lu_yunli",
        candidate_paper_concept_ids=[
            "#V#paper_causal_science",
            "#V#paper_graph_methods",
            "#V#paper_sparse_stub",
        ],
        max_results=5,
    )

    assert payload["success"] is True
    assert payload["ranked_count"] == 2
    assert payload["skipped_count"] == 1
    assert payload["profile_signal_summary"]["usable"] is True

    first_result = payload["results"][0]
    assert first_result["paper_concept_id"] == "#V#paper_causal_science"
    assert first_result["recommendation_tier"] == "strong"
    assert first_result["score"] > 0.7
    assert first_result["provenance"]["author_concept_ids"] == ["#V#judea_pearl"]
    evidence_types = {
        row["evidence_type"] for row in first_result["evidence"] if isinstance(row, dict)
    }
    assert "interest_term_match" in evidence_types
    assert "preferred_author_match" in evidence_types
    assert "preferred_venue_match" in evidence_types

    skipped = next(
        row
        for row in payload["results"]
        if row["paper_concept_id"] == "#V#paper_sparse_stub"
    )
    assert skipped["status"] == "skipped"
    assert skipped["skip_reason"] == "insufficient_paper_representation"
    assert skipped["representation_failures"] == ["insufficient_paper_metadata"]


def test_build_paper_recommendations_fails_on_insufficient_profile_signal(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "load_paper_recommendation_profile",
        lambda **_kwargs: {
            "success": True,
            "user_concept_id": "#V#lu_yunli",
            "profile_concept_id": "#V#paper_recommendation_profile_for_lu_yunli",
            "profile": {
                "project_description": "",
                "stated_interest_terms": [],
                "negative_interest_terms": [],
                "preferred_authors": [],
                "preferred_venues": [],
                "notes": "",
            },
            "derived_context": {"research_interest_concepts": []},
            "diagnostics": {"profile_exists": True},
        },
    )

    payload = service.build_paper_recommendations(
        user_concept_id="#V#lu_yunli",
        candidate_paper_concept_ids=["#V#paper_causal_science"],
    )

    assert payload["success"] is False
    assert payload["error"] == "insufficient_profile_signals"
    assert payload["profile_signal_summary"]["usable"] is False
    assert payload["suggestions"] == [
        "Add stated interest terms or preferred authors/venues to the recommendation profile",
        "Link research-interest concepts to the target person concept",
        "Expand the project description with concrete topic language",
    ]
