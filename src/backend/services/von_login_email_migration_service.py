"""Create and populate the narrow Von login-email authority predicate.

Unlike organisation membership, there is intentionally no data-driven legacy
migration here.  Ordinary ``#V#has_email`` values may be contact addresses and
are never copied into authentication authority.  An operator must provide the
exact reviewed ``(user, email)`` allow-list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import bypass_access_control
from . import concept_service
from .relationship_write_service import add_dynamic_relationship
from .von_user_authentication_service import (
    GENERIC_EMAIL_PREDICATE,
    LOGIN_EMAIL_PREDICATE,
    PREDICATE_SPECIALISATION_PREDICATE,
    bind_von_login_email,
    list_von_login_email_user_ids,
    normalise_von_login_email,
)

MIGRATION_SCHEMA_VERSION = "von_login_email_predicate_migration.v1"


def _normalise_concept_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("user_concept_id_required")
    concept_id = value.strip()
    return concept_id if concept_id.startswith("#V#") else f"#V#{concept_id}"


def _normalise_values(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            continue
        concept_id = item.strip()
        if not concept_id.startswith("#V#"):
            concept_id = f"#V#{concept_id}"
        if concept_id not in output:
            output.append(concept_id)
    return output


def ensure_von_login_email_vocabulary() -> dict[str, Any]:
    """Materialise hasVonLoginEmail and its one-way email entailment."""

    with bypass_access_control():
        existing = ConceptsRepository.find_one({"concept_id": LOGIN_EMAIL_PREDICATE})
    if not isinstance(existing, Mapping):
        with bypass_access_control():
            concept_service.create_concept(
                name="Has Von login email",
                concept_id=LOGIN_EMAIL_PREDICATE,
                description=(
                    "An explicit server-authoritative email permitted to "
                    "authenticate as this Von user. It entails ordinary "
                    "has_email, but an ordinary has_email assertion never "
                    "implies this predicate or grants login authority."
                ),
                parent_concept_ids=["#V#binary_text_predicate"],
                create_as_instance=True,
                visibility_scope_mode="global_general",
                maintain_relationship_inverses=False,
            )
        state = "created"
    else:
        state = "existing"

    with bypass_access_control():
        predicate = ConceptsRepository.find_one(
            {"concept_id": LOGIN_EMAIL_PREDICATE},
            {
                "_id": 0,
                "concept_id": 1,
                "relationships.is_an_instance_of": 1,
            },
        )
    relationships = (
        predicate.get("relationships") if isinstance(predicate, Mapping) else {}
    )
    types = _normalise_values(
        relationships.get("is_an_instance_of")
        if isinstance(relationships, Mapping)
        else None
    )
    if "#V#binary_text_predicate" not in types:
        raise RuntimeError("login_email_predicate_type_read_back_failed")

    with bypass_access_control():
        result = add_dynamic_relationship(
            LOGIN_EMAIL_PREDICATE,
            PREDICATE_SPECIALISATION_PREDICATE,
            GENERIC_EMAIL_PREDICATE,
        )
    if result.get("success") is not True:
        raise RuntimeError(
            "login_email_predicate_specialisation_write_failed:"
            f"{result.get('error') or 'unknown'}"
        )

    with bypass_access_control():
        read_back = ConceptsRepository.find_one(
            {"concept_id": LOGIN_EMAIL_PREDICATE},
            {
                "_id": 0,
                "concept_id": 1,
                f"relationships.{PREDICATE_SPECIALISATION_PREDICATE}": 1,
            },
        )
    read_back_relationships = (
        read_back.get("relationships") if isinstance(read_back, Mapping) else {}
    )
    targets = _normalise_values(
        read_back_relationships.get(PREDICATE_SPECIALISATION_PREDICATE)
        if isinstance(read_back_relationships, Mapping)
        else None
    )
    if GENERIC_EMAIL_PREDICATE not in targets:
        raise RuntimeError("login_email_predicate_specialisation_read_back_failed")

    return {
        "success": True,
        "predicate_state": state,
        "entailment": {
            "specific_predicate": LOGIN_EMAIL_PREDICATE,
            "relation": PREDICATE_SPECIALISATION_PREDICATE,
            "generic_predicate": GENERIC_EMAIL_PREDICATE,
            "reverse_inference_allowed": False,
        },
    }


def _normalise_explicit_bindings(
    bindings: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    normalised: list[dict[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    requested_owners: dict[str, str] = {}
    for binding in bindings:
        if not isinstance(binding, Mapping):
            raise ValueError("login_email_binding_not_mapping")
        user_id = _normalise_concept_id(binding.get("user_concept_id"))
        email = normalise_von_login_email(binding.get("email"))
        prior_owner = requested_owners.get(email)
        if prior_owner is not None and prior_owner != user_id:
            raise ValueError("login_email_requested_for_multiple_users")
        requested_owners[email] = user_id
        pair = (user_id, email)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        normalised.append({"user_concept_id": user_id, "email": email})
    return normalised


def migrate_explicit_von_login_email_bindings(
    *,
    bindings: Iterable[Mapping[str, Any]],
    dry_run: bool = True,
    approved: bool = False,
) -> dict[str, Any]:
    """Validate or apply only the exact operator-supplied login allow-list."""

    try:
        requested = _normalise_explicit_bindings(bindings)
    except ValueError as exc:
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "success": False,
            "effect_status": "not_started",
            "error_code": str(exc),
            "generic_contact_emails_scanned": False,
            "generic_contact_emails_migrated": False,
        }

    if not dry_run and not approved:
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "success": False,
            "effect_status": "not_started",
            "error_code": "explicit_migration_approval_required",
            "requested_binding_count": len(requested),
            "generic_contact_emails_scanned": False,
            "generic_contact_emails_migrated": False,
        }

    conflicts: list[dict[str, Any]] = []
    ready: list[dict[str, Any]] = []
    for binding in requested:
        user_id = binding["user_concept_id"]
        email = binding["email"]
        with bypass_access_control():
            user = ConceptsRepository.find_one(
                {"concept_id": user_id},
                projection={"_id": 1, "concept_id": 1},
            )
        if not isinstance(user, Mapping):
            conflicts.append(
                {
                    **binding,
                    "reason_code": "user_concept_not_found",
                }
            )
            continue
        existing_user_ids = list_von_login_email_user_ids(email)
        other_user_ids = [item for item in existing_user_ids if item != user_id]
        if other_user_ids:
            conflicts.append(
                {
                    **binding,
                    "reason_code": "login_email_bound_to_other_user",
                    "existing_user_concept_ids": other_user_ids,
                }
            )
            continue
        ready.append(
            {
                **binding,
                "already_bound": existing_user_ids == [user_id],
            }
        )

    report: dict[str, Any] = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "success": not conflicts,
        "effect_status": "dry_run" if dry_run else "not_started",
        "requested_binding_count": len(requested),
        "ready_binding_count": len(ready),
        "conflict_count": len(conflicts),
        "ready_bindings": ready,
        "conflicts": conflicts,
        "login_email_predicate": LOGIN_EMAIL_PREDICATE,
        "entailed_generic_predicate": GENERIC_EMAIL_PREDICATE,
        "generic_contact_emails_scanned": False,
        "generic_contact_emails_migrated": False,
    }
    if conflicts or dry_run:
        return report

    vocabulary = ensure_von_login_email_vocabulary()
    results: list[dict[str, Any]] = []
    for binding in ready:
        result = bind_von_login_email(
            user_concept_id=binding["user_concept_id"],
            email=binding["email"],
            provenance={
                "migration_schema_version": MIGRATION_SCHEMA_VERSION,
                "approved": True,
            },
        )
        results.append(result)

    report.update(
        {
            "success": True,
            "effect_status": "completed",
            "vocabulary": vocabulary,
            "binding_results": results,
            "created_binding_count": sum(
                1 for result in results if result["relation_created"]
            ),
            "verified_binding_count": len(results),
        }
    )
    return report


__all__ = [
    "MIGRATION_SCHEMA_VERSION",
    "ensure_von_login_email_vocabulary",
    "migrate_explicit_von_login_email_bindings",
]
