import json
import pytest
from src.backend.services import login_organisation_preference_service as service


@pytest.fixture
def preferences(monkeypatch):
    mapping = {"work@example.test": "#V#primary", "home@example.test": "#V#household"}
    memberships = {"#V#primary", "#V#household"}
    monkeypatch.setattr(service, "read_preferences", lambda actor: mapping)
    monkeypatch.setattr(service, "resolve_user_organisation_membership",
                        lambda actor, org: {"role": "member"} if org in memberships else None)
    return mapping, memberships


def test_email_change_same_person_resets_cookie_context_and_generation(preferences):
    session = dict(user_concept_id="#V#person", user_email="work@example.test",
                   organisation_concept_id="sail", namespace="wrong", session_id="old")
    first = service.initialise_login_context(session)
    assert first["organisation_concept_id"] == "#V#primary"
    assert session["organisation_concept_id"] == "primary"
    assert "session_id" not in session and "namespace" not in session
    session["organisation_concept_id"] = "deliberate_other_org"
    assert service.initialise_login_context(session) == first
    assert session["organisation_concept_id"] == "deliberate_other_org"
    session["user_email"] = "home@example.test"
    second = service.initialise_login_context(session)
    assert second["generation"] != first["generation"]
    assert session["organisation_concept_id"] == "household"


def test_missing_or_revoked_default_is_personal_never_first_membership(preferences):
    session = dict(user_concept_id="#V#person", user_email="unknown@example.test")
    assert service.initialise_login_context(session)["organisation_concept_id"] is None
    session["user_email"] = "work@example.test"
    before = service.initialise_login_context(session)
    preferences[1].remove("#V#primary")
    after = service.initialise_login_context(session)
    assert after["organisation_concept_id"] is None
    assert after["reason"] == "preferred_membership_unavailable"
    assert after["generation"] != before["generation"]
    assert "organisation_concept_id" not in session


def test_preference_write_cannot_grant_membership_or_bind_email(preferences, monkeypatch):
    monkeypatch.setattr(service, "find_user_concept_by_login_email", lambda email: {"concept_id": "#V#person"})
    with pytest.raises(ValueError, match="membership_required"):
        service.set_login_organisation_preference(user_concept_id="#V#person", email="work@example.test",
                                                 organisation_concept_id="#V#sail", provenance={})
    with pytest.raises(ValueError, match="subject_mismatch"):
        service.set_login_organisation_preference(user_concept_id="#V#other", email="work@example.test",
                                                 organisation_concept_id="#V#household", provenance={})


@pytest.mark.parametrize("rows", [[{"text":"not-json"}], [{"text":"[]"}],
    [{"text":json.dumps({"email": 1})}], [{"text":"{}"}, {"text":"{}"}]])
def test_malformed_or_ambiguous_preferences_fail_closed(monkeypatch, rows):
    monkeypatch.setattr(service, "get_texts_for_concept", lambda *a, **k: rows)
    with pytest.raises(ValueError):
        service.read_preferences("#V#person")
