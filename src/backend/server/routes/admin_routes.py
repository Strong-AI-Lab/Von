from __future__ import annotations
from flask import Blueprint, request, jsonify
from ...db import connection_manager as conn_mgr

admin_bp = Blueprint('admin_routes', __name__, url_prefix='/admin')

@admin_bp.route('/db/health', methods=['GET'])
def db_health():  # pragma: no cover - simple wrapper
    return jsonify(conn_mgr.health_summary())

@admin_bp.route('/db/reconnect', methods=['POST'])
def db_reconnect():
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get('force'))
    max_attempts = int(payload.get('max_attempts', 5))
    base_ms = int(payload.get('base_ms', 1000))
    max_ms = int(payload.get('max_ms', 30000))
    result = conn_mgr.attempt_reconnect(force=force, max_attempts=max_attempts, base_ms=base_ms, max_ms=max_ms)
    return jsonify(result)
