import logging
from typing import Dict, Any, List, Optional
from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import TextValuesRepository
from ..services.concept_service import get_concept_by_id
from ..vontology.utils_vontology import simulate_or_delete_concept

logger = logging.getLogger(__name__)

PROTECTED_CONCEPTS = {'#V#Thing', '#V#Root', '#V#System'}

def merge_concepts(source_id: str, target_id: str, simulate: bool = True) -> Dict[str, Any]:
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
        "errors": []
    }

    if source_id in PROTECTED_CONCEPTS:
        report["errors"].append(f"Source concept {source_id} is protected and cannot be merged.")
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

    # 1. Analyze Relationships
    source_rels = source_doc.get("relationships", {})
    ops_rels = []

    # Incoming relationships (other concepts pointing to source)
    # We need to find all concepts that point to source_id and update them to point to target_id
    # This is expensive to query perfectly without a reverse index, but we can check standard fields
    # For now, we'll rely on the fact that we can query by value in Mongo

    # Find concepts where source is a parent
    children = ConceptsRepository.find({"relationships.is_a_type_of": source_id})
    for child in children:
        ops_rels.append({
            "type": "reparent_child",
            "concept_id": child["concept_id"],
            "detail": f"Change parent from {source_id} to {target_id}"
        })

    # Find concepts where source is a type (instance of)
    instances = ConceptsRepository.find({"relationships.is_an_instance_of": source_id})
    for inst in instances:
        ops_rels.append({
            "type": "retype_instance",
            "concept_id": inst["concept_id"],
            "detail": f"Change type from {source_id} to {target_id}"
        })

    # Outgoing relationships (source pointing to others)
    # We merge these into target. If target already has them, we skip.
    for rel_type, targets in source_rels.items():
        if isinstance(targets, list):
            for t in targets:
                ops_rels.append({
                    "type": "move_outgoing_relation",
                    "predicate": rel_type,
                    "target": t,
                    "detail": f"Add {rel_type} -> {t} to target"
                })
        elif isinstance(targets, str):
             ops_rels.append({
                "type": "move_outgoing_relation",
                "predicate": rel_type,
                "target": targets,
                "detail": f"Set {rel_type} -> {targets} on target"
            })

    report["operations"].extend(ops_rels)

    # 2. Analyze Names
    source_names = source_doc.get("names", [])
    ops_names = []
    target_names = target_doc.get("names", [])
    existing_names = {n.get("name") for n in target_names if isinstance(n, dict)}

    for name_entry in source_names:
        if isinstance(name_entry, dict):
            name_val = name_entry.get("name")
            if name_val and name_val not in existing_names:
                ops_names.append({
                    "type": "move_name",
                    "name": name_val,
                    "detail": f"Add alias '{name_val}' to target"
                })

    # Also check the top-level name
    source_top_name = source_doc.get("name")
    if source_top_name and source_top_name not in existing_names and source_top_name != target_doc.get("name"):
         ops_names.append({
            "type": "move_name",
            "name": source_top_name,
            "detail": f"Add source name '{source_top_name}' as alias to target"
        })

    report["operations"].extend(ops_names)

    # 3. Analyze Text Values
    # We need to find text values linked to source_id
    text_values = TextValuesRepository.find({"concept_id": source_id})
    ops_text = []
    for tv in text_values:
        ops_text.append({
            "type": "move_text_value",
            "id": str(tv.get("_id")),
            "predicate": tv.get("predicate"),
            "text": tv.get("text")[:30] + "...",
            "detail": f"Reassign text value '{tv.get('predicate')}' to target"
        })

    report["operations"].extend(ops_text)

    # 4. Deletion
    report["operations"].append({
        "type": "delete_source",
        "concept_id": source_id,
        "detail": "Delete the source concept after migration"
    })

    if simulate:
        report["success"] = True
        return report

    # EXECUTION
    try:
        # 1. Update incoming references
        # Reparent children
        ConceptsRepository.update_many(
            {"relationships.is_a_type_of": source_id},
            {"$set": {"relationships.is_a_type_of.$": target_id}} # This replaces the specific array element
        )
        # Retype instances
        ConceptsRepository.update_many(
            {"relationships.is_an_instance_of": source_id},
            {"$set": {"relationships.is_an_instance_of.$": target_id}}
        )

        # 2. Merge outgoing relationships
        # This is complex to do atomically without full document replacement.
        # We'll fetch target again, update in memory, and save.
        target_doc = get_concept_by_id(target_id) # Refresh
        target_rels = target_doc.get("relationships", {})

        for rel_type, targets in source_rels.items():
            if rel_type not in target_rels:
                target_rels[rel_type] = targets
            else:
                # Merge lists
                if isinstance(target_rels[rel_type], list):
                    current_list = set(target_rels[rel_type])
                    new_items = targets if isinstance(targets, list) else [targets]
                    for item in new_items:
                        current_list.add(item)
                    target_rels[rel_type] = list(current_list)
                # Overwrite scalars (or convert to list? For now, keep scalar if scalar)
                # If both are scalar and different, we might have a conflict.
                # Strategy: Convert to list if conflict.
                elif target_rels[rel_type] != targets:
                     target_rels[rel_type] = [target_rels[rel_type], targets]

        ConceptsRepository.update_one(
            {"concept_id": target_id},
            {"$set": {"relationships": target_rels}}
        )

        # 3. Merge Names
        names_to_add = []
        for op in ops_names:
            # Reconstruct the name entry (simplified)
            names_to_add.append({
                "name": op["name"],
                "type": "NL", # Defaulting to NL for merged names
                "language": "en" # Default
            })

        if names_to_add:
            ConceptsRepository.update_one(
                {"concept_id": target_id},
                {"$push": {"names": {"$each": names_to_add}}}
            )

        # 4. Move Text Values
        TextValuesRepository.update_many(
            {"concept_id": source_id},
            {"$set": {"concept_id": target_id}}
        )

        # 5. Delete Source
        ConceptsRepository.delete_one({"concept_id": source_id})

        report["success"] = True
        report["executed"] = True

    except Exception as e:
        logger.error(f"Error merging concepts {source_id} -> {target_id}: {e}", exc_info=True)
        report["errors"].append(str(e))
        report["success"] = False

    return report
