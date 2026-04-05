from __future__ import annotations
from flask import Blueprint, current_app, jsonify, request
from ...db import connection_manager as conn_mgr
from ...services.mongo_startup_config import run_mongo_startup_probe
from ...services.workflow_materialisation_diagnostics_service import (
    build_workflow_materialisation_diagnostics,
)

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


def _collect_required_concept_ids_from_query() -> list[str]:
    values: list[str] = []
    for key in ("required_concept_id", "required_concept_ids"):
        for raw_value in request.args.getlist(key):
            if not isinstance(raw_value, str):
                continue
            values.extend(part.strip() for part in raw_value.split(","))
    return [value for value in dict.fromkeys(values) if value]


@admin_bp.route("/workflow_materialisation_diagnostics", methods=["GET"])
def workflow_materialisation_diagnostics():
    include_present_raw = (
        (request.args.get("include_present_concepts") or "").strip().lower()
    )
    include_present_concepts = include_present_raw not in {
        "0",
        "false",
        "no",
        "off",
    }
    payload = build_workflow_materialisation_diagnostics(
        required_concept_ids=_collect_required_concept_ids_from_query() or None,
        include_present_concepts=include_present_concepts,
        startup_status=current_app.config.get("DURABLE_WORKFLOW_STARTUP_STATUS"),
        workflow_components=current_app.config.get("DURABLE_WORKFLOW_COMPONENTS"),
    )
    return jsonify(payload)
