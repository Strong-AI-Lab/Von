import os
import json
import threading
import time
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app, send_file, session
import tempfile
from ...vontology.utils_vontology import get_concept_notes  # relative import
from typing import Optional, Tuple, Dict, Any, Mapping
from werkzeug.datastructures import FileStorage
from ...services.settings_service import (
    get_openai_env_var,
    set_openai_env_var,
    set_fetch_counts_on_load,
    set_preload_vontology_tree,
    get_ollama_hosts_list,
    set_ollama_hosts_list,
    get_active_ollama_host,
    set_active_ollama_host,
    resolve_llm_setting,
    resolve_enabled_llm_settings,
    set_user_llm_setting,
    set_org_llm_setting,
    set_disable_remote_ollama_scan,
    set_show_tool_use_during_thinking,
    get_internal_mcp_max_tool_invocations,
    get_internal_mcp_tool_batch_cap,
    set_internal_mcp_max_tool_invocations,
    set_internal_mcp_tool_batch_cap,
    INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MIN,
    INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MAX,
    INTERNAL_MCP_TOOL_BATCH_CAP_MIN,
    INTERNAL_MCP_TOOL_BATCH_CAP_MAX,
    get_gmail_outbound_rate_limit_settings,
    parse_gmail_outbound_rate_limit_settings,
    set_gmail_outbound_rate_limit_settings,
    get_disable_write_tool_conservatism,
    set_disable_write_tool_conservatism,
    get_global_mutation_authority_level,
    get_require_human_review_for_high_impact_kb_writes,
    get_user_mutation_authority_level,
    set_require_human_review_for_high_impact_kb_writes,
    set_user_mutation_authority_level,
    set_buttonify_model_enabled,
    set_auto_proceed_minimal_imposition_enabled,
    get_all_settings_batch,
    get_server_default_llm_setting,
    resolve_rag_embedder_setting,
    resolve_rag_llm_setting,
    set_rag_embedder_setting,
    set_rag_llm_setting,
    set_server_default_llm_setting,
    get_model_llm_timeout_overrides,
    set_model_llm_timeout,
    _make_model_timeout_key,
)
from ...services.feature_flags import (
    get_expert_footer_enabled,
    get_expert_tabs_enabled,
)
from ...services.paper_recommendation_profile_vontology_service import (
    load_paper_recommendation_profile,
    upsert_paper_recommendation_profile,
)
from ...services.paper_recommendation_review_service import (
    build_paper_recommendation_review,
)
from ...services.paper_recommendation_workflow_vontology_service import (
    request_paper_recommendation_refresh,
)
from ...services.window_session_context_service import get_effective_context
from ...security.access_control import (
    LEGACY_IDENTITY_HEADER_ACTOR_SOURCE,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
    get_effective_user_concept_id_with_source,
)
from ...services.buttonify_service import BUTTONIFY_PROMPT_IDS
from ...services.workflow_capability_service import (
    invalidate_workflow_capability_index,
    prewarm_workflow_capability_index,
)
from ...services.mongo_observability_service import (
    build_mongo_operation_comment,
    build_mongo_cost_guardrail_report,
    observe_mongo_operation,
)
from ...services.model_parameter_service import (
    MODEL_PARAMETERS_KEY,
    build_model_parameter_capabilities,
    normalise_model_parameters_for_storage,
    openai_responses_kwargs_from_model_parameters,
)
from ...services.llm_model_cost_history_service import (
    get_actor_historical_model_cost_summary,
)
from ...services.model_registry_service import get_model_registry_snapshot
from ...services.rag_service import peek_rag_service
from ...integrations.google.gmail_service import list_profile_ids_from_env
from ...services.concept_service import list_concepts, get_concept_by_id
from ...services.concept_service import ConceptNotFoundError
from ...languagemodels.llm_interface import (
    GeminiClient,
    MetaMuseClient,
    ModelExecutionEligibilityError,
    OpenAIClient,
    OpenRouterClient,
    assert_model_execution_allowed,
    get_ollama_auto_pull_state_snapshot,
    resolve_openai_responses_max_output_tokens,
)
from ...utils.runtime_env import (
    load_secret_from_env_or_file,
    read_repo_dotenv_values,
    read_secret_file,
)
from ...db.repositories.concepts_repository import ConceptsRepository
from bson import ObjectId
from pymongo.errors import BulkWriteError
from ...db.mongo_client import (
    MONGO_URI,
    DATABASE_NAME,
    get_db,
    get_effective_mongo_uri,
    get_mongo_fallback_policy_state,
    is_using_fallback_uri,
    assert_destructive_db_operation_allowed,
)
from ...db.mongo_uri_redaction import (
    build_safe_mongo_connection_location,
    classify_mongo_connection_location,
    sanitize_mongo_uri_for_display,
)
from ...workflows.write_tool_policy import (
    MUTATION_AUTHORITY_LEVEL_ADDITIVE_VONTOLOGY,
    MUTATION_AUTHORITY_LEVEL_DESTRUCTIVE_VONTOLOGY_WITH_CONFIRMATION,
    MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED,
    MUTATION_AUTHORITY_LEVEL_MUTATIVE_VONTOLOGY_NON_DESTRUCTIVE,
    MUTATION_AUTHORITY_LEVEL_READ_ONLY,
)

# REFACTORING_NOTE: This blueprint is part of the backend model selection refactoring.
# It provides API endpoints for managing global application settings.


def _build_runtime_component_signature_from_resolution(
    kind: str,
    resolution: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(resolution, Mapping):
        return None
    effective = resolution.get("effective")
    if not isinstance(effective, Mapping):
        return None
    provider = str(effective.get("provider") or "").strip().lower()
    model = str(effective.get("model") or "").strip()
    if not provider or not model:
        return None
    host = str(effective.get("host") or "").strip()
    return {
        "schema_version": "rag_component_signature.v1",
        "kind": str(kind or "").strip() or "unknown",
        "provider": provider,
        "model": model,
        "host": host or None,
    }


def _invalidate_cached_rag_runtime_configuration() -> None:
    service = peek_rag_service("llamaindex")
    invalidate = (
        getattr(service, "invalidate_runtime_configuration_cache", None)
        if service is not None
        else None
    )
    if callable(invalidate):
        invalidate()


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
            errors.append(
                (
                    f"Item {index}: 'concept_data.preserved_fields' is deprecated. "
                    "Use canonical text relations (hasDescription/hasNote/hasContent)."
                )
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

META_MUSE_SUPPORTED_MODELS = ("muse-spark-1.3",)

_SHARED_RUNTIME_MODEL_SETTING_KEYS = frozenset(
    {"server_default_llm", "rag_embedder", "rag_llm"}
)
_ADMIN_ONLY_SETTING_KEYS = _SHARED_RUNTIME_MODEL_SETTING_KEYS | frozenset(
    {
        "disable_write_tool_conservatism",
        "require_human_review_for_high_impact_kb_writes",
        "gmail_outbound_rate_limits",
    }
)


def _jsonify_no_store(payload: dict[str, Any], status_code: int = 200):
    response = jsonify(payload)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response


def _has_valid_admin_token() -> bool:
    """Return whether this request carries the configured ops admin token."""

    try:
        required_token = os.getenv("VON_ADMIN_TOKEN")
        if not required_token:
            return False
        provided = request.headers.get("X-Von-Admin-Token") or request.headers.get(
            "X-Admin-Token"
        )
        return bool(provided) and provided == required_token
    except Exception:
        return False


def _is_admin_or_owner_session() -> bool:
    """Best-effort check for admin/owner privileges.

    We accept either a session role (normal UI path) or an admin token header
    (for scripted/ops use when configured via VON_ADMIN_TOKEN).
    """

    try:
        effective = get_effective_context(
            request.headers.get("X-Von-Window-Session"),
            dict(session),
            session.get("user_concept_id"),
        )
        role = (
            effective.get("role")
            if isinstance(effective, dict)
            else session.get("role_in_org")
        )
        if isinstance(role, str) and role.strip().lower() in {"admin", "owner"}:
            return True
    except Exception:
        pass

    return _has_valid_admin_token()


def _normalise_scope_concept_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("#") else f"#V#{cleaned}"


def _authorise_active_llm_scope(
    scope: str,
    concept_id: Any,
) -> tuple[str | None, str | None]:
    """Bind a client model-setting target to ambient actor authority.

    Returns the canonical target concept ID and no error on success.  Client
    claims never establish identity or organisation authority.
    """

    target_id = _normalise_scope_concept_id(concept_id)
    if target_id is None:
        return None, "Model changes require a valid scoped concept ID."

    # Model-setting mutations must be bound to identity established in the
    # signed server-side Flask session.  ``get_effective_user_concept_id`` also
    # supports a legacy, concept-validated identity header for read/tool paths;
    # that header is not authentication and must never authorise a settings
    # write.  The configured admin token remains the explicit automation path.
    actor_user_id = _normalise_scope_concept_id(session.get("user_concept_id"))
    if actor_user_id is None:
        if _has_valid_admin_token():
            return target_id, None
        return None, "An authenticated actor is required to update model settings."

    if scope == "user":
        if actor_user_id != target_id:
            return (
                None,
                "The selected user model scope does not match the authenticated user.",
            )
        return target_id, None

    if scope == "organisation":
        effective = get_effective_context(
            request.headers.get("X-Von-Window-Session"),
            dict(session),
            actor_user_id,
        )
        actor_org_id = _normalise_scope_concept_id(effective.get("organisation_id"))
        if actor_org_id is None or actor_org_id != target_id:
            return (
                None,
                "The selected organisation model scope does not match the effective organisation.",
            )
        if not _is_admin_or_owner_session():
            return (
                None,
                "Admin or owner privileges are required to update an organisation model scope.",
            )
        return target_id, None

    return None, "Model scope must be 'user' or 'organisation'."


def _can_access_user_scoped_profile(user_concept_id: str) -> bool:
    """Allow only the authenticated user (or admin/owner) to access a profile."""

    requested_id = str(user_concept_id or "").strip()
    if not requested_id:
        return False
    session_user_id = str(session.get("user_concept_id") or "").strip()
    if not session_user_id:
        return False
    return session_user_id == requested_id or _is_admin_or_owner_session()


# --- Import & Orphan Metrics (JVNAUTOSCI-584) ---
# These dicts are appended (never structural breaking changes) and surfaced via /diag.
# They remain empty until first relevant operation to avoid changing baseline /diag output.
_IMPORT_METRICS: dict = {}
_ORPHAN_SCAN_METRICS: dict = {}
_OLLAMA_MODELS_CACHE_LOCK = threading.Lock()
_OLLAMA_MODELS_CACHE: dict = {}
_OLLAMA_MODELS_CACHE_TTL_SECONDS_DEFAULT = 20.0


def _read_ollama_models_cache_ttl_seconds() -> float:
    raw = os.getenv(
        "VON_OLLAMA_MODELS_CACHE_TTL_SECONDS",
        str(_OLLAMA_MODELS_CACHE_TTL_SECONDS_DEFAULT),
    )
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        return _OLLAMA_MODELS_CACHE_TTL_SECONDS_DEFAULT
    return max(0.0, ttl)


def _read_cached_ollama_models(*, bypass_cache: bool) -> Optional[list]:
    if bypass_cache:
        return None

    ttl_seconds = _read_ollama_models_cache_ttl_seconds()
    if ttl_seconds <= 0.0:
        return None

    now_monotonic = time.monotonic()
    with _OLLAMA_MODELS_CACHE_LOCK:
        expires_at = _OLLAMA_MODELS_CACHE.get("expires_at_monotonic")
        models = _OLLAMA_MODELS_CACHE.get("models")
        if (
            not isinstance(expires_at, (int, float))
            or now_monotonic >= float(expires_at)
            or not isinstance(models, list)
        ):
            _OLLAMA_MODELS_CACHE.clear()
            return None
        return models


def _write_cached_ollama_models(models: list) -> None:
    ttl_seconds = _read_ollama_models_cache_ttl_seconds()
    if ttl_seconds <= 0.0:
        return

    now_monotonic = time.monotonic()
    with _OLLAMA_MODELS_CACHE_LOCK:
        _OLLAMA_MODELS_CACHE.clear()
        _OLLAMA_MODELS_CACHE.update(
            {
                "models": models,
                "stored_at_monotonic": now_monotonic,
                "expires_at_monotonic": now_monotonic + ttl_seconds,
            }
        )


# --- DB-location info cache (JVNAUTOSCI-2383) -----------------------------
#
# /api/settings/db/info is one of the hottest pollers (observed ~367 calls in a
# single log window) and each call issued a live Mongo ping plus, in fallback
# mode, an outbound ipify HTTP lookup. The reported state (URI classification,
# ping, fallback, public IP) changes very rarely, so a short global TTL cache
# removes that per-poll Mongo/IO cost without changing what users see. The
# response is identical for all callers, so a single global slot is correct.
_DB_INFO_CACHE_LOCK = threading.Lock()
_DB_INFO_CACHE: dict = {}
_DB_INFO_CACHE_TTL_SECONDS_DEFAULT = 10.0


def _read_db_info_cache_ttl_seconds() -> float:
    raw = os.getenv(
        "VON_DB_INFO_CACHE_TTL_SECONDS",
        str(_DB_INFO_CACHE_TTL_SECONDS_DEFAULT),
    )
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        return _DB_INFO_CACHE_TTL_SECONDS_DEFAULT
    return max(0.0, ttl)


def _read_cached_db_info(*, bypass_cache: bool) -> Optional[dict]:
    if bypass_cache:
        return None
    ttl_seconds = _read_db_info_cache_ttl_seconds()
    if ttl_seconds <= 0.0:
        return None
    now_monotonic = time.monotonic()
    with _DB_INFO_CACHE_LOCK:
        expires_at = _DB_INFO_CACHE.get("expires_at_monotonic")
        payload = _DB_INFO_CACHE.get("payload")
        if (
            not isinstance(expires_at, (int, float))
            or now_monotonic >= float(expires_at)
            or not isinstance(payload, dict)
        ):
            _DB_INFO_CACHE.clear()
            return None
        return dict(payload)


def _write_cached_db_info(payload: dict) -> None:
    ttl_seconds = _read_db_info_cache_ttl_seconds()
    if ttl_seconds <= 0.0:
        return
    now_monotonic = time.monotonic()
    with _DB_INFO_CACHE_LOCK:
        _DB_INFO_CACHE.clear()
        _DB_INFO_CACHE.update(
            {
                "payload": dict(payload),
                "stored_at_monotonic": now_monotonic,
                "expires_at_monotonic": now_monotonic + ttl_seconds,
            }
        )


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


def _sanitize_mongo_uri_for_display(uri: str | None) -> str | None:
    """Compatibility wrapper for older internal callers."""

    return sanitize_mongo_uri_for_display(uri)


@settings_bp.route("/db/info", methods=["GET"])
def get_db_location_info():
    """API endpoint to report the MongoDB location (without credentials)."""
    try:
        bypass_cache = request.args.get("nocache") in {"1", "true", "yes", "on"}
        cached_payload = _read_cached_db_info(bypass_cache=bypass_cache)
        if cached_payload is not None:
            return jsonify(cached_payload), 200

        # Use effective URI (may be fallback) for display classification
        effective_uri = get_effective_mongo_uri()
        sanitized_uri = _sanitize_mongo_uri_for_display(effective_uri)
        # Attempt a quick ping
        ping_ok = False
        mongo_ping_latency_ms = None
        error_message = None
        try:
            db = get_db()
            if db is not None:
                comment = build_mongo_operation_comment(
                    service="settings_routes",
                    collection="$cmd",
                    operation="get_db_location_info.ping",
                )
                ping_started_at = time.perf_counter()
                ping_success = False
                ping_error_type = None
                try:
                    if comment is not None:
                        db.command("ping", comment=comment)
                    else:
                        db.command("ping")
                    ping_success = True
                except TypeError as exc:
                    ping_error_type = type(exc).__name__
                    db.command("ping")
                    ping_success = True
                except Exception as ping_exc:
                    ping_error_type = type(ping_exc).__name__
                    raise
                finally:
                    if ping_success:
                        mongo_ping_latency_ms = round(
                            (time.perf_counter() - ping_started_at) * 1000.0,
                            3,
                        )
                    observe_mongo_operation(
                        service="settings_routes",
                        collection="$cmd",
                        operation="get_db_location_info.ping",
                        started_at=ping_started_at,
                        success=ping_success,
                        error_type=ping_error_type,
                    )
                ping_ok = True
            else:
                error_message = "No DB connection"
        except Exception as e:
            error_message = str(e)
            ping_ok = False

        # get_db()/ping may have switched between the primary and a fallback.
        # Snapshot route identity only after that recovery opportunity.
        effective_uri = get_effective_mongo_uri()
        sanitized_uri = _sanitize_mongo_uri_for_display(effective_uri)
        using_fallback = is_using_fallback_uri()
        fallback_policy = get_mongo_fallback_policy_state()
        fallback_kind = fallback_policy.get("active_fallback_kind")
        connection_location = build_safe_mongo_connection_location(
            effective_uri,
            using_fallback=using_fallback,
            fallback_kind=fallback_kind,
            fallback_target_uri=MONGO_URI,
        )
        classification = connection_location["classification"]
        public_ip = None
        if using_fallback and fallback_kind != "ssh_tunnel":
            # Try to discover outward-facing IP (best-effort, short timeout). Avoid blocking failures.
            try:
                import urllib.request

                with urllib.request.urlopen(
                    "https://api.ipify.org?format=text", timeout=1.5
                ) as resp:
                    txt = resp.read().decode("utf-8").strip()
                    if txt and len(txt) <= 64:
                        public_ip = txt
            except Exception:  # pragma: no cover - non-critical
                public_ip = None
        payload = {
            "sanitized_uri": sanitized_uri,
            "database_name": DATABASE_NAME,
            "ping_ok": ping_ok,
            # This is measured inside the Von server around the Mongo ping.
            # Browser-to-server round-trip latency is measured separately by
            # the frontend and must not be presented as database latency.
            "mongo_ping_latency_ms": mongo_ping_latency_ms,
            "error": error_message,
            "classification": classification,
            "connection_location": connection_location,
            "using_fallback": using_fallback,
            "fallback_kind": fallback_kind,
            "primary_uri_sanitized": (
                _sanitize_mongo_uri_for_display(MONGO_URI) if using_fallback else None
            ),
            "server_public_ip": public_ip,
        }
        # Only cache healthy responses so a transient ping failure is not pinned
        # for the whole TTL window.
        if ping_ok:
            _write_cached_db_info(payload)
        return jsonify(payload), 200
    except Exception as e:
        current_app.logger.error(f"Error retrieving DB info: {e}", exc_info=True)
        return jsonify({"error": "Failed to retrieve DB info."}), 500


def _classify_mongo_sanitized_uri(sanitized_uri: str | None) -> str:
    return classify_mongo_connection_location(sanitized_uri)


@settings_bp.route("/db/guardrails", methods=["GET"])
def get_db_guardrails():
    """Return redacted MongoDB cost/latency guardrails for operators."""
    try:
        effective_uri = get_effective_mongo_uri()
        fallback_policy = get_mongo_fallback_policy_state()
        connection_location = build_safe_mongo_connection_location(
            effective_uri,
            using_fallback=is_using_fallback_uri(),
            fallback_kind=fallback_policy.get("active_fallback_kind"),
            fallback_target_uri=MONGO_URI,
        )
        classification = connection_location["classification"]
        reset = request.args.get("reset") in {"1", "true", "yes", "on"}
        report = build_mongo_cost_guardrail_report(
            mongo_classification=classification,
            sanitized_uri=connection_location["logical_sanitized_uri"],
            using_fallback=is_using_fallback_uri(),
            reset=reset,
        )
        return jsonify({"success": True, "report": report}), 200
    except Exception as e:
        current_app.logger.error(
            "Error retrieving DB guardrail report: %s",
            e,
            exc_info=True,
        )
        return (
            jsonify({"success": False, "error": "Failed to retrieve DB guardrails."}),
            500,
        )


@settings_bp.route("/llm/info", methods=["GET"])
def get_llm_info():
    """API endpoint to report the LLM status."""
    try:
        # Get user/org context from query params or session
        user_concept_id = request.args.get("user_concept_id") or session.get(
            "user_concept_id"
        )
        org_concept_id = request.args.get(
            "organisation_concept_id"
        ) or request.args.get("organization_concept_id")

        resolved = resolve_llm_setting(
            user_concept_id=user_concept_id, org_concept_id=org_concept_id
        )
        active_llm = resolved or {}
        provider = active_llm.get("provider")
        model = active_llm.get("model")

        # Allow the client to pass the effective provider when a local model preference
        # overrides the DB-stored setting (e.g. premium disabled, Ollama selected locally).
        # Only known providers are accepted; any other value is ignored.
        effective_provider_override = request.args.get("effective_provider")
        if effective_provider_override in (
            "openai",
            "openrouter",
            "gemini",
            "meta",
            "ollama",
        ):
            provider = effective_provider_override
            effective_model_override = str(
                request.args.get("effective_model") or ""
            ).strip()
            if effective_model_override:
                model = effective_model_override

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

        elif provider == "openrouter":
            api_key = _resolve_settings_api_key("OPENROUTER_API_KEY")
            if not api_key:
                status = "missing_key"
                error_message = "OpenRouter API key not set"
            else:
                try:
                    OpenRouterClient(api_key=api_key).list_models()
                    status = "ready"
                    ping_ok = True
                except Exception as e:
                    status = "error"
                    error_message = "OpenRouter status check failed."
                    current_app.logger.warning(
                        "OpenRouter LLM status check failed (error_type=%s)",
                        type(e).__name__,
                    )

        elif provider == "gemini":
            api_key = _resolve_settings_api_key(
                "GEMINI_API_KEY",
                fallback_env_vars=("GOOGLE_API_KEY",),
            )
            if not api_key:
                status = "missing_key"
                error_message = "Gemini API key not set"
            else:
                try:
                    client = GeminiClient(
                        api_key=api_key,
                        **({"default_model": model} if model else {}),
                    )
                    client.list_models()
                    status = "ready"
                    ping_ok = True
                except Exception as e:
                    status = "error"
                    error_message = "Gemini status check failed."
                    current_app.logger.warning(
                        "Gemini LLM status check failed (error_type=%s)",
                        type(e).__name__,
                    )

        elif provider == "meta":
            api_key = _resolve_settings_api_key("META_API_KEY")
            if not api_key:
                status = "missing_key"
                error_message = "Meta API key not set"
            else:
                try:
                    available_models = MetaMuseClient(
                        api_key=api_key,
                        **({"default_model": model} if model else {}),
                    ).list_models()
                    if model and model not in available_models:
                        status = "error"
                        error_message = "The configured Meta model is not available."
                    else:
                        status = "ready"
                        ping_ok = True
                except Exception as e:
                    status = "error"
                    error_message = "Meta status check failed."
                    current_app.logger.warning(
                        "Meta LLM status check failed (error_type=%s)",
                        type(e).__name__,
                    )

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

        return _jsonify_no_store(
            {
                "provider": provider,
                "model": model,
                "status": status,
                "ping_ok": ping_ok,
                "error": error_message,
                "details": details,
            },
            200,
        )
    except Exception as e:
        current_app.logger.error(f"Error retrieving LLM info: {e}", exc_info=True)
        return jsonify({"error": "Failed to retrieve LLM info."}), 500


@settings_bp.route("/llm/cost_summary", methods=["GET"])
def get_llm_cost_summary():
    """Return a bounded persisted cost summary for the current actor/model."""

    user_id = get_effective_user_concept_id()
    effective = get_effective_context(
        request.headers.get("X-Von-Window-Session"),
        dict(session),
        user_id,
    )
    namespace = str(effective.get("namespace") or "").strip()
    provider = str(request.args.get("provider") or "").strip().lower()
    model = str(request.args.get("model") or "").strip()
    if not user_id or not namespace:
        return _jsonify_no_store(
            {"error": "An authenticated actor and namespace are required."}, 401
        )
    if not provider or not model:
        return _jsonify_no_store(
            {"error": "provider and model are required."}, 400
        )
    try:
        limit = int(request.args.get("limit") or 200)
        window_days = int(request.args.get("window_days") or 30)
    except (TypeError, ValueError):
        return _jsonify_no_store(
            {"error": "limit and window_days must be integers."}, 400
        )
    try:
        model_registry = get_model_registry_snapshot()
    except Exception:
        model_registry = None
    try:
        payload = get_actor_historical_model_cost_summary(
            user_id=user_id,
            namespace=namespace,
            provider=provider,
            model=model,
            model_registry=model_registry,
            limit=limit,
            window_days=window_days,
        )
    except Exception:
        current_app.logger.exception("Could not read persisted LLM cost history")
        return _jsonify_no_store(
            {"error": "Persisted LLM cost history is currently unavailable."},
            503,
        )
    return _jsonify_no_store(payload, 200)


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
    """Get settings that are independent of login-derived actor identity.

    Organisation and language remain separate actor preferences. Uses a batch
    DB fetch to reduce ~10 individual queries to 1.
    """
    try:
        # Batch fetch all DB-stored settings in one query
        db_settings = get_all_settings_batch()

        # Jira internal MCP guardrail environment settings (read-only; surfaced for visibility).
        jira_allow_list_raw = os.getenv("VON_JIRA_PROJECT_ALLOW_LIST") or os.getenv(
            "VON_JIRA_PROJECT_ALLOWLIST"
        )
        jira_allow_list_effective_raw = jira_allow_list_raw or "JVNAUTOSCI"
        jira_allow_list_effective: list[str] = []
        for part in jira_allow_list_effective_raw.split(","):
            candidate = part.strip().upper()
            if not candidate:
                continue
            if candidate not in jira_allow_list_effective:
                jira_allow_list_effective.append(candidate)

        jira_execute_mode_raw = os.getenv("VON_INTERNAL_MCP_JIRA_EXECUTE_MODE", "0")
        jira_execute_mode_enabled = str(jira_execute_mode_raw).strip().lower() in {
            "1",
            "true",
        }
        gmail_profiles = list_profile_ids_from_env()
        gmail_default_profile = os.getenv("VON_GMAIL_DEFAULT_PROFILE") or None
        if gmail_default_profile and gmail_default_profile not in gmail_profiles:
            gmail_default_profile = None

        # Merge DB settings with computed/env-var settings
        return {
            **db_settings,  # active_llm, openai_api_key_env_var, fetch_counts_on_load, etc.
            "gemini_api_key_env_var": "GEMINI_API_KEY",
            "meta_api_key_env_var": "META_API_KEY",
            "openrouter_api_key_env_var": "OPENROUTER_API_KEY",
            "buttonify_prompt_ids": list(BUTTONIFY_PROMPT_IDS),
            "buttonify_prompt_active": (
                BUTTONIFY_PROMPT_IDS[0] if BUTTONIFY_PROMPT_IDS else None
            ),
            "expert_tabs_enabled": get_expert_tabs_enabled(),
            "expert_footer_enabled": get_expert_footer_enabled(),
            "jira_project_allow_list_raw": jira_allow_list_raw,
            "jira_project_allow_list_effective": jira_allow_list_effective,
            "internal_mcp_jira_execute_mode_raw": jira_execute_mode_raw,
            "internal_mcp_jira_execute_mode_enabled": jira_execute_mode_enabled,
            "gmail_profiles": gmail_profiles,
            "gmail_default_profile": gmail_default_profile,
        }
    except Exception as e:
        current_app.logger.error(f"Error retrieving settings data: {e}", exc_info=True)
        return {}


@settings_bp.route("/", methods=["GET"])
def get_all_settings():
    """API endpoint to retrieve all relevant settings."""
    try:
        settings = get_all_settings_data()

        # Admin-only settings (never trust the client; only reveal to admins/owners)
        if _is_admin_or_owner_session():
            settings["disable_write_tool_conservatism"] = (
                get_disable_write_tool_conservatism()
            )
            settings["require_human_review_for_high_impact_kb_writes"] = (
                get_require_human_review_for_high_impact_kb_writes()
            )
            settings["gmail_outbound_rate_limits"] = (
                get_gmail_outbound_rate_limit_settings().as_dict()
            )
        else:
            # Remove admin-only setting if it leaked via batch query
            settings.pop("disable_write_tool_conservatism", None)
            settings.pop("require_human_review_for_high_impact_kb_writes", None)
            settings.pop("gmail_outbound_rate_limits", None)
        selected_model_scope = str(request.args.get("model_scope") or "").strip()
        if selected_model_scope and selected_model_scope not in {
            "user",
            "organisation",
        }:
            return _jsonify_no_store(
                {
                    "error": "model_scope must be 'user' or 'organisation'.",
                },
                400,
            )
        effective_user_concept_id = get_effective_user_concept_id()
        if effective_user_concept_id:
            # An authenticated actor owns its model-settings read scope.  Query
            # parameters may help the pre-login bootstrap path, but cannot
            # replace a current session/window actor.
            user_concept_id = effective_user_concept_id
            org_concept_id = get_effective_organisation_concept_id()
            effective_llm = resolve_llm_setting(
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
            )
            effective_enabled_llms = resolve_enabled_llm_settings(
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
            )
            if selected_model_scope == "user":
                selected_user_concept_id = user_concept_id
                selected_org_concept_id = None
            elif selected_model_scope == "organisation":
                if not org_concept_id:
                    return _jsonify_no_store(
                        {
                            "error": (
                                "No effective organisation is available for the "
                                "selected model scope."
                            ),
                        },
                        403,
                    )
                selected_user_concept_id = None
                selected_org_concept_id = org_concept_id
            else:
                selected_user_concept_id = user_concept_id
                selected_org_concept_id = org_concept_id
        else:
            if selected_model_scope:
                return _jsonify_no_store(
                    {
                        "error": (
                            "An authenticated actor is required to select a model scope."
                        ),
                    },
                    403,
                )
            # No authenticated session actor means there is no safe scoped
            # model-settings read.  Query claims are untrusted and must not be
            # used to disclose another user's or organisation's primary/pool.
            # Keep the unauthenticated response useful for shared/bootstrap
            # settings while returning an empty scoped model projection.
            user_concept_id = None
            org_concept_id = None
            selected_user_concept_id = None
            selected_org_concept_id = None
            effective_llm = resolve_llm_setting(
                user_concept_id=None,
                org_concept_id=None,
            )
            effective_enabled_llms = resolve_enabled_llm_settings(
                user_concept_id=None,
                org_concept_id=None,
            )
        if selected_model_scope:
            resolved = resolve_llm_setting(
                user_concept_id=selected_user_concept_id,
                org_concept_id=selected_org_concept_id,
            )
            enabled_llms = resolve_enabled_llm_settings(
                user_concept_id=selected_user_concept_id,
                org_concept_id=selected_org_concept_id,
            )
        else:
            resolved = effective_llm
            enabled_llms = effective_enabled_llms
        settings["resolved_llm"] = resolved
        settings["effective_llm"] = effective_llm
        settings["selected_model_scope"] = selected_model_scope or None
        settings["server_default_llm"] = get_server_default_llm_setting()
        settings["enabled_llms"] = enabled_llms
        settings["effective_enabled_llms"] = effective_enabled_llms
        # RAG and the workflow capability index are singleton server services;
        # their models do not inherit browser-only or actor-scoped chat state.
        settings["effective_rag_embedder"] = resolve_rag_embedder_setting()
        settings["effective_rag_llm"] = resolve_rag_llm_setting()
        settings["ollama_model_auto_pull"] = get_ollama_auto_pull_state_snapshot()
        settings["available_mutation_authority_levels"] = [
            MUTATION_AUTHORITY_LEVEL_READ_ONLY,
            MUTATION_AUTHORITY_LEVEL_ADDITIVE_VONTOLOGY,
            MUTATION_AUTHORITY_LEVEL_MUTATIVE_VONTOLOGY_NON_DESTRUCTIVE,
            MUTATION_AUTHORITY_LEVEL_DESTRUCTIVE_VONTOLOGY_WITH_CONFIRMATION,
            MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED,
        ]
        settings["global_mutation_authority_level"] = (
            get_global_mutation_authority_level()
        )
        if user_concept_id:
            settings["resolved_mutation_authority"] = {
                "scope": "user",
                "concept_id": user_concept_id,
                "level": (
                    get_user_mutation_authority_level(user_concept_id)
                    or MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED
                ),
            }
        return _jsonify_no_store(settings, 200)
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
        if any(key in data for key in _ADMIN_ONLY_SETTING_KEYS):
            if not _is_admin_or_owner_session():
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": (
                                "Admin or owner privileges are required to update "
                                "the requested protected settings."
                            ),
                        }
                    ),
                    403,
                )

        llm_scope = None
        llm_scope_concept_id = None
        llm_provider = None
        llm_model = None
        llm_host = None
        llm_model_parameters: dict[str, Any] = {}
        enabled_entries_requested = "enabled_llms" in data
        enabled_entries = data.get("enabled_llms")
        if enabled_entries_requested and not isinstance(enabled_entries, list):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "enabled_llms must be a list of provider/model entries.",
                    }
                ),
                400,
            )

        llm_data = data.get("active_llm")
        if llm_data:
            if not isinstance(llm_data, dict):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "active_llm must be a scoped model object.",
                        }
                    ),
                    400,
                )
            llm_provider = llm_data.get("provider")
            llm_model = llm_data.get("model")
            raw_llm_host = llm_data.get("host")
            if raw_llm_host is not None and not isinstance(raw_llm_host, str):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "active_llm host must be a string when supplied.",
                        }
                    ),
                    400,
                )
            llm_host = str(raw_llm_host or "").strip() or None
            llm_scope = llm_data.get("scope")
            claimed_concept_id = llm_data.get("concept_id")
            if (
                llm_scope not in {"user", "organisation"}
                or not claimed_concept_id
                or not llm_provider
                or not llm_model
            ):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": (
                                "Model changes require a current user or organisation "
                                "context."
                            ),
                        }
                    ),
                    400,
                )
            llm_scope_concept_id, scope_error = _authorise_active_llm_scope(
                llm_scope,
                claimed_concept_id,
            )
            if scope_error:
                return (
                    jsonify({"status": "error", "message": scope_error}),
                    403,
                )
            llm_model_parameters = normalise_model_parameters_for_storage(
                llm_data.get(MODEL_PARAMETERS_KEY) or llm_data.get("modelParameters"),
                provider=llm_provider,
                model=llm_model,
                include_registry=True,
            )
        elif enabled_entries_requested:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": (
                            "Saving enabled_llms requires active_llm scope and "
                            "concept_id in the same request."
                        ),
                    }
                ),
                400,
            )

        resolved_llm = None
        resolved_enabled_llms = None
        resolved_mutation_authority = None
        resolved_rag_embedder = None
        resolved_rag_llm = None
        resolved_gmail_outbound_rate_limits = None
        prior_global_rag_embedder = resolve_rag_embedder_setting()
        prior_embedder_signature = _build_runtime_component_signature_from_resolution(
            "embedder",
            prior_global_rag_embedder,
        )
        internal_mcp_caps_updated = False
        if "gmail_outbound_rate_limits" in data:
            try:
                resolved_gmail_outbound_rate_limits = (
                    parse_gmail_outbound_rate_limit_settings(
                        data.get("gmail_outbound_rate_limits"),
                        current=get_gmail_outbound_rate_limit_settings(),
                    )
                )
            except ValueError as exc:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": str(exc),
                        }
                    ),
                    400,
                )
            if not set_gmail_outbound_rate_limit_settings(
                resolved_gmail_outbound_rate_limits
            ):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": (
                                "Failed to persist gmail_outbound_rate_limits."
                            ),
                        }
                    ),
                    500,
                )
            current_app.logger.warning(
                "gmail_outbound_rate_limits updated: enabled=%s",
                resolved_gmail_outbound_rate_limits.enabled,
            )
        if "disable_write_tool_conservatism" in data:
            try:
                disabled = bool(data.get("disable_write_tool_conservatism"))
            except Exception:
                disabled = False
            set_disable_write_tool_conservatism(disabled)
            current_app.logger.warning(
                "disable_write_tool_conservatism updated: %s",
                disabled,
            )

        if "require_human_review_for_high_impact_kb_writes" in data:
            try:
                required = bool(
                    data.get("require_human_review_for_high_impact_kb_writes")
                )
            except Exception:
                required = False
            set_require_human_review_for_high_impact_kb_writes(required)
            current_app.logger.warning(
                "require_human_review_for_high_impact_kb_writes updated: %s",
                required,
            )

        if llm_scope and llm_scope_concept_id and llm_provider and llm_model:
            setter_kwargs: dict[str, Any] = {}
            if llm_model_parameters:
                setter_kwargs["model_parameters"] = llm_model_parameters
            if llm_host:
                setter_kwargs["host"] = llm_host
            if enabled_entries_requested:
                setter_kwargs["enabled_entries"] = enabled_entries
            setter = (
                set_user_llm_setting if llm_scope == "user" else set_org_llm_setting
            )
            ok = setter(
                llm_scope_concept_id,
                llm_provider,
                llm_model,
                **setter_kwargs,
            )
            if not ok:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": (
                                "Failed to persist the selected model and enabled "
                                "model pool."
                            ),
                        }
                    ),
                    500,
                )
            resolved_llm = resolve_llm_setting(
                user_concept_id=(llm_scope_concept_id if llm_scope == "user" else None),
                org_concept_id=(
                    llm_scope_concept_id if llm_scope == "organisation" else None
                ),
            )
            if not resolved_llm:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": (
                                "The selected model did not resolve after persistence."
                            ),
                        }
                    ),
                    500,
                )
            current_app.logger.info(
                "Scoped LLM set scope=%s concept=%s provider=%s model=%s",
                llm_scope,
                llm_scope_concept_id,
                llm_provider,
                llm_model,
            )

        if llm_scope and llm_scope_concept_id:
            resolved_enabled_llms = resolve_enabled_llm_settings(
                user_concept_id=(llm_scope_concept_id if llm_scope == "user" else None),
                org_concept_id=(
                    llm_scope_concept_id if llm_scope == "organisation" else None
                ),
            )

        if "mutation_authority" in data and data["mutation_authority"]:
            authority_data = data["mutation_authority"]
            if not isinstance(authority_data, dict):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "mutation_authority must be an object payload.",
                        }
                    ),
                    400,
                )
            scope = authority_data.get("scope")
            concept_id = authority_data.get("concept_id")
            level = authority_data.get("level")
            if scope != "user" or not concept_id or not level:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": (
                                "Saving mutation_authority requires user scope, "
                                "concept_id, and level."
                            ),
                        }
                    ),
                    400,
                )
            ok = set_user_mutation_authority_level(str(concept_id), str(level))
            if not ok:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Failed to save mutation_authority setting.",
                        }
                    ),
                    500,
                )
            resolved_mutation_authority = {
                "scope": "user",
                "concept_id": str(concept_id),
                "level": get_user_mutation_authority_level(str(concept_id)),
            }

        if "openai_api_key_env_var" in data and data["openai_api_key_env_var"]:
            env_var = data["openai_api_key_env_var"]
            set_openai_env_var(env_var)
            current_app.logger.info(
                f"OpenAI API key environment variable set to: {env_var}"
            )

        if "server_default_llm" in data:
            if not set_server_default_llm_setting(data.get("server_default_llm")):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Failed to persist server_default_llm.",
                        }
                    ),
                    500,
                )

        if "rag_embedder" in data:
            if not set_rag_embedder_setting(data.get("rag_embedder")):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Failed to persist rag_embedder.",
                        }
                    ),
                    500,
                )

        if "rag_llm" in data:
            if not set_rag_llm_setting(data.get("rag_llm")):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Failed to persist rag_llm.",
                        }
                    ),
                    500,
                )

        if any(
            key in data for key in ("server_default_llm", "rag_embedder", "rag_llm")
        ):
            _invalidate_cached_rag_runtime_configuration()

        if "fetch_counts_on_load" in data:
            try:
                enabled = bool(data.get("fetch_counts_on_load"))
            except Exception:
                enabled = True
            set_fetch_counts_on_load(enabled)
            current_app.logger.info(f"fetch_counts_on_load set to: {enabled}")

        if "preload_vontology_tree" in data:
            try:
                enabled = bool(data.get("preload_vontology_tree"))
            except Exception:
                enabled = False
            set_preload_vontology_tree(enabled)
            current_app.logger.info("preload_vontology_tree set to: %s", enabled)

        if "disable_remote_ollama_scan" in data:
            try:
                disabled = bool(data.get("disable_remote_ollama_scan"))
            except Exception:
                disabled = False
            set_disable_remote_ollama_scan(disabled)
            current_app.logger.info(f"disable_remote_ollama_scan set to: {disabled}")

        if "internal_mcp_max_tool_invocations" in data:
            raw_invocations = data.get("internal_mcp_max_tool_invocations")
            try:
                parsed_invocations = int(raw_invocations)
            except (TypeError, ValueError):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": (
                                "internal_mcp_max_tool_invocations must be an integer "
                                f"between {INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MIN} and "
                                f"{INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MAX}; got "
                                f"{raw_invocations!r}."
                            ),
                        }
                    ),
                    400,
                )
            if (
                parsed_invocations < INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MIN
                or parsed_invocations > INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MAX
            ):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": (
                                "internal_mcp_max_tool_invocations cannot be more "
                                f"than {INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MAX} "
                                f"(or less than {INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MIN}); "
                                f"attempted {parsed_invocations}."
                            ),
                        }
                    ),
                    400,
                )
            set_internal_mcp_max_tool_invocations(parsed_invocations)
            internal_mcp_caps_updated = True
            current_app.logger.info("internal_mcp_max_tool_invocations updated")

        if "internal_mcp_tool_batch_cap" in data:
            raw_batch = data.get("internal_mcp_tool_batch_cap")
            try:
                parsed_batch = int(raw_batch)
            except (TypeError, ValueError):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": (
                                "internal_mcp_tool_batch_cap must be an integer "
                                f"between {INTERNAL_MCP_TOOL_BATCH_CAP_MIN} and "
                                f"{INTERNAL_MCP_TOOL_BATCH_CAP_MAX}; got "
                                f"{raw_batch!r}."
                            ),
                        }
                    ),
                    400,
                )
            if (
                parsed_batch < INTERNAL_MCP_TOOL_BATCH_CAP_MIN
                or parsed_batch > INTERNAL_MCP_TOOL_BATCH_CAP_MAX
            ):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": (
                                "internal_mcp_tool_batch_cap cannot be more than "
                                f"{INTERNAL_MCP_TOOL_BATCH_CAP_MAX} (or less than "
                                f"{INTERNAL_MCP_TOOL_BATCH_CAP_MIN}); attempted "
                                f"{parsed_batch}."
                            ),
                        }
                    ),
                    400,
                )
            set_internal_mcp_tool_batch_cap(parsed_batch)
            internal_mcp_caps_updated = True
            current_app.logger.info("internal_mcp_tool_batch_cap updated")

        if internal_mcp_caps_updated:
            try:
                orchestrator = current_app.config.get("INTERNAL_MCP_ORCHESTRATOR")
                if orchestrator is not None and hasattr(
                    orchestrator, "configure_execution_caps"
                ):
                    orchestrator.configure_execution_caps(
                        max_tool_invocations=get_internal_mcp_max_tool_invocations(),
                        tool_batch_cap=get_internal_mcp_tool_batch_cap(),
                    )
            except Exception:
                current_app.logger.warning(
                    "Failed to refresh internal MCP orchestrator caps after settings save.",
                    exc_info=True,
                )

        if "show_tool_use_during_thinking" in data:
            try:
                enabled = bool(data.get("show_tool_use_during_thinking"))
            except Exception:
                enabled = True
            set_show_tool_use_during_thinking(enabled)
            current_app.logger.info(
                "show_tool_use_during_thinking updated: %s",
                enabled,
            )

        if "buttonify_model_enabled" in data:
            try:
                enabled = bool(data.get("buttonify_model_enabled"))
            except Exception:
                enabled = True
            set_buttonify_model_enabled(enabled)
            current_app.logger.info("buttonify_model_enabled updated: %s", enabled)

        if "auto_proceed_minimal_imposition_enabled" in data:
            try:
                enabled = bool(data.get("auto_proceed_minimal_imposition_enabled"))
            except Exception:
                enabled = True
            set_auto_proceed_minimal_imposition_enabled(enabled)
            current_app.logger.info(
                "auto_proceed_minimal_imposition_enabled updated: %s",
                enabled,
            )

        # user/org/language fields intentionally ignored (browser-local)
        resolved_rag_embedder = resolve_rag_embedder_setting()
        resolved_rag_llm = resolve_rag_llm_setting()
        current_embedder_signature = _build_runtime_component_signature_from_resolution(
            "embedder",
            resolved_rag_embedder,
        )
        if current_embedder_signature != prior_embedder_signature:
            if current_embedder_signature is None:
                rebuild_detail = (
                    "The RAG embedder changed to an unresolved state, so the "
                    "authoritative workflow capability index was invalidated "
                    "and cannot rebuild until a working embedder is configured."
                )
            else:
                rebuild_detail = (
                    "The RAG embedder changed, so the authoritative workflow "
                    "capability index was invalidated and will rebuild with the "
                    "new embedding configuration. Existing embedding-backed "
                    "namespaces built with the previous embedder are treated as "
                    "incompatible until rebuilt."
                )
            invalidation_result = invalidate_workflow_capability_index(
                reason=rebuild_detail,
                reset_backend_namespace=True,
            )
            prewarm_started = False
            if current_embedder_signature is not None:
                prewarm_started = prewarm_workflow_capability_index(force_refresh=True)
            current_app.logger.info(
                "Workflow capability index refresh requested after RAG embedder "
                "change (invalidated=%s prewarm_started=%s)",
                bool(
                    isinstance(invalidation_result, Mapping)
                    and invalidation_result.get("success")
                ),
                bool(prewarm_started),
            )

        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Settings updated successfully.",
                    "resolved_llm": resolved_llm,
                    "enabled_llms": resolved_enabled_llms,
                    "resolved_mutation_authority": resolved_mutation_authority,
                    "server_default_llm": get_server_default_llm_setting(),
                    "resolved_rag_embedder": resolved_rag_embedder,
                    "resolved_rag_llm": resolved_rag_llm,
                    "gmail_outbound_rate_limits": (
                        resolved_gmail_outbound_rate_limits.as_dict()
                        if resolved_gmail_outbound_rate_limits is not None
                        else None
                    ),
                }
            ),
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
    Body: { provider, model, scope: 'user'|'organisation', concept_id, host? }
    Returns resolved setting for convenience.
    """
    try:
        data = request.get_json(silent=True) or {}
        provider = data.get("provider")
        model = data.get("model")
        scope = data.get("scope")
        concept_id = data.get("concept_id")
        raw_host = data.get("host")
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
        if raw_host is not None and not isinstance(raw_host, str):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "host must be a string when supplied",
                    }
                ),
                400,
            )
        host = str(raw_host or "").strip() or None
        authorised_concept_id, scope_error = _authorise_active_llm_scope(
            scope,
            concept_id,
        )
        if scope_error:
            return jsonify({"status": "error", "message": scope_error}), 403
        concept_id = authorised_concept_id
        assert (
            isinstance(concept_id, str)
            and isinstance(provider, str)
            and isinstance(model, str)
        )
        model_parameters = normalise_model_parameters_for_storage(
            data.get(MODEL_PARAMETERS_KEY) or data.get("modelParameters"),
            provider=provider,
            model=model,
            include_registry=True,
        )
        setter_kwargs: dict[str, Any] = {}
        if model_parameters:
            setter_kwargs["model_parameters"] = model_parameters
        if host:
            setter_kwargs["host"] = host
        setter = set_user_llm_setting if scope == "user" else set_org_llm_setting
        ok = setter(concept_id, provider, model, **setter_kwargs)
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
    """Report existence/source for a server-recognised model API key.

    Browser input cannot turn this endpoint into an arbitrary environment or
    ``.env`` reader, and no credential fragment is returned or logged.
    """

    data = request.get_json(silent=True) or {}
    env_var_name = _normalise_settings_key_check_env_var(data.get("env_var_name"))
    if env_var_name is None:
        return _jsonify_no_store(
            {"error": "Unsupported model API key source."},
            400,
        )

    exists, source = _settings_key_presence(env_var_name)
    current_app.logger.info(
        "Model API key presence checked env_var=%s exists=%s source=%s",
        env_var_name,
        exists,
        source or "none",
    )
    response_data: Dict[str, Any] = {"exists": exists}
    if source:
        response_data["source"] = source
    return _jsonify_no_store(response_data, 200)


def _resolve_settings_api_key(
    env_var_name: str | None,
    *,
    fallback_env_vars: tuple[str, ...] = (),
) -> str | None:
    """Resolve a provider key for Settings probes without exposing it."""

    names: list[str] = []
    for raw_name in (env_var_name, *fallback_env_vars):
        name = str(raw_name or "").strip()
        if name and name not in names:
            names.append(name)
    for name in names:
        value = load_secret_from_env_or_file(name, f"{name}_FILE")
        if value:
            return value
    dotenv_keys = tuple(key for name in names for key in (name, f"{name}_FILE"))
    dotenv_values = read_repo_dotenv_values(dotenv_keys)
    for name in names:
        value = dotenv_values.get(name)
        if value:
            return value
        value = read_secret_file(dotenv_values.get(f"{name}_FILE"))
        if value:
            return value
    return None


_GEMINI_SETTINGS_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
_META_SETTINGS_KEY_ENV_VAR = "META_API_KEY"


def _normalise_settings_key_check_env_var(value: object) -> str | None:
    """Accept only key names selected by server-side model configuration."""

    name = str(value or "").strip()
    if name == "OPENROUTER_API_KEY":
        return name
    if name == _META_SETTINGS_KEY_ENV_VAR:
        return name
    if name in _GEMINI_SETTINGS_KEY_ENV_VARS:
        return name
    try:
        configured_openai_env_var = str(get_openai_env_var() or "").strip()
    except Exception:
        configured_openai_env_var = "OPENAI_API_KEY"
    return name if name and name == configured_openai_env_var else None


def _settings_key_presence(env_var_name: str) -> tuple[bool, str | None]:
    """Return credential presence without returning or logging credential data."""

    candidates = (
        _GEMINI_SETTINGS_KEY_ENV_VARS
        if env_var_name == "GEMINI_API_KEY"
        else (env_var_name,)
    )
    for name in candidates:
        direct = str(os.getenv(name) or "").strip()
        if direct:
            return True, "process"
        if read_secret_file(os.getenv(f"{name}_FILE")):
            return True, "secret_file"

    dotenv_keys = tuple(
        key
        for name in candidates
        for key in (name, f"{name}_FILE")
    )
    dotenv_values = read_repo_dotenv_values(dotenv_keys)
    for name in candidates:
        if dotenv_values.get(name):
            return True, "dotenv"
        if read_secret_file(dotenv_values.get(f"{name}_FILE")):
            return True, "dotenv_secret_file"
    return False, None


def _normalise_gemini_settings_key_env_var(value: object) -> str | None:
    """Accept only the server-supported Gemini key sources from browser input."""

    name = str(value or "").strip()
    return name if name in _GEMINI_SETTINGS_KEY_ENV_VARS else None


def _normalise_meta_settings_key_env_var(value: object) -> str | None:
    """Accept only Meta's canonical provider-specific key source."""

    name = str(value or "").strip()
    return name if name == _META_SETTINGS_KEY_ENV_VAR else None


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


@settings_bp.route("/models/gemini", methods=["GET"])
def get_gemini_models():
    """Return Gemini generate-content models available to the configured key."""

    api_key = _resolve_settings_api_key(
        "GEMINI_API_KEY",
        fallback_env_vars=("GOOGLE_API_KEY",),
    )
    if not api_key:
        return _jsonify_no_store(
            {
                "error": (
                    "Gemini API key not found in GEMINI_API_KEY or legacy "
                    "GOOGLE_API_KEY."
                )
            },
            400,
        )
    try:
        models = GeminiClient(api_key=api_key).list_models()
        if not models:
            return _jsonify_no_store(
                {
                    "error": (
                        "Gemini returned no generate-content models for this API key."
                    )
                },
                400,
            )
        current_app.logger.info("Retrieved %s Gemini models", len(models))
        return _jsonify_no_store(models, 200)
    except Exception as exc:
        current_app.logger.warning(
            "Could not list Gemini models (error_type=%s)",
            type(exc).__name__,
        )
        return _jsonify_no_store(
            {"error": "Could not retrieve Gemini models with the configured key."},
            400,
        )


@settings_bp.route("/models/openrouter", methods=["GET"])
def get_openrouter_models():
    """Return the current OpenRouter catalogue without changing eligibility."""

    api_key = _resolve_settings_api_key("OPENROUTER_API_KEY")
    if not api_key:
        return _jsonify_no_store(
            {"error": "OpenRouter API key was not found in OPENROUTER_API_KEY."},
            400,
        )
    try:
        models = OpenRouterClient(api_key=api_key).list_models()
        if not models:
            return _jsonify_no_store(
                {"error": "OpenRouter returned no models for the configured key."},
                400,
            )
        current_app.logger.info("Retrieved %s OpenRouter models", len(models))
        return _jsonify_no_store(models, 200)
    except Exception as exc:
        current_app.logger.warning(
            "Could not list OpenRouter models (error_type=%s)",
            type(exc).__name__,
        )
        return _jsonify_no_store(
            {"error": "Could not retrieve OpenRouter models with the configured key."},
            400,
        )


@settings_bp.route("/models/meta", methods=["GET"])
def get_meta_models():
    """Return the available subset of Von's fixed Meta Muse catalogue."""

    api_key = _resolve_settings_api_key(_META_SETTINGS_KEY_ENV_VAR)
    if not api_key:
        return _jsonify_no_store(
            {"error": "Meta API key was not found in META_API_KEY."},
            400,
        )
    try:
        models = [
            model
            for model in MetaMuseClient(api_key=api_key).list_models()
            if model in META_MUSE_SUPPORTED_MODELS
        ]
        if not models:
            raise RuntimeError("fixed Meta Muse catalogue is unavailable")
        return _jsonify_no_store(models, 200)
    except Exception as exc:
        current_app.logger.warning(
            "Could not load the supported Meta Muse catalogue (error_type=%s)",
            type(exc).__name__,
        )
        return _jsonify_no_store(
            {"error": "Could not load the supported Meta Muse catalogue."},
            400,
        )


@settings_bp.route("/model_parameters/capabilities", methods=["GET"])
def get_model_parameter_capabilities():
    provider = str(request.args.get("provider") or "").strip().lower()
    model = str(request.args.get("model") or "").strip()
    api_surface = str(request.args.get("api_surface") or "responses").strip()
    include_registry_arg = (
        str(request.args.get("include_registry") or "").strip().lower()
    )
    include_registry = include_registry_arg not in {"0", "false", "no", "off"}
    if not provider or not model:
        return _jsonify_no_store(
            {
                "success": False,
                "error": "provider and model are required",
                "parameters": {},
            },
            400,
        )
    return _jsonify_no_store(
        {
            "success": True,
            **build_model_parameter_capabilities(
                provider=provider,
                model=model,
                api_surface=api_surface or "responses",
                include_registry=include_registry,
            ),
        }
    )


# --- Per-user preference (language & organisation) storage via concept relationships ---

USER_PREF_LANG_PREDICATE = "#V#preferred_language"


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
        actor_id = get_effective_user_concept_id()
        if not actor_id:
            return jsonify({"error": "authenticated_actor_context_required"}), 401
        normalised_actor = actor_id if actor_id.startswith("#") else f"#V#{actor_id}"
        normalised_subject = (
            user_concept_id
            if user_concept_id.startswith("#")
            else f"#V#{user_concept_id}"
        )
        if normalised_subject != normalised_actor:
            return jsonify({"error": "user_preferences_subject_mismatch"}), 403
        from ...services.concept_service import get_concept_by_concept_id
        from ...services.text_value_service import get_texts_for_concept

        concept = get_concept_by_concept_id(concept_id=user_concept_id)
        if not concept:
            return jsonify({"error": "User concept not found"}), 404
        rel = _normalize_relationships(concept.get("relationships", {}))
        lang_raw = rel.get(USER_PREF_LANG_PREDICATE)

        def first_val(v):
            if isinstance(v, list):
                return v[0] if v else None
            return v if isinstance(v, str) and v else None

        preferred_language = first_val(lang_raw)
        if preferred_language is None:
            language_rows = get_texts_for_concept(
                normalised_subject,
                predicate=USER_PREF_LANG_PREDICATE,
                limit=1,
                recent_first=True,
            )
            if language_rows:
                preferred_language = language_rows[0].get("text")
        # Legacy member_of_organisation describes membership, not preference.
        # Never choose the first membership as a landing context.
        from ...services.login_organisation_preference_service import initialise_login_context
        organisation_concept_id = (
            initialise_login_context(session)["organisation_concept_id"]
            if session.get("user_email") and session.get("user_concept_id") else None
        )
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
    """Update only the authenticated user's non-authority preferences."""
    try:
        data = request.get_json(silent=True) or {}
        actor_id = get_effective_user_concept_id()
        if not actor_id:
            return jsonify({"error": "authenticated_actor_context_required"}), 401
        normalised_actor = actor_id if actor_id.startswith("#") else f"#V#{actor_id}"
        normalised_subject = (
            user_concept_id
            if user_concept_id.startswith("#")
            else f"#V#{user_concept_id}"
        )
        if normalised_subject != normalised_actor:
            return jsonify({"error": "user_preferences_subject_mismatch"}), 403
        if "organisation_concept_id" in data:
            return (
                jsonify(
                    {
                        "error": "organisation_membership_requires_dedicated_lifecycle",
                        "message": (
                            "Organisation membership is authority-bearing and cannot "
                            "be changed through user preferences."
                        ),
                    }
                ),
                400,
            )
        preferred_language = data.get("preferred_language")
        from ...services.concept_service import get_concept_by_concept_id
        from ...services.ontology_mutation_command_service import (
            execute_governed_ontology_method,
        )
        from ...services.text_value_service import upsert_singleton_text_relation

        concept = get_concept_by_concept_id(concept_id=user_concept_id)
        if not concept:
            return jsonify({"error": "User concept not found"}), 404
        desired_lang = (
            preferred_language.strip() if isinstance(preferred_language, str) else None
        )
        desired_lang = desired_lang or None
        governed: dict[str, Any] | None = None
        if desired_lang:
            preference_context = {"preference": "preferred_language"}
            preference_provenance = {
                "source": "user_preferences",
                "actor_concept_id": normalised_actor,
            }
            governed = execute_governed_ontology_method(
                method_name="upsert_singleton_text_relation",
                arguments={
                    "concept_id": normalised_subject,
                    "predicate": USER_PREF_LANG_PREDICATE,
                    "text": desired_lang,
                    "language": "en-NZ",
                    "context": preference_context,
                    "provenance": preference_provenance,
                    "request_id": data.get("request_id"),
                },
                mutate=lambda: upsert_singleton_text_relation(
                    subject_concept_id=normalised_subject,
                    predicate=USER_PREF_LANG_PREDICATE,
                    text=desired_lang,
                    lang="en-NZ",
                    context=preference_context,
                    provenance=preference_provenance,
                ),
            )
            if governed.get("success") is False:
                status = (
                    503
                    if governed.get("error_code")
                    in {
                        "ontology_mutation_receipt_store_unavailable",
                        "ontology_mutation_receipt_finalisation_failed",
                        "canonical_read_back_failed",
                    }
                    else 400
                )
                return jsonify(governed), status

        return (
            jsonify(
                {
                    "status": "success",
                    "user_concept_id": user_concept_id,
                    "preferred_language": desired_lang,
                    "organisation_concept_id": None,
                    "authority_receipt": (
                        governed.get("authority_receipt") if governed else None
                    ),
                    "canonical_read_back": (
                        governed.get("canonical_read_back") if governed else None
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


@settings_bp.route("/recommendation_profile/<path:user_concept_id>", methods=["GET"])
def get_recommendation_profile(user_concept_id: str):
    """Return a user's paper recommendation profile and derived context."""

    try:
        if not user_concept_id:
            return jsonify({"error": "Missing user_concept_id"}), 400
        if not _can_access_user_scoped_profile(user_concept_id):
            return jsonify({"error": "Forbidden"}), 403
        payload = load_paper_recommendation_profile(
            user_concept_id=user_concept_id,
            create_if_missing=True,
        )
        return jsonify(payload), 200
    except ConceptNotFoundError:
        return jsonify({"error": "User concept not found"}), 404
    except Exception as e:
        current_app.logger.error(
            "Error getting recommendation profile for %s: %s",
            user_concept_id,
            e,
            exc_info=True,
        )
        return jsonify({"error": "Failed to retrieve recommendation profile"}), 500


@settings_bp.route("/recommendation_profile/<path:user_concept_id>", methods=["POST"])
def set_recommendation_profile(user_concept_id: str):
    """Upsert a user's paper recommendation profile."""

    try:
        if not user_concept_id:
            return jsonify({"error": "Missing user_concept_id"}), 400
        if not _can_access_user_scoped_profile(user_concept_id):
            return jsonify({"error": "Forbidden"}), 403

        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "JSON object body required"}), 400

        payload = upsert_paper_recommendation_profile(
            user_concept_id=user_concept_id,
            recommendation_profile=data,
            provenance={
                "source": "settings_routes.recommendation_profile",
            },
            context={
                "path": "settings_routes.recommendation_profile",
            },
        )
        payload["recommendation_refresh"] = request_paper_recommendation_refresh(
            target_subject_concept_ids=[user_concept_id],
            trigger_source="settings_routes.recommendation_profile",
            user_id=user_concept_id,
            event_payload={
                "subject_concept_id": user_concept_id,
                "source": "settings_routes.recommendation_profile",
            },
        )
        return jsonify(payload), 200
    except ConceptNotFoundError:
        return jsonify({"error": "User concept not found"}), 404
    except Exception as e:
        current_app.logger.error(
            "Error setting recommendation profile for %s: %s",
            user_concept_id,
            e,
            exc_info=True,
        )
        return jsonify({"error": "Failed to save recommendation profile"}), 500


@settings_bp.route("/recommendation_review/<path:user_concept_id>", methods=["POST"])
def get_recommendation_review(user_concept_id: str):
    """Return a bounded paper recommendation review payload for one user."""

    try:
        if not user_concept_id:
            return jsonify({"error": "Missing user_concept_id"}), 400
        if not _can_access_user_scoped_profile(user_concept_id):
            return jsonify({"error": "Forbidden"}), 403

        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "JSON object body required"}), 400

        raw_candidate_ids = data.get("candidate_paper_concept_ids")
        if raw_candidate_ids is None:
            raw_candidate_ids = data.get("paper_concept_ids")

        payload = build_paper_recommendation_review(
            user_concept_id=user_concept_id,
            candidate_paper_concept_ids=raw_candidate_ids,
            candidate_limit=data.get("candidate_limit"),
            include_all_candidates=bool(data.get("include_all_candidates", True)),
            trigger_source=str(data.get("trigger_source") or "").strip() or None,
        )
        return _jsonify_no_store(payload), 200
    except ConceptNotFoundError:
        return jsonify({"error": "User concept not found"}), 404
    except Exception as e:
        current_app.logger.error(
            "Error building recommendation review for %s: %s",
            user_concept_id,
            e,
            exc_info=True,
        )
        return jsonify({"error": "Failed to build recommendation review"}), 500


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


@settings_bp.route("/gemini/verify", methods=["POST"])
def verify_gemini_api_key():
    """Verify a configured Gemini key by listing its generate-content models."""

    payload = request.get_json(silent=True) or {}
    api_key_env_var = _normalise_gemini_settings_key_env_var(
        payload.get("api_key_env_var") or "GEMINI_API_KEY"
    )
    if not api_key_env_var:
        return _jsonify_no_store(
            {
                "success": False,
                "error": (
                    "Gemini key source must be GEMINI_API_KEY or GOOGLE_API_KEY."
                ),
            },
            400,
        )
    api_key = _resolve_settings_api_key(
        api_key_env_var,
        fallback_env_vars=("GOOGLE_API_KEY",)
        if api_key_env_var == "GEMINI_API_KEY"
        else (),
    )
    if not api_key:
        return _jsonify_no_store(
            {
                "success": False,
                "error": f"Gemini API key was not found in {api_key_env_var}.",
            },
            400,
        )

    current_app.logger.info(
        "Attempting to verify Gemini API key from environment variable %s",
        api_key_env_var,
    )
    try:
        models = GeminiClient(api_key=api_key).list_models()
        if not models:
            return _jsonify_no_store(
                {
                    "success": False,
                    "error": (
                        "Gemini returned no generate-content models for this API key."
                    ),
                },
                400,
            )
        current_app.logger.info("Successfully retrieved %s Gemini models", len(models))
        return _jsonify_no_store({"success": True, "models": models}, 200)
    except Exception as exc:
        current_app.logger.warning(
            "Gemini API key verification failed (error_type=%s)",
            type(exc).__name__,
        )
        return _jsonify_no_store(
            {
                "success": False,
                "error": "Gemini API verification failed with the configured key.",
            },
            400,
        )


@settings_bp.route("/meta/verify", methods=["POST"])
def verify_meta_api_key():
    """Verify the Meta key with catalogue discovery, without model generation."""

    payload = request.get_json(silent=True) or {}
    api_key_env_var = _normalise_meta_settings_key_env_var(
        payload.get("api_key_env_var") or _META_SETTINGS_KEY_ENV_VAR
    )
    if not api_key_env_var:
        return _jsonify_no_store(
            {
                "success": False,
                "error": "Meta key source must be META_API_KEY.",
            },
            400,
        )
    api_key = _resolve_settings_api_key(api_key_env_var)
    if not api_key:
        return _jsonify_no_store(
            {
                "success": False,
                "error": "Meta API key was not found in META_API_KEY.",
            },
            400,
        )

    try:
        models = [
            model
            for model in MetaMuseClient(api_key=api_key).list_models()
            if model in META_MUSE_SUPPORTED_MODELS
        ]
        if not models:
            raise RuntimeError("fixed Meta Muse catalogue is unavailable")
        return _jsonify_no_store(
            {
                "success": True,
                "models": models,
                "verification_kind": "authenticated_model_catalogue",
            },
            200,
        )
    except Exception as exc:
        current_app.logger.warning(
            "Meta API key configuration check failed (error_type=%s)",
            type(exc).__name__,
        )
        return _jsonify_no_store(
            {
                "success": False,
                "error": "Could not load the supported Meta Muse catalogue.",
            },
            400,
        )


@settings_bp.route("/openrouter/verify", methods=["POST"])
def verify_openrouter_api_key():
    """Verify the fixed OpenRouter key source by listing its current catalogue."""

    payload = request.get_json(silent=True) or {}
    if str(payload.get("api_key_env_var") or "OPENROUTER_API_KEY").strip() != "OPENROUTER_API_KEY":
        return _jsonify_no_store(
            {
                "success": False,
                "error": "OpenRouter key source must be OPENROUTER_API_KEY.",
            },
            400,
        )
    api_key = _resolve_settings_api_key("OPENROUTER_API_KEY")
    if not api_key:
        return _jsonify_no_store(
            {
                "success": False,
                "error": "OpenRouter API key was not found in OPENROUTER_API_KEY.",
            },
            400,
        )
    try:
        models = OpenRouterClient(api_key=api_key).list_models()
        if not models:
            raise RuntimeError("empty model catalogue")
        return _jsonify_no_store({"success": True, "models": models}, 200)
    except Exception as exc:
        current_app.logger.warning(
            "OpenRouter API key verification failed (error_type=%s)",
            type(exc).__name__,
        )
        return _jsonify_no_store(
            {
                "success": False,
                "error": "OpenRouter API verification failed with the configured key.",
            },
            400,
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
        if not _is_admin_or_owner_session():
            return (
                jsonify(
                    {
                        "success": False,
                        "error": (
                            "Admin or owner privileges are required to update "
                            "shared Ollama hosts."
                        ),
                    }
                ),
                403,
            )
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
    bypass_cache = request.args.get("nocache", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cached_models = _read_cached_ollama_models(bypass_cache=bypass_cache)
    if isinstance(cached_models, list):
        return jsonify({"success": True, "models": cached_models}), 200

    try:
        from ...languagemodels.llm_interface import OllamaClient  # inline import

        # Get models from all hosts
        all_models = OllamaClient.list_models_from_all_hosts()
        if isinstance(all_models, list):
            _write_cached_ollama_models(all_models)

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


@settings_bp.route("/organisations", methods=["GET"])
def get_available_organisations():
    """API endpoint to retrieve all available organisation entities for selection."""
    try:
        if ConceptsRepository.collection() is None:
            current_app.logger.warning(
                "Organisation selector unavailable: concepts collection could not be reached."
            )
            response = _jsonify_no_store(
                {
                    "error": "Organisations are temporarily unavailable. Please retry shortly.",
                    "retryable": True,
                    "reason": "concepts_collection_unavailable",
                    "retry_after_seconds": 5,
                },
                503,
            )
            response.headers["Retry-After"] = "5"
            return response

        # Get all Von user organisation entities using the specific concept
        concepts, total_count = list_concepts(
            concept_id="#V#von_user_organisation",  # Use the specific Von user organisation concept ID
            sort_by="name",
            sort_order=1,  # Ascending
            per_page=100,  # Get a reasonable number of organisations
        )

        # Use the concepts directly since we're only querying one specific type
        unique_concepts = concepts

        # Filter and format for dropdown - use simple name extraction without per-concept DB calls
        organisation_options = []
        for concept in unique_concepts:
            from ...vontology.utils_vontology import (
                get_concept_display_name_with_names_fallback,
            )

            # Use get_concept_display_name_with_names_fallback directly on existing data
            # without expensive enrichment (avoids O(n) DB calls)
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
                    "available": True,
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
    return (
        jsonify(
            {
                "success": False,
                "error_code": "ontology_bulk_export_not_governed",
                "error": (
                    "Whole-ontology HTTP export is unavailable because it cannot "
                    "preserve actor-scoped visibility."
                ),
            }
        ),
        410,
    )

    # Unreachable legacy implementation retained temporarily for compatibility
    # archaeology; it must not be re-enabled without a scope-partitioned export.
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
    return (
        jsonify(
            {
                "success": False,
                "error_code": "ontology_bulk_repair_not_governed",
                "error": (
                    "Whole-ontology HTTP repair is unavailable until it has an "
                    "exact global-authority plan, receipt, and canonical read-back."
                ),
            }
        ),
        410,
    )

    # Unreachable legacy implementation retained temporarily for compatibility
    # archaeology.
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
    return (
        jsonify(
            {
                "success": False,
                "error_code": "ontology_bulk_import_not_governed",
                "error": (
                    "Whole-ontology HTTP import is unavailable until it has an "
                    "exact global-authority plan, receipt, and canonical read-back."
                ),
            }
        ),
        410,
    )

    # Unreachable legacy implementation retained temporarily for compatibility
    # archaeology.
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
        # Dry run flag: query param dry_run=1 or header X-Import-Dry-Run=1
        dry_run_flag = request.args.get("dry_run") in ("1", "true", "True") or (
            request.headers.get("X-Import-Dry-Run") in ("1", "true", "True")
        )
        incremental = req_mode == "incremental"
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
                except Exception:
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
                error_summary += " Showing first 10 errors."

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
            # Safety guard: dropping the concepts collection is destructive.
            # By default, refuse to do this on VON_DB_NAME=von_db.
            try:
                assert_destructive_db_operation_allowed("settings_import_apply_drop")
            except RuntimeError as guard_exc:
                current_app.logger.error(str(guard_exc))
                return jsonify({"error": str(guard_exc)}), 400

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


@settings_bp.route("/openai/test_model", methods=["POST"])
def test_openai_model():
    """Probe whether a selected OpenAI model is actually usable for this key."""

    def _classify_openai_probe_exception(exc: Exception) -> tuple[str, str]:
        try:
            import openai  # type: ignore

            if isinstance(exc, openai.AuthenticationError):
                return "authentication_error", f"OpenAI authentication failed: {exc}"
            if isinstance(exc, openai.PermissionDeniedError):
                return (
                    "permission_denied",
                    f"OpenAI access was denied for this model: {exc}",
                )
            if isinstance(exc, openai.NotFoundError):
                return (
                    "model_not_found",
                    f"The selected OpenAI model was not found: {exc}",
                )
            if isinstance(exc, openai.RateLimitError):
                body = getattr(exc, "body", None)
                code = body.get("code") if isinstance(body, dict) else None
                if code == "insufficient_quota":
                    return (
                        "quota_exhausted",
                        f"OpenAI quota exhausted (insufficient_quota): {exc}",
                    )
                return "rate_limited", f"OpenAI rate limit exceeded: {exc}"
            if isinstance(exc, openai.APIConnectionError):
                return "connection_error", f"Could not reach the OpenAI API: {exc}"
            if isinstance(exc, openai.BadRequestError):
                return "bad_request", f"OpenAI rejected the selected model probe: {exc}"
            if isinstance(exc, openai.APIError):
                return (
                    "api_error",
                    f"OpenAI API error while testing the selected model: {exc}",
                )
        except Exception:
            pass
        return "unexpected_error", f"Unexpected OpenAI model probe failure: {exc}"

    payload = request.get_json(silent=True) or {}
    api_key_env_var = str(
        payload.get("api_key_env_var") or get_openai_env_var() or ""
    ).strip()
    requested_model = str(payload.get("model") or "").strip()

    if not api_key_env_var:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "failure_kind": "missing_key_env_var",
                "reason": "OpenAI API key environment variable is not configured.",
            },
            400,
        )
    if not requested_model:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "failure_kind": "missing_model",
                "reason": "No premium model is selected.",
            },
            400,
        )

    try:
        from ...languagemodels.llm_interface import resolve_openai_model_name

        resolved_model = resolve_openai_model_name(requested_model) or requested_model
        try:
            assert_model_execution_allowed(
                provider="openai",
                model=resolved_model,
            )
        except ModelExecutionEligibilityError as exc:
            return _jsonify_no_store(
                {
                    "success": False,
                    "usable": False,
                    "model": exc.model or resolved_model,
                    "failure_kind": exc.failure_kind,
                    "reason": str(exc),
                },
                403,
            )
        model_parameters = normalise_model_parameters_for_storage(
            payload.get(MODEL_PARAMETERS_KEY) or payload.get("modelParameters"),
            provider="openai",
            model=resolved_model,
            api_surface="responses",
            include_registry=True,
        )
        parameter_capabilities = build_model_parameter_capabilities(
            provider="openai",
            model=resolved_model,
            api_surface="responses",
            include_registry=True,
        )
        client = OpenAIClient(api_key_env_var=api_key_env_var)

        try:
            available_models = client.list_models()
        except Exception as exc:
            available_models = []
            current_app.logger.warning(
                "[openai_test_model] Failed to list OpenAI models before probe: %s",
                exc,
            )

        if available_models and resolved_model not in available_models:
            return _jsonify_no_store(
                {
                    "success": True,
                    "usable": False,
                    "model": resolved_model,
                    MODEL_PARAMETERS_KEY: model_parameters,
                    "model_parameter_capabilities": parameter_capabilities,
                    "failure_kind": "model_unavailable",
                    "reason": (
                        f"{resolved_model} is not present in the models available to "
                        "this API key."
                    ),
                }
            )

        api_surface = "responses"
        fallback_used = False
        responses_failure_kind = None
        responses_failure_reason = None
        try:
            response_kwargs = openai_responses_kwargs_from_model_parameters(
                model_parameters,
                model=resolved_model,
            )
            client.client.responses.create(
                model=resolved_model,
                input="Reply exactly with OK.",
                max_output_tokens=resolve_openai_responses_max_output_tokens(
                    model_parameters
                ),
                **response_kwargs,
            )
        except Exception as responses_exc:
            responses_failure_kind, responses_failure_reason = (
                _classify_openai_probe_exception(responses_exc)
            )
            try:
                client.client.chat.completions.create(
                    model=resolved_model,
                    messages=[
                        {"role": "system", "content": "Reply exactly with OK."},
                        {"role": "user", "content": "OK"},
                    ],
                    max_completion_tokens=8,
                )
                api_surface = "chat_completions"
                fallback_used = True
            except Exception as completion_exc:
                failure_kind, reason = _classify_openai_probe_exception(completion_exc)
                current_app.logger.warning(
                    "[openai_test_model] Probe failed for %s after responses/chat fallback. "
                    "responses_error=%s completion_error=%s",
                    resolved_model,
                    responses_exc,
                    completion_exc,
                )
                return _jsonify_no_store(
                    {
                        "success": True,
                        "usable": False,
                        "model": resolved_model,
                        MODEL_PARAMETERS_KEY: model_parameters,
                        "model_parameter_capabilities": parameter_capabilities,
                        "failure_kind": failure_kind,
                        "reason": reason,
                    }
                )

        return _jsonify_no_store(
            {
                "success": True,
                "usable": True,
                "model": resolved_model,
                MODEL_PARAMETERS_KEY: model_parameters,
                "model_parameter_capabilities": parameter_capabilities,
                "failure_kind": None,
                "reason": "The selected premium model completed a live probe successfully.",
                "api_surface": api_surface,
                "fallback_used": fallback_used,
                "responses_failure_kind": responses_failure_kind,
                "responses_failure_reason": responses_failure_reason,
            }
        )
    except Exception as exc:
        failure_kind, reason = _classify_openai_probe_exception(exc)
        current_app.logger.warning(
            "[openai_test_model] Unexpected failure while testing %s: %s",
            requested_model,
            exc,
            exc_info=True,
        )
        return _jsonify_no_store(
            {
                "success": True,
                "usable": False,
                "model": requested_model,
                "failure_kind": failure_kind,
                "reason": reason,
            }
        )


def _classify_gemini_probe_exception(exc: Exception) -> tuple[str, str]:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code = None
    lowered = str(exc).strip().lower()
    if status_code == 401 or "unauth" in lowered or "api key not valid" in lowered:
        return "authentication_error", "Gemini authentication failed."
    if status_code == 403 or "permission" in lowered or "forbidden" in lowered:
        return "permission_denied", "Gemini access was denied for this model."
    if status_code == 404 or "not found" in lowered:
        return "model_not_found", "The selected Gemini model was not found."
    if status_code == 429 or "rate limit" in lowered or "resource_exhausted" in lowered:
        if "quota" in lowered or "resource_exhausted" in lowered:
            return "quota_exhausted", "Gemini quota is exhausted."
        return "rate_limited", "Gemini rate limit exceeded."
    if status_code == 400 or "invalid argument" in lowered:
        return "bad_request", "Gemini rejected the selected model probe."
    if "connect" in lowered or "network" in lowered or "dns" in lowered:
        return "connection_error", "Could not reach the Gemini API."
    return "unexpected_error", "Unexpected Gemini model probe failure."


def _classify_meta_probe_exception(exc: Exception) -> tuple[str, str]:
    """Map Meta failures to bounded, secret-free Settings diagnostics."""

    raw_tokens = [
        getattr(exc, "failure_kind", None),
        getattr(exc, "provider_error_code", None),
        getattr(exc, "code", None),
    ]
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        raw_tokens.extend(body.get(key) for key in ("code", "type", "status"))
        nested_error = body.get("error")
        if isinstance(nested_error, Mapping):
            raw_tokens.extend(
                nested_error.get(key) for key in ("code", "type", "status")
            )
    tokens = {
        str(value).strip().lower().replace("-", "_").replace(" ", "_")
        for value in raw_tokens
        if value is not None and str(value).strip()
    }
    lowered = str(exc).strip().lower()
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code = None

    if (
        "billing_not_configured" in tokens
        or "billing_not_configured" in lowered
        or "billing not configured" in lowered
        or status_code == 402
    ):
        return (
            "billing_not_configured",
            "Meta API billing is not configured for this key.",
        )
    if tokens.intersection({"authentication_error", "authentication_failed"}) or (
        status_code == 401
    ):
        return "authentication_error", "Meta API authentication failed."
    if "permission_denied" in tokens or status_code == 403:
        return "permission_denied", "Meta API access was denied for this model."
    if "model_not_found" in tokens or status_code == 404:
        return "model_not_found", "The selected Meta Muse model was not found."
    if "quota_exhausted" in tokens:
        return "quota_exhausted", "Meta API quota is exhausted."
    if "rate_limited" in tokens or status_code == 429:
        return "rate_limited", "Meta API rate limit exceeded."
    if "bad_request" in tokens or status_code == 400:
        return "bad_request", "Meta rejected the selected model probe."
    if "connection_error" in tokens or any(
        marker in lowered for marker in ("connect", "network", "dns")
    ):
        return "connection_error", "Could not reach the Meta API."
    return "unexpected_error", "Unexpected Meta Muse model probe failure."


def _gemini_probe_api_surface(client: object, model: str) -> str:
    metadata = getattr(client, "last_response_metadata", None)
    observed = (
        str(metadata.get("api_surface") or "").strip()
        if isinstance(metadata, dict)
        else ""
    )
    if observed in {"interactions", "gemini_generate_content"}:
        return observed
    canonical_model = str(model or "").strip().lower()
    if canonical_model.startswith("models/"):
        canonical_model = canonical_model.split("/", 1)[1]
    return (
        "interactions"
        if canonical_model == "gemini-3.7-flash"
        or canonical_model.startswith("gemini-3.7-flash-")
        else "gemini_generate_content"
    )


@settings_bp.route("/gemini/test_model", methods=["POST"])
def test_gemini_model():
    """Probe whether an allowed Gemini model is usable with the configured key."""

    payload = request.get_json(silent=True) or {}
    api_key_env_var = _normalise_gemini_settings_key_env_var(
        payload.get("api_key_env_var") or "GEMINI_API_KEY"
    )
    requested_model = str(payload.get("model") or "").strip()
    if not api_key_env_var:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "failure_kind": "invalid_key_env_var",
                "reason": (
                    "Gemini key source must be GEMINI_API_KEY or GOOGLE_API_KEY."
                ),
            },
            400,
        )
    if not requested_model:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "failure_kind": "missing_model",
                "reason": "No Gemini model is selected.",
            },
            400,
        )

    try:
        assert_model_execution_allowed(provider="gemini", model=requested_model)
    except ModelExecutionEligibilityError as exc:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "model": exc.model or requested_model,
                "failure_kind": exc.failure_kind,
                "reason": str(exc),
            },
            403,
        )

    api_key = _resolve_settings_api_key(
        api_key_env_var,
        fallback_env_vars=("GOOGLE_API_KEY",)
        if api_key_env_var == "GEMINI_API_KEY"
        else (),
    )
    if not api_key:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "model": requested_model,
                "failure_kind": "missing_api_key",
                "reason": f"Gemini API key was not found in {api_key_env_var}.",
            },
            400,
        )

    model_parameters = normalise_model_parameters_for_storage(
        payload.get(MODEL_PARAMETERS_KEY) or payload.get("modelParameters"),
        provider="gemini",
        model=requested_model,
        api_surface="interactions",
        include_registry=True,
    )
    parameter_capabilities = build_model_parameter_capabilities(
        provider="gemini",
        model=requested_model,
        api_surface="interactions",
        include_registry=True,
    )
    try:
        client = GeminiClient(api_key=api_key, default_model=requested_model)
        try:
            available_models = client.list_models()
        except Exception as exc:
            available_models = []
            current_app.logger.warning(
                "[gemini_test_model] Failed to list Gemini models before probe: %s",
                exc,
            )
        if available_models and requested_model not in available_models:
            return _jsonify_no_store(
                {
                    "success": True,
                    "usable": False,
                    "model": requested_model,
                    MODEL_PARAMETERS_KEY: model_parameters,
                    "model_parameter_capabilities": parameter_capabilities,
                    "failure_kind": "model_unavailable",
                    "reason": (
                        f"{requested_model} is not present in the models available "
                        "to this Gemini API key."
                    ),
                }
            )
        client.generate(
            "Reply exactly with OK.",
            model=requested_model,
            llm_params=model_parameters or None,
        )
        api_surface = _gemini_probe_api_surface(client, requested_model)
        return _jsonify_no_store(
            {
                "success": True,
                "usable": True,
                "model": requested_model,
                MODEL_PARAMETERS_KEY: model_parameters,
                "model_parameter_capabilities": parameter_capabilities,
                "failure_kind": None,
                "reason": (
                    "The selected Gemini model completed a live probe successfully."
                ),
                "api_surface": api_surface,
            }
        )
    except Exception as exc:
        failure_kind, reason = _classify_gemini_probe_exception(exc)
        current_app.logger.warning(
            "[gemini_test_model] Probe failed for %s (error_type=%s)",
            requested_model,
            type(exc).__name__,
        )
        return _jsonify_no_store(
            {
                "success": True,
                "usable": False,
                "model": requested_model,
                MODEL_PARAMETERS_KEY: model_parameters,
                "model_parameter_capabilities": parameter_capabilities,
                "failure_kind": failure_kind,
                "reason": reason,
            }
        )


@settings_bp.route("/meta/test_model", methods=["POST"])
def test_meta_model():
    """Run a paid probe only for an exactly allowed Meta Muse model."""

    payload = request.get_json(silent=True) or {}
    api_key_env_var = _normalise_meta_settings_key_env_var(
        payload.get("api_key_env_var") or _META_SETTINGS_KEY_ENV_VAR
    )
    requested_model = str(payload.get("model") or "").strip()
    if not api_key_env_var:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "failure_kind": "invalid_key_env_var",
                "reason": "Meta key source must be META_API_KEY.",
            },
            400,
        )
    if not requested_model:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "failure_kind": "missing_model",
                "reason": "No Meta Muse model is selected.",
            },
            400,
        )
    if requested_model not in META_MUSE_SUPPORTED_MODELS:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "model": requested_model,
                "failure_kind": "model_unavailable",
                "reason": "The selected Meta Muse model is not supported by Von.",
            },
            400,
        )

    actor_user_concept_id, actor_source = (
        get_effective_user_concept_id_with_source()
    )
    if (
        not actor_user_concept_id
        or actor_source == LEGACY_IDENTITY_HEADER_ACTOR_SOURCE
    ):
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "model": requested_model,
                "failure_kind": "authenticated_actor_context_required",
                "reason": (
                    "An authenticated actor context is required before testing "
                    "a paid Meta model."
                ),
            },
            401,
        )
    actor_org_concept_id = get_effective_organisation_concept_id()
    try:
        assert_model_execution_allowed(
            provider="meta",
            model=requested_model,
            user_concept_id=actor_user_concept_id,
            org_concept_id=actor_org_concept_id,
            allow_ambient_actor_scope=False,
        )
    except ModelExecutionEligibilityError as exc:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "model": exc.model or requested_model,
                "failure_kind": exc.failure_kind,
                "reason": str(exc),
            },
            403,
        )

    # Credential resolution and client construction occur only after the exact
    # actor-scoped provider/model eligibility check above.
    api_key = _resolve_settings_api_key(api_key_env_var)
    if not api_key:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "model": requested_model,
                "failure_kind": "missing_api_key",
                "reason": "Meta API key was not found in META_API_KEY.",
            },
            400,
        )

    model_parameters = normalise_model_parameters_for_storage(
        payload.get(MODEL_PARAMETERS_KEY) or payload.get("modelParameters"),
        provider="meta",
        model=requested_model,
        api_surface="responses",
        include_registry=True,
    )
    parameter_capabilities = build_model_parameter_capabilities(
        provider="meta",
        model=requested_model,
        api_surface="responses",
        include_registry=True,
    )
    try:
        client = MetaMuseClient(
            api_key=api_key,
            default_model=requested_model,
            model_execution_user_concept_id=actor_user_concept_id,
            model_execution_org_concept_id=actor_org_concept_id,
            model_execution_actor_scope_bound=True,
        )
        client.generate(
            "Reply exactly with OK.",
            model=requested_model,
            llm_params=model_parameters or None,
        )
        metadata = getattr(client, "last_response_metadata", None)
        observed_surface = (
            str(metadata.get("api_surface") or "").strip()
            if isinstance(metadata, Mapping)
            else ""
        )
        return _jsonify_no_store(
            {
                "success": True,
                "usable": True,
                "model": requested_model,
                MODEL_PARAMETERS_KEY: model_parameters,
                "model_parameter_capabilities": parameter_capabilities,
                "failure_kind": None,
                "reason": "The selected Meta Muse model completed a live probe successfully.",
                "api_surface": observed_surface or "responses",
            }
        )
    except Exception as exc:
        failure_kind, reason = _classify_meta_probe_exception(exc)
        current_app.logger.warning(
            "[meta_test_model] Probe failed for %s (error_type=%s)",
            requested_model,
            type(exc).__name__,
        )
        return _jsonify_no_store(
            {
                "success": True,
                "usable": False,
                "model": requested_model,
                MODEL_PARAMETERS_KEY: model_parameters,
                "model_parameter_capabilities": parameter_capabilities,
                "failure_kind": failure_kind,
                "reason": reason,
            }
        )


@settings_bp.route("/openrouter/test_model", methods=["POST"])
def test_openrouter_model():
    """Probe an already-allowed OpenRouter model with the conservative profile."""

    payload = request.get_json(silent=True) or {}
    api_key_env_var = str(
        payload.get("api_key_env_var") or "OPENROUTER_API_KEY"
    ).strip()
    requested_model = str(payload.get("model") or "").strip()
    if api_key_env_var != "OPENROUTER_API_KEY":
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "failure_kind": "invalid_key_env_var",
                "reason": "OpenRouter key source must be OPENROUTER_API_KEY.",
            },
            400,
        )
    if not requested_model:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "failure_kind": "missing_model",
                "reason": "No OpenRouter model is selected.",
            },
            400,
        )
    try:
        assert_model_execution_allowed(provider="openrouter", model=requested_model)
    except ModelExecutionEligibilityError as exc:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "model": exc.model or requested_model,
                "failure_kind": exc.failure_kind,
                "reason": str(exc),
            },
            403,
        )

    api_key = _resolve_settings_api_key("OPENROUTER_API_KEY")
    if not api_key:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "model": requested_model,
                "failure_kind": "missing_api_key",
                "reason": "OpenRouter API key was not found in OPENROUTER_API_KEY.",
            },
            400,
        )
    model_parameters = normalise_model_parameters_for_storage(
        payload.get(MODEL_PARAMETERS_KEY) or payload.get("modelParameters"),
        provider="openrouter",
        model=requested_model,
        api_surface="chat_completions",
        include_registry=True,
    )
    parameter_capabilities = build_model_parameter_capabilities(
        provider="openrouter",
        model=requested_model,
        api_surface="chat_completions",
        include_registry=True,
    )
    try:
        client = OpenRouterClient(api_key=api_key)
        available_models = client.list_models()
        if available_models and requested_model not in available_models:
            return _jsonify_no_store(
                {
                    "success": True,
                    "usable": False,
                    "model": requested_model,
                    MODEL_PARAMETERS_KEY: model_parameters,
                    "model_parameter_capabilities": parameter_capabilities,
                    "failure_kind": "model_unavailable",
                    "reason": (
                        f"{requested_model} is not present in the current "
                        "OpenRouter model catalogue."
                    ),
                }
            )
        client.generate(
            "Reply exactly with OK.",
            model=requested_model,
            llm_params=model_parameters or None,
        )
        return _jsonify_no_store(
            {
                "success": True,
                "usable": True,
                "model": requested_model,
                MODEL_PARAMETERS_KEY: model_parameters,
                "model_parameter_capabilities": parameter_capabilities,
                "failure_kind": None,
                "reason": (
                    "The selected OpenRouter model completed a live "
                    "privacy-constrained probe successfully."
                ),
                "api_surface": "chat_completions",
                "privacy_profile": {
                    "zdr": True,
                    "data_collection": "deny",
                    "require_parameters": True,
                },
            }
        )
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        lowered = str(exc).lower()
        failure_kind = "unexpected_error"
        reason = "Unexpected OpenRouter model probe failure."
        if status_code == 401 or "auth" in lowered or "api key" in lowered:
            failure_kind, reason = "authentication_error", "OpenRouter authentication failed."
        elif status_code == 403 or "permission" in lowered:
            failure_kind, reason = "permission_denied", "OpenRouter access was denied for this model."
        elif status_code == 404 or "not found" in lowered:
            failure_kind, reason = "model_not_found", "The selected OpenRouter model was not found."
        elif status_code == 429 or "rate limit" in lowered or "quota" in lowered:
            failure_kind, reason = "rate_limited", "OpenRouter rate or quota limit was reached."
        elif "connect" in lowered or "network" in lowered:
            failure_kind, reason = "connection_error", "Could not reach OpenRouter."
        current_app.logger.warning(
            "[openrouter_test_model] Probe failed for %s (error_type=%s)",
            requested_model,
            type(exc).__name__,
        )
        return _jsonify_no_store(
            {
                "success": True,
                "usable": False,
                "model": requested_model,
                MODEL_PARAMETERS_KEY: model_parameters,
                "model_parameter_capabilities": parameter_capabilities,
                "failure_kind": failure_kind,
                "reason": reason,
            }
        )


@settings_bp.route("/ollama/test_model", methods=["POST"])
def test_ollama_model():
    """Probe whether a selected Ollama model is reachable and usable."""
    payload = request.get_json(silent=True) or {}
    requested_model = str(payload.get("model") or "").strip()
    host_url = str(payload.get("host_url") or "").strip() or None

    if not requested_model:
        return _jsonify_no_store(
            {
                "success": False,
                "usable": False,
                "failure_kind": "missing_model",
                "reason": "No Ollama model is selected.",
            },
            400,
        )

    try:
        from ...languagemodels.llm_interface import OllamaClient

        client = OllamaClient(host=host_url)
        available_models = client.list_models()
        if available_models and requested_model not in available_models:
            return _jsonify_no_store(
                {
                    "success": True,
                    "usable": False,
                    "model": requested_model,
                    "host_url": client.host,
                    "failure_kind": "model_unavailable",
                    "reason": (
                        f"{requested_model} is not present in the models available "
                        f"from {client.host}."
                    ),
                }
            )
        if not available_models:
            return _jsonify_no_store(
                {
                    "success": True,
                    "usable": False,
                    "model": requested_model,
                    "host_url": client.host,
                    "failure_kind": "no_models_available",
                    "reason": f"No Ollama models were listed from {client.host}.",
                }
            )

        client.generate(
            "Reply exactly with OK.",
            model=requested_model,
            llm_params={"num_predict": 8},
        )
        return _jsonify_no_store(
            {
                "success": True,
                "usable": True,
                "model": requested_model,
                "host_url": client.host,
                "failure_kind": None,
                "reason": "The selected Ollama model completed a live probe successfully.",
            }
        )
    except Exception as exc:
        current_app.logger.warning(
            "[ollama_test_model] Probe failed for %s: %s",
            requested_model,
            exc,
            exc_info=True,
        )
        return _jsonify_no_store(
            {
                "success": True,
                "usable": False,
                "model": requested_model,
                "host_url": host_url,
                "failure_kind": "probe_failed",
                "reason": f"Ollama model probe failed: {exc}",
            }
        )


@settings_bp.route("/model_timeout", methods=["GET"])
def get_model_timeout_route():
    """Return the saved LLM call timeout for a specific model, or all overrides."""
    provider = str(request.args.get("provider") or "").strip().lower()
    model = str(request.args.get("model") or "").strip()
    overrides = get_model_llm_timeout_overrides()
    if provider and model:
        key = _make_model_timeout_key(provider, model)
        timeout_sec = overrides.get(key)
    else:
        timeout_sec = None
    return jsonify({"timeout_sec": timeout_sec, "overrides": overrides})


@settings_bp.route("/model_timeout", methods=["POST"])
def set_model_timeout_route():
    """Save or clear the per-model LLM call timeout override."""
    body = request.get_json(force=True) or {}
    provider = str(body.get("provider") or "").strip().lower()
    model = str(body.get("model") or "").strip()
    if not provider or not model:
        return jsonify({"error": "provider and model are required"}), 400
    timeout_sec = body.get("timeout_sec")
    try:
        timeout_float = None if timeout_sec is None else float(timeout_sec)
    except (TypeError, ValueError):
        return jsonify({"error": "timeout_sec must be a number or null"}), 400
    success = set_model_llm_timeout(provider, model, timeout_float)
    return jsonify({"success": success})


# To make this blueprint usable, it needs to be registered in your main Flask app,
# typically in src/workflows/von/main.py or wherever your Flask app is initialized.
# Example:
# from src.backend.server.routes.settings_routes import settings_bp
# app.register_blueprint(settings_bp)
