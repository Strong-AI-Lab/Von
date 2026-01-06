from __future__ import annotations

from flask import Blueprint, jsonify, request

from src.backend.services.client_capabilities_service import (
    get_client_capabilities_snapshot,
    sanitise_client_capabilities,
    set_client_capabilities_snapshot,
)


client_capabilities_bp = Blueprint(
    "client_capabilities_bp", __name__, url_prefix="/api/client_capabilities"
)


@client_capabilities_bp.route("", methods=["GET"])
def get_client_capabilities():
    snapshot = get_client_capabilities_snapshot()
    return jsonify({"success": True, "capabilities": snapshot})


@client_capabilities_bp.route("", methods=["POST"])
def set_client_capabilities():
    raw_payload = request.get_json(silent=True)

    snapshot = sanitise_client_capabilities(
        raw_payload,
        user_agent=request.headers.get("User-Agent"),
    )

    set_client_capabilities_snapshot(snapshot)

    return jsonify({"success": True, "stored": snapshot})
