from __future__ import annotations

import json
import os
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken


DEFAULT_GMAIL_TOKEN_KEY_ENV_VAR = "GMAIL_TOKEN_ENCRYPTION_KEY"


class TokenEncryptionError(RuntimeError):
    pass


def _load_fernet_key(*, env_var: str = DEFAULT_GMAIL_TOKEN_KEY_ENV_VAR) -> bytes:
    key = (os.getenv(env_var) or "").strip()
    if not key:
        raise TokenEncryptionError(
            f"Missing {env_var}. Set it to a Fernet key (base64 urlsafe 32-byte key)."
        )
    return key.encode("utf-8")


def get_fernet(*, env_var: str = DEFAULT_GMAIL_TOKEN_KEY_ENV_VAR) -> Fernet:
    """Return a Fernet instance configured from env.

    This helper centralises loading the encryption key so all token storage uses a
    consistent scheme.
    """

    return Fernet(_load_fernet_key(env_var=env_var))


def encrypt_json(payload: Mapping[str, Any], *, env_var: str = DEFAULT_GMAIL_TOKEN_KEY_ENV_VAR) -> str:
    """Encrypt a JSON-serialisable mapping and return a string token."""

    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except Exception as e:
        raise TokenEncryptionError(f"Failed to serialise payload for encryption: {e}")

    token = get_fernet(env_var=env_var).encrypt(raw)
    return token.decode("utf-8")


def decrypt_json(token: str, *, env_var: str = DEFAULT_GMAIL_TOKEN_KEY_ENV_VAR) -> dict[str, Any]:
    """Decrypt a token into a dict.

    Raises TokenEncryptionError on any failure.
    """

    if not isinstance(token, str) or not token:
        raise TokenEncryptionError("Token must be a non-empty string")

    try:
        raw = get_fernet(env_var=env_var).decrypt(token.encode("utf-8"))
    except InvalidToken as e:
        raise TokenEncryptionError("Invalid token or wrong encryption key") from e
    except Exception as e:
        raise TokenEncryptionError(f"Failed to decrypt token: {e}") from e

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise TokenEncryptionError(f"Decrypted payload is not valid JSON: {e}") from e

    if not isinstance(parsed, dict):
        raise TokenEncryptionError("Decrypted payload must be a JSON object")

    return parsed
