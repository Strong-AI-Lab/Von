#!/usr/bin/env python3

import argparse
import faulthandler
from importlib import util as importlib_util
import importlib
import logging
import os
from pathlib import Path
import sys
import threading
import time
import traceback

from flask import abort, render_template

project_root_str = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
src_root = os.path.join(project_root_str, "src")
if src_root not in sys.path:
    sys.path.insert(0, src_root)

_runtime_env = importlib.import_module("src.backend.utils.runtime_env")
apply_repo_dotenv_overrides = _runtime_env.apply_repo_dotenv_overrides

# The Flask route import constructs the process-wide foreground admission
# service. Load its capacity inputs first so direct main.py launches preserve
# the same HTTP-thread headroom as run.sh launches, which already load .env.
apply_repo_dotenv_overrides(
    {
        "VON_WAITRESS_THREADS",
        "VON_TURN_HTTP_HEADROOM_THREADS",
        "VON_MAX_ACTIVE_TURNS_GLOBAL",
        "VON_MAX_ACTIVE_TURNS_PER_USER",
    }
)

_utils_flask = importlib.import_module("src.backend.server.utils_flask")
create_flask_app = _utils_flask.create_flask_app
build_browser_entry_url = _utils_flask.build_browser_entry_url
get_expert_tabs_enabled = importlib.import_module(
    "src.backend.services.feature_flags"
).get_expert_tabs_enabled


# Add src directory to Python path FIRST, before any backend imports

# Now backend imports can resolve

DEBUG_ATTACH_ENV = "VON_DEBUGPY"  # if set, enable debugpy attach
MEM_LOG_INTERVAL = int(os.environ.get("VON_MEM_LOG_INTERVAL", "60"))  # seconds
ENABLE_MEM_LOG = os.environ.get("VON_MEM_LOG", "1") not in ("0", "false", "False")

# --- Logging Setup --- ADDED BLOCK
PROJECT_ROOT = Path(project_root_str)
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE_NAME = "von_main.log"  # Specific log file name for this application
LOG_FILE_PATH = LOG_DIR / LOG_FILE_NAME

# Create logs directory if it doesn't exist
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Configure the root logger.
# All loggers will propagate to this by default.
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)  # Set the root logger to the lowest level

# Suppress noisy third-party DEBUG loggers that flood the log file (pymongo alone
# can produce tens of MB per minute at DEBUG, slowing startup and I/O).
for _noisy_logger_name in (
    # AWS SDK DEBUG records can include SigV4 Authorization headers.  Those
    # credentials are short-lived, but they must never be persisted in the
    # application log.
    "boto3",
    "botocore",
    "s3transfer",
    "pymongo",
    "urllib3",
    "httpcore",
    "httpx",
    # The OpenAI SDK logs complete request bodies at DEBUG, including private
    # document evidence embedded in model prompts.  Keep transport failures
    # visible without persisting those request bodies in local server logs.
    "openai",
):
    logging.getLogger(_noisy_logger_name).setLevel(logging.WARNING)

# File Handler - for detailed logging to a file
# Use 'w' mode to overwrite the log file each time the script runs for ease of debugging.
# We'll use 'a' mode once we are in production to append logs.
file_handler = logging.FileHandler(
    LOG_FILE_PATH, mode="w", encoding="utf-8"
)  # Use 'a' for append mode
file_handler.setLevel(logging.DEBUG)  # Log DEBUG and higher to file
file_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s"
)
file_handler.setFormatter(file_formatter)
root_logger.addHandler(file_handler)  # Add to root logger

# Console Handler - for less detailed logging to the console
console_handler = logging.StreamHandler(sys.stdout)  # Explicitly use sys.stdout
console_handler.setLevel(logging.INFO)  # Log INFO and higher to console
console_formatter = logging.Formatter("%(levelname)s: %(message)s")
console_handler.setFormatter(console_formatter)
root_logger.addHandler(console_handler)  # Add to root logger

# Get a logger for this script (or module).
# Its messages will also go through the root logger's handlers.
# This logger will use the above configuration.
logger = logging.getLogger(__name__)
logger.info("Logging initialized for von. File output to: %s", LOG_FILE_PATH)


def _apply_dotenv_overrides(keys: set[str]) -> None:
    """Load repo-root .env and override specific keys in os.environ.

    This keeps behaviour predictable on Windows, where editing .env is a common
    way to update credentials, but the parent PowerShell environment may still
    contain stale values.
    """

    applied_values = apply_repo_dotenv_overrides(keys)
    applied = len(applied_values)

    if applied:
        token_len = len(os.environ.get("ATLASSIAN_API_TOKEN", ""))
        logger.info(
            "Applied .env overrides for %s key(s). Jira token present=%s length=%s.",
            applied,
            bool(os.environ.get("ATLASSIAN_API_TOKEN")),
            token_len,
        )


# Ensure selected credentials can be updated via .env + restart
_apply_dotenv_overrides(
    {
        "ATLASSIAN_BASE_URL",
        "ATLASSIAN_SITE_BASE",
        "ATLASSIAN_EMAIL",
        "ATLASSIAN_API_EMAIL",
        "ATLASSIAN_API_TOKEN",
        "TAVILY_API_KEY",
        "VON_GMAIL_PROFILES",
        "VON_GMAIL_DEFAULT_PROFILE",
        "VON_LINKEDIN_RESOURCE_ID",
        "VON_LINKEDIN_OWNER_USER_CONCEPT_ID",
        "VON_LINKEDIN_MCP_PROJECT_DIR",
        "VON_LINKEDIN_MCP_COMMAND",
        "VON_LINKEDIN_MCP_TIMEOUT_SEC",
        "LINKEDIN_MJW_DB",
        "VON_INTERNAL_MCP_ENABLE",
        "VON_MCP_ALLOW_WRITES",
        "VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS",
        "VON_EXPERT_FOOTER_ENABLED",
        "VON_WORKFLOWS_TRACE_ENABLED",
        # Durable workflow runtime control must resolve from .env because the
        # local launcher and VS Code hosts do not reliably inherit it.
        "VON_DURABLE_WORKFLOWS_ENABLE",
        # Turn-stall / thread-starvation performance controls (JVNAUTOSCI-2383).
        # These shape Waitress thread sizing, durable-worker pacing, live-load
        # deferral, hot-poller caching, and chat-workflow warm. The launcher and
        # VS Code hosts do not reliably inherit them, so resolve from .env.
        "VON_WAITRESS_THREADS",
        "VON_TURN_HTTP_HEADROOM_THREADS",
        "VON_MAX_ACTIVE_TURNS_GLOBAL",
        "VON_MAX_ACTIVE_TURNS_PER_USER",
        "VON_MAX_QUEUED_TURNS_GLOBAL",
        "VON_MAX_QUEUED_TURNS_PER_USER",
        "VON_DURABLE_WORKER_POLL_INTERVAL",
        "VON_DURABLE_WORKER_PRIORITY_RESERVED_SLOTS",
        "VON_DURABLE_WORKER_PAUSE_BACKGROUND_UNDER_LIVE_LOAD",
        "VON_DURABLE_WORKER_LIVE_LOAD_THRESHOLD",
        "VON_DB_INFO_CACHE_TTL_SECONDS",
        "VON_EAGER_WARM_CHAT_WORKFLOWS",
        "VON_TURN_PIPELINE_MONITORING_SCHEDULE_ENABLE",
        "VON_TURN_PIPELINE_MONITORING_INTERVAL_SECONDS",
        "VON_TURN_PIPELINE_TIER1_REGRESSION_INTERVAL_SECONDS",
        "VON_TURN_PIPELINE_TIER1_CASE_SET",
        "VON_DEBUG_USER_PROMPT_LOADING",
        "VON_MODEL_REGISTRY_SNAPSHOT_CACHE_TTL_SECONDS",
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_ENABLED",
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_TTL_SECONDS",
        "VON_MODEL_REGISTRY_SNAPSHOT_MAX_STALE_SECONDS",
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH",
        "VON_DEFAULT_NAMESPACE",
        "VON_DETERMINISTIC_INTROSPECTION",
        "VON_AGENT_GMAIL_OAUTH_REDIRECT_URI",
        "VON_EXPERT_TABS_ENABLED",
        "VON_BROWSER_TEST_AUTH_ENABLED",
        "VON_BROWSER_TEST_PSEUDOUSER_NAME",
        "VON_BROWSER_TEST_PSEUDOUSER_EMAIL",
        "VON_BROWSER_TEST_PSEUDOUSER_CONCEPT_ID",
        "VON_BROWSER_TEST_ORGANISATION_CONCEPT_ID",
        # Swift/OpenStack blob-store configuration must resolve from .env on
        # Windows because launcher/session inheritance is inconsistent across
        # local runs, and missing these keys changes the failure mode.
        "VON_BLOB_STORE_BACKEND",
        "VON_BLOB_STORE_LOCAL_ROOT",
        "VON_DEBUG_PAYLOAD_BLOB_THRESHOLD_BYTES",
        "VON_DEBUG_TOOL_MESSAGE_BLOB_THRESHOLD_BYTES",
        "VON_WORKFLOW_PAYLOAD_BLOB_THRESHOLD_BYTES",
        # Async blob spillway (JVNAUTOSCI-2382): decouples response finalisation
        # from remote object-storage latency by buffering to local disk first.
        "VON_BLOB_SPILLWAY_ENABLED",
        "VON_BLOB_SPILLWAY_DIR",
        "VON_BLOB_SPILLWAY_MAX_RETRIES",
        "VON_BLOB_SPILLWAY_MIGRATE_INTERVAL_SECONDS",
        "VON_BLOB_SPILLWAY_CACHE_TTL_DAYS",
        "VON_SWIFT_CONTAINER",
        "VON_SWIFT_PREFIX",
        "VON_SWIFT_PUBLIC_BASE_URL",
        "OS_CLOUD",
        "OS_CLOUD_NAME",
        "OS_CLIENT_CONFIG_FILE",
        "OS_AUTH_URL",
        "OS_USERNAME",
        "OS_PASSWORD",
        "OS_PROJECT_NAME",
        "OS_USER_DOMAIN_NAME",
        "OS_PROJECT_DOMAIN_NAME",
        "OS_REGION_NAME",
        "OS_INTERFACE",
        "OS_APPLICATION_CREDENTIAL_ID",
        "OS_APPLICATION_CREDENTIAL_SECRET",
        "VON_S3_BUCKET",
        "VON_S3_PREFIX",
        "VON_S3_ENDPOINT_URL",
        "VON_S3_PUBLIC_BASE_URL",
        "VON_S3_REGION_NAME",
        "VON_S3_ADDRESSING_STYLE",
        "VON_S3_CONNECT_TIMEOUT_SECONDS",
        "VON_S3_READ_TIMEOUT_SECONDS",
        "VON_S3_MAX_ATTEMPTS",
        "VON_SWIFT_S3_FAILOVER_ENABLE",
        "VON_S3_ACCESS_KEY_ID",
        "VON_S3_SECRET_ACCESS_KEY",
        "VON_S3_SESSION_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        # GitHub proxy tokens — override early so _build_github_env() always
        # resolves from .env rather than relying on inherited shell env
        # (VS Code may set GITHUB_TOKEN to its own Copilot auth token).
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GITHUB_VON_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        # External-model credentials and default model overrides — allow .env
        # to override stale inherited shell values after a local restart.
        "OPENAI_API_KEY",
        "OPENAI_API_KEY_FILE",
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_FILE",
        "GOOGLE_API_KEY",
        "GOOGLE_API_KEY_FILE",
        "VON_DEFAULT_OLLAMA_MODEL",
        "VON_DEFAULT_OPENAI_MODEL",
        "VON_DEFAULT_GEMINI_MODEL",
    }
)

# Enable faulthandler early for low-level crash diagnostics
try:
    faulthandler.enable()
    logger.debug("Faulthandler enabled")
except Exception as _e:  # pragma: no cover
    logger.warning("Failed to enable faulthandler: %s", _e)

# Install a global exception hook to surface uncaught exceptions that might otherwise be swallowed
_orig_excepthook = sys.excepthook


def _global_excepthook(exc_type, exc, tb):  # pragma: no cover
    try:
        logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
    finally:
        _orig_excepthook(exc_type, exc, tb)


sys.excepthook = _global_excepthook

# Optional debug adapter attach (debugpy)
if os.environ.get(DEBUG_ATTACH_ENV):  # pragma: no cover
    try:
        import importlib

        # type: ignore[attr-defined]
        if importlib_util.find_spec("debugpy") is not None:
            debugpy = importlib.import_module("debugpy")  # type: ignore
            host = os.environ.get("VON_DEBUGPY_HOST", "127.0.0.1")
            port = int(os.environ.get("VON_DEBUGPY_PORT", "5678"))
            debugpy.listen((host, port))  # type: ignore[attr-defined]
            logger.warning(
                "debugpy listening on %s:%s (set %s=1)", host, port, DEBUG_ATTACH_ENV
            )
            if os.environ.get("VON_DEBUGPY_WAIT"):
                logger.warning("Waiting for debugger attach...")
                debugpy.wait_for_client()  # type: ignore[attr-defined]
        else:
            logger.warning(
                "DEBUGPY requested via %s but package not installed.", DEBUG_ATTACH_ENV
            )
    except Exception as _e:  # pragma: no cover
        logger.error("Failed to initialize debugpy: %s", _e)


def _periodic_mem_logger():  # pragma: no cover
    if not ENABLE_MEM_LOG:
        return
    try:
        import importlib

        # type: ignore[attr-defined]
        if importlib_util.find_spec("psutil") is None:
            logger.debug("psutil not installed; memory logger disabled.")
            return
        psutil = importlib.import_module("psutil")  # type: ignore
        proc = psutil.Process()  # type: ignore[attr-defined]
        while True:
            rss = proc.memory_info().rss / (1024 * 1024)
            logger.info("[diag] RSS=%.1fMB threads=%d", rss, proc.num_threads())
            time.sleep(MEM_LOG_INTERVAL)
    except Exception as _e:
        logger.debug("Memory logger exiting: %s", _e)


threading.Thread(target=_periodic_mem_logger, name="von-mem-log", daemon=True).start()
# --- End Logging Setup ---

# Now we can import the necessary functions/modules


def main():
    import socket

    parser = argparse.ArgumentParser(description="Run the Flask Chat LLM server.")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Host address to bind the server to."
    )
    parser.add_argument(
        "--port", type=int, default=5000, help="Port number to run the server on."
    )
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode.")
    # Add arguments for LLM client configuration if needed (e.g., API keys, host)
    # parser.add_argument("--llm-host", default="http://localhost:11434", help="Ollama host URL.")
    # parser.add_argument("--llm-model", help="Default LLM model (see VON_DEFAULT_OLLAMA_MODEL env var).")
    args = parser.parse_args()

    # Check if the port is already in use, and if so, increment until a free port is found
    def is_port_in_use(port, host="127.0.0.1"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex((host, port)) == 0

    original_port = args.port
    while is_port_in_use(args.port, args.host):
        logger.warning(f"Port {args.port} is already in use. Trying next port...")
        args.port += 1
    if args.port != original_port:
        print(
            f"[INFO] Port {original_port} is in use. The UI will start on port {args.port} instead."
        )

    # --- Instantiate the LLM Client ---
    # Use the unified LLM client factory that respects user settings
    try:
        from src.backend.languagemodels.llm_interface import get_llm_client  # type: ignore

        llm_client = get_llm_client()
        logger.info(f"Using {type(llm_client).__name__} for LLM interactions.")
    except Exception as e:  # pragma: no cover - startup failure path
        logger.error("Failed to initialize LLM client: %s", e, exc_info=True)
        sys.exit(1)  # Exit if client fails to initialize

    # Resolve represented model/API-profile authority before the HTTP server
    # can accept a caller-owned provider deadline. A cold worktree may spend
    # time hydrating here; a bounded last-known represented snapshot returns
    # immediately and refreshes outside the first user call.
    try:
        from src.backend.services.model_registry_service import (
            preload_model_registry_snapshot,
        )

        registry_preload = preload_model_registry_snapshot()
        logger.info(
            "Model registry startup preload ready=%s source=%s "
            "cache_state=%s read_strategy=%s read_phases=%s models=%s "
            "duration_ms=%s.",
            registry_preload.get("ready"),
            registry_preload.get("source"),
            registry_preload.get("cache_state"),
            registry_preload.get("read_strategy"),
            registry_preload.get("read_phases"),
            registry_preload.get("model_count"),
            registry_preload.get("preload_duration_ms"),
        )
    except Exception as exc:
        logger.warning("Model registry startup preload failed: %s", exc)

    # Create the Flask app, injecting the client's methods
    app = create_flask_app(
        list_models_func=llm_client.list_models,
        generate_func=llm_client.generate,
    )

    # ADDED: Route for the settings tab
    # This one is kept as it appears first and is correctly placed before blueprint registration.
    @app.route("/settings")
    def serve_settings_tab_route():
        """Serves the settings tab."""
        return render_template("settings_tab.html")

    # ADDED: Routes for individual tab templates (modular architecture)
    @app.route("/chat_tab")
    def serve_chat_tab_route():
        """Serves the chat tab template."""
        return render_template("chat_tab.html")

    @app.route("/vontology_tab")
    def serve_vontology_tab_route():
        """Serves the vontology tab template."""
        if not get_expert_tabs_enabled():
            abort(404)
        return render_template("vontology_tab.html")

    @app.route("/import_export_tab")
    def serve_import_export_tab_route():
        """Serves the import/export tab template."""
        if not get_expert_tabs_enabled():
            abort(404)
        return render_template("import_export_tab.html")

    @app.route("/entity_tab")
    def serve_entity_tab_route():
        """Serves the entity tab template."""
        return render_template("entity_tab.html")

    @app.route("/concept_tab")
    def serve_concept_tab_route():
        """Serves the concept tab template."""
        return render_template("concept_tab.html")

    @app.route("/annotation_tab")
    def serve_annotation_tab_route():
        """Serves the annotation demo tab template."""
        if not get_expert_tabs_enabled():
            abort(404)
        return render_template("annotation_tab.html")

    @app.route("/settings_tab")
    def serve_settings_tab_content_route():
        """Serves the settings tab content for AJAX loading."""
        return render_template("settings_tab.html")

    # Initialize Flask app and other configurations as before
    # app = Flask(__name__, static_folder='../../frontend/web/von_interface/static', template_folder='../../frontend/web/von_interface/templates')
    # REFACTORING_NOTE: Assuming create_flask_app handles the Flask app creation with static/template folders.
    # If not, the above line or similar Flask() instantiation is needed.
    # We will rely on create_flask_app to return the app object.

    # ... (other app configurations like CORS, etc.)

    # Register blueprints
    # app.register_blueprint(chat_bp, url_prefix='/chat') # Removed: chat_bp is already registered in create_flask_app
    # app.register_blueprint(settings_bp) # Removed: settings_bp is already registered in create_flask_app
    # app.register_blueprint(vontology_bp, url_prefix='/vontology') # Removed: vontology_bp is already registered in create_flask_app
    # ... (other blueprints)

    logger.info(f"Starting Flask server on http://{args.host}:{args.port}")
    logger.debug(
        "Args: host=%s port=%s debug=%s cwd=%s python=%s",
        args.host,
        args.port,
        args.debug,
        os.getcwd(),
        sys.executable,
    )

    # --- Auto-open browser ONCE on server startup (not per-request) ---
    import threading
    import webbrowser

    # def open_browser_once():
    #   url = f"http://{args.host}:{args.port}/von/" # MODIFIED
    #  threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # if not os.environ.get("VON_SKIP_BROWSER_LAUNCH"):
    #   open_browser_once()
    # --- Delay browser launch until app is live ---

    def open_browser_once_delayed():
        import time
        import requests

        def check_server_and_open():
            url = build_browser_entry_url(args.host, args.port)
            for i in range(10):
                try:
                    response = requests.get(url, timeout=1)
                    if response.status_code == 200:
                        print(f"[INFO] Flask ready, opening browser to {url}")
                        webbrowser.open(url)
                        return
                except Exception:
                    time.sleep(1)
            print(
                "[WARN] Flask did not become ready in time, skipping browser launch."
            )

        threading.Thread(target=check_server_and_open).start()

    if not os.environ.get("VON_SKIP_BROWSER_LAUNCH"):
        open_browser_once_delayed()

    # Use waitress or another production server instead of app.run for non-debug
    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)
    else:
        try:
            import importlib

            serve = None
            # type: ignore[attr-defined]
            if importlib_util.find_spec("waitress") is not None:
                waitress_mod = importlib.import_module("waitress")  # type: ignore
                serve = getattr(waitress_mod, "serve", None)
            if callable(serve):
                # Increase threads from the Waitress default of 4 to handle
                # long-lived SSE connections and concurrent polling without
                # starving the live chat path.
                #
                # Each SSE subscription (e.g. /workflows/instances/stream) holds
                # a worker thread for its whole lifetime, and the UI also issues
                # many short Mongo-backed polls (settings/db/info, rag_status,
                # history_sessions, capability index). With Atlas RTT of ~200ms
                # per query these polls each occupy a thread for several hundred
                # ms to multiple seconds. When the in-process durable worker is
                # also busy, a 16-thread pool can be fully drained, which leaves
                # the /von/progress endpoint unscheduled for minutes and makes
                # turns appear to stall ("Awaiting visible progress"). A larger
                # default keeps the live progress path schedulable under load.
                # Tune via VON_WAITRESS_THREADS. See JVNAUTOSCI-2383.
                try:
                    num_threads = int(os.environ.get("VON_WAITRESS_THREADS", "32"))
                except (TypeError, ValueError):
                    num_threads = 32
                if num_threads < 1:
                    num_threads = 32
                logger.info(
                    "Running with Waitress production server (threads=%d).", num_threads
                )
                serve(app, host=args.host, port=args.port, threads=num_threads)
            else:
                logger.warning(
                    "Waitress not found. Falling back to Flask development server (not recommended for production)."
                )
                app.run(host=args.host, port=args.port, debug=False)
        except Exception as e:
            # Capture catastrophic server failures
            logger.critical("Server crashed: %s", e, exc_info=True)
            # Dump traceback to stderr too for process manager visibility
            traceback.print_exc()
            raise


if __name__ == "__main__":
    main()
