from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from pymongo.collection import Collection

from backend.db.mongo_client import get_agent_gmail_tokens_collection
from backend.security.token_encryption import encrypt_json, decrypt_json
from backend.utils.time_utils import utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentGmailTokenStatus:
    profile_id: str
    has_tokens: bool
    authorised_email: Optional[str]
    expires_at: Optional[datetime]
    scopes: list[str]


class AgentGmailTokenStoreError(RuntimeError):
    pass


def _get_collection() -> Collection:
    coll = get_agent_gmail_tokens_collection()
    if coll is None:
        raise AgentGmailTokenStoreError(
            "Agent Gmail token collection unavailable (database connection failed)."
        )
    return coll


def upsert_agent_gmail_tokens(
    *,
    profile_id: str,
    token_payload: Mapping[str, Any],
    authorised_email: str | None = None,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
) -> None:
    """Create or update encrypted token payload for the given profile.

    token_payload should contain sensitive fields (access_token, refresh_token, etc.).
    The payload is encrypted at rest.
    """

    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("profile_id must be a non-empty string")

    # Store sensitive payload encrypted; keep non-sensitive metadata plaintext.
    encrypted = encrypt_json(dict(token_payload))
    now = utc_now()

    coll = _get_collection()
    coll.update_one(
        {"profile_id": profile_id},
        {
            "$set": {
                "profile_id": profile_id,
                "authorised_email": authorised_email,
                "scopes": list(scopes or []),
                "expires_at": expires_at,
                "token_payload_enc": encrypted,
                "encryption": {
                    "scheme": "fernet",
                    "env_var": "GMAIL_TOKEN_ENCRYPTION_KEY",
                },
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )

    # Never log token material.
    logger.info(
        "Stored agent Gmail tokens for profile_id=%s email=%s",
        profile_id,
        authorised_email or "(unknown)",
    )


def get_agent_gmail_token_payload(profile_id: str) -> Optional[dict[str, Any]]:
    """Return decrypted token payload, or None if not present."""

    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("profile_id must be a non-empty string")

    coll = _get_collection()
    doc = coll.find_one({"profile_id": profile_id})
    if not doc:
        return None

    token_enc = doc.get("token_payload_enc")
    if not isinstance(token_enc, str) or not token_enc:
        return None

    return decrypt_json(token_enc)


def get_agent_gmail_token_status(profile_id: str) -> AgentGmailTokenStatus:
    """Return non-sensitive status about stored tokens."""

    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("profile_id must be a non-empty string")

    coll = _get_collection()
    doc = coll.find_one({"profile_id": profile_id})
    if not doc:
        return AgentGmailTokenStatus(
            profile_id=profile_id,
            has_tokens=False,
            authorised_email=None,
            expires_at=None,
            scopes=[],
        )

    token_enc = doc.get("token_payload_enc")
    has_tokens = isinstance(token_enc, str) and bool(token_enc)

    scopes_raw = doc.get("scopes")
    scopes = (
        [s for s in scopes_raw if isinstance(s, str)]
        if isinstance(scopes_raw, list)
        else []
    )

    expires_at = doc.get("expires_at")
    if not isinstance(expires_at, datetime):
        expires_at = None

    authorised_email = doc.get("authorised_email")
    if not isinstance(authorised_email, str):
        authorised_email = None

    return AgentGmailTokenStatus(
        profile_id=profile_id,
        has_tokens=has_tokens,
        authorised_email=authorised_email,
        expires_at=expires_at,
        scopes=scopes,
    )


def revoke_agent_gmail_tokens(profile_id: str) -> bool:
    """Delete stored tokens for the profile."""

    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("profile_id must be a non-empty string")

    coll = _get_collection()
    result = coll.delete_one({"profile_id": profile_id})
    deleted = bool(result.deleted_count)
    logger.info(
        "Revoked agent Gmail tokens for profile_id=%s deleted=%s", profile_id, deleted
    )
    return deleted
