import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pymongo.errors import DuplicateKeyError

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import TextRelationsRepository
from ..services.concept_service import get_concept_by_id, delete_concept

logger = logging.getLogger(__name__)

PROTECTED_CONCEPTS = {"#V#Thing", "#V#Root", "#V#System"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ordered_unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _replace_value(value: Any, source_id: str, target_id: str) -> Tuple[Any, bool]:
    """Replace occurrences of source_id with target_id within relationship values."""

    if isinstance(value, str):
        if value == source_id:
            return target_id, True
        return value, False

    if isinstance(value, list):
        changed = False
        replaced_list: List[Any] = []
        for item in value:
            new_item, item_changed = _replace_value(item, source_id, target_id)
            changed = changed or item_changed
            replaced_list.append(new_item)

        # Relationship lists are expected to be strings; enforce de-dup on strings.
        if all(isinstance(x, str) for x in replaced_list):
            deduped = _ordered_unique([x for x in replaced_list if isinstance(x, str)])
            return deduped, changed or (deduped != replaced_list)

        return replaced_list, changed

    if isinstance(value, dict):
        changed = False
        out: Dict[str, Any] = {}
        for k, v in value.items():
            new_v, v_changed = _replace_value(v, source_id, target_id)
            changed = changed or v_changed
            out[k] = new_v
        return out, changed

    return value, False


def _replace_relationships(
    relationships: Any, source_id: str, target_id: str
) -> Tuple[Dict[str, Any], bool]:
    if not isinstance(relationships, dict):
        return {}, False
    changed = False
    new_rels: Dict[str, Any] = {}
    for predicate, targets in relationships.items():
        new_targets, targets_changed = _replace_value(targets, source_id, target_id)
        changed = changed or targets_changed
        new_rels[predicate] = new_targets
    return new_rels, changed


def _merge_relationship_maps(
    target_relationships: Dict[str, Any], source_relationships: Dict[str, Any]
) -> Tuple[Dict[str, Any], bool]:
    """Merge source relationships into target; keep target ordering and add new uniques."""
    merged: Dict[str, Any] = dict(target_relationships)
    changed = False

    for predicate, source_targets in source_relationships.items():
        if predicate not in merged:
            merged[predicate] = source_targets
            changed = True
            continue

        target_targets = merged.get(predicate)
        if isinstance(target_targets, list) or isinstance(source_targets, list):
            t_list = (
                target_targets
                if isinstance(target_targets, list)
                else ([target_targets] if isinstance(target_targets, str) else [])
            )
            s_list = (
                source_targets
                if isinstance(source_targets, list)
                else ([source_targets] if isinstance(source_targets, str) else [])
            )
            if all(isinstance(x, str) for x in t_list + s_list):
                combined = _ordered_unique([*t_list, *s_list])
                if combined != t_list:
                    merged[predicate] = combined
                    changed = True
            else:
                # Fallback: keep target value.
                pass
        else:
            # Both scalars
            if target_targets != source_targets:
                merged[predicate] = [target_targets, source_targets]
                changed = True

    return merged, changed


def _merge_legacy_names(
    target_doc: Dict[str, Any], source_doc: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], bool]:
    """Merge legacy `names` entries (if present) without duplication."""
    target_names = target_doc.get("names")
    source_names = source_doc.get("names")

    target_list = target_names if isinstance(target_names, list) else []
    source_list = source_names if isinstance(source_names, list) else []

    seen: set[Tuple[str, str, str]] = set()
    merged: List[Dict[str, Any]] = []

    def _normalise_entry(entry: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        language = entry.get("language")
        if not isinstance(language, str) or not language.strip():
            language = "en"
        kind = entry.get("type")
        if not isinstance(kind, str) or not kind.strip():
            kind = "NL"
        return {
            "name": name.strip(),
            "language": language.strip(),
            "type": kind.strip(),
        }

    for entry in target_list:
        norm = _normalise_entry(entry)
        if not norm:
            continue
        key = (norm["name"], norm["language"], norm["type"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(norm)

    changed = False
    for entry in source_list:
        norm = _normalise_entry(entry)
        if not norm:
            continue
        key = (norm["name"], norm["language"], norm["type"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(norm)
        changed = True

    # Also add source top-level name as an alias when distinct.
    source_top = source_doc.get("name")
    if isinstance(source_top, str) and source_top.strip():
        source_top = source_top.strip()
        target_top = target_doc.get("name")
        if source_top != target_top:
            key = (source_top, "en", "NL")
            if key not in seen:
                merged.append({"name": source_top, "language": "en", "type": "NL"})
                changed = True

    return merged, changed


def merge_concepts(
    source_id: str, target_id: str, simulate: bool = True
) -> Dict[str, Any]:
    """
    Merges source_concept into target_concept.

    1. Moves all relationships from source to target.
    2. Moves all names/aliases from source to target.
    3. Moves all text values (notes, etc) from source to target.
    4. Deletes source concept.

    Args:
        source_id: The concept_id of the concept to be merged and deleted.
        target_id: The concept_id of the concept to receive the data.
        simulate: If True, only returns a report of what would happen.

    Returns:
        Dict containing the merge report/result.
    """
    report = {
        "success": False,
        "simulate": simulate,
        "source_id": source_id,
        "target_id": target_id,
        "operations": [],
        "warnings": [],
        "errors": [],
    }

    if source_id in PROTECTED_CONCEPTS:
        report["errors"].append(
            f"Source concept {source_id} is protected and cannot be merged."
        )
        return report

    if source_id == target_id:
        report["errors"].append("Source and target concepts must be different.")
        return report

    source_doc = get_concept_by_id(source_id)
    target_doc = get_concept_by_id(target_id)

    if not source_doc:
        report["errors"].append(f"Source concept {source_id} not found.")
        return report
    if not target_doc:
        report["errors"].append(f"Target concept {target_id} not found.")
        return report

    # 1. Analyse relationship references across all concepts.
    source_rels_raw = source_doc.get("relationships", {})
    target_rels_raw = target_doc.get("relationships", {})

    source_rels, _ = _replace_relationships(source_rels_raw, source_id, target_id)
    target_rels, _ = _replace_relationships(target_rels_raw, source_id, target_id)

    concepts_cursor = ConceptsRepository.find({}, {"concept_id": 1, "relationships": 1})
    affected_concepts: List[str] = []
    for doc in concepts_cursor:
        cid = doc.get("concept_id")
        if not isinstance(cid, str) or not cid:
            continue
        if cid == source_id:
            continue
        new_rels, changed = _replace_relationships(
            doc.get("relationships"), source_id, target_id
        )
        if changed:
            affected_concepts.append(cid)

    if affected_concepts:
        report["operations"].append(
            {
                "type": "rewrite_incoming_relationship_references",
                "count": len(affected_concepts),
                "concept_ids": affected_concepts[:50],
                "detail": f"Replace references to {source_id} with {target_id} across concepts",
            }
        )
        if len(affected_concepts) > 50:
            report["warnings"].append(
                f"Relationship rewrite affects {len(affected_concepts)} concepts; report truncated to 50 ids"
            )

    merged_rels_preview, rels_changed = _merge_relationship_maps(
        target_rels, source_rels
    )
    if rels_changed:
        report["operations"].append(
            {
                "type": "merge_outgoing_relationships",
                "detail": "Merge source outgoing relationships into target",
            }
        )

    # 2. Analyse legacy names field (if present)
    merged_names_preview, names_changed = _merge_legacy_names(target_doc, source_doc)
    if names_changed:
        report["operations"].append(
            {
                "type": "merge_legacy_names",
                "detail": "Merge legacy name/alias entries from source into target",
                "added_count": max(
                    0,
                    len(merged_names_preview)
                    - (
                        len(target_doc.get("names") or [])
                        if isinstance(target_doc.get("names"), list)
                        else 0
                    ),
                ),
            }
        )

    # 3. Analyse text relations
    source_text_relations = list(
        TextRelationsRepository.find({"subject_concept_id": source_id})
    )
    target_text_relations = list(
        TextRelationsRepository.find({"subject_concept_id": target_id})
    )
    target_keys: set[Tuple[str, Any]] = set()
    for rel in target_text_relations:
        pred = rel.get("predicate")
        obj = rel.get("object_text_id")
        if isinstance(pred, str) and pred and obj is not None:
            target_keys.add((pred, obj))

    move_count = 0
    dup_count = 0
    for rel in source_text_relations:
        pred = rel.get("predicate")
        obj = rel.get("object_text_id")
        if not isinstance(pred, str) or not pred or obj is None:
            continue
        if (pred, obj) in target_keys:
            dup_count += 1
        else:
            move_count += 1

    if move_count or dup_count:
        report["operations"].append(
            {
                "type": "migrate_text_relations",
                "move_count": move_count,
                "duplicate_count": dup_count,
                "detail": "Reassign text_relations from source to target (de-dup on conflicts)",
            }
        )

    # 4. Deletion
    report["operations"].append(
        {
            "type": "delete_source",
            "concept_id": source_id,
            "detail": "Delete the source concept after migration",
        }
    )

    if simulate:
        report["success"] = True
        return report

    # EXECUTION
    try:
        # Refresh documents
        source_doc = get_concept_by_id(source_id)
        target_doc = get_concept_by_id(target_id)
        if source_doc is None:
            raise ValueError(f"Source concept '{source_id}' no longer exists")
        if target_doc is None:
            raise ValueError(f"Target concept '{target_id}' no longer exists")

        # 1. Rewrite relationship references in all concepts except the source.
        concepts_cursor = ConceptsRepository.find(
            {}, {"concept_id": 1, "relationships": 1}
        )
        for doc in concepts_cursor:
            cid = doc.get("concept_id")
            if not isinstance(cid, str) or not cid:
                continue
            if cid == source_id:
                continue

            new_rels, changed = _replace_relationships(
                doc.get("relationships"), source_id, target_id
            )
            if changed:
                ConceptsRepository.update_one(
                    {"concept_id": cid}, {"$set": {"relationships": new_rels}}
                )

        # 2. Merge source outgoing relationships into the target.
        source_rels, _ = _replace_relationships(
            source_doc.get("relationships", {}), source_id, target_id
        )
        target_rels, _ = _replace_relationships(
            target_doc.get("relationships", {}), source_id, target_id
        )
        merged_rels, rels_changed = _merge_relationship_maps(target_rels, source_rels)
        if rels_changed:
            ConceptsRepository.update_one(
                {"concept_id": target_id}, {"$set": {"relationships": merged_rels}}
            )

        # 3. Merge legacy names (if used)
        merged_names, names_changed = _merge_legacy_names(target_doc, source_doc)
        if names_changed:
            ConceptsRepository.update_one(
                {"concept_id": target_id}, {"$set": {"names": merged_names}}
            )

        # 4. Migrate text relations from source -> target (de-dup on unique index conflicts)
        target_text_relations = list(
            TextRelationsRepository.find({"subject_concept_id": target_id})
        )
        target_keys: set[Tuple[str, Any]] = set()
        for rel in target_text_relations:
            pred = rel.get("predicate")
            obj = rel.get("object_text_id")
            if isinstance(pred, str) and pred and obj is not None:
                target_keys.add((pred, obj))

        source_text_relations = list(
            TextRelationsRepository.find({"subject_concept_id": source_id})
        )
        for rel in source_text_relations:
            rel_id = rel.get("_id")
            pred = rel.get("predicate")
            obj = rel.get("object_text_id")
            if rel_id is None or not isinstance(pred, str) or not pred or obj is None:
                continue

            if (pred, obj) in target_keys:
                TextRelationsRepository.delete_one({"_id": rel_id})
                continue

            try:
                TextRelationsRepository.update_one(
                    {"_id": rel_id},
                    {
                        "$set": {
                            "subject_concept_id": target_id,
                            "updated_at": _now(),
                        }
                    },
                )
                target_keys.add((pred, obj))
            except DuplicateKeyError:
                # If another process created the relation on target between fetch and update.
                TextRelationsRepository.delete_one({"_id": rel_id})

        # 5. Delete Source (use canonical delete helper for consistent cleanup semantics)
        delete_concept(source_id)

        report["success"] = True
        report["executed"] = True

    except Exception as e:
        logger.error(
            f"Error merging concepts {source_id} -> {target_id}: {e}", exc_info=True
        )
        report["errors"].append(str(e))
        report["success"] = False

    return report
