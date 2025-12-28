"""Small helpers for working with Von concept identifiers.

Concept identifiers in Von generally use the form `#V#<slug>`. Some legacy
records (especially older RAG/chat metadata) may have stored the slug without
the `#V#` prefix.

These helpers are intentionally tiny and dependency-free so they can be used in
security-sensitive pathways (e.g. permission checks) and easily unit tested.
"""

from __future__ import annotations

from typing import Optional


def normalise_concept_id_for_compare(value: object) -> Optional[str]:
    """Return a normalised concept id suitable for equality comparisons.

    Behaviour:
    - `#V#foo` -> `foo`
    - `foo` -> `foo`
    - None / non-str / blank -> None
    """

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("#V#"):
        return cleaned[3:]
    return cleaned


def ensure_v_concept_prefix(value: object) -> Optional[str]:
    """Ensure a concept id is in `#V#...` form.

    Behaviour:
    - `#V#foo` -> `#V#foo`
    - `foo` -> `#V#foo`
    - `#foo` -> `#V#foo`
    - None / non-str / blank -> None
    """

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.startswith("#V#"):
        return cleaned
    if cleaned.startswith("#"):
        cleaned = cleaned.lstrip("#")
    return f"#V#{cleaned}"
