"""Small signed opaque cursors for actor-scoped keyset pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from .conversation_scope_binding_service import _binding_secret_bytes

CURSOR_SCHEMA_VERSION = "opaque_cursor.v1"
_MAX_CURSOR_CHARS = 8192


class OpaqueCursorError(ValueError):
    """Raised when a continuation cursor is invalid for the requested page."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))
    except Exception as exc:
        raise OpaqueCursorError("cursor encoding is invalid") from exc


def encode_opaque_cursor(*, purpose: str, payload: Mapping[str, Any]) -> str:
    """Return a tamper-evident token without exposing cursor fields to callers."""

    if not isinstance(purpose, str) or not purpose.strip():
        raise OpaqueCursorError("cursor purpose is required")
    envelope = {
        "schema_version": CURSOR_SCHEMA_VERSION,
        "purpose": purpose.strip(),
        "payload": dict(payload),
    }
    encoded_payload = _b64encode(_canonical_json(envelope))
    signature = hmac.new(
        _binding_secret_bytes(), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded_payload}.{_b64encode(signature)}"


def decode_opaque_cursor(*, cursor: str, purpose: str) -> dict[str, Any]:
    """Verify a cursor and return its payload for one exact pagination purpose."""

    if not isinstance(cursor, str) or not cursor.strip():
        raise OpaqueCursorError("cursor is required")
    token = cursor.strip()
    if len(token) > _MAX_CURSOR_CHARS:
        raise OpaqueCursorError("cursor is too large")
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
    except ValueError as exc:
        raise OpaqueCursorError("cursor format is invalid") from exc
    expected = hmac.new(
        _binding_secret_bytes(), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    supplied = _b64decode(encoded_signature)
    if not hmac.compare_digest(expected, supplied):
        raise OpaqueCursorError("cursor signature is invalid")
    try:
        envelope = json.loads(_b64decode(encoded_payload).decode("utf-8"))
    except Exception as exc:
        raise OpaqueCursorError("cursor payload is invalid") from exc
    if not isinstance(envelope, Mapping):
        raise OpaqueCursorError("cursor payload must be an object")
    if envelope.get("schema_version") != CURSOR_SCHEMA_VERSION:
        raise OpaqueCursorError("cursor schema_version is invalid")
    if envelope.get("purpose") != purpose:
        raise OpaqueCursorError("cursor purpose does not match this request")
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        raise OpaqueCursorError("cursor payload is missing")
    return dict(payload)
