"""Repair invalid relationship predicate keys in the concepts collection.

This script exists to clean up legacy or accidental writes where arbitrary strings
(e.g. "has_item") were used as keys under the "relationships" subdocument.

Target state:
- Structural relationship fields remain as-is (e.g. is_a_type_of, is_an_instance_of).
- Predicate-like relationship fields MUST use compliant Vontology concept ids
  (start with "#V#").

The script can:
- Report invalid relationship keys.
- Optionally rewrite invalid keys to compliant "#V#..." ids.
- Optionally create missing predicate concept nodes so rewritten keys are not
  "virtual".

It is designed to be idempotent.

PowerShell examples:
    pdm run python utilities/migrate_virtual_relationship_predicates.py

    pdm run python utilities/migrate_virtual_relationship_predicates.py --apply

    pdm run python utilities/migrate_virtual_relationship_predicates.py --apply --create-predicate-concepts

"""

from __future__ import annotations

import sys
import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


_STRUCTURAL_RELATIONSHIP_KEYS = {
    "is_a_type_of",
    "has_subtype",
    "is_an_instance_of",
    "has_instance",
    "related_to",
}

# Known relationship fields that are not predicates but are legitimately persisted.
# These should NOT be migrated into predicate concepts.
_KNOWN_NON_PREDICATE_RELATIONSHIP_KEYS = {
    "linked_to",
    "most_salient_type",
}


# Canonical mappings for known legacy relationship keys.
#
# JVNAUTOSCI-913: historical todo list membership relations were accidentally
# stored under 'has_item' (a non-Vontology predicate key). The canonical predicate
# concept id is '#V#has_todo_item'.
_CANONICAL_PREDICATE_ID_OVERRIDES = {
    "has_item": "#V#has_todo_item",
    "#V#has_item": "#V#has_todo_item",
}


@dataclass(frozen=True)
class MigrationChange:
    concept_id: str
    old_key: str
    new_key: str
    moved_count: int
    removed_old_key: bool


def _normalise_relationship_values(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, str) and v.strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw]
    return []


def _iter_invalid_relationship_keys(relationships: Dict[str, Any]) -> Iterable[str]:
    for key, value in relationships.items():
        if key in _STRUCTURAL_RELATIONSHIP_KEYS:
            continue
        if key in _KNOWN_NON_PREDICATE_RELATIONSHIP_KEYS:
            continue
        if isinstance(key, str) and key.startswith("#V#"):
            continue
        # Ignore empty/none buckets quietly
        if not _normalise_relationship_values(value):
            continue
        yield key


def _canonicalise_predicate_id(raw_key: str) -> Optional[str]:
    if not isinstance(raw_key, str):
        return None

    raw_key = raw_key.strip()
    if not raw_key:
        return None

    override = _CANONICAL_PREDICATE_ID_OVERRIDES.get(raw_key)
    if override:
        return override

    if raw_key.startswith("#V#"):
        return raw_key

    # Use the same normaliser as concept creation.
    from src.backend.utils.concept_id_utils import canonicalise_vontology_concept_id

    return canonicalise_vontology_concept_id(raw_key)


def _ensure_predicate_concept_exists(predicate_id: str) -> bool:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.concept_service import create_concept
    from src.backend.vontology.code_concepts_registry import is_code_concept_id

    if is_code_concept_id(predicate_id):
        return True

    existing = ConceptsRepository.find_one(
        {"concept_id": predicate_id}, {"concept_id": 1}
    )
    if existing:
        return True

    # Create as an instance of #V#predicate. The type itself may be virtual;
    # that is acceptable for research prototype workflows.
    base_name = predicate_id[3:] if predicate_id.startswith("#V#") else predicate_id

    created = create_concept(
        name=base_name,
        concept_id=predicate_id,
        parent_concept_ids=["#V#predicate"],
        create_as_instance=True,
        description=(
            "Auto-created predicate concept to replace a legacy relationship key."
        ),
    )
    return bool(created)


def _ensure_predicate_concept_is_typed(predicate_id: str) -> bool:
    """Ensure an existing predicate concept is typed as a predicate.

    This adds relationships.is_an_instance_of += '#V#predicate' when needed.

    We do not maintain the inverse edge here because '#V#predicate' may be a
    code-only concept in some environments.
    """

    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.vontology.code_concepts_registry import is_code_concept_id
    from src.backend.vontology.utils_vontology import is_predicate

    if is_code_concept_id(predicate_id):
        return False

    doc = ConceptsRepository.find_one(
        {"concept_id": predicate_id}, {"concept_id": 1, "relationships": 1}
    )
    if not doc:
        return False

    if is_predicate(doc):
        return False

    ConceptsRepository.update_one(
        {"concept_id": predicate_id},
        {"$addToSet": {"relationships.is_an_instance_of": "#V#predicate"}},
    )
    return True


def run(
    *,
    apply: bool,
    create_predicate_concepts: bool,
    ensure_predicate_typing: bool,
    limit: Optional[int],
) -> Dict[str, Any]:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.db.repositories.text_value_repository import (
        TextRelationsRepository,
    )

    coll = ConceptsRepository.collection()
    if coll is None:
        raise RuntimeError("Concepts collection not available")

    projection = {"concept_id": 1, "relationships": 1}

    cursor = coll.find({"relationships": {"$exists": True}}, projection)
    changes: List[MigrationChange] = []
    invalid_key_counts: Counter[str] = Counter()
    created_predicates: Counter[str] = Counter()
    typed_predicates: Counter[str] = Counter()
    existing_vkey_predicates_scanned: Counter[str] = Counter()
    existing_vkey_predicates_created: Counter[str] = Counter()
    text_vkey_predicates_scanned: Counter[str] = Counter()
    text_vkey_predicates_created: Counter[str] = Counter()

    scanned = 0
    for doc in cursor:
        scanned += 1
        if limit is not None and scanned > limit:
            break

        concept_id = doc.get("concept_id")
        relationships = doc.get("relationships")
        if not isinstance(concept_id, str) or not concept_id:
            continue
        if not isinstance(relationships, dict):
            continue

        invalid_keys = list(_iter_invalid_relationship_keys(relationships))
        for old_key in invalid_keys:
            invalid_key_counts[old_key] += 1

            new_key = _canonicalise_predicate_id(old_key)
            if not new_key or not new_key.startswith("#V#"):
                continue

            values = _normalise_relationship_values(relationships.get(old_key))
            if not values:
                continue

            if create_predicate_concepts:
                if _ensure_predicate_concept_exists(new_key):
                    created_predicates[new_key] += 1
                if ensure_predicate_typing:
                    if _ensure_predicate_concept_is_typed(new_key):
                        typed_predicates[new_key] += 1

            if not apply:
                changes.append(
                    MigrationChange(
                        concept_id=concept_id,
                        old_key=old_key,
                        new_key=new_key,
                        moved_count=len(values),
                        removed_old_key=True,
                    )
                )
                continue

            # Merge values into the new key, avoid duplicates.
            update_ops: Dict[str, Any] = {
                "$addToSet": {f"relationships.{new_key}": {"$each": values}},
                "$unset": {f"relationships.{old_key}": ""},
            }

            coll.update_one({"concept_id": concept_id}, update_ops)
            changes.append(
                MigrationChange(
                    concept_id=concept_id,
                    old_key=old_key,
                    new_key=new_key,
                    moved_count=len(values),
                    removed_old_key=True,
                )
            )

        # Optional: if requested, ensure predicate concepts exist for existing
        # compliant relationship keys that are currently "missing".
        if create_predicate_concepts:
            for rel_key in relationships.keys():
                if not isinstance(rel_key, str) or not rel_key.startswith("#V#"):
                    continue
                if rel_key in _STRUCTURAL_RELATIONSHIP_KEYS:
                    continue
                if rel_key in _KNOWN_NON_PREDICATE_RELATIONSHIP_KEYS:
                    continue

                # Ignore empty buckets.
                if not _normalise_relationship_values(relationships.get(rel_key)):
                    continue

                existing_vkey_predicates_scanned[rel_key] += 1

                if _ensure_predicate_concept_exists(rel_key):
                    existing_vkey_predicates_created[rel_key] += 1
                if ensure_predicate_typing:
                    if _ensure_predicate_concept_is_typed(rel_key):
                        typed_predicates[rel_key] += 1

    # Also cover #V#... predicates used in text relations.
    if create_predicate_concepts:
        for tr in TextRelationsRepository.find({}, {"predicate": 1}):
            predicate = tr.get("predicate")
            if not isinstance(predicate, str) or not predicate.startswith("#V#"):
                continue

            text_vkey_predicates_scanned[predicate] += 1

            if _ensure_predicate_concept_exists(predicate):
                text_vkey_predicates_created[predicate] += 1
            if ensure_predicate_typing:
                if _ensure_predicate_concept_is_typed(predicate):
                    typed_predicates[predicate] += 1

    return {
        "success": True,
        "apply": apply,
        "create_predicate_concepts": create_predicate_concepts,
        "ensure_predicate_typing": ensure_predicate_typing,
        "scanned": scanned,
        "invalid_key_counts": dict(invalid_key_counts),
        "changes": [change.__dict__ for change in changes],
        "created_predicates": dict(created_predicates),
        "typed_predicates": dict(typed_predicates),
        "existing_vkey_predicates_scanned": dict(existing_vkey_predicates_scanned),
        "existing_vkey_predicates_created": dict(existing_vkey_predicates_created),
        "text_vkey_predicates_scanned": dict(text_vkey_predicates_scanned),
        "text_vkey_predicates_created": dict(text_vkey_predicates_created),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate invalid relationship keys (e.g. 'has_item') to compliant '#V#...' predicate ids."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes (default is dry-run/report only).",
    )
    parser.add_argument(
        "--create-predicate-concepts",
        action="store_true",
        help="Create missing predicate concept nodes for migrated keys.",
    )
    parser.add_argument(
        "--ensure-predicate-typing",
        action="store_true",
        help=(
            "Ensure predicate concepts are typed as predicates by adding is_an_instance_of '#V#predicate' when missing."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only scan the first N concepts (debugging aid).",
    )

    args = parser.parse_args()
    result = run(
        apply=bool(args.apply),
        create_predicate_concepts=bool(args.create_predicate_concepts),
        ensure_predicate_typing=bool(args.ensure_predicate_typing),
        limit=args.limit,
    )

    # Keep output simple and machine-readable.
    import json

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
