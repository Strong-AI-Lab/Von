"""Unified factory for workflow and action registries.

Centralises the logic for building a single WorkflowRegistry and
ActionRegistry that serve both conversation-turn and durable execution
paths.  All consumers (orchestrator, durable worker, MCP tools, REST
routes) should obtain registries from this module.

See JVNAUTOSCI-922 Phase 1 for the motivation and design.
"""

from __future__ import annotations

import logging
from .. import WorkflowRegistry, register_default_workflows
from ..action_registry import ActionRegistry
from .rag_sync_workflow import (
    get_rag_sync_workflow_registration,
    register_rag_sync_actions,
)
from .considerations_workflow import (
    get_considerations_workflow_registration,
    register_considerations_actions,
)
from .enrichment_workflow import (
    get_enrichment_workflow_registration,
    register_enrichment_actions,
)
from .rumination_workflow import (
    get_rumination_workflow_registration,
    register_rumination_actions,
)
from ..vontology_loader import (
    discover_workflow_ids,
    load_workflow_definition_from_vontology,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workflow Registry
# ---------------------------------------------------------------------------


def build_workflow_registry() -> WorkflowRegistry:
    """Build a unified WorkflowRegistry with all workflow sources.

    Registration order:
    1. Built-in conversation-turn workflows (definitions.py)
    2. Durable-specific workflows (rag_sync, considerations)
    3. Vontology-discovered workflows (skip already-registered IDs)

    This is the single authoritative factory — both the orchestrator and
    durable subsystem should call this instead of maintaining separate
    registries.
    """
    registry = WorkflowRegistry()

    # 1. Built-in conversation-turn workflows
    register_default_workflows(registry)

    # 2. Durable-specific workflows
    registry.register(get_rag_sync_workflow_registration())
    registry.register(get_considerations_workflow_registration())
    registry.register(get_enrichment_workflow_registration())
    registry.register(get_rumination_workflow_registration())

    # 3. Vontology-discovered workflows
    try:
        workflow_ids = discover_workflow_ids()
        registered_ids = set(registry.all_workflow_ids())

        for wf_id in workflow_ids:
            if wf_id in registered_ids:
                logger.debug(
                    "Skipping Vontology workflow %s: already registered via code.",
                    wf_id,
                )
                continue

            try:
                definition = load_workflow_definition_from_vontology(wf_id)
                if definition:
                    from ..workflow_registry import WorkflowRegistration

                    reg = WorkflowRegistration(
                        workflow_id=definition.workflow_id,
                        definition=definition,
                        purpose=definition.purpose,
                        source="vontology",
                    )
                    registry.register(reg)
                    logger.info("Registered Vontology workflow: %s", wf_id)
            except Exception as e:
                logger.warning("Failed to load Vontology workflow %s: %s", wf_id, e)

    except Exception as e:
        logger.error("Failed to discover Vontology workflows: %s", e)

    return registry


# Keep the old name as an alias for backward compatibility.
build_durable_workflow_registry = build_workflow_registry


# ---------------------------------------------------------------------------
# Action Registry
# ---------------------------------------------------------------------------


def build_durable_action_registry() -> ActionRegistry:
    """Build an ActionRegistry containing durable workflow action handlers.

    This registers handlers for background/durable workflows only
    (rag_sync, considerations).  The orchestrator's conversation-turn
    handlers (narration, missing-tool-call, write-policy, todo-refresh)
    are added separately via ``ActionRegistry.merge()`` in the
    orchestrator constructor.
    """
    registry = ActionRegistry()
    register_rag_sync_actions(registry)
    register_considerations_actions(registry)
    register_enrichment_actions(registry)
    register_rumination_actions(registry)
    return registry
