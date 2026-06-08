"""Service for renaming concept IDs.

JVNAUTOSCI-945: Since concepts now have stable GUIDs, the human-readable
#V#... concept_id can be safely renamed. This service handles:

1. Validating the new ID doesn't conflict with existing concepts
2. Updating all relationship references (source and target)
3. Updating all text_relation references
4. Registering the old ID as an alias (CODE name) for backwards compatibility
5. Updating the concept document itself

The GUID remains unchanged, ensuring stable references via UUID.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import TextRelationsRepository
from ..db.mongo_client import get_db
from ..utils.concept_id_utils import (
    canonicalise_vontology_concept_id,
    validate_concept_id_for_rename,
)
from .namespace_service import concept_id_to_namespace_slug
from ..security.access_control import bypass_access_control
from .text_value_service import audit_concept_text_relations, upsert_text_for_concept

logger = logging.getLogger(__name__)

# Concepts that cannot be renamed (system/root concepts)
PROTECTED_CONCEPTS: frozenset[str] = frozenset(
    {
        "#V#thing",
        "#V#root",
        "#V#system",
        "#V#predicate",
        "#V#binary_predicate",
    }
)

# Concept ID for the "mentioned in Von code" marker type
MENTIONED_IN_VON_CODE_ID = "#V#mentioned_in_von_code"

RENAME_COVERED_REFERENCE_SURFACES: tuple[Dict[str, str], ...] = (
    {
        "collection": "concepts",
        "path": "concept_id",
        "handling": "updated",
        "reason": "canonical concept identifier for the renamed document",
    },
    {
        "collection": "concepts",
        "path": "relationships.*",
        "handling": "rewritten",
        "reason": "structural and dynamic relationship target values are concept IDs",
    },
    {
        "collection": "text_relations",
        "path": "subject_concept_id",
        "handling": "rewritten",
        "reason": "text relations are keyed by subject concept ID",
    },
    {
        "collection": "text_relations",
        "path": "hasName CODE alias",
        "handling": "preserved",
        "reason": "old #V# ID remains a CODE alias for backwards-compatible lookup",
    },
)

RENAME_EXCLUDED_REFERENCE_SURFACES: tuple[Dict[str, str], ...] = (
    {
        "surface": "chat/RAG/session namespaces",
        "handling": "blocked when detected",
        "reason": "namespace storage still depends on user/org concept IDs",
    },
    {
        "surface": "workflow IDs and code-mentioned concepts",
        "handling": "blocked when code-mentioned",
        "reason": "code and workflow metadata can treat these IDs as stable authority handles",
    },
    {
        "surface": "derived caches, indexes, and telemetry",
        "handling": "not migrated by this operation",
        "reason": "these are regenerated or require a separate GUID-backed migration path",
    },
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rename_reference_surface_report(
    *,
    namespace_usage: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Return the explicit support-surface contract for concept ID rename."""

    usage = dict(namespace_usage or {})
    return {
        "covered": [dict(item) for item in RENAME_COVERED_REFERENCE_SURFACES],
        "excluded": [dict(item) for item in RENAME_EXCLUDED_REFERENCE_SURFACES],
        "namespace_usage": usage,
        "namespace_blocking": any(int(v or 0) > 0 for v in usage.values()),
        "notes": [
            "This operation preserves GUID identity and rewrites the canonical Vontology surfaces it owns.",
            "It does not silently migrate namespace-bearing user/org data, RAG records, workflow IDs, caches, or telemetry.",
        ],
    }


def _replace_id_in_value(value: Any, old_id: str, new_id: str) -> Tuple[Any, bool]:
    """Replace occurrences of old_id with new_id within a value (recursively)."""
    if isinstance(value, str):
        if value == old_id:
            return new_id, True
        return value, False

    if isinstance(value, list):
        changed = False
        result: List[Any] = []
        for item in value:
            new_item, item_changed = _replace_id_in_value(item, old_id, new_id)
            changed = changed or item_changed
            result.append(new_item)
        return result, changed

    if isinstance(value, dict):
        changed = False
        out: Dict[str, Any] = {}
        for k, v in value.items():
            new_v, v_changed = _replace_id_in_value(v, old_id, new_id)
            changed = changed or v_changed
            out[k] = new_v
        return out, changed

    return value, False


def _replace_relationships(
    relationships: Any, old_id: str, new_id: str
) -> Tuple[Dict[str, Any], bool]:
    """Replace concept ID references within a relationships dict."""
    if not isinstance(relationships, dict):
        return {}, False
    changed = False
    new_rels: Dict[str, Any] = {}
    for predicate, targets in relationships.items():
        new_targets, targets_changed = _replace_id_in_value(targets, old_id, new_id)
        changed = changed or targets_changed
        new_rels[predicate] = new_targets
    return new_rels, changed


def check_concept_used_in_namespaces(concept_id: str) -> Tuple[bool, Dict[str, int]]:
    """Check if a concept_id is used in namespaces (as user or organisation).

    Namespaces are used in chat/interaction sessions and RAG indices.
    If a concept is used in namespaces, renaming it would break namespace
    resolution and orphan user data.

    Args:
        concept_id: The concept_id to check.

    Returns:
        Tuple of (is_used, usage_counts).
        usage_counts has keys: 'as_user_id', 'as_organisation_id', 'in_namespace_string'
    """
    db = get_db()
    if db is None:
        # Can't check, assume not used to avoid blocking all renames
        return False, {}

    usage: Dict[str, int] = {
        "as_user_id": 0,
        "as_organisation_id": 0,
        "in_namespace_string": 0,
    }

    # Composite namespace strings use "#V#<user_slug>@<org_slug>", while
    # user/org component fields carry the full canonical concept IDs.
    slug = concept_id_to_namespace_slug(concept_id) or concept_id
    escaped_slug = re.escape(slug)
    namespace_component_pattern = f"(^#V#{escaped_slug}($|@)|@{escaped_slug}$)"

    # Check chat_history collection
    chat_history = db.get_collection("chat_history")
    if chat_history is not None:
        # Check as user_id field
        user_count = chat_history.count_documents({"user_id": concept_id})
        usage["as_user_id"] += user_count

        # Check as organisation_concept_id field
        org_count = chat_history.count_documents(
            {"organisation_concept_id": concept_id}
        )
        usage["as_organisation_id"] += org_count

        # Check within namespace string (e.g., #V#user@org contains the slug)
        namespace_count = chat_history.count_documents(
            {"namespace": {"$regex": namespace_component_pattern}}
        )
        usage["in_namespace_string"] += namespace_count

    # Check interaction_sessions collection
    interactions = db.get_collection("interaction_sessions")
    if interactions is not None:
        user_count = interactions.count_documents({"user_id": concept_id})
        usage["as_user_id"] += user_count

        org_count = interactions.count_documents(
            {"organisation_concept_id": concept_id}
        )
        usage["as_organisation_id"] += org_count

        namespace_count = interactions.count_documents(
            {"namespace": {"$regex": namespace_component_pattern}}
        )
        usage["in_namespace_string"] += namespace_count

    total = sum(usage.values())
    return total > 0, usage


def check_concept_id_available(concept_id: str) -> Tuple[bool, Optional[str]]:
    """Check if a concept_id is available for use.

    Args:
        concept_id: The concept_id to check.

    Returns:
        Tuple of (is_available, error_message).
        If available, returns (True, None).
        If not available, returns (False, reason).
    """
    canonical = canonicalise_vontology_concept_id(concept_id)
    if not canonical:
        return False, "Invalid concept_id format"

    # Check if concept exists
    existing = ConceptsRepository.find_one({"concept_id": canonical})
    if existing:
        return False, f"Concept with ID '{canonical}' already exists"

    # Also check for aliases (CODE names that match this ID)
    # This prevents reusing an old ID that was previously aliased
    alias_check = TextRelationsRepository.find_one(
        {
            "predicate": "hasName",
            "text": canonical,
            "context.name_type": "CODE",
        }
    )
    if alias_check:
        subject_id = alias_check.get("subject_concept_id")
        return False, f"ID '{canonical}' is an alias for concept '{subject_id}'"

    return True, None


def rename_concept(
    old_id: str,
    new_id: str,
    *,
    simulate: bool = True,
    preserve_alias: bool = True,
    skip_inaccessible: bool = False,
) -> Dict[str, Any]:
    """Rename a concept's ID while preserving its GUID and data.

    This operation:
    1. Validates the rename is permissible
    2. Checks the new ID is available
    3. Updates all relationship references across all concepts
    4. Updates all text_relation references (optionally skipping inaccessible ones)
    5. Optionally registers the old ID as an alias
    6. Updates the concept document

    Args:
        old_id: The current concept_id to rename.
        new_id: The new concept_id to use.
        simulate: If True, returns a report without making changes.
        preserve_alias: If True, register old_id as a CODE alias (default True).
        skip_inaccessible: If True, skip inaccessible text relations instead of
            failing. Skipped relations are reported in the output.

    Returns:
        Dict containing the operation report/result.
    """
    report: Dict[str, Any] = {
        "success": False,
        "simulate": simulate,
        "old_id": old_id,
        "new_id": new_id,
        "skip_inaccessible": skip_inaccessible,
        "operations": [],
        "warnings": [],
        "errors": [],
        "skipped_relations": [],
        "reference_surfaces": _rename_reference_surface_report(),
    }

    # Validate and canonicalise IDs
    canonical_new, error = validate_concept_id_for_rename(old_id, new_id)
    if error:
        report["errors"].append(error)
        return report

    # Type guard: if we get here, canonical_new is not None
    assert (
        canonical_new is not None
    ), "validate_concept_id_for_rename bug: no error but canonical_new is None"

    canonical_old = canonicalise_vontology_concept_id(old_id)
    if not canonical_old:
        report["errors"].append("Invalid old concept_id")
        return report

    report["old_id"] = canonical_old
    report["new_id"] = canonical_new

    # Check protected concepts
    if canonical_old in PROTECTED_CONCEPTS:
        report["errors"].append(
            f"Concept '{canonical_old}' is protected and cannot be renamed"
        )
        return report

    # Verify old concept exists
    old_doc = ConceptsRepository.find_one({"concept_id": canonical_old})
    if not old_doc:
        report["errors"].append(f"Concept '{canonical_old}' not found")
        return report

    # Check for concepts mentioned in code (cannot be renamed to avoid breaking code)
    relationships = old_doc.get("relationships") or {}
    inst_of = relationships.get("is_an_instance_of") or []
    if isinstance(inst_of, str):
        inst_list = [inst_of]
    elif isinstance(inst_of, list):
        inst_list = [i for i in inst_of if isinstance(i, str)]
    else:
        inst_list = []
    if MENTIONED_IN_VON_CODE_ID in inst_list:
        report["errors"].append(
            f"Concept '{canonical_old}' is mentioned in Von code and cannot be renamed. "
            "Renaming would break code references. To proceed, first remove code references "
            "or update the code to use the concept's GUID instead."
        )
        return report

    # Check for concepts used in namespaces (user/org in chat sessions)
    is_used_in_namespaces, namespace_usage = check_concept_used_in_namespaces(
        canonical_old
    )
    report["reference_surfaces"] = _rename_reference_surface_report(
        namespace_usage=namespace_usage
    )
    if is_used_in_namespaces:
        usage_details = ", ".join(
            f"{k}: {v}" for k, v in namespace_usage.items() if v > 0
        )
        report["errors"].append(
            f"Concept '{canonical_old}' is used in namespaces ({usage_details}) and cannot be renamed. "
            "Renaming would orphan chat sessions and RAG indexed content. "
            "To enable renaming, migrate namespaces to use GUIDs instead of concept_ids, "
            "or update all affected sessions first."
        )
        return report

    # Verify GUID exists (required for rename safety)
    guid = old_doc.get("guid")
    if not guid:
        report["warnings"].append(
            "Concept lacks a GUID; recommend running GUID migration first"
        )

    # Check new ID availability
    is_available, availability_error = check_concept_id_available(canonical_new)
    if not is_available:
        report["errors"].append(availability_error or "New ID not available")
        return report

    # Analyse relationship references across all concepts
    concepts_cursor = ConceptsRepository.find({}, {"concept_id": 1, "relationships": 1})
    affected_concepts: List[str] = []

    for doc in concepts_cursor:
        cid = doc.get("concept_id")
        if not isinstance(cid, str) or not cid:
            continue
        _, changed = _replace_relationships(
            doc.get("relationships"), canonical_old, canonical_new
        )
        if changed:
            affected_concepts.append(cid)

    if affected_concepts:
        report["operations"].append(
            {
                "type": "rewrite_relationship_references",
                "count": len(affected_concepts),
                "concept_ids": affected_concepts[:50],
                "detail": f"Replace '{canonical_old}' with '{canonical_new}' in relationship targets",
            }
        )
        if len(affected_concepts) > 50:
            report["warnings"].append(
                f"Relationship rewrite affects {len(affected_concepts)} concepts; "
                "report truncated to 50 IDs"
            )

    # Analyse text_relation references and check accessibility
    audit_result = audit_concept_text_relations(
        canonical_old, include_text_preview=False
    )
    text_relations_affected = audit_result.get("total_relations", 0)
    accessible_count = audit_result.get("accessible_count", 0)
    inaccessible_count = audit_result.get("inaccessible_count", 0)
    blocking_relations = audit_result.get("blocking_relations", [])

    if text_relations_affected > 0:
        report["operations"].append(
            {
                "type": "rewrite_text_relation_subjects",
                "count": text_relations_affected,
                "accessible": accessible_count,
                "inaccessible": inaccessible_count,
                "detail": f"Update subject_concept_id from '{canonical_old}' to '{canonical_new}'",
            }
        )

    # Handle inaccessible text relations
    if inaccessible_count > 0:
        if skip_inaccessible:
            report["warnings"].append(
                f"{inaccessible_count} text relation(s) are inaccessible and will be skipped"
            )
            report["skipped_relations"] = blocking_relations
        else:
            report["errors"].append(
                f"Cannot rename: {inaccessible_count} text relation(s) are inaccessible. "
                f"Use skip_inaccessible=True to proceed anyway, or use "
                f"audit_concept_text_relations to inspect and clean up first."
            )
            report["blocking_relations"] = blocking_relations
            return report

    # Plan alias registration
    if preserve_alias:
        report["operations"].append(
            {
                "type": "register_old_id_as_alias",
                "old_id": canonical_old,
                "detail": f"Register '{canonical_old}' as CODE alias for backwards compatibility",
            }
        )

    # Plan the actual concept_id update
    report["operations"].append(
        {
            "type": "update_concept_id",
            "old_id": canonical_old,
            "new_id": canonical_new,
            "detail": "Update concept document with new concept_id",
        }
    )

    if simulate:
        report["success"] = True
        return report

    # EXECUTION
    try:
        # 1. Rewrite relationship references in all concepts
        concepts_cursor = ConceptsRepository.find(
            {}, {"concept_id": 1, "relationships": 1}
        )
        for doc in concepts_cursor:
            cid = doc.get("concept_id")
            if not isinstance(cid, str) or not cid:
                continue

            new_rels, changed = _replace_relationships(
                doc.get("relationships"), canonical_old, canonical_new
            )
            if changed:
                ConceptsRepository.update_one(
                    {"concept_id": cid},
                    {"$set": {"relationships": new_rels, "updated_at": _now()}},
                )

        # 2. Rewrite text_relation subject references
        # If skip_inaccessible is True, only update accessible relations
        if skip_inaccessible and inaccessible_count > 0:
            # Get IDs of accessible relations and update only those
            accessible_relation_ids = [
                rel.get("relation_id")
                for rel in audit_result.get("relations", [])
                if rel.get("accessible") and rel.get("relation_id")
            ]
            if accessible_relation_ids:
                from bson import ObjectId

                TextRelationsRepository.update_many(
                    {
                        "_id": {
                            "$in": [ObjectId(rid) for rid in accessible_relation_ids]
                        }
                    },
                    {
                        "$set": {
                            "subject_concept_id": canonical_new,
                            "updated_at": _now(),
                        }
                    },
                )
            report["text_relations_updated"] = len(accessible_relation_ids)
            report["text_relations_skipped"] = inaccessible_count
        else:
            # Update all text relations
            TextRelationsRepository.update_many(
                {"subject_concept_id": canonical_old},
                {"$set": {"subject_concept_id": canonical_new, "updated_at": _now()}},
            )
            report["text_relations_updated"] = text_relations_affected
            report["text_relations_skipped"] = 0

        # 3. Register old ID as alias (CODE name)
        # Use bypass_access_control since this is an admin operation
        if preserve_alias:
            with bypass_access_control():
                upsert_text_for_concept(
                    subject_concept_id=canonical_new,  # Use new ID as subject
                    predicate="hasName",
                    text=canonical_old,  # Old ID as the alias text
                    lang="en-NZ",
                    context={"name_type": "CODE", "alias_source": "rename"},
                )

        # 4. Update the concept document itself
        ConceptsRepository.update_one(
            {"concept_id": canonical_old},
            {"$set": {"concept_id": canonical_new, "updated_at": _now()}},
        )

        report["success"] = True
        report["executed"] = True

    except Exception as e:
        logger.error(
            "Error renaming concept %s -> %s: %s",
            canonical_old,
            canonical_new,
            e,
            exc_info=True,
        )
        report["errors"].append(str(e))
        report["success"] = False

    return report


def get_concept_aliases(concept_id: str) -> List[Dict[str, Any]]:
    """Get all aliases (old IDs) for a concept.

    Args:
        concept_id: The concept_id to look up aliases for.

    Returns:
        List of alias records (CODE names that look like concept IDs).
    """
    canonical = canonicalise_vontology_concept_id(concept_id)
    if not canonical:
        return []

    aliases = []
    cursor = TextRelationsRepository.find(
        {
            "subject_concept_id": canonical,
            "predicate": "hasName",
            "context.name_type": "CODE",
        }
    )

    for doc in cursor:
        text = doc.get("text", "")
        # Only include entries that look like concept IDs
        if text.startswith("#V#") or text.startswith("#v#"):
            aliases.append(
                {
                    "alias": text,
                    "source": doc.get("context", {}).get("alias_source"),
                    "created_at": doc.get("created_at"),
                }
            )

    return aliases


def resolve_concept_by_alias(alias_id: str) -> Optional[str]:
    """Resolve an old/alias concept ID to its current ID.

    Args:
        alias_id: A potentially old/aliased concept_id.

    Returns:
        The current concept_id if alias_id is an alias, or None if not found.
    """
    canonical = canonicalise_vontology_concept_id(alias_id)
    if not canonical:
        return None

    # First check if this ID exists directly
    direct = ConceptsRepository.find_one({"concept_id": canonical})
    if direct:
        return canonical  # Not an alias, direct hit

    # Check if it's registered as an alias
    alias_record = TextRelationsRepository.find_one(
        {
            "predicate": "hasName",
            "text": canonical,
            "context.name_type": "CODE",
        }
    )

    if alias_record:
        return alias_record.get("subject_concept_id")

    return None
