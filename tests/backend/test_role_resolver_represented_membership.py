"""Represented organisation roles take precedence over compatibility stubs."""

from __future__ import annotations

from src.backend.security import role_resolver
from src.backend.services import organisation_membership_service


def test_represented_owner_precedes_legacy_admin_stub(monkeypatch) -> None:
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda user_id, organisation_id: {
            "user_concept_id": user_id,
            "organisation_concept_id": organisation_id,
            "role": "owner",
        },
    )

    role = role_resolver.get_user_role(
        "michael_witbrock",
        "university_of_auckland_strong_ai_lab",
    )

    assert role == "owner"
    assert role_resolver.get_effective_permissions(role) == {
        "READ_ORG_CONTENT",
        "WRITE_ORG_CONTENT",
        "MANAGE_MEMBERS",
        "MANAGE_ROLES",
        "DELETE_ORG",
    }


def test_storage_failure_retains_legacy_stub_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )
    monkeypatch.setattr(role_resolver, "_legacy_fallback_allowed", lambda _user: True)

    assert role_resolver.get_user_role(
        "michael_witbrock",
        "university_of_auckland_strong_ai_lab",
    ) == "admin"


def test_migrated_operational_role_retires_stub_during_membership_read_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )
    monkeypatch.setattr(role_resolver, "_legacy_fallback_allowed", lambda _user: False)

    assert role_resolver.get_user_role(
        "michael_witbrock",
        "university_of_auckland_strong_ai_lab",
    ) == role_resolver.DEFAULT_ROLE


def test_authoritative_non_membership_does_not_resurrect_admin_stub(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda *_args: None,
    )

    assert role_resolver.get_user_role(
        "michael_witbrock",
        "university_of_auckland_strong_ai_lab",
    ) == role_resolver.DEFAULT_ROLE


def test_represented_membership_list_precedes_stub(monkeypatch) -> None:
    monkeypatch.setattr(
        organisation_membership_service,
        "get_user_memberships",
        lambda _user_id: {
            "memberships": [
                {
                    "organisation_concept_id": (
                        "#V#university_of_auckland_strong_ai_lab"
                    ),
                    "role": "owner",
                }
            ]
        },
    )

    assert role_resolver.get_all_user_organisations("michael_witbrock") == {
        "university_of_auckland_strong_ai_lab": "owner"
    }


def test_authoritative_empty_membership_list_does_not_resurrect_stub(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        organisation_membership_service,
        "get_user_memberships",
        lambda _user_id: {"memberships": [], "total_memberships": 0},
    )

    assert role_resolver.get_all_user_organisations("michael_witbrock") == {}
