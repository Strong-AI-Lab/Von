import sys
import sys
import os

# Adjust path to ensure project root and src are included for imports BEFORE any backend.* imports
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
src_path = os.path.join(project_root, "src")
for p in (project_root, src_path):
    if p not in sys.path and os.path.isdir(p):
        sys.path.insert(0, p)
if os.path.isdir(src_path) and src_path not in sys.path:
    sys.path.insert(0, src_path)
# print("\n".join(sys.path)) # Keep for debugging if needed

# Load repo-root .env early so feature flags like VON_INTERNAL_MCP_ENABLE take effect
# when running via run.ps1/Flask (JVNAUTOSCI-xxx).
try:  # pragma: no cover - exercised implicitly in dev runs
    from dotenv import load_dotenv

    env_path = os.path.join(project_root, ".env")
    if os.path.isfile(env_path):
        load_dotenv(env_path, override=False)
except Exception:
    pass

from flask import (
    Flask,
    jsonify,
    redirect,
    url_for,
    request,
)  # Added request for shutdown endpoint
import os

# --- Updated Typing Imports ---
from typing import Optional, List, Dict, Any, Callable  # Use List and Dict
import logging  # Import logging

# Import Blueprints
from .routes.von_routes import von_bp  # relative import
from .routes.vontology_routes import vontology_bp, get_instance_counts
from .routes.concept_routes import concept_bp
from .routes.settings_routes import settings_bp
from .routes.elicitation_routes import elicitation_bp
from .routes.annotations_routes import annotations_bp
from .routes.admin_routes import admin_bp
from .routes.auth_routes import auth_bp
from .routes.agent_gmail_oauth_routes import agent_gmail_oauth_bp
from .routes.predicate_routes import predicate_bp
from ..db.connection_manager import (
    ensure_monitor_started,
    get_db,
)  # start background DB monitor
from ..services.annotation_extraction_service import (
    prompt_concept_health_status,
    PROMPT_CONCEPT_ID,
)

# Define a version string
APP_VERSION = "v20250421_1015_backend"  # Updated version


def create_flask_app(
    # --- Updated Type Hints ---
    list_models_func: Callable[
        [], List[str]
    ],  # Expects a function returning list of strings
    generate_func: Callable[
        [str, Optional[List[Dict[str, str]]], Optional[str]], str
    ],  # Matches LLMInterface.generate signature (prompt, context, model)
    static_folder_path: str = os.path.join(
        project_root, "src", "frontend", "web", "von_interface", "static"
    ),
    template_folder_path: str = os.path.join(
        project_root, "src", "frontend", "web", "von_interface", "templates"
    ),
) -> Flask:
    """Create and configure a Flask application using dependency injection and Blueprints."""
    # Use absolute paths for static and template folders based on project root
    app = Flask(
        __name__, static_folder=static_folder_path, template_folder=template_folder_path
    )  # Template folder set here is default, Blueprint can override

    # --- Configuration Setup ---
    # Set secret key for session management (required for Google OAuth)
    app.secret_key = os.environ.get(
        "FLASK_SECRET_KEY", "von-dev-secret-key-change-in-production"
    )

    # Enable template auto-reload in development
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # --- Use Generic Config Keys ---
    app.config["GENERATE_FUNC"] = generate_func  # Store the generate function
    app.config["LIST_MODELS_FUNC"] = list_models_func  # Store the list models function

    # Initialize storage (use setdefault for safety)
    app.config.setdefault("PEOPLE", [])
    app.config.setdefault("CONTEXT", [])
    # Consider making the default model configurable or deriving it from the client
    app.config.setdefault(
        "MODEL", "granite3.3:2b"
    )  # Keep default for now, but be aware

    # --- Register Blueprints ---
    app.register_blueprint(von_bp, url_prefix="/von")  # MODIFIED
    app.register_blueprint(
        vontology_bp, url_prefix="/vontology/api/vontology"
    )  # MODIFIED: Full prefix
    app.register_blueprint(
        concept_bp, url_prefix="/api/concepts"
    )  # Register the new concept blueprint with prefix
    app.register_blueprint(
        settings_bp, url_prefix="/api/settings"
    )  # Register the new settings blueprint with prefix
    app.register_blueprint(
        elicitation_bp, url_prefix="/api/elicitation"
    )  # Register the new elicitation blueprint with prefix
    app.register_blueprint(annotations_bp, url_prefix="/api/annotations")
    app.register_blueprint(predicate_bp)  # Already has /api/predicates prefix
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(
        auth_bp, url_prefix="/von"
    )  # Register the new auth blueprint with /von prefix to match Google OAuth config
    app.register_blueprint(
        agent_gmail_oauth_bp, url_prefix="/von"
    )  # Agent Gmail OAuth endpoints (separate from user login)
    # ---------------------------

    # --- Log App Version ---
    # Use app.logger if available, otherwise print
    try:
        app.logger.setLevel(logging.INFO)  # Ensure INFO level is logged
        app.logger.info(f"--- Flask App Initializing - Version: {APP_VERSION} ---")
    except Exception:
        print(
            f"--- Flask App Initializing - Version: {APP_VERSION} ---"
        )  # Fallback print

    # Internal MCP gateway bootstrap (disabled by default until flag flipped)
    gateway_instance = None
    try:
        from ..integrations.internal_mcp import (
            InternalMCPGateway,
            InternalMCPTransport,
            InternalMCPChatOrchestrator,
            build_default_catalogue,
        )

        internal_mcp_enabled = os.getenv("VON_INTERNAL_MCP_ENABLE", "0").lower() in {
            "1",
            "true",
        }
        catalogue = build_default_catalogue()
        transport = InternalMCPTransport()
        gateway_instance = InternalMCPGateway(
            catalogue=catalogue,
            transport=transport,
            enabled=internal_mcp_enabled,
        )
        method_count = len(catalogue.list_methods())
        if internal_mcp_enabled:
            app.logger.info(
                "[mcp_gateway] Enabled with %d registered methods.", method_count
            )
        else:
            app.logger.info(
                "[mcp_gateway] Initialised (disabled). Set VON_INTERNAL_MCP_ENABLE=1 to activate. Methods=%d",
                method_count,
            )
    except Exception as exc:  # pragma: no cover - defensive bootstrap
        try:
            app.logger.warning("[mcp_gateway] Failed to initialise: %s", exc)
        except Exception:
            pass
        gateway_instance = None
    app.config["INTERNAL_MCP_GATEWAY"] = gateway_instance
    orchestrator_instance = None
    if gateway_instance is not None:
        try:
            orchestrator_instance = InternalMCPChatOrchestrator(
                gateway=gateway_instance,
                logger=app.logger.getChild("mcp_orchestrator") if app.logger else None,
                max_tool_invocations=8,  # JVNAUTOSCI-699: Allow complex chained workflows
                default_gmail_profile=os.getenv("VON_GMAIL_DEFAULT_PROFILE") or None,
            )
        except Exception as exc:  # pragma: no cover - defensive bootstrap
            try:
                app.logger.warning("[mcp_orchestrator] Failed to initialise: %s", exc)
            except Exception:
                pass
            orchestrator_instance = None
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator_instance

    # Start DB connection monitor (idempotent)
    try:
        ensure_monitor_started()
    except Exception as _e:  # pragma: no cover
        try:
            app.logger.warning("Failed to start DB monitor: %s", _e)
        except Exception:
            pass

    # Prompt concept health logging
    try:
        pc_status = prompt_concept_health_status()
        if not pc_status.get("available"):
            app.logger.warning(
                "Prompt concept %s missing or incomplete (source_field=%s, error=%s). LLM annotation requests will fail until resolved.",
                PROMPT_CONCEPT_ID,
                pc_status.get("source_field"),
                pc_status.get("error"),
            )
        else:
            app.logger.info(
                "Prompt concept %s OK (source_field=%s)",
                PROMPT_CONCEPT_ID,
                pc_status.get("source_field"),
            )
    except Exception as e:  # pragma: no cover - defensive
        try:
            app.logger.warning("Prompt concept health check unexpected error: %s", e)
        except Exception:
            pass

    # (Prewarm logic moved below route registrations to avoid early first-request state.)

    # --- Root/Health Check Endpoint ---
    @app.route("/")
    def root_redirect():
        """Redirect root to Von interface."""
        return redirect(url_for("von.serve_page"))

    # Capture process start time once for uptime reporting
    from datetime import datetime, timezone as _tz

    app.config["SERVER_START_TIME"] = app.config.get(
        "SERVER_START_TIME"
    ) or datetime.now(_tz.utc).isoformat().replace("+00:00", "Z")

    @app.route("/health")
    def health_check():
        """Health check endpoint with pid and start time for process manager UI."""
        # Get local IP address
        import socket

        local_ip = None
        try:
            # Create a socket to get the local IP (doesn't actually connect)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception as e:
            print(f"[health] Local IP detection (method 1) failed: {e}")
            try:
                local_ip = socket.gethostbyname(socket.gethostname())
                print(f"[health] Local IP from hostname: {local_ip}")
            except Exception as e2:
                print(f"[health] Local IP detection (method 2) failed: {e2}")
                local_ip = "127.0.0.1"

        # Get public IP address (cached in app config to avoid repeated external calls)
        public_ip = app.config.get("PUBLIC_IP_ADDRESS")
        if not public_ip:
            try:
                import urllib.request

                with urllib.request.urlopen(
                    "https://api.ipify.org?format=text", timeout=3
                ) as response:
                    public_ip = response.read().decode("utf-8").strip()
                    app.config["PUBLIC_IP_ADDRESS"] = public_ip  # Cache it
                    print(f"[health] Public IP fetched and cached: {public_ip}")
            except Exception as e:
                print(f"[health] Public IP fetch failed: {e}")
                public_ip = None

        # RAG Indexing Status
        rag_pending_count = 0
        try:
            db = get_db()
            if db is not None:
                # Use string "pending" to avoid importing model class and potential circular deps
                rag_pending_count = db["interaction_sessions"].count_documents(
                    {"indexing_status": "pending"}
                )
        except Exception as e:
            print(f"[health] RAG status check failed: {e}")
            rag_pending_count = -1  # Indicate error

        return jsonify(
            status="healthy",
            version=APP_VERSION,
            pid=os.getpid(),
            start_time=app.config["SERVER_START_TIME"],
            local_ip=local_ip,
            public_ip=public_ip,
            rag_pending_count=rag_pending_count,
        )

    @app.route("/admin/rag_status")
    def rag_status():
        """Return detailed RAG indexing status counts for footer display.

        Response:
          {
            "total": int,
            "indexed": int,
            "pending": int,
            "failed": int,
            "skipped": int
          }
        """
        try:
            db = get_db()
            if db is None:
                return jsonify(error="db_unavailable"), 503
            sessions_coll = db["interaction_sessions"]
            interactions_coll = (
                db["interactions"]
                if "interactions" in db.list_collection_names()
                else None
            )
            # Optional namespace filter: expects sessions to store a 'namespace' field
            ns = request.args.get("namespace")
            sess_filter = {"namespace": ns} if ns else {}
            total_sessions = sessions_coll.count_documents({})
            indexed = sessions_coll.count_documents(
                {"indexing_status": "indexed", **sess_filter}
            )
            pending = sessions_coll.count_documents(
                {"indexing_status": "pending", **sess_filter}
            )
            failed = sessions_coll.count_documents(
                {"indexing_status": "failed", **sess_filter}
            )
            skipped = sessions_coll.count_documents(
                {"indexing_status": "skipped", **sess_filter}
            )
            # Eligible heuristic: sessions with a non-empty 'history' or any interaction with text
            eligible_sessions = sessions_coll.count_documents(
                {
                    "$or": [
                        {"history": {"$exists": True, "$ne": []}},
                        {"summary": {"$exists": True, "$type": "string", "$ne": ""}},
                    ],
                    **sess_filter,
                }
            )
            total_interactions = 0
            eligible_interactions = 0
            if interactions_coll is not None:
                total_interactions = interactions_coll.count_documents({})
                eligible_interactions = interactions_coll.count_documents(
                    {
                        "$or": [
                            {"text": {"$exists": True, "$type": "string", "$ne": ""}},
                            {
                                "message": {
                                    "$exists": True,
                                    "$type": "string",
                                    "$ne": "",
                                }
                            },
                        ]
                    }
                )
            return jsonify(
                {
                    "total": total_sessions,
                    "indexed": indexed,
                    "pending": pending,
                    "failed": failed,
                    "skipped": skipped,
                    "sessions": total_sessions,
                    "interactions": total_interactions,
                    "eligible_sessions": eligible_sessions,
                    "eligible_interactions": eligible_interactions,
                    "namespace": ns,
                }
            )
        except Exception as e:
            return jsonify(error="unexpected", detail=str(e)), 500

    @app.route("/admin/rag_integrity", methods=["POST"])
    def admin_rag_integrity():
        db = get_db()
        if db is None:
            return jsonify({"error": "db_unavailable"}), 503
        sessions_coll = db["interaction_sessions"]
        interactions_coll = (
            db["interactions"] if "interactions" in db.list_collection_names() else None
        )
        ns = request.args.get("namespace")
        sess_filter = {"namespace": ns} if ns else {}
        result = {
            "sessions": sessions_coll.count_documents(sess_filter or {}),
            "interactions": (
                interactions_coll.count_documents({})
                if interactions_coll is not None
                else 0
            ),
            "indexed": sessions_coll.count_documents(
                {"indexing_status": "indexed", **sess_filter}
            ),
            "pending": sessions_coll.count_documents(
                {"indexing_status": "pending", **sess_filter}
            ),
            "failed": sessions_coll.count_documents(
                {"indexing_status": "failed", **sess_filter}
            ),
            "skipped": sessions_coll.count_documents(
                {"indexing_status": "skipped", **sess_filter}
            ),
            "eligible_sessions": sessions_coll.count_documents(
                {
                    "$or": [
                        {"history": {"$exists": True, "$ne": []}},
                        {"summary": {"$exists": True, "$type": "string", "$ne": ""}},
                    ],
                    **sess_filter,
                }
            ),
            "eligible_interactions": 0,
            "anomalies": [],
            "namespace": ns,
        }
        if interactions_coll is not None:
            result["eligible_interactions"] = interactions_coll.count_documents(
                {
                    "$or": [
                        {"text": {"$exists": True, "$type": "string", "$ne": ""}},
                        {"message": {"$exists": True, "$type": "string", "$ne": ""}},
                    ]
                }
            )
            # Anomaly example: interactions with text but session missing pending/indexed
            sample_with_text = interactions_coll.find(
                {
                    "$or": [
                        {"text": {"$exists": True, "$type": "string", "$ne": ""}},
                        {"message": {"$exists": True, "$type": "string", "$ne": ""}},
                    ]
                },
                {"session_id": 1},
            ).limit(25)
            orphan_sessions = []
            for it in sample_with_text:
                sid = it.get("session_id")
                if sid is None:
                    continue
                sess = sessions_coll.find_one({"_id": sid}, {"indexing_status": 1})
                if not sess or sess.get("indexing_status") not in (
                    "pending",
                    "indexed",
                    "failed",
                    "skipped",
                ):
                    orphan_sessions.append(str(sid))
            if orphan_sessions:
                result["anomalies"].append(
                    {"type": "orphan_text_interactions", "session_ids": orphan_sessions}
                )
        return jsonify(result)

    @app.route("/admin/rag_sync", methods=["POST"])
    def admin_rag_sync():
        from ..services.rag_sync_service import sync_to_chat_store

        payload = request.get_json(silent=True) or {}
        namespace = payload.get("namespace")
        try:
            result = sync_to_chat_store(namespace=namespace)
            return jsonify(result)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/diag")
    def diagnostics():
        """Lightweight diagnostics endpoint exposing runtime/process/cache info."""
        # Lazy imports to avoid overhead if unused
        import json, threading, time

        rss_mb = None
        thread_count = None
        try:
            import psutil  # type: ignore

            p = psutil.Process()
            rss_mb = round(p.memory_info().rss / (1024 * 1024), 2)
            thread_count = p.num_threads()
        except Exception:
            try:
                import tracemalloc

                if tracemalloc.is_tracing():
                    snap = tracemalloc.take_snapshot()
                    rss_mb = round(
                        sum([s.size for s in snap.statistics("filename")])
                        / (1024 * 1024),
                        2,
                    )
            except Exception:
                pass
            thread_count = len(threading.enumerate())

        # Uptime
        from datetime import datetime, timezone as _tz

        try:
            start_iso = app.config.get("SERVER_START_TIME")
            start_dt = (
                datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                if start_iso
                else None
            )
            uptime_sec = (
                (datetime.now(_tz.utc) - start_dt).total_seconds() if start_dt else None
            )
        except Exception:
            uptime_sec = None

        # Gather model cache stats if available
        model_cache_summary = []
        try:
            from ...languagemodels.llm_interface import _MODEL_CACHE, _MODEL_CACHE_TTL  # type: ignore

            now_ts = time.time()
            for key, meta in _MODEL_CACHE.items():
                models = meta.get("models") or []
                model_cache_summary.append(
                    {
                        "key": key,
                        "count": len(models),
                        "age_sec": round(now_ts - meta.get("fetched_at", 0), 1),
                        "build_time_sec": round(meta.get("build_time", 0), 3),
                        "source": meta.get("source"),
                        "error": meta.get("error"),
                        "hit_count": meta.get("hit_count", 0),
                        "ttl_sec": _MODEL_CACHE_TTL,
                    }
                )
        except Exception:
            pass

        # Vontology tree cache stats (improved node counting)
        tree_cache = {}
        try:
            from .routes.vontology_routes import _TREE_CACHE  # type: ignore

            now_ts = time.time()
            ttl_env = os.getenv("VONTOLOGY_TREE_TTL")
            if _TREE_CACHE:
                rec = _TREE_CACHE.get("Thing")
                if rec and isinstance(rec, tuple) and len(rec) >= 5:
                    ts, payload, build_secs, alloc_kb, hits = rec
                    # Traverse to count nodes if payload matches expected shape { 'tree': [ root_node ] }
                    node_count = None
                    try:
                        if isinstance(payload, dict) and isinstance(
                            payload.get("tree"), list
                        ):
                            stack = list(payload["tree"])
                            c = 0
                            while stack:
                                n = stack.pop()
                                c += 1
                                ch = n.get("children") if isinstance(n, dict) else None
                                if isinstance(ch, list):
                                    stack.extend(ch)
                            node_count = c
                    except Exception:
                        pass
                    tree_cache = {
                        "cached": True,
                        "age_sec": round(now_ts - ts, 1),
                        "build_time_sec": round(build_secs, 3),
                        "alloc_kb": round(alloc_kb, 1),
                        "ttl_sec": (
                            int(ttl_env) if ttl_env and ttl_env.isdigit() else None
                        ),
                        "node_count": node_count,
                        "hits": hits,
                    }
                else:
                    tree_cache = {"cached": True}
            else:
                tree_cache = {"cached": False}
        except Exception:
            tree_cache = {"cached": False, "error": "unavailable"}

        # Instance counts cache stats (size and TTL)
        counts_cache = {}
        try:
            from .routes.vontology_routes import _INSTANCE_COUNTS_CACHE  # type: ignore

            ttl_env = os.getenv("VONTOLOGY_COUNTS_TTL")
            size = (
                len(_INSTANCE_COUNTS_CACHE)
                if isinstance(_INSTANCE_COUNTS_CACHE, dict)
                else None
            )
            counts_cache = {
                "size": size,
                "ttl_sec": int(ttl_env) if ttl_env and ttl_env.isdigit() else None,
            }
        except Exception:
            counts_cache = {"error": "unavailable"}

        salient_cache = {}
        try:
            from .routes.vontology_routes import _SALIENT_CACHE, _SALIENT_STATS  # type: ignore

            cache_obj = _SALIENT_CACHE if isinstance(_SALIENT_CACHE, dict) else {}
            cache_size = len(cache_obj) if isinstance(cache_obj, dict) else None
            split_entries = 0
            sample_scope = None
            if isinstance(cache_obj, dict):
                split_entries = sum(
                    1
                    for key in cache_obj.keys()
                    if isinstance(key, str) and key.endswith("|split")
                )
                for value in cache_obj.values():
                    if not isinstance(value, tuple) or len(value) < 4:
                        continue
                    scope_payload = value[3]
                    if not isinstance(scope_payload, dict):
                        continue
                    raw_map = scope_payload.get("raw_scope_map") or {}
                    if not isinstance(raw_map, dict):
                        raw_map = {}
                    sample_scope = {
                        "predicates_by_scope_keys": list(
                            (scope_payload.get("predicates_by_scope") or {}).keys()
                        ),
                        "predicate_origin_keys": list(
                            (scope_payload.get("predicate_origins") or {}).keys()
                        ),
                        "raw_scope_counts": {
                            k: len(v) if isinstance(v, (list, set, tuple)) else 0
                            for k, v in raw_map.items()
                        },
                    }
                    break
            salient_cache = {
                "size": cache_size,
                "split_entries": split_entries,
                "stats": (
                    dict(_SALIENT_STATS) if isinstance(_SALIENT_STATS, dict) else None
                ),
                "ttl_sec": 30,
                "sample_scope_payload": sample_scope,
            }
        except Exception:
            salient_cache = {"error": "unavailable"}

        # Entity counts stats (lightweight)
        entity_counts_stats = {}
        try:
            from .routes.vontology_routes import _ENTITY_COUNTS_STATS  # type: ignore

            # Provide a shallow copy and round ema
            ema = _ENTITY_COUNTS_STATS.get("ema_ms")
            entity_counts_stats = {
                "total_calls": int(_ENTITY_COUNTS_STATS.get("total_calls") or 0),
                "last_ts": _ENTITY_COUNTS_STATS.get("last_ts"),
                "ema_ms": None if ema is None else round(float(ema), 1),
            }
        except Exception:
            entity_counts_stats = {"error": "unavailable"}

        # Mongo connection diagnostics (best-effort; avoid leaking credentials)
        try:
            from ..db.mongo_client import get_effective_mongo_uri, is_using_fallback_uri  # type: ignore

            _eff_uri = get_effective_mongo_uri()
            # Redact credentials if present
            redacted_uri = _eff_uri
            if "://" in redacted_uri and "@" in redacted_uri:
                scheme, rest = redacted_uri.split("://", 1)
                if "@" in rest:
                    creds, hostpart = rest.split("@", 1)
                    # Keep only username (if any) and mask password
                    if ":" in creds:
                        user = creds.split(":", 1)[0]
                        redacted_uri = f"{scheme}://{user}:***@{hostpart}"
                    else:
                        redacted_uri = f"{scheme}://***@{hostpart}"
            mongo_diag = {
                "effective_mongo_uri": redacted_uri,
                "using_fallback": is_using_fallback_uri(),
            }
        except Exception:
            mongo_diag = {"effective_mongo_uri": None, "using_fallback": None}

        # Access control / session visibility diagnostics
        session_user = None
        effective_user = None
        header_user = None
        user_visibility_sample = None
        try:
            from flask import session as _session

            session_user = _session.get("user_concept_id")
            from ..security.access_control import get_effective_user_concept_id  # type: ignore

            # Peek raw headers for fallback diagnostic (do not validate here)
            try:
                header_user = request.headers.get(
                    "X-User-Concept-ID"
                ) or request.headers.get("X-User-Client-ID")
            except Exception:
                header_user = None
            effective_user = get_effective_user_concept_id()
            # Sample: count how many user-specific concepts would be visible for current effective user
            try:
                from ..db.mongo_client import get_concepts_collection  # type: ignore

                coll = get_concepts_collection()
                if coll is not None:
                    total_user_specific = coll.count_documents(
                        {"relationships.specific_to_user": {"$exists": True, "$ne": []}}
                    )
                    if effective_user:
                        visible_user_specific = coll.count_documents(
                            {"relationships.specific_to_user": effective_user}
                        )
                    else:
                        visible_user_specific = 0
                    user_visibility_sample = {
                        "total_user_specific": int(total_user_specific),
                        "visible_for_effective_user": int(visible_user_specific),
                    }
            except Exception:
                pass
        except Exception:
            pass

        # GUID coverage stats
        guid_stats = {}
        try:
            from ..db.mongo_client import get_concepts_collection

            coll = get_concepts_collection()
            if coll is not None:
                total_concepts = coll.count_documents({})
                with_guid = coll.count_documents({"guid": {"$exists": True}})
                guid_stats = {
                    "total_concepts": total_concepts,
                    "with_guid": with_guid,
                    "coverage_percent": (
                        round((with_guid / total_concepts * 100), 1)
                        if total_concepts > 0
                        else 0
                    ),
                }
        except Exception:
            guid_stats = {"error": "unavailable"}

        search_proxy_stats = {}
        try:
            from ..integrations.internal_mcp import search_proxy_mcp as _search_proxy_mod  # type: ignore

            proxy_instance = getattr(_search_proxy_mod, "_proxy_instance", None)
            if proxy_instance is None:
                search_proxy_stats = {"initialised": False}
            else:
                stats = proxy_instance.get_stats()
                search_proxy_stats = {
                    "initialised": True,
                    "call_count": stats.get("call_count"),
                    "error_count": stats.get("error_count"),
                    "command": getattr(proxy_instance._config, "command", None),
                }
        except Exception as exc:  # pragma: no cover - defensive
            search_proxy_stats = {"error": str(exc)}

        diag = {
            "status": "ok",
            "version": APP_VERSION,
            "pid": os.getpid(),
            "rss_mb": rss_mb,
            "thread_count": thread_count,
            "uptime_sec": uptime_sec,
            "session_user_concept_id": session_user,
            "effective_user_concept_id": effective_user,
            "header_user_concept_id": header_user,
            "user_visibility_sample": user_visibility_sample,
            "model_cache": model_cache_summary,
            "tree_cache": tree_cache,
            "instance_counts_cache": counts_cache,
            "salient_cache": salient_cache,
            "entity_counts": entity_counts_stats,
            "guid_stats": guid_stats,
            "search_proxy": search_proxy_stats,
            "python_version": sys.version.split()[0],
        }
        diag["mongo"] = mongo_diag
        gateway = app.config.get("INTERNAL_MCP_GATEWAY")
        if gateway is None:
            diag["internal_mcp_gateway"] = {"configured": False}
        else:
            try:
                diag["internal_mcp_gateway"] = gateway.get_diagnostics()
            except Exception as exc:  # pragma: no cover - defensive
                diag["internal_mcp_gateway"] = {
                    "configured": True,
                    "error": str(exc),
                }
        # Annotation / phrase cache stats (best-effort)
        try:
            from ..services.annotation_extraction_service import phrase_cache_stats, fallback_nonjson_metric_stats  # type: ignore

            diag["annotations"] = {
                "phrase_cache": phrase_cache_stats(),
                "fallback_nonjson": fallback_nonjson_metric_stats(),
            }
        except Exception:
            pass
        # Import / orphan metrics (JVNAUTOSCI-584) best-effort inclusion
        try:
            from .routes.settings_routes import _IMPORT_METRICS, _ORPHAN_SCAN_METRICS  # type: ignore

            if _IMPORT_METRICS:
                # Provide shallow copy to avoid mutation by caller
                diag["import_metrics"] = dict(_IMPORT_METRICS)
            if _ORPHAN_SCAN_METRICS:
                diag["orphan_metrics"] = dict(_ORPHAN_SCAN_METRICS)
        except Exception:
            pass
        # Append accessor stats (best-effort)
        try:
            from ...vontology.utils_vontology import _ACCESSOR_STATS  # type: ignore

            # Copy to avoid mutation during serialization
            diag["accessor_stats"] = {k: dict(v) for k, v in _ACCESSOR_STATS.items()}
        except Exception:
            pass
        return jsonify(diag)

    @app.route("/api/system/db_status")
    def db_status():
        """Return current database connection status (fallback vs Atlas) for UI indicator.

        Response schema:
          {
            "using_fallback": bool | null,
            "atlas_detected": bool | null,
            "effective_host": str | null,   # redacted host:port only
            "timestamp": iso8601
          }
        """
        from datetime import datetime, timezone as _tz

        using_fallback = None
        atlas_detected = None
        host_only = None
        try:
            from ..db.mongo_client import get_effective_mongo_uri, is_using_fallback_uri  # type: ignore

            eff = get_effective_mongo_uri()
            using_fallback = is_using_fallback_uri()
            if eff:
                # strip credentials and keep host portion (after @ and before first /)
                try:
                    after_scheme = eff.split("://", 1)[1] if "://" in eff else eff
                    if "@" in after_scheme:
                        after_scheme = after_scheme.split("@", 1)[1]
                    host_only = after_scheme.split("/", 1)[0]
                except Exception:
                    host_only = None
                atlas_detected = "mongodb.net" in eff.lower()
        except Exception:
            pass
        return jsonify(
            using_fallback=using_fallback,
            atlas_detected=atlas_detected,
            effective_host=host_only,
            timestamp=datetime.now(_tz.utc).isoformat().replace("+00:00", "Z"),
        )

    # --- API Endpoint for Version ---
    @app.route("/api/version")
    def get_version():
        """API endpoint to get the version of the app."""
        return jsonify(version=APP_VERSION)

    # --- Graceful Shutdown Endpoint (admin) ---
    @app.route("/admin/shutdown", methods=["POST"])
    def admin_shutdown():
        """Gracefully shut down the Flask development server.

        Requires header X-Admin-Token matching env VON_ADMIN_TOKEN.
        If token missing or mismatch returns 401.
        Intended for controlled stop via run.ps1 script.
        """
        expected = os.environ.get("VON_ADMIN_TOKEN")
        provided = request.headers.get("X-Admin-Token")
        if not expected:
            return jsonify(success=False, error="shutdown_disabled"), 403
        if provided != expected:
            return jsonify(success=False, error="unauthorized"), 401
        func = request.environ.get("werkzeug.server.shutdown")
        if func is None:
            # Fallback for production servers like Waitress: schedule hard exit
            try:
                import threading, time, os as _os

                def delayed_exit():
                    time.sleep(0.2)
                    _os._exit(0)

                threading.Thread(target=delayed_exit, daemon=True).start()
                return jsonify(success=True, status="shutting_down_fallback")
            except Exception as e:
                return (
                    jsonify(success=False, error="shutdown_failed", detail=str(e)),
                    500,
                )
        else:
            func()
            return jsonify(success=True, status="shutting_down")

    # Backwards-compatible alias for tests and older clients that call the shorter '/api/vontology' path.
    @app.route("/api/vontology/instance_counts")
    def alias_instance_counts():
        # Delegate to the blueprint handler
        return get_instance_counts()

    # Optional background prewarm (model list + tree) to reduce first-request latency.
    # Placed AFTER all routes to ensure decorators complete before any internal
    # test_client calls. Skipped when running under pytest (env PYTEST_CURRENT_TEST) or
    # when app.testing already true, or when disabled via env/config.
    def _start_prewarm():  # local closure
        try:
            if os.getenv("VON_PREWARM_DISABLE") in (
                "1",
                "true",
                "TRUE",
                "True",
            ) or app.config.get("PREWARM_DISABLE"):
                app.logger.info("Prewarm disabled by VON_PREWARM_DISABLE/ config flag.")
                return
            import threading, time as _time

            def _prewarm_worker():
                t0 = _time.time()
                try:
                    try:
                        lm_func = app.config.get("LIST_MODELS_FUNC")
                        if callable(lm_func):
                            models = lm_func()
                            app.logger.info(
                                "[prewarm] Listed %d models.",
                                len(models) if isinstance(models, list) else -1,
                            )
                    except Exception as e:
                        app.logger.warning("[prewarm] Model list failed: %s", e)
                    try:
                        with app.test_client() as c:
                            r = c.get("/vontology/api/vontology/tree?refresh=1")
                            if r.status_code == 200:
                                app.logger.info(
                                    "[prewarm] Tree build OK (len bytes=%s)",
                                    len(r.data),
                                )
                            else:
                                app.logger.warning(
                                    "[prewarm] Tree build non-200 status=%s",
                                    r.status_code,
                                )
                    except Exception as e:
                        app.logger.warning("[prewarm] Tree build failed: %s", e)
                finally:
                    app.logger.info("[prewarm] Completed in %.2fs", _time.time() - t0)

            threading.Thread(
                target=_prewarm_worker, name="prewarm-thread", daemon=True
            ).start()
        except Exception as e:
            try:
                app.logger.warning("[prewarm] Failed to start: %s", e)
            except Exception:
                pass

    if (
        not app.testing
        and "PYTEST_CURRENT_TEST" not in os.environ
        and not app.config.get("PREWARM_DISABLE")
    ):
        try:

            @app.before_first_request  # type: ignore[attr-defined]
            def _defer_prewarm():  # type: ignore
                _start_prewarm()

        except Exception:
            _start_prewarm()

    return app


# Example usage (if running this file directly for testing)
if __name__ == "__main__":
    # For testing: create a simple app with dummy functions
    def dummy_list_models_func() -> List[str]:
        return ["model1", "model2"]

    def dummy_generate_func(
        prompt: str, context: Optional[List[Dict[str, str]]], model: Optional[str]
    ) -> str:
        return f"Generated response for prompt: {prompt}"

    # Create the app with dummy functions
    app = create_flask_app(dummy_list_models_func, dummy_generate_func)
    # Run the app (debug=True for development, use caution in production)
    app.run(host="0.0.0.0", port=5000, debug=True)
