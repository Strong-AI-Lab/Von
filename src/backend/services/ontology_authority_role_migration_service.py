"""One-time, inventory-bound migration of Michael's administrator authority.

The migration is deliberately specific rather than a reusable elevation API.
It preserves legacy operational and organisation-role representations, adds
semantic ontology authority through the canonical role lifecycle, and refuses
to start if the live inventory contradicts the authorised sole-holder premise.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from ..db.mongo_client import get_migrations_log_collection
from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import (
    bypass_access_control,
    get_effective_user_concept_id,
    override_current_organisation,
)
from ..security.role_resolver import STUB_ROLE_MAPPINGS, get_effective_permissions
from .ontology_authority_membership_coordination_service import (
    ontology_authority_membership_mutation_barrier,
)
from .ontology_authority_role_service import (
    authority_role_read_back,
    grant_ontology_authority_role,
)
from .ontology_authority_vocabulary_service import (
    bootstrap_first_semantic_authority,
)
from .ontology_publication_authority_service import (
    AUTHORITY_ROLE_PREDICATE,
    GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
    VON_ADMINISTRATOR_CONCEPT_ID,
    OntologyMutationResourceBusy,
    current_ontology_invocation,
    ontology_mutation_resource_lock,
    parse_authority_role_storage_text,
)
from .organisation_membership_service import (
    ROLE_PREDICATE,
    get_user_memberships,
    organisation_role_storage_text,
    parse_organisation_role_storage_text,
)
from .text_value_service import delete_text_relation, upsert_text_for_concept
from .von_operational_administrator_service import (
    VON_OPERATIONAL_ADMINISTRATOR_ROLE,
    bootstrap_first_von_operational_administrator,
    list_von_operational_administrators,
    von_operational_administrator_read_back,
)

ONTOLOGY_AUTHORITY_ROLE_MIGRATION_SCHEMA_VERSION = (
    "ontology_authority_role_migration.v1"
)
ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET = "#V#michael_witbrock"

_MIGRATION_RESOURCE_KEY = "ontology-authority-role-migration:michael-witbrock:v1"
_MIGRATION_OPERATION_NAME = "jvnautosci-2632-initial-administrator-roles-v1"
_MIGRATION_REASON = "JVNAUTOSCI-2632 authorised administrator-role migration"
_OWNER_ROLE = "owner"
_NON_PRIVILEGED_ORGANISATION_ROLES = frozenset({"", "user", "viewer", "member"})
_OPERATIONAL_ROLE_TOKENS = frozenset(
    {
        "von administrator",
        "von_administrator",
        VON_ADMINISTRATOR_CONCEPT_ID.lower(),
        "operator",
        "system",
    }
)


class OntologyAuthorityRoleMigrationError(RuntimeError):
    """Typed refusal or partial result from the one-time role migration."""

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        report: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.public_message = message
        self.report = dict(report) if isinstance(report, Mapping) else None


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalise_concept_id(value: object) -> str | None:
    cleaned = _clean(value)
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("#") else f"#V#{cleaned}"


def _normalise_role(value: object) -> str:
    return " ".join(_clean(value).lower().replace("-", "_").split())


def _stable_fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _migration_log_collection():
    collection = get_migrations_log_collection()
    if collection is None:
        raise RuntimeError("migration log store unavailable")
    return collection


def _action_key(action: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        _clean(action.get("operation")),
        _clean(action.get("subject_concept_id")),
        _normalise_role(action.get("role")),
        _clean(_normalise_concept_id(action.get("organisation_concept_id"))),
    )


def _plan_organisation_ids(plan: Mapping[str, Any]) -> set[str]:
    inventory = plan.get("inventory")
    inventory_map = inventory if isinstance(inventory, Mapping) else {}
    return {
        _clean(_normalise_concept_id(row.get("organisation_concept_id")))
        for row in inventory_map.get("memberships") or []
        if isinstance(row, Mapping)
        and _normalise_concept_id(row.get("organisation_concept_id"))
    }


def _continuation_is_safe(
    source_plan: Mapping[str, Any],
    current_plan: Mapping[str, Any],
) -> bool:
    """Allow only expected migration deltas when resuming one fingerprint."""

    if current_plan.get("ready_to_apply") is not True:
        return False
    if _plan_organisation_ids(source_plan) != _plan_organisation_ids(current_plan):
        return False
    source_actions = {
        _action_key(action)
        for action in source_plan.get("actions") or []
        if isinstance(action, Mapping)
    }
    current_actions = {
        _action_key(action)
        for action in current_plan.get("actions") or []
        if isinstance(action, Mapping)
    }
    return bool(source_actions) and source_actions == current_actions


def _load_migration_operation() -> dict[str, Any] | None:
    value = _migration_log_collection().find_one(
        {"migration_name": _MIGRATION_OPERATION_NAME}
    )
    return dict(value) if isinstance(value, Mapping) else None


def _start_migration_operation(
    *,
    source_plan: Mapping[str, Any],
    source_fingerprint: str,
) -> dict[str, Any]:
    operation = {
        "migration_name": _MIGRATION_OPERATION_NAME,
        "schema_version": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_SCHEMA_VERSION,
        "target_concept_id": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
        "source_inventory_fingerprint": source_fingerprint,
        "source_plan": dict(source_plan),
        "status": "in_progress",
        "completed_effects": [],
        "started_at": _now(),
        "updated_at": _now(),
    }
    try:
        _migration_log_collection().insert_one(operation)
        return operation
    except DuplicateKeyError:
        existing = _load_migration_operation()
        if existing is None:
            raise RuntimeError("migration operation record unreadable")
        return existing


def _update_migration_operation(**values: Any) -> None:
    result = _migration_log_collection().update_one(
        {"migration_name": _MIGRATION_OPERATION_NAME},
        {"$set": {**values, "updated_at": _now()}},
    )
    if int(getattr(result, "matched_count", 0) or 0) != 1:
        raise RuntimeError("migration operation record unavailable")


def _assignment(
    *,
    source: str,
    subject_concept_id: object,
    role: object,
    organisation_concept_id: object = None,
    relation_id: object = None,
) -> dict[str, Any] | None:
    subject_id = _normalise_concept_id(subject_concept_id)
    if not subject_id:
        return None
    return {
        "source": source,
        "subject_concept_id": subject_id,
        "role": _normalise_role(role),
        "organisation_concept_id": _normalise_concept_id(organisation_concept_id),
        "relation_id": _clean(relation_id) or None,
    }


def _is_privileged_legacy_role(
    role: object,
    *,
    organisation_concept_id: object = None,
) -> bool:
    role_value = _normalise_role(role)
    if role_value in _OPERATIONAL_ROLE_TOKENS:
        return True
    if _normalise_concept_id(organisation_concept_id):
        # Unknown organisation roles are treated as privileged for migration
        # precondition purposes. A new/custom role must not be silently assumed
        # harmless while deciding who already has elevated authority.
        return role_value not in _NON_PRIVILEGED_ORGANISATION_ROLES
    return False


def _stub_privileged_role_assignments() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_subject, raw_memberships in STUB_ROLE_MAPPINGS.items():
        if not isinstance(raw_memberships, Mapping):
            continue
        for raw_organisation, raw_role in raw_memberships.items():
            if not _is_privileged_legacy_role(
                raw_role,
                organisation_concept_id=raw_organisation,
            ):
                continue
            row = _assignment(
                source="legacy_stub_role_mapping",
                subject_concept_id=raw_subject,
                role=raw_role,
                organisation_concept_id=raw_organisation,
            )
            if row:
                rows.append(row)
    return rows


def _text_privileged_role_assignments() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relation in TextRelationsRepository.find({"predicate": ROLE_PREDICATE}):
        if not isinstance(relation, Mapping):
            continue
        text_value = TextValuesRepository.find_one_by_id(relation.get("object_text_id"))
        if not isinstance(text_value, Mapping):
            continue
        context = relation.get("context")
        context_map = context if isinstance(context, Mapping) else {}
        organisation_id = context_map.get("organisation_concept_id") or context_map.get(
            "organisation_id"
        )
        role, stored_organisation_id = parse_organisation_role_storage_text(
            text_value.get("text"),
            dict(context_map),
        )
        organisation_id = stored_organisation_id or organisation_id
        if not role and not organisation_id:
            raw_role = _normalise_role(text_value.get("text"))
            if raw_role in _OPERATIONAL_ROLE_TOKENS:
                role = raw_role
        if not _is_privileged_legacy_role(
            role,
            organisation_concept_id=organisation_id,
        ):
            continue
        row = _assignment(
            source="represented_legacy_text_role",
            subject_concept_id=relation.get("subject_concept_id"),
            role=role,
            organisation_concept_id=organisation_id,
            relation_id=relation.get("_id"),
        )
        if row:
            rows.append(row)
    return rows


def _relationship_operational_role_assignments() -> list[dict[str, Any]]:
    predicate_names = ("#V#hasRole", "hasRole")
    role_tokens = tuple(
        sorted({*_OPERATIONAL_ROLE_TOKENS, VON_ADMINISTRATOR_CONCEPT_ID})
    )
    query = {
        "$or": [
            {f"relationships.{predicate}": {"$in": list(role_tokens)}}
            for predicate in predicate_names
        ]
    }
    rows: list[dict[str, Any]] = []
    with bypass_access_control():
        concepts = ConceptsRepository.find(
            query,
            projection={"concept_id": 1, "relationships": 1},
        )
        for concept in concepts:
            if not isinstance(concept, Mapping):
                continue
            relationships = concept.get("relationships")
            relationship_map = (
                relationships if isinstance(relationships, Mapping) else {}
            )
            for predicate in predicate_names:
                raw_values = relationship_map.get(predicate)
                values = raw_values if isinstance(raw_values, list) else [raw_values]
                for raw_role in values:
                    if _normalise_role(raw_role) not in _OPERATIONAL_ROLE_TOKENS:
                        continue
                    row = _assignment(
                        source="represented_operational_role_relationship",
                        subject_concept_id=concept.get("concept_id"),
                        role=raw_role,
                    )
                    if row:
                        rows.append(row)
    return rows


def _legacy_privileged_role_assignments() -> list[dict[str, Any]]:
    rows = [
        *_stub_privileged_role_assignments(),
        *_text_privileged_role_assignments(),
        *_relationship_operational_role_assignments(),
    ]
    deduplicated: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            _clean(row.get("source")),
            _clean(row.get("subject_concept_id")),
            _clean(row.get("role")),
            _clean(row.get("organisation_concept_id")),
            _clean(row.get("relation_id")),
        )
        deduplicated[key] = row
    return sorted(
        deduplicated.values(),
        key=lambda row: (
            _clean(row.get("subject_concept_id")),
            _clean(row.get("organisation_concept_id")),
            _clean(row.get("role")),
            _clean(row.get("source")),
            _clean(row.get("relation_id")),
        ),
    )


def _semantic_role_assignments() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relation in TextRelationsRepository.find(
        {"predicate": AUTHORITY_ROLE_PREDICATE}
    ):
        if not isinstance(relation, Mapping):
            continue
        text_value = TextValuesRepository.find_one_by_id(relation.get("object_text_id"))
        if not isinstance(text_value, Mapping):
            continue
        context = relation.get("context")
        context_map = context if isinstance(context, Mapping) else {}
        role, organisation_id = parse_authority_role_storage_text(
            text_value.get("text"),
            context_map,
        )
        if not role:
            continue
        row = _assignment(
            source="represented_semantic_authority_role",
            subject_concept_id=relation.get("subject_concept_id"),
            role=role,
            organisation_concept_id=organisation_id,
            relation_id=relation.get("_id"),
        )
        if row:
            rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            _clean(row.get("subject_concept_id")),
            _clean(row.get("role")),
            _clean(row.get("organisation_concept_id")),
            _clean(row.get("relation_id")),
        ),
    )


def _target_membership_role_assignments() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relation in TextRelationsRepository.find(
        {
            "subject_concept_id": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
            "predicate": ROLE_PREDICATE,
        }
    ):
        if not isinstance(relation, Mapping):
            continue
        context = relation.get("context")
        context_map = context if isinstance(context, Mapping) else {}
        organisation_id = _normalise_concept_id(
            context_map.get("organisation_concept_id")
            or context_map.get("organisation_id")
        )
        if not organisation_id:
            continue
        text_value = TextValuesRepository.find_one_by_id(relation.get("object_text_id"))
        if not isinstance(text_value, Mapping):
            continue
        role, stored_organisation_id = parse_organisation_role_storage_text(
            text_value.get("text"),
            dict(context_map),
        )
        if not role or stored_organisation_id != organisation_id:
            continue
        row = _assignment(
            source="represented_organisation_membership_role",
            subject_concept_id=ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
            role=role,
            organisation_concept_id=organisation_id,
            relation_id=relation.get("_id"),
        )
        if row:
            rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            _clean(row.get("organisation_concept_id")),
            _clean(row.get("role")),
            _clean(row.get("relation_id")),
        ),
    )


def _target_memberships() -> tuple[list[dict[str, Any]], list[str]]:
    membership_payload = get_user_memberships(ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET)
    memberships: list[dict[str, Any]] = []
    missing_organisations: list[str] = []
    seen: set[str] = set()
    for raw_membership in membership_payload.get("memberships", []):
        if not isinstance(raw_membership, Mapping):
            continue
        organisation_id = _normalise_concept_id(
            raw_membership.get("organisation_concept_id")
        )
        if not organisation_id or organisation_id in seen:
            continue
        seen.add(organisation_id)
        with bypass_access_control():
            organisation = ConceptsRepository.find_one(
                {"concept_id": organisation_id},
                projection={"concept_id": 1},
            )
        if not isinstance(organisation, Mapping):
            missing_organisations.append(organisation_id)
        memberships.append(
            {
                "organisation_concept_id": organisation_id,
                "legacy_role": _normalise_role(raw_membership.get("role")) or "member",
            }
        )
    memberships.sort(key=lambda row: row["organisation_concept_id"])
    missing_organisations.sort()
    return memberships, missing_organisations


def _collect_inventory() -> dict[str, Any]:
    with bypass_access_control():
        target = ConceptsRepository.find_one(
            {"concept_id": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET},
            projection={"concept_id": 1},
        )
    target_exists = isinstance(target, Mapping)
    memberships: list[dict[str, Any]] = []
    missing_organisations: list[str] = []
    if target_exists:
        memberships, missing_organisations = _target_memberships()
    return {
        "target_concept_id": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
        "target_exists": target_exists,
        "memberships": memberships,
        "missing_membership_organisations": missing_organisations,
        "legacy_privileged_role_assignments": (_legacy_privileged_role_assignments()),
        "target_membership_role_assignments": (_target_membership_role_assignments()),
        "semantic_role_assignments": _semantic_role_assignments(),
        "operational_role_assignments": list_von_operational_administrators(),
    }


def _inventory_conflicts(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    target_id = ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET
    if inventory.get("target_exists") is not True:
        conflicts.append(
            {
                "reason_code": "migration_target_not_found",
                "message": "The canonical Michael Witbrock concept does not exist.",
            }
        )
    missing_orgs = list(inventory.get("missing_membership_organisations") or [])
    if missing_orgs:
        conflicts.append(
            {
                "reason_code": "membership_organisation_not_found",
                "message": "A represented membership names a missing organisation.",
                "organisation_concept_ids": missing_orgs,
            }
        )

    legacy_rows = list(inventory.get("legacy_privileged_role_assignments") or [])
    legacy_subjects = sorted(
        {
            _clean(row.get("subject_concept_id"))
            for row in legacy_rows
            if isinstance(row, Mapping) and _clean(row.get("subject_concept_id"))
        }
    )
    if target_id not in legacy_subjects:
        conflicts.append(
            {
                "reason_code": "expected_sole_privileged_holder_not_found",
                "message": (
                    "The inventory does not show Michael as an existing privileged "
                    "legacy role holder."
                ),
            }
        )
    represented_membership_orgs = {
        _clean(row.get("organisation_concept_id"))
        for row in (inventory.get("memberships") or [])
        if isinstance(row, Mapping) and _clean(row.get("organisation_concept_id"))
    }
    target_legacy_orgs = sorted(
        {
            _clean(row.get("organisation_concept_id"))
            for row in legacy_rows
            if isinstance(row, Mapping)
            and _clean(row.get("subject_concept_id")) == target_id
            and _clean(row.get("organisation_concept_id"))
        }
    )
    missing_legacy_memberships = [
        organisation_id
        for organisation_id in target_legacy_orgs
        if organisation_id not in represented_membership_orgs
    ]
    if missing_legacy_memberships:
        conflicts.append(
            {
                "reason_code": "privileged_legacy_role_without_membership",
                "message": (
                    "Michael has a privileged legacy organisation role without "
                    "a matching represented membership."
                ),
                "organisation_concept_ids": missing_legacy_memberships,
            }
        )
    unexpected_legacy_subjects = [
        subject for subject in legacy_subjects if subject != target_id
    ]
    if unexpected_legacy_subjects:
        conflicts.append(
            {
                "reason_code": "unexpected_privileged_role_holder",
                "message": (
                    "A legacy privileged role is held by someone other than the "
                    "authorised migration target."
                ),
                "subject_concept_ids": unexpected_legacy_subjects,
            }
        )

    semantic_rows = list(inventory.get("semantic_role_assignments") or [])
    unexpected_semantic_subjects = sorted(
        {
            _clean(row.get("subject_concept_id"))
            for row in semantic_rows
            if isinstance(row, Mapping)
            and _clean(row.get("subject_concept_id"))
            and _clean(row.get("subject_concept_id")) != target_id
        }
    )
    if unexpected_semantic_subjects:
        conflicts.append(
            {
                "reason_code": "unexpected_semantic_administrator",
                "message": (
                    "A represented semantic administrator other than Michael "
                    "already exists."
                ),
                "subject_concept_ids": unexpected_semantic_subjects,
            }
        )

    operational_rows = list(inventory.get("operational_role_assignments") or [])
    operational_subjects = sorted(
        {
            _clean(row.get("subject_concept_id"))
            for row in operational_rows
            if isinstance(row, Mapping) and _clean(row.get("subject_concept_id"))
        }
    )
    unexpected_operational_subjects = [
        subject for subject in operational_subjects if subject != target_id
    ]
    if unexpected_operational_subjects:
        conflicts.append(
            {
                "reason_code": "unexpected_von_operational_administrator",
                "message": (
                    "A represented Von operational administrator other than "
                    "Michael already exists."
                ),
                "subject_concept_ids": unexpected_operational_subjects,
            }
        )
    if len(operational_rows) > 1:
        conflicts.append(
            {
                "reason_code": "duplicate_von_operational_administrator_assignment",
                "message": (
                    "The represented Von operational role has duplicate assignments "
                    "and requires reconciliation before migration."
                ),
            }
        )

    semantic_grant_counts: dict[tuple[str, str, str], int] = {}
    for row in semantic_rows:
        if not isinstance(row, Mapping):
            continue
        key = (
            _clean(row.get("subject_concept_id")),
            _normalise_role(row.get("role")),
            _clean(row.get("organisation_concept_id")),
        )
        semantic_grant_counts[key] = semantic_grant_counts.get(key, 0) + 1
    duplicate_semantic_grants = [
        {
            "subject_concept_id": subject_id,
            "role": role,
            "organisation_concept_id": organisation_id or None,
            "count": count,
        }
        for (subject_id, role, organisation_id), count in sorted(
            semantic_grant_counts.items()
        )
        if count > 1
    ]
    if duplicate_semantic_grants:
        conflicts.append(
            {
                "reason_code": "duplicate_semantic_role_assignment",
                "message": (
                    "A semantic administrator role has more than one canonical "
                    "assignment and requires reconciliation before migration."
                ),
                "assignments": duplicate_semantic_grants,
            }
        )

    membership_orgs = represented_membership_orgs
    target_extra_org_roles = sorted(
        {
            _clean(row.get("organisation_concept_id"))
            for row in semantic_rows
            if isinstance(row, Mapping)
            and _clean(row.get("subject_concept_id")) == target_id
            and _normalise_role(row.get("role"))
            == ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE
            and _clean(row.get("organisation_concept_id")) not in membership_orgs
        }
    )
    if target_extra_org_roles:
        conflicts.append(
            {
                "reason_code": "semantic_role_without_membership",
                "message": (
                    "Michael already has organisation semantic authority outside "
                    "his represented memberships."
                ),
                "organisation_concept_ids": target_extra_org_roles,
            }
        )

    ambiguous_target_membership_roles = [
        dict(row)
        for row in inventory.get("target_membership_role_assignments") or []
        if isinstance(row, Mapping)
        and _normalise_role(row.get("role"))
        not in {"viewer", "member", "contributor", "admin", "owner"}
    ]
    if ambiguous_target_membership_roles:
        conflicts.append(
            {
                "reason_code": "orthogonal_or_custom_membership_role_requires_review",
                "message": (
                    "A contextual role is not a recognised organisation membership "
                    "rank and will not be replaced automatically."
                ),
                "assignments": ambiguous_target_membership_roles,
            }
        )

    global_roles = [
        row
        for row in semantic_rows
        if isinstance(row, Mapping)
        and _normalise_role(row.get("role")) == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE
    ]
    if semantic_rows and not global_roles:
        conflicts.append(
            {
                "reason_code": "semantic_roles_exist_without_global_root",
                "message": (
                    "Semantic organisation roles exist without a global root, so "
                    "the one-time bootstrap cannot safely proceed."
                ),
            }
        )
    return conflicts


def _role_is_present(
    semantic_rows: list[Any],
    *,
    role: str,
    organisation_concept_id: str | None,
) -> bool:
    return any(
        isinstance(row, Mapping)
        and _clean(row.get("subject_concept_id"))
        == ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET
        and _normalise_role(row.get("role")) == role
        and _normalise_concept_id(row.get("organisation_concept_id"))
        == organisation_concept_id
        for row in semantic_rows
    )


def plan_ontology_authority_role_migration() -> dict[str, Any]:
    """Return a stable, non-mutating migration plan bound to current inventory."""

    inventory = _collect_inventory()
    fingerprint = _stable_fingerprint(inventory)
    conflicts = _inventory_conflicts(inventory)
    semantic_rows = list(inventory.get("semantic_role_assignments") or [])
    operational_rows = list(inventory.get("operational_role_assignments") or [])
    target_operational_role_present = (
        len(operational_rows) == 1
        and _clean(operational_rows[0].get("subject_concept_id"))
        == ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET
    )
    actions: list[dict[str, Any]] = [
        {
            "operation": "bootstrap_von_operational_administrator",
            "subject_concept_id": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
            "role": VON_OPERATIONAL_ADMINISTRATOR_ROLE,
            "organisation_concept_id": None,
            "status": (
                "already_satisfied" if target_operational_role_present else "required"
            ),
        },
        {
            "operation": "bootstrap_global_ontology_administrator",
            "subject_concept_id": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
            "role": GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
            "organisation_concept_id": None,
            "status": (
                "already_satisfied"
                if _role_is_present(
                    semantic_rows,
                    role=GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
                    organisation_concept_id=None,
                )
                else "required"
            ),
        },
    ]
    for membership in inventory.get("memberships") or []:
        if not isinstance(membership, Mapping):
            continue
        organisation_id = _normalise_concept_id(
            membership.get("organisation_concept_id")
        )
        if not organisation_id:
            continue
        actions.append(
            {
                "operation": "grant_organisation_ontology_administrator",
                "subject_concept_id": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
                "role": ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
                "organisation_concept_id": organisation_id,
                "status": (
                    "already_satisfied"
                    if _role_is_present(
                        semantic_rows,
                        role=ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
                        organisation_concept_id=organisation_id,
                    )
                    else "required"
                ),
            }
        )
    membership_role_rows = list(
        inventory.get("target_membership_role_assignments") or []
    )
    for membership in inventory.get("memberships") or []:
        if not isinstance(membership, Mapping):
            continue
        organisation_id = _normalise_concept_id(
            membership.get("organisation_concept_id")
        )
        if not organisation_id:
            continue
        exact_rows = [
            row
            for row in membership_role_rows
            if isinstance(row, Mapping)
            and _normalise_concept_id(row.get("organisation_concept_id"))
            == organisation_id
        ]
        exact_owner = (
            len(exact_rows) == 1
            and _normalise_role(exact_rows[0].get("role")) == _OWNER_ROLE
        )
        actions.append(
            {
                "operation": "replace_legacy_organisation_role_with_owner",
                "subject_concept_id": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
                "role": _OWNER_ROLE,
                "organisation_concept_id": organisation_id,
                "status": "already_satisfied" if exact_owner else "required",
                "runs_after_semantic_role_verification": True,
            }
        )
    return {
        "schema_version": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_SCHEMA_VERSION,
        "mode": "dry_run",
        "target_concept_id": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
        "inventory_fingerprint": fingerprint,
        "ready_to_apply": not conflicts,
        "conflicts": conflicts,
        "inventory": inventory,
        "actions": actions,
        "operational_authority": {
            "role_concept_id": VON_ADMINISTRATOR_CONCEPT_ID,
            "represented_assignment_mechanism": "dedicated_role_lifecycle",
            "migration_behaviour": (
                "preserve_existing_operator_controls_and_represent_michael_as_"
                "von_operational_administrator_separately_from_semantic_authority"
            ),
            "semantic_authority_predicate": AUTHORITY_ROLE_PREDICATE,
            "kept_separate_from_semantic_authority": True,
            "not_inferred_from_semantic_roles": True,
            "owner_permissions": sorted(get_effective_permissions(_OWNER_ROLE)),
        },
        "elevation_boundary": {
            "allowed_subject_concept_ids": [ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET],
            "other_subjects_elevated": False,
        },
    }


def _request_id(role: str, organisation_concept_id: str | None) -> str:
    digest = hashlib.sha256(
        "|".join(
            (
                ONTOLOGY_AUTHORITY_ROLE_MIGRATION_SCHEMA_VERSION,
                ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
                role,
                organisation_concept_id or "global",
            )
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"ontology-authority-role-migration-{digest}"


def _verified_read_back(
    *,
    role: str,
    organisation_concept_id: str | None,
) -> dict[str, Any]:
    read_back = authority_role_read_back(
        subject_concept_id=ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
        role=role,
        organisation_concept_id=organisation_concept_id,
    )
    if read_back.get("active") is not True or not list(read_back.get("grants") or []):
        raise OntologyAuthorityRoleMigrationError(
            "ontology_authority_role_migration_read_back_failed",
            "The exact migrated semantic role could not be read back.",
            report={"canonical_read_back": read_back},
        )
    return read_back


def _verified_operational_read_back() -> dict[str, Any]:
    read_back = von_operational_administrator_read_back(
        subject_concept_id=ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
    )
    if read_back.get("active") is not True or len(read_back.get("grants") or []) != 1:
        raise OntologyAuthorityRoleMigrationError(
            "ontology_authority_role_migration_operational_read_back_failed",
            "The exact Von operational administrator role could not be read back.",
            report={"canonical_read_back": read_back},
        )
    return read_back


def _membership_role_read_back(organisation_concept_id: str) -> dict[str, Any]:
    organisation_id = _normalise_concept_id(organisation_concept_id)
    rows = [
        row
        for row in _target_membership_role_assignments()
        if _normalise_concept_id(row.get("organisation_concept_id")) == organisation_id
    ]
    exact_owner = len(rows) == 1 and _normalise_role(rows[0].get("role")) == _OWNER_ROLE
    return {
        "subject_concept_id": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
        "organisation_concept_id": organisation_id,
        "role": _OWNER_ROLE if exact_owner else None,
        "exact_owner": exact_owner,
        "role_assignments": rows,
        "permissions": (
            sorted(get_effective_permissions(_OWNER_ROLE)) if exact_owner else []
        ),
    }


def _replace_membership_role_with_owner(
    organisation_concept_id: str,
) -> dict[str, Any]:
    """Replace exact-org legacy role text only after semantic-role verification."""

    organisation_id = _normalise_concept_id(organisation_concept_id)
    if not organisation_id:
        raise OntologyAuthorityRoleMigrationError(
            "ontology_authority_role_migration_membership_required",
            "An exact represented organisation membership is required.",
        )
    before = _membership_role_read_back(organisation_id)
    if before["exact_owner"] is True:
        return {
            "success": True,
            "changed": False,
            "canonical_read_back": before,
        }

    with override_current_organisation(organisation_id):
        owner_result = upsert_text_for_concept(
            subject_concept_id=ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
            predicate=ROLE_PREDICATE,
            text=organisation_role_storage_text(_OWNER_ROLE, organisation_id),
            lang="en",
            provenance={"source": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_SCHEMA_VERSION},
            context={
                "organisation_id": organisation_id,
                "role_migration": {
                    "schema_version": (
                        ONTOLOGY_AUTHORITY_ROLE_MIGRATION_SCHEMA_VERSION
                    ),
                    "reason": _MIGRATION_REASON,
                },
            },
        )
        owner_relation_id = _clean(owner_result.get("relation_id"))
        after_owner_insert = _membership_role_read_back(organisation_id)
        if not owner_relation_id or not any(
            _clean(row.get("relation_id")) == owner_relation_id
            and _normalise_role(row.get("role")) == _OWNER_ROLE
            for row in after_owner_insert.get("role_assignments") or []
            if isinstance(row, Mapping)
        ):
            raise OntologyAuthorityRoleMigrationError(
                "ontology_authority_role_migration_owner_read_back_failed",
                "The owner role was not readable before legacy role removal.",
                report={"canonical_read_back": after_owner_insert},
            )

        deleted_relation_ids: list[str] = []
        for row in after_owner_insert.get("role_assignments") or []:
            if not isinstance(row, Mapping):
                continue
            relation_id = _clean(row.get("relation_id"))
            if (
                _normalise_role(row.get("role")) == _OWNER_ROLE
                and relation_id == owner_relation_id
            ):
                continue
            if not relation_id:
                raise OntologyAuthorityRoleMigrationError(
                    "ontology_authority_role_migration_legacy_role_unaddressable",
                    "A legacy organisation role has no canonical relation identifier.",
                    report={"legacy_role": dict(row)},
                )
            deletion = delete_text_relation(
                subject_concept_id=ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
                relation_id=relation_id,
                garbage_collect=False,
            )
            if deletion.get("deleted") is not True:
                raise OntologyAuthorityRoleMigrationError(
                    "ontology_authority_role_migration_legacy_role_delete_failed",
                    "A superseded organisation role could not be removed.",
                    report={"legacy_role": dict(row)},
                )
            deleted_relation_ids.append(relation_id)

    after = _membership_role_read_back(organisation_id)
    if after["exact_owner"] is not True:
        raise OntologyAuthorityRoleMigrationError(
            "ontology_authority_role_migration_owner_read_back_failed",
            "The exact organisation owner role could not be read back.",
            report={"canonical_read_back": after},
        )
    return {
        "success": True,
        "changed": True,
        "owner_relation_id": owner_relation_id,
        "deleted_relation_ids": deleted_relation_ids,
        "canonical_read_back": after,
    }


def _canonical_action_read_back(action: Mapping[str, Any]) -> dict[str, Any]:
    operation = _clean(action.get("operation"))
    organisation_id = _normalise_concept_id(action.get("organisation_concept_id"))
    if operation == "bootstrap_von_operational_administrator":
        return _verified_operational_read_back()
    if operation == "replace_legacy_organisation_role_with_owner":
        return _membership_role_read_back(str(organisation_id or ""))
    return _verified_read_back(
        role=_normalise_role(action.get("role")),
        organisation_concept_id=organisation_id,
    )


def _execute_migration_action(action: Mapping[str, Any]) -> dict[str, Any]:
    operation = _clean(action.get("operation"))
    role = _normalise_role(action.get("role"))
    organisation_id = _normalise_concept_id(action.get("organisation_concept_id"))
    if operation == "bootstrap_von_operational_administrator":
        result = bootstrap_first_von_operational_administrator(
            subject_concept_id=ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
        )
    elif operation == "replace_legacy_organisation_role_with_owner":
        result = _replace_membership_role_with_owner(str(organisation_id or ""))
    elif role == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE:
        result = bootstrap_first_semantic_authority(
            subject_concept_id=ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
            role=GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
        )
    elif role == ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE and organisation_id:
        with override_current_organisation(organisation_id):
            result = grant_ontology_authority_role(
                subject_concept_id=ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
                role=ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
                organisation_concept_id=organisation_id,
                request_id=_request_id(role, organisation_id),
                reason=_MIGRATION_REASON,
            )
        if result.get("success") is not True:
            raise OntologyAuthorityRoleMigrationError(
                "ontology_authority_role_migration_semantic_grant_failed",
                "A semantic role grant did not complete successfully.",
                report={"canonical_result": result},
            )
    else:
        raise OntologyAuthorityRoleMigrationError(
            "ontology_authority_role_migration_unknown_action",
            "The migration plan contains an unsupported action.",
            report={"action": dict(action)},
        )
    return {
        **dict(action),
        "effect_status": "succeeded",
        "canonical_result": result,
        "canonical_read_back": _canonical_action_read_back(action),
    }


def _ordered_effects(
    actions: list[Any],
    effects_by_key: Mapping[tuple[str, str, str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        dict(effects_by_key[_action_key(action)])
        for action in actions
        if isinstance(action, Mapping) and _action_key(action) in effects_by_key
    ]


def _record_partial_safely(report: Mapping[str, Any]) -> None:
    try:
        _update_migration_operation(
            status="partial",
            completed_effects=list(report.get("completed_effects") or []),
            last_failure=dict(report),
        )
    except Exception:  # noqa: BLE001 - never obscure canonical partial evidence
        return


def apply_ontology_authority_role_migration(
    *,
    expected_inventory_fingerprint: str,
) -> dict[str, Any]:
    """Apply the inventory-bound migration through canonical role services.

    The first apply records its source plan before any authority effect. A retry
    with that same fingerprint resumes only if the membership/action inventory
    differs by already-satisfied migration effects and no conflicting holder
    has appeared. Each completed effect carries canonical exact-role read-back.
    """

    actor_id = _normalise_concept_id(get_effective_user_concept_id())
    if actor_id != ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET:
        raise OntologyAuthorityRoleMigrationError(
            "ontology_authority_role_migration_target_actor_required",
            "The migration must run in Michael's trusted actor context.",
        )
    invocation = current_ontology_invocation()
    if invocation and invocation.executing_agent_concept_id:
        raise OntologyAuthorityRoleMigrationError(
            "ontology_authority_role_migration_human_actor_required",
            "An executing agent cannot perform the administrator-role migration.",
        )
    expected_fingerprint = _clean(expected_inventory_fingerprint)
    if not expected_fingerprint:
        raise OntologyAuthorityRoleMigrationError(
            "ontology_authority_role_migration_fingerprint_required",
            "Apply requires the exact fingerprint from a current dry-run.",
        )

    plan: dict[str, Any] | None = None
    effects_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    current_action: dict[str, Any] | None = None
    try:
        # Lock ordering is always shared barrier -> migration lock -> dedicated
        # role lock. Canonical role/membership writers enter the same barrier,
        # whose ContextVar makes the migration's nested service calls re-entrant.
        with ontology_authority_membership_mutation_barrier():  # noqa: SIM117
            with ontology_mutation_resource_lock(_MIGRATION_RESOURCE_KEY):
                plan = plan_ontology_authority_role_migration()
                operation = _load_migration_operation()
                if operation is None:
                    if plan["inventory_fingerprint"] != expected_fingerprint:
                        raise OntologyAuthorityRoleMigrationError(
                            "ontology_authority_role_migration_inventory_changed",
                            "The live role inventory changed after the dry-run.",
                            report=plan,
                        )
                    if plan.get("ready_to_apply") is not True:
                        raise OntologyAuthorityRoleMigrationError(
                            "ontology_authority_role_migration_inventory_conflict",
                            "The live role inventory contradicts the migration premise.",
                            report=plan,
                        )
                    operation = _start_migration_operation(
                        source_plan=plan,
                        source_fingerprint=expected_fingerprint,
                    )
                else:
                    source_plan_value = operation.get("source_plan")
                    source_plan = (
                        source_plan_value
                        if isinstance(source_plan_value, Mapping)
                        else None
                    )
                    source_fingerprint = _clean(
                        operation.get("source_inventory_fingerprint")
                    )
                    accepted_fingerprint = expected_fingerprint in {
                        source_fingerprint,
                        _clean(plan.get("inventory_fingerprint")),
                    }
                    completed_state_still_exact = not (
                        operation.get("status") == "completed"
                        and any(
                            isinstance(action, Mapping)
                            and action.get("status") != "already_satisfied"
                            for action in plan.get("actions") or []
                        )
                    )
                    if (
                        source_plan is None
                        or not accepted_fingerprint
                        or not _continuation_is_safe(source_plan, plan)
                        or not completed_state_still_exact
                    ):
                        reason_code = (
                            "ontology_authority_role_migration_completed_state_changed"
                            if operation.get("status") == "completed"
                            else "ontology_authority_role_migration_inventory_changed"
                        )
                        raise OntologyAuthorityRoleMigrationError(
                            reason_code,
                            "The live role inventory is not a safe continuation of the dry-run.",
                            report={
                                "source_plan": dict(source_plan or {}),
                                "current_plan": plan,
                                "operation_status": operation.get("status"),
                            },
                        )

                for effect in operation.get("completed_effects") or []:
                    if isinstance(effect, Mapping):
                        effects_by_key[_action_key(effect)] = dict(effect)

                changed = False
                actions = list(plan.get("actions") or [])
                for raw_action in actions:
                    if not isinstance(raw_action, Mapping):
                        continue
                    current_action = dict(raw_action)
                    key = _action_key(current_action)
                    if current_action.get("status") == "already_satisfied":
                        existing_effect = effects_by_key.get(key, {})
                        effects_by_key[key] = {
                            **current_action,
                            **{
                                name: value
                                for name, value in existing_effect.items()
                                if name not in {"status", "canonical_read_back"}
                            },
                            "effect_status": existing_effect.get(
                                "effect_status", "already_satisfied"
                            ),
                            "canonical_read_back": _canonical_action_read_back(
                                current_action
                            ),
                        }
                    else:
                        effect = _execute_migration_action(current_action)
                        effects_by_key[key] = effect
                        result = effect.get("canonical_result")
                        result_map = result if isinstance(result, Mapping) else {}
                        changed = changed or bool(result_map.get("changed", True))

                    completed_effects = _ordered_effects(actions, effects_by_key)
                    _update_migration_operation(
                        status="in_progress",
                        completed_effects=completed_effects,
                        last_completed_action=list(key),
                    )

                effects = _ordered_effects(actions, effects_by_key)
                final_plan = plan_ontology_authority_role_migration()
                remaining = [
                    action
                    for action in final_plan.get("actions") or []
                    if isinstance(action, Mapping)
                    and action.get("status") != "already_satisfied"
                ]
                if (
                    final_plan.get("ready_to_apply") is not True
                    or remaining
                    or not _continuation_is_safe(plan, final_plan)
                ):
                    raise OntologyAuthorityRoleMigrationError(
                        "ontology_authority_role_migration_final_read_back_failed",
                        "Final inventory does not contain exactly the required roles.",
                        report={
                            "plan_before": plan,
                            "completed_effects": effects,
                            "plan_after": final_plan,
                        },
                    )

                semantic_read_backs = [
                    effect["canonical_read_back"]
                    for effect in effects
                    if _normalise_role(effect.get("role"))
                    in {
                        GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
                        ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
                    }
                ]
                owner_read_backs = [
                    effect["canonical_read_back"]
                    for effect in effects
                    if _clean(effect.get("operation"))
                    == "replace_legacy_organisation_role_with_owner"
                ]
                result = {
                    "schema_version": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_SCHEMA_VERSION,
                    "mode": "apply",
                    "success": True,
                    "changed": changed,
                    "replayed": _clean(operation.get("source_inventory_fingerprint"))
                    != _clean(plan.get("inventory_fingerprint")),
                    "target_concept_id": ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
                    "inventory_fingerprint_before": _clean(
                        operation.get("source_inventory_fingerprint")
                    ),
                    "inventory_fingerprint_after": final_plan["inventory_fingerprint"],
                    "effects": effects,
                    "canonical_read_back": {
                        "von_operational_administrator": (
                            _verified_operational_read_back()
                        ),
                        "semantic_role_grants": semantic_read_backs,
                        "organisation_owner_roles": owner_read_backs,
                        "semantic_role_assignments": final_plan["inventory"][
                            "semantic_role_assignments"
                        ],
                        "operational_role_assignments": final_plan["inventory"][
                            "operational_role_assignments"
                        ],
                        "memberships": final_plan["inventory"]["memberships"],
                    },
                }
                _update_migration_operation(
                    status="completed",
                    completed_effects=effects,
                    completed_at=_now(),
                    result=result,
                )
                return result
    except OntologyMutationResourceBusy as exc:
        raise OntologyAuthorityRoleMigrationError(
            "ontology_mutation_resource_busy",
            "Another administrator-role migration is already in progress.",
        ) from exc
    except OntologyAuthorityRoleMigrationError as exc:
        if (
            exc.reason_code
            in {
                "ontology_authority_role_migration_inventory_changed",
                "ontology_authority_role_migration_inventory_conflict",
                "ontology_authority_role_migration_completed_state_changed",
            }
            and not effects_by_key
        ):
            raise
        completed_effects = _ordered_effects(
            list(plan.get("actions") or []) if isinstance(plan, Mapping) else [],
            effects_by_key,
        )
        report = {
            "plan": plan,
            "completed_effects": completed_effects,
            "failed_effect": current_action,
            "failure_reason_code": exc.reason_code,
            "failure_report": exc.report,
        }
        _record_partial_safely(report)
        raise OntologyAuthorityRoleMigrationError(
            "ontology_authority_role_migration_partial",
            "The migration did not complete; verified effects are safe to resume.",
            report=report,
        ) from exc
    except (PermissionError, ValueError, RuntimeError) as exc:
        completed_effects = _ordered_effects(
            list(plan.get("actions") or []) if isinstance(plan, Mapping) else [],
            effects_by_key,
        )
        report = {
            "plan": plan,
            "completed_effects": completed_effects,
            "failed_effect": current_action,
            "failure_type": type(exc).__name__,
        }
        _record_partial_safely(report)
        raise OntologyAuthorityRoleMigrationError(
            "ontology_authority_role_migration_partial",
            "The migration did not complete; verified effects are safe to resume.",
            report=report,
        ) from exc


__all__ = [
    "ONTOLOGY_AUTHORITY_ROLE_MIGRATION_SCHEMA_VERSION",
    "ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET",
    "OntologyAuthorityRoleMigrationError",
    "apply_ontology_authority_role_migration",
    "plan_ontology_authority_role_migration",
]
