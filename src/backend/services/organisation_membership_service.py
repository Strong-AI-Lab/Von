"""Service functions for managing organisation membership relationships in the Vontology.

Handles creation, retrieval, and management of memberOf relationships between
user concepts and organisation concepts, including role associations.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import bypass_access_control, can_access_concept
from ..services.text_value_service import upsert_text_for_concept
from .ontology_authority_membership_coordination_service import (
    organisation_membership_scope_barrier,
    ontology_authority_membership_mutation_barrier,
)

logger = logging.getLogger(__name__)

# Constants for relationship kinds
MEMBERSHIP_RELATIONSHIP_KIND = "memberOf"
ALT_MEMBERSHIP_RELATIONSHIP = "#V#member_of_organisation"
ROLE_PREDICATE = "#V#hasRole"
_ROLE_STORAGE_SEPARATOR = "::"


def _coordinated_membership_mutation(function):
    """Keep all membership lifecycle writes inside the shared authority barrier."""

    @wraps(function)
    def coordinated(*args, **kwargs):
        missing = object()
        user_concept_id = (
            kwargs.get("user_concept_id")
            if "user_concept_id" in kwargs
            else (args[0] if len(args) > 0 else missing)
        )
        organisation_concept_id = (
            kwargs.get("organisation_concept_id")
            if "organisation_concept_id" in kwargs
            else (args[1] if len(args) > 1 else missing)
        )
        if (
            user_concept_id is missing
            or organisation_concept_id is missing
            or not isinstance(user_concept_id, str)
            or not user_concept_id
            or not isinstance(organisation_concept_id, str)
            or not organisation_concept_id
        ):
            # Preserve each public function's established argument-validation
            # contract (including Python's missing-argument TypeError) before
            # deriving an internal coordination key.
            return function(*args, **kwargs)
        with ontology_authority_membership_mutation_barrier():
            with organisation_membership_scope_barrier(
                user_concept_id,
                organisation_concept_id,
            ):
                return function(*args, **kwargs)

    return coordinated


def _invalidate_user_window_authority(
    user_concept_id: str,
    organisation_concept_id: str,
) -> None:
    """Remove derived tab authority before a role-reducing mutation.

    The durable deletion is shared by every web worker.  A still-authorised
    tab can reconstruct its selection from its actor-owned conversation on the
    next request; a revoked tab cannot.
    """

    from .window_session_context_service import (
        delete_window_contexts_owned_by_user_for_organisation,
    )

    delete_window_contexts_owned_by_user_for_organisation(
        user_concept_id,
        organisation_concept_id,
    )


def _normalise_concept_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text if text.startswith("#") else f"#V#{text}"


def organisation_role_storage_text(
    role: str,
    organisation_concept_id: str,
) -> str:
    """Return a scope-distinct token for one organisation membership role.

    Text relation uniqueness is subject/predicate/object rather than context.
    Including the organisation in the value prevents equal roles in two
    organisations from overwriting one another's relation context.
    """

    role_value = str(role or "").strip()
    organisation_id = _normalise_concept_id(organisation_concept_id)
    if not role_value:
        raise ValueError("role must be a non-empty string")
    if not organisation_id:
        raise ValueError("organisation_concept_id must be a non-empty string")
    if _ROLE_STORAGE_SEPARATOR in role_value:
        raise ValueError("role may not contain the organisation-role separator")
    return f"{role_value}{_ROLE_STORAGE_SEPARATOR}{organisation_id}"


def parse_organisation_role_storage_text(
    stored_text: object,
    relation_context: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Decode scope-distinct roles while retaining legacy plain-role reads."""

    raw_text = str(stored_text or "").strip()
    if not raw_text:
        return None, None
    context = relation_context if isinstance(relation_context, dict) else {}
    context_org = _normalise_concept_id(
        context.get("organisation_concept_id") or context.get("organisation_id")
    )
    if _ROLE_STORAGE_SEPARATOR not in raw_text:
        return (raw_text, context_org) if context_org else (None, None)
    role, raw_organisation_id = raw_text.rsplit(_ROLE_STORAGE_SEPARATOR, 1)
    role = role.strip()
    stored_org = _normalise_concept_id(raw_organisation_id)
    if not role or not stored_org:
        return None, None
    if context_org and context_org != stored_org:
        # Context disagreement is malformed and must not be interpreted as a
        # role for either organisation.
        return None, None
    return role, stored_org


def resolve_user_organisation_membership(
    user_concept_id: str,
    organisation_concept_id: str,
) -> dict[str, Any] | None:
    """Return the represented membership record for an exact user/org pair."""

    user_id = _normalise_concept_id(user_concept_id)
    org_id = _normalise_concept_id(organisation_concept_id)
    if user_id is None or org_id is None:
        return None
    memberships = get_user_memberships(user_id)
    for membership in memberships.get("memberships", []):
        if not isinstance(membership, dict):
            continue
        if _normalise_concept_id(membership.get("organisation_concept_id")) == org_id:
            return {
                "user_concept_id": user_id,
                "organisation_concept_id": org_id,
                "role": str(membership.get("role") or "member").strip() or "member",
            }
    return None


def is_user_member_of_organisation(
    user_concept_id: str,
    organisation_concept_id: str,
) -> bool:
    """Return whether Vontology represents the user as an org member."""

    return (
        resolve_user_organisation_membership(
            user_concept_id,
            organisation_concept_id,
        )
        is not None
    )


@_coordinated_membership_mutation
def create_organisation_membership(
    user_concept_id: str, organisation_concept_id: str, role: str = "member"
) -> dict[str, Any]:
    """Create a memberOf relationship between a user and an organisation.

    Stores the relationship in the Vontology as a concept edge, with the role
    stored as a text relation (hasRole predicate) on a context-decorated link.

    Args:
        user_concept_id: The concept ID of the user (e.g., '#V#michael_witbrock')
        organisation_concept_id: The concept ID of the organisation (e.g., '#V#sail')
        role: The user's role in the organisation (default: 'member')

    Returns:
        Dictionary with:
            - 'user_concept_id': The user's concept ID
            - 'organisation_concept_id': The organisation's concept ID
            - 'role': The assigned role
            - 'relationship_created': Boolean indicating if new relationship was created
            - 'relationship_id': The relationship ID (for future reference)

    Raises:
        ValueError: If concept IDs are invalid or empty
        PermissionError: If user lacks access to one or both concepts
    """
    if not user_concept_id or not isinstance(user_concept_id, str):
        raise ValueError("user_concept_id must be a non-empty string")
    if not organisation_concept_id or not isinstance(organisation_concept_id, str):
        raise ValueError("organisation_concept_id must be a non-empty string")

    # Check access permissions
    if not can_access_concept(user_concept_id):
        raise PermissionError(f"Cannot access user concept '{user_concept_id}'")
    if not can_access_concept(organisation_concept_id):
        raise PermissionError(
            f"Cannot access organisation concept '{organisation_concept_id}'"
        )

    # Verify both concepts exist
    user_concept = ConceptsRepository.find_one({"concept_id": user_concept_id})
    if not user_concept:
        raise ValueError(f"User concept '{user_concept_id}' not found")

    org_concept = ConceptsRepository.find_one({"concept_id": organisation_concept_id})
    if not org_concept:
        raise ValueError(f"Organisation concept '{organisation_concept_id}' not found")

    # Create or update the memberOf relationship edge
    relationship_exists = _check_relationship_exists(
        user_concept_id, organisation_concept_id
    )

    if not relationship_exists:
        ConceptsRepository.mutate_relationship_edge(
            source_id=user_concept_id,
            kind=MEMBERSHIP_RELATIONSHIP_KIND,
            target_id=organisation_concept_id,
            action="add",
            maintain_inverse=True,
        )
        relationship_created = True
        logger.info(
            f"Created memberOf relationship: {user_concept_id} --memberOf--> {organisation_concept_id}"
        )
    else:
        relationship_created = False
        logger.info(
            f"memberOf relationship already exists: {user_concept_id} --memberOf--> {organisation_concept_id}"
        )

    if relationship_exists:
        # This API also acts as an idempotent role upsert for an existing
        # membership, so invalidate the exact org's derived role first.
        _invalidate_user_window_authority(
            user_concept_id,
            organisation_concept_id,
        )

    # Store the role via text relation (hasRole predicate with context)
    role_result = upsert_text_for_concept(
        subject_concept_id=user_concept_id,
        predicate=ROLE_PREDICATE,
        text=organisation_role_storage_text(role, organisation_concept_id),
        lang="en",
        context={"organisation_id": organisation_concept_id},
    )

    return {
        "user_concept_id": user_concept_id,
        "organisation_concept_id": organisation_concept_id,
        "role": role,
        "relationship_created": relationship_created,
        "relationship_id": f"{user_concept_id}::{MEMBERSHIP_RELATIONSHIP_KIND}::{organisation_concept_id}",
        "role_text_value_id": role_result.get("text_value_id"),
        "role_relation_id": role_result.get("relation_id"),
    }


def get_user_memberships(user_concept_id: str) -> dict[str, Any]:
    """Retrieve all organisations a user is a member of and their roles.

    Args:
        user_concept_id: The concept ID of the user

    Returns:
        Dictionary with:
            - 'user_concept_id': The user's concept ID
            - 'memberships': List of dicts with 'organisation_concept_id' and 'role'
            - 'total_memberships': Count of organisations
    """
    if not user_concept_id or not isinstance(user_concept_id, str):
        raise ValueError("user_concept_id must be a non-empty string")

    # Membership queries are an internal authority surface. Bypass the session-org
    # access gate so cross-org user and org concepts can always be resolved by
    # authenticated callers regardless of which org window is currently active.
    with bypass_access_control():
        user_concept = ConceptsRepository.find_one({"concept_id": user_concept_id})
    if not user_concept:
        raise ValueError(f"User concept '{user_concept_id}' not found")

    # Get memberOf relationships
    memberships_dict = user_concept.get("relationships", {})
    org_ids = memberships_dict.get(MEMBERSHIP_RELATIONSHIP_KIND, [])
    alt_org_ids = memberships_dict.get(ALT_MEMBERSHIP_RELATIONSHIP, [])

    if isinstance(org_ids, str):
        org_ids = [org_ids]
    if not isinstance(org_ids, list):
        org_ids = []
    if isinstance(alt_org_ids, str):
        alt_org_ids = [alt_org_ids]
    if not isinstance(alt_org_ids, list):
        alt_org_ids = []

    merged_org_ids = []
    seen_orgs: set[str] = set()
    for value in [*org_ids, *alt_org_ids]:
        if not isinstance(value, str):
            continue
        if not value.strip():
            continue
        org_id = value.strip()
        if not org_id.startswith("#V#"):
            org_id = f"#V#{org_id}"
        if org_id in seen_orgs:
            continue
        seen_orgs.add(org_id)
        merged_org_ids.append(org_id)

    # Fetch roles from text relations
    from ..db.repositories.text_value_repository import TextRelationsRepository

    role_relations = TextRelationsRepository.find(
        {"subject_concept_id": user_concept_id, "predicate": ROLE_PREDICATE}
    )

    # Build map of org_id -> role (from context)
    org_role_map = {}
    for rel in role_relations:
        context = rel.get("context") or {}
        org_id = context.get("organisation_id")
        if org_id:
            # Get the text value for the role
            text_value_id = rel.get("object_text_id")
            if text_value_id:
                from ..db.repositories.text_value_repository import TextValuesRepository

                tv = TextValuesRepository.find_one_by_id(text_value_id)
                if tv:
                    role, stored_org = parse_organisation_role_storage_text(
                        tv.get("text"),
                        context,
                    )
                    normalised_org_id = _normalise_concept_id(org_id)
                    if role and stored_org == normalised_org_id:
                        org_role_map[normalised_org_id] = role

    # Build result
    memberships = []
    for org_id in merged_org_ids:
        role = org_role_map.get(
            org_id, "member"
        )  # Default to member if no role specified
        memberships.append({"organisation_concept_id": org_id, "role": role})

    return {
        "user_concept_id": user_concept_id,
        "memberships": memberships,
        "total_memberships": len(memberships),
    }


def get_organisation_members(
    organisation_concept_id: str, role_filter: str | None = None
) -> dict[str, Any]:
    """Retrieve all members of an organisation, optionally filtered by role.

    Args:
        organisation_concept_id: The concept ID of the organisation
        role_filter: Optional role to filter by (e.g., 'admin', 'member')

    Returns:
        Dictionary with:
            - 'organisation_concept_id': The organisation's concept ID
            - 'members': List of dicts with 'user_concept_id' and 'role'
            - 'total_members': Count of members
            - 'role_filter': The role filter applied (if any)
    """
    if not organisation_concept_id or not isinstance(organisation_concept_id, str):
        raise ValueError("organisation_concept_id must be a non-empty string")

    # Membership queries are an internal authority surface. Bypass the session-org
    # access gate so cross-org org concepts can always be resolved by authenticated
    # callers regardless of which org window is currently active.
    with bypass_access_control():
        org_concept = ConceptsRepository.find_one(
            {"concept_id": organisation_concept_id}
        )
    if not org_concept:
        raise ValueError(f"Organisation concept '{organisation_concept_id}' not found")

    # Build map of user_id -> role from text relations (if present)
    from ..db.repositories.text_value_repository import TextRelationsRepository

    role_relations = TextRelationsRepository.find(
        {
            "predicate": ROLE_PREDICATE,
            "context.organisation_id": organisation_concept_id,
        }
    )

    user_role_map: dict[str, str] = {}
    user_ids: set[str] = set()

    for rel in role_relations:
        user_id = rel.get("subject_concept_id")
        if user_id:
            user_ids.add(user_id)
            text_value_id = rel.get("object_text_id")
            if text_value_id:
                from ..db.repositories.text_value_repository import TextValuesRepository

                tv = TextValuesRepository.find_one_by_id(text_value_id)
                if tv:
                    role, stored_org = parse_organisation_role_storage_text(
                        tv.get("text"),
                        rel.get("context")
                        if isinstance(rel.get("context"), dict)
                        else {},
                    )
                    if role and stored_org == _normalise_concept_id(
                        organisation_concept_id
                    ):
                        user_role_map[user_id] = role

    # Fallback: include concepts with membership edges even if no role text relation.
    org_id_variants = {organisation_concept_id}
    if isinstance(organisation_concept_id, str) and organisation_concept_id.startswith(
        "#V#"
    ):
        org_id_variants.add(organisation_concept_id[3:])

    membership_query = {
        "$or": [
            {
                f"relationships.{MEMBERSHIP_RELATIONSHIP_KIND}": {
                    "$in": list(org_id_variants)
                }
            },
            {
                f"relationships.{ALT_MEMBERSHIP_RELATIONSHIP}": {
                    "$in": list(org_id_variants)
                }
            },
        ]
    }
    with bypass_access_control():
        member_docs = list(
            ConceptsRepository.find(membership_query, projection={"concept_id": 1})
        )
    for doc in member_docs:
        concept_id = doc.get("concept_id") if isinstance(doc, dict) else None
        if isinstance(concept_id, str) and concept_id.strip():
            user_ids.add(concept_id.strip())

    # Filter by role if specified
    members = []
    for user_id in user_ids:
        role = user_role_map.get(user_id, "member")
        if role_filter is None or role == role_filter:
            members.append({"user_concept_id": user_id, "role": role})

    return {
        "organisation_concept_id": organisation_concept_id,
        "members": members,
        "total_members": len(members),
        "role_filter": role_filter,
    }


@_coordinated_membership_mutation
def update_user_role(
    user_concept_id: str, organisation_concept_id: str, new_role: str
) -> dict[str, Any]:
    """Update a user's role in an organisation.

    Args:
        user_concept_id: The concept ID of the user
        organisation_concept_id: The concept ID of the organisation
        new_role: The new role to assign

    Returns:
        Dictionary with:
            - 'user_concept_id': The user's concept ID
            - 'organisation_concept_id': The organisation's concept ID
            - 'new_role': The new role
            - 'role_updated': Boolean indicating if role was actually changed
    """
    if not user_concept_id or not isinstance(user_concept_id, str):
        raise ValueError("user_concept_id must be a non-empty string")
    if not organisation_concept_id or not isinstance(organisation_concept_id, str):
        raise ValueError("organisation_concept_id must be a non-empty string")

    if not can_access_concept(user_concept_id):
        raise PermissionError(f"Cannot access user concept '{user_concept_id}'")
    if not can_access_concept(organisation_concept_id):
        raise PermissionError(
            f"Cannot access organisation concept '{organisation_concept_id}'"
        )

    # Verify membership exists
    memberships = get_user_memberships(user_concept_id)
    org_exists = any(
        m["organisation_concept_id"] == organisation_concept_id
        for m in memberships["memberships"]
    )
    if not org_exists:
        raise ValueError(
            f"User '{user_concept_id}' is not a member of organisation '{organisation_concept_id}'"
        )

    # Get current role
    from ..db.repositories.text_value_repository import TextRelationsRepository

    current_role_rel = TextRelationsRepository.find_one(
        {
            "subject_concept_id": user_concept_id,
            "predicate": ROLE_PREDICATE,
            "context.organisation_id": organisation_concept_id,
        }
    )

    old_role = "member"  # Default
    if current_role_rel:
        text_value_id = current_role_rel.get("object_text_id")
        if text_value_id:
            from ..db.repositories.text_value_repository import TextValuesRepository

            tv = TextValuesRepository.find_one_by_id(text_value_id)
            if tv:
                parsed_role, _stored_org = parse_organisation_role_storage_text(
                    tv.get("text"),
                    current_role_rel.get("context")
                    if isinstance(current_role_rel.get("context"), dict)
                    else {},
                )
                if parsed_role:
                    old_role = parsed_role

    # If role is the same, no update needed
    if old_role == new_role:
        return {
            "user_concept_id": user_concept_id,
            "organisation_concept_id": organisation_concept_id,
            "new_role": new_role,
            "role_updated": False,
        }

    # Invalidate before the represented write so a durable-store failure cannot
    # leave a completed role reduction hidden behind another worker's cache.
    _invalidate_user_window_authority(user_concept_id, organisation_concept_id)

    # Update role via upsert
    upsert_text_for_concept(
        subject_concept_id=user_concept_id,
        predicate=ROLE_PREDICATE,
        text=organisation_role_storage_text(new_role, organisation_concept_id),
        lang="en",
        context={"organisation_id": organisation_concept_id},
    )

    logger.info(
        f"Updated role for {user_concept_id} in {organisation_concept_id}: "
        f"{old_role} -> {new_role}"
    )

    return {
        "user_concept_id": user_concept_id,
        "organisation_concept_id": organisation_concept_id,
        "new_role": new_role,
        "role_updated": True,
        "previous_role": old_role,
    }


@_coordinated_membership_mutation
def remove_organisation_membership(
    user_concept_id: str,
    organisation_concept_id: str,
    *,
    semantic_authority_revoked: bool = False,
) -> dict[str, Any]:
    """Remove a user from an organisation.

    Args:
        user_concept_id: The concept ID of the user
        organisation_concept_id: The concept ID of the organisation

    Returns:
        Dictionary with:
            - 'user_concept_id': The user's concept ID
            - 'organisation_concept_id': The organisation's concept ID
            - 'membership_removed': Boolean indicating if membership was removed
    """
    if not user_concept_id or not isinstance(user_concept_id, str):
        raise ValueError("user_concept_id must be a non-empty string")
    if not organisation_concept_id or not isinstance(organisation_concept_id, str):
        raise ValueError("organisation_concept_id must be a non-empty string")

    if not can_access_concept(user_concept_id):
        raise PermissionError(f"Cannot access user concept '{user_concept_id}'")
    if not can_access_concept(organisation_concept_id):
        raise PermissionError(
            f"Cannot access organisation concept '{organisation_concept_id}'"
        )

    # Membership and semantic publication authority are separate represented
    # assertions, but leaving an organisation must not silently leave the
    # stronger authority behind. The caller must revoke the semantic role via
    # its governed lifecycle first, then attest that exact result here.
    from .ontology_authority_role_service import authority_role_read_back
    from .ontology_publication_authority_service import (
        ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
    )

    authority_read_back = authority_role_read_back(
        subject_concept_id=user_concept_id,
        role=ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        organisation_concept_id=organisation_concept_id,
    )
    if authority_read_back.get("active") is True:
        raise PermissionError(
            "organisation_ontology_authority_must_be_revoked_before_membership"
        )
    if semantic_authority_revoked and authority_read_back.get("active") is not False:
        raise RuntimeError("organisation_ontology_authority_revocation_not_verified")

    # See update_user_role: clearing the shared binding before the authoritative
    # mutation makes cross-worker revocation fail closed.
    _invalidate_user_window_authority(user_concept_id, organisation_concept_id)

    # Remove the memberOf relationship
    ConceptsRepository.mutate_relationship_edge(
        source_id=user_concept_id,
        kind=MEMBERSHIP_RELATIONSHIP_KIND,
        target_id=organisation_concept_id,
        action="remove",
        maintain_inverse=True,
    )

    # Remove role text relations for this organisation
    from ..db.repositories.text_value_repository import TextRelationsRepository

    role_relation = TextRelationsRepository.find_one(
        {
            "subject_concept_id": user_concept_id,
            "predicate": ROLE_PREDICATE,
            "context.organisation_id": organisation_concept_id,
        }
    )

    if role_relation:
        TextRelationsRepository.delete_one({"_id": role_relation["_id"]})

    logger.info(
        f"Removed membership: {user_concept_id} removed from {organisation_concept_id}"
    )

    return {
        "user_concept_id": user_concept_id,
        "organisation_concept_id": organisation_concept_id,
        "membership_removed": True,
    }


def _check_relationship_exists(
    user_concept_id: str, organisation_concept_id: str
) -> bool:
    """Check if a memberOf relationship already exists between user and organisation."""
    user_concept = ConceptsRepository.find_one({"concept_id": user_concept_id})
    if not user_concept:
        return False

    memberships_dict = user_concept.get("relationships", {})
    org_ids = memberships_dict.get(MEMBERSHIP_RELATIONSHIP_KIND, [])
    return organisation_concept_id in org_ids


__all__ = [
    "create_organisation_membership",
    "get_organisation_members",
    "get_user_memberships",
    "is_user_member_of_organisation",
    "organisation_role_storage_text",
    "parse_organisation_role_storage_text",
    "remove_organisation_membership",
    "resolve_user_organisation_membership",
    "update_user_role",
]
