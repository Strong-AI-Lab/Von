"""
Namespace service for user@org composite namespaces in RAG system.

This service handles derivation, parsing, and validation of composite namespaces
that combine user_id and org_id to enable multi-tenant content isolation.

Format: #V#{user_id}@{org_id}
Example: #V#michael_witbrock@sail
"""

import re
from typing import Any, Dict, Optional, Tuple


def derive_namespace(
    user_id: str, org_id: Optional[str] = None, role: Optional[str] = None
) -> str:
    """
    Derive a composite namespace from user and org identifiers.

    Args:
        user_id: User concept identifier (e.g., "michael_witbrock")
        org_id: Organisation concept identifier (e.g., "sail"), optional
        role: User's role in the organisation (stored separately, not in namespace)

    Returns:
        Composite namespace string in format "#V#{user_id}@{org_id}" or "#V#{user_id}"

    Examples:
        >>> derive_namespace("michael_witbrock", "sail")
        '#V#michael_witbrock@sail'
        >>> derive_namespace("michael_witbrock")
        '#V#michael_witbrock'
    """
    if not user_id:
        raise ValueError("user_id is required")

    # Validate format (alphanumeric, underscores only)
    if not re.match(r"^[a-z0-9_]+$", user_id, re.IGNORECASE):
        raise ValueError(f"user_id contains invalid characters: {user_id}")

    if org_id:
        if not re.match(r"^[a-z0-9_]+$", org_id, re.IGNORECASE):
            raise ValueError(f"org_id contains invalid characters: {org_id}")
        return f"#V#{user_id}@{org_id}"
    else:
        return f"#V#{user_id}"


def concept_id_to_namespace_slug(value: Any) -> Optional[str]:
    """Return a namespace slug from a concept ID or already-slugged value."""

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("#V#"):
        cleaned = cleaned[3:]
    cleaned = cleaned.strip()
    return cleaned or None


def derive_namespace_for_actor(
    user_id: Any,
    org_id: Any = None,
) -> Optional[str]:
    """Derive a canonical namespace from concept IDs or namespace slugs."""

    user_slug = concept_id_to_namespace_slug(user_id)
    if not user_slug:
        return None
    org_slug = concept_id_to_namespace_slug(org_id)
    try:
        return derive_namespace(user_slug, org_slug)
    except ValueError:
        return None


def coerce_namespace(value: Any) -> Optional[str]:
    """Normalise common namespace encodings to canonical ``#V#user@org`` form."""

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None

    if cleaned.startswith("#V#"):
        body = cleaned[3:].strip()
        if not body:
            return None
        if "@" in body:
            user_raw, org_raw = body.split("@", 1)
            return derive_namespace_for_actor(user_raw, org_raw)
        if "/" in body:
            user_raw, org_raw = body.split("/", 1)
            return derive_namespace_for_actor(user_raw, org_raw)
        return derive_namespace_for_actor(body)

    if "@" in cleaned:
        user_raw, org_raw = cleaned.split("@", 1)
        return derive_namespace_for_actor(user_raw, org_raw)
    if "/" in cleaned:
        user_raw, org_raw = cleaned.split("/", 1)
        return derive_namespace_for_actor(user_raw, org_raw)
    return derive_namespace_for_actor(cleaned)


def resolve_canonical_namespace(
    namespace: Any,
    user_id: Any = None,
    org_id: Any = None,
    *,
    preserve_explicit: bool = False,
) -> Optional[str]:
    """Prefer canonical explicit namespace text, otherwise derive from actor IDs.

    This helper lets workflow write paths converge on one authoritative namespace
    derivation pathway while still allowing bounded compatibility for legacy read
    surfaces or explicit caller-provided opaque identifiers when requested.
    """

    cleaned_explicit = None
    if isinstance(namespace, str):
        cleaned_explicit = namespace.strip() or None

    canonical = coerce_namespace(cleaned_explicit)
    if canonical:
        return canonical

    derived = derive_namespace_for_actor(user_id, org_id)
    if derived:
        return derived

    if preserve_explicit:
        return cleaned_explicit

    return None


def derive_actor_context_from_namespace(
    namespace: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """Infer canonical concept IDs from a namespace string when possible.

    This helper keeps namespace-as-scope and actor identity aligned without
    treating the full namespace string as a user concept ID. It accepts the
    same compatibility forms as ``coerce_namespace`` and returns canonical
    ``#V#...`` concept IDs for the user and optional organisation components.
    """

    canonical_namespace = coerce_namespace(namespace)
    if canonical_namespace is None:
        return None, None

    try:
        parsed = parse_namespace(canonical_namespace)
    except ValueError:
        return None, None

    user_id = parsed.get("user_id")
    org_id = parsed.get("org_id")
    user_concept_id = f"#V#{user_id}" if isinstance(user_id, str) and user_id else None
    org_concept_id = f"#V#{org_id}" if isinstance(org_id, str) and org_id else None
    return user_concept_id, org_concept_id


def parse_namespace(namespace: str) -> Dict[str, Optional[str]]:
    """
    Parse a composite namespace into components.

    Args:
        namespace: Namespace string to parse (e.g., "#V#michael_witbrock@sail" or "#V#michael_witbrock")

    Returns:
        Dictionary with keys: user_id, org_id (org_id may be None for user-only namespaces)

    Raises:
        ValueError: If namespace format is invalid

    Examples:
        >>> parse_namespace("#V#michael_witbrock@sail")
        {'user_id': 'michael_witbrock', 'org_id': 'sail'}
        >>> parse_namespace("#V#michael_witbrock")
        {'user_id': 'michael_witbrock', 'org_id': None}
    """
    if not namespace or not isinstance(namespace, str):
        raise ValueError(f"Invalid namespace: {namespace}")

    # Remove #V# prefix
    if not namespace.startswith("#V#"):
        raise ValueError(f"Namespace must start with #V#: {namespace}")

    content = namespace[3:]  # Remove "#V#"

    # Check for org_id (contains @)
    if "@" in content:
        parts = content.split("@")
        if len(parts) != 2:
            raise ValueError(
                f"Invalid namespace format (multiple @ symbols): {namespace}"
            )

        user_id, org_id = parts
        if not user_id or not org_id:
            raise ValueError(f"Empty user_id or org_id in namespace: {namespace}")

        return {"user_id": user_id, "org_id": org_id}
    else:
        # User-only namespace
        return {"user_id": content, "org_id": None}


def is_org_scoped(namespace: str) -> bool:
    """
    Check if a namespace includes org scope.

    Args:
        namespace: Namespace to check

    Returns:
        True if namespace includes org_id (e.g., "#V#user@org"), False otherwise
    """
    try:
        parsed = parse_namespace(namespace)
        return parsed.get("org_id") is not None
    except ValueError:
        return False


def get_user_id(namespace: str) -> str:
    """
    Extract user_id from a namespace.

    Args:
        namespace: Namespace string

    Returns:
        User ID extracted from namespace

    Raises:
        ValueError: If namespace format is invalid
    """
    parsed = parse_namespace(namespace)
    user_id = parsed.get("user_id")
    if not user_id:
        raise ValueError(f"Namespace missing user_id: {namespace}")

    return user_id


def get_org_id(namespace: str) -> Optional[str]:
    """
    Extract org_id from a namespace.

    Args:
        namespace: Namespace string

    Returns:
        Organisation ID or None if namespace is user-only

    Raises:
        ValueError: If namespace format is invalid
    """
    return parse_namespace(namespace).get("org_id")


def normalize_namespace(namespace: str) -> str:
    """
    Validate and return a normalised namespace.

    Args:
        namespace: Namespace to normalise

    Returns:
        Normalised namespace (ensures consistent format)

    Raises:
        ValueError: If namespace format is invalid
    """
    parsed = parse_namespace(namespace)
    user_id = parsed.get("user_id")
    if not user_id:
        raise ValueError(f"Namespace missing user_id: {namespace}")

    return derive_namespace(user_id, parsed.get("org_id"))
