"""Concept embedding aggregation service (JVNAUTOSCI-680).

Builds searchable text and metadata for concept vector embeddings.
Aggregates kind-specific content for semantic search:
- All concepts: names, descriptions, notes, content
- Predicates: extent sample (relations showing usage), domain/range
- Types: instance names, subtype names
- Individuals: type chain, key relations

This service is used by the concept index worker to build documents
for the LlamaIndex concept vector store.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..vontology.utils_vontology import (
    get_concept_display_name_with_names_fallback,
    get_concept_description,
    get_concept_notes,
    is_type,
    is_predicate,
)

logger = logging.getLogger(__name__)

# Embedding status constants
EMBEDDING_STATUS_PENDING = "pending"
EMBEDDING_STATUS_INDEXED = "indexed"
EMBEDDING_STATUS_STALE = "stale"
EMBEDDING_STATUS_FAILED = "failed"

EMBEDDING_QUEUE_STATUSES = (
    EMBEDDING_STATUS_PENDING,
    EMBEDDING_STATUS_STALE,
    EMBEDDING_STATUS_FAILED,
)

# Namespace for concept embeddings
CONCEPT_EMBEDDING_NAMESPACE = "concepts"


def determine_concept_kind(concept_doc: Dict[str, Any]) -> str:
    """Determine if a concept is a type, predicate, or individual.

    Args:
        concept_doc: The concept document from MongoDB

    Returns:
        One of: "type", "predicate", "individual"
    """
    try:
        # Check predicate FIRST: predicates can have is_a_type_of relationships
        if is_predicate(concept_doc):
            return "predicate"
        elif is_type(concept_doc):
            return "type"
        else:
            return "individual"
    except Exception as e:
        logger.warning(
            f"Error determining concept kind: {e}, defaulting to 'individual'"
        )
        return "individual"


def _get_all_names(concept_doc: Dict[str, Any]) -> List[str]:
    """Extract all names from a concept document.

    Returns names from both the names[] array and legacy name field.
    """
    names: List[str] = []
    seen: set[str] = set()

    # Get names from names[] array
    names_array = concept_doc.get("names", [])
    if isinstance(names_array, list):
        for entry in names_array:
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and name.strip():
                    normalised = name.strip()
                    if normalised.lower() not in seen:
                        seen.add(normalised.lower())
                        names.append(normalised)

    # Legacy name field
    legacy_name = concept_doc.get("name")
    if isinstance(legacy_name, str) and legacy_name.strip():
        normalised = legacy_name.strip()
        if normalised.lower() not in seen:
            names.append(normalised)

    return names


def _get_text_relation_values(
    concept_id: str, predicates: List[str]
) -> Dict[str, List[str]]:
    """Get text values for a concept from text_relations.

    Args:
        concept_id: The concept ID to look up
        predicates: List of predicates to search for (e.g., ["hasDescription", "hasNote"])

    Returns:
        Dict mapping predicate -> list of text values
    """
    result: Dict[str, List[str]] = {p: [] for p in predicates}

    try:
        relations = list(
            TextRelationsRepository.find(
                {"subject_concept_id": concept_id, "predicate": {"$in": predicates}}
            )
        )

        if not relations:
            return result

        # Collect text value IDs
        text_ids = [
            r.get("object_text_id") for r in relations if r.get("object_text_id")
        ]

        if not text_ids:
            return result

        # Fetch text values
        text_values = {
            tv["_id"]: tv.get("text", "")
            for tv in TextValuesRepository.find({"_id": {"$in": text_ids}})
            if tv
        }

        # Map back to predicates
        for relation in relations:
            predicate = relation.get("predicate")
            text_id = relation.get("object_text_id")
            if predicate and text_id and text_id in text_values:
                text = text_values[text_id]
                if text and text.strip():
                    result[predicate].append(text.strip())

    except Exception as e:
        logger.warning(f"Error fetching text relations for {concept_id}: {e}")

    return result


def _get_type_instances_sample(type_id: str, limit: int = 10) -> List[Tuple[str, str]]:
    """Get a sample of instances for a type.

    Args:
        type_id: The type concept ID
        limit: Maximum number of instances to return

    Returns:
        List of (concept_id, display_name) tuples
    """
    instances: List[Tuple[str, str]] = []

    try:
        cursor = ConceptsRepository.find(
            {"relationships.is_an_instance_of": type_id},
            projection={"concept_id": 1, "names": 1, "name": 1},
            limit=limit,
        )

        for doc in cursor:
            if doc:
                cid = doc.get("concept_id", "")
                name = get_concept_display_name_with_names_fallback(doc)
                instances.append((cid, name))

    except Exception as e:
        logger.warning(f"Error fetching instances for type {type_id}: {e}")

    return instances


def _get_subtypes(type_id: str, limit: int = 10) -> List[Tuple[str, str]]:
    """Get subtypes of a type.

    Args:
        type_id: The type concept ID
        limit: Maximum number of subtypes to return

    Returns:
        List of (concept_id, display_name) tuples
    """
    subtypes: List[Tuple[str, str]] = []

    try:
        cursor = ConceptsRepository.find(
            {"relationships.is_a_type_of": type_id},
            projection={"concept_id": 1, "names": 1, "name": 1},
            limit=limit,
        )

        for doc in cursor:
            if doc:
                cid = doc.get("concept_id", "")
                name = get_concept_display_name_with_names_fallback(doc)
                subtypes.append((cid, name))

    except Exception as e:
        logger.warning(f"Error fetching subtypes for {type_id}: {e}")

    return subtypes


def _get_instance_of_types(concept_doc: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Get the types this concept is an instance of.

    Args:
        concept_doc: The concept document

    Returns:
        List of (concept_id, display_name) tuples
    """
    types: List[Tuple[str, str]] = []

    try:
        relationships = concept_doc.get("relationships", {})
        instance_of = relationships.get("is_an_instance_of", [])

        if isinstance(instance_of, str):
            instance_of = [instance_of]
        elif not isinstance(instance_of, list):
            instance_of = []

        for type_id in instance_of:
            if not isinstance(type_id, str) or not type_id.strip():
                continue

            type_doc = ConceptsRepository.find_one(
                {"concept_id": type_id},
                projection={"concept_id": 1, "names": 1, "name": 1},
            )

            if type_doc:
                name = get_concept_display_name_with_names_fallback(type_doc)
                types.append((type_id, name))
            else:
                # Type not found, just use the ID
                types.append((type_id, type_id))

    except Exception as e:
        logger.warning(f"Error fetching instance_of types: {e}")

    return types


def _get_individual_relations_sample(
    concept_id: str, limit: int = 10
) -> List[Dict[str, str]]:
    """Get a sample of relations involving an individual concept.

    Searches text_relations where this concept is the subject.

    Args:
        concept_id: The concept ID
        limit: Maximum number of relations to return

    Returns:
        List of dicts with predicate and object_text
    """
    relations: List[Dict[str, str]] = []

    try:
        # Get text relations where this concept is the subject
        cursor = TextRelationsRepository.find(
            {"subject_concept_id": concept_id},
            limit=limit,
        )

        for rel in cursor:
            if not rel:
                continue

            predicate = rel.get("predicate", "")
            text_id = rel.get("object_text_id")

            if text_id:
                text_doc = TextValuesRepository.find_one({"_id": text_id})
                if text_doc:
                    text = text_doc.get("text", "")
                    if text and predicate:
                        relations.append(
                            {
                                "predicate": predicate,
                                "object_text": text[:200],  # Truncate for embedding
                            }
                        )

    except Exception as e:
        logger.warning(f"Error fetching relations for {concept_id}: {e}")

    return relations


def _get_predicate_extent_sample(
    predicate_id: str, limit: int = 10
) -> List[Dict[str, str]]:
    """Get a sample of usages (extent) for a predicate.

    Args:
        predicate_id: The predicate concept ID
        limit: Maximum number of usages to return

    Returns:
        List of dicts with subject_name, predicate, object_text
    """
    extent: List[Dict[str, str]] = []

    try:
        # Get text relations using this predicate
        cursor = TextRelationsRepository.find(
            {"predicate": predicate_id},
            limit=limit,
        )

        for rel in cursor:
            if not rel:
                continue

            subject_id = rel.get("subject_concept_id", "")
            text_id = rel.get("object_text_id")

            # Get subject name
            subject_name = subject_id
            if subject_id:
                subject_doc = ConceptsRepository.find_one(
                    {"concept_id": subject_id},
                    projection={"concept_id": 1, "names": 1, "name": 1},
                )
                if subject_doc:
                    subject_name = get_concept_display_name_with_names_fallback(
                        subject_doc
                    )

            # Get text value
            object_text = ""
            if text_id:
                text_doc = TextValuesRepository.find_one({"_id": text_id})
                if text_doc:
                    object_text = text_doc.get("text", "")[:200]  # Truncate

            if subject_name or object_text:
                extent.append(
                    {
                        "subject": subject_name,
                        "predicate": predicate_id,
                        "object": object_text,
                    }
                )

    except Exception as e:
        logger.warning(f"Error fetching extent for predicate {predicate_id}: {e}")

    return extent


def build_concept_searchable_text(concept_doc: Dict[str, Any]) -> str:
    """Build aggregated searchable text for a concept based on its kind.

    Args:
        concept_doc: The concept document from MongoDB

    Returns:
        Aggregated text suitable for embedding
    """
    parts: List[str] = []
    concept_id = concept_doc.get("concept_id", "")
    kind = determine_concept_kind(concept_doc)

    # === Common for all kinds ===

    # All names
    names = _get_all_names(concept_doc)
    if names:
        parts.append(f"Names: {', '.join(names)}")

    # concept_id (useful for exact matches)
    if concept_id:
        # Normalise for readability
        readable_id = concept_id.replace("#V#", "").replace("_", " ")
        parts.append(f"ID: {readable_id}")

    # Description (from legacy fields first, then text relations)
    description = get_concept_description(concept_doc)
    if description:
        parts.append(f"Description: {description}")

    # Notes
    notes = get_concept_notes(concept_doc)
    if notes:
        parts.append(f"Notes: {notes}")

    # Text relations (hasDescription, hasNote, hasContent)
    if concept_id:
        text_rels = _get_text_relation_values(
            concept_id, ["hasDescription", "hasNote", "hasContent"]
        )
        for predicate, texts in text_rels.items():
            for text in texts:
                if predicate == "hasDescription" and text != description:
                    # Avoid duplicating if already got from legacy field
                    parts.append(f"Description: {text}")
                elif predicate == "hasNote" and text != notes:
                    parts.append(f"Note: {text}")
                elif predicate == "hasContent":
                    # Truncate long content
                    truncated = text[:500] if len(text) > 500 else text
                    parts.append(f"Content: {truncated}")

    # === Kind-specific aggregation ===

    if kind == "predicate":
        # Predicate: include extent sample
        extent = _get_predicate_extent_sample(concept_id, limit=10)
        if extent:
            usage_lines = []
            for e in extent:
                usage_lines.append(f"  {e['subject']} → {e['object']}")
            if usage_lines:
                parts.append("Used in relations:\n" + "\n".join(usage_lines))

    elif kind == "type":
        # Type: include instances and subtypes
        instances = _get_type_instances_sample(concept_id, limit=10)
        if instances:
            instance_names = [name for _, name in instances]
            parts.append(f"Instances: {', '.join(instance_names)}")

        subtypes = _get_subtypes(concept_id, limit=10)
        if subtypes:
            subtype_names = [name for _, name in subtypes]
            parts.append(f"Subtypes: {', '.join(subtype_names)}")

    elif kind == "individual":
        # Individual: include type chain and key relations
        types = _get_instance_of_types(concept_doc)
        if types:
            type_names = [name for _, name in types]
            parts.append(f"Instance of: {', '.join(type_names)}")

        relations = _get_individual_relations_sample(concept_id, limit=10)
        if relations:
            rel_lines = []
            for r in relations:
                rel_lines.append(f"  {r['predicate']}: {r['object_text']}")
            if rel_lines:
                parts.append("Relations:\n" + "\n".join(rel_lines))

    return "\n".join(filter(None, parts))


def build_concept_embedding_metadata(concept_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Build metadata for a concept embedding.

    This metadata is stored alongside the embedding and enables
    filtering at query time.

    Args:
        concept_doc: The concept document from MongoDB

    Returns:
        Metadata dict for the embedding
    """
    concept_id = concept_doc.get("concept_id", "")
    kind = determine_concept_kind(concept_doc)
    relationships = concept_doc.get("relationships", {})

    # Extract parent types
    parent_types = relationships.get("is_a_type_of", [])
    if isinstance(parent_types, str):
        parent_types = [parent_types]
    elif not isinstance(parent_types, list):
        parent_types = []

    # Extract instance_of types
    instance_of = relationships.get("is_an_instance_of", [])
    if isinstance(instance_of, str):
        instance_of = [instance_of]
    elif not isinstance(instance_of, list):
        instance_of = []

    # Extract system tags (may contain org info)
    system_tags = concept_doc.get("system_tags", [])
    if not isinstance(system_tags, list):
        system_tags = []

    # Get updated_at timestamp
    updated_at = concept_doc.get("updated_at")
    if isinstance(updated_at, datetime):
        updated_at_iso = updated_at.isoformat()
    elif isinstance(updated_at, str):
        updated_at_iso = updated_at
    else:
        updated_at_iso = datetime.now(timezone.utc).isoformat()

    return {
        "type": "concept",
        "concept_id": concept_id,
        "guid": concept_doc.get("guid", ""),
        "kind": kind,
        "parent_types": parent_types,
        "instance_of": instance_of,
        "system_tags": system_tags,
        "updated_at": updated_at_iso,
    }


def build_concept_document_id(concept_id: str) -> str:
    """Build the document ID for a concept embedding.

    Args:
        concept_id: The concept ID

    Returns:
        Document ID for use in LlamaIndex
    """
    return f"concept:{concept_id}"


def mark_concept_embedding_stale(concept_id: str) -> bool:
    """Mark a concept's embedding as stale (needs reindexing).

    Args:
        concept_id: The concept ID to mark

    Returns:
        True if successfully marked, False otherwise
    """
    try:
        result = ConceptsRepository.update_one(
            {"concept_id": concept_id},
            {"$set": {"embedding_status": EMBEDDING_STATUS_STALE}},
        )
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"Error marking concept {concept_id} as stale: {e}")
        return False


def _embedding_queue_query() -> Dict[str, Any]:
    """Return the index-targetable materialised embedding queue predicate.

    A non-sparse ``embedding_status`` index represents missing fields as null,
    so including ``None`` keeps legacy never-indexed concepts eligible without
    an unindexed ``$exists`` or field-to-field ``$expr`` branch.
    """

    return {
        "embedding_status": {
            "$in": [None, *EMBEDDING_QUEUE_STATUSES],
        }
    }


def get_concepts_needing_indexing(
    batch_size: int = 50,
) -> List[Dict[str, Any]]:
    """Get concepts that need (re)indexing.

    Returns concepts whose materialised embedding status is missing, pending,
    stale, or failed.

    Args:
        batch_size: Maximum number of concepts to return

    Returns:
        List of concept documents needing indexing
    """
    try:
        cursor = ConceptsRepository.find(
            _embedding_queue_query(),
            sort=[("updated_at", -1)],  # Most recently updated first
            limit=batch_size,
        )

        return list(cursor)

    except Exception as e:
        logger.error(f"Error fetching concepts needing indexing: {e}")
        return []


def count_concepts_needing_indexing() -> int:
    """Count concepts that need (re)indexing.

    Returns:
        Count of concepts needing indexing
    """
    try:
        return ConceptsRepository.count_documents(_embedding_queue_query())

    except Exception as e:
        logger.error(f"Error counting concepts needing indexing: {e}")
        return 0


def update_concept_embedding_status(
    concept_id: str,
    status: str,
    error: Optional[str] = None,
) -> bool:
    """Update the embedding status for a concept.

    Args:
        concept_id: The concept ID
        status: New status (one of EMBEDDING_STATUS_* constants)
        error: Optional error message (for failed status)

    Returns:
        True if successfully updated, False otherwise
    """
    try:
        update: Dict[str, Any] = {
            "$set": {
                "embedding_status": status,
            }
        }

        if status == EMBEDDING_STATUS_INDEXED:
            update["$set"]["embedding_updated_at"] = datetime.now(timezone.utc)
            update["$unset"] = {"embedding_error": ""}
        elif status == EMBEDDING_STATUS_FAILED and error:
            update["$set"]["embedding_error"] = error

        result = ConceptsRepository.update_one(
            {"concept_id": concept_id},
            update,
        )

        return result.modified_count > 0 or result.matched_count > 0

    except Exception as e:
        logger.error(f"Error updating embedding status for {concept_id}: {e}")
        return False


def get_concept_embedding_status(concept_id: str) -> Optional[Dict[str, Any]]:
    """Get the embedding status for a concept.

    Args:
        concept_id: The concept ID

    Returns:
        Dict with embedding_status, embedding_updated_at, embedding_error, updated_at
        or None if concept not found
    """
    try:
        doc = ConceptsRepository.find_one(
            {"concept_id": concept_id},
            projection={
                "concept_id": 1,
                "embedding_status": 1,
                "embedding_updated_at": 1,
                "embedding_error": 1,
                "updated_at": 1,
            },
        )

        if not doc:
            return None

        # Format timestamps
        embedding_updated_at = doc.get("embedding_updated_at")
        if isinstance(embedding_updated_at, datetime):
            embedding_updated_at = embedding_updated_at.isoformat()

        updated_at = doc.get("updated_at")
        if isinstance(updated_at, datetime):
            updated_at = updated_at.isoformat()

        return {
            "concept_id": doc.get("concept_id"),
            "embedding_status": doc.get("embedding_status", EMBEDDING_STATUS_PENDING),
            "embedding_updated_at": embedding_updated_at,
            "embedding_error": doc.get("embedding_error"),
            "updated_at": updated_at,
        }

    except Exception as e:
        logger.error(f"Error getting embedding status for {concept_id}: {e}")
        return None


def get_concept_embedding_stats() -> Dict[str, Any]:
    """Get aggregate statistics about concept embedding status.

    Returns:
        Dict with counts by status, oldest pending/stale timestamp, etc.
    """
    try:
        # Count by status using aggregation
        pipeline = [
            {
                "$group": {
                    "_id": "$embedding_status",
                    "count": {"$sum": 1},
                    "oldest_updated_at": {"$min": "$updated_at"},
                }
            }
        ]

        status_counts: Dict[str, int] = {
            EMBEDDING_STATUS_PENDING: 0,
            EMBEDDING_STATUS_INDEXED: 0,
            EMBEDDING_STATUS_STALE: 0,
            EMBEDDING_STATUS_FAILED: 0,
            "missing": 0,  # No embedding_status field
        }
        oldest_update_timestamps: Dict[str, str] = {}

        results = ConceptsRepository.aggregate(pipeline)
        for doc in results:
            status = doc.get("_id")
            count = doc.get("count", 0)
            oldest = doc.get("oldest_updated_at")

            if status is None:
                status_counts["missing"] = count
            elif status in status_counts:
                status_counts[status] = count

            if oldest and status:
                if isinstance(oldest, datetime):
                    oldest_update_timestamps[status or "missing"] = oldest.isoformat()

        # Total concepts
        total = sum(status_counts.values())

        # Concepts needing indexing (pending + stale + missing)
        needing_indexing = (
            status_counts[EMBEDDING_STATUS_PENDING]
            + status_counts[EMBEDDING_STATUS_STALE]
            + status_counts["missing"]
        )

        return {
            "total_concepts": total,
            "status_counts": status_counts,
            "needing_indexing": needing_indexing,
            "oldest_update_by_status": oldest_update_timestamps,
            "namespace": CONCEPT_EMBEDDING_NAMESPACE,
        }

    except Exception as e:
        logger.error(f"Error getting embedding stats: {e}")
        return {
            "error": str(e),
            "total_concepts": 0,
            "status_counts": {},
            "needing_indexing": 0,
        }
