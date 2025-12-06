"""
Stub role resolver for Phase 1 implementation.

This is a temporary implementation with hardcoded mappings. It will be replaced
in Phase 3 with a comprehensive role-based access control (RBAC) system backed
by the Vontology database.

Phase 1 Purpose:
- Provide a working role resolver for testing and basic functionality
- Establish the resolver interface that Phase 3 will build upon
- Enable role metadata in RAG documents

Phase 3 Will Replace:
- Replace hardcoded mappings with database queries
- Implement role hierarchy and inheritance
- Add permission-based access control
"""

from typing import Dict, Optional, Set


# Hardcoded role mappings for Phase 1
# Format: {user_id: {org_id: role_name}}
# These will be replaced with database-backed resolution in Phase 3
STUB_ROLE_MAPPINGS = {
    "michael_witbrock": {
        "sail": "admin",
    },
    "von_archivist": {
        "sail": "member",
    },
}

# Default role for new members
DEFAULT_ROLE = "member"

# Available roles in stub system (will be expanded in Phase 3)
AVAILABLE_ROLES = {"viewer", "member", "contributor", "admin", "owner"}


def get_user_role(user_id: str, org_id: str) -> str:
    """
    Get the role of a user in an organisation.

    This is a stub implementation. In Phase 3, this will query the Vontology
    database for role assignments with proper hierarchy resolution.

    Args:
        user_id: User concept identifier (e.g., "michael_witbrock")
        org_id: Organisation concept identifier (e.g., "sail")

    Returns:
        Role name (e.g., "admin", "member", "viewer")

    Note:
        Always returns a valid role string, never None.
        Falls back to DEFAULT_ROLE if no explicit mapping exists.
    """
    if not user_id or not org_id:
        return DEFAULT_ROLE

    # Check hardcoded mappings
    if user_id in STUB_ROLE_MAPPINGS:
        if org_id in STUB_ROLE_MAPPINGS[user_id]:
            return STUB_ROLE_MAPPINGS[user_id][org_id]

    # Default to member role for unmapped users
    return DEFAULT_ROLE


def get_effective_permissions(role: str) -> Set[str]:
    """
    Get the set of permissions for a role.

    This is a stub implementation. In Phase 3, this will resolve role hierarchy
    and inherit permissions from parent roles.

    Current stub implementation:
    - All roles can READ_ORG_CONTENT
    - Only admin/contributor can WRITE_ORG_CONTENT
    - Only admin can MANAGE_MEMBERS, MANAGE_ROLES
    - Only owner can DELETE_ORG

    Args:
        role: Role name

    Returns:
        Set of permission strings

    Note:
        This stub does not implement true hierarchy. Phase 3 will add:
        - Role inheritance (admin inherits member permissions)
        - Dynamic permission resolution from Vontology
        - Permission caching with TTL
    """
    permissions = {"READ_ORG_CONTENT"}  # Everyone can read

    if role in ("contributor", "admin", "owner"):
        permissions.add("WRITE_ORG_CONTENT")

    if role in ("admin", "owner"):
        permissions.add("MANAGE_MEMBERS")
        permissions.add("MANAGE_ROLES")

    if role == "owner":
        permissions.add("DELETE_ORG")

    return permissions


def has_permission(user_id: str, org_id: str, permission: str) -> bool:
    """
    Check if a user has a specific permission in an organisation.

    This is a convenience function that combines get_user_role and
    get_effective_permissions.

    Args:
        user_id: User concept identifier
        org_id: Organisation concept identifier
        permission: Permission name to check

    Returns:
        True if user has the permission, False otherwise
    """
    role = get_user_role(user_id, org_id)
    permissions = get_effective_permissions(role)
    return permission in permissions


# Migration helper for Phase 2
def get_all_user_organisations(user_id: str) -> Dict[str, str]:
    """
    Get all organisations a user is a member of and their roles.

    This is a helper for Phase 2 UI integration to populate the organisation
    selector. It queries the stub mappings.

    Args:
        user_id: User concept identifier

    Returns:
        Dictionary mapping org_id to role_name
        Empty dict if user has no organisation memberships
    """
    return STUB_ROLE_MAPPINGS.get(user_id, {})


# Phase 3 Note: This module will be significantly refactored to:
# 1. Query role assignments from Vontology via relationships
# 2. Resolve role hierarchy (owner > admin > contributor > member > viewer)
# 3. Cache role resolutions with TTL
# 4. Support custom roles created in Phase 3
# 5. Implement permission inheritance chains
