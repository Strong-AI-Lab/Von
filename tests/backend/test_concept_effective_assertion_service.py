from __future__ import annotations

from datetime import datetime, timezone

import mongomock
import pytest

from src.backend.security.access_control import override_current_actor
import src.backend.services.concept_effective_assertion_service as service
import src.backend.services.scoped_assertion_service as scoped_service


def test_load_effective_assertions_projects_scopes_and_batches_names(
    monkeypatch,
) -> None:
    captured: dict = {}

    def _list_page(**kwargs):
        captured["page"] = kwargs
        return {
            "items": [
                {
                    "assertion_id": "ska_personal",
                    "assertion_revision": 1,
                    "subject_concept_id": "#V#event",
                    "predicate": "#V#date_of_event",
                    "object_kind": "text",
                    "object_text": {"text": "June 2026", "language": "en-NZ"},
                    "scope": {"mode": "user"},
                    "status": "asserted",
                    "provenance": {"capability_name": "upsert_scoped_assertion"},
                },
                {
                    "assertion_id": "ska_org",
                    "assertion_revision": 2,
                    "subject_concept_id": "#V#event",
                    "predicate": "#V#event_location",
                    "object_kind": "concept",
                    "object_concept_id": "#V#room",
                    "scope": {
                        "mode": "organisation",
                        "organisation_concept_id": "#V#lab",
                    },
                    "status": "asserted",
                    "provenance": {"capability_name": "upsert_scoped_assertion"},
                },
            ],
            "has_more": True,
            "next_offset": 2,
            "truncated": True,
            "counts_are_lower_bounds": True,
        }

    def _find(filter, projection, *, limit):
        captured["find"] = {
            "filter": filter,
            "projection": projection,
            "limit": limit,
        }
        return [
            {"concept_id": "#V#event", "name": "Research event"},
            {"concept_id": "#V#date_of_event", "name": "Date of event"},
            {"concept_id": "#V#event_location", "name": "Event location"},
            {"concept_id": "#V#room", "name": "Room 1"},
            {"concept_id": "#V#lab", "name": "Example Lab"},
        ]

    monkeypatch.setattr(service, "list_visible_scoped_assertions_page", _list_page)
    monkeypatch.setattr(service.ConceptsRepository, "find", _find)
    monkeypatch.setattr(
        service,
        "resolve_concept_display_names",
        lambda docs: {row["concept_id"]: row["name"] for row in docs},
    )

    result = service.load_concept_effective_assertions(
        concept_id="#V#event",
        limit=2,
        offset=0,
    )

    assert captured["page"] == {
        "argument_concept_id": "#V#event",
        "epistemic_statuses": ("asserted", "tentative"),
        "limit": 2,
        "offset": 0,
    }
    assert captured["find"]["filter"] == {
        "concept_id": {
            "$in": [
                "#V#event",
                "#V#date_of_event",
                "#V#event_location",
                "#V#room",
                "#V#lab",
            ]
        }
    }
    assert captured["find"]["limit"] == 5
    assert result["has_more"] is True
    assert result["next_offset"] == 2
    assert result["counts_are_lower_bounds"] is True
    assert result["items"][0]["predicate"]["display_name"] == "Date of event"
    assert result["items"][0]["object"] == {
        "kind": "text",
        "text": "June 2026",
        "language": "en-NZ",
    }
    assert result["items"][0]["source_context"] == {
        "kind": "personal",
        "label": "Personal — visible only to you",
        "organisation_concept_id": None,
        "organisation_display_name": None,
    }
    assert result["items"][1]["object"]["display_name"] == "Room 1"
    assert result["items"][1]["source_context"]["label"] == (
        "Organisation — Example Lab"
    )
    assert result["items"][0]["subject"] == {
        "concept_id": "#V#event",
        "display_name": "Research event",
    }
    assert result["items"][0]["human_statement"] == (
        "Research event Date of event June 2026"
    )
    assert result["items"][0]["concept_relevance"] == {
        "kind": "grounded_subject",
        "concept_id": "#V#event",
        "aboutness_only": False,
    }


def test_link_only_text_assertion_is_visible_without_becoming_a_relation(
    monkeypatch,
) -> None:
    collection = mongomock.MongoClient().db.scoped_knowledge_assertions
    now = datetime.now(timezone.utc)
    collection.insert_one(
        {
            "_id": "ska_q_and_a",
            "assertion_id": "ska_q_and_a",
            "assertion_revision": 1,
            "assertion_form": scoped_service.STANDALONE_TEXT_ASSERTION_FORM,
            "subject_concept_id": None,
            "predicate": None,
            "object_kind": "text",
            "object_text": {
                "text": "Primary Labs has a chief scientist.",
                "language": "en-NZ",
            },
            "object_concept_id": None,
            "concept_links": [
                {
                    "concept_id": "#V#primary_labs",
                    "role": "about",
                    "status": "active",
                }
            ],
            "scope": {
                "mode": "user",
                "audience_key": "user:#V#owner",
                "audience_keys": ["user:#V#owner"],
            },
            "status": "asserted",
            "canonical_publication": False,
            "created_at": now,
            "updated_at": now,
            "provenance": {"asserted_by_user_concept_id": "#V#owner"},
        }
    )
    monkeypatch.setattr(
        scoped_service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        scoped_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(service.ConceptsRepository, "find", lambda *_a, **_kw: [])

    with override_current_actor("#V#owner", None):
        result = service.load_concept_effective_assertions(
            concept_id="#V#primary_labs"
        )

    assert [item["assertion_id"] for item in result["items"]] == ["ska_q_and_a"]
    assertion = result["items"][0]
    assert assertion["assertion_form"] == scoped_service.STANDALONE_TEXT_ASSERTION_FORM
    assert assertion["subject"]["concept_id"] is None
    assert assertion["predicate"]["concept_id"] is None
    assert assertion["human_statement"] == "Primary Labs has a chief scientist."
    assert assertion["concept_relevance"] == {
        "kind": "aboutness_only",
        "concept_id": "#V#primary_labs",
        "aboutness_only": True,
    }

    with override_current_actor("#V#outsider", None):
        hidden = service.load_concept_effective_assertions(
            concept_id="#V#primary_labs"
        )
    assert hidden["items"] == []


def test_exact_profile_assertion_projects_user_facing_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "get_visible_scoped_assertion_by_id",
        lambda _assertion_id: {
            "assertion_id": "ska_profile",
            "assertion_revision": 2,
            "subject_concept_id": "#V#student",
            "predicate": "#V#has_paper_matching_profile_json",
            "object_kind": "text",
            "object_text": {
                "text": (
                    '{"schema_version":"paper_matching_profile.v1",'
                    '"project_description":"Robust multimodal learning",'
                    '"stated_interest_terms":["multimodal learning"],'
                    '"negative_interest_terms":[],"preferred_authors":[],'
                    '"preferred_venues":["NeurIPS"],"notes":"Derived from papers"}'
                ),
                "language": "en-NZ",
            },
            "scope": {"mode": "organisation", "organisation_concept_id": "#V#lab"},
            "status": "asserted",
            "provenance": {"capability_name": "upsert_scoped_assertion"},
        },
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#student", "name": "Student A"},
            {
                "concept_id": "#V#has_paper_matching_profile_json",
                "name": "Has paper matching profile",
            },
            {"concept_id": "#V#lab", "name": "Strong AI Lab"},
        ],
    )
    monkeypatch.setattr(
        service,
        "resolve_concept_display_names",
        lambda docs: {row["concept_id"]: row["name"] for row in docs},
    )

    result = service.load_effective_assertion_by_id("ska_profile")

    assert result is not None
    assertion = result["assertion"]
    assert assertion["subject"]["display_name"] == "Student A"
    assert assertion["human_statement"] == "Student A has a paper-matching profile."
    assert assertion["presentation"] == {
        "kind": "paper_matching_profile",
        "project_description": "Robust multimodal learning",
        "stated_interest_terms": ["multimodal learning"],
        "negative_interest_terms": [],
        "preferred_authors": [],
        "preferred_venues": ["NeurIPS"],
        "notes": "Derived from papers",
        "updated_at": None,
    }


def test_load_effective_assertions_does_not_query_metadata_for_empty_page(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "list_visible_scoped_assertions_page",
        lambda **_kwargs: {
            "items": [],
            "has_more": False,
            "next_offset": None,
            "truncated": False,
            "counts_are_lower_bounds": False,
        },
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty assertion pages must not query concept metadata")
        ),
    )

    result = service.load_concept_effective_assertions(concept_id="#V#empty")

    assert result["items"] == []
    assert result["returned"] == 0


def test_effective_projection_respects_personal_and_current_org_audiences(
    monkeypatch,
) -> None:
    collection = mongomock.MongoClient().db.scoped_knowledge_assertions
    now = datetime.now(timezone.utc)
    collection.insert_many(
        [
            {
                "_id": "ska_personal",
                "assertion_id": "ska_personal",
                "assertion_revision": 1,
                "subject_concept_id": "#V#event",
                "predicate": "#V#date_of_event",
                "object_kind": "text",
                "object_text": {"text": "June 2026", "language": "en-NZ"},
                "scope": {
                    "mode": "user",
                    "audience_keys": ["user:#V#owner"],
                },
                "status": "asserted",
                "canonical_publication": False,
                "created_at": now,
                "updated_at": now,
                "provenance": {"asserted_by_user_concept_id": "#V#owner"},
            },
            {
                "_id": "ska_org",
                "assertion_id": "ska_org",
                "assertion_revision": 1,
                "subject_concept_id": "#V#event",
                "predicate": "#V#event_location",
                "object_kind": "text",
                "object_text": {"text": "Main office", "language": "en-NZ"},
                "scope": {
                    "mode": "organisation",
                    "organisation_concept_id": "#V#org_a",
                    "audience_keys": ["org:#V#org_a"],
                },
                "status": "asserted",
                "canonical_publication": False,
                "created_at": now,
                "updated_at": now,
                "provenance": {"asserted_by_user_concept_id": "#V#owner"},
            },
        ]
    )
    monkeypatch.setattr(
        scoped_service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        scoped_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )

    with override_current_actor("#V#owner", "#V#org_a"):
        owner = service.load_concept_effective_assertions(concept_id="#V#event")
    with override_current_actor("#V#colleague", "#V#org_a"):
        colleague = service.load_concept_effective_assertions(concept_id="#V#event")
    with override_current_actor("#V#outsider", "#V#org_b"):
        outsider = service.load_concept_effective_assertions(concept_id="#V#event")

    assert {item["assertion_id"] for item in owner["items"]} == {
        "ska_personal",
        "ska_org",
    }
    assert [item["assertion_id"] for item in colleague["items"]] == ["ska_org"]
    assert outsider["items"] == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"concept_id": "not-an-id"}, "exact #V# concept ID"),
        ({"concept_id": "#V#x", "limit": 0}, "limit must be between"),
        ({"concept_id": "#V#x", "limit": 101}, "limit must be between"),
        ({"concept_id": "#V#x", "offset": -1}, "offset must be between"),
    ],
)
def test_load_effective_assertions_rejects_unbounded_page_inputs(
    monkeypatch,
    kwargs,
    message,
) -> None:
    monkeypatch.setattr(
        service,
        "list_visible_scoped_assertions_page",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid inputs must fail before retrieval")
        ),
    )

    with pytest.raises(ValueError, match=message):
        service.load_concept_effective_assertions(**kwargs)
