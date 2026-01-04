"""Recover hasNote text relations for a concept from a restored backup DB.

This script is designed for safe, idempotent recovery:
- Reads hasNote relations + text_values from a *source* DB (e.g., a restored backup).
- Upserts the same text into the *target* DB using the normal service path
  (text_value_service.upsert_text_for_concept), so fingerprints and uniqueness
  rules are respected.

Default behaviour is DRY-RUN (no writes). Use --apply to perform writes.

PowerShell example:
  pdm run python scripts/recover_paper_notes.py

  pdm run python scripts/recover_paper_notes.py --apply
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from bson import ObjectId
from pymongo import MongoClient


# Ensure repo root is on sys.path so `src.*` imports work when running as a script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@dataclass(frozen=True)
class SourceNote:
    relation_id: str
    text_value_id: str
    text: str
    lang: str
    created_at: Optional[datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _assert_target_is_local_mongo(mongo_uri: str) -> None:
    """Safety guard: refuse writes to non-local MongoDB URIs.

    This recovery workflow is intended to operate only on a local MongoDB.
    """
    lowered = (mongo_uri or "").strip().lower()
    if lowered.startswith("mongodb+srv://") or ".mongodb.net" in lowered:
        raise RuntimeError(
            "Refusing to run against a mongodb+srv / Atlas-like MONGO_URI. "
            "Set --mongo-uri to a local MongoDB (e.g. mongodb://localhost:27017)."
        )


def _read_source_notes(
    *,
    mongo_uri: str,
    source_db: str,
    source_concept_id: str,
) -> List[SourceNote]:
    client = MongoClient(mongo_uri)
    db = client[source_db]

    rels = list(
        db["text_relations"].find(
            {"subject_concept_id": source_concept_id, "predicate": "hasNote"}
        )
    )

    notes: List[SourceNote] = []
    for rel in rels:
        object_text_id = rel.get("object_text_id")
        if not object_text_id:
            continue

        tv_id = ObjectId(object_text_id)
        tv = db["text_values"].find_one({"_id": tv_id})
        if not tv:
            continue

        text = tv.get("text")
        lang = tv.get("lang")
        if not isinstance(text, str) or not isinstance(lang, str):
            continue

        created_at = tv.get("created_at")
        if created_at is not None and not isinstance(created_at, datetime):
            created_at = None

        notes.append(
            SourceNote(
                relation_id=str(rel.get("_id", "")),
                text_value_id=str(tv.get("_id", "")),
                text=text,
                lang=lang,
                created_at=created_at,
            )
        )

    # Prefer stable order: oldest first.
    notes.sort(key=lambda n: n.created_at or datetime.min.replace(tzinfo=timezone.utc))
    return notes


def _describe_plan(
    *,
    target_concept_id: str,
    notes: Iterable[SourceNote],
) -> Dict[str, Any]:
    # Imports live here so env vars (MONGO_URI / VON_DB_NAME) are already set.
    from src.backend.db.repositories.text_value_repository import (  # noqa: WPS433
        TextRelationsRepository,
        TextValuesRepository,
    )
    from src.backend.services.text_value_service import (  # noqa: WPS433
        _compute_fingerprint,
        _prepare_persisted_text,
    )

    planned: List[Dict[str, Any]] = []
    will_create_text_values = 0
    will_create_relations = 0

    for note in notes:
        persisted = _prepare_persisted_text(note.text)
        fp = _compute_fingerprint(persisted, note.lang)

        existing_tv = TextValuesRepository.find_one(
            {"fingerprint": fp, "lang": note.lang}
        )
        existing_tv_id = (
            str(existing_tv["_id"]) if existing_tv and existing_tv.get("_id") else None
        )

        relation_exists = False
        if existing_tv_id:
            existing_rel = TextRelationsRepository.find_one(
                {
                    "subject_concept_id": target_concept_id,
                    "predicate": "hasNote",
                    "object_text_id": existing_tv_id,
                }
            )
            relation_exists = bool(existing_rel)

        tv_action = "reuse" if existing_tv_id else "create"
        rel_action = "skip" if relation_exists else "create"

        if tv_action == "create":
            will_create_text_values += 1
        if rel_action == "create":
            will_create_relations += 1

        planned.append(
            {
                "source_text_value_id": note.text_value_id,
                "source_relation_id": note.relation_id,
                "lang": note.lang,
                "fingerprint": fp,
                "text_preview": (
                    (persisted[:160] + "…") if len(persisted) > 160 else persisted
                ),
                "target_text_value_action": tv_action,
                "target_relation_action": rel_action,
                "target_existing_text_value_id": existing_tv_id,
            }
        )

    return {
        "notes_found": len(list(notes)) if not isinstance(notes, list) else len(notes),
        "planned_items": planned,
        "summary": {
            "will_create_text_values": will_create_text_values,
            "will_create_relations": will_create_relations,
        },
    }


def _apply(
    *,
    target_concept_id: str,
    notes: List[SourceNote],
    source_db: str,
    source_concept_id: str,
) -> List[Dict[str, Any]]:
    # Imports live here so env vars (MONGO_URI / VON_DB_NAME) are already set.
    from src.backend.services.text_value_service import (  # noqa: WPS433
        upsert_text_for_concept,
    )

    results: List[Dict[str, Any]] = []

    for note in notes:
        provenance = {
            "recovered_from_backup": True,
            "recovered_at_utc": _iso_utc(_utc_now()),
            "source_db": source_db,
            "source_concept_id": source_concept_id,
            "source_relation_id": note.relation_id,
            "source_text_value_id": note.text_value_id,
        }
        context = {
            "recovered_from": {
                "source_db": source_db,
                "source_concept_id": source_concept_id,
                "source_relation_id": note.relation_id,
                "source_text_value_id": note.text_value_id,
            }
        }

        res = upsert_text_for_concept(
            subject_concept_id=target_concept_id,
            predicate="hasNote",
            text=note.text,
            lang=note.lang,
            provenance=provenance,
            context=context,
        )
        results.append(res)

    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recover hasNote text relations from restored backup DB into current DB.",
    )
    parser.add_argument(
        "--mongo-uri",
        default="mongodb://localhost:27017",
        help="MongoDB connection string (default: mongodb://localhost:27017)",
    )
    parser.add_argument(
        "--source-db",
        default="von_db_restore_20260103_225836Z_manual",
        help="Source database name (restored backup)",
    )
    parser.add_argument(
        "--target-db",
        default="von_db",
        help="Target database name (current)",
    )
    parser.add_argument(
        "--source-concept-id",
        default="#V#a_community-driven_vision_for_a_new_knowledge_resource_for_ai",
        help="Concept ID to read notes from (source DB)",
    )
    parser.add_argument(
        "--target-concept-id",
        default="#V#a_community_driven_vision_for_a_new_knowledge_resource_for_ai",
        help="Concept ID to attach notes to (target DB)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform writes (default is dry-run)",
    )

    args = parser.parse_args(argv)

    # Ensure repositories/services operate on the intended *local* target.
    # (Process-local only; does not persist anywhere.)
    _assert_target_is_local_mongo(args.mongo_uri)

    import os

    os.environ["MONGO_URI"] = args.mongo_uri
    os.environ["VON_DB_NAME"] = args.target_db

    notes = _read_source_notes(
        mongo_uri=args.mongo_uri,
        source_db=args.source_db,
        source_concept_id=args.source_concept_id,
    )

    if not notes:
        print(
            "No hasNote relations found in source DB for that concept. Nothing to do.",
            file=sys.stderr,
        )
        return 2

    plan = _describe_plan(target_concept_id=args.target_concept_id, notes=notes)
    print("Recovery plan (dry-run):")
    print(plan["summary"])

    if not args.apply:
        print("Dry-run only. Re-run with --apply to perform the recovery.")
        return 0

    print("Applying recovery (upsert_text_for_concept)...")
    results = _apply(
        target_concept_id=args.target_concept_id,
        notes=notes,
        source_db=args.source_db,
        source_concept_id=args.source_concept_id,
    )

    created = sum(1 for r in results if r.get("relation_created"))
    updated_context = sum(1 for r in results if r.get("context_updated"))
    print(
        {
            "notes_processed": len(results),
            "relations_created": created,
            "relations_context_updated": updated_context,
        }
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
