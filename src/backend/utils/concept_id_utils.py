"""Small helpers for working with Von concept identifiers.

Concept identifiers in Von generally use the form `#V#<slug>`. Some legacy
records (especially older RAG/chat metadata) may have stored the slug without
the `#V#` prefix.

These helpers are intentionally tiny and dependency-free so they can be used in
security-sensitive pathways (e.g. permission checks) and easily unit tested.

Unicode Strategy (JVNAUTOSCI-945):
----------------------------------
Concept IDs use ASCII-only slugs for maximum compatibility and predictability:

1. **Diacritic transliteration**: Accented Latin characters are transliterated
   to ASCII equivalents (café→cafe, Ñoño→Nono). This uses NFKD decomposition
   followed by combining-mark removal.

2. **Non-Latin scripts**: CJK and other scripts are not currently supported in
   IDs. Concepts with non-Latin names should use a transliterated or English ID,
   with the native-script name stored as a text relation (hasName).

3. **Lookup normalization**: When looking up concepts by name, inputs are
   normalized via NFKC before comparison to handle equivalent encodings.

4. **Human-readable IDs are editable**: Since concepts have stable GUIDs, the
   `#V#...` ID can be renamed. Old IDs are preserved as CODE-type aliases for
   backwards compatibility.

Future consideration: full Unicode IDs with NFC normalization could be supported
if there's demand, but ASCII-only avoids comparison pitfalls across platforms.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional, Tuple


def _transliterate_to_ascii(text: str) -> str:
    """Transliterate accented characters to their ASCII equivalents.

    Uses Unicode NFKD normalization to decompose accented characters,
    then filters out combining marks (diacritics).

    Examples:
        - "café" -> "cafe"
        - "Ñoño" -> "Nono"
        - "Sorbonne Université" -> "Sorbonne Universite"
        - "naïve" -> "naive"
    """
    # NFKD decomposition splits characters like "é" into "e" + combining accent
    normalized = unicodedata.normalize("NFKD", text)
    # Filter out combining characters (category starts with 'M' for Mark)
    return "".join(c for c in normalized if not unicodedata.category(c).startswith("M"))


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
    - Accented characters transliterated to ASCII equivalents (JVNAUTOSCI-944)
    - Each maximal span of non-alphanumeric characters becomes a single underscore
    - Leading/trailing underscores are trimmed

    Examples:
    - `#V#foo-bar` -> `#V#foo_bar`
    - `#V#Foo  Bar` -> `#V#foo_bar`
    - `#v#foo...bar` -> `#V#foo_bar`
    - `#V#café` -> `#V#cafe`
    - `#V#Sorbonne_Université` -> `#V#sorbonne_universite`
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

    # JVNAUTOSCI-944: Transliterate accented characters to ASCII before canonicalisation
    slug = _transliterate_to_ascii(slug)

    slug = _NON_ALNUM_RUN_RE.sub("_", slug).strip("_")
    if not slug:
        return None

    return f"#V#{slug}"


def normalise_for_lookup(value: str) -> str:
    """Normalise a string for concept lookup comparison.

    Uses NFKC normalization to handle equivalent Unicode representations,
    then lowercases. This handles cases like:
    - Composed vs decomposed accents (é vs e + combining acute)
    - Compatibility characters (ﬁ vs fi)
    - Different width forms (ａ vs a)

    Args:
        value: The string to normalise.

    Returns:
        NFKC-normalised lowercase string.
    """
    return unicodedata.normalize("NFKC", value).lower()


def validate_concept_id_for_rename(
    old_id: str, new_id: str
) -> Tuple[Optional[str], Optional[str]]:
    """Validate a concept ID rename request.

    Args:
        old_id: The current concept_id.
        new_id: The proposed new concept_id.

    Returns:
        Tuple of (canonical_old_id, canonical_new_id) if valid,
        or (None, error_message) if invalid.
    """
    canonical_old = canonicalise_vontology_concept_id(old_id)
    if not canonical_old:
        return None, "Invalid current concept_id format"

    canonical_new = canonicalise_vontology_concept_id(new_id)
    if not canonical_new:
        return None, "Invalid new concept_id format"

    if canonical_old == canonical_new:
        return None, "New concept_id is the same as current (after canonicalisation)"

    # Ensure new ID has reasonable length
    slug = canonical_new[3:]  # Strip #V#
    if len(slug) < 1:
        return None, "Concept ID slug cannot be empty"
    if len(slug) > 200:
        return None, "Concept ID slug exceeds maximum length (200 characters)"

    return canonical_new, None
