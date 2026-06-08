from __future__ import annotations

from flask import Flask
import pytest


class _FakeConceptCollection:
    def __init__(self, *docs: dict) -> None:
        self.docs = {doc.get("concept_id"): doc for doc in docs}

    def find_one(self, query: dict, projection: dict | None = None):
        concept_id = query.get("concept_id")
        return self.docs.get(concept_id)


def test_validate_person_concept_uses_raw_exact_lookup(monkeypatch) -> None:
    import src.backend.security.access_control as access_control
    import src.backend.services.concept_service as concept_service

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError(
                "identity validation should not use recursive concept resolution"
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(
        concept_service,
        "_find_concept_by_exact_concept_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError(
                "identity validation should not use finalised concept lookup"
            )
        ),
    )
    monkeypatch.setattr(
        concept_service,
        "_find_raw_concept_by_exact_concept_id",
        lambda concept_id: {
            "concept_id": concept_id,
            "relationships": {"is_an_instance_of": ["#V#person"]},
        },
    )

    assert (
        access_control._validate_person_concept("#V#michael_witbrock")
        == "#V#michael_witbrock"
    )


def test_get_effective_user_concept_id_caches_validated_header(monkeypatch) -> None:
    import src.backend.security.access_control as access_control

    app = Flask(__name__)
    calls: list[str] = []

    def _fake_validate(concept_id: str | None) -> str | None:
        if concept_id is None:
            return None
        calls.append(concept_id)
        return concept_id

    monkeypatch.setattr(access_control, "_validate_person_concept", _fake_validate)

    with app.test_request_context(
        "/von/generate",
        headers={"X-User-Concept-ID": "#V#michael_witbrock"},
    ):
        cache_token = access_control._HEADER_CACHE.set(None)
        try:
            assert (
                access_control.get_effective_user_concept_id() == "#V#michael_witbrock"
            )
            assert (
                access_control.get_effective_user_concept_id() == "#V#michael_witbrock"
            )
        finally:
            access_control._HEADER_CACHE.reset(cache_token)

    assert calls == ["#V#michael_witbrock"]


def test_can_access_concept_respects_organisation_visibility(monkeypatch) -> None:
    import src.backend.security.access_control as access_control

    collection = _FakeConceptCollection(
        {
            "concept_id": "#V#team_note",
            "relationships": {"specific_to_org": ["#V#sail"]},
        }
    )
    monkeypatch.setattr(access_control, "get_concepts_collection", lambda: collection)

    with (
        access_control.override_current_user("#V#member"),
        access_control.override_current_organisation("#V#sail"),
    ):
        assert access_control.can_access_concept("#V#team_note") is True

    with (
        access_control.override_current_user("#V#member"),
        access_control.override_current_organisation("#V#other_org"),
    ):
        assert access_control.can_access_concept("#V#team_note") is False


def test_can_access_concept_uses_same_user_or_org_semantics(monkeypatch) -> None:
    import src.backend.security.access_control as access_control

    collection = _FakeConceptCollection(
        {
            "concept_id": "#V#mixed_scope_note",
            "relationships": {
                "specific_to_user": ["#V#owner"],
                "#V#specific_to_organisation": ["#V#sail"],
            },
        }
    )
    monkeypatch.setattr(access_control, "get_concepts_collection", lambda: collection)

    with (
        access_control.override_current_user("#V#member"),
        access_control.override_current_organisation("#V#sail"),
    ):
        assert access_control.can_access_concept("#V#mixed_scope_note") is True

    with (
        access_control.override_current_user("#V#member"),
        access_control.override_current_organisation("#V#other_org"),
    ):
        assert access_control.can_access_concept("#V#mixed_scope_note") is False


def test_visibility_filter_requires_no_user_or_org_restrictions_for_global() -> None:
    import src.backend.security.access_control as access_control
    from src.backend.security.visibility_predicates import (
        CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
        CANONICAL_SPECIFIC_TO_USER_PREDICATE,
        SPECIFIC_TO_ORG_PREDICATES_READ,
        SPECIFIC_TO_USER_PREDICATES,
    )

    with (
        access_control.override_current_user("#V#member"),
        access_control.override_current_organisation("#V#sail"),
    ):
        visibility_filter = access_control.build_visibility_filter()

    assert isinstance(visibility_filter, dict)
    clauses = visibility_filter["$or"]
    unrestricted_clause = clauses[0]
    unrestricted_fields = [
        next(iter(part["$or"][0])) for part in unrestricted_clause["$and"]
    ]
    for predicate in (
        *SPECIFIC_TO_USER_PREDICATES,
        *SPECIFIC_TO_ORG_PREDICATES_READ,
    ):
        assert f"relationships.{predicate}" in unrestricted_fields
    assert {
        f"relationships.{CANONICAL_SPECIFIC_TO_USER_PREDICATE}": {"$in": ["#V#member"]}
    } in clauses
    assert {
        f"relationships.{CANONICAL_SPECIFIC_TO_ORG_PREDICATE}": {"$in": ["#V#sail"]}
    } in clauses


def test_filter_accessible_concept_ids_uses_batch_visibility_semantics(
    monkeypatch,
) -> None:
    mongomock = pytest.importorskip("mongomock")
    import src.backend.security.access_control as access_control

    client = mongomock.MongoClient()
    concepts = client.db.concepts
    concepts.insert_many(
        [
            {"concept_id": "#V#global_note", "relationships": {}},
            {
                "concept_id": "#V#owner_note",
                "relationships": {"specific_to_user": ["#V#owner"]},
            },
            {
                "concept_id": "#V#team_note",
                "relationships": {"specific_to_org": ["#V#sail"]},
            },
            {
                "concept_id": "#V#other_note",
                "relationships": {"specific_to_user": ["#V#other_user"]},
            },
        ]
    )
    monkeypatch.setattr(access_control, "get_concepts_collection", lambda: concepts)

    with access_control.override_current_actor(
        user_concept_id="#V#owner",
        organisation_concept_id="#V#sail",
    ):
        allowed = access_control.filter_accessible_concept_ids(
            [
                "#V#global_note",
                "#V#owner_note",
                "#V#team_note",
                "#V#other_note",
                "#V#missing_note",
            ]
        )

    assert allowed == {"#V#global_note", "#V#owner_note", "#V#team_note"}
