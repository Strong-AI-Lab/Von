"""Merge a hyphenated concept into its underscore canonical concept, then delete the hyphen concept.

This is a recovery/cleanup utility for the concept-id normalisation collision (hyphen vs underscore).

Safety properties:
- Dry-run by default.
- Refuses to touch MongoDB Atlas unless --allow-remote is provided.
- Never overwrites non-empty scalar fields on the target; it only adds missing data.
- For text relations: it re-links (subject,predicate,object_text_id) onto the target if missing.
- Optionally updates references in other concepts' relationships arrays (recommended).

Typical usage (PowerShell):
  # Dry-run (prints summary only)
  pdm run python scripts/merge_and_delete_hyphen_concept.py --allow-remote

  # Apply merge + update references + delete hyphen concept
  pdm run python scripts/merge_and_delete_hyphen_concept.py --allow-remote --apply --delete-hyphen --update-references

Notes:
- Requires MONGO_URI to point at the target Mongo instance (Atlas).
- Uses VON_DB_NAME if set; otherwise defaults to 'von_db'.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pymongo import MongoClient


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_atlas_uri(uri: str) -> bool:
    lowered = (uri or "").strip().lower()
    return lowered.startswith("mongodb+srv://") or ".mongodb.net" in lowered


def _redact_uri_for_log(uri: str) -> str:
    if not uri:
        return ""
    # Redact credentials if present
    try:
        scheme, rest = uri.split("://", 1)
        authority = rest.split("/", 1)[0]
        tail = rest[len(authority) :]
        if "@" in authority:
            authority = "***@" + authority.split("@", 1)[1]
        return f"{scheme}://{authority}{tail}"
    except Exception:
        return "<redacted>"


@dataclass(frozen=True)
class MergeSummary:
    source_concept_found: bool
    target_concept_found: bool
    relations_to_add: int
    relations_added: int
    relations_skipped_existing: int
    source_relations_deleted: int
    target_names_added: int
    target_relationship_values_added: int
    reference_docs_scanned: int
    reference_docs_updated: int
    hyphen_concept_deleted: bool


def _union_list(existing: List[Any], incoming: Iterable[Any]) -> Tuple[List[Any], int]:
    seen = set(existing)
    added = 0
    out = list(existing)
    for v in incoming:
        if v not in seen:
            out.append(v)
            seen.add(v)
            added += 1
    return out, added


def _merge_names(
    target: Dict[str, Any], source: Dict[str, Any]
) -> Tuple[Dict[str, Any], int]:
    # Names can be stored under 'names' in a few shapes; we only union list-of-dicts or list-of-strings.
    target_names = target.get("names")
    source_names = source.get("names")

    if not source_names:
        return target, 0

    if target_names is None:
        target["names"] = source_names
        return target, len(source_names) if isinstance(source_names, list) else 1

    if not isinstance(target_names, list) or not isinstance(source_names, list):
        return target, 0

    def _key(n: Any) -> Any:
        if isinstance(n, dict):
            return (
                n.get("name") or n.get("text") or "",
                n.get("language") or n.get("lang") or "",
                n.get("type") or n.get("name_type") or "",
            )
        return n

    existing_keys = {_key(n) for n in target_names}
    added = 0
    for n in source_names:
        k = _key(n)
        if k not in existing_keys:
            target_names.append(n)
            existing_keys.add(k)
            added += 1
    target["names"] = target_names
    return target, added


def _merge_relationships(
    target: Dict[str, Any], source: Dict[str, Any]
) -> Tuple[Dict[str, Any], int]:
    # Merge only when both have a dict at 'relationships'.
    t_rel = target.get("relationships")
    s_rel = source.get("relationships")
    if not isinstance(t_rel, dict) or not isinstance(s_rel, dict):
        return target, 0

    total_added = 0
    for k, v in s_rel.items():
        if not isinstance(v, list):
            continue
        t_list = t_rel.get(k)
        if t_list is None:
            t_rel[k] = list(v)
            total_added += len(v)
            continue
        if isinstance(t_list, list):
            merged, added = _union_list(t_list, v)
            t_rel[k] = merged
            total_added += added

    target["relationships"] = t_rel
    return target, total_added


def _link_text_relations(
    *,
    db,
    source_concept_id: str,
    target_concept_id: str,
    apply: bool,
) -> Tuple[int, int, int, int]:
    rels = list(db["text_relations"].find({"subject_concept_id": source_concept_id}))
    to_add = 0
    added = 0
    skipped = 0

    for rel in rels:
        predicate = rel.get("predicate")
        object_text_id = rel.get("object_text_id")
        if not predicate or not object_text_id:
            continue

        to_add += 1
        existing = db["text_relations"].find_one(
            {
                "subject_concept_id": target_concept_id,
                "predicate": predicate,
                "object_text_id": object_text_id,
            }
        )
        if existing:
            skipped += 1
            continue

        if apply:
            db["text_relations"].insert_one(
                {
                    "subject_concept_id": target_concept_id,
                    "predicate": predicate,
                    "object_text_id": object_text_id,
                    "context": {
                        "merged_from": {
                            "source_concept_id": source_concept_id,
                            "source_relation_id": str(rel.get("_id", "")),
                        }
                    },
                    "created_at": _utc_now(),
                    "updated_at": _utc_now(),
                }
            )
            added += 1

    deleted = 0
    if apply:
        deleted = (
            db["text_relations"]
            .delete_many({"subject_concept_id": source_concept_id})
            .deleted_count
        )

    return to_add, added, skipped, deleted


def _update_relationship_references(
    *,
    db,
    source_concept_id: str,
    target_concept_id: str,
    apply: bool,
) -> Tuple[int, int]:
    """Replace occurrences of source_concept_id with target_concept_id in relationships arrays."""

    relationship_fields = [
        "relationships.is_an_instance_of",
        "relationships.is_a_type_of",
        "relationships.has_subtype",
        "relationships.has_instance",
        "relationships.related_to",
        "relationships.linked_to",
    ]

    # Find candidate docs (broad but bounded).
    query_or = [{f: source_concept_id} for f in relationship_fields]
    cursor = db["concepts"].find(
        {"$or": query_or}, {"concept_id": 1, "relationships": 1}
    )

    scanned = 0
    updated = 0
    for doc in cursor:
        scanned += 1
        rel = doc.get("relationships")
        if not isinstance(rel, dict):
            continue

        changed = False
        for field in [
            "is_an_instance_of",
            "is_a_type_of",
            "has_subtype",
            "has_instance",
            "related_to",
            "linked_to",
        ]:
            values = rel.get(field)
            if not isinstance(values, list):
                continue
            new_values = [
                target_concept_id if v == source_concept_id else v for v in values
            ]
            if new_values != values:
                rel[field] = new_values
                changed = True

        if changed:
            updated += 1
            if apply:
                db["concepts"].update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"relationships": rel, "updated_at": _utc_now()}},
                )

    return scanned, updated


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge hyphen concept into underscore concept, then delete hyphen concept (Atlas-safe)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform writes (default dry-run).",
    )
    parser.add_argument(
        "--delete-hyphen",
        action="store_true",
        help="Delete the hyphen concept document after merge.",
    )
    parser.add_argument(
        "--update-references",
        action="store_true",
        help="Update other concepts' relationships to replace hyphen ID with underscore ID.",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow mongodb+srv / Atlas URIs (required for this task).",
    )
    parser.add_argument(
        "--mongo-uri",
        default=os.environ.get("MONGO_URI", ""),
        help="MongoDB URI (defaults to env MONGO_URI).",
    )
    parser.add_argument(
        "--db-name",
        default=os.environ.get("VON_DB_NAME", "von_db"),
        help="Database name (defaults to env VON_DB_NAME or 'von_db').",
    )
    parser.add_argument(
        "--source-concept-id",
        default="#V#a_community-driven_vision_for_a_new_knowledge_resource_for_ai",
        help="Hyphen concept ID (source).",
    )
    parser.add_argument(
        "--target-concept-id",
        default="#V#a_community_driven_vision_for_a_new_knowledge_resource_for_ai",
        help="Underscore concept ID (target).",
    )

    args = parser.parse_args(argv)

    if not args.mongo_uri:
        print("MONGO_URI is not set; refusing to run.", file=sys.stderr)
        return 2

    atlas = _is_atlas_uri(args.mongo_uri)
    if atlas and not args.allow_remote:
        print(
            "Refusing to run against a remote/Atlas-like Mongo URI without --allow-remote.",
            file=sys.stderr,
        )
        return 2

    client = MongoClient(args.mongo_uri)
    db = client[args.db_name]

    source = db["concepts"].find_one({"concept_id": args.source_concept_id})
    target = db["concepts"].find_one({"concept_id": args.target_concept_id})

    if not source:
        print("Source (hyphen) concept not found.")
        return 2
    if not target:
        print("Target (underscore) concept not found.")
        return 2

    target_names_added = 0
    target_relationship_values_added = 0

    # Merge names + relationships in-memory
    merged_target = dict(target)
    merged_target, names_added = _merge_names(merged_target, source)
    merged_target, rel_added = _merge_relationships(merged_target, source)
    target_names_added += names_added
    target_relationship_values_added += rel_added

    if args.apply:
        set_fields: Dict[str, Any] = {}
        # Only set fields we touched
        if names_added:
            set_fields["names"] = merged_target.get("names")
        if rel_added:
            set_fields["relationships"] = merged_target.get("relationships")
        if set_fields:
            set_fields["updated_at"] = _utc_now()
            db["concepts"].update_one({"_id": target["_id"]}, {"$set": set_fields})

    # Merge text relations
    to_add, added, skipped, source_deleted = _link_text_relations(
        db=db,
        source_concept_id=args.source_concept_id,
        target_concept_id=args.target_concept_id,
        apply=args.apply,
    )

    ref_scanned = 0
    ref_updated = 0
    if args.update_references:
        ref_scanned, ref_updated = _update_relationship_references(
            db=db,
            source_concept_id=args.source_concept_id,
            target_concept_id=args.target_concept_id,
            apply=args.apply,
        )

    hyphen_deleted = False
    if args.apply and args.delete_hyphen:
        deleted = (
            db["concepts"]
            .delete_many({"concept_id": args.source_concept_id})
            .deleted_count
        )
        hyphen_deleted = deleted > 0

    summary = MergeSummary(
        source_concept_found=True,
        target_concept_found=True,
        relations_to_add=to_add,
        relations_added=added,
        relations_skipped_existing=skipped,
        source_relations_deleted=source_deleted,
        target_names_added=target_names_added,
        target_relationship_values_added=target_relationship_values_added,
        reference_docs_scanned=ref_scanned,
        reference_docs_updated=ref_updated,
        hyphen_concept_deleted=hyphen_deleted,
    )

    print(
        {
            "db": args.db_name,
            "mongo_uri": _redact_uri_for_log(args.mongo_uri),
            "apply": bool(args.apply),
            "delete_hyphen": bool(args.delete_hyphen),
            "update_references": bool(args.update_references),
            "source": args.source_concept_id,
            "target": args.target_concept_id,
            "summary": summary.__dict__,
        }
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
