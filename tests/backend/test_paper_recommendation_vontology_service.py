from __future__ import annotations

import src.backend.services.paper_recommendation_vontology_service as service


def test_list_subject_concept_ids_with_paper_matching_profiles_queries_profile_sources(
    monkeypatch,
):
    monkeypatch.setattr(
        service.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"subject_concept_id": "#V#lu_yunli"},
            {"subject_concept_id": "#V#lu_yunli"},
        ],
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [{"concept_id": "#V#michael_witbrock"}],
    )

    assert service.list_subject_concept_ids_with_paper_matching_profiles(limit=10) == [
        "#V#lu_yunli",
        "#V#michael_witbrock",
    ]
