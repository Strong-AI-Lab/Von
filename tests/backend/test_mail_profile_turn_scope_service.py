from __future__ import annotations

from typing import Any

from src.backend.services import mail_profile_turn_scope_service as service


def _authority(*profiles: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "reason_code": "authorised_mail_profiles_resolved",
        "profiles": list(profiles),
    }


def test_turn_scope_intersects_authority_with_runtime_and_uses_default(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "list_authorised_gmail_profiles_for_user",
        lambda **_kwargs: _authority(
            {
                "profile_id": "personal",
                "profile_resource_concept_id": "#V#gmail_profile_personal",
                "is_default": True,
                "represented_identity_concept_ids": ["#V#michael"],
            },
            {
                "profile_id": "lab",
                "profile_resource_concept_id": "#V#gmail_profile_lab",
                "is_default": False,
                "represented_identity_concept_ids": ["#V#strong_ai_lab"],
            },
            {
                "profile_id": "not-configured",
                "profile_resource_concept_id": "#V#gmail_profile_missing",
                "is_default": False,
                "represented_identity_concept_ids": [],
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "load_profiles_from_env",
        lambda: {"personal": object(), "lab": object(), "global": object()},
    )
    monkeypatch.setattr(
        service,
        "list_profile_summaries",
        lambda _profiles: [
            {"profile_id": "personal", "authorised_email": "me@example.test"},
            {"profile_id": "lab", "authorised_email": "lab@example.test"},
            {"profile_id": "global", "authorised_email": "other@example.test"},
        ],
    )

    result = service.build_gmail_profile_turn_scope(user_concept_id="#V#michael")

    assert result["success"] is True
    assert result["profile_id"] == "personal"
    assert result["selection_source"] == "represented_default"
    assert [choice["selector"] for choice in result["choices"]] == [
        "#V#gmail_profile_personal",
        "#V#gmail_profile_lab",
    ]
    assert result["trusted_argument_choice"]["default_selector"] == (
        "#V#gmail_profile_personal"
    )
    assert result["trusted_argument_choice"]["default_selection_source"] == (
        "represented_default"
    )
    assert result["selected_resource_scope"] == {
        "source_family": "gmail",
        "resource_id": "#V#gmail_profile_personal",
        "runtime_alias": "personal",
        "display_label": "me@example.test",
        "selection_source": "represented_default",
    }


def test_turn_scope_does_not_silently_choose_conflicting_defaults(monkeypatch) -> None:
    represented = tuple(
        {
            "profile_id": f"mail-{index}",
            "profile_resource_concept_id": f"#V#gmail_profile_{index}",
            "is_default": index in {0, 1},
            "represented_identity_concept_ids": [],
        }
        for index in range(30)
    )
    monkeypatch.setattr(
        service,
        "list_authorised_gmail_profiles_for_user",
        lambda **_kwargs: _authority(*represented),
    )
    monkeypatch.setattr(
        service,
        "load_profiles_from_env",
        lambda: {f"mail-{index}": object() for index in range(30)},
    )
    monkeypatch.setattr(
        service,
        "list_profile_summaries",
        lambda _profiles: [
            {
                "profile_id": f"mail-{index}",
                "authorised_email": f"mail-{index}@example.test",
            }
            for index in range(30)
        ],
    )

    result = service.build_gmail_profile_turn_scope(user_concept_id="#V#michael")

    assert result["success"] is True
    assert result["profile_id"] is None
    assert result["selection_source"] == "multiple_represented_defaults"
    assert len(result["choices"]) == 30


def test_remembered_authorised_profile_precedes_represented_default(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "list_authorised_gmail_profiles_for_user",
        lambda **_kwargs: _authority(
            {
                "profile_id": "personal",
                "profile_resource_concept_id": "#V#gmail_profile_personal",
                "is_default": True,
                "represented_identity_concept_ids": ["#V#michael"],
            },
            {
                "profile_id": "lab",
                "profile_resource_concept_id": "#V#gmail_profile_lab",
                "is_default": False,
                "represented_identity_concept_ids": ["#V#strong_ai_lab"],
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "load_profiles_from_env",
        lambda: {"personal": object(), "lab": object()},
    )
    monkeypatch.setattr(
        service,
        "list_profile_summaries",
        lambda _profiles: [
            {"profile_id": "personal", "authorised_email": "me@example.test"},
            {"profile_id": "lab", "authorised_email": "lab@example.test"},
        ],
    )

    result = service.build_gmail_profile_turn_scope(
        user_concept_id="#V#michael",
        remembered_resource_scope={
            "source_family": "gmail",
            "resource_id": "#V#gmail_profile_lab",
            "runtime_alias": "lab",
            "display_label": "lab@example.test",
        },
    )

    assert result["success"] is True
    assert result["profile_id"] == "lab"
    assert result["selection_source"] == "conversation_situation"
    assert result["trusted_argument_choice"]["default_selector"] == (
        "#V#gmail_profile_lab"
    )
    assert result["trusted_argument_choice"]["default_selection_source"] == (
        "conversation_situation"
    )
    assert len(result["trusted_argument_choice"]["choices"]) == 2


def test_remembered_stable_resource_survives_runtime_alias_rotation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "list_authorised_gmail_profiles_for_user",
        lambda **_kwargs: _authority(
            {
                "profile_id": "current-runtime-alias",
                "profile_resource_concept_id": "#V#gmail_profile_stable",
                "is_default": False,
                "represented_identity_concept_ids": ["#V#michael"],
            },
            {
                "profile_id": "other",
                "profile_resource_concept_id": "#V#gmail_profile_other",
                "is_default": True,
                "represented_identity_concept_ids": [],
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "load_profiles_from_env",
        lambda: {"current-runtime-alias": object(), "other": object()},
    )
    monkeypatch.setattr(
        service,
        "list_profile_summaries",
        lambda _profiles: [
            {
                "profile_id": "current-runtime-alias",
                "authorised_email": "me@example.test",
            },
            {"profile_id": "other", "authorised_email": "other@example.test"},
        ],
    )

    result = service.build_gmail_profile_turn_scope(
        user_concept_id="#V#michael",
        remembered_resource_scope={
            "source_family": "gmail",
            "resource_id": "#V#gmail_profile_stable",
            "runtime_alias": "retired-runtime-alias",
        },
    )

    assert result["profile_id"] == "current-runtime-alias"
    assert result["selection_source"] == "conversation_situation"


def test_shared_or_stale_remembered_profile_cannot_override_actor_authority(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "list_authorised_gmail_profiles_for_user",
        lambda **_kwargs: _authority(
            {
                "profile_id": "actor-personal",
                "profile_resource_concept_id": "#V#gmail_profile_actor_personal",
                "is_default": True,
                "represented_identity_concept_ids": ["#V#actor"],
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "load_profiles_from_env",
        lambda: {"actor-personal": object(), "owner-personal": object()},
    )
    monkeypatch.setattr(
        service,
        "list_profile_summaries",
        lambda _profiles: [
            {
                "profile_id": "actor-personal",
                "authorised_email": "actor@example.test",
            },
            {
                "profile_id": "owner-personal",
                "authorised_email": "owner@example.test",
            },
        ],
    )

    result = service.build_gmail_profile_turn_scope(
        user_concept_id="#V#actor",
        remembered_resource_scope={
            "source_family": "gmail",
            "resource_id": "#V#gmail_profile_owner_personal",
            "runtime_alias": "owner-personal",
            "display_label": "owner@example.test",
        },
    )

    assert result["success"] is True
    assert result["profile_id"] == "actor-personal"
    assert result["selection_source"] == "represented_default"
    assert result["trusted_argument_choice"]["default_selector"] == (
        "#V#gmail_profile_actor_personal"
    )
    assert [
        choice["value"] for choice in result["trusted_argument_choice"]["choices"]
    ] == ["actor-personal"]


def test_explicit_profile_precedes_memory_and_hard_limits_dispatch_choices(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "list_authorised_gmail_profiles_for_user",
        lambda **_kwargs: _authority(
            {
                "profile_id": "personal",
                "profile_resource_concept_id": "#V#gmail_profile_personal",
                "is_default": True,
                "represented_identity_concept_ids": [],
            },
            {
                "profile_id": "lab",
                "profile_resource_concept_id": "#V#gmail_profile_lab",
                "is_default": False,
                "represented_identity_concept_ids": [],
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "load_profiles_from_env",
        lambda: {"personal": object(), "lab": object()},
    )
    monkeypatch.setattr(
        service,
        "list_profile_summaries",
        lambda _profiles: [
            {"profile_id": "personal", "authorised_email": "me@example.test"},
            {"profile_id": "lab", "authorised_email": "lab@example.test"},
        ],
    )

    result = service.build_gmail_profile_turn_scope(
        user_concept_id="#V#michael",
        requested_profile_id="#V#gmail_profile_personal",
        remembered_resource_scope={
            "source_family": "gmail",
            "resource_id": "#V#gmail_profile_lab",
            "runtime_alias": "lab",
        },
    )

    assert result["profile_id"] == "personal"
    assert result["selection_source"] == "request"
    assert result["trusted_argument_choice"]["default_selector"] == (
        "#V#gmail_profile_personal"
    )
    assert result["trusted_argument_choice"]["default_selection_source"] == "request"
    assert [
        choice["value"] for choice in result["trusted_argument_choice"]["choices"]
    ] == ["personal"]


def test_explicit_foreign_or_unavailable_profile_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "list_authorised_gmail_profiles_for_user",
        lambda **_kwargs: _authority(
            {
                "profile_id": "authorised-but-offline",
                "profile_resource_concept_id": "#V#gmail_profile_offline",
                "is_default": True,
                "represented_identity_concept_ids": [],
            }
        ),
    )
    monkeypatch.setattr(service, "load_profiles_from_env", dict)
    monkeypatch.setattr(service, "list_profile_summaries", lambda _profiles: [])

    unavailable = service.build_gmail_profile_turn_scope(
        user_concept_id="#V#michael",
        requested_profile_id="authorised-but-offline",
    )
    foreign = service.build_gmail_profile_turn_scope(
        user_concept_id="#V#michael",
        requested_profile_id="somebody-elses-mail",
    )

    assert unavailable["reason_code"] == "authorised_mail_profile_unavailable"
    assert foreign["reason_code"] == "mail_profile_not_authorised_for_actor"
