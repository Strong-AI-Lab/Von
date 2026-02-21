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
import os
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List

from .. import WorkflowRegistry, register_default_workflows
from ..action_registry import ActionRegistry, WorkflowActionResult
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
from .planning_workflow import (
    get_planning_workflow_registration,
    register_planning_actions,
)
from ..vontology_loader import (
    build_workflow_process_graph,
    discover_workflow_ids,
    load_workflow_definition_from_vontology,
)
from ..workflow_concept_authority_service import (
    bootstrap_workflow_concepts,
    build_workflow_concept_authority_report,
)

logger = logging.getLogger(__name__)
_inventory_lock = Lock()
_last_inventory_snapshot: Dict[str, Any] | None = None
_durable_mcp_gateway_lock = Lock()
_durable_mcp_gateway: Any | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_workflow_from_vontology(
    *,
    registry: WorkflowRegistry,
    workflow_id: str,
    replace_existing: bool = False,
) -> tuple[bool, str | None]:
    """Try to register a single Vontology-defined workflow into ``registry``.

    Returns:
        (registered_or_already_present, error_code_or_none)
    """

    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return False, "invalid_workflow_id"

    workflow_id = workflow_id.strip()
    existing_registration = registry.get_registration(workflow_id)
    if existing_registration is not None and not replace_existing:
        return True, None

    definition = load_workflow_definition_from_vontology(workflow_id)
    if definition is None:
        return False, "definition_not_loadable"

    from ..workflow_registry import WorkflowRegistration

    registration = WorkflowRegistration(
        workflow_id=definition.workflow_id,
        definition=definition,
        purpose=definition.purpose,
        source="vontology",
    )
    if replace_existing:
        registry.register_or_replace(registration)
        return True, None

    registered = registry.register_if_absent(registration)
    if not registered:
        return False, "registration_conflict"
    return True, None


def _get_or_build_durable_mcp_gateway():
    """Return a lazy singleton internal MCP gateway for durable fallback actions."""

    global _durable_mcp_gateway
    if _durable_mcp_gateway is not None:
        return _durable_mcp_gateway

    with _durable_mcp_gateway_lock:
        if _durable_mcp_gateway is not None:
            return _durable_mcp_gateway

        from ...integrations.internal_mcp.catalogue import build_default_catalogue
        from ...integrations.internal_mcp.gateway import InternalMCPGateway
        from ...integrations.internal_mcp.transport import InternalMCPTransport

        _durable_mcp_gateway = InternalMCPGateway(
            catalogue=build_default_catalogue(),
            transport=InternalMCPTransport(),
            enabled=True,
        )
        return _durable_mcp_gateway


def _durable_mcp_fallback_action(request: Any) -> WorkflowActionResult:
    """Invoke unregistered durable workflow actions as internal MCP tools.

    This mirrors the orchestrator fallback behaviour so Vontology-authored
    durable workflows (including newly authored workflow-creation outputs) can
    execute MCP tool actions without requiring explicit Python action wiring.
    """

    tool_name = str(getattr(request, "action_id", "") or "").strip()
    if not tool_name:
        return WorkflowActionResult(status="failed", error="missing_action_id")

    payload = dict(getattr(request, "inputs", {}) or {})
    environment = getattr(request, "environment", None)
    user_namespace = getattr(environment, "user_namespace", None)
    if isinstance(user_namespace, str) and user_namespace.strip():
        payload.setdefault("namespace", user_namespace.strip())

    try:
        gateway = _get_or_build_durable_mcp_gateway()
        result = gateway.invoke(tool_name, payload)
        try:
            from ..workflow_baseline_telemetry import (
                record_generic_fallback_mcp_invocation,
            )

            record_generic_fallback_mcp_invocation(success=True)
        except Exception:
            pass
        return WorkflowActionResult(
            status="success",
            outputs={
                "mcp_result": result.payload,
                "mcp_tool": tool_name,
                "mcp_duration_ms": result.duration_ms,
                "result": result.payload,
            },
            duration_ms=result.duration_ms,
        )
    except Exception as exc:
        try:
            from ..workflow_baseline_telemetry import (
                record_generic_fallback_mcp_invocation,
            )

            record_generic_fallback_mcp_invocation(success=False)
        except Exception:
            pass
        logger.warning(
            "[durable_workflow] MCP fallback invoke failed for %s: %s",
            tool_name,
            exc,
        )
        return WorkflowActionResult(
            status="failed",
            error=f"mcp_invoke_failed:{tool_name}:{exc}",
        )


def _build_workflow_parity_inventory(
    *,
    registry: WorkflowRegistry,
    discovered_workflow_ids: List[str],
    authority_report: Dict[str, Any] | None = None,
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

    workflow_authority = (
        authority_report
        if isinstance(authority_report, dict)
        else {
            "drift_detected": False,
            "counts": {
                "missing_concepts": 0,
                "missing_required_type": 0,
            },
        }
    )
    authority_counts = (
        workflow_authority.get("counts", {})
        if isinstance(workflow_authority.get("counts"), dict)
        else {}
    )
    authority_missing_concepts = int(authority_counts.get("missing_concepts", 0))
    authority_missing_required_type = int(
        authority_counts.get("missing_required_type", 0)
    )
    authority_drift = bool(workflow_authority.get("drift_detected"))
    summary_lines.append(
        (
            f"Workflow authority: missing_concepts={authority_missing_concepts} "
            f"missing_required_type={authority_missing_required_type}"
        )
    )

    diagnostics_reason_codes: list[str] = []
    if registry_only_ids:
        diagnostics_reason_codes.append("registry_only")
    if vontology_only_ids:
        diagnostics_reason_codes.append("vontology_only")
    if identity_only_ids:
        diagnostics_reason_codes.append("identity_only")
    if authority_drift:
        diagnostics_reason_codes.append("workflow_authority")

    drift_detected = bool(diagnostics_reason_codes)

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
            "authority_missing_concepts": authority_missing_concepts,
            "authority_missing_required_type": authority_missing_required_type,
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
        "workflow_authority": workflow_authority,
        "summary_lines": summary_lines,
        "summary_text": "\n".join(summary_lines),
        "diagnostics": {
            "drift_detected": drift_detected,
            "severity": "warning" if drift_detected else "ok",
            "reason_codes": diagnostics_reason_codes,
        },
    }


def _apply_workflow_parity_policy(inventory_snapshot: Dict[str, Any]) -> None:
    """Apply warn/fail parity drift policy and annotate inventory diagnostics.

    ``VON_WORKFLOW_PARITY_ENFORCEMENT`` controls behaviour:
    - ``warn`` (default): log warning on drift
    - ``fail`` / ``strict`` / ``error``: raise RuntimeError on drift
    - ``off`` / ``none``: do not warn or fail
    """
    mode = os.getenv("VON_WORKFLOW_PARITY_ENFORCEMENT", "warn").strip().lower()
    diagnostics = inventory_snapshot.get("diagnostics")
    drift_detected = bool(
        isinstance(diagnostics, dict) and diagnostics.get("drift_detected")
    )
    reason_codes: list[str] = []
    if isinstance(diagnostics, dict):
        maybe_reason_codes = diagnostics.get("reason_codes")
        if isinstance(maybe_reason_codes, list):
            reason_codes = [item for item in maybe_reason_codes if isinstance(item, str)]
    reason_suffix = ",".join(reason_codes)
    summary_text = inventory_snapshot.get("summary_text", "workflow parity snapshot generated")
    message = f"{summary_text}; drift_reasons={reason_suffix or 'none'}"

    inventory_snapshot["parity_policy"] = {
        "mode": mode,
        "drift_detected": drift_detected,
    }

    if not drift_detected:
        logger.info("[workflow_parity] %s", summary_text)
        return

    if mode in {"off", "none"}:
        logger.info("[workflow_parity] drift detected but policy mode=%s", mode)
        return

    logger.warning("[workflow_parity] %s", message)
    if mode in {"fail", "strict", "error"}:
        raise RuntimeError(f"workflow_parity_drift_detected:{reason_suffix or 'unknown'}")


def get_workflow_registry_inventory_snapshot() -> Dict[str, Any]:
    """Return the latest workflow parity inventory snapshot."""
    with _inventory_lock:
        if _last_inventory_snapshot is None:
            return {}
        return copy.deepcopy(_last_inventory_snapshot)


# ---------------------------------------------------------------------------
# Workflow Registry
# ---------------------------------------------------------------------------


def _build_workflow_registry(*, allow_bootstrap: bool) -> WorkflowRegistry:
    """Build the unified workflow registry.

    Args:
        allow_bootstrap:
            When False, skip concept bootstrap writes and keep this call path
            read-only for diagnostics/introspection surfaces.
    """
    registry = WorkflowRegistry()

    # 1. Built-in conversation-turn workflows
    register_default_workflows(registry)

    # 2. Durable-specific workflows
    registry.register(get_rag_sync_workflow_registration())
    registry.register(get_considerations_workflow_registration())
    registry.register(get_enrichment_workflow_registration())
    registry.register(get_rumination_workflow_registration())
    registry.register(get_planning_workflow_registration())

    # 3. Vontology-discovered workflows
    discovered_workflow_ids: List[str] = []
    try:
        discovered_workflow_ids = discover_workflow_ids()

        for wf_id in discovered_workflow_ids:
            try:
                existing_registration = registry.get_registration(wf_id)
                existing_source = ""
                if existing_registration is not None:
                    existing_source = str(
                        getattr(existing_registration, "source", "") or ""
                    ).strip()
                replace_existing = bool(existing_registration) and (
                    existing_source.lower() != "vontology"
                )

                registered, error_code = register_workflow_from_vontology(
                    registry=registry,
                    workflow_id=wf_id,
                    replace_existing=replace_existing,
                )
                if registered:
                    if replace_existing:
                        logger.info(
                            "Registered Vontology workflow override: %s (replaced source=%s)",
                            wf_id,
                            existing_source or "unknown",
                        )
                    elif existing_registration is None:
                        logger.info("Registered Vontology workflow: %s", wf_id)
                    else:
                        logger.debug(
                            "Vontology workflow %s already authoritative.",
                            wf_id,
                        )
                elif error_code:
                    logger.debug(
                        "Skipping Vontology workflow %s (%s)",
                        wf_id,
                        error_code,
                    )
            except Exception as e:
                logger.warning("Failed to load Vontology workflow %s: %s", wf_id, e)

    except Exception as e:
        logger.error("Failed to discover Vontology workflows: %s", e)

    bootstrap_report: Dict[str, Any] = {
        "counts": {
            "registry_workflows": len(list(registry.all_workflow_ids())),
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "errors": 0,
        },
        "required_type_ids": [],
        "preferred_type_id": None,
        "created_workflow_ids": [],
        "updated_workflow_ids": [],
        "unchanged_workflow_ids": [],
        "errors_by_workflow_id": {},
    }
    bootstrap_enabled = os.getenv("VON_WORKFLOW_CONCEPT_BOOTSTRAP_ENABLE", "1")
    bootstrap_enabled = bootstrap_enabled.strip().lower() in {"1", "true", "yes", "on"}
    bootstrap_allowed = bool(allow_bootstrap and bootstrap_enabled)
    if bootstrap_allowed:
        try:
            bootstrap_report = bootstrap_workflow_concepts(registry=registry)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "workflow_concept_bootstrap_failed: %s",
                exc,
            )
            bootstrap_report["errors_by_workflow_id"] = {"__bootstrap__": str(exc)}
            bootstrap_report["counts"]["errors"] = 1
    bootstrap_report["enabled"] = bootstrap_allowed

    authority_report = build_workflow_concept_authority_report(registry=registry)
    authority_report["bootstrap"] = bootstrap_report

    inventory_snapshot = _build_workflow_parity_inventory(
        registry=registry,
        discovered_workflow_ids=discovered_workflow_ids,
        authority_report=authority_report,
    )
    with _inventory_lock:
        global _last_inventory_snapshot
        _last_inventory_snapshot = inventory_snapshot

    _apply_workflow_parity_policy(inventory_snapshot)

    return registry


def build_workflow_registry() -> WorkflowRegistry:
    """Build a unified WorkflowRegistry with mutating bootstrap enabled by policy."""
    return _build_workflow_registry(allow_bootstrap=True)


def build_workflow_registry_read_only() -> WorkflowRegistry:
    """Build a unified WorkflowRegistry without concept bootstrap side effects."""
    return _build_workflow_registry(allow_bootstrap=False)


# Keep the old name as an alias for backward compatibility.
build_durable_workflow_registry = build_workflow_registry
build_durable_workflow_registry_read_only = build_workflow_registry_read_only


# ---------------------------------------------------------------------------
# Action Registry
# ---------------------------------------------------------------------------


def build_durable_action_registry() -> ActionRegistry:
    """Build an ActionRegistry containing durable workflow action handlers.

    This registers handlers for background/durable workflows only
    (rag_sync, considerations, enrichment, rumination, planning).  The
    orchestrator's conversation-turn
    handlers (narration, missing-tool-call, write-policy, todo-refresh)
    are added separately via ``ActionRegistry.merge()`` in the
    orchestrator constructor.
    """
    registry = ActionRegistry()
    register_rag_sync_actions(registry)
    register_considerations_actions(registry)
    register_enrichment_actions(registry)
    register_rumination_actions(registry)
    register_planning_actions(registry)
    # Keep durable action routing aligned with orchestrator routing: if an
    # action ID is not explicitly registered, treat it as an MCP tool name.
    registry.set_fallback_handler(_durable_mcp_fallback_action)
    return registry
