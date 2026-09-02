from __future__ import annotations

import pytest
from flask import Flask

from src.backend.services import settings_service
from src.backend.services import von_user_authentication_service as service
from src.backend.services.exceptions import MultipleUsersForEmailError


def test_login_lookup_uses_only_narrow_predicate_and_normalised_email(
    monkeypatch,
):
    captured: dict[str, object] = {}

    def find_text_values(query, **_kwargs):
        captured["text_query"] = query
        return [{"_id": "email-text-id"}]

    def find_text_relations(query, **_kwargs):
        captured["relation_query"] = query
        return [{"subject_concept_id": "#V#alice"}]

    monkeypatch.setattr(service.TextValuesRepository, "find", find_text_values)
    monkeypatch.setattr(service.TextRelationsRepository, "find", find_text_relations)
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        lambda query, projection=None: (
            {"concept_id": "#V#alice", "direct_concept_name": "Alice"}
            if query == {"concept_id": "#V#alice"}
            else None
        ),
    )

    user = service.find_user_concept_by_login_email(" Alice@Example.ORG ")

    assert user["concept_id"] == "#V#alice"
    assert captured["text_query"] == {"text": "alice@example.org"}
    assert captured["relation_query"] == {
        "predicate": "#V#hasVonLoginEmail",
        "object_text_id": {"$in": ["email-text-id"]},
    }


def test_generic_has_email_relation_cannot_select_authenticated_actor(
    monkeypatch,
):
    monkeypatch.setattr(
        service.TextValuesRepository,
        "find",
        lambda *_args, **_kwargs: [{"_id": "email-text-id"}],
    )

    def find_relations(query, **_kwargs):
        if query.get("predicate") == "#V#has_email":
            return [{"subject_concept_id": "#V#victim"}]
        return []

    monkeypatch.setattr(service.TextRelationsRepository, "find", find_relations)
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        lambda *_args, **_kwargs: {"concept_id": "#V#victim"},
    )

    assert service.find_user_concept_by_login_email("attacker@example.org") is None


def test_ambiguous_narrow_login_binding_fails_closed(monkeypatch):
    monkeypatch.setattr(
        service.TextValuesRepository,
        "find",
        lambda *_args, **_kwargs: [{"_id": "email-text-id"}],
    )
    monkeypatch.setattr(
        service.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"subject_concept_id": "#V#alice"},
            {"subject_concept_id": "#V#mallory"},
        ],
    )

    with pytest.raises(MultipleUsersForEmailError):
        service.find_user_concept_by_login_email("shared@example.org")


def test_binding_rejects_email_owned_by_another_user(monkeypatch):
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        lambda *_args, **_kwargs: {"concept_id": "#V#alice"},
    )
    monkeypatch.setattr(
        service,
        "list_von_login_email_user_ids",
        lambda _email: ["#V#mallory"],
    )
    monkeypatch.setattr(
        service,
        "upsert_text_for_concept",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("conflicting binding must not write")
        ),
    )

    with pytest.raises(service.LoginEmailBindingConflictError):
        service.bind_von_login_email(
            user_concept_id="#V#alice",
            email="shared@example.org",
        )


def test_binding_writes_only_narrow_predicate_and_reads_back(monkeypatch):
    lookup_count = 0
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        lambda *_args, **_kwargs: {"concept_id": "#V#alice"},
    )

    def list_bindings(_email):
        nonlocal lookup_count
        lookup_count += 1
        return [] if lookup_count == 1 else ["#V#alice"]

    def upsert(**kwargs):
        captured.update(kwargs)
        return {"relation_created": True}

    monkeypatch.setattr(service, "list_von_login_email_user_ids", list_bindings)
    monkeypatch.setattr(service, "upsert_text_for_concept", upsert)

    result = service.bind_von_login_email(
        user_concept_id="#V#alice",
        email="Alice@Example.ORG",
    )

    assert result["success"] is True
    assert captured["predicate"] == "#V#hasVonLoginEmail"
    assert captured["text"] == "alice@example.org"


def test_settings_login_does_not_link_or_create_from_session_hint(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "login-email-test"
    monkeypatch.setattr(
        settings_service,
        "_find_user_concept_by_email",
        lambda _email: None,
    )

    with app.test_request_context("/"):
        from flask import session

        session["user_concept_id"] = "#V#victim"
        result = settings_service.set_current_user_by_email(
            "attacker@example.org",
            "Attacker",
        )

        assert result is None
        assert session["user_concept_id"] == "#V#victim"
