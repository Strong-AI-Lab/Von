from __future__ import annotations

import os
from typing import Optional

from flask import Blueprint, jsonify, request

from ...services.room_device_service import (
    RoomDeviceAuthError,
    get_room_device_from_token,
    login_room_device,
    provision_room_device,
)


room_device_bp = Blueprint("room_device_routes", __name__, url_prefix="")


def _require_admin_token() -> Optional[tuple]:
    expected = os.environ.get("VON_ADMIN_TOKEN")
    if not isinstance(expected, str) or not expected.strip():
        return jsonify({"error": "admin_token_disabled"}), 403

    provided = request.headers.get("X-Admin-Token")
    if provided != expected:
        return jsonify({"error": "unauthorised"}), 401

    return None


def _get_bearer_token() -> Optional[str]:
    auth = request.headers.get("Authorization")
    if not isinstance(auth, str):
        return None
    parts = auth.split(" ", 1)
    if len(parts) != 2:
        return None
    scheme, token = parts[0].strip(), parts[1].strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


@room_device_bp.route("/admin/room_devices/provision", methods=["POST"])
def admin_provision_room_device():
    """Provision a new room device.

    Requires header X-Admin-Token matching env VON_ADMIN_TOKEN.

    Body: {device_name?: str, organisation_concept_id?: str}
    Returns: {device_id, device_secret, organisation_id}

    NOTE: device_secret is returned once and cannot be retrieved later.
    """

    required = _require_admin_token()
    if required is not None:
        return required

    payload = request.get_json(silent=True) or {}
    device_name = payload.get("device_name")
    organisation_concept_id = payload.get("organisation_concept_id")

    try:
        device = provision_room_device(
            device_name=device_name, organisation_concept_id=organisation_concept_id
        )
    except RoomDeviceAuthError as exc:
        if str(exc) == "db_unavailable":
            return jsonify({"error": "db_unavailable"}), 503
        return jsonify({"error": str(exc)}), 400

    return (
        jsonify(
            {
                "device_id": device.device_id,
                "device_secret": device.device_secret,
                "organisation_id": device.organisation_id,
            }
        ),
        201,
    )


@room_device_bp.route("/api/room_devices/login", methods=["POST"])
def room_device_login():
    """Exchange a provisioned device_id/device_secret for a bearer token."""

    payload = request.get_json(silent=True) or {}
    device_id = payload.get("device_id")
    device_secret = payload.get("device_secret")

    ttl_seconds = int(payload.get("ttl_seconds", 3600))
    if ttl_seconds <= 0 or ttl_seconds > 24 * 3600:
        return jsonify({"error": "invalid_ttl_seconds"}), 400

    try:
        token = login_room_device(
            device_id=str(device_id),
            device_secret=str(device_secret),
            ttl_seconds=ttl_seconds,
        )
    except RoomDeviceAuthError as exc:
        code = str(exc)
        if code == "db_unavailable":
            return jsonify({"error": "db_unavailable"}), 503
        if code in ("invalid_credentials", "device_inactive"):
            return jsonify({"error": code}), 401
        return jsonify({"error": code}), 400

    return jsonify({"access_token": token, "token_type": "bearer"}), 200


@room_device_bp.route("/api/room_devices/whoami", methods=["GET"])
def room_device_whoami():
    token = _get_bearer_token()
    if not token:
        return jsonify({"error": "missing_bearer_token"}), 401

    try:
        payload, _ = get_room_device_from_token(token)
    except RoomDeviceAuthError as exc:
        code = str(exc)
        if code in ("invalid_token", "token_expired", "token_revoked"):
            return jsonify({"error": code}), 401
        if code == "db_unavailable":
            return jsonify({"error": "db_unavailable"}), 503
        return jsonify({"error": code}), 400

    return (
        jsonify(
            {
                "device_id": payload.device_id,
                "organisation_id": payload.organisation_id,
                "expires_at_epoch": payload.expires_at_epoch,
            }
        ),
        200,
    )
