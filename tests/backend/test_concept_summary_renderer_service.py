from __future__ import annotations

import src.backend.services.concept_summary_renderer_service as service


class _FakeSummaryFieldResolver:
    _text_predicates = {
        "description": ("hasDescription", "#V#hasDescription"),
        "content": ("hasContent", "#V#hasContent"),
        "email": ("#V#has_email", "has_email"),
        "event_date": ("#V#date_of_event", "#V#has_start_time"),
    }
    _relationship_predicates = {
        "affiliation": (
            "#V#has_affiliation",
            "#V#member_of_organisation",
            "#V#member_of_faculty",
        ),
        "meeting_participant": ("#V#meeting_participant", "#V#performed_by"),
        "meeting_location": ("#V#meeting_location", "#V#has_location"),
        "meeting_host": ("#V#meeting_host_organisation",),
    }
    _fields_by_type = {
        "#V#person": ("description", "email", "affiliation"),
        "#V#meeting": (
            "description",
            "event_date",
            "meeting_participant",
            "meeting_location",
            "meeting_host",
        ),
        "#V#thing": ("description", "content"),
    }

    def get_text_predicates_for_field(self, field_key: str) -> tuple[str, ...]:
        return self._text_predicates.get(field_key, ())

    def get_relationship_predicates_for_field(self, field_key: str) -> tuple[str, ...]:
        return self._relationship_predicates.get(field_key, ())

    def get_summary_fields_for_types(self, type_ids) -> tuple[str, ...]:
        for type_id in type_ids:
            fields = self._fields_by_type.get(type_id)
            if fields:
                return fields
        return ()


def _install_summary_field_resolver(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "get_concept_summary_field_resolver",
        lambda: _FakeSummaryFieldResolver(),
    )


def test_load_concept_summary_renderer_builds_person_identity_payload(monkeypatch) -> None:
    _install_summary_field_resolver(monkeypatch)
    concept_docs = {
        "#V#ada": {
            "concept_id": "#V#ada",
            "name": "Ada Lovelace",
            "names": [{"name": "Ada Lovelace", "type": "NL"}],
            "relationships": {
                "is_an_instance_of": ["#V#person", "#V#researcher"],
                "#V#member_of_organisation": ["#V#analytical_engine_lab"],
            },
        },
        "#V#person": {
            "concept_id": "#V#person",
            "name": "Person",
            "relationships": {"is_a_type_of": ["#V#thing"]},
        },
        "#V#researcher": {
            "concept_id": "#V#researcher",
            "name": "Researcher",
            "relationships": {"is_a_type_of": ["#V#person"]},
        },
        "#V#thing": {
            "concept_id": "#V#thing",
            "name": "Thing",
            "relationships": {"is_a_type_of": []},
        },
        "#V#analytical_engine_lab": {
            "concept_id": "#V#analytical_engine_lab",
            "name": "Analytical Engine Lab",
            "relationships": {"is_a_type_of": []},
        },
    }

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: concept_docs[concept_id],
    )
    monkeypatch.setattr(service, "enrich_concept_with_text_relations", lambda concept: concept)
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda subject_concept_id, limit=250: [
            {"predicate": "hasDescription", "text": "Mathematician and computing pioneer."},
            {"predicate": "#V#has_email", "text": "ada@example.org"},
        ]
        if subject_concept_id == "#V#ada"
        else [],
    )
    monkeypatch.setattr(
        service,
        "load_renderer_definitions_from_concept_ids",
        lambda concept_ids: (
            [
                {
                    "renderer_id": "#V#concept_page_identity_renderer",
                    "renderer_type": "identity_card",
                    "modalities": ["visual"],
                    "applies_to_object_kinds": ["concept"],
                    "applies_to_concept_type_ids": ["#V#person"],
                    "required_context_tags": ["concept_page_summary"],
                    "priority": 90,
                },
                {
                    "renderer_id": "#V#concept_page_text_summary_renderer",
                    "renderer_type": "text_summary",
                    "modalities": ["textual"],
                    "applies_to_object_kinds": ["concept"],
                    "required_context_tags": ["concept_page_summary"],
                    "priority": 10,
                },
            ],
            {"loaded_definition_count": 2, "requested_concept_ids": list(concept_ids)},
        ),
    )

    payload = service.load_concept_summary_renderer("#V#ada")

    assert payload["selected_renderer"]["renderer_id"] == "#V#concept_page_identity_renderer"
    assert payload["panel"]["variant"] == "identity"
    assert payload["panel"]["summary"] == "Mathematician and computing pioneer."
    assert payload["panel"]["facts"] == [
        {"label": "Email", "value": "ada@example.org"},
        {"label": "Affiliation", "value": "Analytical Engine Lab"},
    ]


def test_load_concept_summary_renderer_uses_ancestor_type_for_event_renderer(
    monkeypatch,
) -> None:
    _install_summary_field_resolver(monkeypatch)
    concept_docs = {
        "#V#meeting_1": {
            "concept_id": "#V#meeting_1",
            "name": "Meeting With Andrew",
            "names": [{"name": "Meeting With Andrew", "type": "NL"}],
            "relationships": {"is_an_instance_of": ["#V#business_meet"]},
            "concept_data": {
                "preserved_fields": {
                    "description": "Business meeting with Andrew during the Auckland trip."
                }
            },
        },
        "#V#business_meet": {
            "concept_id": "#V#business_meet",
            "name": "Business Meet",
            "relationships": {"is_a_type_of": ["#V#meeting"]},
        },
        "#V#meeting": {
            "concept_id": "#V#meeting",
            "name": "Meeting",
            "relationships": {"is_a_type_of": ["#V#social_occasion"]},
        },
        "#V#social_occasion": {
            "concept_id": "#V#social_occasion",
            "name": "Social Occasion",
            "relationships": {"is_a_type_of": []},
        },
    }
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: concept_docs[concept_id],
    )
    monkeypatch.setattr(service, "enrich_concept_with_text_relations", lambda concept: concept)
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda subject_concept_id, limit=250: [],
    )
    monkeypatch.setattr(
        service,
        "load_renderer_definitions_from_concept_ids",
        lambda concept_ids: (
            [
                {
                    "renderer_id": "#V#concept_page_event_renderer",
                    "renderer_type": "event_card",
                    "modalities": ["visual"],
                    "applies_to_object_kinds": ["concept"],
                    "applies_to_concept_type_ids": ["#V#meeting"],
                    "required_context_tags": ["concept_page_summary"],
                    "priority": 80,
                },
                {
                    "renderer_id": "#V#concept_page_text_summary_renderer",
                    "renderer_type": "text_summary",
                    "modalities": ["textual"],
                    "applies_to_object_kinds": ["concept"],
                    "required_context_tags": ["concept_page_summary"],
                    "priority": 10,
                },
            ],
            {"loaded_definition_count": 2, "requested_concept_ids": list(concept_ids)},
        ),
    )

    payload = service.load_concept_summary_renderer("#V#meeting_1")

    assert "#V#meeting" in payload["resolved_type_ids"]
    assert payload["selected_renderer"]["renderer_id"] == "#V#concept_page_event_renderer"
    assert payload["panel"]["variant"] == "event"


def test_load_concept_summary_renderer_falls_back_to_generic_text_summary(
    monkeypatch,
) -> None:
    _install_summary_field_resolver(monkeypatch)
    concept_docs = {
        "#V#artifact_1": {
            "concept_id": "#V#artifact_1",
            "name": "Reference Artefact",
            "names": [{"name": "Reference Artefact", "type": "NL"}],
            "relationships": {"is_an_instance_of": ["#V#thing"]},
        },
        "#V#thing": {
            "concept_id": "#V#thing",
            "name": "Thing",
            "relationships": {"is_a_type_of": []},
        },
    }
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: concept_docs[concept_id],
    )
    monkeypatch.setattr(service, "enrich_concept_with_text_relations", lambda concept: concept)
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda subject_concept_id, limit=250: [
            {"predicate": "hasDescription", "text": "Fallback summary content."}
        ],
    )
    monkeypatch.setattr(
        service,
        "load_renderer_definitions_from_concept_ids",
        lambda concept_ids: (
            [
                {
                    "renderer_id": "#V#concept_page_text_summary_renderer",
                    "renderer_type": "text_summary",
                    "modalities": ["textual"],
                    "applies_to_object_kinds": ["concept"],
                    "required_context_tags": ["concept_page_summary"],
                    "priority": 10,
                }
            ],
            {"loaded_definition_count": 1, "requested_concept_ids": list(concept_ids)},
        ),
    )

    payload = service.load_concept_summary_renderer("#V#artifact_1")

    assert payload["selected_renderer"]["renderer_id"] == "#V#concept_page_text_summary_renderer"
    assert payload["panel"]["variant"] == "generic"
    assert payload["panel"]["summary"] == "Fallback summary content."
