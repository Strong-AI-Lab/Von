from __future__ import annotations
from datetime import datetime, timezone
from typing import Union

__all__ = ["utc_now", "utc_iso_now"]

def utc_now() -> datetime:
    """Return a timezone-aware UTC datetime.
    Central helper to avoid scattered datetime.now(timezone.utc) calls and
    ease future change (e.g., tracing injection or clock abstraction in tests).
    """
    return datetime.now(timezone.utc)

def utc_iso_now() -> str:
    """Return an ISO8601 Z-normalized UTC timestamp (no offset variation)."""
    return utc_now().isoformat().replace('+00:00', 'Z')
