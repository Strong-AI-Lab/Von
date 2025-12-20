"""
Routes for predicate-specific operations.

This module provides endpoints for querying predicate extents and metadata,
following a knowledge-driven approach where predicate properties are stored
as facts in the Vontology itself rather than in hard-coded schemas.
"""

from flask import Blueprint, jsonify, request, current_app
from typing import Dict, List, Any, Optional
from bson import ObjectId

from ...db.mongo_client import get_text_relations_collection, get_concepts_collection
from ...db.repositories.text_value_repository import TextValuesRepository


predicate_bp = Blueprint("predicates", __name__, url_prefix="/api/predicates")


@predicate_bp.route("/<concept_id>/extent", methods=["GET"])
def get_predicate_extent(concept_id: str):
    """
    Get the extent (all uses/instances) of a predicate.

    The extent includes all triples where this concept appears as the predicate,
    combining both text_relations and structured relations from concept documents.

    Query parameters:
    - limit (int): Maximum number of results (default: 100, max: 1000)
    - offset (int): Number of results to skip for pagination (default: 0)
    - sort_by (str): Field to sort by - 'subject', 'created_at', etc. (default: 'created_at')
    - sort_order (str): 'asc' or 'desc' (default: 'desc')
    - subject_type (str): Filter by subject type (concept_id)
    - object_type (str): Filter by object type (concept_id)
    - source (str): Filter by source - 'text_relations', 'structured', or 'all' (default: 'all')

    Returns:
    {
        "concept_id": str,
        "extent": [{
            "subject": str,
            "subject_name": str (optional),
            "predicate": str,
            "object": str or [str],  # Single value or array for n-ary
            "object_name": str (optional),
            "source": "text_relations" | "structured",
            "created_at": datetime (optional),
            "updated_at": datetime (optional)
        }],
        "total_count": int,
        "limit": int,
        "offset": int,
        "has_more": bool
    }
    """
    try:
        # Parse query parameters
        limit = min(request.args.get("limit", type=int, default=100), 1000)
        offset = request.args.get("offset", type=int, default=0)
        sort_by = request.args.get("sort_by", default="created_at")
        sort_order = request.args.get("sort_order", default="desc")
        subject_type = request.args.get("subject_type")
        object_type = request.args.get("object_type")
        source_filter = request.args.get("source", default="all")

        extent_items = []
        total_count = 0

        # Query text_relations collection if requested
        if source_filter in ("text_relations", "all"):
            text_rel_items, text_rel_count = _query_text_relations_extent(
                concept_id,
                subject_type,
                object_type,
                limit,
                offset,
                sort_by,
                sort_order,
            )
            extent_items.extend(text_rel_items)
            total_count += text_rel_count

        # Query structured relations from concepts collection if requested
        if source_filter in ("structured", "all"):
            struct_rel_items, struct_rel_count = _query_structured_relations_extent(
                concept_id,
                subject_type,
                object_type,
                limit,
                offset,
                sort_by,
                sort_order,
            )
            extent_items.extend(struct_rel_items)
            total_count += struct_rel_count

        # Sort combined results if querying both sources
        if source_filter == "all" and extent_items:
            extent_items = _sort_extent_items(extent_items, sort_by, sort_order)

        # Apply pagination to combined results
        paginated_items = extent_items[offset : offset + limit]

        return (
            jsonify(
                {
                    "concept_id": concept_id,
                    "extent": paginated_items,
                    "total_count": total_count,
                    "limit": limit,
                    "offset": offset,
                    "has_more": offset + len(paginated_items) < total_count,
                }
            ),
            200,
        )

    except Exception as e:
        current_app.logger.error(
            f"Failed to get extent for predicate {concept_id}: {e}", exc_info=True
        )
        return jsonify({"error": f"Failed to get predicate extent: {str(e)}"}), 500


@predicate_bp.route("/<concept_id>/metadata", methods=["GET"])
def get_predicate_metadata(concept_id: str):
    """
    Get metadata about a predicate from the knowledge base.

    This endpoint queries for meta-predicates about the given predicate,
    such as arity, domain/range constraints, and documentation.
    All metadata is stored as facts in Von itself.

    Returns:
    {
        "concept_id": str,
        "metadata": {
            "arity": int (optional),
            "domain_constraints": [{
                "arg_position": int,
                "type_constraint": str  # concept_id
            }] (optional),
            "documentation": str (optional),
            "custom_properties": [{
                "predicate": str,
                "value": Any
            }]
        },
        "extent_statistics": {
            "total_uses": int,
            "unique_subjects": int,
            "unique_objects": int
        }
    }
    """
    try:
        concepts_coll = get_concepts_collection()
        if concepts_coll is None:
            return jsonify({"error": "Concepts collection not available"}), 500

        # Query for meta-predicates about this predicate
        metadata: Dict[str, Any] = {"custom_properties": []}

        # Look for arity: (#V#predicate_arity, concept_id, N)
        arity_fact = _find_meta_fact("#V#predicate_arity", concept_id, concepts_coll)
        if arity_fact:
            metadata["arity"] = arity_fact.get("object")

        # Look for domain constraints: (#V#arg_num_is_instance, concept_id, arg_num, type)
        domain_constraints = _find_domain_constraints(concept_id, concepts_coll)
        if domain_constraints:
            metadata["domain_constraints"] = domain_constraints

        # Look for documentation: (concept_id, #V#hasDescription, "...")
        doc_relations = _find_text_relations_for_predicate(
            concept_id, "#V#hasDescription"
        )
        if doc_relations:
            metadata["documentation"] = doc_relations[0].get("text")

        # Find any other custom meta-predicates where this concept is the subject
        custom_props = _find_custom_meta_properties(concept_id, concepts_coll)
        metadata["custom_properties"] = custom_props

        # Calculate extent statistics
        extent_stats = _calculate_extent_statistics(concept_id)

        return (
            jsonify(
                {
                    "concept_id": concept_id,
                    "metadata": metadata,
                    "extent_statistics": extent_stats,
                }
            ),
            200,
        )

    except Exception as e:
        current_app.logger.error(
            f"Failed to get metadata for predicate {concept_id}: {e}", exc_info=True
        )
        return jsonify({"error": f"Failed to get predicate metadata: {str(e)}"}), 500


# --- Helper functions ---


def _query_text_relations_extent(
    predicate_concept_id: str,
    subject_type: Optional[str],
    object_type: Optional[str],
    limit: int,
    offset: int,
    sort_by: str,
    sort_order: str,
) -> tuple[List[Dict[str, Any]], int]:
    """Query text_relations collection for predicate extent."""
    try:
        text_rel_coll = get_text_relations_collection()
        if text_rel_coll is None:
            return [], 0

        # Build query filter
        query_filter: Dict[str, Any] = {"predicate": predicate_concept_id}

        # Apply subject type filter if specified
        if subject_type:
            query_filter["subject_concept_id"] = {"$regex": f"^{subject_type}"}

        # Count total matches
        total_count = text_rel_coll.count_documents(query_filter)

        # Build sort criteria
        sort_direction = 1 if sort_order == "asc" else -1
        sort_criteria = [(sort_by, sort_direction)]

        # Query with pagination
        cursor = (
            text_rel_coll.find(query_filter)
            .sort(sort_criteria)
            .skip(offset)
            .limit(limit)
        )

        extent_items = []
        for rel in cursor:
            # Fetch the text value
            text_value = TextValuesRepository.find_one(
                {"_id": ObjectId(rel.get("object_text_id"))}
            )

            # Fetch subject concept for name
            subject_name = _get_concept_name(rel.get("subject_concept_id"))

            extent_items.append(
                {
                    "subject": rel.get("subject_concept_id"),
                    "subject_name": subject_name,
                    "predicate": predicate_concept_id,
                    "object": text_value.get("text") if text_value else None,
                    "object_language": text_value.get("lang") if text_value else None,
                    "source": "text_relations",
                    "created_at": rel.get("created_at"),
                    "updated_at": rel.get("updated_at"),
                }
            )

        return extent_items, total_count

    except Exception as e:
        current_app.logger.error(
            f"Error querying text_relations extent: {e}", exc_info=True
        )
        return [], 0


def _query_structured_relations_extent(
    predicate_concept_id: str,
    subject_type: Optional[str],
    object_type: Optional[str],
    limit: int,
    offset: int,
    sort_by: str,
    sort_order: str,
) -> tuple[List[Dict[str, Any]], int]:
    """Query concepts collection for structured relations extent."""
    try:
        concepts_coll = get_concepts_collection()
        if concepts_coll is None:
            return [], 0

        # Build query to find concepts with this predicate in their relationships
        # Note: This queries for the predicate as a key in the relationships object
        query_filter: Dict[str, Any] = {
            f"relationships.{predicate_concept_id}": {"$exists": True, "$ne": None}
        }

        # Apply subject type filter if specified
        if subject_type:
            query_filter["relationships.is_an_instance_of"] = subject_type

        # Count total matches
        total_count = concepts_coll.count_documents(query_filter)

        # Query with pagination
        cursor = concepts_coll.find(query_filter).skip(offset).limit(limit)

        extent_items = []
        for concept in cursor:
            subject_id = concept.get("concept_id")
            subject_name = concept.get("name") or subject_id

            # Get the object(s) for this predicate
            relationships = concept.get("relationships", {})
            object_values = relationships.get(predicate_concept_id)

            # Handle both single values and arrays
            if not isinstance(object_values, list):
                object_values = [object_values] if object_values else []

            # Create extent items (one per object if multiple)
            for obj_value in object_values:
                obj_name = (
                    _get_concept_name(obj_value) if isinstance(obj_value, str) else None
                )

                extent_items.append(
                    {
                        "subject": subject_id,
                        "subject_name": subject_name,
                        "predicate": predicate_concept_id,
                        "object": obj_value,
                        "object_name": obj_name,
                        "source": "structured",
                        "updated_at": concept.get("updated_at"),
                    }
                )

        return extent_items, total_count

    except Exception as e:
        current_app.logger.error(
            f"Error querying structured relations extent: {e}", exc_info=True
        )
        return [], 0


def _sort_extent_items(
    items: List[Dict[str, Any]], sort_by: str, sort_order: str
) -> List[Dict[str, Any]]:
    """Sort combined extent items."""
    reverse = sort_order == "desc"

    # Handle None values in sorting
    def sort_key(item):
        value = item.get(sort_by)
        if value is None:
            return "" if isinstance(value, str) else 0
        return value

    return sorted(items, key=sort_key, reverse=reverse)


def _get_concept_name(concept_id: str) -> Optional[str]:
    """Get the name of a concept by its ID."""
    try:
        concepts_coll = get_concepts_collection()
        if concepts_coll is None:
            return None

        concept = concepts_coll.find_one({"concept_id": concept_id}, {"name": 1})
        return concept.get("name") if concept else None

    except Exception:
        return None


def _find_meta_fact(
    meta_predicate: str, subject: str, concepts_coll
) -> Optional[Dict[str, Any]]:
    """Find a meta-fact about a predicate."""
    try:
        # Look for the fact in relationships
        concept = concepts_coll.find_one({"concept_id": subject})
        if concept:
            relationships = concept.get("relationships", {})
            if meta_predicate in relationships:
                return {"object": relationships[meta_predicate]}
        return None
    except Exception:
        return None


def _find_domain_constraints(predicate_id: str, concepts_coll) -> List[Dict[str, Any]]:
    """Find domain/range constraints for a predicate."""
    # TODO: Implement querying for (#V#arg_num_is_instance, predicate_id, arg_num, type) facts
    # This requires a more complex query structure that may need to be stored differently
    return []


def _find_text_relations_for_predicate(
    subject_id: str, predicate: str
) -> List[Dict[str, Any]]:
    """Find text relations where the subject is the predicate concept."""
    try:
        text_rel_coll = get_text_relations_collection()
        if text_rel_coll is None:
            return []

        relations = list(
            text_rel_coll.find(
                {"subject_concept_id": subject_id, "predicate": predicate}
            )
        )

        # Resolve text values
        result = []
        for rel in relations:
            text_value = TextValuesRepository.find_one(
                {"_id": ObjectId(rel.get("object_text_id"))}
            )
            if text_value:
                result.append(
                    {
                        "text": text_value.get("text"),
                        "lang": text_value.get("lang"),
                        "predicate": predicate,
                    }
                )

        return result

    except Exception:
        return []


def _find_custom_meta_properties(
    predicate_id: str, concepts_coll
) -> List[Dict[str, Any]]:
    """Find all custom meta-properties for a predicate."""
    try:
        concept = concepts_coll.find_one({"concept_id": predicate_id})
        if not concept:
            return []

        relationships = concept.get("relationships", {})
        custom_props = []

        # Exclude known structural relationships
        excluded_preds = {
            "is_a_type_of",
            "is_an_instance_of",
            "most_salient_type",
            "#V#salient_binary_predicate_for_type",
        }

        for pred_key, pred_value in relationships.items():
            if pred_key not in excluded_preds:
                custom_props.append({"predicate": pred_key, "value": pred_value})

        return custom_props

    except Exception:
        return []


def _calculate_extent_statistics(predicate_id: str) -> Dict[str, int]:
    """Calculate statistics about predicate extent."""
    try:
        text_rel_coll = get_text_relations_collection()
        concepts_coll = get_concepts_collection()

        stats = {"total_uses": 0, "unique_subjects": 0, "unique_objects": 0}

        # Count from text_relations
        if text_rel_coll is not None:
            text_count = text_rel_coll.count_documents({"predicate": predicate_id})
            stats["total_uses"] += text_count

            # Get unique subjects
            unique_subjects = text_rel_coll.distinct(
                "subject_concept_id", {"predicate": predicate_id}
            )
            stats["unique_subjects"] = len(unique_subjects)

        # Count from structured relations
        if concepts_coll is not None:
            struct_count = concepts_coll.count_documents(
                {f"relationships.{predicate_id}": {"$exists": True, "$ne": None}}
            )
            stats["total_uses"] += struct_count

        return stats

    except Exception as e:
        current_app.logger.error(
            f"Error calculating extent statistics: {e}", exc_info=True
        )
        return {"total_uses": 0, "unique_subjects": 0, "unique_objects": 0}
