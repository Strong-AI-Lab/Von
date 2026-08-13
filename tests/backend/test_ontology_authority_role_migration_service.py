"""Focused contract tests for the fixed initial administrator migration."""

from __future__ import annotations

from contextlib import nullcontext

import mongomock
import pytest

from src.backend.services import (
    ontology_authority_role_migration_service as migration,
)

TARGET = migration.ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET
GLOBAL_ROLE = migration.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE
ORG_ROLE = migration.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE


@pytest.fixture(autouse=True)
def isolated_migration_coordination_and_log(monkeypatch):
    """Focused migration tests never touch live locks or migration storage."""

    collection = mongomock.MongoClient()["role_migration_tests"]["migrations"]
    collection.create_index("migration_name", unique=True)
    monkeypatch.setattr(migration, "get_migrations_log_collection", lambda: collection)
    monkeypatch.setattr(
        migration,
        "ontology_authority_membership_mutation_barrier",
        nullcontext,
    )
    yield collection


def _semantic_row(role: str, organisation_id: str | None = None) -> dict:
    return {
        "source": "represented_semantic_authority_role",
        "subject_concept_id": TARGET,
        "role": role,
        "organisation_concept_id": organisation_id,
        "relation_id": f"semantic:{role}:{organisation_id or 'global'}",
    }


def _inventory(
    *,
    semantic_rows: list[dict] | None = None,
    membership_roles: dict[str, str] | None = None,
    extra_legacy_rows: list[dict] | None = None,
    operational_rows: list[dict] | None = None,
) -> dict:
    roles = membership_roles or {"#V#org_a": "admin", "#V#org_b": "member"}
    return {
        "target_concept_id": TARGET,
        "target_exists": True,
        "memberships": [
            {"organisation_concept_id": org_id, "legacy_role": role}
            for org_id, role in sorted(roles.items())
        ],
        "missing_membership_organisations": [],
        "legacy_privileged_role_assignments": [
            {
                "source": "legacy_stub_role_mapping",
                "subject_concept_id": TARGET,
                "role": "admin",
                "organisation_concept_id": "#V#org_a",
                "relation_id": None,
            },
            *(extra_legacy_rows or []),
        ],
        "target_membership_role_assignments": [
            {
                "source": "represented_organisation_membership_role",
                "subject_concept_id": TARGET,
                "role": role,
                "organisation_concept_id": org_id,
                "relation_id": f"legacy:{org_id}:{role}",
            }
            for org_id, role in sorted(roles.items())
        ],
        "semantic_role_assignments": list(semantic_rows or []),
        "operational_role_assignments": list(operational_rows or []),
    }


def test_dry_run_is_fixed_to_michael_and_makes_no_writes(monkeypatch) -> None:
    monkeypatch.setattr(migration, "_collect_inventory", _inventory)
    monkeypatch.setattr(
        migration,
        "bootstrap_first_semantic_authority",
        lambda **_kwargs: pytest.fail("dry-run must not bootstrap"),
    )
    monkeypatch.setattr(
        migration,
        "grant_ontology_authority_role",
        lambda **_kwargs: pytest.fail("dry-run must not grant"),
    )
    monkeypatch.setattr(
        migration,
        "upsert_text_for_concept",
        lambda **_kwargs: pytest.fail("dry-run must not replace legacy roles"),
    )

    plan = migration.plan_ontology_authority_role_migration()

    assert plan["target_concept_id"] == TARGET
    assert plan["ready_to_apply"] is True
    assert [item["operation"] for item in plan["actions"]] == [
        "bootstrap_von_operational_administrator",
        "bootstrap_global_ontology_administrator",
        "grant_organisation_ontology_administrator",
        "grant_organisation_ontology_administrator",
        "replace_legacy_organisation_role_with_owner",
        "replace_legacy_organisation_role_with_owner",
    ]
    assert plan["operational_authority"]["represented_assignment_mechanism"] == (
        "dedicated_role_lifecycle"
    )
    assert plan["operational_authority"]["kept_separate_from_semantic_authority"]
    assert set(plan["operational_authority"]["owner_permissions"]) == {
        "READ_ORG_CONTENT",
        "WRITE_ORG_CONTENT",
        "MANAGE_MEMBERS",
        "MANAGE_ROLES",
        "DELETE_ORG",
    }


def test_inventory_uses_represented_memberships_and_ignores_ordinary_members(
    monkeypatch,
) -> None:
    text_values = {
        "michael-admin": {"text": "admin"},
        "archivist-member": {"text": "member"},
    }
    relations = [
        {
            "_id": "michael-role",
            "subject_concept_id": TARGET,
            "predicate": migration.ROLE_PREDICATE,
            "object_text_id": "michael-admin",
            "context": {"organisation_id": "#V#org_a"},
        },
        {
            "_id": "archivist-role",
            "subject_concept_id": "#V#von_archivist",
            "predicate": migration.ROLE_PREDICATE,
            "object_text_id": "archivist-member",
            "context": {"organisation_id": "#V#org_a"},
        },
    ]

    monkeypatch.setattr(migration, "bypass_access_control", nullcontext)
    monkeypatch.setattr(
        migration.ConceptsRepository,
        "find_one",
        lambda query, projection=None: {"concept_id": query["concept_id"]},
    )

    def find_concepts(query, projection=None):
        query_text = repr(query)
        assert migration.VON_ADMINISTRATOR_CONCEPT_ID in query_text
        return []

    monkeypatch.setattr(migration.ConceptsRepository, "find", find_concepts)
    monkeypatch.setattr(
        migration.TextRelationsRepository,
        "find",
        lambda query: [
            row
            for row in relations
            if query.get("predicate") == migration.ROLE_PREDICATE
            and (
                not query.get("subject_concept_id")
                or row["subject_concept_id"] == query["subject_concept_id"]
            )
        ],
    )
    monkeypatch.setattr(
        migration.TextValuesRepository,
        "find_one",
        lambda query: text_values.get(query.get("_id")),
    )
    monkeypatch.setattr(
        migration,
        "get_user_memberships",
        lambda _target: {
            "memberships": [
                {"organisation_concept_id": "#V#org_a", "role": "admin"}
            ]
        },
    )
    monkeypatch.setattr(migration, "list_von_operational_administrators", list)

    inventory = migration._collect_inventory()

    assert inventory["memberships"] == [
        {"organisation_concept_id": "#V#org_a", "legacy_role": "admin"}
    ]
    assert {
        row["subject_concept_id"]
        for row in inventory["legacy_privileged_role_assignments"]
    } == {TARGET}
    assert inventory["target_membership_role_assignments"][0]["role"] == "admin"


def test_unexpected_privileged_holder_blocks_before_first_write(monkeypatch) -> None:
    inventory = _inventory(
        extra_legacy_rows=[
            {
                "source": "represented_legacy_text_role",
                "subject_concept_id": "#V#other_person",
                "role": "owner",
                "organisation_concept_id": "#V#org_a",
                "relation_id": "unexpected",
            }
        ]
    )
    monkeypatch.setattr(migration, "_collect_inventory", lambda: inventory)
    monkeypatch.setattr(migration, "get_effective_user_concept_id", lambda: TARGET)
    monkeypatch.setattr(migration, "current_ontology_invocation", lambda: None)
    monkeypatch.setattr(
        migration, "ontology_mutation_resource_lock", lambda _key: nullcontext()
    )
    monkeypatch.setattr(
        migration,
        "bootstrap_first_semantic_authority",
        lambda **_kwargs: pytest.fail("conflicting inventory must not write"),
    )

    plan = migration.plan_ontology_authority_role_migration()
    assert plan["ready_to_apply"] is False
    assert any(
        item["reason_code"] == "unexpected_privileged_role_holder"
        for item in plan["conflicts"]
    )
    with pytest.raises(
        migration.OntologyAuthorityRoleMigrationError,
        match="contradicts the migration premise",
    ) as raised:
        migration.apply_ontology_authority_role_migration(
            expected_inventory_fingerprint=plan["inventory_fingerprint"]
        )
    assert raised.value.reason_code == (
        "ontology_authority_role_migration_inventory_conflict"
    )


def test_unexpected_represented_operational_holder_blocks_plan(monkeypatch) -> None:
    inventory = _inventory(
        operational_rows=[
            {"subject_concept_id": "#V#other_person", "relation_id": "operator:other"}
        ]
    )
    monkeypatch.setattr(migration, "_collect_inventory", lambda: inventory)

    plan = migration.plan_ontology_authority_role_migration()

    assert plan["ready_to_apply"] is False
    assert any(
        item["reason_code"] == "unexpected_von_operational_administrator"
        for item in plan["conflicts"]
    )


def test_apply_is_exact_read_back_verified_and_idempotent(monkeypatch) -> None:
    membership_roles = {"#V#org_a": "admin", "#V#org_b": "member"}
    semantic_rows: list[dict] = []
    operational_rows: list[dict] = []
    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        migration,
        "_collect_inventory",
        lambda: _inventory(
            semantic_rows=semantic_rows,
            membership_roles=membership_roles,
            operational_rows=operational_rows,
        ),
    )
    monkeypatch.setattr(migration, "get_effective_user_concept_id", lambda: TARGET)
    monkeypatch.setattr(migration, "current_ontology_invocation", lambda: None)
    monkeypatch.setattr(
        migration, "ontology_mutation_resource_lock", lambda _key: nullcontext()
    )
    monkeypatch.setattr(
        migration, "override_current_organisation", lambda _org: nullcontext()
    )

    def bootstrap(**kwargs):
        assert kwargs["subject_concept_id"] == TARGET
        semantic_rows.append(_semantic_row(GLOBAL_ROLE))
        calls.append((GLOBAL_ROLE, None))
        return {"first_grant": kwargs}

    def bootstrap_operational(**kwargs):
        assert kwargs["subject_concept_id"] == TARGET
        operational_rows.append(
            {"subject_concept_id": TARGET, "relation_id": "operational:target"}
        )
        calls.append((migration.VON_OPERATIONAL_ADMINISTRATOR_ROLE, None))
        return {"success": True, "changed": True}

    def grant(**kwargs):
        assert kwargs["subject_concept_id"] == TARGET
        organisation_id = kwargs["organisation_concept_id"]
        semantic_rows.append(_semantic_row(ORG_ROLE, organisation_id))
        calls.append((ORG_ROLE, organisation_id))
        return {"success": True, "changed": True, "relation_id": "role"}

    def read_back(*, role, organisation_concept_id, **_kwargs):
        matching = [
            row
            for row in semantic_rows
            if row["role"] == role
            and row["organisation_concept_id"] == organisation_concept_id
        ]
        return {"active": bool(matching), "grants": matching}

    def replace(organisation_id):
        calls.append(("owner", organisation_id))
        membership_roles[organisation_id] = "owner"
        return {
            "success": True,
            "changed": True,
            "canonical_read_back": {
                "exact_owner": True,
                "organisation_concept_id": organisation_id,
            },
        }

    monkeypatch.setattr(migration, "bootstrap_first_semantic_authority", bootstrap)
    monkeypatch.setattr(
        migration,
        "bootstrap_first_von_operational_administrator",
        bootstrap_operational,
    )
    monkeypatch.setattr(migration, "grant_ontology_authority_role", grant)
    monkeypatch.setattr(migration, "authority_role_read_back", read_back)
    monkeypatch.setattr(
        migration,
        "von_operational_administrator_read_back",
        lambda **_kwargs: {
            "active": bool(operational_rows),
            "grants": list(operational_rows),
            "subject_concept_id": TARGET,
        },
    )
    monkeypatch.setattr(migration, "_replace_membership_role_with_owner", replace)
    monkeypatch.setattr(
        migration,
        "_membership_role_read_back",
        lambda organisation_id: {
            "exact_owner": membership_roles[organisation_id] == "owner",
            "organisation_concept_id": organisation_id,
        },
    )

    before = migration.plan_ontology_authority_role_migration()
    applied = migration.apply_ontology_authority_role_migration(
        expected_inventory_fingerprint=before["inventory_fingerprint"]
    )

    assert applied["success"] is True
    assert applied["changed"] is True
    assert calls == [
        (migration.VON_OPERATIONAL_ADMINISTRATOR_ROLE, None),
        (GLOBAL_ROLE, None),
        (ORG_ROLE, "#V#org_a"),
        (ORG_ROLE, "#V#org_b"),
        ("owner", "#V#org_a"),
        ("owner", "#V#org_b"),
    ]
    assert {row["subject_concept_id"] for row in semantic_rows} == {TARGET}

    calls.clear()
    repeated = migration.apply_ontology_authority_role_migration(
        expected_inventory_fingerprint=before["inventory_fingerprint"]
    )
    assert repeated["success"] is True
    assert repeated["changed"] is False
    assert repeated["replayed"] is True
    assert calls == []

    # A later, intentional role change is external state, not an invitation for
    # an old completed request to re-elevate the user.
    membership_roles["#V#org_a"] = "member"
    with pytest.raises(migration.OntologyAuthorityRoleMigrationError) as changed:
        migration.apply_ontology_authority_role_migration(
            expected_inventory_fingerprint=before["inventory_fingerprint"]
        )
    assert changed.value.reason_code.endswith("completed_state_changed")
    assert calls == []


def test_apply_requires_current_inventory_and_target_human(monkeypatch) -> None:
    monkeypatch.setattr(migration, "_collect_inventory", _inventory)
    monkeypatch.setattr(
        migration, "ontology_mutation_resource_lock", lambda _key: nullcontext()
    )
    plan = migration.plan_ontology_authority_role_migration()

    monkeypatch.setattr(
        migration, "get_effective_user_concept_id", lambda: "#V#other_person"
    )
    with pytest.raises(migration.OntologyAuthorityRoleMigrationError) as actor_error:
        migration.apply_ontology_authority_role_migration(
            expected_inventory_fingerprint=plan["inventory_fingerprint"]
        )
    assert actor_error.value.reason_code.endswith("target_actor_required")

    monkeypatch.setattr(migration, "get_effective_user_concept_id", lambda: None)
    with pytest.raises(migration.OntologyAuthorityRoleMigrationError) as anonymous:
        migration.apply_ontology_authority_role_migration(
            expected_inventory_fingerprint=plan["inventory_fingerprint"]
        )
    assert anonymous.value.reason_code.endswith("target_actor_required")

    monkeypatch.setattr(migration, "get_effective_user_concept_id", lambda: TARGET)
    monkeypatch.setattr(migration, "current_ontology_invocation", lambda: None)
    with pytest.raises(migration.OntologyAuthorityRoleMigrationError) as stale_error:
        migration.apply_ontology_authority_role_migration(
            expected_inventory_fingerprint="stale"
        )
    assert stale_error.value.reason_code.endswith("inventory_changed")


def test_owner_replacement_writes_owner_before_removing_old_roles(monkeypatch) -> None:
    rows = [
        {
            "subject_concept_id": TARGET,
            "role": "admin",
            "organisation_concept_id": "#V#org_a",
            "relation_id": "old-admin",
        }
    ]
    call_order: list[str] = []
    monkeypatch.setattr(
        migration, "_target_membership_role_assignments", lambda: list(rows)
    )
    monkeypatch.setattr(
        migration, "override_current_organisation", lambda _org: nullcontext()
    )

    def upsert(**kwargs):
        assert kwargs["text"] == "owner::#V#org_a"
        call_order.append("owner-written")
        rows.append(
            {
                "subject_concept_id": TARGET,
                "role": "owner",
                "organisation_concept_id": "#V#org_a",
                "relation_id": "new-owner",
            }
        )
        return {"relation_id": "new-owner"}

    def delete(**kwargs):
        assert call_order == ["owner-written"]
        call_order.append("old-role-deleted")
        rows[:] = [row for row in rows if row["relation_id"] != kwargs["relation_id"]]
        return {"deleted": True}

    monkeypatch.setattr(migration, "upsert_text_for_concept", upsert)
    monkeypatch.setattr(migration, "delete_text_relation", delete)

    result = migration._replace_membership_role_with_owner("#V#org_a")

    assert result["success"] is True
    assert result["canonical_read_back"]["exact_owner"] is True
    assert result["canonical_read_back"]["permissions"] == sorted(
        migration.get_effective_permissions("owner")
    )
    assert call_order == ["owner-written", "old-role-deleted"]


def test_owner_replacement_reconciles_duplicate_owner_rows(monkeypatch) -> None:
    rows = [
        {
            "subject_concept_id": TARGET,
            "role": "owner",
            "organisation_concept_id": "#V#org_a",
            "relation_id": "legacy-owner",
        },
        {
            "subject_concept_id": TARGET,
            "role": "owner",
            "organisation_concept_id": "#V#org_a",
            "relation_id": "canonical-owner",
        },
    ]
    monkeypatch.setattr(
        migration, "_target_membership_role_assignments", lambda: list(rows)
    )
    monkeypatch.setattr(
        migration, "override_current_organisation", lambda _org: nullcontext()
    )
    monkeypatch.setattr(
        migration,
        "upsert_text_for_concept",
        lambda **_kwargs: {"relation_id": "canonical-owner"},
    )

    def delete(**kwargs):
        rows[:] = [row for row in rows if row["relation_id"] != kwargs["relation_id"]]
        return {"deleted": True}

    monkeypatch.setattr(migration, "delete_text_relation", delete)

    result = migration._replace_membership_role_with_owner("#V#org_a")

    assert result["canonical_read_back"]["exact_owner"] is True
    assert result["deleted_relation_ids"] == ["legacy-owner"]


def test_raw_partial_failure_reports_effects_and_resumes_same_fingerprint(
    monkeypatch,
) -> None:
    membership_roles = {"#V#org_a": "admin"}
    semantic_rows: list[dict] = []
    operational_rows: list[dict] = []
    calls: list[str] = []
    fail_grant = True

    monkeypatch.setattr(
        migration,
        "_collect_inventory",
        lambda: _inventory(
            semantic_rows=semantic_rows,
            membership_roles=membership_roles,
            operational_rows=operational_rows,
        ),
    )
    monkeypatch.setattr(migration, "get_effective_user_concept_id", lambda: TARGET)
    monkeypatch.setattr(migration, "current_ontology_invocation", lambda: None)
    monkeypatch.setattr(
        migration, "ontology_mutation_resource_lock", lambda _key: nullcontext()
    )
    monkeypatch.setattr(
        migration, "override_current_organisation", lambda _org: nullcontext()
    )

    def bootstrap_operational(**_kwargs):
        calls.append("operational")
        operational_rows.append(
            {"subject_concept_id": TARGET, "relation_id": "operational:target"}
        )
        return {"success": True, "changed": True}

    def bootstrap_semantic(**_kwargs):
        calls.append("global")
        semantic_rows.append(_semantic_row(GLOBAL_ROLE))
        return {"changed": True}

    def grant(**kwargs):
        calls.append("org")
        if fail_grant:
            raise RuntimeError("simulated write interruption")
        semantic_rows.append(_semantic_row(ORG_ROLE, kwargs["organisation_concept_id"]))
        return {"success": True, "changed": True}

    def replace(organisation_id):
        calls.append("owner")
        membership_roles[organisation_id] = "owner"
        return {
            "success": True,
            "changed": True,
            "canonical_read_back": {
                "exact_owner": True,
                "organisation_concept_id": organisation_id,
            },
        }

    monkeypatch.setattr(
        migration,
        "bootstrap_first_von_operational_administrator",
        bootstrap_operational,
    )
    monkeypatch.setattr(migration, "bootstrap_first_semantic_authority", bootstrap_semantic)
    monkeypatch.setattr(migration, "grant_ontology_authority_role", grant)
    monkeypatch.setattr(migration, "_replace_membership_role_with_owner", replace)
    monkeypatch.setattr(
        migration,
        "von_operational_administrator_read_back",
        lambda **_kwargs: {"active": True, "grants": list(operational_rows)},
    )
    monkeypatch.setattr(
        migration,
        "authority_role_read_back",
        lambda *, role, organisation_concept_id, **_kwargs: {
            "active": any(
                row["role"] == role
                and row["organisation_concept_id"] == organisation_concept_id
                for row in semantic_rows
            ),
            "grants": [
                row
                for row in semantic_rows
                if row["role"] == role
                and row["organisation_concept_id"] == organisation_concept_id
            ],
        },
    )
    monkeypatch.setattr(
        migration,
        "_membership_role_read_back",
        lambda organisation_id: {
            "exact_owner": membership_roles[organisation_id] == "owner",
            "organisation_concept_id": organisation_id,
        },
    )

    before = migration.plan_ontology_authority_role_migration()
    with pytest.raises(migration.OntologyAuthorityRoleMigrationError) as partial:
        migration.apply_ontology_authority_role_migration(
            expected_inventory_fingerprint=before["inventory_fingerprint"]
        )
    assert partial.value.reason_code == "ontology_authority_role_migration_partial"
    assert [item["operation"] for item in partial.value.report["completed_effects"]] == [
        "bootstrap_von_operational_administrator",
        "bootstrap_global_ontology_administrator",
    ]

    fail_grant = False
    resumed = migration.apply_ontology_authority_role_migration(
        expected_inventory_fingerprint=before["inventory_fingerprint"]
    )

    assert resumed["success"] is True
    assert resumed["replayed"] is True
    assert calls == ["operational", "global", "org", "org", "owner"]
