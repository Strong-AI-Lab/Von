from __future__ import annotations
from flask import Blueprint, request, jsonify
from ...db import connection_manager as conn_mgr
from ...services.mongo_startup_config import run_mongo_startup_probe

admin_bp = Blueprint("admin_routes", __name__, url_prefix="/admin")


@admin_bp.route("/db/health", methods=["GET"])
def db_health():  # pragma: no cover - simple wrapper
    payload = conn_mgr.health_summary()
    probe_mode = (request.args.get("probe") or "").strip().lower()
    if probe_mode in {"rw", "readwrite"}:
        try:
            payload["probe"] = run_mongo_startup_probe()
        except Exception as exc:
            payload["probe"] = {"ok": False, "error": str(exc)}
            return jsonify(payload), 503
    return jsonify(payload)


@admin_bp.route("/db/reconnect", methods=["POST"])
def db_reconnect():
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force"))
    max_attempts = int(payload.get("max_attempts", 5))
    base_ms = int(payload.get("base_ms", 1000))
    max_ms = int(payload.get("max_ms", 30000))
    result = conn_mgr.attempt_reconnect(
        force=force, max_attempts=max_attempts, base_ms=base_ms, max_ms=max_ms
    )
    return jsonify(result)
