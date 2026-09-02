"""Migrate operational organisation membership to narrow predicates.

The historical implementation treated generic ``memberOf`` assertions as Von
access grants.  This migration deliberately does not copy every generic
membership edge.  An old edge is eligible only when it is paired with the
scoped legacy role relation written by the former governed membership
lifecycle.  Generic-only ontology facts remain descriptive and grant nothing.

The migration is non-destructive and idempotent.  It creates the narrow
predicate vocabulary, represents that ``memberOfVonOrg`` specialises (and
therefore entails) ``memberOf``, and copies eligible operational records to the
narrow membership and role predicates.  Legacy assertions are retained as
historical semantic data but are no longer read for authority after cutover.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import bypass_access_control
from ..security.role_resolver import AVAILABLE_ROLES
from . import concept_service
from .organisation_membership_service import (
    GENERIC_MEMBERSHIP_PREDICATE,
    LEGACY_MEMBERSHIP_RELATIONSHIP_KINDS,
    LEGACY_ROLE_PREDICATES,
    MEMBERSHIP_RELATIONSHIP_KIND,
    PREDICATE_SPECIALISATION_PREDICATE,
    ROLE_PREDICATE,
    create_organisation_membership,
    parse_organisation_role_storage_text,
    resolve_user_organisation_membership,
)
from .relationship_write_service import add_dynamic_relationship

MIGRATION_SCHEMA_VERSION = "von_organisation_membership_predicate_migration.v1"
AUDIT_SCHEMA_VERSION = "von_organisation_membership_predicate_audit.v1"


def _normalise_concept_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    return cleaned if cleaned.startswith("#V#") else f"#V#{cleaned}"


def _normalise_values(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in values:
        normalised = _normalise_concept_id(item)
        if normalised and normalised not in output:
            output.append(normalised)
    return output


def _normalise_role_overrides(
    overrides: Iterable[Mapping[str, Any]] | None,
) -> dict[tuple[str, str], str]:
    """Validate exact operator choices for otherwise ambiguous legacy pairs."""

    normalised: dict[tuple[str, str], str] = {}
    for override in overrides or ():
        if not isinstance(override, Mapping):
            raise ValueError("membership_role_override_not_mapping")
        user_id = _normalise_concept_id(override.get("user_concept_id"))
        organisation_id = _normalise_concept_id(override.get("organisation_concept_id"))
        role = str(override.get("role") or "").strip()
        if user_id is None or organisation_id is None:
            raise ValueError("membership_role_override_pair_required")
        if role not in AVAILABLE_ROLES:
            raise ValueError("membership_role_override_invalid_role")
        pair = (user_id, organisation_id)
        previous = normalised.get(pair)
        if previous is not None and previous != role:
            raise ValueError("membership_role_override_conflicts_with_itself")
        normalised[pair] = role
    return normalised


def _legacy_edge_pairs() -> dict[tuple[str, str], set[str]]:
    query = {
        "$or": [
            {f"relationships.{predicate}": {"$exists": True}}
            for predicate in LEGACY_MEMBERSHIP_RELATIONSHIP_KINDS
        ]
    }
    projection = {
        "_id": 0,
        "concept_id": 1,
        **{
            f"relationships.{predicate}": 1
            for predicate in LEGACY_MEMBERSHIP_RELATIONSHIP_KINDS
        },
    }
    pairs: dict[tuple[str, str], set[str]] = {}
    with bypass_access_control():
        documents = ConceptsRepository.find(query, projection=projection)
        for document in documents:
            if not isinstance(document, Mapping):
                continue
            user_id = _normalise_concept_id(document.get("concept_id"))
            relationships = document.get("relationships")
            if user_id is None or not isinstance(relationships, Mapping):
                continue
            for predicate in LEGACY_MEMBERSHIP_RELATIONSHIP_KINDS:
                for organisation_id in _normalise_values(relationships.get(predicate)):
                    pairs.setdefault((user_id, organisation_id), set()).add(predicate)
    return pairs


def _legacy_role_pairs() -> tuple[
    dict[tuple[str, str], set[str]],
    list[dict[str, Any]],
]:
    roles: dict[tuple[str, str], set[str]] = {}
    invalid: list[dict[str, Any]] = []
    relations = TextRelationsRepository.find(
        {"predicate": {"$in": list(LEGACY_ROLE_PREDICATES)}}
    )
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        user_id = _normalise_concept_id(relation.get("subject_concept_id"))
        context = relation.get("context")
        context_map = dict(context) if isinstance(context, Mapping) else {}
        organisation_id = _normalise_concept_id(
            context_map.get("organisation_concept_id")
            or context_map.get("organisation_id")
        )
        text_value_id = relation.get("object_text_id")
        text_value = (
            TextValuesRepository.find_one_by_id(text_value_id)
            if text_value_id is not None
            else None
        )
        role = None
        stored_organisation_id = None
        if isinstance(text_value, Mapping):
            role, stored_organisation_id = parse_organisation_role_storage_text(
                text_value.get("text"),
                context_map,
            )
        if stored_organisation_id is not None:
            organisation_id = stored_organisation_id
        if user_id is None or organisation_id is None or role not in AVAILABLE_ROLES:
            invalid.append(
                {
                    "relation_id": str(relation.get("_id") or ""),
                    "user_concept_id": user_id,
                    "organisation_concept_id": organisation_id,
                    "reason_code": "legacy_role_relation_invalid",
                }
            )
            continue
        roles.setdefault((user_id, organisation_id), set()).add(role)
    return roles, invalid


def _narrow_membership_pairs() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    with bypass_access_control():
        documents = ConceptsRepository.find(
            {f"relationships.{MEMBERSHIP_RELATIONSHIP_KIND}": {"$exists": True}},
            projection={
                "_id": 0,
                "concept_id": 1,
                f"relationships.{MEMBERSHIP_RELATIONSHIP_KIND}": 1,
            },
        )
        for document in documents:
            if not isinstance(document, Mapping):
                continue
            user_id = _normalise_concept_id(document.get("concept_id"))
            relationships = document.get("relationships")
            if user_id is None or not isinstance(relationships, Mapping):
                continue
            for organisation_id in _normalise_values(
                relationships.get(MEMBERSHIP_RELATIONSHIP_KIND)
            ):
                pairs.add((user_id, organisation_id))
    return pairs


def audit_von_organisation_membership_predicates(
    *, sample_limit: int = 20
) -> dict[str, Any]:
    """Classify legacy facts without treating generic membership as authority."""

    sample_limit = max(0, int(sample_limit))
    edge_pairs = _legacy_edge_pairs()
    role_pairs, invalid_roles = _legacy_role_pairs()
    narrow_pairs = _narrow_membership_pairs()

    eligible: list[dict[str, Any]] = []
    generic_only: list[dict[str, Any]] = []
    role_only: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for pair in sorted(set(edge_pairs) | set(role_pairs)):
        user_id, organisation_id = pair
        predicates = sorted(edge_pairs.get(pair, set()))
        roles = sorted(role_pairs.get(pair, set()))
        common = {
            "user_concept_id": user_id,
            "organisation_concept_id": organisation_id,
        }
        if predicates and len(roles) == 1:
            eligible.append(
                {
                    **common,
                    "role": roles[0],
                    "legacy_membership_predicates": predicates,
                    "already_narrow": pair in narrow_pairs,
                }
            )
        elif predicates and not roles:
            generic_only.append({**common, "legacy_membership_predicates": predicates})
        elif roles and not predicates:
            role_only.append({**common, "legacy_roles": roles})
        else:
            conflicts.append(
                {
                    **common,
                    "legacy_membership_predicates": predicates,
                    "legacy_roles": roles,
                    "already_narrow": pair in narrow_pairs,
                    "reason_code": "conflicting_legacy_roles",
                }
            )

    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "narrow_membership_predicate": MEMBERSHIP_RELATIONSHIP_KIND,
        "narrow_role_predicate": ROLE_PREDICATE,
        "entailed_generic_predicate": GENERIC_MEMBERSHIP_PREDICATE,
        "predicate_specialisation_predicate": (PREDICATE_SPECIALISATION_PREDICATE),
        "eligible_operational_membership_count": len(eligible),
        "already_narrow_count": sum(
            1 for record in eligible if record["already_narrow"]
        ),
        "generic_only_non_authority_count": len(generic_only),
        "role_only_count": len(role_only),
        "conflict_count": len(conflicts),
        "invalid_legacy_role_count": len(invalid_roles),
        "eligible_operational_memberships": eligible[:sample_limit],
        "generic_only_non_authority_samples": generic_only[:sample_limit],
        "role_only_samples": role_only[:sample_limit],
        "conflict_samples": conflicts[:sample_limit],
        "invalid_legacy_role_samples": invalid_roles[:sample_limit],
        "complete": True,
    }


def _ensure_predicate(
    *,
    concept_id: str,
    name: str,
    description: str,
    predicate_type_id: str,
) -> str:
    with bypass_access_control():
        existing = ConceptsRepository.find_one({"concept_id": concept_id})
    if not isinstance(existing, Mapping):
        with bypass_access_control():
            concept_service.create_concept(
                name=name,
                concept_id=concept_id,
                description=description,
                parent_concept_ids=[predicate_type_id],
                create_as_instance=True,
                visibility_scope_mode="global_general",
                maintain_relationship_inverses=False,
            )
        state = "created"
    else:
        state = "existing"

    with bypass_access_control():
        read_back = ConceptsRepository.find_one(
            {"concept_id": concept_id},
            {"_id": 0, "concept_id": 1, "relationships.is_an_instance_of": 1},
        )
    if not isinstance(read_back, Mapping):
        raise RuntimeError(f"predicate_read_back_failed:{concept_id}")
    relationships = read_back.get("relationships")
    types = _normalise_values(
        relationships.get("is_an_instance_of")
        if isinstance(relationships, Mapping)
        else None
    )
    if predicate_type_id not in types:
        raise RuntimeError(f"predicate_type_read_back_failed:{concept_id}")
    return state


def ensure_von_organisation_membership_vocabulary() -> dict[str, Any]:
    """Materialise the narrow predicates and their one-way entailment."""

    states = {
        MEMBERSHIP_RELATIONSHIP_KIND: _ensure_predicate(
            concept_id=MEMBERSHIP_RELATIONSHIP_KIND,
            name="Member of Von organisation",
            description=(
                "An explicit operational Von membership assertion. It entails "
                "ordinary memberOf, but generic memberOf assertions do not imply "
                "this predicate and grant no Von authority."
            ),
            predicate_type_id="#V#binary_predicate",
        ),
        ROLE_PREDICATE: _ensure_predicate(
            concept_id=ROLE_PREDICATE,
            name="Has Von organisation role",
            description=(
                "Stores the operational role for one explicit Von organisation "
                "membership, scoped to that organisation in relation context."
            ),
            predicate_type_id="#V#binary_text_predicate",
        ),
    }
    with bypass_access_control():
        result = add_dynamic_relationship(
            MEMBERSHIP_RELATIONSHIP_KIND,
            PREDICATE_SPECIALISATION_PREDICATE,
            GENERIC_MEMBERSHIP_PREDICATE,
        )
    if result.get("success") is not True:
        raise RuntimeError(
            "membership_predicate_specialisation_write_failed:"
            f"{result.get('error') or 'unknown'}"
        )
    with bypass_access_control():
        narrow = ConceptsRepository.find_one(
            {"concept_id": MEMBERSHIP_RELATIONSHIP_KIND},
            {
                "_id": 0,
                "concept_id": 1,
                f"relationships.{PREDICATE_SPECIALISATION_PREDICATE}": 1,
            },
        )
    relationships = narrow.get("relationships") if isinstance(narrow, Mapping) else {}
    targets = _normalise_values(
        relationships.get(PREDICATE_SPECIALISATION_PREDICATE)
        if isinstance(relationships, Mapping)
        else None
    )
    if GENERIC_MEMBERSHIP_PREDICATE not in targets:
        raise RuntimeError("membership_predicate_specialisation_read_back_failed")
    return {
        "success": True,
        "predicate_states": states,
        "entailment": {
            "specific_predicate": MEMBERSHIP_RELATIONSHIP_KIND,
            "relation": PREDICATE_SPECIALISATION_PREDICATE,
            "generic_predicate": GENERIC_MEMBERSHIP_PREDICATE,
            "reverse_inference_allowed": False,
        },
    }


def migrate_von_organisation_membership_predicates(
    *,
    dry_run: bool = True,
    approved: bool = False,
    sample_limit: int = 20,
    role_overrides: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Copy only evidenced operational memberships to the narrow vocabulary."""

    try:
        normalised_overrides = _normalise_role_overrides(role_overrides)
    except ValueError as exc:
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "success": False,
            "effect_status": "not_started",
            "error_code": str(exc),
        }

    audit = audit_von_organisation_membership_predicates(
        sample_limit=max(sample_limit, 100_000)
    )
    sample_count = max(0, int(sample_limit))
    public_audit = {
        **audit,
        "eligible_operational_memberships": audit["eligible_operational_memberships"][
            :sample_count
        ],
        "generic_only_non_authority_samples": audit[
            "generic_only_non_authority_samples"
        ][:sample_count],
        "role_only_samples": audit["role_only_samples"][:sample_count],
        "conflict_samples": audit["conflict_samples"][:sample_count],
        "invalid_legacy_role_samples": audit["invalid_legacy_role_samples"][
            :sample_count
        ],
    }
    eligible_records = list(audit["eligible_operational_memberships"])
    resolved_conflicts: list[dict[str, Any]] = []
    unresolved_conflicts: list[dict[str, Any]] = []
    override_errors: list[dict[str, Any]] = []
    unused_override_pairs = set(normalised_overrides)
    for conflict in audit["conflict_samples"]:
        pair = (
            conflict["user_concept_id"],
            conflict["organisation_concept_id"],
        )
        selected_role = normalised_overrides.get(pair)
        if selected_role is None:
            unresolved_conflicts.append(conflict)
            continue
        unused_override_pairs.discard(pair)
        if selected_role not in conflict.get("legacy_roles", []):
            override_errors.append(
                {
                    "user_concept_id": pair[0],
                    "organisation_concept_id": pair[1],
                    "requested_role": selected_role,
                    "legacy_roles": list(conflict.get("legacy_roles", [])),
                    "reason_code": "role_override_not_present_in_legacy_evidence",
                }
            )
            continue
        resolved = {
            "user_concept_id": pair[0],
            "organisation_concept_id": pair[1],
            "role": selected_role,
            "legacy_membership_predicates": list(
                conflict.get("legacy_membership_predicates", [])
            ),
            "legacy_roles": list(conflict.get("legacy_roles", [])),
            "already_narrow": bool(conflict.get("already_narrow")),
            "role_override_applied": True,
        }
        resolved_conflicts.append(resolved)
        eligible_records.append(resolved)
    for pair in sorted(unused_override_pairs):
        override_errors.append(
            {
                "user_concept_id": pair[0],
                "organisation_concept_id": pair[1],
                "requested_role": normalised_overrides[pair],
                "reason_code": "role_override_does_not_match_a_conflicted_pair",
            }
        )
    common = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "dry_run": bool(dry_run),
        "approved": bool(approved),
        "audit": public_audit,
        "resolved_conflict_count": len(resolved_conflicts),
        "resolved_conflicts": resolved_conflicts[:sample_count],
        "unresolved_conflict_count": len(unresolved_conflicts),
        "unresolved_conflicts": unresolved_conflicts[:sample_count],
        "role_override_error_count": len(override_errors),
        "role_override_errors": override_errors[:sample_count],
    }
    if not dry_run and not approved:
        return {
            **common,
            "success": False,
            "effect_status": "not_started",
            "error_code": "explicit_migration_approval_required",
        }
    if unresolved_conflicts or override_errors or audit["invalid_legacy_role_count"]:
        return {
            **common,
            "success": False,
            "effect_status": "not_started",
            "error_code": "legacy_membership_inventory_requires_resolution",
        }
    if dry_run:
        return {
            **common,
            "success": True,
            "effect_status": "not_started",
            "would_migrate_count": len(eligible_records),
            "generic_only_relations_will_grant_authority": False,
        }

    vocabulary = ensure_von_organisation_membership_vocabulary()
    migrated: list[dict[str, Any]] = []
    already_current: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for record in eligible_records:
        user_id = record["user_concept_id"]
        organisation_id = record["organisation_concept_id"]
        role = record["role"]
        try:
            before = resolve_user_organisation_membership(user_id, organisation_id)
            if isinstance(before, Mapping) and before.get("role") == role:
                already_current.append(
                    {
                        "user_concept_id": user_id,
                        "organisation_concept_id": organisation_id,
                        "role": role,
                    }
                )
                continue
            with bypass_access_control():
                create_organisation_membership(
                    user_concept_id=user_id,
                    organisation_concept_id=organisation_id,
                    role=role,
                )
            read_back = resolve_user_organisation_membership(user_id, organisation_id)
            if not isinstance(read_back, Mapping) or read_back.get("role") != role:
                raise RuntimeError("narrow_membership_read_back_mismatch")
            migrated.append(
                {
                    "user_concept_id": user_id,
                    "organisation_concept_id": organisation_id,
                    "role": role,
                }
            )
        except Exception as exc:  # noqa: BLE001 - report resumable partial migration
            failures.append(
                {
                    "user_concept_id": user_id,
                    "organisation_concept_id": organisation_id,
                    "error_class": type(exc).__name__,
                    "error": str(exc),
                }
            )

    missing_read_back: list[dict[str, Any]] = []
    for record in eligible_records:
        read_back = resolve_user_organisation_membership(
            record["user_concept_id"],
            record["organisation_concept_id"],
        )
        if (
            not isinstance(read_back, Mapping)
            or read_back.get("role") != record["role"]
        ):
            missing_read_back.append(
                {
                    "user_concept_id": record["user_concept_id"],
                    "organisation_concept_id": record["organisation_concept_id"],
                    "expected_role": record["role"],
                    "actual_role": (
                        read_back.get("role")
                        if isinstance(read_back, Mapping)
                        else None
                    ),
                }
            )
    success = not failures and not missing_read_back
    return {
        **common,
        "success": success,
        "effect_status": "succeeded" if success else "partial_success",
        "vocabulary": vocabulary,
        "migrated_count": len(migrated),
        "already_current_count": len(already_current),
        "failed_count": len(failures),
        "migrated": migrated[: max(0, int(sample_limit))],
        "already_current": already_current[: max(0, int(sample_limit))],
        "failures": failures[: max(0, int(sample_limit))],
        "missing_read_back_pairs": missing_read_back[:sample_count],
        "generic_only_relations_grant_authority": False,
    }


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "MIGRATION_SCHEMA_VERSION",
    "audit_von_organisation_membership_predicates",
    "ensure_von_organisation_membership_vocabulary",
    "migrate_von_organisation_membership_predicates",
]
