# REFACTORING_NOTE: This file is being updated to align the interaction logging mechanism
# with the new generalized entity model. The goal is to ensure that interactions with any
# type of entity (not just 'people') are logged with sufficient detail to understand
# what was interacted with (type or individual) and its place in the Vontology.

import logging
from flask import request
from datetime import datetime, timezone # Added timezone
from typing import Optional, Dict, Any, List

from ..db.mongo_client import get_interaction_log_collection, INTERACTION_LOG_COLLECTION_NAME


logger = logging.getLogger(__name__)

def log_knowledge_interaction(
    interaction_type: str,
    target_entity_id: str, # For individuals: their _id from 'entities'. For types: their vontology_path string.
    target_entity_kind: str, # "type" or "individual"
    target_entity_vontology_path: List[str], # e.g., ["Concept", "Person"]
    target_entity_name: Optional[str] = None, # Denormalized name for display
    context_before: Optional[Dict[str, Any]] = None,
    interaction_payload: Optional[Dict[str, Any]] = None,
    context_after: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    user_identifier_override: Optional[str] = None
):
    # REFACTORING_NOTE: This function is updated to log interactions with generalized entities.
    # Key changes:
    # - `target_entity_id` now handles both individual IDs and type paths.
    # - `target_entity_kind` ("type" or "individual") is a new required field.
    # - `target_entity_vontology_path` (List[str]) is a new required field.
    # - The old `target_entity_type` field is removed.
    # - Uses datetime.now(timezone.utc) instead of datetime.utcnow().

    try:
        log_collection = get_interaction_log_collection()
        if log_collection is None:
            logger.error(
                "Could not get %s. Interaction of type '%s' for entity '%s' not logged.",
                INTERACTION_LOG_COLLECTION_NAME,
                interaction_type,
                target_entity_id,
            )
            return

        user_identifier = user_identifier_override if user_identifier_override else request.headers.get('X-User-Client-ID', 'unknown_client')

        log_entry = {
            "timestamp": datetime.now(timezone.utc),
            "user_identifier": user_identifier,
            "interaction_type": interaction_type,
            "target_entity_id": str(target_entity_id),
            "target_entity_kind": target_entity_kind,
            "target_entity_vontology_path": target_entity_vontology_path,
            "target_entity_name": target_entity_name,
            "context_before_interaction": context_before,
            "interaction_payload": interaction_payload,
            "context_after_interaction": context_after,
            "session_id": session_id
        }
        log_collection.insert_one(log_entry)
    except Exception as e:  # pragma: no cover
        logger.exception(
            "Error logging knowledge interaction (type: %s, entity: %s): %s",
            interaction_type,
            target_entity_id,
            e,
        )
        # Optionally, implement more robust error handling here (e.g., retry, log to file)
        # For now, we don't want logging failure to break the main operation.
