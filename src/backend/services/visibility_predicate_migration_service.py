"""Audit and migrate legacy visibility predicate storage.

Visibility predicate authority is represented in Vontology predicates:
``#V#specific_to_user`` and ``#V#specific_to_organisation``.  This service keeps
legacy storage cleanup explicit, dry-run by default, and bounded in what it
reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from pymongo import UpdateOne

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import bypass_access_control
from ..security.visibility_predicates import (
    CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
    CANONICAL_SPECIFIC_TO_USER_PREDICATE,
    LEGACY_SPECIFIC_TO_ORG_PREDICATES,
    LEGACY_SPECIFIC_TO_USER_PREDICATES,
    SPECIFIC_TO_ORG_PREDICATES_READ,
    SPECIFIC_TO_USER_PREDICATES,
    collect_visibility_values,
)


VISIBILITY_FIELD_FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "family": "specific_to_user",
        "canonical": CANONICAL_SPECIFIC_TO_USER_PREDICATE,
        "legacy": LEGACY_SPECIFIC_TO_USER_PREDICATES,
        "read": SPECIFIC_TO_USER_PREDICATES,
    },
    {
        "family": "specific_to_organisation",
        "canonical": CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
        "legacy": LEGACY_SPECIFIC_TO_ORG_PREDICATES,
        "read": SPECIFIC_TO_ORG_PREDICATES_READ,
    },
)

VISIBILITY_FIELD_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(
        field
        for family in VISIBILITY_FIELD_FAMILIES
        for field in (family["canonical"], *family["legacy"])
    )
)


@dataclass(frozen=True)
class VisibilityMigrationPlan:
    concept_id: str
    updated_relationships: dict[str, Any]
    changed: bool
    conflicts: tuple[dict[str, Any], ...]


def _visibility_update_document(plan: VisibilityMigrationPlan) -> dict[str, Any]:
    set_values: dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc),
    }
    unset_values: dict[str, str] = {}
    for family in VISIBILITY_FIELD_FAMILIES:
        canonical = str(family["canonical"])
        legacy = tuple(str(field) for field in family["legacy"])
        canonical_value = plan.updated_relationships.get(canonical)
        if canonical_value:
            set_values[f"relationships.{canonical}"] = list(canonical_value)
        else:
            unset_values[f"relationships.{canonical}"] = ""
        for legacy_field in legacy:
            unset_values[f"relationships.{legacy_field}"] = ""

    update: dict[str, Any] = {"$set": set_values}
    if unset_values:
        update["$unset"] = unset_values
    return update


def _normalise_concept_values(raw: Any) -> list[str]:
    return collect_visibility_values({"_": raw}, ("_",))


def _field_filter() -> dict[str, Any]:
    return {
        "$or": [
            {f"relationships.{field}": {"$exists": True}}
            for field in VISIBILITY_FIELD_NAMES
        ]
    }


def _projection() -> dict[str, int]:
    return {
        "concept_id": 1,
        **{f"relationships.{field}": 1 for field in VISIBILITY_FIELD_NAMES},
    }


def build_visibility_migration_plan(
    concept: Mapping[str, Any],
    *,
    remove_legacy: bool = True,
) -> VisibilityMigrationPlan | None:
    concept_id = concept.get("concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip():
        return None
    relationships_raw = concept.get("relationships")
    relationships = dict(relationships_raw) if isinstance(relationships_raw, Mapping) else {}
    updated = dict(relationships)
    changed = False
    conflicts: list[dict[str, Any]] = []

    for family in VISIBILITY_FIELD_FAMILIES:
        family_name = str(family["family"])
        canonical = str(family["canonical"])
        legacy = tuple(str(field) for field in family["legacy"])

        canonical_values = _normalise_concept_values(relationships.get(canonical))
        legacy_values = collect_visibility_values(relationships, legacy)
        merged_values = collect_visibility_values(
            {canonical: canonical_values, "__legacy": legacy_values},
            (canonical, "__legacy"),
        )

        if canonical_values and legacy_values and set(canonical_values) != set(legacy_values):
            conflicts.append(
                {
                    "family": family_name,
                    "canonical_values": list(canonical_values),
                    "legacy_values": list(legacy_values),
                    "merged_values": list(merged_values),
                }
            )

        if merged_values:
            if updated.get(canonical) != merged_values:
                updated[canonical] = list(merged_values)
                changed = True
        elif canonical in updated:
            updated.pop(canonical, None)
            changed = True

        if remove_legacy:
            for legacy_field in legacy:
                if legacy_field in updated:
                    updated.pop(legacy_field, None)
                    changed = True

    return VisibilityMigrationPlan(
        concept_id=concept_id.strip(),
        updated_relationships=updated,
        changed=changed,
        conflicts=tuple(conflicts),
    )


def audit_visibility_predicate_storage(
    *,
    sample_limit: int = 20,
    scan_limit: int | None = None,
) -> dict[str, Any]:
    sample_limit = max(0, int(sample_limit))
    scan_limit_value = max(0, int(scan_limit or 0))
    field_counts = {field: 0 for field in VISIBILITY_FIELD_NAMES}
    family_dual_counts = {
        str(family["family"]): 0 for family in VISIBILITY_FIELD_FAMILIES
    }
    family_conflict_counts = {
        str(family["family"]): 0 for family in VISIBILITY_FIELD_FAMILIES
    }
    samples: list[str] = []
    conflict_samples: list[dict[str, Any]] = []
    matching_concept_count = 0
    would_update_count = 0

    with bypass_access_control():
        cursor = ConceptsRepository.find(
            _field_filter(),
            _projection(),
            sort=[("concept_id", 1)],
            limit=scan_limit_value,
        )
        for concept in cursor:
            if not isinstance(concept, Mapping):
                continue
            concept_id = concept.get("concept_id")
            relationships = concept.get("relationships")
            if not isinstance(concept_id, str) or not isinstance(relationships, Mapping):
                continue
            matching_concept_count += 1
            if len(samples) < sample_limit:
                samples.append(concept_id)
            for field in VISIBILITY_FIELD_NAMES:
                if field in relationships:
                    field_counts[field] += 1
            plan = build_visibility_migration_plan(concept)
            if plan is not None and plan.changed:
                would_update_count += 1
            if plan is not None and plan.conflicts:
                for conflict in plan.conflicts:
                    family = str(conflict.get("family") or "")
                    if family in family_conflict_counts:
                        family_conflict_counts[family] += 1
                if len(conflict_samples) < sample_limit:
                    conflict_samples.append(
                        {
                            "concept_id": concept_id,
                            "conflicts": list(plan.conflicts),
                        }
                    )
            for family in VISIBILITY_FIELD_FAMILIES:
                family_name = str(family["family"])
                canonical = str(family["canonical"])
                legacy = tuple(str(field) for field in family["legacy"])
                has_canonical = bool(_normalise_concept_values(relationships.get(canonical)))
                has_legacy = bool(collect_visibility_values(relationships, legacy))
                if has_canonical and has_legacy:
                    family_dual_counts[family_name] += 1

    return {
        "schema_version": "visibility_predicate_storage_audit.v1",
        "canonical_predicates": {
            "specific_to_user": CANONICAL_SPECIFIC_TO_USER_PREDICATE,
            "specific_to_organisation": CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
        },
        "legacy_predicates": {
            "specific_to_user": list(LEGACY_SPECIFIC_TO_USER_PREDICATES),
            "specific_to_organisation": list(LEGACY_SPECIFIC_TO_ORG_PREDICATES),
        },
        "matching_concept_count": matching_concept_count,
        "would_update_count": would_update_count,
        "field_counts": field_counts,
        "family_dual_counts": family_dual_counts,
        "family_conflict_counts": family_conflict_counts,
        "sample_concept_ids": samples,
        "conflict_samples": conflict_samples,
        "scan_limit": scan_limit_value or None,
        "sample_limit": sample_limit,
    }


def migrate_visibility_predicate_storage(
    *,
    dry_run: bool = True,
    approved: bool = False,
    sample_limit: int = 20,
    scan_limit: int | None = None,
) -> dict[str, Any]:
    sample_limit = max(0, int(sample_limit))
    scan_limit_value = max(0, int(scan_limit or 0))
    if not dry_run and not approved:
        return {
            "schema_version": "visibility_predicate_storage_migration.v1",
            "dry_run": False,
            "approved": False,
            "would_update_count": 0,
            "updated_count": 0,
            "verified_count": 0,
            "conflict_count": 0,
            "sample_changes": [],
            "errors": [
                {
                    "concept_id": "*approval*",
                    "error": "approved=true is required when dry_run=false",
                }
            ],
            "error_count": 1,
            "scan_limit": scan_limit_value or None,
            "sample_limit": sample_limit,
        }

    updated_count = 0
    would_update_count = 0
    verified_count = 0
    conflict_count = 0
    errors: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    pending_writes: list[UpdateOne] = []

    def _flush_pending_writes() -> None:
        nonlocal updated_count
        if not pending_writes:
            return
        collection = ConceptsRepository.collection()
        if collection is None:
            raise RuntimeError("Concepts collection not available")
        result = collection.bulk_write(list(pending_writes), ordered=False)
        updated_count += int(result.modified_count or 0)
        pending_writes.clear()

    with bypass_access_control():
        cursor = ConceptsRepository.find(
            _field_filter(),
            _projection(),
            sort=[("concept_id", 1)],
            limit=scan_limit_value,
        )
        for concept in cursor:
            if not isinstance(concept, Mapping):
                continue
            plan = build_visibility_migration_plan(concept)
            if plan is None or not plan.changed:
                continue
            would_update_count += 1
            if plan.conflicts:
                conflict_count += 1
            if len(samples) < sample_limit:
                samples.append(
                    {
                        "concept_id": plan.concept_id,
                        "conflicts": list(plan.conflicts),
                    }
                )
            if dry_run:
                continue
            try:
                pending_writes.append(
                    UpdateOne(
                        {"concept_id": plan.concept_id},
                        _visibility_update_document(plan),
                    )
                )
                if len(pending_writes) >= 500:
                    _flush_pending_writes()
            except Exception as exc:
                errors.append(
                    {
                        "concept_id": plan.concept_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        if not dry_run:
            try:
                _flush_pending_writes()
                post_verify = audit_visibility_predicate_storage(
                    sample_limit=0,
                    scan_limit=scan_limit_value or None,
                )
                remaining = int(post_verify.get("would_update_count") or 0)
                verified_count = max(0, would_update_count - remaining)
            except Exception as exc:
                errors.append(
                    {
                        "concept_id": "*bulk*",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    return {
        "schema_version": "visibility_predicate_storage_migration.v1",
        "dry_run": bool(dry_run),
        "approved": bool(approved),
        "would_update_count": would_update_count,
        "updated_count": updated_count,
        "verified_count": verified_count,
        "conflict_count": conflict_count,
        "sample_changes": samples,
        "errors": errors[:sample_limit],
        "error_count": len(errors),
        "scan_limit": scan_limit_value or None,
        "sample_limit": sample_limit,
    }
