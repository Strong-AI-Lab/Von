"""Focused Tier-3 checks for represented semantic-authority role lifecycle."""

from __future__ import annotations

from collections.abc import Callable

import mongomock
import pytest
from bson import ObjectId

from src.backend.security.access_control import override_current_actor
from src.backend.services import ontology_authority_role_service as roles
from src.backend.services import ontology_publication_authority_service as authority


def _evidence(
    actor_id: str,
    role: str,
    organisation_id: str | None = None,
) -> authority.AuthorityRoleEvidence:
    return authority.AuthorityRoleEvidence(
        role=role,
        actor_concept_id=actor_id,
        organisation_concept_id=organisation_id,
        relation_id=f"role:{actor_id}:{role}:{organisation_id or 'global'}",
        revision="test",
    )


@pytest.fixture
def role_lifecycle(monkeypatch):
    database = mongomock.MongoClient()["ontology_authority_role_lifecycle"]
    receipts = database["receipts"]
    receipts.create_index("receipt_id", unique=True)
    receipts.create_index(
        [("actor_concept_id", 1), ("idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"idempotency_key": {"$exists": True}},
    )
    monkeypatch.setattr(
        authority, "get_ontology_mutation_receipts_collection", lambda: receipts
    )
    monkeypatch.setattr(authority, "_gateway_actor_trust_source", lambda: None)
    monkeypatch.setattr(roles, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(
        roles,
        "is_user_member_of_organisation",
        lambda _subject_id, _organisation_id: True,
    )

    grants: list[dict[str, object]] = []

    def read_rows(subject_concept_id: str | None = None):
        return [
            dict(item)
            for item in grants
            if subject_concept_id is None
            or item["subject_concept_id"] == subject_concept_id
        ]

    def upsert(**kwargs):
        relation_id = f"role-relation-{len(grants) + 1}"
        grants.append(
            {
                "relation_id": relation_id,
                "subject_concept_id": kwargs["subject_concept_id"],
                "role": kwargs["text"].split("::", 1)[0],
                "organisation_concept_id": kwargs["context"].get(
                    "organisation_concept_id"
                ),
                "context": dict(kwargs["context"]),
                "grant_provenance": dict(
                    kwargs["context"].get("grant_provenance") or {}
                ),
                "updated_at": "test",
                "storage_text": kwargs["text"],
            }
        )
        return {
            "relation_created": True,
            "context_updated": False,
            "relation_id": relation_id,
        }

    def delete(**kwargs):
        relation_id = kwargs["relation_id"]
        before = len(grants)
        grants[:] = [item for item in grants if item["relation_id"] != relation_id]
        return {"deleted": len(grants) != before}

    monkeypatch.setattr(roles, "_role_rows", read_rows)
    monkeypatch.setattr(
        authority,
        "_organisation_authority_root_exists",
        lambda organisation_id: any(
            item["role"] == authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE
            and item.get("organisation_concept_id") == organisation_id
            for item in grants
        ),
    )
    monkeypatch.setattr(roles, "upsert_text_for_concept", upsert)
    monkeypatch.setattr(roles, "delete_text_relation", delete)
    return grants, receipts


def _with_roles(
    monkeypatch,
    role_lookup: Callable[[str], tuple[authority.AuthorityRoleEvidence, ...]],
) -> None:
    monkeypatch.setattr(authority, "resolve_live_semantic_roles", role_lookup)
    monkeypatch.setattr(roles, "resolve_live_semantic_roles", role_lookup)


def test_semantic_role_readers_resolve_string_text_reference_to_object_id(
    monkeypatch,
) -> None:
    relation_id = ObjectId()
    text_id = ObjectId()
    relation = {
        "_id": relation_id,
        "subject_concept_id": "#V#michael",
        "predicate": authority.AUTHORITY_ROLE_PREDICATE,
        "object_text_id": str(text_id),
        "context": {"authority_scope": "global"},
    }
    text_value = {
        "_id": text_id,
        "text": authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    }
    monkeypatch.setattr(
        roles.TextRelationsRepository,
        "find",
        lambda _query: [relation],
    )
    monkeypatch.setattr(
        authority.TextRelationsRepository,
        "find",
        lambda _query: [relation],
    )
    monkeypatch.setattr(
        roles.TextValuesRepository,
        "find_one",
        lambda query, _projection=None: (
            text_value if query.get("_id") == text_id else None
        ),
    )

    read_back = roles.authority_role_read_back(
        subject_concept_id="#V#michael",
        role=authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
        organisation_concept_id=None,
    )
    evidence = authority.resolve_live_semantic_roles("#V#michael")

    assert read_back["active"] is True
    assert read_back["grants"][0]["relation_id"] == str(relation_id)
    assert len(evidence) == 1
    assert evidence[0].relation_id == str(relation_id)


def test_exact_org_admin_grants_and_revokes_only_its_own_org(
    role_lifecycle, monkeypatch
) -> None:
    grants, receipts = role_lifecycle
    admin = "#V#org_a_admin"

    _with_roles(
        monkeypatch,
        lambda actor: (
            (
                _evidence(
                    admin,
                    authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
                    "#V#org_a",
                ),
            )
            if actor == admin
            else ()
        ),
    )

    with override_current_actor(admin, "#V#org_a"):
        granted = roles.grant_ontology_authority_role(
            subject_concept_id="#V#member",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_a",
            request_id="grant-org-a",
        )
        denied = roles.grant_ontology_authority_role(
            subject_concept_id="#V#member",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_b",
            request_id="grant-org-b",
        )
        revoked = roles.revoke_ontology_authority_role(
            subject_concept_id="#V#member",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_a",
            request_id="revoke-org-a",
        )

    assert granted["success"] is True
    assert granted["canonical_read_back"]["active"] is True
    assert granted["authority_receipt"]["actor_concept_id"] == admin
    assert denied["success"] is False
    assert denied["error_code"] == "organisation_ontology_admin_authority_required"
    assert revoked["success"] is True
    assert revoked["canonical_read_back"]["active"] is False
    assert grants == []
    assert receipts.count_documents({}) == 2


def test_global_admin_establishes_only_the_first_organisation_role_root(
    role_lifecycle, monkeypatch
) -> None:
    grants, _receipts = role_lifecycle
    global_admin = "#V#global_admin"
    grants.append(
        {
            "relation_id": "global-root",
            "subject_concept_id": global_admin,
            "role": authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
            "organisation_concept_id": None,
            "context": {"authority_scope": "global"},
            "updated_at": "test",
            "storage_text": authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
        }
    )
    _with_roles(
        monkeypatch,
        lambda actor: (
            (
                _evidence(
                    global_admin,
                    authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
                ),
            )
            if actor == global_admin
            else ()
        ),
    )

    with override_current_actor(global_admin, None):
        global_grant = roles.grant_ontology_authority_role(
            subject_concept_id="#V#other_global_admin",
            role=authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
            request_id="global-grant",
        )
        first_org_root = roles.grant_ontology_authority_role(
            subject_concept_id="#V#org_member",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_a",
            request_id="global-to-org",
        )
        later_org_grant = roles.grant_ontology_authority_role(
            subject_concept_id="#V#other_org_member",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_a",
            request_id="global-to-existing-org",
        )
        global_revoked = roles.revoke_ontology_authority_role(
            subject_concept_id="#V#other_global_admin",
            role=authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
            request_id="global-revoke",
        )

    assert global_grant["success"] is True
    assert global_revoked["success"] is True
    assert global_revoked["canonical_read_back"]["active"] is False
    assert first_org_root["success"] is True
    assert first_org_root["canonical_read_back"]["active"] is True
    assert later_org_grant["success"] is False
    assert (
        later_org_grant["error_code"]
        == "organisation_ontology_admin_authority_required"
    )


def test_roles_for_two_organisations_use_distinct_storage_values(
    role_lifecycle, monkeypatch
) -> None:
    grants, _receipts = role_lifecycle
    admin = "#V#dual_org_admin"
    _with_roles(
        monkeypatch,
        lambda actor: (
            tuple(
                _evidence(
                    admin,
                    authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
                    organisation,
                )
                for organisation in ("#V#org_a", "#V#org_b")
            )
            if actor == admin
            else ()
        ),
    )
    with override_current_actor(admin, "#V#org_a"):
        first = roles.grant_ontology_authority_role(
            subject_concept_id="#V#member",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_a",
            request_id="two-org-a",
        )
    with override_current_actor(admin, "#V#org_b"):
        second = roles.grant_ontology_authority_role(
            subject_concept_id="#V#member",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_b",
            request_id="two-org-b",
        )

    assert first["success"] and second["success"]
    assert {item["storage_text"] for item in grants} == {
        "organisation_ontology_administrator::#V#org_a",
        "organisation_ontology_administrator::#V#org_b",
    }
    assert {item["organisation_concept_id"] for item in grants} == {
        "#V#org_a",
        "#V#org_b",
    }


def test_each_role_relation_keeps_its_exact_grant_provenance(
    role_lifecycle,
    monkeypatch,
) -> None:
    grants, _receipts = role_lifecycle
    admins = {"#V#admin_a", "#V#admin_b"}
    _with_roles(
        monkeypatch,
        lambda actor: (
            (
                _evidence(
                    actor,
                    authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
                    "#V#org_a",
                ),
            )
            if actor in admins
            else ()
        ),
    )

    with override_current_actor("#V#admin_a", "#V#org_a"):
        first = roles.grant_ontology_authority_role(
            subject_concept_id="#V#member_a",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_a",
            request_id="grant-by-a",
            reason="first reason",
        )
    with override_current_actor("#V#admin_b", "#V#org_a"):
        second = roles.grant_ontology_authority_role(
            subject_concept_id="#V#member_b",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_a",
            request_id="grant-by-b",
            reason="second reason",
        )

    assert first["success"] is True
    assert second["success"] is True
    assert [item["grant_provenance"] for item in grants] == [
        {
            "source": "ontology_authority_role_grant",
            "granted_by_actor_concept_id": "#V#admin_a",
            "request_id": "grant-by-a",
            "reason": "first reason",
        },
        {
            "source": "ontology_authority_role_grant",
            "granted_by_actor_concept_id": "#V#admin_b",
            "request_id": "grant-by-b",
            "reason": "second reason",
        },
    ]


def test_member_and_executing_agent_cannot_manage_roles(
    role_lifecycle, monkeypatch
) -> None:
    _grants, _receipts = role_lifecycle
    _with_roles(monkeypatch, lambda _actor: ())

    with override_current_actor("#V#member", "#V#org_a"):
        denied = roles.grant_ontology_authority_role(
            subject_concept_id="#V#target",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_a",
            request_id="member-denied",
        )
    assert denied["success"] is False
    assert denied["error_code"] == "organisation_ontology_admin_authority_required"

    with (
        override_current_actor("#V#admin", "#V#org_a"),
        authority.bind_ontology_invocation(
            surface="test",
            executing_agent_concept_id="#V#agent",
            audience="test",
            effect_id="role-management",
        ),
        pytest.raises(
            PermissionError,
            match="ontology_authority_role_human_administrator_required",
        ),
    ):
        roles.grant_ontology_authority_role(
            subject_concept_id="#V#target",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_a",
            request_id="agent-denied",
        )


def test_organisation_role_grant_requires_live_exact_membership(
    role_lifecycle, monkeypatch
) -> None:
    _grants, _receipts = role_lifecycle
    admin = "#V#org_admin"
    _with_roles(
        monkeypatch,
        lambda actor: (
            (
                _evidence(
                    admin,
                    authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
                    "#V#org_a",
                ),
            )
            if actor == admin
            else ()
        ),
    )
    monkeypatch.setattr(
        roles,
        "is_user_member_of_organisation",
        lambda _subject, _organisation: False,
    )

    with override_current_actor(admin, "#V#org_a"):
        denied = roles.grant_ontology_authority_role(
            subject_concept_id="#V#former_member",
            role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
            organisation_concept_id="#V#org_a",
            request_id="membership-required",
        )

    assert denied["success"] is False
    assert denied["error_code"] == (
        "organisation_membership_required_for_ontology_authority"
    )


def test_last_global_role_is_not_revoked_and_receipt_reads_back_active(
    role_lifecycle, monkeypatch
) -> None:
    grants, receipts = role_lifecycle
    admin = "#V#only_global_admin"
    grants.append(
        {
            "relation_id": "global-role",
            "subject_concept_id": admin,
            "role": authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
            "organisation_concept_id": None,
            "context": {"authority_scope": "global"},
            "updated_at": "test",
            "storage_text": authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
        }
    )
    _with_roles(
        monkeypatch,
        lambda actor: (
            (_evidence(admin, authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE),)
            if actor == admin
            else ()
        ),
    )

    with override_current_actor(admin, None):
        result = roles.revoke_ontology_authority_role(
            subject_concept_id=admin,
            role=authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
            request_id="cannot-remove-root",
        )

    assert result["success"] is False
    assert (
        result["error_code"] == "last_global_ontology_administrator_cannot_be_revoked"
    )
    assert result["canonical_read_back"]["active"] is True
    assert result["authority_receipt"]["status"] == "failed"
    assert receipts.count_documents({}) == 1
