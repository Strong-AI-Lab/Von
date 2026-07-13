"""Manual end-to-end test for the todo_refresh workflow.

Instantiates a real orchestrator with a real gateway and LLM client,
then runs the todo_refresh workflow through execute_workflow().

Prerequisites:
  - MongoDB running (VON_DB_NAME defaults to test_von_db here for safety)
  - .env loaded (LLM API key, etc.)
  - Optionally: VON_GMAIL_DEFAULT_PROFILE set for Gmail fetch

Usage (from project root):
  $env:VON_DB_NAME = 'test_von_db'
  pdm run python scripts/manual_test_todo_refresh.py

  # With Gmail profile:
  $env:VON_GMAIL_DEFAULT_PROFILE = 'your_profile'
  pdm run python scripts/manual_test_todo_refresh.py

  # Skip cache (force full refresh):
  pdm run python scripts/manual_test_todo_refresh.py --force-refresh
"""

import sys
import os
import json
import logging
import argparse
import time

# --- Path Setup ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
src_root = os.path.join(project_root, "src")
sys.path.insert(0, project_root)
sys.path.insert(0, src_root)

# Load .env if python-dotenv is available.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(project_root, ".env"))
except ImportError:
    pass

# Safety: default to test DB.
if not os.environ.get("VON_DB_NAME"):
    os.environ["VON_DB_NAME"] = "test_von_db"
    print("[SAFETY] VON_DB_NAME not set — defaulting to 'test_von_db'")

# Enable internal MCP.
os.environ.setdefault("VON_INTERNAL_MCP_ENABLE", "1")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-30s %(levelname)-5s %(message)s",
)
logger = logging.getLogger("manual_test_todo_refresh")


def main():
    parser = argparse.ArgumentParser(
        description="Run todo_refresh workflow end-to-end."
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Set todo_refresh_needed=True to bypass cache freshness check.",
    )
    parser.add_argument(
        "--user-namespace",
        default=None,
        help="Vontology user namespace (e.g. #V#michael_witbrock). "
        "Defaults to VON_DEFAULT_NAMESPACE env var.",
    )
    parser.add_argument(
        "--gmail-profile",
        default=None,
        help="Gmail profile name. Defaults to VON_GMAIL_DEFAULT_PROFILE env var.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="LLM model override (e.g. gpt-4.1-mini). Defaults to VON_DEFAULT_MODEL.",
    )
    args = parser.parse_args()

    # ---- Imports (after path setup) ----
    from src.backend.db.connection_manager import get_db
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        InternalMCPChatOrchestrator,
        build_default_catalogue,
    )
    from src.backend.workflows.definitions import TODO_REFRESH_WORKFLOW_ID

    # ---- DB ----
    db = get_db()
    if db is None:
        logger.error("Failed to connect to MongoDB. Is it running?")
        sys.exit(1)
    db_name = os.environ.get("VON_DB_NAME", "?")
    logger.info("Connected to MongoDB (db=%s)", db_name)

    # ---- Gateway + Orchestrator ----
    catalogue = build_default_catalogue()
    transport = InternalMCPTransport()
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=transport,
        enabled=True,
    )
    logger.info("Gateway: %d methods registered.", len(catalogue.list_methods()))

    gmail_profile = (
        args.gmail_profile or os.environ.get("VON_GMAIL_DEFAULT_PROFILE") or None
    )
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        logger=logger,
        max_tool_invocations=8,
        default_gmail_profile=gmail_profile,
    )
    logger.info("Orchestrator initialised.")

    # ---- LLM Client ----
    # Try to obtain the same client the server would use.
    try:
        from src.backend.languagemodels.llm_interface import get_llm_client

        model = args.model or os.environ.get("VON_DEFAULT_MODEL", "gpt-4.1-mini")
        llm_client = get_llm_client()
        logger.info("LLM client ready (model=%s)", model)
    except Exception as exc:
        logger.warning("Could not create LLM client: %s", exc)
        logger.warning("Handlers needing LLM will degrade gracefully.")
        llm_client = None
        model = None

    # ---- User namespace ----
    user_namespace = args.user_namespace or os.environ.get("VON_DEFAULT_NAMESPACE")
    logger.info("User namespace: %s", user_namespace or "(none)")
    logger.info("Gmail profile:  %s", gmail_profile or "(none — fetch_gmail will skip)")

    # ---- Build workflow data ----
    data: dict = {}
    if args.force_refresh:
        data["todo_refresh_needed"] = True
        logger.info("Force-refresh: todo_refresh_needed=True")

    # ---- Execute ----
    logger.info("=" * 60)
    logger.info("Running workflow: %s", TODO_REFRESH_WORKFLOW_ID)
    logger.info("=" * 60)

    t0 = time.perf_counter()
    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
    )

    # This executable is an explicit local-operator gateway, not an
    # authenticated user request. Mark that provenance rather than relying on
    # execute_workflow to trust an unscoped namespace claim implicitly.
    with bind_internal_mcp_actor_context_source(
        "trusted_operator_payload_fallback"
    ):
        result = orchestrator.execute_workflow(
            TODO_REFRESH_WORKFLOW_ID,
            data=data,
            llm_client=llm_client,
            model=model,
            user_namespace=user_namespace,
        )
    elapsed = time.perf_counter() - t0

    # ---- Report ----
    logger.info("=" * 60)
    if result is None:
        logger.error("Workflow returned None (definition not found in registry?).")
        sys.exit(1)

    logger.info("Workflow completed in %.2f s", elapsed)
    logger.info("Final state:  %s", result.final_state)
    logger.info("Completed:    %s", result.completed)

    # Pretty-print the final data dict (the accumulated workflow context).
    safe_data = {}
    for k, v in result.data.items():
        try:
            json.dumps(v)
            safe_data[k] = v
        except (TypeError, ValueError):
            safe_data[k] = repr(v)

    logger.info("Final data:\n%s", json.dumps(safe_data, indent=2, default=str))

    if result.error:
        logger.warning("Workflow error: %s", result.error)

    logger.info("Done.")


if __name__ == "__main__":
    main()
