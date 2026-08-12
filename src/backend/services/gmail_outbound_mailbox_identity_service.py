"""Privacy-preserving identity for one canonical Gmail mailbox.

Gmail runtime profile names and represented profile resources are authority and
audit aliases, not durable mailbox identity.  This module converts the primary
address returned by Gmail ``users.getProfile`` into an opaque, stable key.  The
raw address must remain transient at the integration boundary and must never be
written to the outbound delivery or quota collections.
"""

from __future__ import annotations

import hashlib
import json
import re
from email.utils import getaddresses
from typing import Any

GMAIL_CANONICAL_MAILBOX_KEY_SCHEMA_VERSION = "gmail_canonical_mailbox_key.v1"
GMAIL_CANONICAL_MAILBOX_KEY_PREFIX = "gmail-mailbox-sha256:"
_GMAIL_CANONICAL_MAILBOX_KEY_PATTERN = re.compile(
    rf"^{re.escape(GMAIL_CANONICAL_MAILBOX_KEY_PREFIX)}[0-9a-f]{{64}}$"
)


def normalise_canonical_gmail_address(value: Any) -> str:
    """Validate and normalise Gmail's primary-address response in memory only."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("Gmail profile did not expose its canonical sender")
    parsed = [
        address.strip().casefold()
        for _display_name, address in getaddresses([value.strip()])
        if address.strip()
    ]
    if len(parsed) != 1:
        raise ValueError("Gmail profile exposed an invalid canonical sender")
    address = parsed[0]
    local_part, separator, domain = address.rpartition("@")
    if (
        separator != "@"
        or not local_part
        or not domain
        or any(character.isspace() for character in address)
    ):
        raise ValueError("Gmail profile exposed an invalid canonical sender")
    return address


def derive_canonical_gmail_mailbox_key(value: Any) -> str:
    """Derive an opaque deterministic key from Gmail's canonical address."""

    canonical_address = normalise_canonical_gmail_address(value)
    encoded = json.dumps(
        {
            "schema_version": GMAIL_CANONICAL_MAILBOX_KEY_SCHEMA_VERSION,
            "canonical_address": canonical_address,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return GMAIL_CANONICAL_MAILBOX_KEY_PREFIX + hashlib.sha256(encoded).hexdigest()


def require_canonical_gmail_mailbox_key(value: Any) -> str:
    """Accept only keys produced by :func:`derive_canonical_gmail_mailbox_key`."""

    cleaned = str(value or "").strip()
    if not _GMAIL_CANONICAL_MAILBOX_KEY_PATTERN.fullmatch(cleaned):
        raise ValueError("canonical_mailbox_key must be an opaque Gmail mailbox key")
    return cleaned


__all__ = [
    "GMAIL_CANONICAL_MAILBOX_KEY_PREFIX",
    "GMAIL_CANONICAL_MAILBOX_KEY_SCHEMA_VERSION",
    "derive_canonical_gmail_mailbox_key",
    "normalise_canonical_gmail_address",
    "require_canonical_gmail_mailbox_key",
]
