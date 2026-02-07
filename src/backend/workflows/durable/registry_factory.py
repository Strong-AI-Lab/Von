"""Unified factory for workflow and action registries.

Centralises the logic for building a single WorkflowRegistry and
ActionRegistry that serve both conversation-turn and durable execution
paths.  All consumers (orchestrator, durable worker, MCP tools, REST
routes) should obtain registries from this module.

See JVNAUTOSCI-922 Phase 1 for the motivation and design.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List

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
    build_workflow_process_graph,
    discover_workflow_ids,
    load_workflow_definition_from_vontology,
)

logger = logging.getLogger(__name__)
_inventory_lock = Lock()
_last_inventory_snapshot: Dict[str, Any] | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_workflow_parity_inventory(
    *,
    registry: WorkflowRegistry,
    discovered_workflow_ids: List[str],
) -> Dict[str, Any]:
    """Build a parity/inventory report for workflow registration vs Vontology."""
    registry_ids = sorted(set(registry.all_workflow_ids()))
    discovered_ids = sorted(
        wid for wid in set(discovered_workflow_ids) if isinstance(wid, str) and wid
    )
    registry_id_set = set(registry_ids)
    discovered_id_set = set(discovered_ids)

    overlap_ids = sorted(registry_id_set.intersection(discovered_id_set))
    registry_only_ids = sorted(registry_id_set - discovered_id_set)
    vontology_only_ids = sorted(discovered_id_set - registry_id_set)

    graph_complete_ids: list[str] = []
    identity_only_ids: list[str] = []
    graph_warnings: dict[str, list[str]] = {}
    for workflow_id in discovered_ids:
        try:
            graph, warnings = build_workflow_process_graph(workflow_id)
            steps = graph.get("steps") if isinstance(graph, dict) else None
            initial_step = graph.get("initial_step") if isinstance(graph, dict) else None
            has_graph = (
                isinstance(graph, dict)
                and isinstance(initial_step, str)
                and isinstance(steps, list)
                and len(steps) > 0
            )
            if has_graph:
                graph_complete_ids.append(workflow_id)
            else:
                identity_only_ids.append(workflow_id)
            if warnings:
                graph_warnings[workflow_id] = [str(item) for item in warnings[:8]]
        except Exception as exc:
            identity_only_ids.append(workflow_id)
            graph_warnings[workflow_id] = [f"graph_build_error:{exc}"]

    source_counts: dict[str, int] = {}
    source_by_workflow_id: dict[str, str] = {}
    for workflow_id in registry_ids:
        source = "unknown"
        try:
            registration = registry.get_registration(workflow_id)
            if registration and isinstance(registration.source, str) and registration.source:
                source = registration.source
        except Exception:
            source = "unknown"
        source_by_workflow_id[workflow_id] = source
        source_counts[source] = source_counts.get(source, 0) + 1

    summary_lines = [
        (
            f"Workflow parity: registry={len(registry_ids)} "
            f"discovered={len(discovered_ids)} overlap={len(overlap_ids)} "
            f"registry_only={len(registry_only_ids)} "
            f"vontology_only={len(vontology_only_ids)}"
        ),
        (
            f"Representation coverage: graph_complete={len(graph_complete_ids)} "
            f"identity_only={len(identity_only_ids)}"
        ),
    ]

    return {
        "generated_at_utc": _utc_now_iso(),
        "counts": {
            "registry": len(registry_ids),
            "vontology_discovered": len(discovered_ids),
            "overlap": len(overlap_ids),
            "registry_only": len(registry_only_ids),
            "vontology_only": len(vontology_only_ids),
            "graph_complete": len(graph_complete_ids),
            "identity_only": len(identity_only_ids),
        },
        "registry_workflow_ids": registry_ids,
        "vontology_discovered_workflow_ids": discovered_ids,
        "overlap_workflow_ids": overlap_ids,
        "registry_only_workflow_ids": registry_only_ids,
        "vontology_only_workflow_ids": vontology_only_ids,
        "representation": {
            "graph_complete_workflow_ids": sorted(set(graph_complete_ids)),
            "identity_only_workflow_ids": sorted(set(identity_only_ids)),
            "graph_warnings_by_workflow_id": graph_warnings,
        },
        "registry_sources": {
            "counts": source_counts,
            "source_by_workflow_id": source_by_workflow_id,
        },
        "summary_lines": summary_lines,
        "summary_text": "\n".join(summary_lines),
    }


def get_workflow_registry_inventory_snapshot() -> Dict[str, Any]:
    """Return the latest workflow parity inventory snapshot."""
    with _inventory_lock:
        if _last_inventory_snapshot is None:
            return {}
        return copy.deepcopy(_last_inventory_snapshot)


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
    discovered_workflow_ids: List[str] = []
    try:
        discovered_workflow_ids = discover_workflow_ids()
        registered_ids = set(registry.all_workflow_ids())

        for wf_id in discovered_workflow_ids:
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

    inventory_snapshot = _build_workflow_parity_inventory(
        registry=registry,
        discovered_workflow_ids=discovered_workflow_ids,
    )
    with _inventory_lock:
        global _last_inventory_snapshot
        _last_inventory_snapshot = inventory_snapshot

    counts = inventory_snapshot.get("counts", {})
    if counts.get("vontology_only", 0) or counts.get("identity_only", 0):
        logger.warning(
            "[workflow_parity] %s",
            inventory_snapshot.get("summary_text", "workflow parity snapshot generated"),
        )
    else:
        logger.info(
            "[workflow_parity] %s",
            inventory_snapshot.get("summary_text", "workflow parity snapshot generated"),
        )

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
