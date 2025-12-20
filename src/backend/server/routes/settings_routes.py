import os
import json
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app, send_file, session
from werkzeug.utils import secure_filename
import tempfile
import logging
from ...vontology.utils_vontology import (
    get_concept_notes,
    EXAMPLE_USER_CONCEPT_ID,
)  # relative import
from typing import Optional, Tuple, Dict, Any
from werkzeug.datastructures import FileStorage
from ...services.settings_service import (
    get_active_llm_setting,
    set_active_llm_setting,
    get_openai_env_var,
    set_openai_env_var,
    get_fetch_counts_on_load,
    set_fetch_counts_on_load,
    get_ollama_hosts_list,
    set_ollama_hosts_list,
    get_active_ollama_host,
    set_active_ollama_host,
    resolve_llm_setting,
    set_user_llm_setting,
    set_org_llm_setting,
    get_disable_remote_ollama_scan,
    set_disable_remote_ollama_scan,
)
from ...services.concept_service import list_concepts, get_concept_by_id
from ...languagemodels.llm_interface import OpenAIClient
from ...db.repositories.concepts_repository import ConceptsRepository
from bson import ObjectId
from pymongo.errors import BulkWriteError, PyMongoError
from ...db.mongo_client import (
    MONGO_URI,
    DATABASE_NAME,
    get_db,
    get_effective_mongo_uri,
    is_using_fallback_uri,
)
import re

# REFACTORING_NOTE: This blueprint is part of the backend model selection refactoring.
# It provides API endpoints for managing global application settings.


def _validate_unified_concept_schema(concept, index):
    """Validate that a concept follows the unified schema structure."""
    errors = []

    # Required field: concept_id
    if "concept_id" not in concept or not concept["concept_id"]:
        errors.append(f"Item {index}: Missing required field 'concept_id'")
        return errors  # Critical error, stop validation

    # Validate names array structure
    if "names" in concept:
        if not isinstance(concept["names"], list):
            errors.append(f"Item {index}: 'names' must be an array")
        else:
            for i, name in enumerate(concept["names"]):
                if not isinstance(name, dict):
                    errors.append(f"Item {index}, names[{i}]: Must be an object")
                elif (
                    "text" not in name and "name" not in name
                ):  # Allow both 'text' and 'name'
                    errors.append(
                        f"Item {index}, names[{i}]: Missing required 'text' or 'name' field"
                    )

    # Validate metadata structure
    if "metadata" in concept and concept["metadata"] is not None:
        if not isinstance(concept["metadata"], dict):
            errors.append(f"Item {index}: 'metadata' must be an object")

    # Validate relationships structure - can be dict or list
    if "relationships" in concept:
        if not isinstance(concept["relationships"], (list, dict)):
            errors.append(f"Item {index}: 'relationships' must be an array or object")
        elif isinstance(concept["relationships"], list):
            for i, rel in enumerate(concept["relationships"]):
                if not isinstance(rel, dict):
                    errors.append(
                        f"Item {index}, relationships[{i}]: Must be an object"
                    )
                elif "predicate" not in rel or "target" not in rel:
                    errors.append(
                        f"Item {index}, relationships[{i}]: Missing required 'predicate' or 'target' field"
                    )

    # Validate concept_data structure
    if "concept_data" in concept:
        if not isinstance(concept["concept_data"], dict):
            errors.append(f"Item {index}: 'concept_data' must be an object")
        elif "preserved_fields" in concept["concept_data"]:
            if not isinstance(concept["concept_data"]["preserved_fields"], dict):
                errors.append(
                    f"Item {index}: 'concept_data.preserved_fields' must be an object"
                )

    # Validate timestamps structure
    if "timestamps" in concept:
        if not isinstance(concept["timestamps"], dict):
            errors.append(f"Item {index}: 'timestamps' must be an object")

    # Validate individual timestamp fields
    timestamp_fields = [
        "created_timestamp",
        "last_modified",
        "last_accessed",
        "created_at",
        "updated_at",
        "accessed_at",
    ]
    for field in timestamp_fields:
        if field in concept and concept[field] is not None:
            if not isinstance(concept[field], (str, datetime)):
                errors.append(f"Item {index}: '{field}' must be a string or datetime")

    return errors


settings_bp = Blueprint(
    "settings", __name__
)  # REMOVED url_prefix, as it's set during registration

# --- Import & Orphan Metrics (JVNAUTOSCI-584) ---
# These dicts are appended (never structural breaking changes) and surfaced via /diag.
# They remain empty until first relevant operation to avoid changing baseline /diag output.
_IMPORT_METRICS: dict = {}
_ORPHAN_SCAN_METRICS: dict = {}


def _utc_now_iso() -> str:
    """Return timezone-aware UTC ISO string (trailing Z) for metrics."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _compute_orphan_metrics(limit_sample: int = 25) -> dict:
    """Scan concepts to compute orphan statistics.
    An "orphan" is a concept that declares parent relationships but none of the referenced parent concept_ids exist.
    We treat relationships as either legacy dict/list forms handled by utils_vontology processing or a unified
    'relationships' collection; we look for parent identifiers in probable fields: 'parent_ids',
    relationships entries with predicate forms indicating is-a relations (e.g., 'is_a_type_of', 'subconceptOf').
    This keeps logic shallow (no deep traversal) for O(n) scan.
    """
    db = get_db()
    collection = db.get_collection("vontology_nodes") if db is not None else None
    if collection is None:
        return {"error": "no_collection", "ts": _utc_now_iso()}

    start = datetime.now(timezone.utc)
    # Fetch necessary fields only
    cursor = collection.find({}, {"concept_id": 1, "relationships": 1, "parent_ids": 1})
    all_concepts = list(cursor)
    existing_ids = {
        doc.get("concept_id") for doc in all_concepts if doc.get("concept_id")
    }

    orphan_records = []
    total_with_parents = 0

    def extract_parent_ids(doc):
        parent_ids = set()
        # Unified processed field
        raw_parent_ids = doc.get("parent_ids")
        if isinstance(raw_parent_ids, list):
            for pid in raw_parent_ids:
                if isinstance(pid, str) and pid:
                    parent_ids.add(pid)
        # Raw relationships (list form)
        rels = doc.get("relationships")
        if isinstance(rels, list):
            for rel in rels:
                if not isinstance(rel, dict):
                    continue
                pred = rel.get("predicate") or rel.get("type")
                tgt = rel.get("target") or rel.get("concept_id") or rel.get("target_id")
                if pred and tgt and isinstance(tgt, str):
                    # Heuristic: treat these predicates as parent links
                    if pred in ("is_a_type_of", "subconceptOf", "is_a", "isA"):
                        parent_ids.add(tgt)
        # Raw relationships (dict form) - treat keys mapping to list/str potential parent refs for known keys
        if isinstance(rels, dict):
            for key, val in rels.items():
                if key in ("is_a_type_of", "subconceptOf"):
                    if isinstance(val, list):
                        for v in val:
                            if isinstance(v, str):
                                parent_ids.add(v)
                    elif isinstance(val, str):
                        parent_ids.add(val)
        return parent_ids

    for doc in all_concepts:
        cid = doc.get("concept_id")
        if not cid:
            continue
        parents = extract_parent_ids(doc)
        if not parents:
            continue
        total_with_parents += 1
        missing = [p for p in parents if p not in existing_ids and p != cid]
        if missing:
            orphan_records.append(
                {
                    "concept_id": cid,
                    "missing_parents": sorted(missing)[:10],
                    "parent_ref_count": len(parents),
                }
            )

    duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    orphan_count = len(orphan_records)
    sample = orphan_records[:limit_sample]
    return {
        "ts": _utc_now_iso(),
        "duration_ms": duration_ms,
        "scanned_count": len(all_concepts),
        "with_parent_ref_count": total_with_parents,
        "orphan_concept_count": orphan_count,
        "sample": sample,
        "sample_size": len(sample),
    }


@settings_bp.route("/ontology/orphan_scan", methods=["POST"])
def scan_orphan_concepts():
    """Trigger an orphan parent reference scan and populate _ORPHAN_SCAN_METRICS.
    Returns metrics dict. Safe, read-only (apart from metrics mutation).
    """
    try:
        metrics = _compute_orphan_metrics()
        # Update global (append-only evolution contract)
        _ORPHAN_SCAN_METRICS.update(metrics)
        # Increment run counter
        _ORPHAN_SCAN_METRICS["run_count"] = _ORPHAN_SCAN_METRICS.get("run_count", 0) + 1
        return jsonify({"orphan_metrics": dict(_ORPHAN_SCAN_METRICS)}), 200
    except Exception as e:
        current_app.logger.error(f"[orphan_scan] error: {e}", exc_info=True)
        return jsonify({"error": "scan_failed", "detail": str(e)}), 500


def _sanitize_mongo_uri_for_display(uri: str) -> str:
    """Return a sanitized Mongo connection location without credentials or query params.
    Examples:
      mongodb://user:pass@localhost:27017/von_db?authSource=admin -> mongodb://localhost:27017
      mongodb+srv://user:pass@cluster0.abcd.mongodb.net/von_db -> mongodb+srv://cluster0.abcd.mongodb.net
    """
    try:
        # Remove credentials segment between scheme and '@'
        i = uri.find("://")
        if i != -1:
            at = uri.find("@", i + 3)
            if at != -1:
                uri = uri[: i + 3] + uri[at + 1 :]
        # Drop query string
        q = uri.find("?")
        if q != -1:
            uri = uri[:q]
        # Keep only scheme + host(s)[:port], drop trailing path (db name) for location clarity
        j = uri.find("://")
        if j != -1:
            slash = uri.find("/", j + 3)
            if slash != -1:
                uri = uri[:slash]
        return uri
    except Exception:
        # Fallback: best-effort removal of credentials
        if "@" in uri:
            uri = uri.split("@", 1)[-1]
        if "?" in uri:
            uri = uri.split("?", 1)[0]
        if "://" in uri:
            scheme = uri.split("://", 1)[0]
            rest = uri.split("://", 1)[1]
            host = rest.split("/", 1)[0]
            return f"{scheme}://{host}"
        return uri


@settings_bp.route("/db/info", methods=["GET"])
def get_db_location_info():
    """API endpoint to report the MongoDB location (without credentials)."""
    try:
        # Use effective URI (may be fallback) for display classification
        effective_uri = get_effective_mongo_uri()
        sanitized_uri = _sanitize_mongo_uri_for_display(effective_uri)
        # Attempt a quick ping
        ping_ok = False
        error_message = None
        try:
            db = get_db()
            if db is not None:
                db.command("ping")
                ping_ok = True
            else:
                error_message = "No DB connection"
        except Exception as e:
            error_message = str(e)
            ping_ok = False

        # Classification heuristics
        is_local = bool(
            re.search(
                r"mongodb://(localhost|127\.0\.0\.1|0\.0\.0\.0)",
                sanitized_uri,
                re.IGNORECASE,
            )
        )
        is_srv = sanitized_uri.startswith("mongodb+srv://")
        classification = "local" if is_local else ("atlas" if is_srv else "remote")
        using_fallback = is_using_fallback_uri()
        public_ip = None
        if using_fallback:
            # Try to discover outward-facing IP (best-effort, short timeout). Avoid blocking failures.
            try:
                import urllib.request, socket

                socket.setdefaulttimeout(1.5)
                with urllib.request.urlopen(
                    "https://api.ipify.org?format=text", timeout=1.5
                ) as resp:
                    txt = resp.read().decode("utf-8").strip()
                    if txt and len(txt) <= 64:
                        public_ip = txt
            except Exception:  # pragma: no cover - non-critical
                public_ip = None
        return (
            jsonify(
                {
                    "sanitized_uri": sanitized_uri,
                    "database_name": DATABASE_NAME,
                    "ping_ok": ping_ok,
                    "error": error_message,
                    "classification": classification,
                    "using_fallback": using_fallback,
                    "primary_uri_sanitized": (
                        _sanitize_mongo_uri_for_display(MONGO_URI)
                        if using_fallback
                        else None
                    ),
                    "server_public_ip": public_ip,
                }
            ),
            200,
        )
    except Exception as e:
        current_app.logger.error(f"Error retrieving DB info: {e}", exc_info=True)
        return jsonify({"error": "Failed to retrieve DB info."}), 500


@settings_bp.route("/llm/info", methods=["GET"])
def get_llm_info():
    """API endpoint to report the LLM status."""
    try:
        active_llm = get_active_llm_setting() or {}
        provider = active_llm.get("provider", "openai")
        model = active_llm.get("model", "gpt-4o")

        status = "unknown"
        error_message = None
        ping_ok = False
        details = {}

        if provider == "openai":
            api_key = get_openai_env_var()
            if not api_key:
                status = "missing_key"
                error_message = "OpenAI API key not set"
            else:
                try:
                    # Verify connectivity
                    # We use a lightweight check if possible, or just assume configured if key is present
                    # to avoid latency on every page load if this is called frequently.
                    # However, the user wants "available", which implies a check.
                    # The DB check does a ping.
                    # Let's do a list_models check but maybe catch errors gracefully.
                    client = OpenAIClient(api_key_env_var=api_key)
                    # client.list_models() # This might be slow.
                    # For now, let's assume if key is present it is "configured"
                    # but maybe we can do a real check if the user specifically asked for "available".
                    # "blue cartouche when the model set is available"
                    # I'll do the check.
                    client.list_models()
                    status = "ready"
                    ping_ok = True
                    details["masked_key"] = (
                        f"{api_key[:5]}...{api_key[-4:]}" if len(api_key) > 9 else "..."
                    )
                except Exception as e:
                    status = "error"
                    error_message = str(e)

        elif provider == "ollama":
            host = get_active_ollama_host()
            details["host"] = host
            try:
                from ...languagemodels.llm_interface import OllamaClient

                client = OllamaClient(host=host)
                client.list_models()
                status = "ready"
                ping_ok = True
            except Exception as e:
                status = "error"
                error_message = str(e)

        return (
            jsonify(
                {
                    "provider": provider,
                    "model": model,
                    "status": status,
                    "ping_ok": ping_ok,
                    "error": error_message,
                    "details": details,
                }
            ),
            200,
        )
    except Exception as e:
        current_app.logger.error(f"Error retrieving LLM info: {e}", exc_info=True)
        return jsonify({"error": "Failed to retrieve LLM info."}), 500


def _get_entity_with_fallback(
    entity_id: Optional[str], concept_id: Optional[str] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Try to get entity by ID first, then fall back to concept_id if provided.
    Returns (entity_dict, actual_id_used) or (None, None) if not found.
    """
    if not entity_id:
        return None, None

    # Try to get by ObjectId first
    try:
        concept = get_concept_by_id(entity_id)
        if concept:
            return concept, entity_id
    except Exception as e:
        current_app.logger.warning(f"Failed to get concept by ID {entity_id}: {e}")

    # If ObjectId lookup failed and we have a concept_id, try that
    if concept_id:
        try:
            entity = ConceptsRepository.find_one({"concept_id": concept_id})
            if entity:
                # Convert ObjectId to string for consistency
                entity["id"] = str(entity.pop("_id"))
                # Ensure a readable name is present
                if not entity.get("name"):
                    metadata = entity.get("metadata", {})
                    if isinstance(metadata, dict) and metadata.get("title"):
                        entity["name"] = metadata.get("title")
                    elif isinstance(entity.get("names"), list) and entity["names"]:
                        first = entity["names"][0]
                        if isinstance(first, dict):
                            entity["name"] = (
                                first.get("text")
                                or first.get("name")
                                or entity.get("name")
                            )
                actual_id = entity["id"]
                current_app.logger.info(
                    f"Found entity by concept_id {concept_id}, actual ID: {actual_id}"
                )
                return entity, actual_id
        except Exception as e:
            current_app.logger.warning(
                f"Failed to get entity by concept_id {concept_id}: {e}"
            )

    return None, None


def get_all_settings_data():
    """Get all settings (user/org/language now excluded - browser-local)."""
    try:
        return {
            "active_llm": get_active_llm_setting(),
            "openai_api_key_env_var": get_openai_env_var(),
            "fetch_counts_on_load": get_fetch_counts_on_load(),
            "disable_remote_ollama_scan": get_disable_remote_ollama_scan(),
        }
    except Exception as e:
        current_app.logger.error(f"Error retrieving settings data: {e}", exc_info=True)
        return {}


@settings_bp.route("/", methods=["GET"])
def get_all_settings():
    """API endpoint to retrieve all relevant settings."""
    try:
        settings = get_all_settings_data()
        # Optional resolution using query args
        user_concept_id = request.args.get("user_concept_id")
        org_concept_id = request.args.get(
            "organisation_concept_id"
        ) or request.args.get("organization_concept_id")
        resolved = resolve_llm_setting(
            user_concept_id=user_concept_id, org_concept_id=org_concept_id
        )
        settings["resolved_llm"] = resolved
        return jsonify(settings), 200
    except Exception as e:
        current_app.logger.error(f"Error retrieving all settings: {e}", exc_info=True)
        return (
            jsonify(
                {"error": "An unexpected error occurred while retrieving settings."}
            ),
            500,
        )


@settings_bp.route("/", methods=["POST"])
def save_all_settings():
    """API endpoint to save multiple settings at once."""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "Invalid JSON payload"}), 400

    try:
        if "active_llm" in data and data["active_llm"]:
            llm_data = data["active_llm"]
            provider = llm_data.get("provider")
            model = llm_data.get("model")
            scope = llm_data.get("scope")  # optional: user/organisation override
            concept_id = llm_data.get("concept_id")
            if scope in ("user", "organisation") and concept_id and provider and model:
                ok = (
                    set_user_llm_setting(concept_id, provider, model)
                    if scope == "user"
                    else set_org_llm_setting(concept_id, provider, model)
                )
                if ok:
                    current_app.logger.info(
                        f"Scoped LLM override set scope={scope} concept={concept_id} provider={provider} model={model}"
                    )
            elif provider and model:
                set_active_llm_setting(provider, model)
                current_app.logger.info(
                    f"Active LLM set to: Provider={provider}, Model={model}"
                )

        if "openai_api_key_env_var" in data and data["openai_api_key_env_var"]:
            env_var = data["openai_api_key_env_var"]
            set_openai_env_var(env_var)
            current_app.logger.info(
                f"OpenAI API key environment variable set to: {env_var}"
            )

        if "fetch_counts_on_load" in data:
            try:
                enabled = bool(data.get("fetch_counts_on_load"))
            except Exception:
                enabled = True
            set_fetch_counts_on_load(enabled)
            current_app.logger.info(f"fetch_counts_on_load set to: {enabled}")

        if "disable_remote_ollama_scan" in data:
            try:
                disabled = bool(data.get("disable_remote_ollama_scan"))
            except Exception:
                disabled = False
            set_disable_remote_ollama_scan(disabled)
            current_app.logger.info(f"disable_remote_ollama_scan set to: {disabled}")

        # user/org/language fields intentionally ignored (browser-local)

        return (
            jsonify({"status": "success", "message": "Settings updated successfully."}),
            200,
        )
    except Exception as e:
        current_app.logger.error(f"Error saving settings: {e}", exc_info=True)
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "An unexpected error occurred while saving settings.",
                }
            ),
            500,
        )


@settings_bp.route("/llm/override", methods=["POST"])
def set_llm_override():
    """Set a scoped LLM override (user or organisation).
    Body: { provider, model, scope: 'user'|'organisation', concept_id }
    Returns resolved setting for convenience.
    """
    try:
        data = request.get_json(silent=True) or {}
        provider = data.get("provider")
        model = data.get("model")
        scope = data.get("scope")
        concept_id = data.get("concept_id")
        if not all([provider, model, scope, concept_id]):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "provider, model, scope, concept_id required",
                    }
                ),
                400,
            )
        if scope not in ("user", "organisation"):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "scope must be 'user' or 'organisation'",
                    }
                ),
                400,
            )
        assert (
            isinstance(concept_id, str)
            and isinstance(provider, str)
            and isinstance(model, str)
        )
        if scope == "user":
            ok = set_user_llm_setting(concept_id, provider, model)
        else:
            ok = set_org_llm_setting(concept_id, provider, model)
        if not ok:
            return (
                jsonify({"status": "error", "message": "Failed to persist override"}),
                500,
            )
        resolved = resolve_llm_setting(
            user_concept_id=concept_id if scope == "user" else None,
            org_concept_id=concept_id if scope == "organisation" else None,
        )
        return jsonify({"status": "ok", "resolved_llm": resolved}), 200
    except Exception as e:
        current_app.logger.error(f"Error setting LLM override: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "Failed to set override"}), 500


@settings_bp.route("/env_var/check", methods=["POST"])
def check_environment_variable():
    """API endpoint to check if an environment variable is set."""
    data = request.get_json()
    if not data:
        current_app.logger.warning(
            "No JSON data received in environment variable check request"
        )
        return jsonify({"error": "Invalid JSON payload"}), 400

    env_var_name = data.get("env_var_name")
    if not env_var_name:
        current_app.logger.warning("Missing 'env_var_name' in request body")
        return jsonify({"error": "Missing 'env_var_name' in request body."}), 400

    current_app.logger.info(f"Checking environment variable: {env_var_name}")
    value = os.getenv(env_var_name)

    response_data: Dict[str, Any] = {"exists": bool(value)}
    if value:
        # Return a masked version of the key for verification
        masked_value = f"{value[:5]}...{value[-4:]}" if len(value) > 9 else value
        response_data["masked_value"] = masked_value
        current_app.logger.info(
            f"Environment variable {env_var_name} exists (masked: {masked_value})"
        )
    else:
        current_app.logger.info(f"Environment variable {env_var_name} not found")

    return jsonify(response_data), 200


@settings_bp.route("/models/ollama", methods=["GET"])
def get_ollama_models():
    """API endpoint to get available Ollama models regardless of current selection."""
    try:
        from ...languagemodels.llm_interface import (
            OllamaClient,
        )  # inline import to avoid circulars

        # Create a direct OllamaClient instance to get models
        ollama_client = OllamaClient()
        models = ollama_client.list_models()

        current_app.logger.info(f"Retrieved {len(models)} Ollama models")
        return jsonify(models), 200

    except Exception as e:
        error_msg = str(e)
        current_app.logger.error(
            f"Error retrieving Ollama models: {error_msg}", exc_info=True
        )
        return jsonify({"error": f"Could not retrieve Ollama models: {error_msg}"}), 500


@settings_bp.route("/models/openai", methods=["GET"])
def get_openai_models():
    """API endpoint to get available OpenAI models regardless of current selection."""
    try:
        # Get the configured OpenAI API key environment variable
        api_key_env_var = get_openai_env_var()

        # Create a direct OpenAIClient instance to get models
        client = OpenAIClient(api_key_env_var=api_key_env_var)
        models = client.list_models()

        if models:
            current_app.logger.info(f"Retrieved {len(models)} OpenAI models")
            return jsonify(models), 200
        else:
            current_app.logger.warning("OpenAI API returned empty models list")
            return (
                jsonify(
                    {
                        "error": "Could not retrieve OpenAI models. Please check your API key and permissions."
                    }
                ),
                400,
            )

    except ValueError as e:
        error_msg = str(e)
        current_app.logger.error(f"ValueError retrieving OpenAI models: {error_msg}")
        return jsonify({"error": error_msg}), 400
    except Exception as e:
        error_msg = str(e)
        current_app.logger.error(
            f"Error retrieving OpenAI models: {error_msg}", exc_info=True
        )

        # Check if it's an OpenAI-specific error that should return 400 vs 500
        if (
            "api" in error_msg.lower()
            or "unauthorized" in error_msg.lower()
            or "invalid" in error_msg.lower()
        ):
            return jsonify({"error": f"OpenAI API error: {error_msg}"}), 400
        else:
            return (
                jsonify(
                    {
                        "error": "An unexpected error occurred while retrieving OpenAI models."
                    }
                ),
                500,
            )


# --- Per-user preference (language & organisation) storage via concept relationships ---

USER_PREF_LANG_PREDICATE = "#V#preferred_language"
USER_PREF_ORG_PREDICATE = "#V#member_of_organisation"


def _normalize_relationships(rel):
    if not isinstance(rel, (dict, list)):
        return {}
    if isinstance(rel, list):  # legacy list-of-dicts form
        out = {}
        for item in rel:
            if isinstance(item, dict):
                pred = item.get("predicate")
                tgt = item.get("target")
                if pred and tgt:
                    out.setdefault(pred, [])
                    if isinstance(tgt, list):
                        out[pred].extend(tgt)
                    else:
                        out[pred].append(tgt)
        return out
    return rel or {}


@settings_bp.route("/user_prefs/<path:user_concept_id>", methods=["GET"])
def get_user_prefs(user_concept_id: str):
    """Return stored preferred language & organisation for a user concept.
    Preferences are stored in the concept's relationships under dedicated predicates.
    """
    try:
        if not user_concept_id:
            return jsonify({"error": "Missing user_concept_id"}), 400
        concept = ConceptsRepository.find_one({"concept_id": user_concept_id})
        if not concept:
            return jsonify({"error": "User concept not found"}), 404
        rel = _normalize_relationships(concept.get("relationships", {}))
        lang_raw = rel.get(USER_PREF_LANG_PREDICATE)
        org_raw = rel.get(USER_PREF_ORG_PREDICATE)

        def first_val(v):
            if isinstance(v, list):
                return v[0] if v else None
            return v if isinstance(v, str) and v else None

        preferred_language = first_val(lang_raw)
        organisation_concept_id = first_val(org_raw)
        return (
            jsonify(
                {
                    "user_concept_id": user_concept_id,
                    "preferred_language": preferred_language,
                    "organisation_concept_id": organisation_concept_id,
                }
            ),
            200,
        )
    except Exception as e:
        current_app.logger.error(
            f"Error getting user prefs for {user_concept_id}: {e}", exc_info=True
        )
        return jsonify({"error": "Failed to retrieve user preferences"}), 500


@settings_bp.route("/user_prefs/<path:user_concept_id>", methods=["POST"])
def set_user_prefs(user_concept_id: str):
    """Upsert preferred language & organisation for a user concept.
    Body: { preferred_language?: str|null, organisation_concept_id?: str|null }
    Stores values in the concept's relationships dict under predicates defined above.
    """
    try:
        data = request.get_json(silent=True) or {}
        preferred_language = data.get("preferred_language")
        organisation_concept_id = data.get("organisation_concept_id")
        concept = ConceptsRepository.find_one({"concept_id": user_concept_id})
        if not concept:
            return jsonify({"error": "User concept not found"}), 404
        rel = _normalize_relationships(concept.get("relationships", {}))

        def _as_list(value) -> list[str]:
            if isinstance(value, list):
                return [v for v in value if isinstance(v, str) and v]
            if isinstance(value, str) and value:
                return [value]
            return []

        # Preferred language update (single value)
        existing_lang = _as_list(rel.get(USER_PREF_LANG_PREDICATE))
        desired_lang = (
            preferred_language.strip() if isinstance(preferred_language, str) else None
        )
        desired_lang = desired_lang or None

        for current_lang in list(existing_lang):
            if desired_lang and current_lang == desired_lang:
                continue
            try:
                ConceptsRepository.mutate_relationship_edge(
                    user_concept_id,
                    USER_PREF_LANG_PREDICATE,
                    current_lang,
                    action="remove",
                    maintain_inverse=False,
                )
            except ValueError:
                current_app.logger.debug(
                    f"preferred_language remove skipped for {user_concept_id}: missing concept",
                    exc_info=False,
                )
            existing_lang.remove(current_lang)

        if desired_lang:
            if desired_lang not in existing_lang:
                try:
                    ConceptsRepository.mutate_relationship_edge(
                        user_concept_id,
                        USER_PREF_LANG_PREDICATE,
                        desired_lang,
                        action="add",
                        maintain_inverse=False,
                    )
                    existing_lang.append(desired_lang)
                except ValueError:
                    current_app.logger.warning(
                        f"Failed to set preferred_language for {user_concept_id}"
                    )
        else:
            rel.pop(USER_PREF_LANG_PREDICATE, None)

        if existing_lang:
            rel[USER_PREF_LANG_PREDICATE] = existing_lang
        else:
            rel.pop(USER_PREF_LANG_PREDICATE, None)

        # Organisation membership update (single value)
        existing_org = _as_list(rel.get(USER_PREF_ORG_PREDICATE))
        desired_org = (
            organisation_concept_id.strip()
            if isinstance(organisation_concept_id, str)
            else None
        )
        desired_org = desired_org or None

        for current_org in list(existing_org):
            if desired_org and current_org == desired_org:
                continue
            try:
                ConceptsRepository.mutate_relationship_edge(
                    user_concept_id,
                    USER_PREF_ORG_PREDICATE,
                    current_org,
                    action="remove",
                    maintain_inverse=False,
                )
            except ValueError:
                current_app.logger.debug(
                    f"organisation remove skipped for {user_concept_id}: missing concept",
                    exc_info=False,
                )
            existing_org.remove(current_org)

        if desired_org:
            if desired_org not in existing_org:
                try:
                    ConceptsRepository.mutate_relationship_edge(
                        user_concept_id,
                        USER_PREF_ORG_PREDICATE,
                        desired_org,
                        action="add",
                        maintain_inverse=False,
                    )
                    existing_org.append(desired_org)
                except ValueError:
                    current_app.logger.warning(
                        f"Failed to set organisation preference for {user_concept_id}"
                    )
        else:
            rel.pop(USER_PREF_ORG_PREDICATE, None)

        if existing_org:
            rel[USER_PREF_ORG_PREDICATE] = existing_org
        else:
            rel.pop(USER_PREF_ORG_PREDICATE, None)

        return (
            jsonify(
                {
                    "status": "success",
                    "user_concept_id": user_concept_id,
                    "preferred_language": existing_lang[0] if existing_lang else None,
                    "organisation_concept_id": (
                        existing_org[0] if existing_org else None
                    ),
                }
            ),
            200,
        )
    except Exception as e:
        current_app.logger.error(
            f"Error setting user prefs for {user_concept_id}: {e}", exc_info=True
        )
        return jsonify({"error": "Failed to set user preferences"}), 500


@settings_bp.route("/openai/verify", methods=["POST"])
def verify_openai_api_key():
    """API endpoint to verify the OpenAI API key and get available models."""
    data = request.get_json()
    api_key_env_var = data.get("api_key_env_var")

    # Use settings if not provided in request
    if api_key_env_var is None:
        api_key_env_var = get_openai_env_var()

    current_app.logger.info(
        f"Attempting to verify OpenAI API key using environment variable: {api_key_env_var}"
    )

    try:
        client = OpenAIClient(api_key_env_var=api_key_env_var)
        models = client.list_models()
        if models:
            current_app.logger.info(
                f"Successfully retrieved {len(models)} OpenAI models"
            )
            return jsonify({"success": True, "models": models}), 200
        else:
            current_app.logger.warning("OpenAI API returned empty models list")
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Could not retrieve OpenAI models. Please check your API key and permissions.",
                    }
                ),
                400,
            )
    except ValueError as e:
        error_msg = str(e)
        current_app.logger.error(f"ValueError during OpenAI verification: {error_msg}")
        return jsonify({"success": False, "error": error_msg}), 400
    except Exception as e:
        error_msg = str(e)
        current_app.logger.error(
            f"Error verifying OpenAI API key: {error_msg}", exc_info=True
        )

        # Check if it's an OpenAI-specific error that should return 400 vs 500
        if (
            "api" in error_msg.lower()
            or "unauthorized" in error_msg.lower()
            or "invalid" in error_msg.lower()
        ):
            return (
                jsonify({"success": False, "error": f"OpenAI API error: {error_msg}"}),
                400,
            )
        else:
            return (
                jsonify({"success": False, "error": "An unexpected error occurred."}),
                500,
            )


@settings_bp.route("/ollama/hosts", methods=["GET"])
def get_ollama_hosts():
    """API endpoint to get all configured Ollama hosts."""
    try:
        hosts_list = get_ollama_hosts_list()
        active_host = get_active_ollama_host()
        # Ensure active_host points to one of the normalized URLs; if not, pick first
        try:
            valid_urls = {h["url"] for h in hosts_list}
            if active_host not in valid_urls:
                active_host = next(iter(valid_urls), None)
        except Exception:
            pass

        current_app.logger.info(f"Retrieved {len(hosts_list)} Ollama hosts")
        return (
            jsonify({"success": True, "hosts": hosts_list, "active_host": active_host}),
            200,
        )
    except Exception as e:
        error_msg = str(e)
        current_app.logger.error(
            f"Error retrieving Ollama hosts: {error_msg}", exc_info=True
        )
        return (
            jsonify({"success": False, "error": "Failed to retrieve Ollama hosts"}),
            500,
        )


@settings_bp.route("/ollama/hosts", methods=["POST"])
def set_ollama_hosts():
    """API endpoint to set Ollama hosts configuration."""
    try:
        data = request.get_json()
        hosts_list = data.get("hosts", [])
        active_host = data.get("active_host")

        # Validate hosts list format
        if not isinstance(hosts_list, list):
            return (
                jsonify(
                    {"success": False, "error": "Hosts must be provided as a list"}
                ),
                400,
            )

        # Save hosts list
        if not set_ollama_hosts_list(hosts_list):
            return (
                jsonify({"success": False, "error": "Failed to save hosts list"}),
                500,
            )

        # Save active host if provided
        if active_host:
            if not set_active_ollama_host(active_host):
                return (
                    jsonify({"success": False, "error": "Failed to save active host"}),
                    500,
                )

        current_app.logger.info(f"Successfully saved {len(hosts_list)} Ollama hosts")
        return (
            jsonify({"success": True, "message": "Ollama hosts saved successfully"}),
            200,
        )
    except Exception as e:
        error_msg = str(e)
        current_app.logger.error(
            f"Error saving Ollama hosts: {error_msg}", exc_info=True
        )
        return jsonify({"success": False, "error": "Failed to save Ollama hosts"}), 500


@settings_bp.route("/ollama/models", methods=["GET"])
def get_ollama_models_from_all_hosts():
    """API endpoint to get all Ollama models from all configured hosts."""
    try:
        from ...languagemodels.llm_interface import OllamaClient  # inline import

        # Get models from all hosts
        all_models = OllamaClient.list_models_from_all_hosts()

        current_app.logger.info(
            f"Retrieved {len(all_models)} Ollama models from all hosts"
        )
        return jsonify({"success": True, "models": all_models}), 200
    except Exception as e:
        error_msg = str(e)
        current_app.logger.error(
            f"Error retrieving Ollama models: {error_msg}", exc_info=True
        )
        return (
            jsonify({"success": False, "error": "Failed to retrieve Ollama models"}),
            500,
        )


@settings_bp.route("/ollama/verify", methods=["POST"])
def verify_ollama_host():
    """API endpoint to verify connectivity to an Ollama host."""
    host_url: Optional[str] = None
    try:
        data = request.get_json()
        host_url = data.get("host_url")

        if not host_url:
            return jsonify({"success": False, "error": "Host URL is required"}), 400

        from ...languagemodels.llm_interface import OllamaClient  # inline import

        # Try to connect to the host and get models
        test_client = OllamaClient(host=host_url)
        models = test_client.list_models()

        if models:
            current_app.logger.info(
                f"Successfully connected to Ollama host {host_url} and retrieved {len(models)} models"
            )
            return (
                jsonify(
                    {
                        "success": True,
                        "message": f"Successfully connected to {host_url}",
                        "models": models,
                    }
                ),
                200,
            )
        else:
            current_app.logger.warning(
                f"Connected to Ollama host {host_url} but no models found"
            )
            return (
                jsonify(
                    {
                        "success": True,
                        "message": f"Connected to {host_url} but no models available",
                        "models": [],
                    }
                ),
                200,
            )
    except Exception as e:
        error_msg = str(e)
        current_app.logger.error(
            f"Error verifying Ollama host {host_url or ''}: {error_msg}", exc_info=True
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"Failed to connect to Ollama host: {error_msg}",
                }
            ),
            400,
        )


@settings_bp.route("/people", methods=["GET"])
def get_available_people():
    """API endpoint to retrieve all available person entities for selection."""
    try:
        # Get all Von user entities by using the specific Von user concept ID
        concepts, total_count = list_concepts(
            concept_id="#V#von_user",  # Use the specific Von user concept ID
            sort_by="name",
            sort_order=1,  # Ascending
            per_page=100,  # Get a reasonable number of users
        )

        # Filter and format for dropdown - include all fields needed for filtering
        people_options = []
        for concept in concepts:
            from ...vontology.utils_vontology import (
                get_concept_display_name_with_names_fallback,
            )

            display_name = get_concept_display_name_with_names_fallback(concept)
            people_options.append(
                {
                    "id": concept.get("_id"),
                    "concept_id": concept.get("concept_id"),
                    "name": display_name or "Unknown",
                    "notes": (get_concept_notes(concept) or "")[
                        :100
                    ],  # Truncate notes for display
                    "concept_type": concept.get("direct_concept_name", "Person"),
                    "system_tags": concept.get(
                        "system_tags", []
                    ),  # Include system_tags for filtering
                }
            )

        return (
            jsonify({"people": people_options, "total_count": len(people_options)}),
            200,
        )

    except Exception as e:
        current_app.logger.error(
            f"Error retrieving people for selection: {e}", exc_info=True
        )
        return (
            jsonify({"error": "An unexpected error occurred while retrieving people."}),
            500,
        )


@settings_bp.route("/organisations", methods=["GET"])
def get_available_organisations():
    """API endpoint to retrieve all available organisation entities for selection."""
    try:
        # Get all Von user organisation entities using the specific concept
        concepts, total_count = list_concepts(
            concept_id="#V#von_user_organisation",  # Use the specific Von user organisation concept ID
            sort_by="name",
            sort_order=1,  # Ascending
            per_page=100,  # Get a reasonable number of organisations
        )

        # Use the concepts directly since we're only querying one specific type
        unique_concepts = concepts

        # Filter and format for dropdown - include all fields needed for filtering
        organisation_options = []
        for concept in unique_concepts:
            from backend.vontology.utils_vontology import (
                get_concept_display_name_with_names_fallback,
            )

            display_name = get_concept_display_name_with_names_fallback(concept)
            organisation_options.append(
                {
                    "id": concept.get("_id"),
                    "concept_id": concept.get("concept_id"),
                    "name": display_name or "Unknown",
                    "notes": (get_concept_notes(concept) or "")[
                        :100
                    ],  # Truncate notes for display
                    "concept_type": concept.get("direct_concept_name", "Organisation"),
                    "system_tags": concept.get(
                        "system_tags", []
                    ),  # Include system_tags for filtering
                }
            )

        # Sort by name for consistent display
        organisation_options.sort(key=lambda x: x["name"])

        return (
            jsonify(
                {
                    "organisations": organisation_options,
                    "total_count": len(organisation_options),
                }
            ),
            200,
        )

    except Exception as e:
        current_app.logger.error(
            f"Error retrieving organisations for selection: {e}", exc_info=True
        )
        return (
            jsonify(
                {
                    "error": "An unexpected error occurred while retrieving organisations."
                }
            ),
            500,
        )


def _repair_concept_id(doc, index):
    """
    Attempt to repair a missing concept_id using available document fields.
    Returns a generated concept_id or None if repair is not possible.
    """
    # Try various fields that might contain usable identifiers
    candidates = []

    # Check for name-based identifiers
    if doc.get("name"):
        candidates.append(f"#V#{doc['name'].lower().replace(' ', '_')}")

    # Check for direct_concept_name
    if doc.get("direct_concept_name"):
        candidates.append(f"#V#{doc['direct_concept_name'].lower().replace(' ', '_')}")

    # Check for existing partial concept_id patterns
    if doc.get("concept_type"):
        name_part = doc.get("name", doc.get("direct_concept_name", f"concept_{index}"))
        candidates.append(f"#V#{name_part.lower().replace(' ', '_')}")

    # Use _id as last resort
    if doc.get("_id"):
        candidates.append(f"#V#id_{doc['_id']}")

    # Use index as absolute last resort
    if not candidates:
        candidates.append(f"#V#repaired_concept_{index}")

    # Return the first candidate (they're ordered by preference)
    return candidates[0] if candidates else None


@settings_bp.route("/export-ontology", methods=["GET"])
def export_ontology():
    """API endpoint to export the entire concepts collection as JSON."""
    try:
        current_app.logger.info("Starting ontology export...")

        # Get the concepts collection
        concepts_collection = ConceptsRepository.collection()
        if concepts_collection is None:
            current_app.logger.error("Could not access concepts collection")
            return jsonify({"error": "Database connection failed"}), 500

        # Export all documents from concepts collection
        concepts_cursor = ConceptsRepository.find({})
        concepts_list = []
        repaired_count = 0
        issues_found = []

        for i, doc in enumerate(concepts_cursor):
            # Convert ObjectId to string for JSON serialization
            if "_id" in doc and isinstance(doc["_id"], ObjectId):
                doc["_id"] = str(doc["_id"])

            # Validate and repair concept_id
            if "concept_id" not in doc or not doc["concept_id"]:
                # Try to repair using available fields
                repaired_id = _repair_concept_id(doc, i)
                if repaired_id:
                    doc["concept_id"] = repaired_id
                    repaired_count += 1
                    issues_found.append(
                        f"Concept {i}: Generated missing concept_id: {repaired_id}"
                    )
                    current_app.logger.warning(
                        f"Repaired missing concept_id for document {i}: {repaired_id}"
                    )
                else:
                    issues_found.append(
                        f"Concept {i}: Could not generate concept_id - may cause import issues"
                    )
                    current_app.logger.error(
                        f"Could not repair concept_id for document {i}"
                    )

            # Handle any other datetime objects that might be in the document
            doc = _serialize_document(doc)
            concepts_list.append(doc)

        current_app.logger.info(f"Exported {len(concepts_list)} concepts")

        # Log repair summary
        if repaired_count > 0:
            current_app.logger.warning(
                f"Repaired {repaired_count} concepts during export"
            )
        if issues_found:
            current_app.logger.info(f"Export issues found: {len(issues_found)}")

        # Create filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"von_ontology_export_{timestamp}.json"

        # Create temporary file for download
        temp_file = tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".json", encoding="utf-8"
        )
        try:
            json.dump(
                concepts_list, temp_file, indent=2, ensure_ascii=False, default=str
            )
            temp_file.close()

            current_app.logger.info(f"Created export file: {filename}")

            return send_file(
                temp_file.name,
                as_attachment=True,
                download_name=filename,
                mimetype="application/json",
            )
        except Exception as e:
            # Clean up temp file on error
            if os.path.exists(temp_file.name):
                os.unlink(temp_file.name)
            raise e

    except Exception as e:
        current_app.logger.error(f"Error exporting ontology: {e}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred during export."}), 500


@settings_bp.route("/repair-ontology", methods=["POST"])
def repair_ontology():
    """API endpoint to repair missing concept_ids in the database."""
    try:
        current_app.logger.info("Starting ontology repair...")

        # Get the concepts collection
        concepts_collection = ConceptsRepository.collection()
        if concepts_collection is None:
            current_app.logger.error("Could not access concepts collection")
            return jsonify({"error": "Database connection failed"}), 500

        # Find documents with missing concept_id
        missing_id_docs = list(
            concepts_collection.find(
                {
                    "$or": [
                        {"concept_id": {"$exists": False}},
                        {"concept_id": None},
                        {"concept_id": ""},
                    ]
                }
            )
        )

        if not missing_id_docs:
            return (
                jsonify(
                    {
                        "success": True,
                        "message": "No repairs needed - all concepts have valid concept_ids",
                        "repaired_count": 0,
                    }
                ),
                200,
            )

        current_app.logger.info(
            f"Found {len(missing_id_docs)} concepts missing concept_id"
        )

        # Create backup before repair
        backup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_collection_name = f"concepts_backup_repair_{backup_timestamp}"

        try:
            current_concepts = list(concepts_collection.find({}))
            if current_concepts:
                backup_collection = concepts_collection.database[backup_collection_name]
                backup_collection.insert_many(current_concepts)
                current_app.logger.info(
                    f"Created backup collection: {backup_collection_name}"
                )
        except Exception as e:
            current_app.logger.error(f"Failed to create backup: {e}")
            return jsonify({"error": "Failed to create backup before repair"}), 500

        # Repair missing concept_ids
        repaired_count = 0
        repair_details = []

        for i, doc in enumerate(missing_id_docs):
            repaired_id = _repair_concept_id(doc, i)
            if repaired_id:
                # Update the document in the database
                result = concepts_collection.update_one(
                    {"_id": doc["_id"]}, {"$set": {"concept_id": repaired_id}}
                )
                if result.modified_count > 0:
                    repaired_count += 1
                    repair_details.append(
                        {
                            "document_id": str(doc["_id"]),
                            "generated_concept_id": repaired_id,
                            "based_on": doc.get("name")
                            or doc.get("direct_concept_name")
                            or "document_id",
                        }
                    )
                    current_app.logger.info(
                        f"Repaired concept_id for {doc['_id']}: {repaired_id}"
                    )

        return (
            jsonify(
                {
                    "success": True,
                    "message": f"Successfully repaired {repaired_count} concepts",
                    "repaired_count": repaired_count,
                    "backup_collection": backup_collection_name,
                    "repair_details": repair_details[:10],  # Show first 10 repairs
                }
            ),
            200,
        )

    except Exception as e:
        current_app.logger.error(f"Error repairing ontology: {e}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred during repair."}), 500


@settings_bp.route("/import-ontology", methods=["POST"])
def import_ontology():
    """API endpoint to import ontology JSON and replace the concepts collection."""
    try:
        current_app.logger.info("Starting ontology import...")

        # --- Gating & mode detection ---
        # Environment variable must be explicitly set to allow any import at all.
        gate_env = os.getenv("VON_ALLOW_IMPORT") or os.getenv(
            "VON_ENABLE_IMPORT"
        )  # allow either name
        admin_header = request.headers.get("X-Von-Admin-Token") or request.headers.get(
            "X-Admin-Token"
        )
        required_token = os.getenv("VON_ADMIN_TOKEN")

        if not gate_env:
            return (
                jsonify({"error": "Ontology import disabled (set VON_ALLOW_IMPORT=1)"}),
                403,
            )
        if required_token:
            if not admin_header or admin_header != required_token:
                return jsonify({"error": "Missing or invalid admin token header"}), 403

        # Mode selection
        req_mode = request.args.get("mode")  # None | 'incremental' | 'replace'
        replace_all_flag = request.args.get("replace_all") in ("1", "true", "True")
        # Dry run flag: query param dry_run=1 or header X-Import-Dry-Run=1
        dry_run_flag = request.args.get("dry_run") in ("1", "true", "True") or (
            request.headers.get("X-Import-Dry-Run") in ("1", "true", "True")
        )
        incremental = req_mode == "incremental"
        destructive = (
            (req_mode == "replace")
            or (not incremental and not replace_all_flag)
            and not dry_run_flag
        )  # legacy default remains destructive apply unless incremental explicitly requested
        # Normalise mode string for responses
        if incremental:
            mode = "incremental-dry-run" if dry_run_flag else "incremental"
        else:
            mode = "dry-run" if dry_run_flag else "apply"

        # Check if file was uploaded
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file: FileStorage = request.files["file"]
        filename = file.filename or ""
        if filename == "":
            return jsonify({"error": "No file selected"}), 400

        # Validate file extension
        if not filename.lower().endswith(".json"):
            return jsonify({"error": "File must be a JSON file"}), 400

        # Get the concepts collection
        coll = ConceptsRepository.collection()
        if coll is None:
            current_app.logger.error("Could not access concepts collection")
            return jsonify({"error": "Database connection failed"}), 500
        concepts_collection = coll  # local alias with non-None value

        # Read and parse JSON file
        try:
            file_content = file.read().decode("utf-8")
            import_data = json.loads(file_content)
        except json.JSONDecodeError as e:
            current_app.logger.error(f"Invalid JSON file: {e}")
            return jsonify({"error": f"Invalid JSON file: {str(e)}"}), 400
        except UnicodeDecodeError as e:
            current_app.logger.error(f"File encoding error: {e}")
            return jsonify({"error": "File must be UTF-8 encoded"}), 400

        # Validate that import_data is a list
        if not isinstance(import_data, list):
            return (
                jsonify({"error": "JSON file must contain an array of concepts"}),
                400,
            )

        # Validate concept documents against unified schema
        validation_errors = []
        valid_concepts = []

        for i, concept in enumerate(import_data):
            if not isinstance(concept, dict):
                validation_errors.append(f"Item {i}: Must be an object")
                continue

            # Validate unified schema structure
            errors = _validate_unified_concept_schema(concept, i)
            if errors:
                validation_errors.extend(errors)
                continue

            # Convert string _id back to ObjectId if present
            if "_id" in concept and isinstance(concept["_id"], str):
                try:
                    concept["_id"] = ObjectId(concept["_id"])
                except:
                    # If conversion fails, remove _id to let MongoDB generate new one
                    del concept["_id"]

            valid_concepts.append(concept)

        if validation_errors:
            current_app.logger.error(
                f"Validation errors: {validation_errors[:10]}"
            )  # Log first 10 errors
            error_summary = (
                f"Validation failed. Found {len(validation_errors)} error(s)."
            )
            if len(validation_errors) > 10:
                error_summary += f" Showing first 10 errors."

            return (
                jsonify(
                    {
                        "error": error_summary,
                        "details": validation_errors[:10],  # Return first 10 errors
                        "total_errors": len(validation_errors),
                    }
                ),
                400,
            )

        if not valid_concepts:
            return jsonify({"error": "No valid concepts found in file"}), 400

        # Current concepts snapshot (ids only) for diff statistics
        existing_count = concepts_collection.count_documents({})
        existing_ids = set()
        try:
            for doc in concepts_collection.find({}, {"concept_id": 1}):
                cid = doc.get("concept_id")
                if cid:
                    existing_ids.add(cid)
        except Exception as e:
            current_app.logger.warning(
                f"Failed to enumerate existing concept ids for diff: {e}"
            )

        incoming_ids = set()
        for c in valid_concepts:
            cid = c.get("concept_id")
            if cid:
                incoming_ids.add(cid)

        to_add = incoming_ids - existing_ids
        to_delete = existing_ids - incoming_ids
        unchanged = existing_ids & incoming_ids

        # Incremental diff enhancements
        updates_detail = []
        update_ids = set()
        if incremental and unchanged:
            # Build a map of existing docs for shallow compare (only concept_id + top-level fields)
            existing_map = {}
            try:
                for doc in concepts_collection.find(
                    {"concept_id": {"$in": list(unchanged)}}, {"_id": 0}
                ):
                    cid = doc.get("concept_id")
                    if cid:
                        existing_map[cid] = doc
            except Exception as _e:  # pragma: no cover - defensive
                current_app.logger.warning(
                    f"[import] incremental existing fetch failed: {_e}"
                )
            for c in valid_concepts:
                cid = c.get("concept_id")
                if not cid or cid not in existing_map:
                    continue
                old = existing_map[cid]
                changed_fields = []
                for k, new_val in c.items():
                    if k == "_id":
                        continue
                    if old.get(k) != new_val:
                        changed_fields.append(k)
                # Detect removed top-level keys (treat as change if key absent in new)
                for k in old.keys():
                    if k not in c and k != "_id":
                        changed_fields.append(k)
                if changed_fields:
                    update_ids.add(cid)
                    if len(updates_detail) < 25:
                        updates_detail.append(
                            {"concept_id": cid, "changed_fields": changed_fields}
                        )

        if incremental:
            # In incremental mode we never delete automatically; deletions only via destructive path
            effective_delete_count = 0
            unchanged_effective = unchanged - update_ids
        else:
            effective_delete_count = len(to_delete)
            unchanged_effective = unchanged

        diff_stats = {
            "existing": existing_count,
            "incoming": len(valid_concepts),
            "add_count": len(to_add),
            "delete_count": effective_delete_count,
            "unchanged_count": len(unchanged_effective),
        }
        if incremental:
            diff_stats["update_count"] = len(update_ids)

        if dry_run_flag:
            current_app.logger.info(f"[import] {mode} stats: {diff_stats}")
            # Update metrics (non-destructive)
            try:
                now_iso = _utc_now_iso()
                if not _IMPORT_METRICS:
                    _IMPORT_METRICS.update(
                        {
                            "run_count": 0,
                            "dry_run_count": 0,
                            "apply_count": 0,
                            "incremental_run_count": 0,
                        }
                    )
                _IMPORT_METRICS.update(
                    {
                        "last_run": now_iso,
                        "mode": mode,
                        "diff": diff_stats,
                        "success": True,
                        "backup_collection": None,
                        "last_update_count": diff_stats.get("update_count"),
                        "last_added_count": diff_stats.get("add_count"),
                    }
                )
                _IMPORT_METRICS["run_count"] += 1
                _IMPORT_METRICS["dry_run_count"] += 1
                if incremental:
                    _IMPORT_METRICS["incremental_run_count"] = (
                        _IMPORT_METRICS.get("incremental_run_count", 0) + 1
                    )
            except Exception as _e:  # pragma: no cover - defensive
                current_app.logger.warning(
                    f"[import] metrics update failed (dry-run): {_e}"
                )
            payload = {
                "success": True,
                "mode": mode,
                "diff": diff_stats,
                "message": "Dry run only. No changes applied.",
            }
            if incremental:
                payload["updates_detail"] = updates_detail
            return jsonify(payload), 200

        if incremental:
            # Non-destructive incremental apply
            added_count = 0
            updated_count = 0
            # Insert new concepts
            if to_add:
                to_insert = [c for c in valid_concepts if c.get("concept_id") in to_add]
                try:
                    if to_insert:
                        concepts_collection.insert_many(to_insert, ordered=False)
                        added_count = len(to_insert)
                except BulkWriteError as bwe:  # pragma: no cover - rare path
                    added_count = bwe.details.get("nInserted", 0)
                    current_app.logger.warning(
                        "[import] incremental insert partial failures: %s",
                        len(bwe.details.get("writeErrors", [])),
                    )
            # Apply updates (shallow)
            if update_ids:
                existing_map2 = {}
                try:
                    for doc in concepts_collection.find(
                        {"concept_id": {"$in": list(update_ids)}}, {"_id": 0}
                    ):
                        cid = doc.get("concept_id")
                        if cid:
                            existing_map2[cid] = doc
                except Exception as _e:  # pragma: no cover
                    current_app.logger.warning(
                        f"[import] incremental update fetch failed: {_e}"
                    )
                for c in valid_concepts:
                    cid = c.get("concept_id")
                    if cid in update_ids:
                        old = existing_map2.get(cid, {})
                        patch = {}
                        for k, v in c.items():
                            if k == "_id":
                                continue
                            if old.get(k) != v:
                                patch[k] = v
                        # Removed keys not handled (safety) – future enhancement could use $unset
                        if patch:
                            try:
                                concepts_collection.update_one(
                                    {"concept_id": cid}, {"$set": patch}
                                )
                                updated_count += 1
                            except Exception as _e:  # pragma: no cover
                                current_app.logger.warning(
                                    f"[import] incremental update failed for {cid}: {_e}"
                                )
            current_app.logger.info(
                f"[import] incremental complete: added={added_count} updated={updated_count} unchanged={len(unchanged) - len(update_ids)}"
            )
            resp = {
                "success": True,
                "mode": mode,
                "message": "Incremental import complete",
                "added_count": added_count,
                "updated_count": updated_count,
                "skipped_unchanged": len(unchanged) - len(update_ids),
                "diff": diff_stats,
            }
            if updates_detail:
                resp["updates_detail"] = updates_detail
            # Metrics update
            try:
                now_iso = _utc_now_iso()
                if not _IMPORT_METRICS:
                    _IMPORT_METRICS.update(
                        {
                            "run_count": 0,
                            "dry_run_count": 0,
                            "apply_count": 0,
                            "incremental_run_count": 0,
                        }
                    )
                _IMPORT_METRICS.update(
                    {
                        "last_run": now_iso,
                        "mode": mode,
                        "diff": diff_stats,
                        "success": True,
                        "backup_collection": None,
                        "last_update_count": diff_stats.get("update_count"),
                        "last_added_count": diff_stats.get("add_count"),
                    }
                )
                _IMPORT_METRICS["run_count"] += 1
                _IMPORT_METRICS["apply_count"] += 1
                _IMPORT_METRICS["incremental_run_count"] = (
                    _IMPORT_METRICS.get("incremental_run_count", 0) + 1
                )
            except Exception as _e:  # pragma: no cover
                current_app.logger.warning(
                    f"[import] metrics update failed (incremental apply): {_e}"
                )
            return jsonify(resp), 200

        # Destructive path (legacy / replace_all)
        # Create backup before destructive apply
        current_app.logger.info("Creating backup before import (apply mode)...")
        backup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_collection_name = f"concepts_backup_{backup_timestamp}"

        try:
            current_concepts = list(concepts_collection.find({}))
            if current_concepts:
                backup_collection = concepts_collection.database[backup_collection_name]
                backup_collection.insert_many(current_concepts)
                current_app.logger.info(
                    f"Created backup collection: {backup_collection_name}"
                )
        except Exception as e:
            current_app.logger.error(f"Failed to create backup: {e}")
            return jsonify({"error": "Failed to create backup before import"}), 500

        current_app.logger.info(
            (
                "[import] apply replacing %s existing with %s incoming; "
                "add=%s delete=%s unchanged=%s"
            ),
            existing_count,
            len(valid_concepts),
            len(to_add),
            len(to_delete),
            len(unchanged),
        )

        try:
            concepts_collection.drop()
            coll2 = ConceptsRepository.collection()
            if coll2 is None:
                current_app.logger.error(
                    "Could not access concepts collection after drop"
                )
                return jsonify({"error": "Database connection failed after drop"}), 500
            concepts_collection = coll2

            batch_size = 1000
            inserted_count = 0
            for i in range(0, len(valid_concepts), batch_size):
                batch = valid_concepts[i : i + batch_size]
                try:
                    result = concepts_collection.insert_many(batch, ordered=False)
                    inserted_count += len(result.inserted_ids)
                except BulkWriteError as e:
                    inserted_count += e.details.get("nInserted", 0)
                    current_app.logger.warning(
                        "Batch %s: %s inserted, %s errors",
                        (i // batch_size) + 1,
                        e.details.get("nInserted", 0),
                        len(e.details.get("writeErrors", [])),
                    )

            current_app.logger.info(
                f"[import] apply complete: {inserted_count} inserted"
            )
            resp = {
                "success": True,
                "mode": mode,
                "message": f"Successfully imported {inserted_count} concepts",
                "imported_count": inserted_count,
                "backup_collection": backup_collection_name,
                "diff": diff_stats,
            }
            # Metrics update (apply)
            try:
                now_iso = _utc_now_iso()
                if not _IMPORT_METRICS:
                    _IMPORT_METRICS.update(
                        {"run_count": 0, "dry_run_count": 0, "apply_count": 0}
                    )
                _IMPORT_METRICS.update(
                    {
                        "last_run": now_iso,
                        "mode": "apply",
                        "diff": diff_stats,
                        "success": True,
                        "backup_collection": backup_collection_name,
                    }
                )
                _IMPORT_METRICS["run_count"] += 1
                _IMPORT_METRICS["apply_count"] += 1
            except Exception as _e:  # pragma: no cover
                current_app.logger.warning(
                    f"[import] metrics update failed (apply): {_e}"
                )
            return jsonify(resp), 200
        except Exception as e:
            current_app.logger.error(f"Error during import operation: {e}")
            return jsonify({"error": f"Import failed: {str(e)}"}), 500

    except Exception as e:
        current_app.logger.error(f"Error importing ontology: {e}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred during import."}), 500


def _serialize_document(doc):
    """Helper function to serialize MongoDB document for JSON export."""
    if isinstance(doc, dict):
        serialized = {}
        for key, value in doc.items():
            if isinstance(value, ObjectId):
                serialized[key] = str(value)
            elif isinstance(value, datetime):
                serialized[key] = value.isoformat()
            elif isinstance(value, (dict, list)):
                serialized[key] = _serialize_document(value)
            else:
                serialized[key] = value
        return serialized
    elif isinstance(doc, list):
        return [_serialize_document(item) for item in doc]
    elif isinstance(doc, ObjectId):
        return str(doc)
    elif isinstance(doc, datetime):
        return doc.isoformat()
    else:
        return doc


@settings_bp.route("/user/current", methods=["GET"])
def get_current_user():
    """Return current user / organisation context placeholder values.

    Post JVNAUTOSCI-628: Server-side user/org storage removed - client localStorage
    is now sole authority. This endpoint returns placeholder values for backward
    compatibility with any remaining frontend code.
    """
    try:
        user_id = session.get("user_concept_id", EXAMPLE_USER_CONCEPT_ID)
        user_name = session.get("user_name", "Default User")
        org_id = session.get("organisation_concept_id", "#V#example_organization")
        org_name = session.get("organisation_name", "Example Organization")

        return (
            jsonify(
                {
                    "user_id": user_id,
                    "username": user_name,
                    "organization_id": org_id,
                    "organization_name": org_name,
                }
            ),
            200,
        )
    except Exception as e:
        current_app.logger.error(f"Error getting current user: {e}", exc_info=True)
        return (
            jsonify(
                {
                    "user_id": EXAMPLE_USER_CONCEPT_ID,
                    "username": "Default User",
                    "organization_id": "#V#example_organization",
                    "organization_name": "Example Organization",
                }
            ),
            200,
        )


@settings_bp.route("/metrics/deprecations", methods=["GET"])
def get_deprecation_metrics():
    """Return persisted deprecation & performance metrics for monitoring."""
    try:
        db = get_db()
        if db is None:
            return jsonify({"success": False, "error": "Database unavailable"}), 503
        coll = db.get_collection("system_metrics")
        dep_doc = coll.find_one({"_id": "deprecation_counters"}) or {}
        perf_doc = coll.find_one({"_id": "performance_counters"}) or {}
        ui_doc = coll.find_one({"_id": "ui_counters"}) or {}
        fe_tree_doc = coll.find_one({"_id": "frontend_tree_load"}) or {}
        counters = dep_doc.get("counters", {}) or {}
        updated_at = dep_doc.get("updated_at")
        if isinstance(updated_at, datetime):
            updated_at = updated_at.isoformat()
        tree_perf = perf_doc.get("vontology_tree_build") or {}
        avg_build = None
        try:
            if tree_perf.get("count") and tree_perf.get("total_build_sec") is not None:
                c = tree_perf.get("count") or 0
                if c > 0:
                    avg_build = tree_perf.get("total_build_sec", 0.0) / c
        except Exception:
            avg_build = None
        name_norm = ui_doc.get("name_normalizations") or {}
        by_kind = name_norm.get("by_kind") or {}
        total_norm = name_norm.get("total") or 0
        fe_totals = fe_tree_doc.get("totals") or {}
        fe_count = fe_totals.get("count") or 0
        fe_total_ms = (
            fe_totals.get("total_ms")
            if isinstance(fe_totals.get("total_ms"), (int, float))
            else None
        )
        avg_frontend_ms = None
        if fe_count and fe_total_ms is not None:
            try:
                avg_frontend_ms = fe_total_ms / fe_count
            except Exception:
                avg_frontend_ms = None
        return (
            jsonify(
                {
                    "success": True,
                    "counters": counters,
                    "updated_at": updated_at,
                    "performance": {
                        "tree_build": {
                            "count": tree_perf.get("count", 0),
                            "last_sec": tree_perf.get("last_build_sec"),
                            "max_sec": tree_perf.get("max_build_sec"),
                            "avg_sec": avg_build,
                            "last_built_at": tree_perf.get("last_built_at"),
                        },
                        "frontend_tree_load": {
                            "count": fe_count,
                            "last_ms": fe_totals.get("last_ms"),
                            "max_ms": fe_totals.get("max_ms"),
                            "avg_ms": avg_frontend_ms,
                            "last_at": fe_totals.get("last_at"),
                            "cache": fe_tree_doc.get("cache") or {},
                            "nodes": fe_tree_doc.get("nodes") or {},
                        },
                    },
                    "ui_counters": {
                        "name_normalizations": {"total": total_norm, "by_kind": by_kind}
                    },
                }
            ),
            200,
        )
    except Exception as e:  # pragma: no cover
        current_app.logger.error(f"Error fetching metrics: {e}")
        return jsonify({"success": False, "error": "Failed to fetch metrics"}), 500


# To make this blueprint usable, it needs to be registered in your main Flask app,
# typically in src/workflows/von/main.py or wherever your Flask app is initialized.
# Example:
# from src.backend.server.routes.settings_routes import settings_bp
# app.register_blueprint(settings_bp)
