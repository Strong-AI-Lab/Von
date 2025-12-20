from __future__ import annotations
from typing import Optional


def safe_strip(value: Optional[str]) -> Optional[str]:
    """Return value.strip() if value is a str, else the original None.
    Used to avoid Optional member access warnings when normalizing strings.
    """
    return value.strip() if isinstance(value, str) else value
