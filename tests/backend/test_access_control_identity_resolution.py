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


def test_header_identity_cache_is_request_local(monkeypatch) -> None:
    import src.backend.security.access_control as access_control

    app = Flask(__name__)
    monkeypatch.setattr(
        access_control,
        "_validate_person_concept",
        lambda concept_id: concept_id,
    )

    with app.test_request_context(
        "/von/generate",
        headers={"X-User-Concept-ID": "#V#actor_a"},
    ):
        assert access_control.get_effective_user_concept_id() == "#V#actor_a"

    with app.test_request_context(
        "/von/generate",
        headers={"X-User-Concept-ID": "#V#actor_b"},
    ):
        assert access_control.get_effective_user_concept_id() == "#V#actor_b"


def test_visibility_evaluator_is_rebuilt_for_each_request_after_revocation(
    monkeypatch,
) -> None:
    import src.backend.security.access_control as access_control
    from flask import session

    app = Flask(__name__)
    app.secret_key = "test-secret"
    collection = _FakeConceptCollection(
        {
            "concept_id": "#V#team_note",
            "relationships": {"specific_to_org": ["#V#sail"]},
        }
    )
    monkeypatch.setattr(access_control, "get_concepts_collection", lambda: collection)

    with app.test_request_context("/first"):
        session["user_concept_id"] = "#V#member"
        session["organisation_concept_id"] = "sail"
        assert access_control.can_access_concept("#V#team_note") is True

    collection.docs["#V#team_note"]["relationships"] = {
        "specific_to_org": ["#V#other_org"]
    }

    with app.test_request_context("/second"):
        session["user_concept_id"] = "#V#member"
        session["organisation_concept_id"] = "sail"
        assert access_control.can_access_concept("#V#team_note") is False


def test_visibility_evaluator_can_be_invalidated_within_one_request(
    monkeypatch,
) -> None:
    import src.backend.security.access_control as access_control
    from flask import session

    app = Flask(__name__)
    app.secret_key = "test-secret"
    collection = _FakeConceptCollection(
        {
            "concept_id": "#V#team_note",
            "relationships": {"specific_to_org": ["#V#sail"]},
        }
    )
    monkeypatch.setattr(access_control, "get_concepts_collection", lambda: collection)

    with app.test_request_context("/same-turn"):
        session["user_concept_id"] = "#V#member"
        session["organisation_concept_id"] = "sail"
        assert access_control.can_access_concept("#V#team_note") is True

        collection.docs["#V#team_note"]["relationships"] = {
            "specific_to_org": ["#V#other_org"]
        }
        assert access_control.can_access_concept("#V#team_note") is True

        access_control.invalidate_current_access_evaluator()
        assert access_control.can_access_concept("#V#team_note") is False


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


def test_window_session_organisation_context_controls_visibility(monkeypatch) -> None:
    mongomock = pytest.importorskip("mongomock")
    import src.backend.security.access_control as access_control
    import src.backend.services.window_session_context_service as window_context
    from src.backend.services.window_session_context_service import (
        WindowSessionContext,
        get_window_session_store,
    )
    from flask import session

    window_context._window_session_store = None
    store = get_window_session_store()
    store.set(
        WindowSessionContext(
            window_session_id="ws_sail",
            user_id="#V#michael_witbrock",
            organisation_concept_id="university_of_auckland_strong_ai_lab",
            namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
            role_in_org="member",
        )
    )

    client = mongomock.MongoClient()
    concepts = client.db.concepts
    concepts.insert_one(
        {
            "concept_id": "#V#von_catalyst_dev_vm_operator_manual",
            "relationships": {
                "#V#specific_to_organisation": [
                    "#V#university_of_auckland_strong_ai_lab"
                ]
            },
        }
    )
    monkeypatch.setattr(access_control, "get_concepts_collection", lambda: concepts)

    app = Flask(__name__)
    app.secret_key = "test-secret"
    with app.test_request_context(
        "/vontology/api/vontology/node_content",
        headers={"X-Von-Window-Session": "ws_sail"},
    ):
        session["user_concept_id"] = "#V#michael_witbrock"
        session["organisation_concept_id"] = "other_org"
        assert (
            access_control.get_effective_organisation_concept_id()
            == "#V#university_of_auckland_strong_ai_lab"
        )
        assert (
            access_control.describe_concept_access(
                "#V#von_catalyst_dev_vm_operator_manual"
            )["accessible"]
            is True
        )
