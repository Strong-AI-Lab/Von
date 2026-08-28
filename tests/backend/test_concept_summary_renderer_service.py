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
        return tuple(
            definition.field_key
            for definition in self.get_summary_field_definitions_for_types(type_ids)
        )

    def get_summary_field_definitions_for_types(self, type_ids):
        field_keys = []
        origins = {}
        for type_id in type_ids:
            for field_key in self._fields_by_type.get(type_id, ()):
                if field_key not in origins:
                    field_keys.append(field_key)
                    origins[field_key] = []
                origins[field_key].append(type_id)
        return tuple(
            service.SummaryFieldDefinition(
                field_key=field_key,
                field_concept_id=f"#V#summary_field_{field_key}",
                label=field_key.replace("_", " ").title(),
                display_order=float(index * 10),
                text_predicates=self.get_text_predicates_for_field(field_key),
                relationship_predicates=(
                    self.get_relationship_predicates_for_field(field_key)
                ),
                origin_type_ids=tuple(origins[field_key]),
            )
            for index, field_key in enumerate(field_keys, start=1)
        )


def _install_summary_field_resolver(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "get_concept_summary_field_resolver",
        lambda: _FakeSummaryFieldResolver(),
    )

    def _type_closure(type_ids):
        direct = list(type_ids)
        queue = list(direct)
        ordered = list(direct)
        seen = set(direct)
        docs = {}
        while queue:
            type_id = queue.pop(0)
            try:
                doc = service.get_concept_by_concept_id(type_id)
            except Exception:
                continue
            docs[type_id] = doc
            for parent_id in (doc.get("relationships") or {}).get("is_a_type_of") or []:
                if parent_id in seen:
                    continue
                seen.add(parent_id)
                ordered.append(parent_id)
                queue.append(parent_id)
        return {
            "ordered_type_ids": ordered,
            "ancestor_type_ids": ordered[len(direct):],
            "documents_by_id": docs,
            "truncated": False,
        }

    monkeypatch.setattr(service, "load_type_closure", _type_closure)
    monkeypatch.setattr(
        service,
        "resolve_concept_display_names",
        lambda docs: {
            doc["concept_id"]: doc.get("name") or doc["concept_id"]
            for doc in docs
        },
    )

    def _find_concepts(filter_value, *_args, **_kwargs):
        ids = (filter_value.get("concept_id") or {}).get("$in") or []
        docs = []
        for concept_id in ids:
            try:
                docs.append(service.get_concept_by_concept_id(concept_id))
            except Exception:
                pass
        return docs

    monkeypatch.setattr(service.ConceptsRepository, "find", _find_concepts)


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


def test_load_concept_summary_renderer_projects_all_values_of_custom_type_field(
    monkeypatch,
) -> None:
    class _PromotedFieldResolver(_FakeSummaryFieldResolver):
        _text_predicates = {
            **_FakeSummaryFieldResolver._text_predicates,
            "research_description": ("#V#has_research_description_as_of",),
        }
        _fields_by_type = {
            **_FakeSummaryFieldResolver._fields_by_type,
            "#V#researcher": (
                "research_description",
                "description",
                "content",
            ),
        }

        def get_summary_field_definitions_for_types(self, type_ids):
            definitions = super().get_summary_field_definitions_for_types(type_ids)
            return tuple(
                service.SummaryFieldDefinition(
                    field_key=definition.field_key,
                    field_concept_id=definition.field_concept_id,
                    label=(
                        "Research description"
                        if definition.field_key == "research_description"
                        else definition.label
                    ),
                    display_order=(
                        5.0
                        if definition.field_key == "research_description"
                        else definition.display_order
                    ),
                    text_predicates=definition.text_predicates,
                    relationship_predicates=definition.relationship_predicates,
                    origin_type_ids=definition.origin_type_ids,
                )
                for definition in definitions
            )

    _install_summary_field_resolver(monkeypatch)
    monkeypatch.setattr(
        service,
        "get_concept_summary_field_resolver",
        lambda: _PromotedFieldResolver(),
    )
    concept_docs = {
        "#V#profile": {
            "concept_id": "#V#profile",
            "name": "Research profile",
            "relationships": {"is_an_instance_of": ["#V#researcher"]},
        },
        "#V#researcher": {
            "concept_id": "#V#researcher",
            "name": "Researcher",
            "relationships": {"is_a_type_of": ["#V#thing"]},
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
    monkeypatch.setattr(
        service,
        "enrich_concept_with_text_relations",
        lambda concept: concept,
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda subject_concept_id, limit=250: [],
    )
    captured_query = {}

    def _promoted_texts(*_args, **kwargs):
        captured_query.update(kwargs)
        return {
            "#V#profile": [
                {
                    "predicate": "#V#has_research_description_as_of",
                    "text": "As of 2026-08-05: first exact long description.",
                    "lang": "en-NZ",
                    "relation_id": "rel-1",
                    "row_kind": "base_text_relation",
                    "provenance": {"source": "canonical"},
                },
                {
                    "predicate": "#V#has_research_description_as_of",
                    "text": "A personal revision that must remain separately labelled.",
                    "lang": "en-NZ",
                    "assertion_id": "assertion-1",
                    "row_kind": "scoped_assertion",
                    "assertion_scope": {"mode": "user"},
                    "provenance": {"source": "conversation"},
                },
            ]
        }

    monkeypatch.setattr(service, "get_texts_for_concepts", _promoted_texts)
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
            {
                "loaded_definition_count": 1,
                "requested_concept_ids": list(concept_ids),
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "list_focal_conversation_backlinks",
        lambda **_kwargs: {},
    )

    payload = service.load_concept_summary_renderer(
        "#V#profile",
        actor_user_id="#V#actor",
        actor_namespace="#V#actor",
    )

    assert captured_query["predicates"] == [
        "#V#has_research_description_as_of"
    ]
    assert captured_query["context_view"] == "actor_effective"
    promoted = payload["panel"]["promoted_fields"]
    assert len(promoted) == 1
    assert promoted[0]["field_key"] == "research_description"
    assert promoted[0]["label"] == "Research description"
    assert [value["text"] for value in promoted[0]["values"]] == [
        "As of 2026-08-05: first exact long description.",
        "A personal revision that must remain separately labelled.",
    ]
    assert promoted[0]["values"][0]["canonical_publication"] is True
    assert promoted[0]["values"][1]["source_context"]["kind"] == "personal"
    assert payload["diagnostics"]["promoted_fields"]["context_view"] == (
        "actor_effective"
    )

    monkeypatch.setattr(
        service,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("temporary promoted-field read failure")
        ),
    )
    degraded_payload = service.load_concept_summary_renderer(
        "#V#profile",
        actor_user_id="#V#actor",
        actor_namespace="#V#actor",
    )
    assert degraded_payload["panel"]["title"] == "Research profile"
    assert degraded_payload["panel"]["promoted_fields"] == []
    assert degraded_payload["panel"]["promoted_fields_status"] == {
        "available": False,
        "message": "Promoted fields are temporarily unavailable.",
    }
    assert degraded_payload["diagnostics"]["promoted_fields"]["available"] is False


def _install_conversation_concept(monkeypatch, *, concept_id="#V#conversation_abc"):
    _install_summary_field_resolver(monkeypatch)
    concept_docs = {
        concept_id: {
            "concept_id": concept_id,
            "relationships": {"is_an_instance_of": ["#V#conversation"]},
            "metadata": {
                "session_id": "session-abc",
                "organisation_concept_id": "#V#hidden_org",
            },
        },
        "#V#conversation": {
            "concept_id": "#V#conversation",
            "name": "Conversation",
            "relationships": {"is_a_type_of": ["#V#thing"]},
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
        lambda requested_id: concept_docs[requested_id],
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
                    "renderer_id": "#V#concept_page_conversation_renderer",
                    "renderer_type": "concept_page_conversation",
                    "modalities": ["visual"],
                    "applies_to_object_kinds": ["concept"],
                    "applies_to_concept_type_ids": ["#V#conversation"],
                    "required_context_tags": ["concept_page_summary"],
                    "priority": 95,
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
    return concept_id


def test_conversation_renderer_is_a_vontology_loaded_profile_candidate() -> None:
    assert (
        "#V#concept_page_conversation_renderer"
        in service.CONCEPT_PAGE_RENDERER_CONCEPT_IDS
    )


def test_load_conversation_renderer_uses_fresh_actor_projection_and_safe_shape(
    monkeypatch,
) -> None:
    concept_id = _install_conversation_concept(monkeypatch)
    captured = {}

    def _projection(**kwargs):
        captured.update(kwargs)
        return {
            "session_id": "session-abc",
            "owner_concept_id": "#V#must_not_escape",
            "namespace": "#V#must_not_escape@org",
            "conversation_projection": {
                "schema_version": "conversation_projection.v1",
                "conversation_concept_id": concept_id,
                "title": "Current conversation title",
                "last_activity_at": "2026-08-22T11:30:00Z",
                "access_mode": "owner",
                "focal_concepts": [
                    {
                        "concept_id": "#V#visible_focus",
                        "display_name": "Visible focus",
                        "type_ids": ["#V#project"],
                        "private_note": "must not escape",
                    }
                ],
                "projection_status": "materialised",
                "open_action": {
                    "kind": "open_conversation",
                    "session_id": "session-abc",
                },
                "transcript": "must not escape",
            },
        }

    monkeypatch.setattr(service, "get_conversation_projection_for_session", _projection)

    payload = service.load_concept_summary_renderer(
        concept_id,
        actor_user_id="#V#actor",
        actor_namespace="#V#actor@lab",
        organisation_concept_id="#V#lab",
    )

    assert captured == {
        "actor_user_id": "#V#actor",
        "session_id": "session-abc",
        "actor_namespace": "#V#actor@lab",
        "organisation_concept_id": "#V#lab",
    }
    assert payload["selected_renderer"]["renderer_id"] == (
        "#V#concept_page_conversation_renderer"
    )
    assert payload["panel"] == {
        "variant": "conversation",
        "eyebrow": "Conversation",
        "title": "Current conversation title",
        "subtitle": "Focused discussion",
        "badges": [],
        "summary": "",
        "facts": [
            {"label": "Last activity", "value": "2026-08-22T11:30:00Z"}
        ],
        "expanded_sections": [],
        "focal_concepts": [
            {
                "concept_id": "#V#visible_focus",
                "display_name": "Visible focus",
                "type_ids": ["#V#project"],
            }
        ],
        "open_action": {"kind": "open_conversation", "session_id": "session-abc"},
    }
    rendered = repr(payload)
    assert "must_not_escape" not in rendered
    assert "transcript" not in rendered
    assert "namespace" not in rendered
    assert "owner_concept_id" not in rendered


def test_load_conversation_renderer_fails_closed_for_projection_identity_mismatch(
    monkeypatch,
) -> None:
    concept_id = _install_conversation_concept(monkeypatch)
    monkeypatch.setattr(
        service,
        "get_conversation_projection_for_session",
        lambda **_kwargs: {
            "conversation_projection": {
                "conversation_concept_id": "#V#conversation_other",
                "title": "Other conversation",
                "access_mode": "owner",
                "focal_concepts": [],
                "projection_status": "materialised",
                "open_action": {
                    "kind": "open_conversation",
                    "session_id": "session-abc",
                },
            }
        },
    )

    try:
        service.load_concept_summary_renderer(
            concept_id,
            actor_user_id="#V#actor",
            actor_namespace="#V#actor",
        )
    except service.ConceptNotFoundError:
        pass
    else:
        raise AssertionError("projection identity mismatch must fail closed")


def test_load_conversation_renderer_requires_authenticated_actor(monkeypatch) -> None:
    concept_id = _install_conversation_concept(monkeypatch)
    monkeypatch.setattr(
        service,
        "get_conversation_projection_for_session",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("anonymous renderer must not query a conversation source")
        ),
    )

    try:
        service.load_concept_summary_renderer(concept_id)
    except service.ConceptNotFoundError:
        pass
    else:
        raise AssertionError("anonymous conversation renderer must fail closed")


def test_anonymous_conversation_fails_closed_when_renderer_profile_is_missing(
    monkeypatch,
) -> None:
    concept_id = _install_conversation_concept(monkeypatch)
    monkeypatch.setattr(
        service,
        "load_renderer_definitions_from_concept_ids",
        lambda concept_ids: (
            [],
            {"loaded_definition_count": 0, "requested_concept_ids": list(concept_ids)},
        ),
    )
    monkeypatch.setattr(
        service,
        "get_conversation_projection_for_session",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("anonymous fallback must not query a conversation source")
        ),
    )

    try:
        service.load_concept_summary_renderer(concept_id)
    except service.ConceptNotFoundError:
        pass
    else:
        raise AssertionError("missing profile must not expose a generic conversation panel")


def test_authenticated_ordinary_concept_gets_safe_conversation_backlinks(
    monkeypatch,
) -> None:
    _install_summary_field_resolver(monkeypatch)
    concept_docs = {
        "#V#project": {
            "concept_id": "#V#project",
            "name": "Project",
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
        lambda requested_id: concept_docs[requested_id],
    )
    monkeypatch.setattr(service, "enrich_concept_with_text_relations", lambda concept: concept)
    monkeypatch.setattr(service, "get_texts_for_concept", lambda **_kwargs: [])
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
    monkeypatch.setattr(
        service,
        "list_focal_conversation_backlinks",
        lambda **_kwargs: {
            "owner_concept_id": "#V#must_not_escape",
            "sessions": [{"session_id": "legacy-must-not-escape"}],
            "conversation_backlinks": {
                "schema_version": "conversation_backlinks.v1",
                "source_concept_id": "#V#project",
                "items": [
                    {
                        "conversation_concept_id": None,
                        "title": "Shared discussion",
                        "last_activity_at": "2026-08-22T10:00:00Z",
                        "access_mode": "shared",
                        "focal_concepts": [
                            {
                                "concept_id": "#V#project",
                                "display_name": "Project",
                                "type_ids": ["#V#thing"],
                            }
                        ],
                        "projection_status": "source_backed",
                        "open_action": {
                            "kind": "open_conversation",
                            "session_id": "shared-session",
                        },
                        "namespace": "#V#must_not_escape",
                    }
                ],
                "more_count": 2,
            },
        },
    )

    payload = service.load_concept_summary_renderer(
        "#V#project",
        actor_user_id="#V#actor",
        actor_namespace="#V#actor",
    )

    assert payload["conversation_backlinks"] == {
        "schema_version": "conversation_backlinks.v1",
        "source_concept_id": "#V#project",
        "items": [
            {
                "schema_version": "conversation_projection.v1",
                "conversation_concept_id": None,
                "title": "Shared discussion",
                "last_activity_at": "2026-08-22T10:00:00Z",
                "access_mode": "shared",
                "focal_concepts": [
                    {
                        "concept_id": "#V#project",
                        "display_name": "Project",
                        "type_ids": ["#V#thing"],
                    }
                ],
                "projection_status": "source_backed",
                "open_action": {
                    "kind": "open_conversation",
                    "session_id": "shared-session",
                },
            }
        ],
        "more_count": 2,
    }
    assert "must_not_escape" not in repr(payload)
    assert "legacy-must-not-escape" not in repr(payload)


def test_anonymous_ordinary_concept_does_not_query_or_return_backlinks(
    monkeypatch,
) -> None:
    _install_summary_field_resolver(monkeypatch)
    concept_docs = {
        "#V#public": {
            "concept_id": "#V#public",
            "name": "Public",
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
        lambda requested_id: concept_docs[requested_id],
    )
    monkeypatch.setattr(service, "enrich_concept_with_text_relations", lambda concept: concept)
    monkeypatch.setattr(service, "get_texts_for_concept", lambda **_kwargs: [])
    monkeypatch.setattr(
        service,
        "load_renderer_definitions_from_concept_ids",
        lambda concept_ids: (
            [],
            {"loaded_definition_count": 0, "requested_concept_ids": list(concept_ids)},
        ),
    )
    monkeypatch.setattr(
        service,
        "list_focal_conversation_backlinks",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("anonymous renderer must not query private backlinks")
        ),
    )

    payload = service.load_concept_summary_renderer("#V#public")

    assert payload["panel"]["variant"] == "generic"
    assert "conversation_backlinks" not in payload


def test_ordinary_summary_survives_typed_chat_history_backlink_failure(
    monkeypatch,
) -> None:
    _install_summary_field_resolver(monkeypatch)
    concept_docs = {
        "#V#project": {
            "concept_id": "#V#project",
            "name": "Project",
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
        lambda requested_id: concept_docs[requested_id],
    )
    monkeypatch.setattr(service, "enrich_concept_with_text_relations", lambda concept: concept)
    monkeypatch.setattr(service, "get_texts_for_concept", lambda **_kwargs: [])
    monkeypatch.setattr(
        service,
        "load_renderer_definitions_from_concept_ids",
        lambda concept_ids: (
            [],
            {"loaded_definition_count": 0, "requested_concept_ids": list(concept_ids)},
        ),
    )
    monkeypatch.setattr(
        service,
        "list_focal_conversation_backlinks",
        lambda **_kwargs: (_ for _ in ()).throw(
            service.ChatHistoryServiceError("chat history unavailable")
        ),
    )

    payload = service.load_concept_summary_renderer(
        "#V#project",
        actor_user_id="#V#actor",
        actor_namespace="#V#actor",
    )

    assert payload["panel"]["variant"] == "generic"
    assert payload["panel"]["title"] == "Project"
    assert "conversation_backlinks" not in payload
