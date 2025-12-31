from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken


class RoomDeviceTokenError(Exception):
    pass


@dataclass(frozen=True)
class RoomDeviceTokenPayload:
    device_id: str
    organisation_id: Optional[str]
    issued_at_epoch: int
    expires_at_epoch: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "organisation_id": self.organisation_id,
            "iat": self.issued_at_epoch,
            "exp": self.expires_at_epoch,
        }


def _get_fernet() -> Fernet:
    key = os.environ.get("VON_ROOM_DEVICE_TOKEN_KEY")
    if not isinstance(key, str) or not key.strip():
        raise RoomDeviceTokenError(
            "VON_ROOM_DEVICE_TOKEN_KEY is not set (required for room device tokens)"
        )
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:
        raise RoomDeviceTokenError(
            "Invalid VON_ROOM_DEVICE_TOKEN_KEY format; expected a Fernet key"
        ) from exc


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def issue_room_device_token(
    *,
    device_id: str,
    organisation_id: Optional[str],
    ttl_seconds: int = 60 * 60,
) -> str:
    if not isinstance(device_id, str) or not device_id.strip():
        raise RoomDeviceTokenError("device_id is required")

    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise RoomDeviceTokenError("ttl_seconds must be a positive integer")

    issued_at = _now_epoch()
    expires_at = issued_at + ttl_seconds

    payload = RoomDeviceTokenPayload(
        device_id=device_id.strip(),
        organisation_id=organisation_id.strip() if organisation_id else None,
        issued_at_epoch=issued_at,
        expires_at_epoch=expires_at,
    )

    token_bytes = json.dumps(payload.to_dict(), separators=(",", ":")).encode("utf-8")
    return _get_fernet().encrypt(token_bytes).decode("utf-8")


def verify_room_device_token(token: str) -> RoomDeviceTokenPayload:
    if not isinstance(token, str) or not token.strip():
        raise RoomDeviceTokenError("token is required")

    try:
        raw = _get_fernet().decrypt(token.encode("utf-8"))
    except InvalidToken as exc:
        raise RoomDeviceTokenError("invalid_token") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RoomDeviceTokenError("invalid_token_payload") from exc

    device_id = data.get("device_id")
    organisation_id = data.get("organisation_id")
    iat = data.get("iat")
    exp = data.get("exp")

    if not isinstance(device_id, str) or not device_id.strip():
        raise RoomDeviceTokenError("invalid_token_payload")
    if organisation_id is not None and not isinstance(organisation_id, str):
        raise RoomDeviceTokenError("invalid_token_payload")
    if not isinstance(iat, int) or not isinstance(exp, int):
        raise RoomDeviceTokenError("invalid_token_payload")

    now = _now_epoch()
    if exp <= now:
        raise RoomDeviceTokenError("token_expired")

    return RoomDeviceTokenPayload(
        device_id=device_id,
        organisation_id=organisation_id,
        issued_at_epoch=iat,
        expires_at_epoch=exp,
    )
