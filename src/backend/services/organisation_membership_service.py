"""Service functions for managing organisation membership relationships in the Vontology.

Handles creation, retrieval, and management of memberOf relationships between
user concepts and organisation concepts, including role associations.
"""

from __future__ import annotations

import logging
from typing import Dict, Any, Optional, List, Set

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import can_access_concept
from ..services.text_value_service import upsert_text_for_concept

logger = logging.getLogger(__name__)

# Constants for relationship kinds
MEMBERSHIP_RELATIONSHIP_KIND = "memberOf"


def create_organisation_membership(
    user_concept_id: str,
    organisation_concept_id: str,
    role: str = "member"
) -> Dict[str, Any]:
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
        raise PermissionError(f"Cannot access organisation concept '{organisation_concept_id}'")

    # Verify both concepts exist
    user_concept = ConceptsRepository.find_one({"concept_id": user_concept_id})
    if not user_concept:
        raise ValueError(f"User concept '{user_concept_id}' not found")

    org_concept = ConceptsRepository.find_one({"concept_id": organisation_concept_id})
    if not org_concept:
        raise ValueError(f"Organisation concept '{organisation_concept_id}' not found")

    # Create or update the memberOf relationship edge
    relationship_exists = _check_relationship_exists(user_concept_id, organisation_concept_id)

    if not relationship_exists:
        ConceptsRepository.mutate_relationship_edge(
            source_id=user_concept_id,
            kind=MEMBERSHIP_RELATIONSHIP_KIND,
            target_id=organisation_concept_id,
            action="add",
            maintain_inverse=True
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

    # Store the role via text relation (hasRole predicate with context)
    role_result = upsert_text_for_concept(
        subject_concept_id=user_concept_id,
        predicate="hasRole",
        text=role,
        lang="en",
        context={"organisation_id": organisation_concept_id}
    )

    return {
        "user_concept_id": user_concept_id,
        "organisation_concept_id": organisation_concept_id,
        "role": role,
        "relationship_created": relationship_created,
        "relationship_id": f"{user_concept_id}::{MEMBERSHIP_RELATIONSHIP_KIND}::{organisation_concept_id}",
        "role_text_value_id": role_result.get("text_value_id"),
        "role_relation_id": role_result.get("relation_id")
    }


def get_user_memberships(user_concept_id: str) -> Dict[str, Any]:
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

    if not can_access_concept(user_concept_id):
        raise PermissionError(f"Cannot access user concept '{user_concept_id}'")

    user_concept = ConceptsRepository.find_one({"concept_id": user_concept_id})
    if not user_concept:
        raise ValueError(f"User concept '{user_concept_id}' not found")

    # Get memberOf relationships
    memberships_dict = user_concept.get("relationships", {})
    org_ids = memberships_dict.get(MEMBERSHIP_RELATIONSHIP_KIND, [])

    # Fetch roles from text relations
    from ..db.repositories.text_value_repository import TextRelationsRepository
    role_relations = TextRelationsRepository.find({
        "subject_concept_id": user_concept_id,
        "predicate": "hasRole"
    })

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
                tv = TextValuesRepository.find_one({"_id": text_value_id})
                if tv:
                    org_role_map[org_id] = tv.get("text", "member")

    # Build result
    memberships = []
    for org_id in org_ids:
        role = org_role_map.get(org_id, "member")  # Default to member if no role specified
        memberships.append({
            "organisation_concept_id": org_id,
            "role": role
        })

    return {
        "user_concept_id": user_concept_id,
        "memberships": memberships,
        "total_memberships": len(memberships)
    }


def get_organisation_members(
    organisation_concept_id: str,
    role_filter: Optional[str] = None
) -> Dict[str, Any]:
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

    if not can_access_concept(organisation_concept_id):
        raise PermissionError(f"Cannot access organisation concept '{organisation_concept_id}'")

    org_concept = ConceptsRepository.find_one({"concept_id": organisation_concept_id})
    if not org_concept:
        raise ValueError(f"Organisation concept '{organisation_concept_id}' not found")

    # Find inverse memberOf relationships (users who are members of this org)
    # In the relationships structure, memberOf relationships on the org concept point inward
    # We need to find all user concepts that have memberOf pointing to this org

    from ..db.repositories.text_value_repository import TextRelationsRepository

    # Find all text relations with this org in the context (role associations)
    role_relations = TextRelationsRepository.find({
        "predicate": "hasRole",
        "context.organisation_id": organisation_concept_id
    })

    # Build map of user_id -> role
    user_role_map: Dict[str, str] = {}
    user_ids: Set[str] = set()

    for rel in role_relations:
        user_id = rel.get("subject_concept_id")
        if user_id:
            user_ids.add(user_id)
            # Get the text value for the role
            text_value_id = rel.get("object_text_id")
            if text_value_id:
                from ..db.repositories.text_value_repository import TextValuesRepository
                tv = TextValuesRepository.find_one({"_id": text_value_id})
                if tv:
                    user_role_map[user_id] = tv.get("text", "member")
                else:
                    user_role_map[user_id] = "member"  # Default if text value not found

    # Filter by role if specified
    members = []
    for user_id in user_ids:
        role = user_role_map.get(user_id, "member")
        if role_filter is None or role == role_filter:
            members.append({
                "user_concept_id": user_id,
                "role": role
            })

    return {
        "organisation_concept_id": organisation_concept_id,
        "members": members,
        "total_members": len(members),
        "role_filter": role_filter
    }


def update_user_role(
    user_concept_id: str,
    organisation_concept_id: str,
    new_role: str
) -> Dict[str, Any]:
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
        raise PermissionError(f"Cannot access organisation concept '{organisation_concept_id}'")

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
    current_role_rel = TextRelationsRepository.find_one({
        "subject_concept_id": user_concept_id,
        "predicate": "hasRole",
        "context.organisation_id": organisation_concept_id
    })

    old_role = "member"  # Default
    if current_role_rel:
        text_value_id = current_role_rel.get("object_text_id")
        if text_value_id:
            from ..db.repositories.text_value_repository import TextValuesRepository
            tv = TextValuesRepository.find_one({"_id": text_value_id})
            if tv:
                old_role = tv.get("text", "member")

    # If role is the same, no update needed
    if old_role == new_role:
        return {
            "user_concept_id": user_concept_id,
            "organisation_concept_id": organisation_concept_id,
            "new_role": new_role,
            "role_updated": False
        }

    # Update role via upsert
    upsert_text_for_concept(
        subject_concept_id=user_concept_id,
        predicate="hasRole",
        text=new_role,
        lang="en",
        context={"organisation_id": organisation_concept_id}
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
        "previous_role": old_role
    }


def remove_organisation_membership(
    user_concept_id: str,
    organisation_concept_id: str
) -> Dict[str, Any]:
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
        raise PermissionError(f"Cannot access organisation concept '{organisation_concept_id}'")

    # Remove the memberOf relationship
    ConceptsRepository.mutate_relationship_edge(
        source_id=user_concept_id,
        kind=MEMBERSHIP_RELATIONSHIP_KIND,
        target_id=organisation_concept_id,
        action="remove",
        maintain_inverse=True
    )

    # Remove role text relations for this organisation
    from ..db.repositories.text_value_repository import TextRelationsRepository
    role_relation = TextRelationsRepository.find_one({
        "subject_concept_id": user_concept_id,
        "predicate": "hasRole",
        "context.organisation_id": organisation_concept_id
    })

    if role_relation:
        TextRelationsRepository.delete_one({"_id": role_relation["_id"]})

    logger.info(
        f"Removed membership: {user_concept_id} removed from {organisation_concept_id}"
    )

    return {
        "user_concept_id": user_concept_id,
        "organisation_concept_id": organisation_concept_id,
        "membership_removed": True
    }


def _check_relationship_exists(user_concept_id: str, organisation_concept_id: str) -> bool:
    """Check if a memberOf relationship already exists between user and organisation."""
    user_concept = ConceptsRepository.find_one({"concept_id": user_concept_id})
    if not user_concept:
        return False

    memberships_dict = user_concept.get("relationships", {})
    org_ids = memberships_dict.get(MEMBERSHIP_RELATIONSHIP_KIND, [])
    return organisation_concept_id in org_ids


__all__ = [
    "create_organisation_membership",
    "get_user_memberships",
    "get_organisation_members",
    "update_user_role",
    "remove_organisation_membership",
]
