import logging
import argparse
import importlib
import os
import sys

# --- Path Setup ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
src_root = os.path.join(project_root, "src")
sys.path.insert(0, project_root)
sys.path.insert(0, src_root)

get_db = importlib.import_module("src.backend.db.connection_manager").get_db
WorkflowRegistry = importlib.import_module(
    "src.backend.workflows.workflow_registry"
).WorkflowRegistry
register_default_workflows = importlib.import_module(
    "src.backend.workflows.definitions"
).register_default_workflows
WorkflowInstanceManager = importlib.import_module(
    "src.backend.workflows.durable.instance_manager"
).WorkflowInstanceManager

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("manual_run_workflow")


def run(workflow_id: str):
    logger.info(f"Preparing to run workflow: {workflow_id}")

    # 1. Connect to DB (via get_db lazy init)
    db = get_db()
    if db is None:
        logger.error("Failed to connect to Mongo DB.")
        sys.exit(1)
    logger.info("Database connected.")

    # 2. Setup Registry
    # We don't actually need the registry for creating an instance (the manager persists it),
    # but it's good practice to ensure the ID is valid.
    registry = WorkflowRegistry()
    register_default_workflows(registry)

    definition = registry.get(workflow_id)
    if definition is None:
        logger.warning(
            f"Workflow ID '{workflow_id}' is not in the default registry. Proceeding anyway (database definition might exist)."
        )
    else:
        logger.info(f"Verified definition: {definition.purpose}")

    # 3. Create Instance
    manager = WorkflowInstanceManager()

    # Dummy user context (could be arguments)
    user_id = "#V#user_manual_trigger"
    org_id = "#V#org_manual_trigger"
    namespace = "#V#manual_ns"

    logger.info(f"Creating instance for user={user_id}...")
    instance_id = manager.create_instance(
        workflow_id=workflow_id,
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        inputs={"manual_trigger": True},
    )

    logger.info("Instance created successfully!")
    logger.info(f"Instance ID: {instance_id}")
    logger.info("----------------------------------------------------------------")
    logger.info(
        "To process this instance, the durable workflow worker must be running."
    )
    logger.info("Ensure the following is set in your .env or environment:")
    logger.info("  VON_DURABLE_WORKFLOWS_ENABLE=1")
    logger.info("----------------------------------------------------------------")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manually trigger a workflow.")
    parser.add_argument(
        "--workflow-id",
        default="#V#todo_refresh_workflow",
        help="Workflow ID to trigger.",
    )
    args = parser.parse_args()

    try:
        run(args.workflow_id)
    except Exception as e:
        logger.exception(f"Failed to run workflow: {e}")
        sys.exit(1)
