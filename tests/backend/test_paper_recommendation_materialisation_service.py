from __future__ import annotations

import src.backend.services.paper_recommendation_materialisation_service as service


def test_materialise_paper_recommendations_for_subject_persists_ranked_and_inactive(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "_build_subject_bundle",
        lambda _subject_id: {
            "success": True,
            "subject_concept_id": "#V#project_alpha",
            "profile_concept_id": "#V#paper_recommendation_profile_for_project_alpha",
            "profile_source_predicate": "#V#has_paper_matching_profile_json",
            "profile_present": True,
            "related_concepts": [],
            "research_interest_concepts": [],
            "organisation_concept_ids": [],
            "profile": {},
        },
    )
    monkeypatch.setattr(
        service,
        "_score_candidates_with_embeddings",
        lambda **_kwargs: (
            [
                {
                    "paper_concept_id": "#V#paper_new",
                    "embedding_score": 0.88,
                    "status": "scored",
                    "paper_bundle": {
                        "paper_title": "New Paper",
                        "summary_excerpt": "new summary",
                        "author_names": ["A. Author"],
                        "topic_labels": ["Causal AI"],
                        "publication_date": "2026-01-01",
                    },
                },
                {
                    "paper_concept_id": "#V#paper_old",
                    "embedding_score": 0.44,
                    "status": "scored",
                    "paper_bundle": {
                        "paper_title": "Old Paper",
                        "summary_excerpt": "old summary",
                        "author_names": ["B. Author"],
                        "topic_labels": ["Legacy Topic"],
                        "publication_date": "2024-01-01",
                    },
                },
            ],
            {"embedding_client": "FakeClient"},
        ),
    )
    monkeypatch.setattr(
        service,
        "_llm_rerank_candidates",
        lambda **_kwargs: (
            [
                {
                    "paper_concept_id": "#V#paper_new",
                    "score": 0.91,
                    "rationale_summary": "Excellent fit.",
                    "rationale": "Matches the project focus.",
                    "evidence": [{"kind": "topic_fit"}],
                }
            ],
            {"decision_mode": "embedding_plus_llm"},
        ),
    )
    monkeypatch.setattr(
        service,
        "load_materialised_paper_recommendations",
        lambda **_kwargs: {
            "success": True,
            "recommendations": [
                {
                    "paper_concept_id": "#V#paper_old",
                    "evaluation": {
                        "active": True,
                        "score": 0.72,
                        "rationale_summary": "Previously active.",
                    },
                }
            ],
        },
    )

    calls: list[dict[str, object]] = []

    def _fake_upsert(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "assertion_concept_id": f"#V#assertion_{kwargs['paper_concept_id']}",
        }

    monkeypatch.setattr(service, "upsert_paper_recommendation_assertion", _fake_upsert)

    payload = service.materialise_paper_recommendations_for_subject(
        subject_concept_id="#V#project_alpha",
        candidate_paper_concept_ids=["#V#paper_new", "#V#paper_old"],
        max_results=5,
    )

    assert payload["success"] is True
    assert payload["ranked_count"] == 1
    assert payload["results"][0]["paper_concept_id"] == "#V#paper_new"
    assert payload["results"][0]["active"] is True
    assert len(calls) == 2
    assert calls[0]["paper_concept_id"] == "#V#paper_new"
    assert calls[0]["evaluation_payload"]["active"] is True
    assert calls[1]["paper_concept_id"] == "#V#paper_old"
    assert calls[1]["evaluation_payload"]["active"] is False


def test_materialise_paper_recommendations_from_event_resolves_legacy_profile_update(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "resolve_subject_ids_for_legacy_profile_concept",
        lambda _profile_id: ["#V#project_alpha", "#V#org_beta"],
    )

    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        service,
        "materialise_paper_recommendations_for_subject",
        lambda **kwargs: captured.append(kwargs)
        or {"success": True, "subject_concept_id": kwargs["subject_concept_id"]},
    )

    payload = service.materialise_paper_recommendations_from_event(
        event_payload={
            "event_type": "text_relation.upserted",
            "subject_concept_id": "#V#paper_recommendation_profile_for_lu_yunli",
            "predicate": "#V#has_paper_recommendation_profile_json",
        },
        trigger_source="test",
    )

    assert payload["success"] is True
    assert payload["refreshed_subject_count"] == 2
    assert [row["subject_concept_id"] for row in captured] == [
        "#V#project_alpha",
        "#V#org_beta",
    ]


def test_materialise_paper_recommendations_from_event_refreshes_generic_subject_profile(
    monkeypatch,
):
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        service,
        "materialise_paper_recommendations_for_subject",
        lambda **kwargs: captured.append(kwargs)
        or {"success": True, "subject_concept_id": kwargs["subject_concept_id"]},
    )

    payload = service.materialise_paper_recommendations_from_event(
        event_payload={
            "event_type": "text_relation.upserted",
            "subject_concept_id": "#V#project_alpha",
            "predicate": "#V#has_paper_matching_profile_json",
        },
        trigger_source="test",
    )

    assert payload["success"] is True
    assert payload["refreshed_subject_count"] == 1
    assert len(captured) == 1
    assert captured[0]["subject_concept_id"] == "#V#project_alpha"
    assert captured[0]["candidate_paper_concept_ids"] is None
    assert captured[0]["candidate_limit"] == service.DEFAULT_CANDIDATE_RECALL_LIMIT
    assert captured[0]["max_results"] == service.DEFAULT_MAX_RESULTS
    assert captured[0]["include_all_candidates"] is False
    assert captured[0]["trigger_source"] == "test"


def test_materialise_paper_recommendations_from_event_discovers_delivery_subjects(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "list_paper_recommendation_delivery_subject_ids",
        lambda **_kwargs: ["#V#lu_yunli", "#V#michael_witbrock"],
    )

    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        service,
        "materialise_paper_recommendations_for_subject",
        lambda **kwargs: captured.append(kwargs)
        or {"success": True, "subject_concept_id": kwargs["subject_concept_id"]},
    )

    payload = service.materialise_paper_recommendations_from_event(
        event_payload={},
        trigger_source="paper_recommendation_background_schedule",
        discover_subjects_if_missing=True,
    )

    assert payload["success"] is True
    assert payload["triggered"] is True
    assert payload["subject_concept_ids"] == ["#V#lu_yunli", "#V#michael_witbrock"]
    assert [row["subject_concept_id"] for row in captured] == [
        "#V#lu_yunli",
        "#V#michael_witbrock",
    ]


def test_materialise_paper_recommendations_for_subject_uses_bounded_fallback_shortlist(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "_build_subject_bundle",
        lambda _subject_id: {
            "success": True,
            "subject_concept_id": "#V#michael_witbrock",
            "profile_concept_id": "#V#paper_recommendation_profile_for_michael_witbrock",
            "profile_source_predicate": "#V#has_paper_recommendation_profile_json",
            "profile_present": True,
            "related_concepts": [],
            "research_interest_concepts": [],
            "organisation_concept_ids": [],
            "profile": {},
        },
    )
    monkeypatch.setattr(
        service,
        "_score_candidates_with_embeddings",
        lambda **_kwargs: (
            [
                {
                    "paper_concept_id": "#V#paper_1",
                    "embedding_score": 0.43,
                    "status": "scored",
                    "paper_bundle": {"paper_title": "Paper 1", "summary_excerpt": ""},
                },
                {
                    "paper_concept_id": "#V#paper_2",
                    "embedding_score": 0.41,
                    "status": "scored",
                    "paper_bundle": {"paper_title": "Paper 2", "summary_excerpt": ""},
                },
                {
                    "paper_concept_id": "#V#paper_3",
                    "embedding_score": 0.37,
                    "status": "scored",
                    "paper_bundle": {"paper_title": "Paper 3", "summary_excerpt": ""},
                },
                {
                    "paper_concept_id": "#V#paper_4",
                    "embedding_score": 0.18,
                    "status": "scored",
                    "paper_bundle": {"paper_title": "Paper 4", "summary_excerpt": ""},
                },
            ],
            {"embedding_backend": "local_text_hashing_fallback"},
        ),
    )
    monkeypatch.setattr(
        service,
        "_llm_rerank_candidates",
        lambda **_kwargs: (None, {"decision_mode": "embedding_only_fallback"}),
    )
    monkeypatch.setattr(
        service,
        "load_materialised_paper_recommendations",
        lambda **_kwargs: {"success": True, "recommendations": []},
    )

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        service,
        "upsert_paper_recommendation_assertion",
        lambda **kwargs: calls.append(kwargs)
        or {
            "success": True,
            "assertion_concept_id": f"#V#assertion_{kwargs['paper_concept_id']}",
        },
    )

    payload = service.materialise_paper_recommendations_for_subject(
        subject_concept_id="#V#michael_witbrock",
        candidate_paper_concept_ids=[
            "#V#paper_1",
            "#V#paper_2",
            "#V#paper_3",
            "#V#paper_4",
        ],
        max_results=5,
    )

    assert payload["success"] is True
    assert payload["warnings"] == [
        "Authoritative LLM reranking was unavailable; a bounded embedding-only "
        "fallback shortlist was used."
    ]
    assert [row["paper_concept_id"] for row in payload["results"][:3]] == [
        "#V#paper_1",
        "#V#paper_2",
        "#V#paper_3",
    ]
    assert [call["evaluation_payload"]["active"] for call in calls] == [
        True,
        True,
        True,
        False,
    ]
    assert calls[0]["evaluation_payload"]["selection_rule"] == (
        "embedding_only_top_shortlist"
    )


def test_score_candidates_with_embeddings_falls_back_to_local_text_hashing(
    monkeypatch,
):
    monkeypatch.setattr(
        service,
        "_build_embedding_client",
        lambda _subject_id: (_ for _ in ()).throw(RuntimeError("quota exceeded")),
    )
    monkeypatch.setattr(
        service,
        "_build_paper_bundle",
        lambda paper_concept_id: {
            "paper_concept_id": paper_concept_id,
            "paper_title": paper_concept_id,
            "representation_complete": True,
            "representation_failures": [],
            "matching_text": (
                "knowledge reasoning agents"
                if paper_concept_id == "#V#paper_1"
                else "biology microscopy"
            ),
        },
    )

    rows, diagnostics = service._score_candidates_with_embeddings(
        subject_bundle={
            "subject_concept_id": "#V#michael_witbrock",
            "matching_text": "knowledge reasoning agents",
        },
        candidate_ids=["#V#paper_2", "#V#paper_1"],
    )

    assert diagnostics["embedding_backend"] == "local_text_hashing_fallback"
    assert diagnostics["embedding_error"] == "quota exceeded"
    assert rows[0]["paper_concept_id"] == "#V#paper_1"
    assert rows[0]["embedding_score"] > rows[1]["embedding_score"]


def test_llm_rerank_candidates_falls_back_when_generation_errors(monkeypatch):
    monkeypatch.setattr(
        service,
        "_resolve_reranker_prompt",
        lambda: ("Return JSON", {"resolved_prompt_concept_id": "#V#prompt"}),
    )

    class _FakeClient:
        def generate(self, *_args, **_kwargs):
            raise RuntimeError("quota exhausted")

    monkeypatch.setattr(service, "_build_embedding_client", lambda _subject_id: _FakeClient())

    rows, diagnostics = service._llm_rerank_candidates(
        subject_bundle={"subject_concept_id": "#V#michael_witbrock"},
        candidate_rows=[
            {
                "paper_concept_id": "#V#paper_1",
                "embedding_score": 0.4,
                "paper_bundle": {
                    "paper_title": "Paper 1",
                    "summary_excerpt": "summary",
                    "author_names": [],
                    "topic_labels": [],
                    "publication_date": None,
                },
            }
        ],
        max_results=3,
    )

    assert rows is None
    assert diagnostics["decision_mode"] == "embedding_only_fallback"
    assert diagnostics["llm_error"] == "quota exhausted"
