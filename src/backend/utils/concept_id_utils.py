"""Small helpers for working with Von concept identifiers.

Concept identifiers in Von generally use the form `#V#<slug>`. Some legacy
records (especially older RAG/chat metadata) may have stored the slug without
the `#V#` prefix.

These helpers are intentionally tiny and dependency-free so they can be used in
security-sensitive pathways (e.g. permission checks) and easily unit tested.
"""

from __future__ import annotations

import re
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


_NON_ALNUM_RUN_RE = re.compile(r"[^a-z0-9]+")


def canonicalise_vontology_concept_id(value: object) -> Optional[str]:
    """Canonicalise a Vontology concept identifier.

    Canonical form:
    - Always `#V#<slug>`
    - Slug lowercased
    - Each maximal span of non-alphanumeric characters becomes a single underscore
    - Leading/trailing underscores are trimmed

    Examples:
    - `#V#foo-bar` -> `#V#foo_bar`
    - `#V#Foo  Bar` -> `#V#foo_bar`
    - `#v#foo...bar` -> `#V#foo_bar`
    """

    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None

    # Accept common variants and coerce to #V#...
    if raw.lower().startswith("#v#"):
        slug = raw[3:]
    else:
        prefixed = ensure_v_concept_prefix(raw)
        if prefixed is None:
            return None
        slug = prefixed[3:]

    slug = slug.strip().lower()
    if not slug:
        return None

    slug = _NON_ALNUM_RUN_RE.sub("_", slug).strip("_")
    if not slug:
        return None

    return f"#V#{slug}"
