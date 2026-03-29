import sys
import os
import datetime as _dt

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
    g,
)  # Added request for shutdown endpoint
import time

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
from .routes.workflows_routes import workflows_bp
from .routes.room_device_routes import room_device_bp
from .routes.client_capabilities_routes import client_capabilities_bp
from .routes.speech_routes import speech_bp
from .routes.task_routes import task_bp  # Task management (JVNAUTOSCI-1040)
from .routes.message_routes import message_bp  # Inter-user messaging (JVNAUTOSCI-1071)
from ..db.connection_manager import (
    ensure_monitor_started,
    get_db,
)  # start background DB monitor
from ..services.annotation_extraction_service import (
    prompt_concept_health_status,
    PROMPT_CONCEPT_ID,
)
from ..services.runtime_code_version_service import (
    DEFAULT_APP_VERSION,
    get_runtime_code_version,
    get_runtime_code_version_info,
)
from ..services.google_oauth_config import (
    google_oauth_strict_startup_enabled,
    validate_google_oauth_startup_or_raise,
)
from ..services.mongo_startup_config import (
    mongo_startup_probe_enabled,
    mongo_strict_startup_enabled,
    run_mongo_startup_probe,
    validate_mongo_startup_or_raise,
)
from ..services.settings_service import (
    get_internal_mcp_max_tool_invocations,
    get_internal_mcp_tool_batch_cap,
)

# Legacy fallback version string retained for backwards compatibility.
APP_VERSION = DEFAULT_APP_VERSION


# --- Durable Workflow System Globals ---
# These are module-level singletons for the durable workflow system.
# Initialised lazily via _start_durable_workflow_system().
# Type: WorkflowRegistry | None (from ..workflows)
_durable_workflow_registry = None
# Type: ActionRegistry | None (from ..workflows)
_durable_action_registry = None


def _build_durable_workflow_registry():
    """Build the durable runtime registry without blocking on parity work."""
    from ..workflows.durable.registry_factory import (
        get_shared_workflow_registry_read_only,
    )

    # Durable startup should populate the same shared registry that verified
    # submission and discovery reuse later, otherwise launch-time verification
    # pays for a second full lazy-registry build in the same process.
    return get_shared_workflow_registry_read_only(defer_parity_work=True)


def _bootstrap_workflow_authority_for_startup(app_logger) -> dict[str, Any]:
    """Repair workflow identity/type drift before strict parity checks run.

    Registry construction is intentionally read-only. Startup still needs one
    authoritative preflight repair pass so strict parity enforcement does not
    collapse interactive routes back to direct-LLM fallback when workflow
    concepts exist but are missing required typing metadata.
    """

    try:
        from ..workflows.durable.registry_factory import (
            build_vontology_workflow_registry_snapshot,
            invalidate_shared_workflow_registry_read_only,
        )
        from ..workflows.workflow_concept_authority_service import (
            bootstrap_workflow_concept_identities,
            build_workflow_concept_authority_report,
        )

        authority_registry = build_vontology_workflow_registry_snapshot()
        before_report = build_workflow_concept_authority_report(
            registry=authority_registry
        )
        bootstrap_report = dict(
            bootstrap_workflow_concept_identities(registry=authority_registry)
        )
        after_report = build_workflow_concept_authority_report(registry=authority_registry)
        invalidate_shared_workflow_registry_read_only()

        report = {
            **bootstrap_report,
            "success": not bool(after_report.get("drift_detected")),
            "before_authority_counts": dict(before_report.get("counts") or {}),
            "after_authority_counts": dict(after_report.get("counts") or {}),
        }
        if not bool(report.get("success")):
            app_logger.warning(
                "[durable_workflows] workflow authority bootstrap incomplete: %s",
                report,
            )
        return report
    except Exception as exc:
        report = {
            "success": False,
            "error": str(exc),
            "counts": {
                "registry_workflows": 0,
                "created": 0,
                "updated": 0,
                "unchanged": 0,
                "errors": 1,
            },
            "before_authority_counts": {},
            "after_authority_counts": {},
        }
        app_logger.warning(
            "[durable_workflows] workflow authority bootstrap error: %s",
            exc,
        )
        return report


def _build_durable_action_registry():
    """Build an ActionRegistry for durable workflow execution.

    Uses the centralised factory from registry_factory.py.
    See JVNAUTOSCI-922 Phase 1.
    """
    from ..workflows.durable.registry_factory import get_shared_durable_action_registry

    return get_shared_durable_action_registry()


def _get_durable_definition_loader():
    """Create a definition loader function for the durable worker."""
    global _durable_workflow_registry

    if _durable_workflow_registry is None:
        _durable_workflow_registry = _build_durable_workflow_registry()
    registry = _durable_workflow_registry

    def loader(workflow_id: str):
        if registry is None:
            return None
        # Primary lookup: existing in-memory registry.
        definition = registry.get(workflow_id)
        if definition is not None:
            return definition

        # Runtime bridge (JVNAUTOSCI-1106): lazily register newly authored
        # Vontology workflows when a worker first encounters them, so UI/chat
        # workflow authoring does not require a process restart.
        try:
            from ..workflows.durable.registry_factory import (
                register_workflow_from_vontology,
            )

            registered, _error_code = register_workflow_from_vontology(
                registry=registry,
                workflow_id=workflow_id,
            )
            if registered:
                return registry.get(workflow_id)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "[durable_workflows] Runtime workflow registration failed for %s: %s",
                workflow_id,
                exc,
            )

        return None

    return loader


def _start_durable_workflow_system(app_logger) -> dict | None:
    """Start the durable workflow worker and scheduler.

    Returns the system components dict or None if disabled/failed.
    """
    global _durable_workflow_registry, _durable_action_registry

    # Check if enabled via environment
    from ..services.feature_flags import get_durable_workflows_enabled

    enabled = get_durable_workflows_enabled(default=False)
    if not enabled:
        try:
            app_logger.info(
                "[durable_workflows] Disabled. Set VON_DURABLE_WORKFLOWS_ENABLE=1 to activate."
            )
        except Exception:
            pass
        return None

    try:
        from ..workflows.durable.startup import (
            start_worker_and_scheduler,
            recover_orphaned_instances,
            get_system_status,
        )
        from ..services.paper_representation_workflow_vontology_service import (
            bootstrap_canonical_paper_representation_workflows,
        )
        from ..services.episode_evaluation_workflow_vontology_service import (
            bootstrap_canonical_episode_evaluation_workflow,
        )
        from ..services.testing_workflow_vontology_service import (
            bootstrap_canonical_testing_workflows,
        )
        from ..services.talk_representation_workflow_vontology_service import (
            bootstrap_canonical_talk_representation_workflows,
        )

        def _run_workflow_family_bootstrap(
            *,
            label: str,
            bootstrap_fn: Callable[[], dict[str, Any]],
        ) -> dict[str, Any]:
            try:
                report = dict(bootstrap_fn())
                report.setdefault("success", True)
                return report
            except Exception as bootstrap_exc:
                report = {
                    "success": False,
                    "error": str(bootstrap_exc),
                }
                app_logger.warning(
                    "[durable_workflows] %s bootstrap error: %s",
                    label,
                    bootstrap_exc,
                )
                return report

        paper_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="paper workflow",
            bootstrap_fn=bootstrap_canonical_paper_representation_workflows,
        )
        episode_evaluation_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="episode evaluation workflow",
            bootstrap_fn=bootstrap_canonical_episode_evaluation_workflow,
        )
        talk_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="talk workflow",
            bootstrap_fn=bootstrap_canonical_talk_representation_workflows,
        )
        testing_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="testing workflow",
            bootstrap_fn=bootstrap_canonical_testing_workflows,
        )

        workflow_authority_bootstrap_report = _bootstrap_workflow_authority_for_startup(
            app_logger
        )

        # Build registries if not already done
        if _durable_workflow_registry is None:
            _durable_workflow_registry = _build_durable_workflow_registry()
        if _durable_action_registry is None:
            _durable_action_registry = _build_durable_action_registry()

        # Recover any orphaned instances from previous crashes
        recovered = recover_orphaned_instances()
        if recovered > 0:
            app_logger.info(
                "[durable_workflows] Recovered %d orphaned instances.", recovered
            )

        # Start worker and scheduler
        result = start_worker_and_scheduler(
            registry=_durable_action_registry,
            definition_loader=_get_durable_definition_loader(),
            enable_worker=True,
            enable_scheduler=True,
            worker_poll_interval=float(
                os.getenv("VON_DURABLE_WORKER_POLL_INTERVAL", "5.0")
            ),
            scheduler_check_interval=float(
                os.getenv("VON_DURABLE_SCHEDULER_CHECK_INTERVAL", "60.0")
            ),
        )
        result["paper_workflow_bootstrap"] = paper_workflow_bootstrap_report
        result["episode_evaluation_workflow_bootstrap"] = (
            episode_evaluation_workflow_bootstrap_report
        )
        result["talk_workflow_bootstrap"] = talk_workflow_bootstrap_report
        result["testing_workflow_bootstrap"] = testing_workflow_bootstrap_report
        result["workflow_authority_bootstrap"] = workflow_authority_bootstrap_report
        if not bool(paper_workflow_bootstrap_report.get("success", False)):
            app_logger.warning(
                "[durable_workflows] paper workflow bootstrap failed: %s",
                paper_workflow_bootstrap_report,
            )
        if not bool(
            episode_evaluation_workflow_bootstrap_report.get("success", False)
        ):
            app_logger.warning(
                "[durable_workflows] episode evaluation workflow bootstrap failed: %s",
                episode_evaluation_workflow_bootstrap_report,
            )
        if not bool(talk_workflow_bootstrap_report.get("success", False)):
            app_logger.warning(
                "[durable_workflows] talk workflow bootstrap failed: %s",
                talk_workflow_bootstrap_report,
            )
        if not bool(testing_workflow_bootstrap_report.get("success", False)):
            app_logger.warning(
                "[durable_workflows] testing workflow bootstrap failed: %s",
                testing_workflow_bootstrap_report,
            )
        if not bool(workflow_authority_bootstrap_report.get("success", False)):
            app_logger.warning(
                "[durable_workflows] workflow authority bootstrap failed: %s",
                workflow_authority_bootstrap_report,
            )

        # Ensure long-running identity-resolution maintenance keeps running
        # without manual schedule setup.
        try:
            from ..services.identity_resolution_schedule_bootstrap_service import (
                ensure_identity_resolution_background_schedule,
            )

            identity_schedule_report = (
                ensure_identity_resolution_background_schedule()
            )
            result["identity_resolution_schedule_bootstrap"] = identity_schedule_report
            if not bool(identity_schedule_report.get("success", False)):
                app_logger.warning(
                    "[durable_workflows] identity schedule bootstrap failed: %s",
                    identity_schedule_report,
                )
        except Exception as schedule_exc:
            app_logger.warning(
                "[durable_workflows] identity schedule bootstrap error: %s",
                schedule_exc,
            )

        # Parent-specificity rumination uses a Vontology-governed prompt plus a
        # managed durable schedule so it can keep scanning for new refinement
        # opportunities whenever the backend is running.
        try:
            from ..services.parent_specificity_vontology_service import (
                ensure_parent_specificity_prompt_support,
            )

            parent_specificity_prompt_report = (
                ensure_parent_specificity_prompt_support()
            )
            result["parent_specificity_prompt_bootstrap"] = (
                parent_specificity_prompt_report
            )
            if not bool(parent_specificity_prompt_report.get("success", False)):
                app_logger.warning(
                    "[durable_workflows] parent-specificity prompt bootstrap failed: %s",
                    parent_specificity_prompt_report,
                )
        except Exception as prompt_exc:
            app_logger.warning(
                "[durable_workflows] parent-specificity prompt bootstrap error: %s",
                prompt_exc,
            )

        try:
            from ..services.workflow_gap_vontology_service import (
                ensure_workflow_gap_prompt_support,
            )

            workflow_gap_prompt_report = ensure_workflow_gap_prompt_support()
            result["workflow_gap_prompt_bootstrap"] = workflow_gap_prompt_report
            if not bool(workflow_gap_prompt_report.get("success", False)):
                app_logger.warning(
                    "[durable_workflows] workflow-gap prompt bootstrap failed: %s",
                    workflow_gap_prompt_report,
                )
        except Exception as prompt_exc:
            app_logger.warning(
                "[durable_workflows] workflow-gap prompt bootstrap error: %s",
                prompt_exc,
            )

        try:
            from ..services.workflow_description_vontology_service import (
                ensure_workflow_description_prompt_support,
            )

            workflow_description_prompt_report = (
                ensure_workflow_description_prompt_support()
            )
            result["workflow_description_prompt_bootstrap"] = (
                workflow_description_prompt_report
            )
            if not bool(workflow_description_prompt_report.get("success", False)):
                app_logger.warning(
                    "[durable_workflows] workflow-description prompt bootstrap failed: %s",
                    workflow_description_prompt_report,
                )
        except Exception as prompt_exc:
            app_logger.warning(
                "[durable_workflows] workflow-description prompt bootstrap error: %s",
                prompt_exc,
            )

        try:
            from ..services.parent_specificity_schedule_bootstrap_service import (
                ensure_parent_specificity_background_schedule,
            )

            parent_specificity_schedule_report = (
                ensure_parent_specificity_background_schedule()
            )
            result["parent_specificity_schedule_bootstrap"] = (
                parent_specificity_schedule_report
            )
            if not bool(parent_specificity_schedule_report.get("success", False)):
                app_logger.warning(
                    "[durable_workflows] parent-specificity schedule bootstrap failed: %s",
                    parent_specificity_schedule_report,
                )
        except Exception as schedule_exc:
            app_logger.warning(
                "[durable_workflows] parent-specificity schedule bootstrap error: %s",
                schedule_exc,
            )

        # Log status
        status = get_system_status()
        app_logger.info(
            "[durable_workflows] Started. worker=%s, scheduler=%s, pending=%d",
            status.get("worker_running"),
            status.get("scheduler_running"),
            status.get("instances", {}).get("pending", 0),
        )

        return result
    except Exception as exc:
        try:
            app_logger.warning("[durable_workflows] Failed to start: %s", exc)
        except Exception:
            pass
        return None


def _stop_durable_workflow_system():
    """Stop the durable workflow system gracefully."""
    try:
        from ..workflows.durable.startup import stop_worker_and_scheduler

        stop_worker_and_scheduler(timeout=10.0)
    except Exception:
        pass


def _is_running_under_pytest() -> bool:
    # Avoid startup side-effects (DB writes, slow calls) during test imports.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    return "pytest" in sys.modules


def _startup_requeue_unindexed_interaction_sessions(app_logger: logging.Logger) -> None:
    """Requeue eligible unindexed interaction_sessions back to pending.

    This is a lightweight safety-net for cases where sessions were created/updated
    but never got picked up by the background indexing worker.

    It is deliberately conservative:
    - only touches sessions with indexing_status missing/None/skipped
    - only touches sessions with non-empty history or non-empty summary
    - caps the number of sessions requeued per startup
    """

    try:
        from datetime import datetime, timezone

        db = get_db()
        if db is None:
            return

        coll = db["interaction_sessions"]
        limit = int(os.getenv("VON_RAG_STARTUP_REQUEUE_LIMIT", "25"))
        if limit <= 0:
            return

        eligible_filter = {
            "$or": [
                {"history": {"$exists": True, "$ne": []}},
                {"summary": {"$exists": True, "$type": "string", "$ne": ""}},
            ]
        }
        status_filter = {
            "$or": [
                {"indexing_status": {"$exists": False}},
                {"indexing_status": None},
                {"indexing_status": "skipped"},
            ]
        }
        cursor = (
            coll.find({**eligible_filter, **status_filter}, {"_id": 1})
            .sort([("last_updated_time", -1), ("_id", -1)])
            .limit(limit)
        )
        ids = [d.get("_id") for d in cursor if d.get("_id") is not None]
        if not ids:
            return

        now = datetime.now(timezone.utc)
        result = coll.update_many(
            {"_id": {"$in": ids}},
            {"$set": {"indexing_status": "pending", "requeued_at": now}},
        )
        try:
            app_logger.info(
                "[startup] Requeued %d/%d eligible interaction sessions to pending.",
                result.modified_count,
                len(ids),
            )
        except Exception:
            pass
    except Exception as exc:
        try:
            app_logger.warning("[startup] RAG requeue check failed: %s", exc)
        except Exception:
            pass


_DEV_SECRET_KEY = "von-dev-secret-key-change-in-production"


def _truthy_env_value(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return _truthy_env_value(raw)


def _normalise_session_cookie_samesite(raw: str | None) -> str:
    if raw is None:
        return "Lax"
    normalised = raw.strip().lower()
    if normalised == "strict":
        return "Strict"
    if normalised == "none":
        return "None"
    return "Lax"


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

    # --- Request Timing Middleware ---
    # Log slow requests to help diagnose performance issues
    SLOW_REQUEST_THRESHOLD_MS = float(
        os.environ.get("VON_SLOW_REQUEST_THRESHOLD_MS", "1000")
    )

    @app.before_request
    def _start_request_timer():
        """Record request start time for timing middleware."""
        g._request_start_time = time.perf_counter()

    @app.after_request
    def _log_slow_requests(response):
        """Log requests that exceed the slow request threshold."""
        start_time = getattr(g, "_request_start_time", None)
        if start_time is not None:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            endpoint = request.endpoint or request.path
            if elapsed_ms >= SLOW_REQUEST_THRESHOLD_MS:
                app.logger.warning(
                    "[slow_request] %s %s took %.1fms (threshold=%.0fms) status=%s",
                    request.method,
                    request.path,
                    elapsed_ms,
                    SLOW_REQUEST_THRESHOLD_MS,
                    response.status_code,
                )
            elif elapsed_ms >= 200:  # Log moderate latency at INFO level
                app.logger.info(
                    "[request_timing] %s %s %.1fms status=%s",
                    request.method,
                    request.path,
                    elapsed_ms,
                    response.status_code,
                )
        return response

    # --- Configuration Setup ---
    # Set secret key for session management (required for Google OAuth)
    app.secret_key = os.environ.get(
        "FLASK_SECRET_KEY", _DEV_SECRET_KEY
    )
    strict_oauth_startup = google_oauth_strict_startup_enabled()

    # Cookie defaults are conservative, and become strict-by-default when OAuth
    # strict startup mode is enabled for hosted HTTPS deployments.
    app.config["SESSION_COOKIE_HTTPONLY"] = _env_bool(
        "FLASK_SESSION_COOKIE_HTTPONLY", True
    )
    app.config["SESSION_COOKIE_SECURE"] = _env_bool(
        "FLASK_SESSION_COOKIE_SECURE", strict_oauth_startup
    )
    app.config["SESSION_COOKIE_SAMESITE"] = _normalise_session_cookie_samesite(
        os.getenv("FLASK_SESSION_COOKIE_SAMESITE")
    )

    if strict_oauth_startup:
        if app.secret_key == _DEV_SECRET_KEY:
            raise RuntimeError(
                "GOOGLE_OAUTH_STRICT_STARTUP requires FLASK_SECRET_KEY to be set to a non-default value."
            )
        if len(str(app.secret_key)) < 32:
            raise RuntimeError(
                "GOOGLE_OAUTH_STRICT_STARTUP requires FLASK_SECRET_KEY length >= 32 characters."
            )
        validate_google_oauth_startup_or_raise()

    strict_mongo_startup = mongo_strict_startup_enabled()
    if strict_mongo_startup:
        validate_mongo_startup_or_raise()

    startup_probe_enabled = mongo_startup_probe_enabled()
    if startup_probe_enabled and not _is_running_under_pytest():
        probe_result = run_mongo_startup_probe()
        app.logger.info(
            "Mongo startup probe succeeded (read=%s write=%s collection=%s).",
            probe_result.get("read_ok"),
            probe_result.get("write_ok"),
            probe_result.get("write_collection"),
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

    @app.context_processor
    def inject_feature_flags():
        from ..services.feature_flags import (
            get_expert_footer_enabled,
            get_expert_tabs_enabled,
        )

        return {
            "expert_tabs_enabled": get_expert_tabs_enabled(),
            "expert_footer_enabled": get_expert_footer_enabled(),
        }

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
    app.register_blueprint(workflows_bp)  # /api/workflows/*
    app.register_blueprint(room_device_bp)
    app.register_blueprint(client_capabilities_bp)
    app.register_blueprint(speech_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(
        task_bp, url_prefix="/api/tasks"
    )  # Task management (JVNAUTOSCI-1040)
    app.register_blueprint(
        message_bp, url_prefix="/api/messages"
    )  # Inter-user messaging (JVNAUTOSCI-1071)
    app.register_blueprint(
        auth_bp, url_prefix="/von"
    )  # Register the new auth blueprint with /von prefix to match Google OAuth config
    app.register_blueprint(
        agent_gmail_oauth_bp, url_prefix="/von"
    )  # Agent Gmail OAuth endpoints (separate from user login)
    # ---------------------------

    def _slug_from_maybe_concept_id(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None
        return raw[3:] if raw.startswith("#V#") else raw

    def _get_session_user_slug(flask_session: object) -> str | None:
        get = getattr(flask_session, "get", None)
        if not callable(get):
            return None

        user_id = _slug_from_maybe_concept_id(get("user_id"))
        if user_id:
            return user_id.lower().replace(" ", "_")

        user_concept_id = get("user_concept_id")
        user_slug = _slug_from_maybe_concept_id(user_concept_id)
        if user_slug:
            return user_slug.lower().replace(" ", "_")

        return None

    # --- Log App Version ---
    # Use app.logger if available, otherwise print
    runtime_code_version = get_runtime_code_version()
    try:
        app.logger.setLevel(logging.INFO)  # Ensure INFO level is logged
        app.logger.info(
            f"--- Flask App Initializing - Version: {runtime_code_version} ---"
        )
    except Exception:
        print(
            f"--- Flask App Initializing - Version: {runtime_code_version} ---"
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
    # Keep HTTP startup non-blocking: orchestrator construction can be expensive
    # because it builds workflow/action registries (including Vontology policy checks).
    # When this blocks the main thread, the process can be alive for minutes without
    # binding the web port, which prevents run.ps1 health/browser flow from completing.
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    app.config["INTERNAL_MCP_ORCHESTRATOR_STATUS"] = {
        "state": "disabled" if gateway_instance is None else "pending",
        "ready": False,
        "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }

    if gateway_instance is not None:
        orchestrator_logger = app.logger.getChild("mcp_orchestrator") if app.logger else None
        blocking_orchestrator_start = (
            os.getenv("VON_INTERNAL_MCP_ORCHESTRATOR_BLOCKING_STARTUP", "0")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )

        def _build_orchestrator() -> None:
            start_perf = time.perf_counter()
            app.config["INTERNAL_MCP_ORCHESTRATOR_STATUS"] = {
                "state": "initialising",
                "ready": False,
                "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }
            try:
                try:
                    bootstrap_max_tool_invocations = (
                        get_internal_mcp_max_tool_invocations()
                    )
                except Exception:
                    bootstrap_max_tool_invocations = 30
                try:
                    bootstrap_tool_batch_cap = get_internal_mcp_tool_batch_cap()
                except Exception:
                    bootstrap_tool_batch_cap = 10
                orchestrator_instance = InternalMCPChatOrchestrator(
                    gateway=gateway_instance,
                    logger=orchestrator_logger,
                    max_tool_invocations=bootstrap_max_tool_invocations,
                    tool_batch_cap=bootstrap_tool_batch_cap,
                    default_gmail_profile=os.getenv("VON_GMAIL_DEFAULT_PROFILE") or None,
                )
                duration_ms = int((time.perf_counter() - start_perf) * 1000)
                app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator_instance
                app.config["INTERNAL_MCP_ORCHESTRATOR_STATUS"] = {
                    "state": "ready",
                    "ready": True,
                    "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "duration_ms": duration_ms,
                }
                try:
                    app.logger.info(
                        "[mcp_orchestrator] Initialised in %dms (blocking_startup=%s).",
                        duration_ms,
                        blocking_orchestrator_start,
                    )
                except Exception:
                    pass
            except Exception as exc:  # pragma: no cover - defensive bootstrap
                app.config["INTERNAL_MCP_ORCHESTRATOR_STATUS"] = {
                    "state": "failed",
                    "ready": False,
                    "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "error": str(exc),
                }
                try:
                    app.logger.warning("[mcp_orchestrator] Failed to initialise: %s", exc)
                except Exception:
                    pass

        if blocking_orchestrator_start:
            _build_orchestrator()
        else:
            try:
                app.logger.info(
                    "[mcp_orchestrator] Deferring initialisation to background thread "
                    "(set VON_INTERNAL_MCP_ORCHESTRATOR_BLOCKING_STARTUP=1 to restore blocking startup)."
                )
            except Exception:
                pass
            try:
                import threading

                threading.Thread(
                    target=_build_orchestrator,
                    name="mcp_orchestrator_init",
                    daemon=True,
                ).start()
            except Exception as exc:  # pragma: no cover - defensive bootstrap
                app.config["INTERNAL_MCP_ORCHESTRATOR_STATUS"] = {
                    "state": "failed",
                    "ready": False,
                    "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "error": f"thread_start_failed:{exc}",
                }
                try:
                    app.logger.warning(
                        "[mcp_orchestrator] Failed to start async init thread: %s", exc
                    )
                except Exception:
                    pass

    # Start DB connection monitor (idempotent)
    try:
        ensure_monitor_started()
    except Exception as _e:  # pragma: no cover
        try:
            app.logger.warning("Failed to start DB monitor: %s", _e)
        except Exception:
            pass

    # Lightweight startup reconcile: requeue eligible sessions that are not indexed.
    # Skip during pytest to avoid DB side-effects during test imports.
    if not _is_running_under_pytest():
        try:
            import threading

            if os.getenv("VON_RAG_STARTUP_REQUEUE", "1").lower() in {"1", "true"}:
                threading.Thread(
                    target=_startup_requeue_unindexed_interaction_sessions,
                    args=(app.logger,),
                    daemon=True,
                    name="rag_startup_requeue",
                ).start()
        except Exception as exc:  # pragma: no cover
            try:
                app.logger.warning("[startup] Failed to start requeue thread: %s", exc)
            except Exception:
                pass

    # --- Durable Workflow System Startup ---
    # Keep HTTP startup non-blocking: durable startup can synchronously build
    # workflow/action registries and perform recovery, which can take minutes
    # and delay the first HTTP bind.
    app.config["DURABLE_WORKFLOW_COMPONENTS"] = None
    app.config["DURABLE_WORKFLOW_STARTUP_STATUS"] = {
        "state": "skipped_pytest" if _is_running_under_pytest() else "pending",
        "ready": False,
        "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    durable_workflow_components = None
    if not _is_running_under_pytest():
        blocking_durable_startup = (
            os.getenv("VON_DURABLE_WORKFLOWS_BLOCKING_STARTUP", "0").strip().lower()
            in {"1", "true", "yes", "on"}
        )

        def _bootstrap_durable_workflow_system() -> None:
            app.config["DURABLE_WORKFLOW_STARTUP_STATUS"] = {
                "state": "initialising",
                "ready": False,
                "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }
            startup_perf = time.perf_counter()
            try:
                components = _start_durable_workflow_system(app.logger)
                duration_ms = int((time.perf_counter() - startup_perf) * 1000)
                if components is not None:
                    app.config["DURABLE_WORKFLOW_COMPONENTS"] = components
                    app.config["DURABLE_WORKFLOW_STARTUP_STATUS"] = {
                        "state": "ready",
                        "ready": True,
                        "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                        "duration_ms": duration_ms,
                    }
                    try:
                        app.logger.info(
                            "[durable_workflows] Initialised in %dms (blocking_startup=%s).",
                            duration_ms,
                            blocking_durable_startup,
                        )
                    except Exception:
                        pass

                    # Register atexit handler for graceful shutdown once running.
                    import atexit

                    atexit.register(_stop_durable_workflow_system)
                else:
                    app.config["DURABLE_WORKFLOW_STARTUP_STATUS"] = {
                        "state": "not_started",
                        "ready": False,
                        "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                        "duration_ms": duration_ms,
                    }
            except Exception as exc:  # pragma: no cover
                app.config["DURABLE_WORKFLOW_STARTUP_STATUS"] = {
                    "state": "failed",
                    "ready": False,
                    "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "error": str(exc),
                }
                try:
                    app.logger.warning(
                        "[startup] Durable workflow system startup failed: %s", exc
                    )
                except Exception:
                    pass

        if blocking_durable_startup:
            _bootstrap_durable_workflow_system()
        else:
            try:
                app.logger.info(
                    "[durable_workflows] Deferring startup to background thread "
                    "(set VON_DURABLE_WORKFLOWS_BLOCKING_STARTUP=1 to restore blocking startup)."
                )
            except Exception:
                pass
            try:
                import threading

                threading.Thread(
                    target=_bootstrap_durable_workflow_system,
                    name="durable_workflow_startup",
                    daemon=True,
                ).start()
            except Exception as exc:  # pragma: no cover
                app.config["DURABLE_WORKFLOW_STARTUP_STATUS"] = {
                    "state": "failed",
                    "ready": False,
                    "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "error": f"thread_start_failed:{exc}",
                }
                try:
                    app.logger.warning(
                        "[durable_workflows] Failed to start async startup thread: %s",
                        exc,
                    )
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

        # NOTE: Keep /health lightweight.
        # Avoid DB calls here because the UI polls frequently and client-side aborts
        # do not cancel server work. If a DB call hangs, it can occupy Waitress
        # threads and block *all* endpoints (including /health) for minutes.
        rag_pending_count = None
        version_info = get_runtime_code_version_info()

        return jsonify(
            status="healthy",
            version=version_info.get("version"),
            version_details=version_info,
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
            # Cache results briefly to avoid self-DOS:
            # the UI polls frequently and aborts client-side after ~5s, but the
            # server keeps running DB work unless we short-circuit.
            import threading
            import time

            t0 = time.perf_counter()

            ttl_seconds = 10.0
            if request.args.get("nocache") in {"1", "true", "yes", "on"}:
                ttl_seconds = 0.0

            ns = request.args.get("namespace")
            include_detail = request.args.get("detail", "").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            include_namespace_events = request.args.get(
                "include_namespace_events", ""
            ).lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            try:
                namespace_event_limit = int(
                    request.args.get("namespace_event_limit", 20) or 20
                )
            except Exception:
                namespace_event_limit = 20
            cache_key = (
                ns or "",
                bool(include_detail),
                bool(include_namespace_events),
                int(namespace_event_limit),
            )
            cache = app.config.setdefault("_RAG_STATUS_CACHE", {})
            lock = app.config.setdefault("_RAG_STATUS_CACHE_LOCK", threading.Lock())

            if ttl_seconds > 0:
                try:
                    with lock:
                        entry = cache.get(cache_key)
                    if entry:
                        cached_at = float(entry.get("at", 0.0) or 0.0)
                        if (time.time() - cached_at) <= ttl_seconds:
                            payload = entry.get("payload")
                            if isinstance(payload, dict):
                                response_payload = dict(payload)
                                response_payload["cache_hit"] = True
                                response_payload["server_elapsed_ms"] = int(
                                    (time.perf_counter() - t0) * 1000
                                )
                                response_payload["cache_age_sec"] = max(
                                    0.0, float(time.time() - cached_at)
                                )
                                return jsonify(response_payload)
                except Exception:
                    # If caching fails, fall back to computing.
                    pass

            db = get_db()
            if db is None:
                return jsonify(error="db_unavailable"), 503
            sessions_coll = db["interaction_sessions"]
            chat_history_coll = (
                db["chat_history"]
                if "chat_history" in db.list_collection_names()
                else None
            )
            interactions_coll = (
                db["interactions"]
                if "interactions" in db.list_collection_names()
                else None
            )
            # Optional namespace filter: interaction_sessions may store either a
            # user-only namespace (#V#user) or a composite namespace (#V#user@org).
            # For backwards compatibility, if a composite namespace is provided we
            # scope to BOTH values.
            # NOTE: ns/include_detail already parsed above for caching.
            session_ns_values: list[str] | None = None
            if ns:
                session_ns_values = [ns]
                try:
                    from src.backend.services.namespace_service import parse_namespace

                    parsed = parse_namespace(ns)
                    if parsed.get("user_id"):
                        user_only = f"#V#{parsed['user_id']}"
                        if user_only not in session_ns_values:
                            session_ns_values.append(user_only)
                except Exception:
                    # If the namespace is not parseable, treat it as an opaque key.
                    pass

            # Diagnostics: explain scoping precisely.
            missing_namespace_filter = {
                "$or": [
                    {"namespace": {"$exists": False}},
                    {"namespace": None},
                    {"namespace": {"$in": ["", " "]}},
                ]
            }

            if session_ns_values:
                sess_filter = {"namespace": {"$in": session_ns_values}}
            else:
                sess_filter = {}
            total_sessions = sessions_coll.count_documents({})
            scoped_sessions = sessions_coll.count_documents(sess_filter or {})
            sessions_missing_namespace = sessions_coll.count_documents(
                missing_namespace_filter
            )
            sessions_other_namespace = None
            if session_ns_values:
                sessions_other_namespace = max(
                    0, total_sessions - scoped_sessions - sessions_missing_namespace
                )

            sessions_namespace_breakdown = []
            try:
                pipeline = [
                    {
                        "$project": {
                            "namespace": {"$ifNull": ["$namespace", "__MISSING__"]},
                            "indexing_status": {
                                "$ifNull": ["$indexing_status", "__NONE__"]
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": "$namespace",
                            "total": {"$sum": 1},
                            "indexed": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$indexing_status", "indexed"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "pending": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$indexing_status", "pending"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "failed": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$indexing_status", "failed"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "skipped": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$indexing_status", "skipped"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "none": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$indexing_status", "__NONE__"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                    {"$sort": {"total": -1}},
                    {"$limit": 15},
                ]
                rows = list(sessions_coll.aggregate(pipeline))
                for row in rows:
                    ns_key = row.get("_id")
                    if ns_key == "__MISSING__":
                        ns_key = None
                    sessions_namespace_breakdown.append(
                        {
                            "namespace": ns_key,
                            "total": int(row.get("total", 0) or 0),
                            "indexed": int(row.get("indexed", 0) or 0),
                            "pending": int(row.get("pending", 0) or 0),
                            "failed": int(row.get("failed", 0) or 0),
                            "skipped": int(row.get("skipped", 0) or 0),
                            "none": int(row.get("none", 0) or 0),
                        }
                    )
            except Exception:
                sessions_namespace_breakdown = []
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

            chat_summary = {
                "chat_history_sessions": 0,
                "chat_history_messages": 0,
                "chat_history_rag_success": 0,
                "chat_history_rag_failed": 0,
                "chat_history_sessions_in_namespace": 0,
                "chat_history_sessions_missing_namespace": 0,
                "chat_history_sessions_other_namespace": 0,
                "chat_history_session_details": None,
                "chat_history_backfill_available": False,
                "chat_history_backfill_reason": None,
                "chat_history_backfill_session_namespace": None,
            }
            if chat_history_coll is not None:
                # If a namespace is provided, treat it as a user@org scope and:
                # - always scope chat history to that user (prevents cross-user leakage)
                # - break down sessions by: in-namespace vs missing namespace (legacy) vs other namespace
                user_id_for_ns = None
                if ns:
                    try:
                        from src.backend.services.namespace_service import (
                            parse_namespace,
                        )

                        parsed = parse_namespace(ns)
                        if parsed.get("user_id"):
                            user_id_for_ns = f"#V#{parsed['user_id']}"
                    except Exception:
                        user_id_for_ns = None

                match = {"user_id": user_id_for_ns} if user_id_for_ns else {}

                pipeline = [
                    {"$match": match},
                    {
                        "$project": {
                            "namespace": 1,
                            "rag_indexed_success": {
                                "$ifNull": ["$rag_indexed_success", 0]
                            },
                            "rag_indexed_failed": {
                                "$ifNull": ["$rag_indexed_failed", 0]
                            },
                            "messages_stored": {
                                "$cond": [
                                    {"$isArray": "$history"},
                                    {"$size": "$history"},
                                    {"$ifNull": ["$message_count", 0]},
                                ]
                            },
                            "in_namespace": {
                                "$cond": [
                                    {"$eq": ["$namespace", ns]},
                                    1,
                                    0,
                                ]
                            },
                            "missing_namespace": {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {"$ifNull": ["$namespace", None]},
                                            None,
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "sessions": {"$sum": 1},
                            "sessions_in_namespace": {"$sum": "$in_namespace"},
                            "sessions_missing_namespace": {
                                "$sum": "$missing_namespace"
                            },
                            "messages": {"$sum": "$messages_stored"},
                            "rag_success": {"$sum": "$rag_indexed_success"},
                            "rag_failed": {"$sum": "$rag_indexed_failed"},
                        }
                    },
                ]

                agg = list(chat_history_coll.aggregate(pipeline))
                if agg:
                    sessions_total = int(agg[0].get("sessions", 0) or 0)
                    sessions_in_namespace = int(
                        agg[0].get("sessions_in_namespace", 0) or 0
                    )
                    sessions_missing_namespace = int(
                        agg[0].get("sessions_missing_namespace", 0) or 0
                    )
                    sessions_other_namespace = max(
                        0,
                        sessions_total
                        - sessions_in_namespace
                        - sessions_missing_namespace,
                    )
                    chat_summary = {
                        "chat_history_sessions": sessions_total,
                        "chat_history_messages": int(agg[0].get("messages", 0) or 0),
                        "chat_history_rag_success": int(
                            agg[0].get("rag_success", 0) or 0
                        ),
                        "chat_history_rag_failed": int(
                            agg[0].get("rag_failed", 0) or 0
                        ),
                        "chat_history_sessions_in_namespace": sessions_in_namespace,
                        "chat_history_sessions_missing_namespace": sessions_missing_namespace,
                        "chat_history_sessions_other_namespace": sessions_other_namespace,
                        "chat_history_session_details": None,
                        "chat_history_backfill_available": False,
                        "chat_history_backfill_reason": None,
                    }

                # Optional per-session breakdown (for diagnostics / UI modal).
                # Only return details when a namespace is provided so we can scope
                # by user_id safely.
                if include_detail and user_id_for_ns:
                    try:
                        details_pipeline = [
                            {"$match": match},
                            {
                                "$project": {
                                    "_id": 0,
                                    "session_id": 1,
                                    "namespace": 1,
                                    "rag_indexed_success": {
                                        "$ifNull": ["$rag_indexed_success", 0]
                                    },
                                    "rag_indexed_failed": {
                                        "$ifNull": ["$rag_indexed_failed", 0]
                                    },
                                    "messages_stored": {
                                        "$cond": [
                                            {"$isArray": "$history"},
                                            {"$size": "$history"},
                                            {"$ifNull": ["$message_count", 0]},
                                        ]
                                    },
                                    "messages_non_reset": {
                                        "$cond": [
                                            {"$isArray": "$history"},
                                            {
                                                "$size": {
                                                    "$filter": {
                                                        "input": "$history",
                                                        "as": "m",
                                                        "cond": {
                                                            "$not": {
                                                                "$and": [
                                                                    {
                                                                        "$eq": [
                                                                            "$$m.role",
                                                                            "system",
                                                                        ]
                                                                    },
                                                                    {
                                                                        "$eq": [
                                                                            "$$m.content",
                                                                            "__RESET__",
                                                                        ]
                                                                    },
                                                                ]
                                                            }
                                                        },
                                                    }
                                                }
                                            },
                                            {"$ifNull": ["$message_count", 0]},
                                        ]
                                    },
                                    "messages_indexable": {
                                        "$cond": [
                                            {"$isArray": "$history"},
                                            {
                                                "$size": {
                                                    "$filter": {
                                                        "input": "$history",
                                                        "as": "m",
                                                        "cond": {
                                                            "$and": [
                                                                {
                                                                    "$not": {
                                                                        "$and": [
                                                                            {
                                                                                "$eq": [
                                                                                    "$$m.role",
                                                                                    "system",
                                                                                ]
                                                                            },
                                                                            {
                                                                                "$eq": [
                                                                                    "$$m.content",
                                                                                    "__RESET__",
                                                                                ]
                                                                            },
                                                                        ]
                                                                    }
                                                                },
                                                                {
                                                                    "$eq": [
                                                                        {
                                                                            "$type": "$$m.content"
                                                                        },
                                                                        "string",
                                                                    ]
                                                                },
                                                                {
                                                                    "$gt": [
                                                                        {
                                                                            "$strLenCP": {
                                                                                "$trim": {
                                                                                    "input": "$$m.content"
                                                                                }
                                                                            }
                                                                        },
                                                                        0,
                                                                    ]
                                                                },
                                                            ]
                                                        },
                                                    }
                                                }
                                            },
                                            {"$ifNull": ["$message_count", 0]},
                                        ]
                                    },
                                    "in_namespace": {
                                        "$cond": [
                                            {"$eq": ["$namespace", ns]},
                                            True,
                                            False,
                                        ]
                                    },
                                }
                            },
                            {
                                "$addFields": {
                                    "messages_indexed_total": {
                                        "$add": [
                                            "$rag_indexed_success",
                                            "$rag_indexed_failed",
                                        ]
                                    },
                                }
                            },
                            {
                                "$addFields": {
                                    "messages_missing_index": {
                                        "$max": [
                                            0,
                                            {
                                                "$subtract": [
                                                    "$messages_indexable",
                                                    "$messages_indexed_total",
                                                ]
                                            },
                                        ]
                                    }
                                }
                            },
                            {
                                "$sort": {
                                    "messages_missing_index": -1,
                                    "messages_non_reset": -1,
                                }
                            },
                            {"$limit": 100},
                        ]

                        details = list(chat_history_coll.aggregate(details_pipeline))
                        chat_summary["chat_history_session_details"] = details
                    except Exception:
                        # Best-effort diagnostics only; never fail rag_status.
                        chat_summary["chat_history_session_details"] = None

                # Availability is based on being logged in AND the requested namespace matching
                # the current session-derived namespace (prevents cross-user actions).
                if ns:
                    try:
                        from flask import session as flask_session
                        from src.backend.services.namespace_service import (
                            derive_namespace,
                        )

                        sess_user = _get_session_user_slug(flask_session)
                        sess_org_raw = flask_session.get(
                            "organisation_concept_id"
                        ) or flask_session.get("org_id")
                        sess_org = _slug_from_maybe_concept_id(sess_org_raw)
                        sess_ns = flask_session.get("namespace")
                        if not sess_ns and sess_user:
                            sess_ns = derive_namespace(sess_user, sess_org)

                        chat_summary["chat_history_backfill_session_namespace"] = (
                            sess_ns
                        )

                        is_authenticated = bool(
                            sess_user or flask_session.get("user_concept_id")
                        )

                        if not is_authenticated:
                            chat_summary["chat_history_backfill_available"] = False
                            chat_summary["chat_history_backfill_reason"] = (
                                "not_authenticated"
                            )
                        elif sess_ns != ns:
                            chat_summary["chat_history_backfill_available"] = False
                            chat_summary["chat_history_backfill_reason"] = (
                                "namespace_mismatch"
                            )
                        else:
                            chat_summary["chat_history_backfill_available"] = True
                            chat_summary["chat_history_backfill_reason"] = None
                    except Exception:
                        chat_summary["chat_history_backfill_available"] = False
                        chat_summary["chat_history_backfill_reason"] = "unavailable"

            namespace_isolation_diagnostics = {"available": False}
            try:
                from ..services.namespace_isolation_diagnostics_service import (
                    get_namespace_isolation_diagnostics_snapshot,
                )

                namespace_isolation_diagnostics = (
                    get_namespace_isolation_diagnostics_snapshot(
                        include_recent_events=include_namespace_events,
                        recent_limit=namespace_event_limit,
                    )
                )
                namespace_isolation_diagnostics["available"] = True
                namespace_isolation_diagnostics["include_recent_events"] = bool(
                    include_namespace_events
                )
            except Exception as exc:
                namespace_isolation_diagnostics = {
                    "available": False,
                    "error": type(exc).__name__,
                }
            base_payload = {
                "total": total_sessions,
                "scoped_sessions": scoped_sessions,
                "indexed": indexed,
                "pending": pending,
                "failed": failed,
                "skipped": skipped,
                "sessions_missing_namespace": sessions_missing_namespace,
                "sessions_other_namespace": sessions_other_namespace,
                "sessions_namespace_breakdown": sessions_namespace_breakdown,
                "sessions": total_sessions,
                "interactions": total_interactions,
                "eligible_sessions": eligible_sessions,
                "eligible_interactions": eligible_interactions,
                "namespace": ns,
                "session_namespace": (
                    session_ns_values[0] if session_ns_values else None
                ),
                "session_namespaces": session_ns_values,
                "namespace_isolation_diagnostics": namespace_isolation_diagnostics,
                **chat_summary,
            }

            if ttl_seconds > 0:
                try:
                    with lock:
                        cache[cache_key] = {
                            "at": time.time(),
                            "payload": base_payload,
                        }
                except Exception:
                    pass

            response_payload = dict(base_payload)
            response_payload["cache_hit"] = False
            response_payload["server_elapsed_ms"] = int(
                (time.perf_counter() - t0) * 1000
            )
            return jsonify(response_payload)
        except Exception as e:
            return jsonify(error="unexpected", detail=str(e)), 500

    @app.route("/admin/rag_runtime")
    def rag_runtime():
        """Return lightweight RAG runtime info for UI diagnostics.

        Intended for local dev UX (e.g., showing which embedder is active).
        Must never expose secrets (API keys, tokens).

        Query params:
          - namespace (optional)
        """

        import os

        def _describe_component(obj):
            if obj is None:
                return None
            try:
                cls = obj.__class__
                info = {
                    "class_name": getattr(cls, "__name__", None),
                    "module": getattr(cls, "__module__", None),
                }
                for attr in ("model_name", "model", "name"):
                    try:
                        v = getattr(obj, attr, None)
                        if isinstance(v, str) and v.strip():
                            info[attr] = v.strip()
                    except Exception:
                        pass
                return info
            except Exception:
                return None

        try:
            from src.backend.services.rag_service import (
                RAGBackendUnavailable,
                peek_rag_service,
            )

            ns = request.args.get("namespace")

            # Do not initialise a RAG backend just to render diagnostics.
            # Initialising LlamaIndex (or its embedder) can be slow and can block
            # the UI if the dev server is single-threaded.
            service = peek_rag_service()

            if service is None:
                effective_namespace = ns or os.getenv("VON_DEFAULT_NAMESPACE")
                return jsonify(
                    {
                        "success": True,
                        "service_initialised": False,
                        "requested_namespace": ns,
                        "effective_namespace": effective_namespace,
                        "backend": {
                            "requested": "llamaindex",
                            "class_name": None,
                            "module": None,
                        },
                        "persistence_dir": None,
                        "index_persist_dir": None,
                        "index_cached": False,
                        "embedder": None,
                        "llm": None,
                        "openai_key_present": bool(os.getenv("OPENAI_API_KEY")),
                        "last_query": None,
                        "note": "RAG service not initialised yet; open this panel again after the first RAG query/index operation.",
                    }
                )

            effective_namespace = None
            try:
                resolver = getattr(service, "_resolve_effective_namespace", None)
                if callable(resolver):
                    effective_namespace = resolver(ns)
                else:
                    effective_namespace = ns or os.getenv("VON_DEFAULT_NAMESPACE")
            except Exception:
                effective_namespace = ns or os.getenv("VON_DEFAULT_NAMESPACE")

            index_persist_dir = None
            try:
                ns_dir = getattr(service, "_namespace_persist_dir", None)
                if callable(ns_dir) and isinstance(effective_namespace, str):
                    index_persist_dir = ns_dir(effective_namespace)
            except Exception:
                index_persist_dir = None

            index_cached = False
            try:
                indices = getattr(service, "_indices", None)
                if isinstance(indices, dict) and isinstance(effective_namespace, str):
                    index_cached = effective_namespace in indices
            except Exception:
                index_cached = False

            embedder = None
            llm = None
            try:
                get_embedder = getattr(service, "get_runtime_embed_model", None)
                if callable(get_embedder):
                    embedder = _describe_component(get_embedder())

                get_llm = getattr(service, "get_runtime_llm", None)
                if callable(get_llm):
                    llm = _describe_component(get_llm())

                if embedder is None or llm is None:
                    sc = getattr(service, "service_context", None)
                    if sc is not None:
                        if embedder is None:
                            embedder = _describe_component(
                                getattr(sc, "embed_model", None)
                            )
                        if llm is None:
                            llm = _describe_component(getattr(sc, "llm", None))
            except Exception:
                pass

            last_query = None
            try:
                last_query = getattr(service, "_last_query_info", None)
            except Exception:
                last_query = None

            payload = {
                "success": True,
                "service_initialised": True,
                "requested_namespace": ns,
                "effective_namespace": effective_namespace,
                "backend": {
                    "class_name": service.__class__.__name__,
                    "module": service.__class__.__module__,
                },
                "persistence_dir": getattr(service, "persistence_dir", None),
                "index_persist_dir": index_persist_dir,
                "index_cached": index_cached,
                "embedder": embedder,
                "llm": llm,
                "openai_key_present": bool(os.getenv("OPENAI_API_KEY")),
                "last_query": last_query,
            }
            return jsonify(payload)

        except RAGBackendUnavailable as e:
            return (
                jsonify(
                    success=False,
                    error="rag_backend_unavailable",
                    message=str(e),
                ),
                503,
            )
        except Exception as e:
            return jsonify(success=False, error="unexpected", detail=str(e)), 500

    @app.route("/admin/chat_history_backfill", methods=["POST"])
    def admin_chat_history_backfill():
        """Backfill legacy chat history to the current user@org namespace and RAG.

        Logged-in only. Uses the current Flask session to derive user/org/namespace.
        Optional JSON body:
          {"max_sessions": int, "max_messages": int, "dry_run": bool}
        """
        try:
            from flask import session as flask_session
            from src.backend.services.namespace_service import derive_namespace
            from src.backend.services import chat_history_service

            sess_user_slug = _get_session_user_slug(flask_session)
            sess_user_concept_id = flask_session.get("user_concept_id")

            if not (sess_user_slug or sess_user_concept_id):
                return jsonify(error="Not authenticated"), 401

            sess_org_raw = flask_session.get(
                "organisation_concept_id"
            ) or flask_session.get("org_id")
            sess_org = _slug_from_maybe_concept_id(sess_org_raw)
            sess_role = flask_session.get("role_in_org")

            target_ns = flask_session.get("namespace")
            if not target_ns and sess_user_slug:
                target_ns = derive_namespace(sess_user_slug, sess_org)

            if not isinstance(target_ns, str) or not target_ns:
                return jsonify(error="Not authenticated"), 401

            user_concept_id = (
                sess_user_concept_id
                if isinstance(sess_user_concept_id, str) and sess_user_concept_id
                else (f"#V#{sess_user_slug}" if sess_user_slug else None)
            )
            if not user_concept_id:
                return jsonify(error="Not authenticated"), 401

            body = request.get_json(silent=True) or {}
            max_sessions = int(body.get("max_sessions", 10))
            max_messages = int(body.get("max_messages", 500))
            dry_run = bool(body.get("dry_run", False))

            res = chat_history_service.backfill_chat_history_for_user(
                user_concept_id=user_concept_id,
                target_namespace=target_ns,
                organisation_concept_id=sess_org,
                role_in_org=sess_role,
                max_sessions=max_sessions,
                max_messages=max_messages,
                dry_run=dry_run,
            )
            return jsonify(res)
        except Exception as e:
            return jsonify(error="unexpected", detail=str(e)), 500

    @app.route("/admin/chat_history_reindex", methods=["POST"])
    def admin_chat_history_reindex():
        """Reindex chat history messages for the current user/namespace into RAG.

        This is intended to repair older sessions that were never indexed, while
        keeping namespace isolation intact.

                Optional JSON body:
                    {
                        "max_sessions": int,
                        "max_messages": int,
                        "reset_counters": bool,
                        "dry_run": bool,
                        "session_ids": [str],

                        # Chunked mode (recommended for reliability):
                        # If provided, requires exactly one session_id.
                        "chunk_start": int,
                        "chunk_size": int
                    }
        """
        try:
            from flask import session as flask_session
            from src.backend.services.namespace_service import derive_namespace
            from src.backend.services import chat_history_service

            sess_user_slug = _get_session_user_slug(flask_session)
            sess_user_concept_id = flask_session.get("user_concept_id")

            if not (sess_user_slug or sess_user_concept_id):
                return jsonify(error="Not authenticated"), 401

            sess_org_raw = flask_session.get(
                "organisation_concept_id"
            ) or flask_session.get("org_id")
            sess_org = _slug_from_maybe_concept_id(sess_org_raw)
            sess_role = flask_session.get("role_in_org")

            target_ns = request.args.get("namespace") or flask_session.get("namespace")
            if not target_ns and sess_user_slug:
                target_ns = derive_namespace(sess_user_slug, sess_org)

            if not isinstance(target_ns, str) or not target_ns:
                return jsonify(error="Not authenticated"), 401

            # Prevent cross-user / cross-namespace actions.
            sess_ns = flask_session.get("namespace")
            if not sess_ns and sess_user_slug:
                sess_ns = derive_namespace(sess_user_slug, sess_org)
            if sess_ns != target_ns:
                return (
                    jsonify(error="namespace_mismatch", session_namespace=sess_ns),
                    403,
                )

            user_concept_id = (
                sess_user_concept_id
                if isinstance(sess_user_concept_id, str) and sess_user_concept_id
                else (f"#V#{sess_user_slug}" if sess_user_slug else None)
            )
            if not user_concept_id:
                return jsonify(error="Not authenticated"), 401

            body = request.get_json(silent=True) or {}
            max_sessions = int(body.get("max_sessions", 50))
            max_messages = int(body.get("max_messages", 5000))
            reset_counters = bool(body.get("reset_counters", True))
            dry_run = bool(body.get("dry_run", False))
            session_ids = body.get("session_ids")
            if not isinstance(session_ids, list):
                session_ids = None

            chunk_start = body.get("chunk_start")
            chunk_size = body.get("chunk_size")
            use_chunked = chunk_start is not None or chunk_size is not None

            if use_chunked:
                if (
                    not session_ids
                    or len(session_ids) != 1
                    or not isinstance(session_ids[0], str)
                ):
                    return (
                        jsonify(
                            error="invalid_request",
                            detail="chunked reindex requires exactly one session_id",
                        ),
                        400,
                    )
                sid = session_ids[0]
                try:
                    import time

                    t0 = time.monotonic()
                except Exception:
                    t0 = None

                try:
                    app.logger.info(
                        "[chat_history_reindex] chunk start user=%s ns=%s session=%s chunk_start=%s chunk_size=%s dry_run=%s",
                        user_concept_id,
                        target_ns,
                        sid,
                        int(chunk_start or 0),
                        int(chunk_size or 25),
                        bool(dry_run),
                    )
                except Exception:
                    pass

                res = chat_history_service.reindex_chat_history_session_chunk(
                    user_concept_id=user_concept_id,
                    target_namespace=target_ns,
                    organisation_concept_id=sess_org,
                    role_in_org=sess_role,
                    session_id=sid,
                    chunk_start=int(chunk_start or 0),
                    chunk_size=int(chunk_size or 25),
                    reset_counters=reset_counters,
                    dry_run=dry_run,
                )

                try:
                    elapsed_ms = (
                        int((time.monotonic() - t0) * 1000) if t0 is not None else None
                    )
                    app.logger.info(
                        "[chat_history_reindex] chunk done user=%s ns=%s session=%s attempted=%s ok=%s failed=%s next=%s done=%s elapsed_ms=%s errors=%s",
                        user_concept_id,
                        target_ns,
                        sid,
                        res.get("messages_indexed_attempted"),
                        res.get("messages_indexed_success"),
                        res.get("messages_indexed_failed"),
                        res.get("next_chunk_start"),
                        res.get("done"),
                        elapsed_ms,
                        len(res.get("errors") or []),
                    )
                except Exception:
                    pass
                return jsonify(res)

            res = chat_history_service.reindex_chat_history_for_user_namespace(
                user_concept_id=user_concept_id,
                target_namespace=target_ns,
                organisation_concept_id=sess_org,
                role_in_org=sess_role,
                session_ids=session_ids,
                max_sessions=max_sessions,
                max_messages=max_messages,
                reset_counters=reset_counters,
                dry_run=dry_run,
            )
            return jsonify(res)
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
        # Optional namespace filter: interaction_sessions may store either a user-only
        # namespace (#V#user) or a composite namespace (#V#user@org). For backwards
        # compatibility, if a composite namespace is provided we scope to BOTH values.
        ns = request.args.get("namespace")
        session_ns_values: list[str] | None = None
        if ns:
            session_ns_values = [ns]
            try:
                from src.backend.services.namespace_service import parse_namespace

                parsed = parse_namespace(ns)
                if parsed.get("user_id"):
                    user_only = f"#V#{parsed['user_id']}"
                    if user_only not in session_ns_values:
                        session_ns_values.append(user_only)
            except Exception:
                # If the namespace is not parseable, treat it as an opaque key.
                pass

        sess_filter = (
            {"namespace": {"$in": session_ns_values}} if session_ns_values else {}
        )
        result = {
            "sessions": sessions_coll.count_documents(sess_filter or {}),
            "scoped_sessions": sessions_coll.count_documents(sess_filter or {}),
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
            "session_namespace": (session_ns_values[0] if session_ns_values else None),
            "session_namespaces": session_ns_values,
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
        user_concept_id = payload.get("user_concept_id")
        organisation_concept_id = payload.get("organisation_concept_id")
        try:
            result = sync_to_chat_store(
                namespace=namespace,
                user_concept_id=user_concept_id,
                organisation_concept_id=organisation_concept_id,
            )
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
            from ..services.vontology_concept_stats_service import (
                get_vontology_concept_stats_cache_summary,
            )

            ttl_env = os.getenv("VONTOLOGY_COUNTS_TTL")
            size = (
                len(_INSTANCE_COUNTS_CACHE)
                if isinstance(_INSTANCE_COUNTS_CACHE, dict)
                else None
            )
            summary = get_vontology_concept_stats_cache_summary()
            counts_cache = {
                "size": size,
                "ttl_sec": int(ttl_env) if ttl_env and ttl_env.isdigit() else None,
                "stats_cache": summary,
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
        normalised_header_user = None
        header_user_validation = None
        header_user_raw_exact_exists = None
        header_user_normalised_exact_exists = None
        user_visibility_sample = None
        try:
            from flask import session as _session

            session_user = _session.get("user_concept_id")
            from ..security.access_control import (
                _normalise_concept_id,  # type: ignore
                _validate_person_concept,  # type: ignore
                get_effective_user_concept_id,
            )
            from ..services.concept_service import (
                _find_raw_concept_by_exact_concept_id,  # type: ignore
            )

            # Peek raw headers for fallback diagnostic (do not validate here)
            try:
                header_user = request.headers.get(
                    "X-User-Concept-ID"
                ) or request.headers.get("X-User-Client-ID")
            except Exception:
                header_user = None
            effective_user = get_effective_user_concept_id()
            if header_user:
                normalised_header_user = _normalise_concept_id(header_user)
                header_user_validation = _validate_person_concept(header_user)
                header_user_raw_exact_exists = bool(
                    _find_raw_concept_by_exact_concept_id(header_user)
                )
                if normalised_header_user:
                    header_user_normalised_exact_exists = bool(
                        _find_raw_concept_by_exact_concept_id(normalised_header_user)
                    )
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

        version_info = get_runtime_code_version_info()

        diag = {
            "status": "ok",
            "version": version_info.get("version"),
            "version_details": version_info,
            "pid": os.getpid(),
            "rss_mb": rss_mb,
            "thread_count": thread_count,
            "uptime_sec": uptime_sec,
            "session_user_concept_id": session_user,
            "effective_user_concept_id": effective_user,
            "header_user_concept_id": header_user,
            "normalised_header_user_concept_id": normalised_header_user,
            "header_user_validation": header_user_validation,
            "header_user_raw_exact_exists": header_user_raw_exact_exists,
            "header_user_normalised_exact_exists": header_user_normalised_exact_exists,
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
        try:
            from ..services.namespace_isolation_diagnostics_service import (
                get_namespace_isolation_diagnostics_snapshot,
            )

            diag["namespace_isolation_diagnostics"] = (
                get_namespace_isolation_diagnostics_snapshot(
                    include_recent_events=False
                )
            )
        except Exception as exc:
            diag["namespace_isolation_diagnostics"] = {
                "available": False,
                "error": type(exc).__name__,
            }
        # Durable workflow system status (best-effort)
        try:
            from ..workflows.durable.startup import get_system_status

            durable_status = get_system_status()
            diag["durable_workflows"] = durable_status
        except Exception as exc:
            diag["durable_workflows"] = {
                "error": str(exc),
                "available": False,
            }
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
        diag["internal_mcp_orchestrator_startup"] = app.config.get(
            "INTERNAL_MCP_ORCHESTRATOR_STATUS"
        )
        diag["durable_workflow_startup"] = app.config.get(
            "DURABLE_WORKFLOW_STARTUP_STATUS"
        )
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
                "timestamp": iso8601,
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
        return jsonify(get_runtime_code_version_info())

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

        # Gracefully stop durable workflow system before server shutdown
        try:
            _stop_durable_workflow_system()
        except Exception as exc:
            app.logger.warning("[shutdown] Durable workflow stop error: %s", exc)

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

    # --- Workflow Model Policy Diagnostics (JVNAUTOSCI-998) ---
    @app.route("/admin/policy_comparison")
    def admin_policy_comparison():
        """Compare JSON-based and graph-based workflow model policy representations.

        Returns a diagnostic report showing differences between the two.
        Used to validate graph parity before deprecating JSON fallback.
        """
        try:
            from ..services.workflow_policy_graph_service import (
                compare_policy_json_vs_graph,
            )

            policy_id = request.args.get(
                "policy_id", "#V#default_workflow_model_policy"
            )
            report = compare_policy_json_vs_graph(policy_id)
            return jsonify(report)
        except Exception as e:
            return jsonify(error=str(e), status="error"), 500

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
