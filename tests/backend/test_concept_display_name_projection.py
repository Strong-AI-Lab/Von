from __future__ import annotations

import pytest


def test_oldest_relation_name_beats_later_generated_duplicate_and_legacy(monkeypatch):
    from src.backend.services import concept_service

    concept_id = "#V#current_uo_asail_ph_d_student"
    monkeypatch.setattr(
        concept_service,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: {
            concept_id: [
                {
                    "text": "Current UoA SAIL PhD Student",
                    "lang": "en-NZ",
                    "context": {"name_type": "NL"},
                    "relation_id": "697848218f11ba4cb9f7fa15",
                },
                {
                    "text": "Current Uo Asail Ph D Student",
                    "lang": "en-NZ",
                    "context": {"name_type": "NL"},
                    "relation_id": "69e95d03937092432f1c7111",
                },
            ]
        },
    )

    resolved = concept_service.resolve_concept_display_names(
        [
            {
                "concept_id": concept_id,
                "name": "Current Uo Asail Ph D Student",
            }
        ],
        preferred_language="en-NZ",
    )

    assert resolved[concept_id] == "Current UoA SAIL PhD Student"


@pytest.mark.parametrize(
    ("preferred_language", "stored_language", "stored_name"),
    [
        ("sl-SI", "sl", "Študentka doktorskega študija"),
        ("mi", "mi", "Te Whare Wānanga"),
        ("zh", "zh", "博士研究生"),
        ("ar", "ar", "طالبة دكتوراه"),
        ("fr", "fr", "E\u0301tudiante en IA"),
    ],
)
def test_relation_backed_name_preserves_unicode_exactly(
    monkeypatch,
    preferred_language,
    stored_language,
    stored_name,
):
    from src.backend.services import concept_service

    concept_id = "#V#doctoral_student"
    monkeypatch.setattr(
        concept_service,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: {
            concept_id: [
                {
                    "text": "Doctoral Student",
                    "lang": "en-NZ",
                    "context": {"name_type": "NL"},
                    "relation_id": "2",
                },
                {
                    "text": stored_name,
                    "lang": stored_language,
                    "context": {"name_type": "NL"},
                    "relation_id": "1",
                },
            ]
        },
    )

    resolved = concept_service.resolve_concept_display_names(
        [{"concept_id": concept_id}],
        preferred_language=preferred_language,
    )

    assert resolved[concept_id] == stored_name
    assert [ord(char) for char in resolved[concept_id]] == [
        ord(char) for char in stored_name
    ]


def test_list_concepts_batches_canonical_instance_and_type_names(monkeypatch):
    from src.backend.services import concept_service, settings_service

    instance_id = "#V#ana_novak"
    type_id = "#V#current_uo_a_ph_d_student"
    instance_doc = {
        "_id": "instance-object-id",
        "concept_id": instance_id,
        "name": "Ana Novak legacy",
        "relationships": {"is_an_instance_of": [type_id]},
    }
    type_doc = {
        "concept_id": type_id,
        "name": "Current Uo A Ph D Student",
    }
    find_calls = []

    def fake_find(_query, projection=None, **_kwargs):
        find_calls.append(projection)
        return [instance_doc] if projection is None else [type_doc]

    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "collection",
        staticmethod(lambda: object()),
    )
    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "count_documents",
        staticmethod(lambda _query: 1),
    )
    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "find",
        staticmethod(fake_find),
    )
    monkeypatch.setattr(
        concept_service,
        "apply_concept_query_filter",
        lambda query: query,
    )
    monkeypatch.setattr(
        concept_service,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: {
            instance_id: [
                {
                    "text": "Ana Š. Novak",
                    "lang": "sl",
                    "context": {"name_type": "NL"},
                    "relation_id": "1",
                }
            ],
            type_id: [
                {
                    "text": "Current UoA PhD Student",
                    "lang": "en-NZ",
                    "context": {"name_type": "NL"},
                    "relation_id": "2",
                }
            ],
        },
    )
    monkeypatch.setattr(settings_service, "get_preferred_language", lambda: "en-NZ")
    monkeypatch.setattr(
        concept_service,
        "get_concept_name_by_id",
        lambda _concept_id: pytest.fail("per-row type lookup should not run"),
    )

    concepts, count = concept_service.list_concepts(
        concept_id=type_id,
        include_descendants=False,
    )

    assert count == 1
    assert len(find_calls) == 2
    assert concepts[0]["name"] == "Ana Š. Novak"
    assert concepts[0]["direct_concept_name"] == "Current UoA PhD Student"


def test_text_relation_enrichment_updates_convenience_name(monkeypatch):
    from src.backend.services import concept_service, text_value_service

    concept_id = "#V#current_uo_a_ph_d_student"
    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: {
            concept_id: [
                {
                    "predicate": "hasName",
                    "text": "Current UoA PhD Student",
                    "lang": "en-NZ",
                    "context": {"name_type": "NL"},
                    "relation_id": "1",
                }
            ]
        },
    )

    enriched = concept_service.enrich_concept_with_text_relations(
        {
            "concept_id": concept_id,
            "name": "Current Uo A Ph D Student",
            "names": [],
        }
    )

    assert enriched["name"] == "Current UoA PhD Student"
