from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..db.mongo_client import get_room_devices_collection
from ..security.room_device_tokens import (
    RoomDeviceTokenError,
    RoomDeviceTokenPayload,
    issue_room_device_token,
    verify_room_device_token,
)


class RoomDeviceAuthError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_slug(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    slug = value.strip()
    if not slug:
        return None
    if slug.startswith("#V#"):
        slug = slug[3:]
    return slug.strip().lower().replace(" ", "_")


def _hash_secret(secret_value: str, *, salt: bytes, iterations: int) -> str:
    import hashlib

    dk = hashlib.pbkdf2_hmac(
        "sha256",
        secret_value.encode("utf-8"),
        salt,
        iterations,
        dklen=32,
    )
    return dk.hex()


@dataclass(frozen=True)
class ProvisionedRoomDevice:
    device_id: str
    device_secret: str
    organisation_id: Optional[str]


def provision_room_device(
    *,
    device_name: Optional[str] = None,
    organisation_concept_id: Optional[str] = None,
) -> ProvisionedRoomDevice:
    coll = get_room_devices_collection()
    if coll is None:
        raise RoomDeviceAuthError("db_unavailable")

    device_id = secrets.token_urlsafe(12)
    device_secret = secrets.token_urlsafe(32)

    salt = secrets.token_bytes(16)
    iterations = int(os.environ.get("VON_ROOM_DEVICE_SECRET_PBKDF2_ITERS", "210000"))
    if iterations < 100_000:
        iterations = 100_000

    secret_hash = _hash_secret(device_secret, salt=salt, iterations=iterations)

    org_slug = _normalise_slug(organisation_concept_id)

    now = _now()
    doc: Dict[str, Any] = {
        "device_id": device_id,
        "device_name": device_name.strip() if isinstance(device_name, str) else None,
        "organisation_id": org_slug,
        "status": "active",
        "secret_salt": salt.hex(),
        "secret_hash": secret_hash,
        "secret_iters": iterations,
        "token_revoked_before": None,
        "created_at": now,
        "updated_at": now,
        "last_login_at": None,
    }

    coll.insert_one(doc)
    return ProvisionedRoomDevice(
        device_id=device_id,
        device_secret=device_secret,
        organisation_id=org_slug,
    )


def authenticate_room_device(*, device_id: str, device_secret: str) -> Dict[str, Any]:
    coll = get_room_devices_collection()
    if coll is None:
        raise RoomDeviceAuthError("db_unavailable")

    if not isinstance(device_id, str) or not device_id.strip():
        raise RoomDeviceAuthError("invalid_credentials")
    if not isinstance(device_secret, str) or not device_secret.strip():
        raise RoomDeviceAuthError("invalid_credentials")

    doc = coll.find_one({"device_id": device_id.strip()})
    if not doc:
        raise RoomDeviceAuthError("invalid_credentials")

    if doc.get("status") != "active":
        raise RoomDeviceAuthError("device_inactive")

    salt_hex = doc.get("secret_salt")
    stored_hash = doc.get("secret_hash")
    iters = doc.get("secret_iters")

    if not isinstance(salt_hex, str) or not isinstance(stored_hash, str):
        raise RoomDeviceAuthError("device_record_invalid")
    if not isinstance(iters, int) or iters < 1:
        raise RoomDeviceAuthError("device_record_invalid")

    try:
        salt = bytes.fromhex(salt_hex)
    except Exception as exc:
        raise RoomDeviceAuthError("device_record_invalid") from exc

    candidate_hash = _hash_secret(device_secret, salt=salt, iterations=iters)
    if not hmac.compare_digest(candidate_hash, stored_hash):
        raise RoomDeviceAuthError("invalid_credentials")

    now = _now()
    coll.update_one(
        {"_id": doc.get("_id")},
        {"$set": {"last_login_at": now, "updated_at": now}},
    )

    return doc


def login_room_device(*, device_id: str, device_secret: str, ttl_seconds: int) -> str:
    doc = authenticate_room_device(device_id=device_id, device_secret=device_secret)
    org_id = doc.get("organisation_id")
    org_slug = org_id if isinstance(org_id, str) and org_id.strip() else None

    token = issue_room_device_token(
        device_id=device_id.strip(), organisation_id=org_slug, ttl_seconds=ttl_seconds
    )
    return token


def get_room_device_from_token(
    token: str,
) -> tuple[RoomDeviceTokenPayload, Dict[str, Any]]:
    try:
        payload = verify_room_device_token(token)
    except RoomDeviceTokenError as exc:
        raise RoomDeviceAuthError(str(exc)) from exc

    coll = get_room_devices_collection()
    if coll is None:
        raise RoomDeviceAuthError("db_unavailable")

    doc = coll.find_one({"device_id": payload.device_id})
    if not doc:
        raise RoomDeviceAuthError("device_not_found")

    if doc.get("status") != "active":
        raise RoomDeviceAuthError("device_inactive")

    revoked_before = doc.get("token_revoked_before")
    if revoked_before is not None:
        try:
            revoked_epoch = int(revoked_before)
            if payload.issued_at_epoch <= revoked_epoch:
                raise RoomDeviceAuthError("token_revoked")
        except ValueError:
            # If malformed, fail closed.
            raise RoomDeviceAuthError("device_record_invalid")

    if (
        payload.organisation_id
        and doc.get("organisation_id") != payload.organisation_id
    ):
        raise RoomDeviceAuthError("token_scope_mismatch")

    return payload, doc
