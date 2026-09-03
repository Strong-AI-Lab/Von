from __future__ import annotations

from src.backend.services import (
    organisation_membership_predicate_migration_service as migration,
)


def test_audit_separates_governed_legacy_records_from_generic_membership(
    monkeypatch,
):
    governed = ("#V#governed_user", "#V#von_org")
    descriptive = ("#V#researcher", "#V#ordinary_org")
    monkeypatch.setattr(
        migration,
        "_legacy_edge_pairs",
        lambda: {
            governed: {"memberOf"},
            descriptive: {"#V#member_of_organisation"},
        },
    )
    monkeypatch.setattr(
        migration,
        "_legacy_role_pairs",
        lambda: ({governed: {"owner"}}, []),
    )
    monkeypatch.setattr(migration, "_narrow_membership_pairs", set)

    report = migration.audit_von_organisation_membership_predicates()

    assert report["eligible_operational_membership_count"] == 1
    assert report["eligible_operational_memberships"] == [
        {
            "user_concept_id": governed[0],
            "organisation_concept_id": governed[1],
            "role": "owner",
            "legacy_membership_predicates": ["memberOf"],
            "already_narrow": False,
        }
    ]
    assert report["generic_only_non_authority_count"] == 1
    assert (
        report["generic_only_non_authority_samples"][0]["user_concept_id"]
        == descriptive[0]
    )


def test_apply_migrates_only_role_backed_operational_membership(monkeypatch):
    governed = ("#V#governed_user", "#V#von_org")
    descriptive = ("#V#researcher", "#V#ordinary_org")
    narrow_pairs: set[tuple[str, str]] = set()
    roles: dict[tuple[str, str], str] = {}
    create_calls: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        migration,
        "_legacy_edge_pairs",
        lambda: {
            governed: {"memberOf"},
            descriptive: {"#V#member_of_organisation"},
        },
    )
    monkeypatch.setattr(
        migration,
        "_legacy_role_pairs",
        lambda: ({governed: {"owner"}}, []),
    )
    monkeypatch.setattr(
        migration, "_narrow_membership_pairs", lambda: set(narrow_pairs)
    )
    monkeypatch.setattr(
        migration,
        "ensure_von_organisation_membership_vocabulary",
        lambda: {"success": True},
    )

    def resolve(user_id, organisation_id):
        role = roles.get((user_id, organisation_id))
        if role is None:
            return None
        return {
            "user_concept_id": user_id,
            "organisation_concept_id": organisation_id,
            "role": role,
        }

    def create(*, user_concept_id, organisation_concept_id, role):
        pair = (user_concept_id, organisation_concept_id)
        create_calls.append((user_concept_id, organisation_concept_id, role))
        narrow_pairs.add(pair)
        roles[pair] = role
        return {"relationship_created": True}

    monkeypatch.setattr(migration, "resolve_user_organisation_membership", resolve)
    monkeypatch.setattr(migration, "create_organisation_membership", create)

    report = migration.migrate_von_organisation_membership_predicates(
        dry_run=False,
        approved=True,
    )

    assert report["success"] is True
    assert report["migrated_count"] == 1
    assert create_calls == [(governed[0], governed[1], "owner")]
    assert descriptive not in narrow_pairs
    assert report["future_generic_relations_grant_authority"] is False


def test_pre_cutover_mode_copies_generic_only_pair_with_old_member_default(
    monkeypatch,
):
    governed = ("#V#governed_user", "#V#von_org")
    legacy_default = ("#V#legacy_user", "#V#legacy_org")
    narrow_pairs: set[tuple[str, str]] = set()
    roles: dict[tuple[str, str], str] = {}
    create_calls: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        migration,
        "_legacy_edge_pairs",
        lambda: {
            governed: {"memberOf"},
            legacy_default: {"#V#member_of_organisation"},
        },
    )
    monkeypatch.setattr(
        migration,
        "_legacy_role_pairs",
        lambda: ({governed: {"owner"}}, []),
    )
    monkeypatch.setattr(
        migration, "_narrow_membership_pairs", lambda: set(narrow_pairs)
    )
    monkeypatch.setattr(
        migration,
        "ensure_von_organisation_membership_vocabulary",
        lambda: {"success": True},
    )

    def resolve(user_id, organisation_id):
        role = roles.get((user_id, organisation_id))
        if role is None:
            return None
        return {
            "user_concept_id": user_id,
            "organisation_concept_id": organisation_id,
            "role": role,
        }

    def create(*, user_concept_id, organisation_concept_id, role):
        pair = (user_concept_id, organisation_concept_id)
        create_calls.append((user_concept_id, organisation_concept_id, role))
        narrow_pairs.add(pair)
        roles[pair] = role
        return {"relationship_created": True}

    monkeypatch.setattr(migration, "resolve_user_organisation_membership", resolve)
    monkeypatch.setattr(migration, "create_organisation_membership", create)

    report = migration.migrate_von_organisation_membership_predicates(
        dry_run=False,
        approved=True,
        preserve_pre_cutover_authority=True,
    )

    assert report["success"] is True
    assert report["migrated_count"] == 2
    assert create_calls == [
        (governed[0], governed[1], "owner"),
        (legacy_default[0], legacy_default[1], "member"),
    ]
    assert report["pre_cutover_default_member_count"] == 1
    assert report["legacy_generic_only_pairs_copied"] is True
    assert report["future_generic_relations_grant_authority"] is False


def test_apply_requires_explicit_approval(monkeypatch):
    monkeypatch.setattr(migration, "_legacy_edge_pairs", dict)
    monkeypatch.setattr(migration, "_legacy_role_pairs", lambda: ({}, []))
    monkeypatch.setattr(migration, "_narrow_membership_pairs", set)

    report = migration.migrate_von_organisation_membership_predicates(
        dry_run=False,
        approved=False,
    )

    assert report["success"] is False
    assert report["effect_status"] == "not_started"
    assert report["error_code"] == "explicit_migration_approval_required"


def test_conflicting_roles_require_an_exact_evidenced_override(monkeypatch):
    pair = ("#V#alice", "#V#von_org")
    monkeypatch.setattr(migration, "_legacy_edge_pairs", lambda: {pair: {"memberOf"}})
    monkeypatch.setattr(
        migration,
        "_legacy_role_pairs",
        lambda: ({pair: {"member", "owner"}}, []),
    )
    monkeypatch.setattr(migration, "_narrow_membership_pairs", set)

    blocked = migration.migrate_von_organisation_membership_predicates()
    assert blocked["success"] is False
    assert blocked["unresolved_conflict_count"] == 1
    assert blocked["error_code"] == "legacy_membership_inventory_requires_resolution"

    resolved = migration.migrate_von_organisation_membership_predicates(
        role_overrides=[
            {
                "user_concept_id": pair[0],
                "organisation_concept_id": pair[1],
                "role": "owner",
            }
        ]
    )
    assert resolved["success"] is True
    assert resolved["resolved_conflict_count"] == 1
    assert resolved["would_migrate_count"] == 1
    assert resolved["resolved_conflicts"][0]["role"] == "owner"


def test_role_override_cannot_invent_a_role_or_target_an_unconflicted_pair(
    monkeypatch,
):
    pair = ("#V#alice", "#V#von_org")
    monkeypatch.setattr(migration, "_legacy_edge_pairs", lambda: {pair: {"memberOf"}})
    monkeypatch.setattr(
        migration,
        "_legacy_role_pairs",
        lambda: ({pair: {"member", "owner"}}, []),
    )
    monkeypatch.setattr(migration, "_narrow_membership_pairs", set)

    invented = migration.migrate_von_organisation_membership_predicates(
        role_overrides=[
            {
                "user_concept_id": pair[0],
                "organisation_concept_id": pair[1],
                "role": "admin",
            }
        ]
    )
    assert invented["success"] is False
    assert invented["role_override_errors"][0]["reason_code"] == (
        "role_override_not_present_in_legacy_evidence"
    )

    unrelated = migration.migrate_von_organisation_membership_predicates(
        role_overrides=[
            {
                "user_concept_id": "#V#bob",
                "organisation_concept_id": pair[1],
                "role": "member",
            }
        ]
    )
    assert unrelated["success"] is False
    assert unrelated["role_override_errors"][0]["reason_code"] == (
        "role_override_does_not_match_a_conflicted_pair"
    )


def test_vocabulary_represents_one_way_memberof_entailment(monkeypatch):
    documents: dict[str, dict] = {
        migration.GENERIC_MEMBERSHIP_PREDICATE: {
            "concept_id": migration.GENERIC_MEMBERSHIP_PREDICATE,
            "relationships": {"is_an_instance_of": ["#V#predicate"]},
        },
        migration.PREDICATE_SPECIALISATION_PREDICATE: {
            "concept_id": migration.PREDICATE_SPECIALISATION_PREDICATE,
            "relationships": {"is_an_instance_of": ["#V#predicate"]},
        },
    }

    def find_one(query, projection=None):
        return documents.get(query.get("concept_id"))

    def create_concept(**kwargs):
        documents[kwargs["concept_id"]] = {
            "concept_id": kwargs["concept_id"],
            "relationships": {"is_an_instance_of": list(kwargs["parent_concept_ids"])},
        }
        return documents[kwargs["concept_id"]]

    def add_relationship(source_id, predicate, target_id):
        relationships = documents[source_id].setdefault("relationships", {})
        relationships.setdefault(predicate, []).append(target_id)
        return {"success": True, "modified": True}

    monkeypatch.setattr(migration.ConceptsRepository, "find_one", find_one)
    monkeypatch.setattr(migration.concept_service, "create_concept", create_concept)
    monkeypatch.setattr(migration, "add_dynamic_relationship", add_relationship)

    report = migration.ensure_von_organisation_membership_vocabulary()

    assert report["success"] is True
    assert report["entailment"] == {
        "specific_predicate": "#V#memberOfVonOrg",
        "relation": "#V#predicate_specialises_predicate",
        "generic_predicate": "#V#memberOf",
        "reverse_inference_allowed": False,
    }
    assert documents["#V#memberOfVonOrg"]["relationships"][
        "#V#predicate_specialises_predicate"
    ] == ["#V#memberOf"]
    assert (
        "#V#predicate_specialises_predicate"
        not in documents["#V#memberOf"]["relationships"]
    )
