"""Relationship write service - single authoritative pathway for relationship writes.

JVNAUTOSCI-986: This module consolidates all relationship write logic to prevent
drift between structural relationship fields and canonical predicate concepts.

Authoritative pathway:
1. All relationship writes flow through this service.
2. Structural predicates (#V#is_a_type_of etc.) are normalised to field names.
3. Kind derivation is computed from structural fields (is_a_type_of, is_an_instance_of).
4. Structural inverses can be maintained when the caller is authorised to
   mutate both concepts. Source-only callers leave the target unchanged and
   use the derived relationship-extent index for incoming traversal.

Usage:
    from src.backend.services.relationship_write_service import (
        add_relationship,
        normalise_structural_predicate,
        detect_kind_drift,
    )
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..db.repositories.concepts_repository import (
    ConceptsRepository,
)
from ..security.access_control import bypass_access_control
from .concept_predicate_metadata_service import (
    get_relationship_kinds_set,
    get_structural_predicate_aliases,
    get_structural_inverse_map,
)
from .feature_flags import (
    get_event_workflow_integration_enabled,
    get_workflow_discovery_cache_invalidation_enabled,
)
from ..vontology.utils_vontology import is_predicate, is_type
from ..vontology.code_concepts_registry import (
    build_virtual_concept_doc,
    is_code_concept_id,
)
from ..security.visibility_predicates import VISIBILITY_PREDICATE_ALIAS_TO_CANONICAL

_logger = logging.getLogger(__name__)

# Common predicate name aliases that map to structural field names.
PREDICATE_NAME_ALIASES: Dict[str, str] = {
    "instance_of": "is_an_instance_of",
    "instanceOf": "is_an_instance_of",
    "type_of": "is_a_type_of",
    "typeOf": "is_a_type_of",
    "subtype": "has_subtype",
    "instance": "has_instance",
    **VISIBILITY_PREDICATE_ALIAS_TO_CANONICAL,
}

# Blocked parent types that defeat the purpose of the ontology (JVNAUTOSCI-1072).
# #V#thing is the universal top type - everything is implicitly a thing.
# Explicit relationships to it provide no semantic value.
BLOCKED_PARENT_TYPES: frozenset[str] = frozenset({"#V#thing"})
THING_PARENT_GUIDANCE = (
    "Parents should be a semantically close generalisation of the child concept, "
    "and can be introduced if no adequate parent exists."
)

# Suggested alternative supertypes to offer when blocked parent type is detected.
SUGGESTED_SUPERTYPES: List[str] = [
    "#V#physical_object",
    "#V#abstract_object",
    "#V#living_organism",
    "#V#event",
    "#V#process",
    "#V#information_object",
]

CORE_RELATIONSHIP_TEXT_PREDICATES: frozenset[str] = frozenset(
    {"hasContent", "hasDescription", "hasName"}
)
TREE_PROJECTION_RELATIONSHIP_PREDICATES: frozenset[str] = frozenset(
    {
        "is_a_type_of",
        "has_subtype",
        "is_an_instance_of",
        "has_instance",
        "most_salient_type",
        *VISIBILITY_PREDICATE_ALIAS_TO_CANONICAL.values(),
    }
)


def _emit_relationship_mutation_event(
    *,
    event_type: str,
    source_id: str,
    predicate: str,
    target_id: str,
    is_structural: bool,
) -> None:
    """Best-effort workflow event emission for relationship mutations."""

    if not get_event_workflow_integration_enabled(default=True):
        return

    try:
        from .workflow_event_integration_service import (
            maybe_launch_vontology_mutation_workflow,
            resolve_event_actor_context,
        )

        actor_id, actor_org = resolve_event_actor_context()
        maybe_launch_vontology_mutation_workflow(
            mutation_event_type=event_type,
            mutation_id=f"{source_id}:{predicate}:{target_id}",
            user_id=actor_id,
            org_id=actor_org,
            event_payload={
                "source_id": source_id,
                "predicate": predicate,
                "target_id": target_id,
                "is_structural": is_structural,
            },
            inputs={
                "source_id": source_id,
                "predicate": predicate,
                "target_id": target_id,
                "is_structural": is_structural,
            },
        )
    except Exception as exc:  # pragma: no cover - event emission is best-effort
        _logger.warning(
            "relationship_write_service: failed to emit relationship mutation event for %s %s %s: %s",
            source_id,
            predicate,
            target_id,
            exc,
        )


def _invalidate_workflow_routing_projection_for_relationship_change(
    *,
    source_id: str,
    predicate: str,
    target_id: str,
) -> None:
    """Best-effort invalidation for workflow graph/type routing projections."""

    if not get_workflow_discovery_cache_invalidation_enabled(default=True):
        return

    predicate_text = str(predicate or "").strip()
    if not predicate_text:
        return
    relevant_predicates = {"is_a_type_of", "is_an_instance_of"}
    try:
        from ..workflows.vontology_loader import WORKFLOW_GRAPH_PREDICATE_ALIASES

        relevant_predicates.update(
            item
            for aliases in WORKFLOW_GRAPH_PREDICATE_ALIASES.values()
            for item in aliases
        )
    except Exception:
        pass
    if predicate_text not in relevant_predicates:
        return

    try:
        from .workflow_capability_service import invalidate_workflow_capability_index
        from .workflow_discovery_service import (
            invalidate_workflow_discovery_executability_caches,
        )

        invalidate_workflow_capability_index(
            reason=(
                "workflow_routing_relationship_changed:"
                f"{source_id}:{predicate_text}:{target_id}"
            ),
        )
        invalidate_workflow_discovery_executability_caches()
    except Exception:
        pass


def _invalidate_vontology_projection_for_relationship_change(
    *,
    source_id: str,
    predicate: str,
    target_id: str,
) -> None:
    """Invalidate actor-scoped tree projections affected by an edge write."""

    predicate_text = str(predicate or "").strip()
    canonical_predicate = VISIBILITY_PREDICATE_ALIAS_TO_CANONICAL.get(
        predicate_text,
        predicate_text,
    )
    if canonical_predicate not in TREE_PROJECTION_RELATIONSHIP_PREDICATES:
        return

    try:
        from ..vontology.utils_vontology import invalidate_vontology_caches

        invalidate_vontology_caches(
            [source_id, target_id],
            correlation_id=(
                "relationship-add:"
                f"{source_id}:{canonical_predicate}:{target_id}"
            ),
        )
    except Exception:
        _logger.warning(
            "relationship_write_service: Vontology projection invalidation failed for %s %s %s",
            source_id,
            predicate,
            target_id,
            exc_info=True,
        )


def _sync_relationship_extent_index_for_sources(source_ids: List[str]) -> None:
    """Best-effort refresh of the derived relationship extent query index."""

    try:
        from .relationship_extent_index_service import (
            sync_relationship_extent_index_for_concept_id,
        )

        for source_id in source_ids:
            if isinstance(source_id, str) and source_id.strip():
                sync_relationship_extent_index_for_concept_id(source_id.strip())
    except Exception:
        _logger.debug(
            "relationship_write_service: relationship extent index sync failed",
            exc_info=True,
        )


def normalise_structural_predicate(predicate: str) -> str:
    """Normalise a predicate string to its canonical form.

    Converts #V# prefixed structural predicates to their field names.
    Non-structural predicates are returned unchanged.

    Args:
        predicate: The predicate string to normalise.

    Returns:
        The normalised predicate (structural field name or original).
    """
    if not isinstance(predicate, str):
        return predicate

    predicate = predicate.strip()

    # Check #V# prefixed structural aliases first (Vontology-backed)
    aliases = get_structural_predicate_aliases()
    if predicate in aliases:
        return aliases[predicate]

    # Check common name aliases
    if predicate in PREDICATE_NAME_ALIASES:
        return PREDICATE_NAME_ALIASES[predicate]

    return predicate


def is_structural_predicate(predicate: str) -> bool:
    """Check if a predicate is a structural relationship predicate.

    Structural predicates determine kind classification and have inverse relationships.

    Args:
        predicate: The predicate (may be normalised or not).

    Returns:
        True if this is a structural predicate.
    """
    normalised = normalise_structural_predicate(predicate)
    return normalised in get_relationship_kinds_set()


def compute_kind_from_relationships(
    relationships: Mapping[str, Any],
) -> str:
    """Compute kind classification from structural relationship fields.

    This is the authoritative source of truth for kind classification.

    Args:
        relationships: The relationships dict from a concept document.

    Returns:
        'type', 'predicate', or 'individual'.
    """
    node = {"relationships": dict(relationships) if relationships else {}}

    # Check predicate FIRST: predicates can have is_a_type_of relationships
    # (e.g., a predicate subtype), which would incorrectly match is_type().
    if is_predicate(node):
        return "predicate"
    elif is_type(node):
        return "type"
    else:
        return "individual"


def detect_vacuous_typing(
    node: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Detect if a concept has vacuous typing (only #V#thing as parent).

    Vacuous typing occurs when:
    - A type has only #V#thing in is_a_type_of (no meaningful supertype)
    - An individual has only #V#thing in is_an_instance_of (no meaningful type)

    This is a soft warning for retrieval - agents should repair these concepts
    by searching for appropriate parent types.

    JVNAUTOSCI-1072: Centralised vacuous typing detection.

    Args:
        node: A concept document with 'relationships' field.

    Returns:
        None if no vacuous typing, otherwise a warning dict with repair suggestions.
    """
    if not isinstance(node, Mapping):
        return None

    relationships = node.get("relationships", {}) or {}
    concept_id = node.get("concept_id")

    # Check for vacuous type (is_a_type_of only contains #V#thing)
    is_a_type_of = relationships.get("is_a_type_of", [])
    if isinstance(is_a_type_of, str):
        is_a_type_of = [is_a_type_of]

    # Check for vacuous instance (is_an_instance_of only contains #V#thing)
    is_an_instance_of = relationships.get("is_an_instance_of", [])
    if isinstance(is_an_instance_of, str):
        is_an_instance_of = [is_an_instance_of]

    # Determine if vacuous
    vacuous_type = len(is_a_type_of) > 0 and all(
        t in BLOCKED_PARENT_TYPES for t in is_a_type_of
    )
    vacuous_instance = len(is_an_instance_of) > 0 and all(
        t in BLOCKED_PARENT_TYPES for t in is_an_instance_of
    )

    if not vacuous_type and not vacuous_instance:
        return None

    warning: Dict[str, Any] = {
        "code": "vacuous_typing",
        "message": (
            f"Concept '{concept_id}' has only #V#thing as parent. "
            "Search for appropriate types using search_concepts or find_subconcepts "
            "and add a more specific parent type."
        ),
        "concept_id": concept_id,
        "suggested_alternatives": SUGGESTED_SUPERTYPES,
    }

    if vacuous_type:
        warning["vacuous_is_a_type_of"] = is_a_type_of
        warning["repair_action"] = (
            "add_relationship with is_a_type_of to a more specific supertype"
        )
    if vacuous_instance:
        warning["vacuous_is_an_instance_of"] = is_an_instance_of
        warning["repair_action"] = (
            "add_relationship with is_an_instance_of to a more specific type"
        )

    return warning


def detect_kind_drift(
    node: Mapping[str, Any],
    expected_kind: str,
) -> Optional[Dict[str, Any]]:
    """Detect drift between expected and computed kind.

    Args:
        node: A concept document with 'relationships' field.
        expected_kind: The expected kind classification.

    Returns:
        None if no drift, otherwise a dict with drift details.
    """
    if not isinstance(node, Mapping):
        return {"error": "invalid_node", "expected": expected_kind}

    relationships = node.get("relationships", {}) or {}
    computed_kind = compute_kind_from_relationships(relationships)

    if computed_kind == expected_kind:
        return None

    return {
        "expected": expected_kind,
        "computed": computed_kind,
        "concept_id": node.get("concept_id"),
        "is_a_type_of": relationships.get("is_a_type_of", []),
        "is_an_instance_of": relationships.get("is_an_instance_of", []),
    }


def validate_predicate_concept(
    predicate: str,
    repo: Any = None,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """Validate that a dynamic predicate concept exists and is typed correctly.

    Args:
        predicate: The predicate concept ID (must start with #V#).
        repo: Optional repository instance (defaults to ConceptsRepository).

    Returns:
        Tuple of (is_valid, error_code, error_details).
    """
    if repo is None:
        repo = ConceptsRepository

    if not isinstance(predicate, str) or not predicate.startswith("#V#"):
        return False, "invalid_predicate_format", {"predicate": predicate}

    # Check if it's a structural predicate (always valid)
    if predicate in get_structural_predicate_aliases():
        return True, None, None

    # Check for persisted or code-registered concept
    # Predicate typing is a hard schema invariant, not user-visible domain data.
    # Actor-scoped relationship sanitisation must not hide ``#V#predicate`` here
    # and turn an authorised write into a false ``predicate_concept_not_typed``
    # failure. Source and target checks below remain access-controlled.
    with bypass_access_control():
        pred_doc = repo.find_one(
            {"concept_id": predicate}, {"concept_id": 1, "relationships": 1}
        )
    if pred_doc is None and is_code_concept_id(predicate):
        pred_doc = build_virtual_concept_doc(predicate)

    if pred_doc is None:
        return (
            False,
            "predicate_concept_not_found",
            {
                "predicate": predicate,
                "suggestion": "Create it as an instance of #V#predicate",
            },
        )

    if not is_predicate(pred_doc):
        return (
            False,
            "predicate_concept_not_typed",
            {
                "predicate": predicate,
                "required_instance_of": "#V#predicate",
                "current_instance_of": pred_doc.get("relationships", {}).get(
                    "is_an_instance_of", []
                ),
            },
        )

    return True, None, None


def resolve_existing_predicate_value_kind(
    predicate: str,
    repo: Any = None,
) -> str | None:
    """Return the canonical object kind for one existing exact predicate."""

    if repo is None:
        repo = ConceptsRepository
    predicate_id = str(predicate or "").strip()
    if not predicate_id.startswith("#V#"):
        return None
    if predicate_id[3:] in CORE_RELATIONSHIP_TEXT_PREDICATES:
        return "text"
    if is_structural_predicate(predicate_id):
        return "concept"
    with bypass_access_control():
        predicate_doc = repo.find_one(
            {"concept_id": predicate_id},
            {"concept_id": 1, "relationships.is_an_instance_of": 1},
        )
    if predicate_doc is None and is_code_concept_id(predicate_id):
        predicate_doc = build_virtual_concept_doc(predicate_id)
    if not isinstance(predicate_doc, dict) or not is_predicate(predicate_doc):
        return None
    relationships = (
        predicate_doc.get("relationships")
        if isinstance(predicate_doc, Mapping)
        else {}
    )
    instance_of = (
        relationships.get("is_an_instance_of")
        if isinstance(relationships, Mapping)
        else None
    )
    if isinstance(instance_of, str):
        instance_of = [instance_of]
    return (
        "text"
        if isinstance(instance_of, list)
        and "#V#binary_text_predicate" in instance_of
        else "concept"
    )


def add_structural_relationship(
    source_id: str,
    predicate: str,
    target_id: str,
    *,
    repo: Any = None,
    maintain_inverse: bool = True,
) -> Dict[str, Any]:
    """Add a structural relationship with inverse consistency.

    This is the authoritative write path for structural relationships.

    Args:
        source_id: The source concept ID.
        predicate: The predicate (will be normalised).
        target_id: The target concept ID.
        repo: Optional repository instance.
        maintain_inverse: Whether to store the inverse on the target concept.
            Set this to ``False`` when the authorised effect covers only the
            source assertion; incoming traversal remains available through the
            derived relationship-extent index refreshed from that source.

    Returns:
        Result dict with success status and details.
    """
    if repo is None:
        repo = ConceptsRepository

    normalised = normalise_structural_predicate(predicate)

    if normalised not in get_relationship_kinds_set():
        return {
            "success": False,
            "error": "not_a_structural_predicate",
            "predicate": predicate,
            "normalised": normalised,
        }

    if source_id == target_id:
        return {
            "success": False,
            "error": "self_reference",
            "message": "Source and target cannot be the same.",
        }

    # Ensure source exists
    src = repo.find_one({"concept_id": source_id}, {"concept_id": 1})
    if not src:
        return {
            "success": False,
            "error": "source_not_found",
            "concept_id": source_id,
        }

    # Ensure target exists
    tgt = repo.find_one({"concept_id": target_id}, {"concept_id": 1})
    if not tgt:
        return {
            "success": False,
            "error": "target_not_found",
            "concept_id": target_id,
        }

    # Block vacuous typing (JVNAUTOSCI-1072: hard block for #V#thing as parent)
    # Both is_a_type_of and is_an_instance_of are blocked for #V#thing
    if (
        normalised in ("is_a_type_of", "is_an_instance_of")
        and target_id in BLOCKED_PARENT_TYPES
    ):
        _logger.warning(
            "Blocked vacuous typing: %s %s %s",
            source_id,
            normalised,
            target_id,
        )
        # Build contextual guidance based on the relationship type
        if normalised == "is_an_instance_of":
            guidance = (
                "For instances, use a semantically close parent type. "
                "If one does not exist, create it first, then attach instances to it "
                "rather than to #V#thing. "
                "For example, if creating Otter recordings, first create "
                "'#V#otter_recording_session' as a subtype of #V#event, then create "
                "instances under that specific type."
            )
        else:
            guidance = (
                "Choose a parent that is a semantically close generalisation of the child. "
                f"{THING_PARENT_GUIDANCE} Example parent families include: "
                "#V#physical_object, #V#abstract_object, #V#living_organism, "
                "#V#event, #V#process, #V#information_object."
            )

        return {
            "success": False,
            "error": "blocked_parent_type",
            "message": (
                f"Cannot use '{target_id}' as parent type. Every concept needs a "
                "semantically close parent. If one does not exist, create a new parent "
                "type first. This cannot be #V#thing."
            ),
            "guidance": guidance,
            "blocked_target": target_id,
            "predicate": normalised,
            "source_id": source_id,
            "suggested_alternatives": SUGGESTED_SUPERTYPES,
        }

    # Ensure array storage for forward relationship
    repo._ensure_relationship_array(source_id, normalised)

    # Add forward relationship
    forward_update = repo.update_one(
        {"concept_id": source_id},
        {"$addToSet": {f"relationships.{normalised}": target_id}},
    )

    result = {
        "success": True,
        "source_id": source_id,
        "predicate": normalised,
        "predicate_input": predicate,
        "target_id": target_id,
        "forward_modified": forward_update.modified_count > 0,
    }

    # Maintain inverse relationship
    _inverse_map = get_structural_inverse_map()
    if maintain_inverse and normalised in _inverse_map:
        inv_kind = _inverse_map[normalised]

        try:
            repo._ensure_relationship_array(target_id, inv_kind)
            inv_update = repo.update_one(
                {"concept_id": target_id},
                {"$addToSet": {f"relationships.{inv_kind}": source_id}},
            )
            result["inverse_predicate"] = inv_kind
            result["inverse_modified"] = inv_update.modified_count > 0
        except Exception as e:
            _logger.warning(
                "Failed to add inverse relationship %s -> %s: %s",
                target_id,
                source_id,
                e,
            )
            result["inverse_error"] = str(e)

    if bool(result.get("forward_modified")) or bool(result.get("inverse_modified")):
        sync_sources = [source_id]
        if bool(result.get("inverse_modified")):
            sync_sources.append(target_id)
        _sync_relationship_extent_index_for_sources(sync_sources)
        try:
            from .workflow_event_integration_service import (
                EVENT_TYPE_RELATIONSHIP_ADDED,
            )

            _emit_relationship_mutation_event(
                event_type=EVENT_TYPE_RELATIONSHIP_ADDED,
                source_id=source_id,
                predicate=normalised,
                target_id=target_id,
                is_structural=True,
            )
        except Exception:
            pass

    return result


def add_dynamic_relationship(
    source_id: str,
    predicate: str,
    target_id: str,
    *,
    repo: Any = None,
) -> Dict[str, Any]:
    """Add a dynamic (non-structural) concept-to-concept relationship.

    Dynamic predicates do not have inverse relationships maintained automatically.

    Args:
        source_id: The source concept ID.
        predicate: The predicate concept ID (must be a valid predicate concept).
        target_id: The target concept ID.
        repo: Optional repository instance.

    Returns:
        Result dict with success status and details.
    """
    if repo is None:
        repo = ConceptsRepository

    # Validate the predicate concept
    is_valid, error_code, error_details = validate_predicate_concept(predicate, repo)
    if not is_valid:
        return {
            "success": False,
            "error": error_code,
            **(error_details or {}),
        }

    if source_id == target_id:
        return {
            "success": False,
            "error": "self_reference",
            "message": "Source and target cannot be the same.",
        }

    # Ensure source exists
    src = repo.find_one({"concept_id": source_id}, {"concept_id": 1})
    if not src:
        return {
            "success": False,
            "error": "source_not_found",
            "concept_id": source_id,
        }

    # Ensure target exists
    tgt = repo.find_one({"concept_id": target_id}, {"concept_id": 1})
    if not tgt:
        return {
            "success": False,
            "error": "target_not_found",
            "concept_id": target_id,
        }

    # Ensure array storage
    repo._ensure_relationship_array(source_id, predicate)

    # Add relationship
    update = repo.update_one(
        {"concept_id": source_id},
        {"$addToSet": {f"relationships.{predicate}": target_id}},
    )

    result = {
        "success": True,
        "source_id": source_id,
        "predicate": predicate,
        "target_id": target_id,
        "modified": update.modified_count > 0,
    }
    if bool(result.get("modified")):
        _sync_relationship_extent_index_for_sources([source_id])
        try:
            from .workflow_event_integration_service import (
                EVENT_TYPE_RELATIONSHIP_ADDED,
            )

            _emit_relationship_mutation_event(
                event_type=EVENT_TYPE_RELATIONSHIP_ADDED,
                source_id=source_id,
                predicate=predicate,
                target_id=target_id,
                is_structural=False,
            )
        except Exception:
            pass
    return result


def add_relationship(
    source_id: str,
    predicate: str,
    target: str,
    *,
    repo: Any = None,
    maintain_inverse: bool = True,
) -> Dict[str, Any]:
    """Add a relationship using the appropriate pathway.

    This is the main entry point for adding relationships. It automatically
    routes to structural or dynamic pathways based on the predicate.

    Args:
        source_id: The source concept ID.
        predicate: The predicate (structural or dynamic).
        target: The target concept ID.
        repo: Optional repository instance.
        maintain_inverse: Whether a structural edge may also mutate its target
            by storing the canonical inverse. Governed source-only writes pass
            ``False``; the default preserves explicit two-sided internal uses.

    Returns:
        Result dict with success status and details.
    """
    if repo is None:
        repo = ConceptsRepository

    normalised = normalise_structural_predicate(predicate)

    is_structural = normalised in get_relationship_kinds_set()
    if is_structural:
        result = add_structural_relationship(
            source_id,
            normalised,
            target,
            repo=repo,
            maintain_inverse=maintain_inverse,
        )
        predicate_for_invalidation = normalised
    else:
        result = add_dynamic_relationship(source_id, predicate, target, repo=repo)
        predicate_for_invalidation = predicate

    if isinstance(result, Mapping) and bool(result.get("success")):
        modified = (
            bool(result.get("forward_modified"))
            or bool(result.get("inverse_modified"))
            or bool(result.get("modified"))
            or bool(result.get("created"))
        )
        if modified:
            _invalidate_workflow_routing_projection_for_relationship_change(
                source_id=source_id,
                predicate=predicate_for_invalidation,
                target_id=target,
            )
            _invalidate_vontology_projection_for_relationship_change(
                source_id=source_id,
                predicate=predicate_for_invalidation,
                target_id=target,
            )
    return result
