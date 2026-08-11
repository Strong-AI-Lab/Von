"""Shared bounds for exact reusable referent identities.

Display text may be shortened for presentation, but opaque identifiers must
never be shortened: a truncated identifier is a different, unusable handle.
Identifiers above the explicit situation/evidence envelope are therefore
omitted rather than corrupted.
"""

from __future__ import annotations

from typing import Any

MAX_EXACT_REFERENT_ID_CHARS = 4_096


def normalise_exact_referent_id(value: Any) -> str | None:
    """Return one exact bounded identifier, or ``None`` when it cannot fit."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    if not value.strip() or len(value) > MAX_EXACT_REFERENT_ID_CHARS:
        return None
    return value


__all__ = ["MAX_EXACT_REFERENT_ID_CHARS", "normalise_exact_referent_id"]
