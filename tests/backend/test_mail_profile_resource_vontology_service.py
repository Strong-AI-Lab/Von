from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services import mail_profile_resource_vontology_service as service


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    yield


def _relationships(concept_id: str) -> dict[str, Any]:
    concept_doc = concept_service.get_concept_by_concept_id(concept_id)
    assert concept_doc is not None
    return dict(concept_doc.get("relationships") or {})


def _targets(concept_id: str, predicate: str) -> set[str]:
    values = _relationships(concept_id).get(predicate) or []
    if isinstance(values, str):
        return {values}
    return {str(value) for value in values}


def _create_user(concept_id: str) -> None:
    concept_service.create_concept(
        name="Test user",
        concept_id=concept_id,
        description="User concept for mail profile resource tests.",
        parent_concept_ids=["#V#abstract_object"],
        create_as_instance=True,
        visibility_scope_mode="global_general",
    )


def test_bootstrap_materialises_mail_profile_resource_vocabulary(
    _reset_mock_db: Any,
) -> None:
    report = service.bootstrap_mail_profile_resource_vocabulary()

    assert report["success"] is True
    assert report["schema_version"] == "mail_profile_resource.v2"
    assert report["errors"] == []

    validation = service.validate_mail_profile_resource_vocabulary()
    assert validation["success"] is True
    assert validation["missing_concept_ids"] == []

    profile_type_targets = _targets(
        service.GMAIL_PROFILE_RESOURCE_TYPE_ID,
        "is_a_type_of",
    )
    assert service.MAIL_PROFILE_RESOURCE_TYPE_ID in profile_type_targets

    predicate_targets = _targets(
        service.HAS_AUTHORISED_MAIL_PROFILE_PREDICATE_ID,
        "is_an_instance_of",
    )
    assert "#V#predicate" in predicate_targets
    assert "#V#binary_predicate" in predicate_targets


def test_materialise_gmail_profile_resources_links_user_profiles_and_aliases(
    _reset_mock_db: Any,
) -> None:
    user_concept_id = "#V#michael_witbrock"
    _create_user(user_concept_id)
    _create_user("#V#zhan_vonwitbrock")

    report = service.materialise_gmail_profile_resources_for_user(
        user_concept_id=user_concept_id,
        profile_ids=["vonwitbrock-gmail", "zhan-gmail"],
        default_profile_id="vonwitbrock-gmail",
        profile_identity_concept_ids={
            "zhan-gmail": "#V#zhan_vonwitbrock",
        },
    )

    assert report["success"] is True
    assert report["errors"] == []
    assert report["counts"]["profile_ids"] == 2

    von_profile = service.gmail_profile_resource_concept_id("vonwitbrock-gmail")
    zhan_profile = service.gmail_profile_resource_concept_id("zhan-gmail")
    von_alias = service.gmail_profile_alias_concept_id("vonwitbrock-gmail")

    authorised_targets = _targets(
        user_concept_id,
        service.HAS_AUTHORISED_MAIL_PROFILE_PREDICATE_ID,
    )
    assert authorised_targets == {von_profile, zhan_profile}

    default_targets = _targets(
        user_concept_id,
        service.HAS_DEFAULT_MAIL_PROFILE_PREDICATE_ID,
    )
    assert default_targets == {von_profile}

    identity_targets = _targets(
        zhan_profile,
        service.MAIL_PROFILE_REPRESENTS_IDENTITY_PREDICATE_ID,
    )
    assert identity_targets == {"#V#zhan_vonwitbrock"}

    alias_targets = _targets(
        von_profile,
        service.HAS_RUNTIME_PROFILE_ALIAS_PREDICATE_ID,
    )
    assert alias_targets == {von_alias}

    alias_doc = concept_service.get_concept_by_concept_id(von_alias)
    assert alias_doc is not None
    alias_attributes = dict(alias_doc.get("attributes") or {})
    assert alias_attributes["runtime_profile_alias"] == "vonwitbrock-gmail"

    profile_doc = concept_service.get_concept_by_concept_id(von_profile)
    assert profile_doc is not None
    profile_attributes = dict(profile_doc.get("attributes") or {})
    assert profile_attributes["runtime_profile_alias"] == "vonwitbrock-gmail"
    assert "token_path" not in profile_attributes
    assert "credentials_path" not in profile_attributes

    authority = service.list_authorised_gmail_profiles_for_user(
        user_concept_id=user_concept_id,
    )
    assert authority == {
        "success": True,
        "reason_code": "authorised_mail_profiles_resolved",
        "profiles": [
            {
                "profile_id": "vonwitbrock-gmail",
                "profile_resource_concept_id": von_profile,
                "is_default": True,
                "represented_identity_concept_ids": [],
            },
            {
                "profile_id": "zhan-gmail",
                "profile_resource_concept_id": zhan_profile,
                "is_default": False,
                "represented_identity_concept_ids": ["#V#zhan_vonwitbrock"],
            },
        ],
    }

    default_resolution = service.resolve_authorised_gmail_profile_for_user(
        user_concept_id=user_concept_id,
    )
    assert default_resolution == {
        "success": True,
        "reason_code": "authorised_mail_profile_resolved",
        "profile_id": "vonwitbrock-gmail",
        "profile_resource_concept_id": von_profile,
        "selection_source": "represented_default",
    }

    requested_resolution = service.resolve_authorised_gmail_profile_for_user(
        user_concept_id=user_concept_id,
        requested_profile_id="zhan-gmail",
    )
    assert requested_resolution["success"] is True
    assert requested_resolution["profile_id"] == "zhan-gmail"
    assert requested_resolution["profile_resource_concept_id"] == zhan_profile
    assert requested_resolution["selection_source"] == "request"

    rejected_resolution = service.resolve_authorised_gmail_profile_for_user(
        user_concept_id=user_concept_id,
        requested_profile_id="somebody-elses-profile",
    )
    assert rejected_resolution == {
        "success": False,
        "reason_code": "mail_profile_not_authorised_for_actor",
        "profile_id": None,
    }
