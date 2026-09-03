from __future__ import annotations

from src.backend.services import von_login_email_migration_service as migration


def test_legacy_audit_deduplicates_same_user_email_relations(monkeypatch):
    relations = [
        {
            "_id": "r1",
            "subject_concept_id": "#V#alice",
            "object_text_id": "tv1",
        },
        {
            "_id": "r2",
            "subject_concept_id": "#V#alice",
            "object_text_id": "tv2",
        },
        {
            "_id": "r3",
            "subject_concept_id": "#V#bob",
            "object_text_id": "tv3",
        },
    ]
    text_values = {
        "tv1": {"text": "Alice@Example.ORG"},
        "tv2": {"text": "alice@example.org"},
        "tv3": {"text": "bob@example.org"},
    }
    monkeypatch.setattr(
        migration.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: relations,
    )
    monkeypatch.setattr(
        migration.TextValuesRepository,
        "find_one_by_id",
        lambda value_id: text_values.get(value_id),
    )
    monkeypatch.setattr(
        migration.ConceptsRepository,
        "find_one",
        lambda query, **_kwargs: {"concept_id": query["concept_id"]},
    )
    monkeypatch.setattr(migration, "list_von_login_email_user_ids", lambda _email: [])

    report = migration.audit_legacy_von_login_email_bindings()

    assert report["legacy_relation_count"] == 3
    assert report["normalised_email_count"] == 2
    assert report["eligible_binding_count"] == 2
    assert report["conflict_count"] == 0
    alice = next(
        item
        for item in report["eligible_bindings"]
        if item["user_concept_id"] == "#V#alice"
    )
    assert alice["email"] == "alice@example.org"
    assert alice["legacy_relation_count"] == 2


def test_legacy_audit_rejects_normalised_email_shared_by_users(monkeypatch):
    relations = [
        {
            "_id": "r1",
            "subject_concept_id": "#V#alice",
            "object_text_id": "tv1",
        },
        {
            "_id": "r2",
            "subject_concept_id": "#V#bob",
            "object_text_id": "tv2",
        },
    ]
    text_values = {
        "tv1": {"text": "shared@example.org"},
        "tv2": {"text": "SHARED@example.org"},
    }
    monkeypatch.setattr(
        migration.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: relations,
    )
    monkeypatch.setattr(
        migration.TextValuesRepository,
        "find_one_by_id",
        lambda value_id: text_values.get(value_id),
    )

    report = migration.audit_legacy_von_login_email_bindings()

    assert report["eligible_binding_count"] == 0
    assert report["conflict_count"] == 1
    assert report["conflicts"][0]["reason_code"] == (
        "legacy_login_email_maps_to_multiple_users"
    )


def test_apply_legacy_bindings_uses_canonical_writer_and_reads_back(monkeypatch):
    binding = {
        "user_concept_id": "#V#alice",
        "email": "alice@example.org",
        "legacy_relation_count": 1,
        "already_bound": False,
    }
    audit = {
        "schema_version": migration.LEGACY_AUDIT_SCHEMA_VERSION,
        "legacy_email_predicate": "#V#has_email",
        "narrow_login_email_predicate": "#V#hasVonLoginEmail",
        "legacy_relation_count": 1,
        "normalised_email_count": 1,
        "eligible_binding_count": 1,
        "already_bound_count": 0,
        "conflict_count": 0,
        "invalid_relation_count": 0,
        "eligible_bindings": [binding],
        "conflicts": [],
        "invalid_relations": [],
        "complete": True,
    }
    bound: dict[str, list[str]] = {}
    monkeypatch.setattr(
        migration,
        "audit_legacy_von_login_email_bindings",
        lambda **_kwargs: audit,
    )
    monkeypatch.setattr(
        migration,
        "ensure_von_login_email_vocabulary",
        lambda: {"success": True},
    )

    def bind(*, user_concept_id, email, provenance):
        bound[email] = [user_concept_id]
        return {
            "success": True,
            "user_concept_id": user_concept_id,
            "email": email,
            "relation_created": True,
            "provenance": provenance,
        }

    monkeypatch.setattr(migration, "bind_von_login_email", bind)
    monkeypatch.setattr(
        migration,
        "list_von_login_email_user_ids",
        lambda email: bound.get(email, []),
    )

    report = migration.migrate_legacy_von_login_email_bindings(
        dry_run=False,
        approved=True,
    )

    assert report["success"] is True
    assert report["migrated_count"] == 1
    assert report["missing_read_back_bindings"] == []
    assert report["future_generic_contact_emails_grant_authority"] is False


def test_legacy_migration_fails_closed_on_audit_conflict(monkeypatch):
    audit = {
        "schema_version": migration.LEGACY_AUDIT_SCHEMA_VERSION,
        "legacy_email_predicate": "#V#has_email",
        "narrow_login_email_predicate": "#V#hasVonLoginEmail",
        "legacy_relation_count": 2,
        "normalised_email_count": 1,
        "eligible_binding_count": 0,
        "already_bound_count": 0,
        "conflict_count": 1,
        "invalid_relation_count": 0,
        "eligible_bindings": [],
        "conflicts": [
            {
                "email": "shared@example.org",
                "user_concept_ids": ["#V#alice", "#V#bob"],
                "reason_code": "legacy_login_email_maps_to_multiple_users",
            }
        ],
        "invalid_relations": [],
        "complete": True,
    }
    monkeypatch.setattr(
        migration,
        "audit_legacy_von_login_email_bindings",
        lambda **_kwargs: audit,
    )

    report = migration.migrate_legacy_von_login_email_bindings()

    assert report["success"] is False
    assert report["effect_status"] == "not_started"
    assert report["error_code"] == "legacy_login_email_inventory_requires_resolution"


def test_legacy_migration_apply_requires_explicit_approval(monkeypatch):
    audit = {
        "schema_version": migration.LEGACY_AUDIT_SCHEMA_VERSION,
        "legacy_email_predicate": "#V#has_email",
        "narrow_login_email_predicate": "#V#hasVonLoginEmail",
        "legacy_relation_count": 0,
        "normalised_email_count": 0,
        "eligible_binding_count": 0,
        "already_bound_count": 0,
        "conflict_count": 0,
        "invalid_relation_count": 0,
        "eligible_bindings": [],
        "conflicts": [],
        "invalid_relations": [],
        "complete": True,
    }
    monkeypatch.setattr(
        migration,
        "audit_legacy_von_login_email_bindings",
        lambda **_kwargs: audit,
    )

    report = migration.migrate_legacy_von_login_email_bindings(
        dry_run=False,
        approved=False,
    )

    assert report["success"] is False
    assert report["effect_status"] == "not_started"
    assert report["error_code"] == "explicit_migration_approval_required"


def test_dry_run_uses_only_explicit_bindings_and_never_scans_contact_email(
    monkeypatch,
):
    monkeypatch.setattr(
        migration.ConceptsRepository,
        "find_one",
        lambda *_args, **_kwargs: {"concept_id": "#V#alice"},
    )
    monkeypatch.setattr(
        migration,
        "list_von_login_email_user_ids",
        lambda _email: [],
    )

    report = migration.migrate_explicit_von_login_email_bindings(
        bindings=[
            {
                "user_concept_id": "#V#alice",
                "email": "Alice@Example.ORG",
            }
        ]
    )

    assert report["success"] is True
    assert report["effect_status"] == "dry_run"
    assert report["ready_bindings"] == [
        {
            "user_concept_id": "#V#alice",
            "email": "alice@example.org",
            "already_bound": False,
        }
    ]
    assert report["generic_contact_emails_scanned"] is False
    assert report["generic_contact_emails_migrated"] is False


def test_apply_requires_explicit_approval():
    report = migration.migrate_explicit_von_login_email_bindings(
        bindings=[
            {
                "user_concept_id": "#V#alice",
                "email": "alice@example.org",
            }
        ],
        dry_run=False,
        approved=False,
    )

    assert report["success"] is False
    assert report["error_code"] == "explicit_migration_approval_required"


def test_apply_binds_only_the_reviewed_allow_list(monkeypatch):
    monkeypatch.setattr(
        migration.ConceptsRepository,
        "find_one",
        lambda *_args, **_kwargs: {"concept_id": "#V#alice"},
    )
    monkeypatch.setattr(
        migration,
        "list_von_login_email_user_ids",
        lambda _email: [],
    )
    monkeypatch.setattr(
        migration,
        "ensure_von_login_email_vocabulary",
        lambda: {"success": True},
    )
    calls: list[dict] = []

    def bind(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "relation_created": True,
            "user_concept_id": kwargs["user_concept_id"],
            "email": kwargs["email"],
        }

    monkeypatch.setattr(migration, "bind_von_login_email", bind)

    report = migration.migrate_explicit_von_login_email_bindings(
        bindings=[
            {
                "user_concept_id": "#V#alice",
                "email": "alice@example.org",
            }
        ],
        dry_run=False,
        approved=True,
    )

    assert report["success"] is True
    assert report["created_binding_count"] == 1
    assert calls[0]["user_concept_id"] == "#V#alice"
    assert calls[0]["email"] == "alice@example.org"
    assert report["generic_contact_emails_migrated"] is False


def test_vocabulary_represents_one_way_email_entailment(monkeypatch):
    documents: dict[str, dict] = {
        migration.GENERIC_EMAIL_PREDICATE: {
            "concept_id": migration.GENERIC_EMAIL_PREDICATE,
            "relationships": {"is_an_instance_of": ["#V#binary_text_predicate"]},
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

    report = migration.ensure_von_login_email_vocabulary()

    assert report["entailment"] == {
        "specific_predicate": "#V#hasVonLoginEmail",
        "relation": "#V#predicate_specialises_predicate",
        "generic_predicate": "#V#has_email",
        "reverse_inference_allowed": False,
    }
    assert documents["#V#hasVonLoginEmail"]["relationships"][
        "#V#predicate_specialises_predicate"
    ] == ["#V#has_email"]
    assert (
        "#V#predicate_specialises_predicate"
        not in documents["#V#has_email"]["relationships"]
    )
