"""Service functions for TextValue documents and their relations.

Provides create, link, upsert, and fetch helpers around the text_values and
text_relations repositories, with simple normalization and validation.
"""

from __future__ import annotations

import re
from typing import Dict, Any, List, Optional, Literal, cast, Tuple
from datetime import datetime, timezone
from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError

from ..db.repositories.text_value_repository import (
    TextValuesRepository,
    TextRelationsRepository,
)
from ..models.text_value_models import (
    RelationPredicate,
    TextValueModel,
    TextRelationModel,
)
from ..security.access_control import can_access_concept


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _invalidate_stats_for_predicate_change(predicate: str | None) -> None:
    """Best-effort stats invalidation for ontology predicate extent changes."""

    if not isinstance(predicate, str) or not predicate.startswith("#V#"):
        return
    try:
        from .vontology_concept_stats_service import (
            invalidate_vontology_concept_stats_cache,
        )

        invalidate_vontology_concept_stats_cache(
            reason="predicate_extent_mutation",
            affected_concepts=[predicate],
        )
    except Exception:
        pass


def _normalize_text(raw: str) -> str:
    """Normalise text for fingerprint/dedup purposes.

    This is deliberately *less* aggressive than a full whitespace collapse.
    Newlines are meaningful for Markdown and other formatted text, so we preserve
    them in the fingerprint and only normalise intra-line whitespace.
    """
    text = raw.strip()
    # Normalise line endings so equivalent text across platforms dedups.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of non-newline whitespace within lines.
    text = re.sub(r"[\t\f\v ]+", " ", text)
    # Strip incidental spaces around newlines (does not remove the newlines).
    text = re.sub(r" *\n *", "\n", text)
    return text


def _prepare_persisted_text(raw: str) -> str:
    """Light cleanup for storage: trim ends but preserve internal newlines/spacing."""
    return raw.strip()


def _compute_fingerprint(text: str, lang: str) -> str:
    # Normalize: collapse whitespace, lowercase, include lang to avoid cross-lang collisions
    normalized = _normalize_text(text).lower()
    return f"{normalized}||{lang.lower()}"


def create_text_value(
    text: str, lang: str = "en", provenance: Optional[Dict[str, Any]] = None
) -> str:
    """Create or return an existing TextValue document based on (fingerprint, lang).

    Returns the inserted/existing ObjectId as a hex string.
    """
    provenance = provenance or {}
    stored_text = _prepare_persisted_text(text)
    fp = _compute_fingerprint(stored_text, lang)

    # Validate input via Pydantic (stores the preserved formatting)
    tv = TextValueModel(text=stored_text, lang=lang, provenance=provenance)

    # Try to find by fingerprint+lang
    existing = TextValuesRepository.find_one({"fingerprint": fp, "lang": tv.lang})
    if existing and existing.get("_id"):
        return str(existing["_id"])

    doc = {
        "text": tv.text,
        "lang": tv.lang,
        "provenance": provenance,
        "fingerprint": fp,
        "created_at": _now(),
        "updated_at": _now(),
    }
    res = TextValuesRepository.insert_one(doc)
    return str(res.inserted_id)


def link_text_to_concept(
    subject_concept_id: str,
    predicate: str,
    object_text_id: str,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[str, bool, bool]:
    """Create a relation linking a concept to a text value.

    Enforces uniqueness via collection unique index on (subject, predicate, object).
    Returns (relation id, created_flag, context_updated_flag).
    """
    if not can_access_concept(subject_concept_id):
        raise PermissionError("User cannot link text to an inaccessible concept")
    context = context or {}
    # Validate relation via Pydantic (predicate constrained)
    _ = TextRelationModel(
        subject_concept_id=subject_concept_id,
        predicate=cast(
            Literal[
                "hasName", "hasNote", "hasDescription", "hasInteraction", "hasContent"
            ],
            predicate,
        ),  # Literal validated in model
        object_text_id=object_text_id,
        context=context,
    )
    relation = {
        "subject_concept_id": subject_concept_id,
        "predicate": predicate,
        "object_text_id": object_text_id,
        "context": context,
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        res = TextRelationsRepository.insert_one(relation)
        return str(res.inserted_id), True, False
    except DuplicateKeyError:
        # Already linked; fetch existing id
        existing = TextRelationsRepository.find_one(
            {
                "subject_concept_id": subject_concept_id,
                "predicate": predicate,
                "object_text_id": object_text_id,
            }
        )
        context_updated = False
        if existing and context:
            existing_context = existing.get("context") or {}
            if existing_context != context:
                TextRelationsRepository.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {"context": context, "updated_at": _now()}},
                )
                context_updated = True
        return (
            str(existing["_id"]) if existing and existing.get("_id") else "",
            False,
            context_updated,
        )


def upsert_text_for_concept(
    subject_concept_id: str,
    predicate: str,
    text: str,
    lang: str = "en",
    provenance: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convenience: ensure a TextValue exists and is linked to the concept.

    Returns a payload with text_value_id and relation_id.
    """
    stored_text = _prepare_persisted_text(text)
    text_value_id = create_text_value(
        text=stored_text, lang=lang, provenance=provenance
    )
    relation_id, relation_created, context_updated = link_text_to_concept(
        subject_concept_id=subject_concept_id,
        predicate=predicate,
        object_text_id=text_value_id,
        context=context,
    )

    # JVNAUTOSCI-680: Mark concept embedding as stale when text relations change
    if relation_created or context_updated:
        try:
            from ..db.repositories.concepts_repository import ConceptsRepository

            ConceptsRepository.update_one(
                {"concept_id": subject_concept_id},
                {"$set": {"embedding_status": "stale"}},
            )
        except Exception:
            pass  # Best effort - don't fail the main operation

    if relation_created:
        _invalidate_stats_for_predicate_change(predicate)

    relation_doc = None
    if relation_id:
        try:
            relation_doc = TextRelationsRepository.find_one(
                {"_id": ObjectId(relation_id)}
            )
        except (InvalidId, TypeError):
            relation_doc = None

    payload: Dict[str, Any] = {
        "text_value_id": text_value_id,
        "relation_id": relation_id,
        "relation_created": relation_created,
        "context_updated": context_updated,
        "text": stored_text,
        "lang": lang,
    }
    if relation_doc:
        payload["context"] = relation_doc.get("context", {})
        payload["predicate"] = relation_doc.get("predicate")
    elif context:
        payload["context"] = context
    return payload


def get_texts_for_concept(
    subject_concept_id: str,
    predicate: Optional[str] = None,
    lang: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Fetch linked TextValues for a concept, optionally filtered by predicate and lang.

    Returns a list of { text, lang, text_value_id, predicate, relation_id, context }.
    """
    if not can_access_concept(subject_concept_id):
        return []
    rel_filter: Dict[str, Any] = {"subject_concept_id": subject_concept_id}
    if predicate:
        rel_filter["predicate"] = predicate

    relations = list(TextRelationsRepository.find(rel_filter, limit=limit))
    if not relations:
        return []

    object_id_values: List[ObjectId] = []
    string_id_values: List[str] = []
    for relation in relations:
        raw_id = relation.get("object_text_id")
        if not raw_id:
            continue
        if isinstance(raw_id, ObjectId):
            object_id_values.append(raw_id)
            continue
        if isinstance(raw_id, str):
            try:
                object_id_values.append(ObjectId(raw_id))
                continue
            except (InvalidId, TypeError):
                string_id_values.append(raw_id)
                continue
        string_id_values.append(str(raw_id))

    values: Dict[str, Any] = {}
    if object_id_values:
        tv_filter: Dict[str, Any] = {"_id": {"$in": object_id_values}}
        if lang:
            tv_filter["lang"] = lang
        for tv in TextValuesRepository.find(tv_filter):
            values[str(tv["_id"])] = tv

    if string_id_values:
        tv_filter: Dict[str, Any] = {"_id": {"$in": string_id_values}}
        if lang:
            tv_filter["lang"] = lang
        for tv in TextValuesRepository.find(tv_filter):
            values[str(tv["_id"])] = tv

    results: List[Dict[str, Any]] = []
    for r in relations:
        key = r.get("object_text_id")
        if isinstance(key, ObjectId):
            lookup_key = str(key)
        else:
            lookup_key = str(key) if key is not None else None
        tv = values.get(lookup_key) if lookup_key is not None else None
        if not tv:
            continue
        results.append(
            {
                "text": tv.get("text"),
                "lang": tv.get("lang"),
                "text_value_id": str(tv.get("_id")),
                "predicate": r.get("predicate"),
                "relation_id": str(r.get("_id")) if r.get("_id") else None,
                "context": r.get("context", {}),
            }
        )
        if len(results) >= limit:
            break
    return results


def update_text_relation_text(
    subject_concept_id: str,
    relation_id: str,
    new_text: str,
    lang: str = "en",
    provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Update the text tied to a specific relation.

    Strategy:
      * Locate relation (validate subject)
      * If existing TextValue already has new_text (case/whitespace normalized) do nothing
      * Else find or create a TextValue for new_text (dedup via fingerprint)
      * Update relation.object_text_id accordingly & timestamps
    Returns minimal payload with updated relation + text contents.
    """
    if not can_access_concept(subject_concept_id):
        raise PermissionError("User cannot update text for an inaccessible concept")
    provenance = provenance or {}

    try:
        rel = TextRelationsRepository.find_one({"_id": ObjectId(relation_id)})
    except (InvalidId, TypeError):
        rel = None
    if not rel:
        raise ValueError("Relation not found")
    if rel.get("subject_concept_id") != subject_concept_id:
        raise ValueError("Relation subject mismatch")

    lang = (lang or "en").strip() or "en"

    # Fetch current text value if available so we can decide whether an update is needed
    current_tv_id = rel.get("object_text_id")
    current_tv_id_str = str(current_tv_id) if current_tv_id is not None else None
    current_tv = None
    if current_tv_id:
        try:
            current_tv = TextValuesRepository.find_one({"_id": ObjectId(current_tv_id)})
        except (InvalidId, TypeError):
            current_tv = None

    new_raw = _prepare_persisted_text(new_text)
    if (
        current_tv
        and current_tv.get("text") == new_raw
        and (current_tv.get("lang") or lang) == lang
    ):
        return {
            "relation_id": relation_id,
            "text_value_id": current_tv_id_str,
            "text": new_raw,
            "lang": lang,
            "predicate": rel.get("predicate"),
            "updated": False,
        }

    # Reuse create_text_value for fingerprint logic to avoid duplicate documents for whitespace-only changes
    new_text_value_id = create_text_value(
        text=new_raw, lang=lang, provenance=provenance
    )
    relation_updated = False
    if new_text_value_id != current_tv_id_str:
        TextRelationsRepository.update_one(
            {"_id": ObjectId(relation_id)},
            {"$set": {"object_text_id": new_text_value_id, "updated_at": _now()}},
        )
        relation_updated = True

    return {
        "relation_id": relation_id,
        "text_value_id": new_text_value_id,
        "text": new_raw,
        "lang": lang,
        "predicate": rel.get("predicate"),
        "updated": relation_updated,
    }


def delete_text_relation_by_predicate_and_text(
    subject_concept_id: str,
    predicate: str,
    text: str,
    *,
    lang: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    garbage_collect: bool = False,
) -> Dict[str, Any]:
    """Delete a text relation by predicate and text value. Optionally garbage-collect orphaned text values.

    Finds the text relation matching the given predicate and text value, then deletes it.
    """
    if not can_access_concept(subject_concept_id):
        raise PermissionError("User cannot delete text for an inaccessible concept")

    lang = (lang or "en").strip() or "en"
    raw_text = _prepare_persisted_text(text)
    fingerprint = _compute_fingerprint(raw_text, lang)

    if predicate == RelationPredicate.HAS_NAME and context:
        name_type = context.get("name_type")
        if isinstance(name_type, str):
            normalized_context = dict(context)
            normalized_context["name_type"] = name_type.strip().upper()
            context = normalized_context

    text_value = TextValuesRepository.find_one(
        {"fingerprint": fingerprint, "lang": lang}
    )
    if not text_value:
        # Legacy fallback: direct text/lang lookup
        text_value = TextValuesRepository.find_one({"text": raw_text, "lang": lang})
    if not text_value:
        text_value = TextValuesRepository.find_one({"text": raw_text})
    if not text_value:
        raise ValueError(f"Text value '{text}' not found")

    text_value_id = str(text_value["_id"])

    rel_candidates = list(
        TextRelationsRepository.find(
            {
                "subject_concept_id": subject_concept_id,
                "predicate": predicate,
                "object_text_id": text_value_id,
            }
        )
    )

    if not rel_candidates:
        raise ValueError(
            f"Text relation not found for predicate '{predicate}' and text '{text}'"
        )

    relation = None
    if context:
        for rel in rel_candidates:
            existing_context = rel.get("context") or {}
            if all(
                existing_context.get(k) == v
                for k, v in context.items()
                if v is not None
            ):
                relation = rel
                break
    if relation is None:
        relation = rel_candidates[0]

    relation_id = str(relation["_id"])

    # Delete the relation
    TextRelationsRepository.delete_one({"_id": ObjectId(relation_id)})
    _invalidate_stats_for_predicate_change(predicate)

    # Check if the text value is now orphaned
    orphaned = False
    if garbage_collect:
        others = TextRelationsRepository.find_one({"object_text_id": text_value_id})
        if not others:
            # Safe to delete the text value
            TextValuesRepository.delete_one({"_id": ObjectId(text_value_id)})
            orphaned = True

    return {
        "deleted": True,
        "relation_id": relation_id,
        "predicate": predicate,
        "text": text,
        "orphaned_text_value_deleted": orphaned,
    }


def delete_text_relation(
    subject_concept_id: str,
    relation_id: str,
    *,
    garbage_collect: bool = False,
) -> Dict[str, Any]:
    """Delete a specific text relation. Optionally garbage-collect orphaned text values.

    For now, we retain the TextValue (shared across relations) unless unreferenced;
    if unreferenced we remove it to avoid buildup.
    """
    if not can_access_concept(subject_concept_id):
        raise PermissionError("User cannot delete text for an inaccessible concept")
    rel = TextRelationsRepository.find_one({"_id": ObjectId(relation_id)})
    if not rel:
        raise ValueError("Relation not found")
    if rel.get("subject_concept_id") != subject_concept_id:
        raise ValueError("Relation subject mismatch")
    tv_id = rel.get("object_text_id")
    predicate = rel.get("predicate")
    TextRelationsRepository.delete_one({"_id": ObjectId(relation_id)})
    _invalidate_stats_for_predicate_change(predicate if isinstance(predicate, str) else None)
    orphaned = False
    if tv_id and garbage_collect:
        # Check if any other relation references this text value
        others = TextRelationsRepository.find_one({"object_text_id": tv_id})
        if not others:
            # Safe to delete
            TextValuesRepository.delete_one({"_id": ObjectId(tv_id)})
            orphaned = True
    return {
        "deleted": True,
        "relation_id": relation_id,
        "orphaned_text_value_deleted": orphaned,
    }


def get_text_relations_summary(
    subject_concept_id: str,
    *,
    predicates: Optional[List[str]] = None,
    languages: Optional[List[str]] = None,
    max_relation_ids_per_group: int = 25,
) -> Dict[str, Any]:
    """Return counts + relation IDs grouped by predicate/language.

    This is intentionally "lightweight": it does not return full text bodies.

    Notes:
    - Text relation documents do not store language directly; language is derived from
      the referenced text_values documents.
    - `languages` is therefore a post-filter applied after resolving text value lang.
    """
    if not can_access_concept(subject_concept_id):
        raise PermissionError("User cannot view text for an inaccessible concept")

    pred_filter: Dict[str, Any] = {"subject_concept_id": subject_concept_id}
    if predicates:
        pred_filter["predicate"] = {
            "$in": [p for p in predicates if isinstance(p, str)]
        }

    rels = list(
        TextRelationsRepository.find(
            pred_filter,
            projection={
                "_id": 1,
                "predicate": 1,
                "object_text_id": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        )
    )

    tv_object_ids: List[ObjectId] = []
    tv_string_ids: List[str] = []
    for rel in rels:
        tv_id = rel.get("object_text_id")
        if not tv_id:
            continue
        if isinstance(tv_id, ObjectId):
            tv_object_ids.append(tv_id)
        elif isinstance(tv_id, str):
            try:
                tv_object_ids.append(ObjectId(tv_id))
            except (InvalidId, TypeError):
                tv_string_ids.append(tv_id)

    tv_lang_by_id: Dict[str, str] = {}
    if tv_object_ids:
        for tv in TextValuesRepository.find(
            {"_id": {"$in": tv_object_ids}},
            projection={"lang": 1},
        ):
            tv_lang_by_id[str(tv.get("_id"))] = str(tv.get("lang") or "")
    if tv_string_ids:
        # Some stored ids may be non-ObjectId strings.
        for tv in TextValuesRepository.find(
            {"_id": {"$in": tv_string_ids}},
            projection={"lang": 1},
        ):
            tv_lang_by_id[str(tv.get("_id"))] = str(tv.get("lang") or "")

    language_allow = None
    if languages:
        language_allow = {str(lang) for lang in languages if isinstance(lang, str)}

    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    for rel in rels:
        predicate = str(rel.get("predicate") or "")
        tv_id = rel.get("object_text_id")
        tv_id_str = str(tv_id) if tv_id is not None else ""
        lang = tv_lang_by_id.get(tv_id_str) or ""

        if language_allow is not None and lang not in language_allow:
            continue

        key = (predicate, lang)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "predicate": predicate,
                "language": lang,
                "count": 0,
                "relation_ids": [],
                "latest_relation_id": None,
                "latest_updated_at": None,
            }
            grouped[key] = bucket

        bucket["count"] += 1
        rel_id = str(rel.get("_id"))
        if rel_id:
            if len(bucket["relation_ids"]) < max_relation_ids_per_group:
                bucket["relation_ids"].append(rel_id)

        updated_at = rel.get("updated_at") or rel.get("created_at")
        if updated_at is not None:
            current_latest = bucket.get("latest_updated_at")
            if current_latest is None or updated_at > current_latest:
                bucket["latest_updated_at"] = updated_at
                bucket["latest_relation_id"] = rel_id

    summary_items = list(grouped.values())
    summary_items.sort(
        key=lambda x: (x.get("predicate") or "", x.get("language") or "")
    )
    for item in summary_items:
        # Make datetime JSON-friendly
        dt = item.get("latest_updated_at")
        if isinstance(dt, datetime):
            item["latest_updated_at"] = dt.isoformat()

    return {
        "success": True,
        "concept_id": subject_concept_id,
        "groups": summary_items,
        "groups_found": len(summary_items),
        "total_relations_scanned": len(rels),
        "max_relation_ids_per_group": max_relation_ids_per_group,
    }


def upsert_singleton_text_relation(
    *,
    subject_concept_id: str,
    predicate: str,
    text: str,
    lang: str = "en-NZ",
    policy: str = "replace_others",
    provenance: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    garbage_collect: bool = True,
) -> Dict[str, Any]:
    """Upsert text and enforce singleton semantics for (concept, predicate, language).

    `policy='replace_others'` means: after inserting/upserting the desired relation,
    delete all other relations for the same subject+predicate whose text_value language
    matches `lang`.
    """
    if policy != "replace_others":
        raise ValueError("Unsupported policy (expected 'replace_others')")

    result = upsert_text_for_concept(
        subject_concept_id=subject_concept_id,
        predicate=predicate,
        text=text,
        lang=lang,
        provenance=provenance,
        context=context,
    )
    kept_relation_id = str(result.get("relation_id") or "")

    rels = list(
        TextRelationsRepository.find(
            {
                "subject_concept_id": subject_concept_id,
                "predicate": predicate,
            },
            projection={"_id": 1, "object_text_id": 1},
        )
    )

    tv_object_ids: List[ObjectId] = []
    tv_ids_raw: List[str] = []
    for rel in rels:
        tv_id = rel.get("object_text_id")
        if tv_id is None:
            continue
        if isinstance(tv_id, ObjectId):
            tv_object_ids.append(tv_id)
        else:
            tv_id_str = str(tv_id)
            try:
                tv_object_ids.append(ObjectId(tv_id_str))
            except (InvalidId, TypeError):
                tv_ids_raw.append(tv_id_str)

    tv_lang_by_id: Dict[str, str] = {}
    if tv_object_ids:
        for tv in TextValuesRepository.find(
            {"_id": {"$in": tv_object_ids}},
            projection={"lang": 1},
        ):
            tv_lang_by_id[str(tv.get("_id"))] = str(tv.get("lang") or "")
    if tv_ids_raw:
        for tv in TextValuesRepository.find(
            {"_id": {"$in": tv_ids_raw}},
            projection={"lang": 1},
        ):
            tv_lang_by_id[str(tv.get("_id"))] = str(tv.get("lang") or "")

    replaced_relation_ids: List[str] = []
    lang_normalised = (lang or "").strip()
    for rel in rels:
        rel_id = str(rel.get("_id"))
        if not rel_id or rel_id == kept_relation_id:
            continue

        tv_id = rel.get("object_text_id")
        tv_id_str = str(tv_id) if tv_id is not None else ""
        rel_lang = tv_lang_by_id.get(tv_id_str) or ""

        if rel_lang != lang_normalised:
            continue

        delete_text_relation(
            subject_concept_id,
            rel_id,
            garbage_collect=garbage_collect,
        )
        replaced_relation_ids.append(rel_id)

    return {
        "success": True,
        "concept_id": subject_concept_id,
        "predicate": predicate,
        "language": lang,
        "kept_relation_id": kept_relation_id,
        "replaced_relation_ids": replaced_relation_ids,
        "replaced_count": len(replaced_relation_ids),
        "relation_created": bool(result.get("relation_created")),
        "text_value_id": result.get("text_value_id"),
    }


def audit_concept_text_relations(
    concept_id: str,
    *,
    include_text_preview: bool = True,
    max_preview_length: int = 100,
) -> Dict[str, Any]:
    """Audit all text relations attached to a concept, including accessibility status.

    This function bypasses normal access control to provide a complete view of all
    text relations. It's designed for pre-rename auditing to identify relations that
    would block a rename operation.

    Returns:
        A dictionary with:
        - concept_id: The queried concept
        - total_relations: Total count of text relations
        - accessible_count: Relations that can be modified by current user
        - inaccessible_count: Relations that would block rename
        - relations: List of relation details with accessibility status
        - summary: Grouped counts by predicate
    """
    # Query all text relations directly, bypassing normal access control
    rels = list(
        TextRelationsRepository.find(
            {"subject_concept_id": concept_id},
            limit=1000,
        )
    )

    if not rels:
        return {
            "success": True,
            "concept_id": concept_id,
            "total_relations": 0,
            "accessible_count": 0,
            "inaccessible_count": 0,
            "relations": [],
            "summary": {},
            "can_rename": True,
            "blocking_relations": [],
        }

    # Collect text value IDs to fetch text content
    tv_ids: List[ObjectId] = []
    for rel in rels:
        tv_id = rel.get("object_text_id")
        if tv_id:
            try:
                if isinstance(tv_id, ObjectId):
                    tv_ids.append(tv_id)
                elif isinstance(tv_id, str):
                    tv_ids.append(ObjectId(tv_id))
            except (InvalidId, TypeError):
                pass

    # Fetch text values
    tv_by_id: Dict[str, Dict[str, Any]] = {}
    if tv_ids:
        for tv in TextValuesRepository.find({"_id": {"$in": tv_ids}}):
            tv_by_id[str(tv.get("_id"))] = {
                "text": tv.get("text", ""),
                "lang": tv.get("lang", ""),
            }

    # Check accessibility for each relation
    accessible_count = 0
    inaccessible_count = 0
    relations_detail: List[Dict[str, Any]] = []
    blocking_relations: List[Dict[str, Any]] = []
    summary_by_predicate: Dict[str, Dict[str, int]] = {}

    for rel in rels:
        rel_id = str(rel.get("_id"))
        predicate = rel.get("predicate", "")
        tv_id = rel.get("object_text_id")
        tv_id_str = str(tv_id) if tv_id else ""

        # Check if current user can access this concept (and thus modify relations)
        is_accessible = can_access_concept(concept_id)

        if is_accessible:
            accessible_count += 1
        else:
            inaccessible_count += 1

        # Get text info
        tv_info = tv_by_id.get(tv_id_str, {})
        text = tv_info.get("text", "")
        lang = tv_info.get("lang", "")

        # Build relation detail
        rel_detail: Dict[str, Any] = {
            "relation_id": rel_id,
            "predicate": predicate,
            "text_value_id": tv_id_str,
            "language": lang,
            "accessible": is_accessible,
            "context": rel.get("context", {}),
            "created_at": (
                rel.get("created_at").isoformat() if rel.get("created_at") else None
            ),
        }

        if include_text_preview:
            preview = text[:max_preview_length]
            if len(text) > max_preview_length:
                preview += "..."
            rel_detail["text_preview"] = preview

        relations_detail.append(rel_detail)

        if not is_accessible:
            blocking_relations.append(rel_detail)

        # Update summary
        if predicate not in summary_by_predicate:
            summary_by_predicate[predicate] = {"accessible": 0, "inaccessible": 0}
        if is_accessible:
            summary_by_predicate[predicate]["accessible"] += 1
        else:
            summary_by_predicate[predicate]["inaccessible"] += 1

    return {
        "success": True,
        "concept_id": concept_id,
        "total_relations": len(rels),
        "accessible_count": accessible_count,
        "inaccessible_count": inaccessible_count,
        "can_rename": inaccessible_count == 0,
        "relations": relations_detail,
        "blocking_relations": blocking_relations,
        "summary": summary_by_predicate,
    }


__all__ = [
    "RelationPredicate",
    "create_text_value",
    "link_text_to_concept",
    "upsert_text_for_concept",
    "upsert_singleton_text_relation",
    "get_texts_for_concept",
    "get_text_relations_summary",
    "update_text_relation_text",
    "delete_text_relation_by_predicate_and_text",
    "delete_text_relation",
    "audit_concept_text_relations",
]
