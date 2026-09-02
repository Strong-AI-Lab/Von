from __future__ import annotations

from src.backend.services import von_login_email_migration_service as migration


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
