"""Migrate legacy concept_data.preserved_fields text into canonical relations.

JVNAUTOSCI-1171 decommissions runtime dependence on preserved_fields. This
service provides an idempotent migration path plus inventory reporting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from .text_value_service import upsert_text_for_concept

_PRESERVED_TEXT_KEY_TO_PREDICATE: Dict[str, str] = {
    "description": "hasDescription",
    "notes": "hasNote",
    "content": "hasContent",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_non_empty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _iter_preserved_field_docs(
    *,
    concept_ids: Sequence[str] | None = None,
    limit: int | None = None,
) -> Iterable[Dict[str, Any]]:
    query: Dict[str, Any] = {"concept_data.preserved_fields": {"$exists": True}}
    if concept_ids:
        filtered_ids = [cid.strip() for cid in concept_ids if isinstance(cid, str) and cid.strip()]
        if filtered_ids:
            query["concept_id"] = {"$in": filtered_ids}
    projection = {
        "concept_id": 1,
        "concept_data.preserved_fields": 1,
    }
    cursor = ConceptsRepository.find(
        query,
        projection=projection,
        limit=int(limit) if isinstance(limit, int) and limit > 0 else 0,
    )
    for doc in cursor:
        if isinstance(doc, dict):
            yield doc


def inventory_preserved_fields(
    *,
    concept_ids: Sequence[str] | None = None,
    limit: int | None = None,
) -> Dict[str, Any]:
    """Return inventory counts for preserved_fields keys and eligible text values."""

    keys_total: Dict[str, int] = {}
    keys_with_text: Dict[str, int] = {}
    concepts_scanned = 0

    for doc in _iter_preserved_field_docs(concept_ids=concept_ids, limit=limit):
        concepts_scanned += 1
        preserved = (
            doc.get("concept_data", {}).get("preserved_fields", {})
            if isinstance(doc.get("concept_data"), Mapping)
            else {}
        )
        if not isinstance(preserved, Mapping):
            continue
        for key, value in preserved.items():
            key_name = str(key)
            keys_total[key_name] = keys_total.get(key_name, 0) + 1
            if _normalise_non_empty_text(value):
                keys_with_text[key_name] = keys_with_text.get(key_name, 0) + 1

    return {
        "success": True,
        "scanned_at": _iso_now(),
        "concepts_scanned": concepts_scanned,
        "keys_total": keys_total,
        "keys_with_non_empty_text": keys_with_text,
        "migration_key_mapping": dict(_PRESERVED_TEXT_KEY_TO_PREDICATE),
    }


def migrate_preserved_fields_to_text_relations(
    *,
    concept_ids: Sequence[str] | None = None,
    dry_run: bool = True,
    limit: int | None = None,
    purge_migrated_keys: bool = False,
) -> Dict[str, Any]:
    """Backfill preserved_fields text into canonical text relations.

    This operation is idempotent because upsert_text_for_concept deduplicates by
    text fingerprint + relation uniqueness.
    """

    stats = {
        "concepts_scanned": 0,
        "concepts_with_preserved_fields": 0,
        "eligible_text_entries": 0,
        "migrated_entries": 0,
        "purged_keys": 0,
        "skipped_missing_concept_id": 0,
        "skipped_unmapped_keys": 0,
        "errors": 0,
    }
    details: List[Dict[str, Any]] = []

    for doc in _iter_preserved_field_docs(concept_ids=concept_ids, limit=limit):
        stats["concepts_scanned"] += 1
        preserved = (
            doc.get("concept_data", {}).get("preserved_fields", {})
            if isinstance(doc.get("concept_data"), Mapping)
            else {}
        )
        if not isinstance(preserved, Mapping):
            continue
        stats["concepts_with_preserved_fields"] += 1

        concept_id = doc.get("concept_id")
        if not isinstance(concept_id, str) or not concept_id.strip():
            stats["skipped_missing_concept_id"] += 1
            details.append(
                {
                    "status": "skipped_missing_concept_id",
                    "concept_id": concept_id,
                }
            )
            continue
        concept_id = concept_id.strip()

        migrated_keys_for_concept: List[str] = []
        for raw_key, raw_value in preserved.items():
            key = str(raw_key)
            predicate = _PRESERVED_TEXT_KEY_TO_PREDICATE.get(key)
            text_value = _normalise_non_empty_text(raw_value)
            if not text_value:
                continue
            if not predicate:
                stats["skipped_unmapped_keys"] += 1
                details.append(
                    {
                        "status": "skipped_unmapped_key",
                        "concept_id": concept_id,
                        "key": key,
                    }
                )
                continue

            stats["eligible_text_entries"] += 1
            if dry_run:
                stats["migrated_entries"] += 1
                migrated_keys_for_concept.append(key)
                details.append(
                    {
                        "status": "would_migrate",
                        "concept_id": concept_id,
                        "key": key,
                        "predicate": predicate,
                    }
                )
                continue

            try:
                upsert_text_for_concept(
                    subject_concept_id=concept_id,
                    predicate=predicate,
                    text=text_value,
                    lang="en-NZ",
                    provenance={"source": "preserved_fields_migration"},
                )
                stats["migrated_entries"] += 1
                migrated_keys_for_concept.append(key)
                details.append(
                    {
                        "status": "migrated",
                        "concept_id": concept_id,
                        "key": key,
                        "predicate": predicate,
                    }
                )
            except Exception as exc:
                stats["errors"] += 1
                details.append(
                    {
                        "status": "error",
                        "concept_id": concept_id,
                        "key": key,
                        "predicate": predicate,
                        "detail": str(exc),
                    }
                )

        if (
            not dry_run
            and purge_migrated_keys
            and migrated_keys_for_concept
            and stats["errors"] == 0
        ):
            unset_payload = {
                f"concept_data.preserved_fields.{key}": ""
                for key in migrated_keys_for_concept
            }
            if unset_payload:
                ConceptsRepository.update_one(
                    {"concept_id": concept_id},
                    {"$unset": unset_payload},
                )
                stats["purged_keys"] += len(unset_payload)

    return {
        "success": stats["errors"] == 0,
        "dry_run": bool(dry_run),
        "purge_migrated_keys": bool(purge_migrated_keys),
        "scanned_at": _iso_now(),
        "stats": stats,
        "details": details,
        "migration_key_mapping": dict(_PRESERVED_TEXT_KEY_TO_PREDICATE),
    }

