# TODO: ARCHITECTURAL RENAMING NEEDED (Separate from JVNAUTOSCI-320)
#
# This service should be renamed to better reflect its current purpose:
# - Current: concept_service.py (legacy naming)
# - Proposed: concept_instance_service.py or individual_concept_service.py
#
# Rationale:
# - Already operates on concepts collection (not separate concepts collection)
# - Handles individual concept instances (concept_type="individual")
# - concept tab is now Concept tab in UI
# - "concept" operations are actually concept operations
#
# Related files that need renaming:
# - src/backend/server/routes/concept_routes.py → concept_instance_routes.py
# - src/backend/models/concept_models.py → concept_instance_models.py
# - API endpoints: /api/concepts → /api/concept-instances (with backward compatibility)

# REFACTORING_NOTE: This service handles business logic related to the generalized 'concepts'
# (individual instances of vontology types) and the 'user_concept_tracking' collection.
# It is responsible for CRUD operations on concepts, managing user tracking data (recents, key concepts),
# and other related functionalities.

from pymongo.collection import Collection
from pymongo.results import InsertOneResult, UpdateResult, DeleteResult
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
import logging
import os
import uuid  # Added for GUID generation
import traceback  # Added for error logging
from datetime import datetime, timezone  # Ensure timezone is imported
import requests  # Added for requests.exceptions.ConnectionError
from typing import Dict, Any, Optional, List, Tuple, Iterable  # Added Tuple
from pymongo.errors import PyMongoError  # Added for DB operations

try:
    from ..services.annotation_extraction_service import invalidate_phrase_cache  # type: ignore
except Exception:  # pragma: no cover

    def invalidate_phrase_cache():  # type: ignore
        return


from ..vontology.utils_vontology import (
    VONTOLOGY_NODES_COLLECTION_NAME,
    get_vontology_node_and_descendant_ids,
    get_concept_details_from_db,
    update_vontology_node_in_db,
    get_concept_description,
    get_concept_notes,
    set_concept_description,
    set_concept_notes,
    is_thing_id,
    THING_PRIMARY_ID,
    get_concept_display_name_with_names_fallback,
)  # Added imports for concept field accessors
from pymongo.database import Database  # For type hinting db

from ..db import mongo_client
from ..db.repositories.concepts_repository import ConceptsRepository

# from src.backend.languagemodels.llm_interface import OllamaClient # ADDED: Import OllamaClient
from ..db.mongo_client import get_db  # , get_database_name

# Assuming concept_model.py is in src.backend.models, adjust if necessary
# Correcting the import path for concept_model based on typical project structure
# If it is in src.backend.models, the original path should be fine if PYTHONPATH is set up correctly or it\'s a package.
# For now, assuming the original path was intended to be resolvable.
from ..models.concept_models import (
    ConceptInteraction,
    ConceptModel,
    InteractionEntry,
    interaction_session_collection_name,
)  # Ensure this line is uncommented and correct
from ..security.access_control import (
    get_effective_user_concept_id,
    apply_concept_query_filter,
)
from .text_value_service import upsert_text_for_concept
from ..db.repositories.text_value_repository import TextRelationsRepository

# Setup logger
logger = logging.getLogger(__name__)

# REFACTORING_NOTE: Define a type alias for MongoDB query objects for clarity
MongoQuery = Dict[str, Any]


def _invalidate_concept_mutation_caches() -> None:
    """Invalidate caches that may become stale after concept mutations.

    Keep all mutation-side cache invalidation in one helper so create/update/
    delete paths stay consistent as workflow and ontology caches evolve.
    """

    try:
        invalidate_phrase_cache()
    except Exception:
        pass

    try:
        from .workflow_discovery_service import (
            invalidate_workflow_discovery_executability_caches,
        )

        invalidate_workflow_discovery_executability_caches()
    except Exception:
        pass

    try:
        from ..workflows.workflow_concept_authority_service import (
            clear_workflow_type_resolution_cache,
        )

        clear_workflow_type_resolution_cache()
    except Exception:
        pass

    try:
        from .vontology_concept_stats_service import (
            invalidate_vontology_concept_stats_cache,
        )

        invalidate_vontology_concept_stats_cache(reason="concept_mutation")
    except Exception:
        pass


# REFACTORING_NOTE: Placeholder for potential error/exception classes
class ConceptServiceError(Exception):
    "Base class for errors in conceptService."

    pass


class ConceptNotFoundError(ConceptServiceError):
    "Raised when an concept is not found for a given ID."

    pass


class InvalidConceptDataError(ConceptServiceError):
    "Raised when provided data for an concept is invalid."

    pass


# --- Preserved Fields Write Guard ---
_ALLOWED_PRESERVED_FIELDS = {"notes", "description"}


def enforce_preserved_fields_write(payload: Dict[str, Any]):
    """Scan a $set payload for concept_data.preserved_fields.* writes.

    If any preserved_fields keys are present that are not explicitly handled
    (notes/description) we raise to force developers to route through proper
    text_value / relation pathways.
    """
    try:
        set_ops = payload.get("$set", {}) if isinstance(payload, dict) else {}
        suspicious = []
        for k in list(set_ops.keys()):
            if k.startswith("concept_data.preserved_fields."):
                field = k.split("concept_data.preserved_fields.", 1)[1]
                if field not in _ALLOWED_PRESERVED_FIELDS:
                    suspicious.append(field)
        if suspicious:
            # Log and raise to surface deprecated direct write
            logger.error(
                "Deprecated preserved_fields direct write for fields=%s. Must migrate to relation/text_value storage.",
                suspicious,
            )
            raise InvalidConceptDataError(
                f"Direct write to preserved_fields disallowed for: {', '.join(suspicious)}"
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.exception(f"Error enforcing preserved_fields write policy: {e}")


# REFACTORING_NOTE: Functions for 'concepts' collection will go here.


# ---------- Context helpers ----------
def gather_descriptive_material(
    concept: Dict[str, Any],
    vontology_node: Optional[Dict[str, Any]] = None,
    *,
    include_notes_in_block: bool = False,
) -> Dict[str, Any]:
    """Collect descriptive context for prompting.

    Returns a dict with:
      - description: Prefer the type node's description; fallback to concept's own description
      - notes: Current concept notes (already resolved to preserved_fields where applicable)
      - context_block: A small textual block you can inject into prompts. By default it includes
        only the type description to avoid duplicating notes when templates already have {concept_notes}.
        Pass include_notes_in_block=True to also include a notes line.
    """
    # Notes from the concept (the caller may have pre-merged initial notes into concept)
    notes_val = get_concept_notes(concept) or ""

    # Description: prefer type node when available
    description_val = ""
    try:
        if vontology_node:
            description_val = get_concept_description(vontology_node) or ""
        if not description_val:
            description_val = get_concept_description(concept) or ""
    except Exception as e:
        logger.warning(f"gather_descriptive_material: error reading description: {e}")
        description_val = ""

    # Build context block
    lines: List[str] = []
    if description_val:
        lines.append(f"Type description: {description_val}")
    if include_notes_in_block and notes_val:
        lines.append(f"Concept notes: {notes_val}")
    context_block = "\n".join(lines) + ("\n" if lines else "")

    return {
        "description": description_val,
        "notes": notes_val,
        "context_block": context_block,
    }


def create_concept(
    name: str,  # Display name (will be persisted in names[] canonical field)
    concept_id: Optional[
        str
    ] = None,  # Type concept_id for the instance, or node concept_id for types
    vontology_path: Optional[str] = None,  # Legacy/path context if applicable
    description: Optional[str] = None,
    notes: Optional[str] = None,
    attributes: Optional[Dict[str, Any]] = None,
    system_tags: Optional[List[str]] = None,
    user_tags: Optional[List[str]] = None,
    linked_concepts: Optional[List[Dict[str, Any]]] = None,
    parent_concept_ids: Optional[List[str]] = None,  # For parent relationships
    create_as_instance: bool = True,  # New: determines whether to set instance vs type relationship
    instance_of_type: Optional[
        str
    ] = None,  # JVNAUTOSCI-689: explicit instance_of relationship
) -> Dict[str, Any]:
    """Creates a new concept in the 'concepts' collection.
    REFACTORING_NOTE: This is the first CRUD operation for the new generalized concept model.
    It adheres to the schema defined in Sub-Task 1.2 of concept_refactoring.md.
    Now accepts concept_id.
    """
    # Use repository for concepts collection access
    concepts_coll = ConceptsRepository.collection()
    if concepts_coll is None:
        raise ConceptServiceError("Database collection 'concepts' not available.")

    if not name:
        raise InvalidConceptDataError("'name' is a required field.")

    if not concept_id and not vontology_path:
        raise InvalidConceptDataError(
            "Either 'concept_id' or 'vontology_path' must be provided."
        )

    if concept_id:
        try:
            from ..utils.concept_id_utils import canonicalise_vontology_concept_id

            canonical_id = canonicalise_vontology_concept_id(concept_id)
        except Exception:
            canonical_id = None

        if canonical_id:
            concept_id = canonical_id
        else:
            raise InvalidConceptDataError("Concept ID is empty after normalisation.")

    now = datetime.now(timezone.utc)

    # Persist display name using text_relations (modern approach) instead of legacy names[] field
    # Determine relationship shape based on creation kind
    # JVNAUTOSCI-1047: Canonicalise parent IDs to prevent dangling references
    # when LLM uses variant spellings (e.g., "#V#Business_Trip" vs "#V#businesstrip")
    parent_ids = parent_concept_ids or []
    if parent_ids:
        from ..utils.concept_id_utils import canonicalise_vontology_concept_id

        parent_ids = [canonicalise_vontology_concept_id(p) or p for p in parent_ids]

    # JVNAUTOSCI-689: Handle explicit instance_of_type parameter
    # When instance_of_type is provided, the concept is BOTH:
    # - A subtype of parent_ids (is_a_type_of)
    # - An instance of instance_of_type (is_an_instance_of)
    if instance_of_type:
        from ..utils.concept_id_utils import canonicalise_vontology_concept_id

        canonical_instance_type = (
            canonicalise_vontology_concept_id(instance_of_type) or instance_of_type
        )
        is_instance_of = [canonical_instance_type]
        is_a_type_of = parent_ids  # Preserve hierarchy from parent_ids
    else:
        # Default behaviour (backward compatible)
        is_instance_of = parent_ids if create_as_instance else []
        is_a_type_of = [] if create_as_instance else parent_ids

    concept_doc: Dict[str, Any] = {
        # DO NOT include "names" field - will be created as text_relations below
        "guid": str(uuid.uuid4()),  # JVNAUTOSCI-730: Stable GUID
        "created_at": now,
        "updated_at": now,
        "embedding_status": "pending",  # JVNAUTOSCI-680: Mark for indexing
        "attributes": attributes or {},
        "system_tags": system_tags or [],
        "user_tags": user_tags or [],
        "relationships": {
            "is_a_type_of": is_a_type_of,
            "is_an_instance_of": is_instance_of,
            "linked_to": linked_concepts or [],
        },
    }
    if description:
        set_concept_description(concept_doc, description)
    if notes:
        set_concept_notes(concept_doc, notes)

    if concept_id:
        concept_doc["concept_id"] = concept_id

    if vontology_path:
        concept_doc["vontology_path"] = vontology_path

    try:
        result: InsertOneResult = concepts_coll.insert_one(concept_doc)
        # Fetch the document to ensure all defaults/triggers (if any) are included
        created_concept = concepts_coll.find_one({"_id": result.inserted_id})

        # CRITICAL: Create name as text_relation immediately (modern approach)
        # This prevents migrate-on-read from triggering and creating duplicates
        concept_identifier = concept_doc.get("concept_id")
        if concept_identifier:
            try:
                from .text_value_service import upsert_text_for_concept

                # 1. Register the primary display name (NL)
                if name and name.strip():
                    upsert_text_for_concept(
                        subject_concept_id=concept_identifier,
                        predicate="hasName",
                        text=name.strip(),
                        lang="en-NZ",
                        context={"name_type": "NL"},
                    )
                    logger.info(
                        f"[create_concept] Created hasName text_relation for {concept_identifier}: {name}"
                    )

                # 2. Register vonID as CODE name (JVNAUTOSCI-316)
                upsert_text_for_concept(
                    subject_concept_id=concept_identifier,
                    predicate="hasName",
                    text=concept_identifier,
                    lang="en-NZ",
                    context={"name_type": "CODE"},
                )

                # 3. Register GUID as CODE name (JVNAUTOSCI-316)
                if "_id" in concept_doc:
                    upsert_text_for_concept(
                        subject_concept_id=concept_identifier,
                        predicate="hasName",
                        text=str(concept_doc["_id"]),
                        lang="en-NZ",
                        context={"name_type": "CODE"},
                    )

                # 4. Register stable GUID as CODE name (JVNAUTOSCI-730)
                if "guid" in concept_doc:
                    upsert_text_for_concept(
                        subject_concept_id=concept_identifier,
                        predicate="hasName",
                        text=concept_doc["guid"],
                        lang="en-NZ",
                        context={"name_type": "CODE"},
                    )

            except Exception as name_err:
                # Non-fatal: concept is created, just name relation failed
                logger.warning(
                    f"[create_concept] Failed to create name relations for {concept_identifier}: {name_err}"
                )

        try:
            if concept_identifier:
                ConceptsRepository.reconcile_relationships(
                    concept_identifier,
                    concept_doc.get("relationships") or {},
                )
        except Exception as reconcile_err:
            logger.warning(
                "create_concept: relationship reconciliation best-effort failure for %s: %s",
                concept_doc.get("concept_id"),
                reconcile_err,
            )

        workflow_event_launches: Dict[str, Any] = {}
        if concept_identifier:
            # Keep mutation emission in this canonical create path so all
            # create surfaces (routes, MCP tools, internal services) stay consistent.
            try:
                from .workflow_event_integration_service import (
                    EVENT_TYPE_CONCEPT_CREATED,
                    maybe_launch_type_created_workflow,
                    maybe_launch_vontology_mutation_workflow,
                    resolve_event_actor_context,
                )

                created_by_concept_id, organisation_concept_id = (
                    resolve_event_actor_context()
                )
                workflow_event_launches[EVENT_TYPE_CONCEPT_CREATED] = (
                    maybe_launch_vontology_mutation_workflow(
                        mutation_event_type=EVENT_TYPE_CONCEPT_CREATED,
                        mutation_id=concept_identifier,
                        user_id=created_by_concept_id,
                        org_id=organisation_concept_id,
                        event_payload={
                            "concept_id": concept_identifier,
                            "instance_of_type": instance_of_type,
                            "parent_type_ids": list(parent_ids),
                            "create_as_instance": bool(create_as_instance),
                        },
                        inputs={
                            "concept_id": concept_identifier,
                            "instance_of_type": instance_of_type,
                            "parent_type_ids": list(parent_ids),
                            "create_as_instance": bool(create_as_instance),
                        },
                    )
                )

                if not create_as_instance:
                    workflow_event_launches["type.created"] = (
                        maybe_launch_type_created_workflow(
                            type_concept_id=concept_identifier,
                            created_by_concept_id=created_by_concept_id,
                            organisation_concept_id=organisation_concept_id,
                            parent_type_ids=list(parent_ids),
                        )
                    )
            except Exception as workflow_exc:
                logger.warning(
                    "create_concept: workflow event launch failed for %s: %s",
                    concept_identifier,
                    workflow_exc,
                )

        if created_concept:
            if "_id" in created_concept:
                created_concept["id"] = str(created_concept.pop("_id"))
            # Ensure a display name is present in response using centralized accessor
            try:
                from ..vontology.utils_vontology import (
                    get_concept_display_name_with_names_fallback,
                )

                disp = get_concept_display_name_with_names_fallback(created_concept)
                if disp:
                    created_concept["name"] = disp
            except Exception:
                # Best-effort; leave name as-is if accessor fails
                pass
            if workflow_event_launches:
                created_concept["workflow_event_launch"] = workflow_event_launches
        _invalidate_concept_mutation_caches()
        return created_concept if created_concept else {}

    except DuplicateKeyError as e:
        raise InvalidConceptDataError(
            f"concept creation failed due to duplicate key: {e}"
        )
    except Exception as e:
        raise ConceptServiceError(f"Error creating concept in database: {e}")


# Note: get_concept and get_concept_by_id are defined below (duplicate removed)


def get_concept(
    concept_id: str,
) -> Optional[Dict[str, Any]]:  # Backward-compatible alias expected by other services
    return get_concept_by_id(concept_id)


def get_concept_by_id(concept_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves a concept by its unique ID.
    Supports documents where _id may be an ObjectId or a string of the ObjectId hex.
    """
    concepts_coll = ConceptsRepository.collection()
    if concepts_coll is None:
        raise ConceptServiceError("Database collection 'concepts' not available.")

    # Try ObjectId first, then fall back to string _id
    obj_id = None
    try:
        obj_id = ObjectId(concept_id)
    except Exception:
        logger.debug(
            f"get_concept_by_id: '{concept_id}' is not a valid ObjectId; will try string _id."
        )

    try:
        concept_doc = None
        # Use a unified query to check _id (ObjectId/string) and concept_id
        query_filter: Dict[str, Any]
        if obj_id is not None:
            query_filter = {
                "$or": [
                    {"_id": obj_id},
                    {"_id": concept_id},
                    {"concept_id": concept_id},
                ]
            }
        else:
            query_filter = {"$or": [{"_id": concept_id}, {"concept_id": concept_id}]}

        concept_doc = concepts_coll.find_one(query_filter)

        if concept_doc:
            concept_doc["id"] = (
                str(concept_doc.pop("_id")) if "_id" in concept_doc else concept_id
            )

            # Ensure name is present using centralized accessor with language fallback
            try:
                from ..vontology.utils_vontology import (
                    get_concept_display_name_with_names_fallback,
                )

                resolved_name = get_concept_display_name_with_names_fallback(
                    concept_doc
                )
                if resolved_name:
                    concept_doc["name"] = resolved_name
            except Exception:
                # Legacy fallback from concept_id
                if not concept_doc.get("name"):
                    cid = concept_doc.get("concept_id")
                    if isinstance(cid, str) and cid.startswith("#V#"):
                        concept_doc["name"] = cid[3:].replace("_", " ").title()
            # Scrub legacy top-level description for protected prompt concept(s)
            try:
                from .annotation_extraction_service import (
                    PROMPT_CONCEPT_ID,
                )  # lazy import to avoid cycle

                if (
                    concept_doc.get("concept_id") == PROMPT_CONCEPT_ID
                    and "description" in concept_doc
                ):
                    concept_doc.pop("description", None)
            except Exception:
                pass
            return concept_doc

        raise ConceptNotFoundError(f"concept with ID '{concept_id}' not found.")
    except Exception as e:
        if isinstance(e, ConceptNotFoundError):
            raise
        logger.error(f"Error retrieving concept by ID {concept_id}: {e}", exc_info=True)
        raise ConceptServiceError(f"Could not retrieve concept: {str(e)}")


def get_concept_by_concept_id(concept_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a concept by its ontological concept_id (e.g., '#V#person').
    Returns the full document with 'id' as string if found; otherwise raises ConceptNotFoundError.
    """
    concepts_coll = ConceptsRepository.collection()
    if concepts_coll is None:
        raise ConceptServiceError("Database collection 'concepts' not available.")

    try:
        # Canonicalise punctuation variants (e.g. hyphen vs underscore) while keeping
        # a safe fallback to the original value for existing legacy records.
        from ..utils.concept_id_utils import canonicalise_vontology_concept_id

        canonical_id = canonicalise_vontology_concept_id(concept_id)
        concept_doc = None
        if canonical_id and canonical_id != concept_id:
            concept_doc = concepts_coll.find_one({"concept_id": canonical_id})
        if concept_doc is None:
            concept_doc = concepts_coll.find_one({"concept_id": concept_id})
        if concept_doc:
            if "_id" in concept_doc:
                concept_doc["id"] = str(concept_doc.pop("_id"))
            # Ensure name is present using centralized accessor
            try:
                from ..vontology.utils_vontology import (
                    get_concept_display_name_with_names_fallback,
                )

                resolved_name = get_concept_display_name_with_names_fallback(
                    concept_doc
                )
                if resolved_name:
                    concept_doc["name"] = resolved_name
            except Exception:
                if not concept_doc.get("name"):
                    cid = concept_doc.get("concept_id")
                    if isinstance(cid, str) and cid.startswith("#V#"):
                        concept_doc["name"] = cid[3:].replace("_", " ").title()
            try:
                from .annotation_extraction_service import (
                    PROMPT_CONCEPT_ID,
                )  # lazy import

                if (
                    concept_doc.get("concept_id") == PROMPT_CONCEPT_ID
                    and "description" in concept_doc
                ):
                    concept_doc.pop("description", None)
            except Exception:
                pass

            # Add kind field (type, predicate, or individual) for frontend
            try:
                from ..vontology.utils_vontology import is_type, is_predicate

                # Check predicate FIRST: predicates can have is_a_type_of relationships
                if is_predicate(concept_doc):
                    concept_doc["kind"] = "predicate"
                elif is_type(concept_doc):
                    concept_doc["kind"] = "type"
                else:
                    concept_doc["kind"] = "individual"
            except Exception:
                concept_doc["kind"] = "unknown"

            return concept_doc

        # JVNAUTOSCI-945: Try alias resolution for renamed concepts
        try:
            from .concept_rename_service import resolve_concept_by_alias

            resolved_id = resolve_concept_by_alias(concept_id)
            if resolved_id and resolved_id != concept_id:
                # Recursively fetch using the resolved (current) ID
                return get_concept_by_concept_id(resolved_id)
        except Exception as alias_err:
            logger.debug("Alias resolution for '%s' failed: %s", concept_id, alias_err)

        # Virtual fallback for code-handled concepts (read-only).
        try:
            if isinstance(concept_id, str) and concept_id.startswith("#V#"):
                from ..vontology.code_concepts_registry import build_virtual_concept_doc

                virtual_doc = build_virtual_concept_doc(concept_id)
                if isinstance(virtual_doc, dict) and virtual_doc.get("concept_id"):
                    virtual_doc = {**virtual_doc}
                    virtual_doc.setdefault("id", f"virtual:{concept_id}")
                    virtual_doc.setdefault(
                        "kind",
                        virtual_doc.get("metadata", {}).get("concept_type", "unknown"),
                    )
                    return virtual_doc
        except Exception:
            pass

        raise ConceptNotFoundError(f"concept with concept_id '{concept_id}' not found.")
    except Exception as e:
        if isinstance(e, ConceptNotFoundError):
            raise
        logger.error(
            f"Error retrieving concept by concept_id {concept_id}: {e}", exc_info=True
        )
        raise ConceptServiceError(f"Could not retrieve concept by concept_id: {str(e)}")


def enrich_concept_with_text_relations(
    concept: Dict[str, Any], logger=None
) -> Dict[str, Any]:
    """
    Enriches a concept document with names and descriptions from text relations.
    Also performs migrate-on-read for legacy names/descriptions in concept document.

    This is the SHARED implementation used by both HTTP API endpoints and internal MCP.

    Args:
        concept: The concept document to enrich
        logger: Optional logger instance (uses module logger if not provided)


        # Maintain relationship invariants for parent/child edges using the central helper
        try:
            concept_identifier = concept_doc.get("concept_id")
            if concept_identifier:
                ConceptsRepository.reconcile_relationships(
                    concept_identifier,
                    concept_doc.get("relationships") or {},
                )
        except Exception as reconcile_err:
            logger.warning(
                "create_concept: relationship reconciliation best-effort failure for %s: %s",
                concept_doc.get("concept_id"),
                reconcile_err,
            )
    Returns:
        The enriched concept document with 'names' array populated from text relations
    """
    from .text_value_service import get_texts_for_concept, upsert_text_for_concept
    from ..db.repositories.concepts_repository import ConceptsRepository

    if not logger:
        logger = globals().get("logger")

    if not concept:
        return concept

    concept_id = concept.get("concept_id")
    if not concept_id:
        if logger:
            logger.warning(
                "[enrich_concept] Missing concept_id, cannot fetch text relations"
            )
        concept["names"] = []
        return concept

    # MIGRATE-ON-READ: Legacy names field → text relations
    legacy_names = concept.get("names", [])
    if legacy_names and isinstance(legacy_names, list) and len(legacy_names) > 0:
        if logger:
            logger.info(
                f"[migrate-on-read] Migrating {len(legacy_names)} legacy names for {concept_id}"
            )
        migrated_count = 0
        for legacy_name in legacy_names:
            try:
                name_text = (
                    legacy_name.get("name", "")
                    if isinstance(legacy_name, dict)
                    else str(legacy_name)
                )
                if not name_text:
                    continue
                lang = (
                    legacy_name.get("language", "en")
                    if isinstance(legacy_name, dict)
                    else "en"
                )
                name_type = (
                    legacy_name.get("type", "NL")
                    if isinstance(legacy_name, dict)
                    else "NL"
                )

                result = upsert_text_for_concept(
                    subject_concept_id=concept_id,
                    predicate="hasName",
                    text=name_text,
                    lang=lang,
                    context={"name_type": name_type},
                )
                if result:
                    migrated_count += 1
                    if logger:
                        logger.debug(
                            f"[migrate-on-read] Migrated name: {name_text} ({lang})"
                        )
            except Exception as e:
                if logger:
                    logger.warning(
                        f"[migrate-on-read] Failed to migrate name {legacy_name}: {e}"
                    )

        # Remove legacy names field
        if migrated_count > 0:
            try:
                ConceptsRepository.update_one(
                    {"concept_id": concept_id}, {"$unset": {"names": ""}}
                )
                if logger:
                    logger.info(
                        f"[migrate-on-read] Removed legacy 'names' field after migrating {migrated_count} names"
                    )
            except Exception as e:
                if logger:
                    logger.error(
                        f"[migrate-on-read] Failed to remove legacy names field: {e}"
                    )

    # Fetch names from text relations (authoritative source)
    names_from_relations = get_texts_for_concept(
        subject_concept_id=concept_id, predicate="hasName", limit=100
    )
    # Convert to frontend format
    concept["names"] = [
        {
            "name": item.get("text", ""),
            "language": item.get("lang", "en-NZ"),
            "type": item.get("context", {}).get("name_type", "NL"),
            "relation_id": item.get("relation_id"),
        }
        for item in names_from_relations
    ]

    # Ensure CODE identifiers are present for UI/debugging (JVNAUTOSCI-938):
    # Some concepts may not have had CODE names persisted historically, but users
    # expect to see IDs (e.g., #V#..., db ObjectId, stable guid) in the Names section.
    def _looks_like_object_id(value: str) -> bool:
        if not isinstance(value, str):
            return False
        s = value.strip()
        if len(s) != 24:
            return False
        try:
            int(s, 16)
            return True
        except Exception:
            return False

    existing_keys = set()
    for n in concept.get("names", []) or []:
        if not isinstance(n, dict):
            continue
        existing_keys.add(
            (
                str(n.get("name") or "").strip(),
                str(n.get("language") or "").strip() or "en-NZ",
                str(n.get("type") or "").strip().upper() or "NL",
            )
        )

    def _add_code_name(text: Optional[str]) -> None:
        if not text or not isinstance(text, str):
            return
        cleaned = text.strip()
        if not cleaned:
            return
        key = (cleaned, "en-NZ", "CODE")
        if key in existing_keys:
            return
        concept.setdefault("names", []).append(
            {
                "name": cleaned,
                "language": "en-NZ",
                "type": "CODE",
                "relation_id": None,
            }
        )
        existing_keys.add(key)

    # Always include the ontological ID itself.
    _add_code_name(concept_id)

    # Include db document ID when it looks like a Mongo ObjectId.
    maybe_db_id = concept.get("id")
    if isinstance(maybe_db_id, str) and _looks_like_object_id(maybe_db_id):
        _add_code_name(maybe_db_id)

    # Include stable UUID (JVNAUTOSCI-730).
    maybe_guid = concept.get("guid")
    if isinstance(maybe_guid, str) and maybe_guid.strip():
        _add_code_name(maybe_guid)

    # MIGRATE-ON-READ: Legacy description field → text relations
    legacy_description = concept.get("description")
    if (
        legacy_description
        and isinstance(legacy_description, str)
        and legacy_description.strip()
    ):
        if logger:
            logger.info(f"[migrate-on-read] Found legacy description for {concept_id}")
        try:
            existing_descriptions = get_texts_for_concept(
                subject_concept_id=concept_id, predicate="hasDescription", limit=1
            )

            if not existing_descriptions:
                if logger:
                    logger.info(
                        f"[migrate-on-read] Migrating legacy description to text relations"
                    )
                upsert_text_for_concept(
                    subject_concept_id=concept_id,
                    predicate="hasDescription",
                    text=legacy_description,
                    lang="en",
                    context={"source": "OpenCyc", "format": "html"},
                )

            # Remove legacy description field
            try:
                ConceptsRepository.update_one(
                    {"concept_id": concept_id}, {"$unset": {"description": ""}}
                )
                if logger:
                    logger.info(f"[migrate-on-read] Removed legacy 'description' field")
            except Exception as e:
                if logger:
                    logger.error(
                        f"[migrate-on-read] Failed to remove legacy description: {e}"
                    )
        except Exception as e:
            if logger:
                logger.warning(f"[migrate-on-read] Failed to migrate description: {e}")

    # Fetch content from text relations (for diary entries, articles, etc.)
    content_relations = get_texts_for_concept(
        subject_concept_id=concept_id, predicate="hasContent", limit=1
    )
    if content_relations:
        concept["content"] = content_relations[0].get("text", "")

    # Fetch notes from text relations
    note_relations = get_texts_for_concept(
        subject_concept_id=concept_id, predicate="hasNote", limit=1
    )
    if note_relations:
        concept["note"] = note_relations[0].get("text", "")

    return concept


def update_concept(concept_id: str, update_data: Dict[str, Any]) -> Dict[str, Any]:
    """Updates an existing concept by its ID.
    REFACTORING_NOTE: Implements the 'Update' part of CRUD for concepts.
    Allows partial updates. Automatically updates the 'updated_at' timestamp.
    Prevents updates to immutable fields like '_id', 'created_at', and 'vontology_path'.
    """
    concepts_coll = ConceptsRepository.collection()
    if concepts_coll is None:
        raise ConceptServiceError("Database collection 'concepts' not available.")

    if not update_data:
        raise InvalidConceptDataError("Update data cannot be empty.")

    # Build a filter that works for both ObjectId and string _id
    obj_id = None
    try:
        obj_id = ObjectId(concept_id)
    except Exception:
        logger.debug(
            f"update_concept: '{concept_id}' is not a valid ObjectId; will try string _id only."
        )

    # REFACTORING_NOTE: Prevent modification of certain fields.
    # Client should not send these, but good to enforce on server-side.
    immutable_fields = ["_id", "created_at", "vontology_path", "guid"]
    for field in immutable_fields:
        if field in update_data:
            raise InvalidConceptDataError(f"Field '{field}' cannot be modified.")

    # BUGFIX: Prevent concept_id updates that would cause duplicate key errors
    # Individual concepts should not have their concept_id changed to type concept_ids
    if "concept_id" in update_data:
        new_concept_id = update_data["concept_id"]
        logger.warning(
            f"Attempt to update concept_id for concept {concept_id} to '{new_concept_id}'. This is generally not allowed for individual concepts."
        )
        # For now, remove concept_id from update_data to prevent duplicate key errors
        # TODO: Implement proper validation based on concept type vs individual distinction
        del update_data["concept_id"]
        logger.info(
            f"Removed concept_id from update payload to prevent duplicate key error"
        )

    # Prevent reintroducing legacy storage for descriptions.
    # Canonical descriptive text must be stored via text relations (predicate hasDescription).
    if "description" in update_data:
        logger.warning(
            "update_concept: Ignoring legacy 'description' field update for %s; use hasDescription text relations instead",
            concept_id,
        )
        del update_data["description"]

    # Prepare the update document for MongoDB
    # We will use $set to update specified fields and also set the new updated_at timestamp.
    update_payload = {"$set": {}}
    for key, value in update_data.items():
        # Special handling for notes field to use proper schema location
        if key == "notes":
            # Phase 1 removal: do not map 'notes' into preserved_fields. Leave for relation-based handlers.
            logger.info(
                "Skipping legacy mapping of 'notes' into concept_data.preserved_fields (deprecated Phase 1)"
            )
            continue
        else:
            update_payload["$set"][key] = value

    update_payload["$set"]["updated_at"] = datetime.now(timezone.utc)
    # Mark embedding as stale for reindexing (JVNAUTOSCI-680)
    update_payload["$set"]["embedding_status"] = "stale"

    relationship_update = any(
        key == "relationships" or key.startswith("relationships.")
        for key in update_data.keys()
    )

    try:
        # REFACTORING_NOTE: Using find_one_and_update to get the updated document back.
        # The `return_document=ReturnDocument.AFTER` option ensures the document after the update is returned.
        # Try update by ObjectId or string _id, and also allow matching by ontological concept_id (e.g., '#V#person').
        # This fixes updates for type nodes referenced by concept_id in routes like /api/concepts/%23V%23person.
        id_filter: Dict[str, Any]
        if obj_id is not None:
            id_filter = {
                "$or": [
                    {"_id": obj_id},
                    {"_id": concept_id},
                    {"concept_id": concept_id},
                ]
            }
        else:
            id_filter = {"$or": [{"_id": concept_id}, {"concept_id": concept_id}]}

        previous_relationships = None
        previous_concept_id = None
        if relationship_update:
            try:
                prior_doc = concepts_coll.find_one(
                    id_filter, {"relationships": 1, "concept_id": 1}
                )
                if prior_doc:
                    previous_relationships = prior_doc.get("relationships") or {}
                    previous_concept_id = prior_doc.get("concept_id")
            except Exception as prior_err:
                logger.warning(
                    "update_concept: failed to load prior relationships for %s: %s",
                    concept_id,
                    prior_err,
                )

        updated_concept_doc = concepts_coll.find_one_and_update(
            id_filter, update_payload, return_document=ReturnDocument.AFTER
        )

        if not updated_concept_doc:
            raise ConceptNotFoundError(
                f"concept with ID '{concept_id}' not found for update."
            )

        # REFACTORING_NOTE: Convert _id to id string for consistent API response
        if "_id" in updated_concept_doc:
            updated_concept_doc["id"] = str(updated_concept_doc.pop("_id"))

        # Ensure notes field uses proper getter function for consistency in API response
        if updated_concept_doc:
            updated_concept_doc["notes"] = get_concept_notes(updated_concept_doc)

        if relationship_update:
            try:
                concept_identifier = (
                    updated_concept_doc.get("concept_id")
                    if updated_concept_doc
                    else previous_concept_id
                )
                concept_identifier = concept_identifier or concept_id
                if concept_identifier:
                    ConceptsRepository.reconcile_relationships(
                        concept_identifier,
                        (updated_concept_doc or {}).get("relationships") or {},
                        previous_relationships=previous_relationships,
                    )
            except Exception as reconcile_err:
                logger.warning(
                    "update_concept: relationship reconciliation best-effort failure for %s: %s",
                    concept_id,
                    reconcile_err,
                )

        try:
            from .workflow_event_integration_service import (
                EVENT_TYPE_CONCEPT_UPDATED,
                maybe_launch_vontology_mutation_workflow,
                resolve_event_actor_context,
            )

            updated_concept_id = (
                str(updated_concept_doc.get("concept_id") or "").strip()
                if isinstance(updated_concept_doc, dict)
                else ""
            ) or concept_id
            actor_id, actor_org = resolve_event_actor_context()
            mutation_launch = maybe_launch_vontology_mutation_workflow(
                mutation_event_type=EVENT_TYPE_CONCEPT_UPDATED,
                mutation_id=updated_concept_id,
                user_id=actor_id,
                org_id=actor_org,
                event_payload={
                    "concept_id": updated_concept_id,
                    "updated_fields": sorted(list(update_data.keys())),
                },
                inputs={
                    "concept_id": updated_concept_id,
                    "updated_fields": sorted(list(update_data.keys())),
                },
            )
            if isinstance(updated_concept_doc, dict):
                updated_concept_doc["workflow_event_launch"] = mutation_launch
        except Exception as workflow_exc:
            logger.warning(
                "update_concept: workflow event launch failed for %s: %s",
                concept_id,
                workflow_exc,
            )

        _invalidate_concept_mutation_caches()
        return updated_concept_doc
    except ConceptNotFoundError:  # Re-raise specific error
        raise
    except Exception as e:
        # Log the error e
        raise ConceptServiceError(
            f"Error updating concept with ID '{concept_id}' in database: {e}"
        )


def delete_concept(concept_id: str) -> bool:
    """Deletes an concept by its ID.
    REFACTORING_NOTE: Implements the 'Delete' part of CRUD for concepts.
    Returns True if deletion was successful (acknowledged and count > 0), False otherwise.
    """
    concepts_coll = ConceptsRepository.collection()
    if concepts_coll is None:  # MODIFIED
        raise ConceptServiceError("Database collection 'concepts' not available.")

    if not isinstance(concept_id, str) or not concept_id.strip():
        raise InvalidConceptDataError("concept_id must be a non-empty string")

    # Resolve identifier to a concrete concept document and canonical concept_id.
    # Callers may provide either:
    # - a MongoDB _id (ObjectId hex string or string-stored _id)
    # - an ontological concept_id (e.g. '#V#person')
    try:
        doc = None

        if concept_id.startswith("#V#"):
            doc = ConceptsRepository.find_one(
                {"concept_id": concept_id}, {"concept_id": 1, "_id": 1}
            )
        else:
            obj_id = None
            try:
                obj_id = ObjectId(concept_id)
            except Exception:
                obj_id = None

            if obj_id is not None:
                doc = ConceptsRepository.find_one(
                    {"_id": obj_id}, {"concept_id": 1, "_id": 1}
                )
            if doc is None:
                doc = ConceptsRepository.find_one(
                    {"_id": concept_id}, {"concept_id": 1, "_id": 1}
                )
            if doc is None:
                # Fallback: treat as a concept_id-like identifier even if it doesn't start with '#V#'.
                doc = ConceptsRepository.find_one(
                    {"concept_id": concept_id}, {"concept_id": 1, "_id": 1}
                )

        if not doc:
            raise ConceptNotFoundError(
                f"concept with ID '{concept_id}' not found for deletion."
            )

        canonical_concept_id = doc.get("concept_id")
        if not isinstance(canonical_concept_id, str) or not canonical_concept_id:
            # Extremely defensive: if the document lacks a concept_id, fall back to deleting by _id.
            delete_filter: Dict[str, Any] = {"_id": doc.get("_id")}
            result: DeleteResult = concepts_coll.delete_one(delete_filter)
            if result.deleted_count == 0:
                raise ConceptNotFoundError(
                    f"concept with ID '{concept_id}' not found for deletion."
                )
            try:
                from .workflow_event_integration_service import (
                    EVENT_TYPE_CONCEPT_DELETED,
                    maybe_launch_vontology_mutation_workflow,
                    resolve_event_actor_context,
                )

                actor_id, actor_org = resolve_event_actor_context()
                maybe_launch_vontology_mutation_workflow(
                    mutation_event_type=EVENT_TYPE_CONCEPT_DELETED,
                    mutation_id=concept_id,
                    user_id=actor_id,
                    org_id=actor_org,
                    event_payload={"concept_id": concept_id},
                    inputs={"concept_id": concept_id},
                )
            except Exception as workflow_exc:
                logger.warning(
                    "delete_concept: workflow event launch failed for %s: %s",
                    concept_id,
                    workflow_exc,
                )
            _invalidate_concept_mutation_caches()
            return bool(result.acknowledged and result.deleted_count > 0)

        # Best-effort: remove text relations (and GC orphaned text_values) before deleting.
        # This keeps behaviour consistent even if the lower-level delete helper changes.
        cascade_ids = {canonical_concept_id}
        if concept_id != canonical_concept_id:
            cascade_ids.add(concept_id)

        total_rel = 0
        total_tv = 0
        for sid in cascade_ids:
            try:
                rel_removed, tv_removed = _cascade_delete_text_relations(sid)
                total_rel += rel_removed
                total_tv += tv_removed
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    f"[cascade_delete] Failed for subject variant {sid}: {e}"
                )

        # Canonical delete path: use vontology delete helper so rewiring/protection is consistent.
        from ..vontology.utils_vontology import simulate_or_delete_concept

        delete_report = simulate_or_delete_concept(canonical_concept_id, execute=True)

        if not delete_report.get("success"):
            if delete_report.get("protected"):
                raise ConceptServiceError(
                    f"Concept '{canonical_concept_id}' is protected and cannot be deleted."
                )
            raise ConceptServiceError(
                f"Delete failed for concept '{canonical_concept_id}': {delete_report.get('error') or delete_report.get('message') or delete_report}"
            )

        try:
            from .workflow_event_integration_service import (
                EVENT_TYPE_CONCEPT_DELETED,
                maybe_launch_vontology_mutation_workflow,
                resolve_event_actor_context,
            )

            actor_id, actor_org = resolve_event_actor_context()
            maybe_launch_vontology_mutation_workflow(
                mutation_event_type=EVENT_TYPE_CONCEPT_DELETED,
                mutation_id=canonical_concept_id,
                user_id=actor_id,
                org_id=actor_org,
                event_payload={
                    "concept_id": canonical_concept_id,
                    "removed_text_relations_count": total_rel,
                    "removed_orphan_text_values_count": total_tv,
                },
                inputs={
                    "concept_id": canonical_concept_id,
                    "removed_text_relations_count": total_rel,
                    "removed_orphan_text_values_count": total_tv,
                },
            )
        except Exception as workflow_exc:
            logger.warning(
                "delete_concept: workflow event launch failed for %s: %s",
                canonical_concept_id,
                workflow_exc,
            )

        _invalidate_concept_mutation_caches()

        logger.info(
            f"[cascade_delete] concept={canonical_concept_id} subject_variants={len(cascade_ids)} removed_relations={total_rel} gc_text_values={total_tv}"
        )
        return True
    except ConceptNotFoundError:  # Re-raise specific error
        raise
    except Exception as e:
        # Log the error e
        raise ConceptServiceError(
            f"Error deleting concept with ID '{concept_id}' from database: {e}"
        )


def _cascade_delete_text_relations(concept_id: str) -> tuple[int, int]:
    """Remove text_relations for a subject id and GC unreferenced text_values.

    Returns (relations_removed, text_values_removed).
    Silent on individual GC failures; used best-effort during concept deletion.
    """
    from ..db.mongo_client import (
        get_text_relations_collection,
        get_text_values_collection,
    )

    rel_coll = get_text_relations_collection()
    if rel_coll is None:
        return (0, 0)
    rels = list(
        rel_coll.find({"subject_concept_id": concept_id}, {"object_text_id": 1})
    )
    if not rels:
        return (0, 0)
    tv_ids: list[str] = []
    for rel in rels:
        tv_id = rel.get("object_text_id")
        if isinstance(tv_id, str):
            tv_ids.append(tv_id)
    delete_res = rel_coll.delete_many({"subject_concept_id": concept_id})
    removed = delete_res.deleted_count if delete_res else 0
    if not tv_ids:
        return (removed, 0)
    tv_coll = get_text_values_collection()
    if tv_coll is None:
        return (removed, 0)
    seen = set()
    unique_tv_ids = []
    for _id in tv_ids:
        if _id not in seen:
            seen.add(_id)
            unique_tv_ids.append(_id)
    tv_removed = 0
    from bson import ObjectId as _OID  # local alias

    for tv_id in unique_tv_ids:
        try:
            # Still referenced? skip
            if rel_coll.find_one({"object_text_id": tv_id}):
                continue
            try:
                oid = _OID(tv_id)
                r = tv_coll.delete_one({"_id": oid})
            except Exception:
                r = tv_coll.delete_one({"_id": tv_id})
            if r and r.deleted_count:
                tv_removed += r.deleted_count
        except Exception:
            continue
    return (removed, tv_removed)


def list_concepts(
    concept_id: Optional[str] = None,
    vontology_path: Optional[str] = None,
    system_tags: Optional[List[str]] = None,
    user_tags: Optional[List[str]] = None,
    search_term: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
    sort_by: str = "updated_at",  # Default sort field
    sort_order: int = -1,  # Default sort order (descending)
    include_descendants: bool = True,  # New parameter for transitive queries
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Lists instances (concepts) from the unified 'concepts' collection.
    Instances are identified by having 'relationships.is_an_instance_of' (they can also have is_a_type_of).
    If concept_id is provided, it lists instances of that concept_id or any of its descendant types.

    Note: In the multi-level ontology, concepts can be both types and instances simultaneously.
    For the concept tab, we want to show all concepts that are instances of something,
    regardless of whether they also serve as types for other concepts.

    Supports filtering by vontology_path (legacy), tags, and a search term.
    Supports pagination and sorting.
    """
    concepts_coll = ConceptsRepository.collection()
    if concepts_coll is None:
        logger.error("Concepts collection is not available for list_concepts.")
        return [], 0

    query: MongoQuery = {}

    if concept_id:
        try:
            if include_descendants:
                # Get the concept_id and all its descendant type IDs
                descendant_ids = get_vontology_node_and_descendant_ids(concept_id)
                if descendant_ids:
                    query["relationships.is_an_instance_of"] = {"$in": descendant_ids}
                    logger.info(
                        f"Querying for instances of {concept_id} and its {len(descendant_ids)-1} descendants"
                    )
                else:
                    # Fallback to direct instances only
                    query["relationships.is_an_instance_of"] = concept_id
                    logger.info(
                        f"No descendants found for {concept_id}, querying direct instances only"
                    )
            else:
                # Direct instances only
                query["relationships.is_an_instance_of"] = concept_id
                logger.info(
                    f"Querying for direct instances with concept_id: {concept_id}"
                )
        except Exception as e:
            logger.error(
                f"Error processing concept_id {concept_id}: {e}. Falling back to direct instances.",
                exc_info=True,
            )
            # Fallback to direct instances
            query["relationships.is_an_instance_of"] = concept_id
    else:
        # Base query: only get instances (have non-empty is_an_instance_of relationship)
        # Note: They may also have is_a_type_of (multi-level concepts)
        # Use $ne: [] to exclude documents with empty arrays (which would match $exists: True)
        query["relationships.is_an_instance_of"] = {"$exists": True, "$ne": []}

    if vontology_path:
        # DEPRECATION: This path-based logic should be phased out.
        query["vontology_path"] = vontology_path

    if system_tags:
        query["system_tags"] = {"$all": system_tags}
    if user_tags:
        query["user_tags"] = {"$all": user_tags}

    if search_term:
        # Use unified search query building from concept_search_service
        # This ensures consistent search behaviour across all endpoints
        from .concept_search_service import _build_name_query

        name_search_query = _build_name_query(
            search_term,
            exact=False,  # Always substring for list_concepts
            prefix=False,  # No prefix optimization here (already filtered by instance_of)
            include_description=True,  # Search in descriptions for concept tab
        )
        # Merge the name search query into our base query
        if "$or" in query:
            # If there's already an $or, we need to combine with $and
            query = {"$and": [query, name_search_query]}
        else:
            # Otherwise, merge directly
            query.update(name_search_query)

    # Apply the visibility filter to the query
    query = apply_concept_query_filter(query)

    try:
        total_count = ConceptsRepository.count_documents(query)

        # MongoDB sort order: 1 for ascending, -1 for descending.
        # sort_order parameter is expected to be this value.
        sort_criteria = [(sort_by, sort_order)]

        skip_amount = (page - 1) * per_page
        concepts_cursor = ConceptsRepository.find(
            query, sort=sort_criteria, skip=skip_amount, limit=per_page
        )

        concepts_list = []
        for concept_doc in concepts_cursor:
            concept_doc["_id"] = str(concept_doc["_id"])  # Convert ObjectId for JSON
            # Convert datetime objects to ISO format strings for JSON
            if isinstance(concept_doc.get("created_at"), datetime):
                concept_doc["created_at"] = concept_doc["created_at"].isoformat()
            if isinstance(concept_doc.get("updated_at"), datetime):
                concept_doc["updated_at"] = concept_doc["updated_at"].isoformat()

            # Ensure display name present for frontend using names[] accessor with fallbacks
            try:
                from ..vontology.utils_vontology import (
                    get_concept_display_name_with_names_fallback,
                )

                resolved = get_concept_display_name_with_names_fallback(concept_doc)
                if resolved:
                    concept_doc["name"] = resolved
            except Exception:
                # Legacy fallback from concept_id
                if not concept_doc.get("name"):
                    cid = concept_doc.get("concept_id")
                    if isinstance(cid, str) and cid.startswith("#V#"):
                        concept_doc["name"] = cid[3:].replace("_", " ").title()

            # Get concept type from relationships.is_an_instance_of
            relationships = concept_doc.get("relationships", {})
            instance_of_raw = relationships.get("is_an_instance_of", [])

            # Handle both string and list formats for is_an_instance_of
            if isinstance(instance_of_raw, str):
                # If it's a string, convert to list
                instance_of_list = [instance_of_raw] if instance_of_raw else []
            elif isinstance(instance_of_raw, list):
                # If it's already a list, use as-is
                instance_of_list = instance_of_raw
            else:
                # Fallback for other types
                instance_of_list = []

            # Add concept_id and direct_concept_name for compatibility
            # Keep the concept's own concept_id (don't overwrite it)
            if not concept_doc.get("concept_id"):
                concept_doc["concept_id"] = ""
            concept_doc["direct_concept_name"] = ""
            # Also expose the direct type id for frontend usage (to open the type tab)
            concept_doc["direct_concept_id"] = ""

            if instance_of_list and len(instance_of_list) > 0:
                # Take the first is_an_instance_of as the primary type
                primary_type_id = instance_of_list[0]
                # Expose this ID directly
                try:
                    concept_doc["direct_concept_id"] = str(primary_type_id)
                except Exception:
                    concept_doc["direct_concept_id"] = ""
                # Don't overwrite the concept's own concept_id - that stays as-is

                # Look up the proper name/title for the TYPE (what goes in parentheses)
                try:
                    type_name = get_concept_name_by_id(str(primary_type_id))
                    if type_name:
                        concept_doc["direct_concept_name"] = type_name
                    else:
                        # Fallback: Extract readable name from type concept_id like "#V#person" -> "Person"
                        if isinstance(
                            primary_type_id, str
                        ) and primary_type_id.startswith("#V#"):
                            type_name = primary_type_id[3:].replace("_", " ").title()
                            concept_doc["direct_concept_name"] = type_name
                        else:
                            concept_doc["direct_concept_name"] = str(primary_type_id)
                except Exception as e:
                    logger.warning(
                        f"Could not lookup name for type {primary_type_id}: {e}"
                    )
                    # Fallback: Extract readable name from type concept_id
                    if isinstance(primary_type_id, str) and primary_type_id.startswith(
                        "#V#"
                    ):
                        type_name = primary_type_id[3:].replace("_", " ").title()
                        concept_doc["direct_concept_name"] = type_name
                    else:
                        concept_doc["direct_concept_name"] = str(primary_type_id)

            concepts_list.append(concept_doc)

        logger.debug(
            f"List concepts: query={query}, found {len(concepts_list)} concepts, total_count={total_count}"
        )
        return concepts_list, total_count
    except PyMongoError as e:
        logger.error(f"Database error in list_concepts: {e}", exc_info=True)
        # Propagate as a service-level exception
        raise ConceptServiceError(f"Database error listing concepts: {str(e)}")
    except Exception as e:  # Catch any other unexpected errors
        logger.error(f"Unexpected error in list_concepts: {e}", exc_info=True)
        raise ConceptServiceError(
            f"An unexpected error occurred while listing concepts: {str(e)}"
        )


def iter_concept_ids() -> Iterable[str]:
    """Yield concept_id values for all visible concepts.

    This avoids pagination/count overhead when you need to scan the full set.
    """
    concepts_coll = ConceptsRepository.collection()
    if concepts_coll is None:
        raise ConceptServiceError("Database collection 'concepts' not available.")

    query: MongoQuery = apply_concept_query_filter({})
    cursor = ConceptsRepository.find(query, {"concept_id": 1})
    for concept_doc in cursor:
        cid = concept_doc.get("concept_id")
        if isinstance(cid, str) and cid:
            yield cid


def iter_concept_relationship_docs() -> Iterable[Dict[str, Any]]:
    """Yield concept_id, relationships, and metadata for all visible concepts."""
    concepts_coll = ConceptsRepository.collection()
    if concepts_coll is None:
        raise ConceptServiceError("Database collection 'concepts' not available.")

    query: MongoQuery = apply_concept_query_filter({})
    cursor = ConceptsRepository.find(
        query, {"concept_id": 1, "relationships": 1, "metadata": 1}
    )
    for concept_doc in cursor:
        yield concept_doc


# REFACTORING_NOTE: Functions for 'user_concept_tracking' collection will go here.


def track_concept_access(
    user_identifier: str,
    concept_identifier: str,  # vontology_path for "type", string _id for "individual"
    concept_kind: str,  # "type" or "individual"
    vontology_path: str,  # Full vontology path (e.g., "Concept/Person" or "Concept/Software/Project")
    concept_name_for_display: str,
) -> Dict[str, Any]:
    """Tracks access to an concept (type or individual) for a user.
    Creates a new tracking record or updates an existing one.
    Updates last_accessed_timestamp and increments access_count.
    REFACTORING_NOTE: Implements the first part of Sub-Task 3.3.
    Adheres to the schema defined in Sub-Task 1.1 of concept_refactoring.md.
    """
    tracking_coll = get_user_concept_tracking_collection_service()
    if tracking_coll is None:  # MODIFIED
        raise ConceptServiceError(
            "Database collection 'user_concept_tracking' not available."
        )

    if not all(
        [
            user_identifier,
            concept_identifier,
            concept_kind,
            vontology_path,
            concept_name_for_display,
        ]
    ):
        raise InvalidConceptDataError(
            "Missing required fields for tracking concept access."
        )

    if concept_kind not in ["type", "individual"]:
        raise InvalidConceptDataError(
            f"Invalid concept_kind: '{concept_kind}'. Must be 'type' or 'individual'."
        )

    now = datetime.now(timezone.utc)

    query: MongoQuery = {
        "user_identifier": user_identifier,
        "concept_identifier": concept_identifier,
        "concept_kind": concept_kind,
    }
    # Prepare update payload using the provided parameters
    now = datetime.now(timezone.utc)
    update_payload = {
        "$set": {
            "concept": {
                "_id": None,
                "name": concept_name_for_display,
                "notes": "",
                "concept_id": concept_identifier,
            },
            "last_accessed_timestamp": now,
        },
        "$inc": {"access_count": 1},
    }

    try:
        # Use upsert to create record if it doesn't exist and return the updated document
        updated_tracking_record = tracking_coll.find_one_and_update(
            query, update_payload, upsert=True, return_document=ReturnDocument.AFTER
        )
        if not updated_tracking_record:
            raise ConceptServiceError(
                "Failed to update or create concept tracking record."
            )
        # Normalize _id to string for callers
        if "_id" in updated_tracking_record:
            updated_tracking_record["id"] = str(updated_tracking_record.pop("_id"))
        return updated_tracking_record
    except Exception as e:
        logger.error(f"Error tracking concept access in database: {e}", exc_info=True)
        raise ConceptServiceError(f"Error tracking concept access in database: {e}")


def get_recent_concepts(
    user_identifier: str,
    limit: int = 10,
    concept_kind_filter: Optional[str] = None,  # Optional: "type" or "individual"
) -> List[Dict[str, Any]]:
    """Retrieves a list of recently accessed concepts for a user, sorted by last_accessed_timestamp.
    REFACTORING_NOTE: Implements part of Sub-Task 3.3.
    Allows optional filtering by concept_kind.
    """
    tracking_coll = get_user_concept_tracking_collection_service()
    if tracking_coll is None:  # MODIFIED
        raise ConceptServiceError(
            "Database collection 'user_concept_tracking' not available."
        )

    if not user_identifier:
        raise InvalidConceptDataError(
            "User identifier is required to fetch recent concepts."
        )

    if limit <= 0:
        raise InvalidConceptDataError(
            "Limit for recent concepts must be a positive integer."
        )

    query: MongoQuery = {"user_identifier": user_identifier}
    if concept_kind_filter:
        if concept_kind_filter not in ["type", "individual"]:
            raise InvalidConceptDataError(
                f"Invalid concept_kind_filter: '{concept_kind_filter}'. Must be 'type' or 'individual'."
            )
        query["concept_kind"] = concept_kind_filter

    try:
        recent_concepts = list(
            tracking_coll.find(query)
            .sort("last_accessed_timestamp", -1)  # -1 for pymongo.DESCENDING
            .limit(limit)
        )
        return recent_concepts
    except Exception as e:
        # Log the error e
        raise ConceptServiceError(
            f"Error retrieving recent concepts from database: {e}"
        )


# REFACTORING_NOTE: This function was previously named set_key_concept_status.
# Renaming to mark_concept_as_key to match the test expectation.
def mark_concept_as_key(
    user_identifier: str, concept_identifier: str, concept_kind: str, is_key: bool
) -> Dict[str, Any]:
    """Sets or unsets an concept as a key concept for a user.
    REFACTORING_NOTE: Implements part of Sub-Task 3.3.
    """
    tracking_coll = get_user_concept_tracking_collection_service()
    if tracking_coll is None:  # MODIFIED
        raise ConceptServiceError(
            "Database collection 'user_concept_tracking' not available."
        )

    if not all([user_identifier, concept_identifier, concept_kind]):
        raise InvalidConceptDataError(
            "Missing required fields for setting key concept status."
        )

    if concept_kind not in ["type", "individual"]:
        raise InvalidConceptDataError(
            f"Invalid concept_kind: '{concept_kind}'. Must be 'type' or 'individual'."
        )

    query: MongoQuery = {
        "user_identifier": user_identifier,
        "concept_identifier": concept_identifier,
        "concept_kind": concept_kind,
    }

    # REFACTORING_NOTE: Added last_accessed_timestamp update when marking as key,
    # as this is a significant interaction.
    now = datetime.now(timezone.utc)
    update_payload = {
        "$set": {"is_key_concept": is_key, "last_accessed_timestamp": now}
    }

    try:
        # REFACTORING_NOTE: Using find_one_and_update to get the updated document.
        # If the record doesn't exist, it won't be created (upsert=False by default).
        # This implies an concept must have been accessed at least once to be marked as key.
        updated_record = tracking_coll.find_one_and_update(
            query, update_payload, return_document=ReturnDocument.AFTER
        )
        if not updated_record:
            # REFACTORING_NOTE: Consider if this should create the record if it doesn't exist.
            # For now, it assumes the record must exist (i.e., concept was accessed).
            raise ConceptNotFoundError(
                f"Tracking record not found for user '{user_identifier}', "
                f"concept '{concept_identifier}' (kind: {concept_kind}). Cannot mark as key."
            )
        # Convert ObjectId to string if present
        if updated_record and "_id" in updated_record:
            updated_record["id"] = str(updated_record.pop("_id"))
        return updated_record
    except ConceptNotFoundError:  # Re-raise specific error
        raise
    except Exception as e:
        # Log the error e
        logger.error(
            f"Error marking concept as key for user '{user_identifier}', concept '{concept_identifier}': {e}",
            exc_info=True,
        )
        raise ConceptServiceError(f"Error setting key concept status in database: {e}")


def get_key_concepts(
    user_identifier: str,
    limit: int = 20,
    concept_kind_filter: Optional[str] = None,  # Optional: "type" or "individual"
) -> List[Dict[str, Any]]:
    """Retrieves a list of key concepts for a user.
    REFACTORING_NOTE: Implements part of Sub-Task 3.3.
    Allows optional filtering by concept_kind.
    """
    tracking_coll = get_user_concept_tracking_collection_service()
    if tracking_coll is None:  # MODIFIED
        raise ConceptServiceError(
            "Database collection 'user_concept_tracking' not available."
        )

    query: MongoQuery = {"user_identifier": user_identifier, "is_key_concept": True}
    if concept_kind_filter:
        if concept_kind_filter not in ["type", "individual"]:
            raise InvalidConceptDataError(
                f"Invalid concept_kind_filter: '{concept_kind_filter}'. Must be 'type' or 'individual'."
            )
        query["concept_kind"] = concept_kind_filter

    try:
        key_concepts = list(
            tracking_coll.find(query)
            .sort(
                "last_accessed_timestamp", -1
            )  # Sort by most recently accessed among key concepts
            .limit(limit)
        )
        return key_concepts
    except Exception as e:
        # Log the error e
        raise ConceptServiceError(f"Error retrieving key concepts from database: {e}")


def get_concept_tracking_info(
    user_identifier: str, concept_identifier: str, concept_kind: str
) -> Optional[Dict[str, Any]]:
    """Retrieves a specific concept tracking record for a user.
    Raises ConceptNotFoundError if the record is not found.
    """
    tracking_coll = get_user_concept_tracking_collection_service()
    if tracking_coll is None:  # MODIFIED
        raise ConceptServiceError(
            "Database collection 'user_concept_tracking' not available."
        )

    if not all([user_identifier, concept_identifier, concept_kind]):
        raise InvalidConceptDataError(
            "User identifier, concept identifier, and concept kind are required for get_concept_tracking_info."
        )

    if concept_kind not in ["type", "individual"]:
        raise InvalidConceptDataError(
            f"Invalid concept_kind: '{concept_kind}'. Must be 'type' or 'individual'."
        )

    query: MongoQuery = {
        "user_identifier": user_identifier,
        "concept_identifier": concept_identifier,
        "concept_kind": concept_kind,
    }

    try:
        tracking_record = tracking_coll.find_one(query)
        if not tracking_record:
            raise ConceptNotFoundError(
                f"Tracking record not found for user '{user_identifier}', "
                f"concept '{concept_identifier}' (kind: {concept_kind})."
            )
        # Convert ObjectId to string if present, similar to other service functions
        if tracking_record and "_id" in tracking_record:
            tracking_record["id"] = str(tracking_record.pop("_id"))
        return tracking_record
    except ConceptNotFoundError:  # Re-raise specific error
        raise
    except Exception as e:
        logger.error(
            f"Error retrieving concept tracking info for user '{user_identifier}', concept '{concept_identifier}': {e}",
            exc_info=True,
        )
        raise ConceptServiceError(
            f"Error retrieving concept tracking info from database: {e}"
        )


# REFACTORING_NOTE: Helper functions to get collections.
# These could be expanded with error handling or logging if needed.


def get_concept_display_name(concept_doc):
    """Get a readable display name for a concept using the centralized accessor with language fallback."""
    try:
        from ..vontology.utils_vontology import (
            get_concept_display_name_with_names_fallback,
        )

        return get_concept_display_name_with_names_fallback(concept_doc)
    except Exception:
        # Legacy fallbacks
        if concept_doc.get("name"):
            return concept_doc["name"]
        concept_id = concept_doc.get("concept_id", "")
        if concept_id and isinstance(concept_id, str) and concept_id.startswith("#V#"):
            name_part = concept_id[3:]
            return name_part.replace("_", " ").title()
        return str(concept_doc.get("_id", "Unknown"))


def suggest_concepts_for_text(text: str, limit: int = 6):
    """Return a short list of candidate concepts matching `text`.

    Helper for the annotations pipeline. Uses the unified search service which properly
    handles both legacy schema (name, names[]) and modern schema (text_relations).
    Returns a list of dicts with keys `concept_id` and `name`.
    """
    try:
        if not text or not isinstance(text, str):
            return []

        # Import here to avoid circular dependency
        from .concept_search_service import search_concepts as search_concepts_unified

        # Use unified search with two-pass strategy (prefix first, substring fallback)
        search_result = search_concepts_unified(
            query=text,
            match_type="substring",
            include_description=False,  # Name search only for annotations
            limit=limit,
            use_two_pass=True,  # Prefer prefix matches, fallback to substring
        )

        # Transform to expected format for annotations pipeline
        results = []
        for result in search_result.get("results", []):
            results.append(
                {"concept_id": result.get("concept_id"), "name": result.get("name")}
            )

        return results[:limit]
    except Exception as e:
        logger.exception(f"suggest_concepts_for_text failed for '{text}': {e}")
        return []


# Backward compatibility alias (some routes/tests still reference concept_service.search_concepts)
search_concepts = suggest_concepts_for_text


def get_concept_name_by_id(concept_id: str) -> Optional[str]:
    """Look up a display name for a concept by its concept_id.
    Prefers names[] NL entry, then legacy 'name', then concept_id-derived (metadata.title deprecated).
    """
    try:
        concepts_coll = ConceptsRepository.collection()
        if concepts_coll is None:  # Fixed: proper None check for collection
            return None

        # Find the concept by concept_id
        concept = concepts_coll.find_one({"concept_id": concept_id})
        if concept:
            try:
                from ..vontology.utils_vontology import (
                    get_concept_display_name_with_names_fallback,
                )

                nm = get_concept_display_name_with_names_fallback(concept)
                if nm:
                    return nm
            except Exception:
                # Legacy fallback to top-level name only; metadata.title is deprecated
                nm = concept.get("name")
                if isinstance(nm, str) and nm.strip():
                    return nm.strip()
                cid = concept.get("concept_id")
                if isinstance(cid, str) and cid.startswith("#V#"):
                    return cid[3:].replace("_", " ").title()

        return None
    except Exception as e:
        logger.warning(f"Error looking up concept name for {concept_id}: {e}")
        return None


def get_user_concept_tracking_collection_service() -> Optional[Collection]:
    """Helper to get the 'user_concept_tracking' collection.
    REFACTORING_NOTE: Centralizes access to the collection.
    """
    try:
        db = mongo_client.get_db()
        if db is None:
            return None
        return db["user_concept_tracking"]
    except Exception as e:
        logger.error(
            f"Failed to get 'user_concept_tracking' collection: {e}", exc_info=True
        )
        return None


# --- Interaction Session Management ---


def get_interaction_sessions_collection_service() -> Optional[Collection]:
    """Helper to get the 'interaction_sessions' collection."""
    try:
        db = mongo_client.get_db()
        if db is None:
            return None
        return db[interaction_session_collection_name]
    except Exception as e:
        logger.error(
            f"Failed to get 'interaction_sessions' collection: {e}", exc_info=True
        )
        return None


def start_interaction_session(
    concept_id: str, user_id: Optional[str], initial_notes: Optional[str] = None
) -> Dict[str, Any]:
    """Starts a new interaction session with an concept.

    Args:
        concept_id: The ID of the concept to interact with.
        user_id: The ID of the user initiating the session.
        initial_notes: Optional initial notes or context from the user.

    Returns:
        A dictionary containing the interaction ID, concept ID, concept name,
        the first question from the system, and session start time.

    Raises:
        ConceptServiceError: If the concept is not found or session creation fails.
    """
    if not user_id or not isinstance(user_id, str):
        raise ConceptServiceError("user_id is required to start an interaction session")
    logger.info(
        f"Attempting to start interaction session for concept_id: {concept_id} by user_id: {user_id}"
    )

    concepts_coll = ConceptsRepository.collection()

    if concepts_coll is None:
        logger.error("Concepts collection is not available.")
        raise ConceptServiceError("Database connection not available.")

    db = get_db()
    if db is None:
        logger.error("Database connection not available.")
        raise ConceptServiceError("Database connection not available.")

    sessions_coll = db[interaction_session_collection_name]

    # Try to find concept by _id (ObjectId or string) first, then by concept_id as fallback
    concept = None
    actual_concept_id = concept_id

    try:
        obj_id = ObjectId(concept_id)
    except Exception as e:
        obj_id = None
        logger.debug(
            f"start_interaction_session: '{concept_id}' is not a valid ObjectId: {e}"
        )

    # Try by ObjectId
    if obj_id is not None:
        try:
            concept = concepts_coll.find_one({"_id": obj_id})
            if concept:
                logger.info(f"Found concept by ObjectId: {concept_id}")
        except Exception as e:
            logger.warning(f"Could not query concept by ObjectId {concept_id}: {e}")

    # If not found, try string _id
    if not concept:
        try:
            concept = concepts_coll.find_one({"_id": concept_id})
            if concept:
                actual_concept_id = str(concept.get("_id", concept_id))
                logger.info(f"Found concept by string _id: {actual_concept_id}")
        except Exception as e:
            logger.warning(f"Could not query concept by string _id {concept_id}: {e}")

    # If still not found by _id, try to find by concept_id
    if not concept:
        try:
            # Check if this looks like a concept_id or try common patterns
            concept_id_candidates = []

            # If it doesn't start with #V#, try adding the prefix
            if not concept_id.startswith("#V#"):
                concept_id_candidates.append(f"#V#{concept_id}")
            else:
                concept_id_candidates.append(concept_id)

            # Try finding by concept_id
            for candidate in concept_id_candidates:
                concept = concepts_coll.find_one({"concept_id": candidate})
                if concept:
                    actual_concept_id = str(concept["_id"])
                    logger.info(
                        f"Found concept by concept_id {candidate}, actual ObjectId: {actual_concept_id}"
                    )
                    break

        except Exception as e:
            logger.warning(f"Could not find concept by concept_id: {e}")

    if not concept:
        logger.warning(
            f"concept not found with ID {concept_id} when trying to start interaction session."
        )
        raise ConceptServiceError(f"concept not found with ID {concept_id}.")

    # Derive composite namespace for the session (user@org isolation for RAG/search)
    # Phase 1: Support basic user@org namespace with stubbed roles
    import os
    from ..services.namespace_service import derive_namespace
    from ..security.role_resolver import get_user_role

    # Get org context from session (if available)
    org_id = None
    role_in_org = None
    try:
        from flask import session as flask_session

        if flask_session:
            org_id = flask_session.get("organisation_concept_id")
            role_in_org = flask_session.get("role_in_org")
    except Exception:
        pass

    # Derive namespace using user and org context
    try:
        # Clean user_id for namespace
        user_slug = user_id.strip().lower().replace(" ", "_")

        # If org_id available, derive composite namespace; otherwise user-only
        if org_id:
            session_namespace = derive_namespace(user_slug, org_id)
            # Get role from resolver if not in session
            if not role_in_org:
                try:
                    role_in_org = get_user_role(user_slug, org_id)
                except Exception:
                    role_in_org = "member"  # Default fallback
        else:
            session_namespace = derive_namespace(user_slug)
            # No org context, no role
    except Exception as e:
        logger.warning(
            f"Failed to derive composite namespace: {e}, falling back to default"
        )
        session_namespace = os.environ.get(
            "VON_DEFAULT_NAMESPACE", "#V#default_namespace"
        )
        org_id = None
        role_in_org = None

    session_doc = {
        "concept_id": ObjectId(actual_concept_id),
        "user_id": user_id,
        "organisation_concept_id": org_id,  # Phase 1: org context
        "role_in_org": role_in_org,  # Phase 1: stubbed role
        "start_time": datetime.now(timezone.utc),  # Use timezone.utc
        "last_updated_time": datetime.now(timezone.utc),  # Use timezone.utc
        "status": "active",
        "history": [],
        "indexing_status": "pending",
        "namespace": session_namespace,  # Composite: #V#user@org or #V#user
    }

    if initial_notes:
        session_doc["history"].append(
            InteractionEntry(
                interaction_type="user_provided_initial_notes",
                details={"notes": initial_notes},
                timestamp=datetime.now(timezone.utc),  # Use timezone.utc
            ).model_dump()
        )

    try:
        result: InsertOneResult = sessions_coll.insert_one(session_doc)
        inserted_session_id = result.inserted_id
        logger.info(
            f"Started interaction session {inserted_session_id} for concept {actual_concept_id}"
        )
    except Exception as e:
        logger.error(
            f"Error starting interaction session for concept {actual_concept_id}: {e}"
        )
        raise ConceptServiceError(f"Could not start interaction session: {e}") from e

    # No static system question - the frontend will generate the first question dynamically
    # using the current concept state (name, type, notes) via the LLM

    return {
        "interaction_id": str(inserted_session_id),
        "concept_id": actual_concept_id,
        "concept_name": concept.get("name", "Unknown concept"),
        "session_start_time": session_doc["start_time"].isoformat(),
    }


def get_active_interactions_for_concept(
    concept_id: str, user_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Retrieves active interaction sessions for a specific concept and user.

    Args:
        concept_id: The ID of the concept to check for active interactions
        user_id: The ID of the user (defaults to "default_user")

    Returns:
        List of active interaction session documents

    Raises:
        ConceptServiceError: If there's a database connection error
    """
    if not user_id or not isinstance(user_id, str):
        raise ConceptServiceError("user_id is required to fetch active interactions")
    logger.info(
        f"Checking for active interactions for concept_id: {concept_id}, user_id: {user_id}"
    )
    db = get_db()
    if db is None:
        logger.error("Database connection not available.")
        raise ConceptServiceError("Database connection not available.")

    sessions_coll = db[interaction_session_collection_name]

    try:
        # Find active sessions for this concept and user
        query: dict[str, Any] = {
            "concept_id": ObjectId(concept_id),
            "user_id": user_id,
        }

        # If an organisation is selected in the current request context, scope
        # active interactions to that organisation.
        try:
            from flask import session as flask_session

            org_id = flask_session.get("organisation_concept_id")
            if org_id:
                query["organisation_concept_id"] = org_id
        except Exception:
            pass

        active_sessions = list(
            sessions_coll.find(query).sort("last_updated_time", -1)
        )  # Most recent first

        # Convert ObjectId to string for JSON serialization
        for session in active_sessions:
            session["_id"] = str(session["_id"])
            session["concept_id"] = str(session["concept_id"])
            if isinstance(session.get("start_time"), datetime):
                session["start_time"] = session["start_time"].isoformat()
            if isinstance(session.get("last_updated_time"), datetime):
                session["last_updated_time"] = session["last_updated_time"].isoformat()

        logger.info(
            f"Found {len(active_sessions)} active interactions for concept {concept_id}"
        )
        return active_sessions

    except Exception as e:
        logger.error(
            f"Error retrieving active interactions for concept {concept_id}: {e}"
        )
        raise ConceptServiceError(f"Could not retrieve active interactions: {e}") from e


def resume_interaction_session(
    interaction_id: str,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Resumes an existing interaction session by returning its current state.

    Args:
        interaction_id: The ID of the interaction session to resume

    Returns:
        Dictionary containing the current session state including last question,
        concept info, and conversation history

    Raises:
        ConceptServiceError: If session not found or database error
    """
    logger.info(f"Resuming interaction session: {interaction_id}")

    try:
        session = get_interaction_session_by_id(interaction_id, user_id=user_id)
        if not session:
            raise ConceptServiceError(f"Interaction session {interaction_id} not found")

        # Get the concept for this session
        concept_id = str(session.get("concept_id"))
        concept = get_concept_by_id(concept_id)
        if not concept:
            raise ConceptServiceError(
                f"concept not found for interaction session {interaction_id}"
            )

        # Find the last question/statement from the history
        last_question = "Continue the conversation..."  # Default fallback
        history = session.get("history", [])

        for entry in reversed(history):
            if entry.get("interaction_type") in ["llm_question", "llm_statement"]:
                details = entry.get("details", {})
                last_question = details.get("question") or details.get(
                    "statement", last_question
                )
                break

        # Format recent history for display
        recent_history = history[-6:] if len(history) > 6 else history
        formatted_history = []
        for entry in recent_history:
            entry_type = entry.get("interaction_type", "")
            details = entry.get("details", {})

            if entry_type == "user_answer":
                formatted_history.append(f"You: {details.get('answer', '')}")
            elif entry_type in ["llm_question", "llm_statement"]:
                content = details.get("question") or details.get("statement", "")
                formatted_history.append(f"AI: {content}")

        return {
            "interaction_id": interaction_id,
            "concept_id": concept_id,
            "concept_name": concept.get("name", "Unknown concept"),
            "concept_notes": get_concept_notes(concept) or "",
            "last_question": last_question,
            "conversation_history": formatted_history,
            "session_start_time": session.get("start_time"),
            "last_updated_time": session.get("last_updated_time"),
            "status": "resumed",
        }

    except Exception as e:
        logger.error(f"Error resuming interaction session {interaction_id}: {e}")
        raise ConceptServiceError(f"Could not resume interaction session: {e}") from e


# src/backend/services/concept_service.py  (keep it in the same module)


def generate_concept_question(
    *,  # all-keyword args ⇒ easier to read
    concept: dict,
    db: Database,
    interaction_history: str = "",
    user_answer: str = "",
    is_initial: bool = True,
) -> str:
    """
    Return a fully-rendered prompt for the LLM.
    - Pulls (or stores) the prompt_template on the concept's Vontology type
    - Handles person vs. non-person pronouns
    - Falls back to a default template if none exists and **persists it** once
    """
    # ---------- gather basics ----------
    concept_id = str(concept.get("id") or concept.get("_id"))
    concept_id = (
        concept.get("concept_id")
        or (concept.get("vontology_path") or [THING_PRIMARY_ID])[-1]
    )
    notes = get_concept_notes(concept) or "No information recorded yet."
    name = concept.get("name", "this concept")

    # ---------- fetch or create template ----------
    template: str | None = None
    vontology_node = None
    try:
        nodes = get_concept_details_from_db(concept_name_or_id=concept_id)
        if nodes:
            vontology_node = nodes[0]
            template = (vontology_node.get("attributes") or {}).get("prompt_template")
    except Exception as e:
        logger.warning(f"Could not fetch Vontology node for {concept_id}: {e}")

    # ---------- gather descriptive material (notes + type description) ----------
    material = gather_descriptive_material(
        concept, vontology_node, include_notes_in_block=True
    )
    concept_context = material.get("context_block", "")
    # Log the context block that will be injected (or prepended) into the prompt
    try:
        logger.info(
            "[PromptContext] concept_id=%s is_initial=%s context_block:\n%s",
            concept_id,
            is_initial,
            (concept_context if concept_context else "(empty)"),
        )
    except Exception:
        pass

    # ---------- get user and organization context ----------
    user_org_context = ""
    try:
        from ..server.routes.settings_routes import get_all_settings_data

        settings = get_all_settings_data()

        context_parts = []
        current_user_name = settings.get("current_user_person_name")
        if current_user_name:
            context_parts.append(f"Current user: {current_user_name}")

        current_org_name = settings.get("current_organisation_name")
        if current_org_name:
            context_parts.append(f"Organization: {current_org_name}")

        if context_parts:
            user_org_context = f"User context: {' | '.join(context_parts)}\n"
    except Exception as e:
        logger.warning(f"Could not retrieve user/organization context: {e}")

    if template is None:
        # one default covers both initial & follow-up (place-holders allow omission)
        template = (
            "{user_org_context}"
            "{concept_context}"
            "Context: Interaction with an concept of type '{concept_type}'.\n"
            "concept name: {concept_name}\n"
            "concept's current notes: {concept_notes}\n"
            "{history_section}"
            "{answer_section}"
            "Task: Formulate a question that deepens knowledge about this concept. "
            "Respond with ONLY the question."
        )
        # save once
    if vontology_node and not is_thing_id(concept_id):
        try:
            update_vontology_node_in_db(
                concept_id=concept_id,
                update_data={"attributes.prompt_template": template},
            )
        except Exception as e:
            logger.error(f"Unable to persist default template on {concept_id}: {e}")

    # ---------- person / pronoun logic ----------
    is_person = concept_id == "#V#person"
    current_user_concept_id = None
    is_current_user = False
    try:
        from flask import session, has_request_context

        if has_request_context():
            current_user_concept_id = session.get("user_concept_id")
            is_current_user = is_person and (current_user_concept_id == concept_id)
        else:
            # Outside request (e.g. unit tests) – treat as not current user
            is_current_user = False
    except Exception:
        is_current_user = False

    if is_person and not is_current_user:
        template += "\nNote: Do not refer to {concept_name} as 'you'."
    elif is_current_user:
        template += "\nNote: Second-person pronouns are allowed for the current user."

    # ---------- fill placeholders ----------
    history_section = (
        f"Ongoing interaction history (last few exchanges):\n{interaction_history}\n\n"
        if interaction_history
        else ""
    )
    answer_section = (
        f"User's latest input: {user_answer}\n\n"
        if (not is_initial and user_answer)
        else ""
    )

    # Check if template has user_org_context placeholder, if not add it at the beginning
    if "{user_org_context}" not in template and user_org_context:
        template = user_org_context + template
        user_org_context = ""  # Clear it since we added it directly

    # Inject concept_context if the template doesn't have the placeholder
    if "{concept_context}" not in template and concept_context:
        template = concept_context + template
        concept_context = ""  # Clear to avoid double-substitution

    # Ensure template is a string and not empty; provide a default if needed
    if template is None or not isinstance(template, str) or not template.strip():
        template = (
            "{user_org_context}"
            "{concept_context}"
            "Context: Interaction with an concept of type '{concept_type}'.\n"
            "concept name: {concept_name}\n"
            "concept's current notes: {concept_notes}\n"
            "{history_section}"
            "{answer_section}"
            "Task: Formulate a question that deepens knowledge about this concept. "
            "Respond with ONLY the question."
        )

    try:
        filled = template.format(
            user_org_context=user_org_context,
            concept_context=concept_context,
            concept_type=concept_id,
            concept_name=name,
            concept_notes=notes,
            history_section=history_section,
            answer_section=answer_section,
        )
    except KeyError as e:
        # Handle templates that don't have all placeholders
        logger.warning(f"Template missing placeholder {e}, using fallback approach")
        filled = template
        # Manual replacement for basic placeholders
        filled = filled.replace("{concept_type}", concept_id)
        filled = filled.replace("{concept_name}", name)
        filled = filled.replace("{concept_notes}", notes)
        filled = filled.replace("{history_section}", history_section)
        filled = filled.replace("{answer_section}", answer_section)
        filled = filled.replace("{user_org_context}", user_org_context)
        filled = filled.replace("{concept_context}", concept_context)
        # If user_org_context wasn't in template, prepend it
        if user_org_context and not filled.startswith(user_org_context.strip()):
            filled = user_org_context + filled
        # If concept_context wasn't in template and not injected earlier, prepend it
        if concept_context and not filled.startswith(concept_context.strip()):
            filled = concept_context + filled

    # Log the full prompt that will be sent to the LLM
    try:
        logger.info(
            "[LLMPrompt] concept_id=%s is_initial=%s full_prompt:\n%s",
            concept_id,
            is_initial,
            filled,
        )
    except Exception:
        pass

    logger.debug(f"Prompt generated ({len(filled)} chars)")
    return filled


def submit_concept_answer(interaction_id: str, user_answer: str, session: dict) -> dict:
    """
    Submits a user's answer during an concept interaction, gets the LLM's next question or conclusion,
    and updates the interaction state.
    The prompt template is dynamically fetched from the concept's Vontology type.
    If the type has no template, a default is used and then stored for that type.
    """
    logger.info(f"Submitting answer for interaction_id: {interaction_id}")
    if not interaction_id or not isinstance(interaction_id, str):
        logger.error("Invalid interaction_id provided.")
        return {"error": "Invalid interaction_id", "status": "error_validation"}

    if not isinstance(user_answer, str):  # Basic check, consider more validation
        logger.error("Invalid user_answer provided (not a string).")
        return {
            "error": "Invalid user_answer (must be a string)",
            "status": "error_validation",
        }

    if (
        not isinstance(session, dict)
        or "concept_id" not in session
        or "history" not in session
    ):
        logger.error(
            f"Invalid session object provided for interaction_id: {interaction_id}. Session: {session}"
        )
        return {"error": "Invalid session object", "status": "error_validation"}

    concept_id_from_session = session.get("concept_id")
    if not concept_id_from_session:  # Should not happen if session validation is robust
        logger.error(
            f"concept_id missing from session for interaction_id: {interaction_id}"
        )
        return {
            "error": "concept_id missing from session",
            "status": "error_session_data",
        }

    # Ensure concept_id is a string for get_concept_by_id, if it's an ObjectId from the session
    concept_id_str = str(concept_id_from_session)

    db = get_db()
    if db is None:
        logger.error("Database connection not available for submit_concept_answer.")
        return {
            "error": "Database connection not available",
            "status": "error_db_connection",
        }

    interactions_collection = db[interaction_session_collection_name]

    concept = get_concept_by_id(
        concept_id_str
    )  # Assumes get_concept_by_id handles ObjectId conversion or takes str
    if not concept:
        logger.error(
            f"concept not found for concept_id: {concept_id_str} in session {interaction_id}"
        )
        return {
            "error": "concept not found for interaction",
            "status": "error_concept_not_found",
        }

    concept_type_concept_id = concept.get("concept_id")
    if not concept_type_concept_id:
        v_path = concept.get("vontology_path")
        if v_path and isinstance(v_path, list) and len(v_path) > 0:
            concept_type_concept_id = v_path[-1]
            logger.info(
                f"Derived concept_type_concept_id '{concept_type_concept_id}' from vontology_path for concept {concept_id_str}"
            )
        else:
            logger.warning(
                f"concept {concept_id_str} has no 'concept_id' or derivable 'vontology_path'. Using a generic type for prompt."
            )
            # Fallback to a generic type or handle as an error if type is strictly required
            concept_type_concept_id = THING_PRIMARY_ID  # Example generic fallback
            # return {"error": "concept type (concept_id or vontology_path) not found", "status": "error_concept_config"}

    # Template handling is now done by the unified generate_concept_question function
    logger.info(f"concept type for prompt: {concept_type_concept_id}")
    logger.info(f"concept ID for prompt: {concept_id_str}")

    now_utc = datetime.now(timezone.utc)
    user_answer_interaction_entry = InteractionEntry(
        interaction_type="user_answer",
        details={"answer": user_answer},
        timestamp=now_utc,
    )
    # Ensure session["history"] is a list before appending
    if not isinstance(session.get("history"), list):
        session["history"] = []
    current_history = session[
        "history"
    ]  # This is a reference, changes will reflect in session dict
    current_history.append(user_answer_interaction_entry.model_dump())

    # Find the previous question from history for synthesis
    previous_question = "What would you like to tell me?"  # Default fallback
    for entry in reversed(
        current_history[:-1]
    ):  # Look backwards, excluding the answer we just added
        if entry.get("interaction_type") in ["llm_question", "llm_statement"]:
            question_details = entry.get("details", {})
            previous_question = question_details.get(
                "question"
            ) or question_details.get("statement", previous_question)
            break

    # Get concept type name for later use in synthesis and prompt
    concept_type_name_for_prompt = concept_type_concept_id  # Default to ID
    if concept.get("name"):  # Fallback to concept name if type name is not available
        concept_type_name_for_prompt = (
            f"{concept.get('name')} (instance of {concept_type_concept_id})"
        )

    # IMPORTANT: Do synthesis FIRST before generating the next question
    # This ensures the next question is based on updated notes that include the current synthesis
    synthesis_result = None
    try:
        from ..languagemodels.llm_interface import get_llm_client, get_active_model_name

        llm_client = get_llm_client()
        selected_model = get_active_model_name()

        # Ensure model is a string; provide a safe default if None
        safe_model = selected_model or "default"
        synthesis_result = synthesize_and_update_concept_notes(
            concept_id=concept_id_str,
            concept=concept,
            question_or_statement=previous_question,
            user_answer=user_answer,
            concept_type_name=concept_type_name_for_prompt,
            ollama_client=llm_client,  # Pass the unified LLM client
            model=safe_model,
            session=session,  # Pass session to access initial notes
        )

        # Fetch the updated concept from the database to ensure we have the latest notes
        if synthesis_result:  # Only re-fetch if synthesis actually happened
            try:
                updated_concept = get_concept_by_id(concept_id_str)
                if updated_concept:
                    concept = updated_concept  # Use the updated concept for the rest of the process
                    concept_notes = get_concept_notes(concept) or ""
                    try:
                        logger.info(
                            f"Re-fetched concept after synthesis. Updated notes length: {len(concept_notes)}"
                        )
                        logger.info(
                            f"Re-fetched concept notes preview: {concept_notes[:200]}..."
                        )
                    except Exception:
                        # In case concept_notes is not a string-like, coerce for logging
                        concept_notes_str = (
                            str(concept_notes) if concept_notes is not None else ""
                        )
                        logger.info(
                            f"Re-fetched concept after synthesis. Updated notes length: {len(concept_notes_str)}"
                        )
                        logger.info(
                            f"Re-fetched concept notes preview: {concept_notes_str[:200]}..."
                        )
                else:
                    logger.warning(
                        f"Could not re-fetch concept {concept_id_str} after synthesis"
                    )
            except Exception as fetch_e:
                logger.error(f"Error re-fetching concept after synthesis: {fetch_e}")
                # Continue with the original concept

    except Exception as e:
        logger.error(f"Error generating or updating concept notes synthesis: {e}")
        # Continue without failing the entire interaction

    # NOW get the updated concept notes (after synthesis) for generating the next question
    concept_notes = get_concept_notes(concept) or ""
    logger.info(
        f"Using concept notes for LLM prompt. Notes length: {len(concept_notes)}"
    )
    logger.info(f"concept notes preview for LLM: {concept_notes[:200]}...")

    recent_history_entries = current_history[-6:]  # Get user_answer + last 5 (or fewer)
    interaction_history_formatted = "\\n".join(
        [
            f"{entry.get('interaction_type', 'Log')}: {entry.get('details', {}).get('question', entry.get('details', {}).get('answer', 'N/A'))}"
            for entry in recent_history_entries
        ]
    )

    # concept_type_name_for_prompt was already calculated above, no need to recalculate

    try:
        final_prompt_for_llm = generate_concept_question(
            concept=concept,
            db=db,
            interaction_history=interaction_history_formatted,
            user_answer=user_answer,
            is_initial=False,
        )
        logger.info(
            f"Generated final prompt using unified function (full):\n{final_prompt_for_llm}"
        )
    except Exception as e:
        logger.error(
            f"Error generating prompt with unified function: {e}", exc_info=True
        )
        return {
            "error": "Failed to generate prompt",
            "status": "error_prompt_generation",
        }

    llm_response_text = None
    try:
        # Use unified LLM interface instead of hardcoded Ollama
        from ..languagemodels.llm_interface import get_llm_client, get_active_model_name

        # Get the configured LLM client and model
        llm_client = get_llm_client()
        selected_model = get_active_model_name()

        # Use the correct generate method with prompt parameter
        raw_llm_response = llm_client.generate(
            prompt=final_prompt_for_llm, model=selected_model
        )
        # The generate method returns a string directly
        if isinstance(raw_llm_response, str):
            llm_response_text = raw_llm_response
        else:
            logger.error(
                f"Unexpected LLM response format: {type(raw_llm_response)}. Response: {raw_llm_response}"
            )
            llm_response_text = str(raw_llm_response)  # Fallback to string conversion
            return {
                "error": "Unexpected LLM response format",
                "status": "error_llm_response",
            }

    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error with Ollama service: {e}")
        return {"error": "LLM connection error", "status": "error_llm_connection"}
    except Exception as e:
        logger.error(f"Error during LLM call: {e}", exc_info=True)
        return {"error": "LLM processing error", "status": "error_llm_processing"}

    if not llm_response_text:  # Check after attempting to extract content
        logger.error("LLM returned an empty or unparseable response.")
        return {
            "error": "LLM returned empty or unparseable response",
            "status": "error_llm_empty_response",
        }

    llm_interaction_type = "llm_question"
    llm_details_key = "question"
    if not llm_response_text.strip().endswith("?"):
        llm_interaction_type = "llm_statement"
        llm_details_key = "statement"

    llm_response_interaction_entry = InteractionEntry(
        interaction_type=llm_interaction_type,
        details={llm_details_key: llm_response_text},
        timestamp=datetime.now(timezone.utc),
    )
    current_history.append(llm_response_interaction_entry.model_dump())

    try:
        # Ensure interaction_id is ObjectId for MongoDB query
        interaction_oid = (
            ObjectId(interaction_id)
            if not isinstance(interaction_id, ObjectId)
            else interaction_id
        )

        update_result = interactions_collection.update_one(
            {"_id": interaction_oid},
            {
                "$set": {
                    "history": current_history,  # current_history has already been updated
                    "last_updated_time": datetime.now(timezone.utc),
                    "indexing_status": "pending",
                }
            },
        )
        if update_result.matched_count == 0:
            logger.error(
                f"Interaction session {interaction_id} (OID: {interaction_oid}) not found for update."
            )
            return {
                "error": "Interaction session not found for update",
                "status": "error_db_update_notfound",
            }
        logger.info(f"Successfully updated interaction session {interaction_id}")
    except PyMongoError as e:
        logger.error(
            f"MongoDB error updating interaction session {interaction_id}: {e}"
        )
        return {
            "error": "Failed to update interaction session (DB error)",
            "status": "error_db_update",
        }
    except Exception as e:
        logger.error(
            f"Generic error updating interaction session {interaction_id}: {e}",
            exc_info=True,
        )
        return {
            "error": "Failed to update interaction session (general error)",
            "status": "error_session_update",
        }

    return {
        "next_step_type": llm_interaction_type,
        "next_step_content": llm_response_text,
        "status": "success",
        "interaction_id": interaction_id,  # Return the original string ID
        "synthesis": synthesis_result,  # Include the synthesis result
        "concept": {
            "_id": str(concept.get("_id", concept_id_str)),
            "name": get_concept_display_name_with_names_fallback(concept),
            "notes": get_concept_notes(concept) or "",
            "concept_id": concept.get("concept_id"),
        },
    }


def update_concept_notes(concept_id: str, new_notes: str) -> bool:
    """Update concept notes using text_relations only (Phase 1 preserved_fields deprecation).

    Behaviour:
      - Upsert / reuse a text_value for the supplied notes (predicate 'hasNote').
      - Prune any stale hasNote relations pointing at older text_values.
      - Do NOT write to concept_data.preserved_fields.* (legacy dual-write removed).
    Returns True on success; False on errors or if concept missing.
    """
    try:
        # Confirm concept exists (cheap existence check without mutating preserved_fields)
        concept_result = ConceptsRepository.find_one(
            {"concept_id": concept_id}, projection={"_id": 1}
        )
        if not concept_result:
            logger.warning(
                f"update_concept_notes: No concept found with ID '{concept_id}'. Notes not updated."
            )
            return False

        try:
            tv_payload = upsert_text_for_concept(
                subject_concept_id=concept_id,
                predicate="hasNote",
                text=new_notes,
                lang="en",
                provenance={
                    "source": "update_concept_notes",
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
                context={"write_strategy": "relations_only_v2"},
            )
            new_text_value_id = tv_payload.get("text_value_id")
            relation_id = tv_payload.get("relation_id")
            logger.info(
                f"update_concept_notes: Upserted text_value {new_text_value_id} via relation {relation_id} for concept {concept_id}."
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.error(
                f"Failed creating text_value for notes concept_id={concept_id}: {e}"
            )
            return False

        # Prune stale relations
        try:
            if new_text_value_id:
                stale_filter = {
                    "subject_concept_id": concept_id,
                    "predicate": "hasNote",
                    "object_text_id": {"$ne": new_text_value_id},
                }
                deleted = TextRelationsRepository.delete_many(stale_filter)
                if hasattr(deleted, "deleted_count") and deleted.deleted_count:
                    logger.info(
                        f"update_concept_notes: Removed {deleted.deleted_count} stale hasNote relations for concept {concept_id}."
                    )
        except Exception as e:  # pragma: no cover
            logger.error(
                f"Failed pruning stale hasNote relations for concept {concept_id}: {e}"
            )

        # Touch last_updated_time separately (atomic minimal update)
        try:
            ConceptsRepository.update_one(
                {"concept_id": concept_id},
                {"$set": {"last_updated_time": datetime.now(timezone.utc)}},
            )
        except Exception as e:  # pragma: no cover
            logger.warning(
                f"Non-fatal: failed to update last_updated_time for {concept_id}: {e}"
            )

        logger.info(
            f"Successfully updated notes (relations only) for concept '{concept_id}'."
        )
        return True
    except PyMongoError as e:  # pragma: no cover
        logger.error(
            f"MongoDB error while updating notes for concept '{concept_id}': {e}"
        )
        return False
    except Exception as e:  # pragma: no cover
        logger.error(
            f"Unexpected error while updating notes for concept '{concept_id}': {e}"
        )
        return False


def update_concept_description(concept_id: str, new_description: str) -> bool:
    """Update concept description using text_relations only (Phase 1 preserved_fields deprecation).

    Behaviour:
      - Upsert / reuse a text_value for predicate 'hasDescription'.
      - Prune stale hasDescription relations.
      - Do NOT write to concept_data.preserved_fields.*.
    Read-only prompt concept remains protected by READ_ONLY_PROMPT_CONCEPT.
    Returns True on success, False otherwise.
    """
    from .annotation_extraction_service import (
        PROMPT_CONCEPT_ID,
    )  # local import to avoid cycles
    import os

    if (
        os.environ.get("READ_ONLY_PROMPT_CONCEPT") in ("1", "true", "True")
        and concept_id == PROMPT_CONCEPT_ID
    ):
        logger.warning(
            "update_concept_description: READ_ONLY_PROMPT_CONCEPT set; refusing to overwrite prompt concept %s",
            concept_id,
        )
        return False
    try:
        # Existence check
        concept_result = ConceptsRepository.find_one(
            {"concept_id": concept_id}, projection={"_id": 1}
        )
        if not concept_result:
            logger.warning(
                f"update_concept_description: No concept found with ID '{concept_id}'. Description not updated."
            )
            return False

        try:
            tv_payload = upsert_text_for_concept(
                subject_concept_id=concept_id,
                predicate="hasDescription",
                text=new_description,
                lang="en",
                provenance={
                    "source": "update_concept_description",
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
                context={"write_strategy": "relations_only_v2"},
            )
            new_text_value_id = tv_payload.get("text_value_id")
            relation_id = tv_payload.get("relation_id")
            logger.info(
                f"update_concept_description: Upserted text_value {new_text_value_id} via relation {relation_id} for concept {concept_id}."
            )
        except Exception as e:  # pragma: no cover
            logger.error(
                f"Failed creating text_value for description concept_id={concept_id}: {e}"
            )
            return False

        try:
            if new_text_value_id:
                stale_filter = {
                    "subject_concept_id": concept_id,
                    "predicate": "hasDescription",
                    "object_text_id": {"$ne": new_text_value_id},
                }
                deleted = TextRelationsRepository.delete_many(stale_filter)
                if hasattr(deleted, "deleted_count") and deleted.deleted_count:
                    logger.info(
                        f"update_concept_description: Removed {deleted.deleted_count} stale hasDescription relations for concept {concept_id}."
                    )
        except Exception as e:  # pragma: no cover
            logger.error(
                f"Failed pruning stale hasDescription relations for concept {concept_id}: {e}"
            )

        try:
            ConceptsRepository.update_one(
                {"concept_id": concept_id},
                {"$set": {"last_updated_time": datetime.now(timezone.utc)}},
            )
        except Exception as e:  # pragma: no cover
            logger.warning(
                f"Non-fatal: failed to update last_updated_time for {concept_id}: {e}"
            )

        logger.info(
            f"Successfully updated description (relations only) for concept '{concept_id}'."
        )
        return True
    except PyMongoError as e:  # pragma: no cover
        logger.error(
            f"MongoDB error while updating description for concept '{concept_id}': {e}"
        )
        return False
    except Exception as e:  # pragma: no cover
        logger.error(
            f"Unexpected error while updating description for concept '{concept_id}': {e}"
        )
        return False


def get_interaction_session_by_id(
    interaction_id: str,
    user_id: Optional[str] = None,
    organisation_concept_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieves an interaction session by its ID.

    Args:
        interaction_id: The ID of the interaction session to retrieve.

    Returns:
        The interaction session document as a dictionary, or None if not found.

    Raises:
        ConceptServiceError: If there's a database connection error.
    """
    logger.info(f"Retrieving interaction session with ID: {interaction_id}")
    db = get_db()
    if db is None:
        logger.error("Database connection not available.")
        raise ConceptServiceError("Database connection not available.")

    sessions_coll = db[interaction_session_collection_name]

    query: dict[str, Any]
    try:
        query = {"_id": ObjectId(interaction_id)}
    except Exception as e:
        logger.error(
            f"Error retrieving interaction session with ID {interaction_id}: {e}"
        )
        raise ConceptServiceError(
            f"Invalid interaction ID format or database error for ID {interaction_id}."
        ) from e

    if user_id:
        query["user_id"] = user_id

    if organisation_concept_id is None:
        try:
            from flask import session as flask_session

            organisation_concept_id = flask_session.get("organisation_concept_id")
        except Exception:
            organisation_concept_id = None

    if organisation_concept_id:
        query["organisation_concept_id"] = organisation_concept_id

    try:
        session = sessions_coll.find_one(query)
        if session:
            logger.info(f"Found interaction session with ID: {interaction_id}")
            return session
        else:
            logger.warning(f"Interaction session not found with ID: {interaction_id}")
            return None
    except Exception as e:
        logger.error(
            f"Error retrieving interaction session with ID {interaction_id}: {e}"
        )
        raise ConceptServiceError(
            f"Invalid interaction ID format or database error for ID {interaction_id}."
        ) from e


def synthesize_and_update_concept_notes(
    concept_id: str,
    concept: Dict[str, Any],
    question_or_statement: str,
    user_answer: str,
    concept_type_name: str,
    ollama_client,  # Generic LLM client (keeping name for compatibility)
    model: str,
    session: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Synthesizes a Q&A exchange into a factual statement and updates the concept's notes.

    Args:
        concept_id: The ID of the concept to update
        concept: The concept document/dict
        question_or_statement: The LLM's question or statement
        user_answer: The user's response
        concept_type_name: The name of the concept type for context
        ollama_client: The LLM client instance to use (OpenAI, Ollama, etc.)
        model: The model name to use for synthesis
        session: The interaction session containing initial notes

    Returns:
        The synthesized text that was added to notes, or None if no update was made

    Raises:
        Exception: If there's an error during synthesis or database update
    """
    # Get concept notes from database using proper getter
    concept_notes = get_concept_notes(concept) or ""

    # Extract initial notes from session if available
    initial_notes = ""
    if session and isinstance(session.get("history"), list):
        for entry in session["history"]:
            if entry.get(
                "interaction_type"
            ) == "user_provided_initial_notes" and isinstance(
                entry.get("details"), dict
            ):
                initial_notes = entry["details"].get("notes", "")
                break

    # Use initial notes if concept notes are empty
    if not concept_notes and initial_notes:
        concept_notes = initial_notes
        logger.info(
            f"Using initial notes from session for synthesis. Notes length: {len(concept_notes)}"
        )
    elif concept_notes and initial_notes and concept_notes != initial_notes:
        # If we have both database notes and initial notes, prefer database notes
        # but log this situation for debugging
        logger.info(
            f"Found both database notes ({len(concept_notes)}) and initial notes ({len(initial_notes)}), using database notes"
        )

    logger.info(
        f"concept notes for synthesis (first 200 chars): {concept_notes[:200]}..."
    )

    # Create a prompt to synthesize the Q&A into a knowledge statement
    # Todo [JVNAUTOSCI-200]: Store the synthesis prompt in the Vontology type node for future use as an explict prompt template concept
    synthesis_prompt = (
        f"Context: You are updating knowledge about an concept of type '{concept_type_name}'.\n"
        f"concept's current notes: {concept_notes}\n\n"
        f"Recent Q&A Exchange:\n"
        f"Question/Statement: {question_or_statement}\n"
        f"User's Answer: {user_answer}\n\n"
        f"Task: Based ONLY on the user's answer, create a concise factual statement that captures the new information about this concept. "
        f"Focus on what the user actually said, not on connecting it to existing notes. "
        f"The statement should be in third person and suitable for appending to the concept's notes. "
        f"If the user's answer doesn't provide new meaningful information, respond with 'NO_UPDATE'. "
        f"Examples:\n"
        f"- If user says 'He is an author' → 'He is an author.'\n"
        f"- If user says 'She lives in New York' → 'She lives in New York.'\n"
        f"- If user says 'It was built in 1990' → 'It was built in 1990.'\n"
        f"Respond with ONLY the factual statement or 'NO_UPDATE' - no explanations or additional text."
    )

    # Get synthesis from LLM
    synthesis_response = ollama_client.generate(prompt=synthesis_prompt, model=model)

    if isinstance(synthesis_response, str):
        synthesis_text = synthesis_response.strip()
    else:
        synthesis_text = str(synthesis_response).strip()

    # Update concept notes if we got meaningful synthesis
    if synthesis_text and synthesis_text != "NO_UPDATE" and len(synthesis_text) > 10:
        updated_notes = concept_notes
        if updated_notes and not updated_notes.endswith("."):
            updated_notes += ". "
        elif updated_notes:
            updated_notes += " "

        updated_notes += synthesis_text

        # Update the concept in database using the repository
        # Use the correct concept ID field (_id for MongoDB ObjectId)
        concept_filter = (
            {"_id": ObjectId(concept_id)}
            if len(concept_id) == 24
            else {"concept_id": concept_id}
        )

        logger.info(
            f"Updating concept notes in DB. concept ID: {concept_id}, Filter: {concept_filter}, Updated notes length: {len(updated_notes)}"
        )

        # Use the canonical notes updater (relations only now) to persist
        if update_concept_notes(concept_id, updated_notes):
            set_concept_notes(
                concept, updated_notes
            )  # in-memory convenience; still uses preserved_fields structure locally
            logger.info(
                f"Successfully updated concept notes (relations only) for {concept_id}. Added: {synthesis_text}"
            )
            return synthesis_text
        logger.warning(
            f"Failed to update concept notes for {concept_id} via relations updater."
        )
        return None
    else:
        logger.info(
            f"No meaningful synthesis generated for Q&A pair. Synthesis: {synthesis_text}"
        )
        return None


def export_concepts(
    concept_id: Optional[str] = None,
    include_descendants: bool = True,
    format: str = "json",
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Export concepts from the database in the specified format.

    This function is designed for export operations and returns ALL matching concepts
    without pagination, unlike list_concepts which supports pagination.

    Args:
        concept_id: Optional concept ID to filter by (e.g., "#V#person")
        include_descendants: Whether to include concepts of descendant concepts (default: True)
        format: Output format, currently only "json" is supported (default: "json")

    Returns:
        Tuple of (concepts_list, total_count) where concepts_list contains all matching concepts

    Raises:
        InvalidConceptDataError: For invalid parameters
        ConceptServiceError: For database or other service errors
    """
    logger.info(
        f"Exporting concepts: concept_id={concept_id}, include_descendants={include_descendants}, format={format}"
    )

    # Ensure DB is available (repository is the only supported access path)
    if ConceptsRepository.collection() is None:
        logger.error("concepts collection is not available for export_concepts.")
        raise ConceptServiceError("Database collection 'concepts' not available.")

    # Validate format parameter
    if format not in ["json"]:
        raise InvalidConceptDataError(
            f"Unsupported export format: '{format}'. Currently supported: 'json'"
        )

    # Build query based on concept_id and include_descendants
    query: MongoQuery = {}

    if concept_id:
        try:
            if include_descendants:
                # Get the provided concept_id and all its descendants
                all_relevant_ids = get_vontology_node_and_descendant_ids(concept_id)
                if all_relevant_ids:
                    query["concept_id"] = {"$in": all_relevant_ids}
                    logger.info(
                        f"Export query will include {len(all_relevant_ids)} concept IDs (including descendants)"
                    )
                else:
                    # If no concepts are found (e.g., invalid ID), query for the ID directly as a fallback
                    query["concept_id"] = concept_id
                    logger.warning(
                        f"No descendants found for concept_id {concept_id}, using direct query"
                    )
            else:
                # Only query for the exact concept_id provided
                query["concept_id"] = concept_id
                logger.info(
                    f"Export query will only include exact concept_id: {concept_id}"
                )
        except Exception as e:
            logger.error(
                f"Error resolving concept descendants for {concept_id}: {e}. Falling back to direct query.",
                exc_info=True,
            )
            query["concept_id"] = (
                concept_id  # Fallback to querying only the given concept_id
            )

    try:
        # Get total count for reporting
        total_count = ConceptsRepository.count_documents(query)
        logger.info(f"Export operation will return {total_count} concepts")

        # Fetch ALL concepts (no pagination for export)
        # Sort by updated_at descending for consistent ordering
        sort_criteria = [("updated_at", -1)]

        concepts_cursor = ConceptsRepository.find(query, sort=sort_criteria)

        concepts_list = []
        for concept_doc in concepts_cursor:
            # Convert ObjectId to string for JSON serialization
            concept_doc["id"] = str(concept_doc.pop("_id"))

            # Ensure created_at/updated_at present and serialized to ISO strings.
            # Prefer top-level fields, fallback to nested timestamps.*, and default to empty string if missing.
            def _to_iso(val):
                if isinstance(val, datetime):
                    return val.isoformat()
                if isinstance(val, str):
                    return val
                return None

            created_raw = concept_doc.get("created_at")
            if created_raw is None:
                created_raw = (concept_doc.get("timestamps") or {}).get("created_at")
            created_iso = _to_iso(created_raw)
            concept_doc["created_at"] = created_iso if created_iso is not None else ""

            updated_raw = concept_doc.get("updated_at")
            if updated_raw is None:
                updated_raw = (concept_doc.get("timestamps") or {}).get("updated_at")
            updated_iso = _to_iso(updated_raw)
            concept_doc["updated_at"] = updated_iso if updated_iso is not None else ""

            # Add direct concept name if concept_id exists (similar to list_concepts)
            direct_concept_id = concept_doc.get("concept_id")
            concept_doc["direct_concept_name"] = (
                None  # Initialize in case it's not found
            )

            if direct_concept_id:
                try:
                    concept_details_list = get_concept_details_from_db(
                        concept_name_or_id=direct_concept_id
                    )
                    if concept_details_list:
                        actual_concept_details = concept_details_list[0]
                        if actual_concept_details and actual_concept_details.get(
                            "name"
                        ):
                            concept_doc["direct_concept_name"] = actual_concept_details[
                                "name"
                            ]
                    else:
                        logger.warning(
                            f"Could not find concept details for concept_id: {direct_concept_id} during export"
                        )
                except Exception as e:
                    logger.error(
                        f"Error fetching concept details for {direct_concept_id} during export: {e}",
                        exc_info=True,
                    )

            # Ensure display name present using centralized accessor with fallbacks
            try:
                from ..vontology.utils_vontology import (
                    get_concept_display_name_with_names_fallback,
                )

                resolved_name = get_concept_display_name_with_names_fallback(
                    concept_doc
                )
                if resolved_name:
                    concept_doc["name"] = resolved_name
                else:
                    concept_doc.setdefault("name", "")
            except Exception:
                concept_doc.setdefault("name", "")

            # Ensure all required fields are present (add defaults if missing)
            concept_doc.setdefault("description", None)
            # Don't set top-level notes field - use proper accessor functions
            concept_doc.setdefault("attributes", {})
            concept_doc.setdefault("system_tags", [])
            concept_doc.setdefault("user_tags", [])
            concept_doc.setdefault("linked_concepts", [])

            concepts_list.append(concept_doc)

        logger.info(
            f"Export completed successfully: {len(concepts_list)} concepts exported, total_count={total_count}"
        )
        return concepts_list, total_count

    except PyMongoError as e:
        logger.error(f"Database error in export_concepts: {e}", exc_info=True)
        raise ConceptServiceError(f"Database error exporting concepts: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error in export_concepts: {e}", exc_info=True)
        raise ConceptServiceError(
            f"An unexpected error occurred while exporting concepts: {str(e)}"
        )


def import_concepts(
    data: Any,
    *,
    conflict_resolution: str = "update",
    validate_concepts: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Import concepts into the concepts collection.

    Args:
        data: JSON payload. Either a list of concepts or an object with 'concepts' (preferred)
              or legacy 'entities' array.
        conflict_resolution: 'update' | 'create_new' | 'skip'
        validate_concepts: If True, verify concept_id refers to a known Vontology type
        dry_run: If True, do not write to DB; just report what would happen

    Returns:
        Dict with counts and errors.
    """
    logger.info(
        f"Importing concepts: conflict_resolution={conflict_resolution}, validate={validate_concepts}, dry_run={dry_run}"
    )

    concepts_coll = ConceptsRepository.collection()
    if concepts_coll is None:
        raise ConceptServiceError("Database collection 'concepts' not available.")

    if conflict_resolution not in {"update", "create_new", "skip"}:
        raise InvalidConceptDataError(
            "conflict_resolution must be one of: update, create_new, skip"
        )

    # Normalize incoming items
    items: List[Dict[str, Any]] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if isinstance(data.get("concepts"), list):
            items = data["concepts"]
        elif isinstance(data.get("entities"), list):  # legacy
            items = data["entities"]
        else:
            # allow single object import
            # or treat as empty if no recognizable array
            single = {k: v for k, v in data.items()}
            if single:
                items = [single]
    else:
        raise InvalidConceptDataError("Import payload must be a JSON array or object")

    now = datetime.now(timezone.utc)
    created = 0
    updated = 0
    skipped = 0
    errors: List[str] = []

    def validate_type_concept(concept_id_val: Optional[str]) -> bool:
        if not validate_concepts:
            return True
        if not concept_id_val:
            return False
        try:
            nodes = get_concept_details_from_db(concept_name_or_id=concept_id_val)
            return bool(nodes)
        except Exception as e:
            logger.warning(f"Validation error for concept_id {concept_id_val}: {e}")
            return False

    for idx, item in enumerate(items):
        try:
            # Normalize legacy fields (Phase 1 JVNAUTOSCI-545)
            try:
                from .concept_normalization import (
                    normalize_concept_payload,
                    LegacyFieldUsage,
                )

                item = normalize_concept_payload(item, record_warnings=True)
            except LegacyFieldUsage as le:  # strict mode rejection
                errors.append(f"Item {idx}: {le}")
                skipped += 1
                continue
            except Exception as norm_err:  # non-fatal; proceed with raw item
                logger.warning(f"Normalization failed for item {idx}: {norm_err}")

            # Extract name
            name = item.get("name") or (item.get("metadata") or {}).get("title")
            if not name or not isinstance(name, str) or not name.strip():
                errors.append(f"Item {idx}: missing required 'name'")
                skipped += 1
                continue

            # Determine type concept_id (preferred) or accept vontology_path (legacy)
            type_concept_id = item.get("concept_id")
            if not type_concept_id:
                vpath = item.get("vontology_path")
                if isinstance(vpath, list) and vpath:
                    type_concept_id = vpath[-1]

            if validate_concepts and not validate_type_concept(type_concept_id):
                errors.append(
                    f"Item {idx} ('{name}'): invalid or missing concept_id for type validation"
                )
                skipped += 1
                continue

            # Build an existence filter based on name+concept_id if available
            exist_filter: Dict[str, Any] = {"name": name}
            if type_concept_id:
                exist_filter["concept_id"] = type_concept_id

            existing = ConceptsRepository.find_one(exist_filter)

            # Prepare update payload mapping incoming fields
            # Map 'notes' to schema location if provided
            set_fields: Dict[str, Any] = {
                "updated_at": now,
            }

            # Copy selected known fields if present
            for field in [
                "description",
                "attributes",
                "system_tags",
                "user_tags",
                "linked_concepts",
                "relationships",
                "metadata",
            ]:
                if field in item:
                    set_fields[field] = item[field]

            # Ensure concept_id/vontology_path preserved
            if type_concept_id:
                set_fields["concept_id"] = type_concept_id
            if isinstance(item.get("vontology_path"), list):
                set_fields["vontology_path"] = item["vontology_path"]

            # Notes: preserved_fields deprecated (Phase 1). Import will NOT write legacy notes directly.
            if isinstance(item.get("notes"), str):
                # Optionally: could queue relation creation here in future Phase 2.
                pass

            if conflict_resolution == "update":
                if existing:
                    if not dry_run:
                        # Update fields as usual
                        ConceptsRepository.update_one(
                            {"_id": existing["_id"]}, {"$set": set_fields}
                        )
                        # Ensure names[] has NL entry; backfill from incoming name if necessary
                        try:
                            names_arr = existing.get("names")
                            has_nl = False
                            if isinstance(names_arr, list):
                                for entry in names_arr:
                                    if (
                                        isinstance(entry, dict)
                                        and entry.get("type") == "NL"
                                        and str(entry.get("name", "")).strip()
                                    ):
                                        has_nl = True
                                        break
                            if not has_nl and isinstance(name, str) and name.strip():
                                ConceptsRepository.update_one(
                                    {"_id": existing["_id"]},
                                    {
                                        "$push": {
                                            "names": {
                                                "name": name.strip(),
                                                "language": "en-NZ",
                                                "type": "NL",
                                            }
                                        }
                                    },
                                )
                        except Exception as _e:
                            logger.warning(
                                f"Failed to backfill names[] during update for item {idx}: {_e}"
                            )
                    updated += 1
                else:
                    # Create new document: store display name in names[] (canonical) instead of legacy top-level name
                    new_doc = {
                        "names": [
                            {"name": name.strip(), "language": "en-NZ", "type": "NL"}
                        ],
                        "created_at": now,
                        **{
                            k: v
                            for k, v in set_fields.items()
                            if not k.startswith("concept_data.")
                        },
                    }
                    # Legacy notes mapping removed (Phase 1)
                    if not dry_run:
                        ConceptsRepository.insert_one(new_doc)
                    created += 1

            elif conflict_resolution == "create_new":
                # Always create; if name+concept_id conflicts, add a suffix
                new_name = name
                if existing:
                    # Simple deterministic suffix to avoid conflict
                    new_name = f"{name} (imported {now.strftime('%Y%m%d%H%M%S')})"
                new_doc = {
                    "names": [
                        {"name": new_name.strip(), "language": "en-NZ", "type": "NL"}
                    ],
                    "created_at": now,
                    **{
                        k: v
                        for k, v in set_fields.items()
                        if not k.startswith("concept_data.")
                    },
                }
                # Legacy notes mapping removed (Phase 1)
                if not dry_run:
                    ConceptsRepository.insert_one(new_doc)
                created += 1

            elif conflict_resolution == "skip":
                if existing:
                    skipped += 1
                else:
                    new_doc = {
                        "names": [
                            {"name": name.strip(), "language": "en-NZ", "type": "NL"}
                        ],
                        "created_at": now,
                        **{
                            k: v
                            for k, v in set_fields.items()
                            if not k.startswith("concept_data.")
                        },
                    }
                    # Legacy notes mapping removed (Phase 1)
                    if not dry_run:
                        ConceptsRepository.insert_one(new_doc)
                    created += 1

        except Exception as e:
            logger.error(f"Error importing item {idx}: {e}", exc_info=True)
            errors.append(f"Item {idx}: {str(e)}")
            skipped += 1

    total = created + updated + skipped
    logger.info(
        f"Import concepts completed: total={total}, created={created}, updated={updated}, skipped={skipped}, errors={len(errors)}"
    )
    result_payload = {
        "success": True,
        "imported": total,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
    }
    # Attach legacy usage summary if available (added earlier in loop via normalization accumulation)
    try:  # best-effort, summary only present if items processed
        from .concept_normalization import summarize_legacy_usage  # inline import safe

        # Reconstruct summary from processed items only if not huge (cap for safety)
        # NOTE: For accuracy we'd have accumulated; if accumulation added later replace this re-scan.
        # Lightweight: just reuse original items slice (may include normalized fields)
        if isinstance(items, list) and items:
            sample = items if len(items) <= 5000 else items[:5000]
            summary = summarize_legacy_usage(sample)  # type: ignore
            result_payload["legacy_usage"] = summary
            # Persist counters
            try:
                db = get_db()
                if db is not None:
                    coll = db.get_collection("system_metrics")
                    now_iso = datetime.now(timezone.utc).isoformat()
                    inc_ops = {}
                    for field, count in summary["counts"].items():
                        if count:
                            inc_ops[f"legacy_import_fields.{field}"] = int(count)
                    if inc_ops:
                        coll.update_one(
                            {"_id": "legacy_import_counters"},
                            {
                                "$inc": inc_ops,
                                "$set": {
                                    "updated_at": now_iso,
                                    "last_batch_total": summary["total"],
                                },
                            },
                            upsert=True,
                        )
            except Exception as persist_err:  # pragma: no cover
                logger.warning(
                    f"Failed persisting legacy import counters: {persist_err}"
                )
    except Exception as summary_err:  # pragma: no cover
        logger.debug(f"No legacy summary available: {summary_err}")
    return result_payload


def generate_initial_question(
    concept_id: str, initial_notes: Optional[str] = None
) -> dict:
    """
    Ask the LLM for the *first* question to pose about an concept.

    ── Workflow ───────────────────────────────────────────────────────────────
      1.  Fetch concept document.
      2.  Build a prompt via generate_concept_question(… , is_initial=True).
          – That helper pulls / stores the template, handles person-pronouns,
            and fills in the concept’s notes.
      3.  Call the LLM.
      4.  Return the question (or a safe fallback) plus basic concept info.
    ───────────────────────────────────────────────────────────────────────────
    """
    logger.info("Generating initial question for concept_id: %s", concept_id)

    # ------------------------------------------------------------------ 1. concept
    # Support both Mongo _id and Vontology concept identifiers (e.g., '#V#person')
    try:
        if isinstance(concept_id, str) and concept_id.startswith("#V#"):
            concept = get_concept_by_concept_id(concept_id)
        else:
            concept = get_concept_by_id(concept_id)
    except ConceptNotFoundError as e:
        logger.error(
            "concept not found for generating initial question (ID: %s): %s",
            concept_id,
            e,
        )
        return {"error": str(e), "status": "error_concept_not_found"}
    if not concept:
        logger.error("concept not found for concept_id: %s", concept_id)
        return {"error": "concept not found", "status": "error_concept_not_found"}

    # ------------------------------------------------------------------ 1.5. combine notes
    # If initial_notes are provided, combine them with existing concept notes
    enhanced_concept = concept.copy()
    existing_notes = get_concept_notes(concept) or ""

    if initial_notes:
        if existing_notes:
            # Combine existing notes with initial notes
            combined_notes = f"{existing_notes}\n\nAdditional context: {initial_notes}"
        else:
            # Use initial notes if no existing notes
            combined_notes = initial_notes
        set_concept_notes(enhanced_concept, combined_notes)
        logger.info(
            "Combined concept notes with initial_notes for concept_id: %s", concept_id
        )

    # ------------------------------------------------------------------ 2. prompt
    try:
        db_for_templates = get_db()
        if db_for_templates is None:
            raise ConceptServiceError(
                "Database connection not available for prompt generation"
            )
        final_prompt = generate_concept_question(
            concept=enhanced_concept,  # Use enhanced concept with combined notes
            db=db_for_templates,  # for template lookup / persistence
            is_initial=True,
        )
    except Exception as e:
        logger.error("Failed to build prompt: %s", e, exc_info=True)
        return {
            "error": "Prompt construction failure",
            "status": "error_prompt_generation",
        }

    # ------------------------------------------------------------------ 3. call LLM
    try:
        from ..languagemodels.llm_interface import get_llm_client, get_active_model_name

        llm_client = get_llm_client()
        model_name = get_active_model_name()  # Get the active model from settings

        raw = llm_client.generate(prompt=final_prompt, model=model_name)
        initial_question = (raw if isinstance(raw, str) else str(raw)).strip()
    except Exception as e:
        logger.error("LLM call failed: %s", e, exc_info=True)
        initial_question = ""

    # ------------------------------------------------------------------ 4. fallback & return
    if not initial_question:
        initial_question = f"What would you like to tell me about {concept.get('name', 'this concept')}?"

    from ..vontology.utils_vontology import get_concept_display_name_with_names_fallback

    return {
        "question": initial_question,
        "status": "success",
        "concept": {
            "_id": str(concept.get("_id", concept_id)),
            "name": get_concept_display_name_with_names_fallback(concept),
            "notes": get_concept_notes(enhanced_concept)
            or "",  # Use enhanced concept with combined notes
            "concept_id": concept.get("concept_id"),
        },
    }
