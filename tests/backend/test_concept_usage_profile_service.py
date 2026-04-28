from __future__ import annotations


def test_build_concept_usage_profile_reports_grounded_usage(monkeypatch) -> None:
    from src.backend.services import concept_usage_profile_service as mod

    doc = {
        "concept_id": "#V#thing",
        "name": "Thing",
        "relationships": {
            "is_a_type_of": ["#V#entity"],
            "#V#related_to": ["#V#a", "#V#b"],
        },
    }
    monkeypatch.setattr(mod.ConceptsRepository, "find_one", lambda *_args, **_kwargs: doc)
    monkeypatch.setattr(
        mod,
        "find_relations_with_argument",
        lambda *_args, **_kwargs: {
            "total_hits": 3,
            "hits": [
                {
                    "predicate_concept_id": "is_a_type_of",
                    "argument_indexes": [1],
                },
                {
                    "predicate_concept_id": "#V#related_to",
                    "argument_indexes": [2],
                },
                {
                    "predicate_concept_id": "#V#related_to",
                    "argument_indexes": [1],
                },
            ],
        },
    )
    monkeypatch.setattr(
        mod,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [
            {"predicate": "#V#hasName", "lang": "en-NZ", "text": "Thing"},
            {
                "predicate": "#V#hasDescription",
                "lang": "en-NZ",
                "text": "A represented test concept.",
            },
            {"predicate": "#V#hasName", "lang": "es", "text": "Cosa"},
        ],
    )
    monkeypatch.setattr(
        mod,
        "get_concept_display_name_with_names_fallback",
        lambda _doc: "Thing",
    )

    profile = mod.build_concept_usage_profile(
        concept_id="#V#thing",
        minimum_total_usage=5,
    )

    metrics = profile["usage_metrics"]
    assert profile["success"] is True
    assert metrics["direct_hierarchy_relation_count"] == 1
    assert metrics["direct_non_hierarchy_relation_count"] == 2
    assert metrics["binary_relation_hits_total"] == 3
    assert metrics["binary_outgoing_hits_returned"] == 2
    assert metrics["binary_incoming_hits_returned"] == 1
    assert metrics["description_languages"] == ["en-NZ"]
    assert metrics["name_languages"] == ["en-NZ", "es"]
    assert metrics["total_usage_count"] == 6
    assert profile["meets_minimum_total_usage"] is True


def test_build_concept_usage_profile_reports_missing_concept(monkeypatch) -> None:
    from src.backend.services import concept_usage_profile_service as mod

    monkeypatch.setattr(mod.ConceptsRepository, "find_one", lambda *_args, **_kwargs: None)

    profile = mod.build_concept_usage_profile(concept_id="#V#missing")

    assert profile["success"] is False
    assert profile["error"] == "concept_not_found"
    assert profile["usage_metrics"] == {}
