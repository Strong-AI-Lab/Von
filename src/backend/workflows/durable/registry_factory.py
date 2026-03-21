"""Unified factory for workflow and action registries.

Centralises the logic for building a single WorkflowRegistry and
ActionRegistry that serve both conversation-turn and durable execution
paths.  All consumers (orchestrator, durable worker, MCP tools, REST
routes) should obtain registries from this module.

See JVNAUTOSCI-922 Phase 1 for the motivation and design.
"""

from __future__ import annotations

import copy
from functools import lru_cache
import json
import logging
import os
import threading
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List

from .. import WorkflowRegistry
from ..workflow_registry import LazyWorkflowRegistration
from ..action_registry import ActionRegistry, WorkflowActionResult
from ..mcp_tool_bridge import workflow_action_result_from_mcp_payload
from ..vontology_loader import (
    build_workflow_process_graph,
    discover_workflow_ids,
    load_workflow_definition_from_vontology,
    batch_fetch_workflow_purposes,
    resolve_workflow_description,
)
from ..workflow_concept_authority_service import (
    build_workflow_concept_authority_report,
)
from ..workflow_description_quality_service import (
    assess_workflow_description_quality,
    summarise_workflow_description_quality,
)
from ..workflow_purity_report import build_workflow_purity_report

logger = logging.getLogger(__name__)
_inventory_lock = Lock()
_last_inventory_snapshot: Dict[str, Any] | None = None
_durable_mcp_gateway_lock = Lock()
_durable_mcp_gateway: Any | None = None

_EXPECTED_AUTHORITATIVE_FILE_COPY_WORKFLOW_IDS: tuple[str, ...] = (
    "#V#file_copy_typing_workflow",
    "#V#file_copy_upload_classification_workflow",
    "#V#file_copy_upload_handler_workflow",
    "#V#file_copy_interpretation_workflow",
)

_EXPECTED_AUTHORITATIVE_REASONING_RECOVERY_WORKFLOW_IDS: tuple[str, ...] = (
    "#V#planning_workflow",
    "#V#rumination_workflow",
    "#V#parent_specificity_concept_dossier_workflow",
    "#V#parent_specificity_rumination_workflow",
    "#V#workflow_discovery_gap_recovery_workflow",
    "#V#workflow_gap_test_workflow",
)
_EXPECTED_AUTHORITATIVE_SUPPORT_MAINTENANCE_WORKFLOW_IDS: tuple[str, ...] = (
    "#V#rag_text_relation_sync_workflow",
    "#V#enrichment_workflow",
    "#V#workflow_introspection_maintenance_workflow",
    "#V#entity_identity_resolution_workflow",
    "#V#jira_task_incremental_import_workflow",
)


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


def _build_expected_authoritative_workflow_report(
    *,
    workflow_ids: tuple[str, ...],
) -> Dict[str, Any]:
    """Report whether required authoritative workflows resolve from Vontology.

    Runtime registry construction is intentionally read-only. Missing workflows
    must surface as diagnostics rather than being materialised from Python
    registration helpers.
    """

    resolved_workflow_ids: list[str] = []
    missing_workflow_ids: list[str] = []
    for workflow_id in workflow_ids:
        definition = load_workflow_definition_from_vontology(workflow_id)
        if definition is None:
            missing_workflow_ids.append(workflow_id)
        else:
            resolved_workflow_ids.append(workflow_id)

    return {
        "enabled": False,
        "reason": "runtime_bootstrap_removed",
        "workflow_ids": list(workflow_ids),
        "resolved_workflow_ids": resolved_workflow_ids,
        "missing_workflow_ids": missing_workflow_ids,
        "counts": {
            "required": len(workflow_ids),
            "resolved": len(resolved_workflow_ids),
            "missing": len(missing_workflow_ids),
        },
    }


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
        return workflow_action_result_from_mcp_payload(
            tool_name=tool_name,
            payload=result.payload,
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
    description_records: list[dict[str, Any]] = []
    for workflow_id in registry_ids:
        source = "unknown"
        registration = None
        try:
            registration = registry.get_registration(workflow_id)
            if registration and isinstance(registration.source, str) and registration.source:
                source = registration.source
        except Exception:
            source = "unknown"
        source_by_workflow_id[workflow_id] = source
        source_counts[source] = source_counts.get(source, 0) + 1
        registration_purpose = getattr(registration, "purpose", None)
        definition = getattr(registration, "definition", None)
        definition_purpose = getattr(definition, "purpose", None)
        description, description_source = resolve_workflow_description(
            workflow_id,
            workflow_source=source,
            registration_purpose=registration_purpose,
            definition_purpose=definition_purpose,
        )
        description_records.append(
            {
                "workflow_id": workflow_id,
                "description": description,
                "description_source": description_source,
                "description_quality": assess_workflow_description_quality(
                    description=description,
                    description_source=description_source,
                ),
            }
        )

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

    workflow_purity = build_workflow_purity_report(registry=registry)
    workflow_description_quality = summarise_workflow_description_quality(
        description_records
    )
    workflow_purity_counters = workflow_purity.get("counters", {})
    workflow_purity_baseline = workflow_purity.get("baseline", {})
    workflow_purity_comparison = (
        workflow_purity_baseline.get("comparison", {})
        if isinstance(workflow_purity_baseline, dict)
        else {}
    )
    if isinstance(workflow_purity_counters, dict):
        summary_lines.append(
            (
                "Workflow purity: "
                f"built_in_registration_count={int(workflow_purity_counters.get('built_in_registration_count', 0))} "
                f"remaining_python_workflow_family_count={int(workflow_purity_counters.get('remaining_python_workflow_family_count', 0))} "
                f"direct_instance_create_callsite_count={int(workflow_purity_counters.get('direct_instance_create_callsite_count', 0))} "
                f"env_event_binding_count={int(workflow_purity_counters.get('env_event_binding_count', 0))} "
                f"legacy_selector_mode_count={int(workflow_purity_counters.get('legacy_selector_mode_count', 0))} "
                f"builtin_capability_override_count={int(workflow_purity_counters.get('builtin_capability_override_count', 0))} "
                f"non_vontology_discoverable_workflow_count={int(workflow_purity_counters.get('non_vontology_discoverable_workflow_count', 0))}"
            )
        )
    quality_counts = (
        workflow_description_quality.get("counts", {})
        if isinstance(workflow_description_quality, dict)
        else {}
    )
    summary_lines.append(
        (
            "Workflow description quality: "
            f"retrieval_ready={int(quality_counts.get('retrieval_ready', 0))} "
            f"developing={int(quality_counts.get('developing', 0))} "
            f"minimal={int(quality_counts.get('minimal', 0))} "
            f"stub={int(quality_counts.get('stub', 0))} "
            f"under_specified={int(quality_counts.get('under_specified', 0))}"
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
    if isinstance(workflow_purity_comparison, dict) and workflow_purity_comparison.get(
        "regression_detected"
    ):
        diagnostics_reason_codes.append("workflow_purity_regression")

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
        "workflow_description_quality": workflow_description_quality,
        "workflow_authority": workflow_authority,
        "workflow_purity": workflow_purity,
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
    - ``warn``: log warning on drift
    - ``fail`` / ``strict`` / ``error``: raise RuntimeError on drift
    - ``off`` / ``none``: do not warn or fail
    """
    mode = os.getenv("VON_WORKFLOW_PARITY_ENFORCEMENT", "fail").strip().lower()
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

    workflow_purity = inventory_snapshot.get("workflow_purity")
    if isinstance(workflow_purity, dict):
        logger.info(
            "[workflow_purity] %s",
            json.dumps(workflow_purity, sort_keys=True),
        )

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


def _infer_vontology_workflow_ids_from_registry(
    registry: WorkflowRegistry,
) -> List[str]:
    workflow_ids: set[str] = set()
    try:
        workflow_ids.update(registry.lazy_workflow_ids())
    except Exception:
        pass

    try:
        all_workflow_ids = list(registry.all_workflow_ids())
    except Exception:
        all_workflow_ids = []

    for workflow_id in all_workflow_ids:
        try:
            source_value = registry.get_registration_source(
                workflow_id,
                resolve_lazy=False,
            )
        except Exception:
            source_value = None
        source = str(source_value or "").strip().lower()
        if source == "vontology":
            workflow_ids.add(workflow_id)

    return sorted(workflow_ids)


def _build_pending_workflow_inventory_snapshot(
    *,
    registry: WorkflowRegistry,
    discovered_workflow_ids: List[str] | None = None,
) -> Dict[str, Any]:
    """Return a lightweight placeholder until background parity work finishes.

    Operator-facing read surfaces should stay responsive even when the full
    parity snapshot has not been published yet. This payload preserves the
    stable shape expected by clients while making the deferred-build state
    explicit instead of performing an expensive synchronous graph scan.
    """

    registry_ids = sorted(set(registry.all_workflow_ids()))
    discovered_ids = (
        sorted(
            {
                wid
                for wid in (discovered_workflow_ids or [])
                if isinstance(wid, str) and wid.strip()
            }
        )
        or _infer_vontology_workflow_ids_from_registry(registry)
    )
    summary_lines = [
        "Workflow parity inventory pending background build.",
        f"Registry workflows available now: {len(registry_ids)}.",
        f"Discovered Vontology workflow IDs: {len(discovered_ids)}.",
    ]
    return {
        "generated_at_utc": _utc_now_iso(),
        "build_state": "pending_background_build",
        "counts": {
            "registry": len(registry_ids),
            "vontology_discovered": len(discovered_ids),
            "overlap": 0,
            "registry_only": 0,
            "vontology_only": 0,
            "graph_complete": 0,
            "identity_only": 0,
            "authority_missing_concepts": 0,
            "authority_missing_required_type": 0,
        },
        "registry_workflow_ids": registry_ids,
        "vontology_discovered_workflow_ids": discovered_ids,
        "overlap_workflow_ids": [],
        "registry_only_workflow_ids": [],
        "vontology_only_workflow_ids": [],
        "representation": {
            "graph_complete_workflow_ids": [],
            "identity_only_workflow_ids": [],
            "graph_warnings_by_workflow_id": {},
        },
        "registry_sources": {"counts": {}, "source_by_workflow_id": {}},
        "workflow_description_quality": {
            "build_state": "pending_background_build",
            "counts": {
                "total": len(registry_ids),
                "stub": 0,
                "minimal": 0,
                "developing": 0,
                "retrieval_ready": 0,
                "under_specified": 0,
                "missing": 0,
            },
            "counts_by_label": {},
            "counts_by_source": {},
            "under_specified_workflow_ids": [],
            "missing_workflow_ids": [],
        },
        "workflow_authority": {
            "build_state": "pending_background_build",
            "drift_detected": False,
            "counts": {"missing_concepts": 0, "missing_required_type": 0},
        },
        "workflow_purity": {
            "build_state": "pending_background_build",
            "counters": {},
            "baseline": {},
        },
        "summary_lines": summary_lines,
        "summary_text": "\n".join(summary_lines),
        "diagnostics": {
            "drift_detected": False,
            "severity": "pending",
            "reason_codes": ["inventory_pending_background_build"],
        },
        "parity_policy": {
            "mode": os.getenv("VON_WORKFLOW_PARITY_ENFORCEMENT", "fail")
            .strip()
            .lower(),
            "drift_detected": False,
        },
    }


def get_or_build_workflow_registry_inventory_snapshot(
    *,
    registry: WorkflowRegistry | None = None,
    discovered_workflow_ids: List[str] | None = None,
    allow_sync_build: bool = True,
) -> Dict[str, Any]:
    """Return the latest inventory snapshot or build one synchronously.

    Read/introspection surfaces can reach the registry before the deferred
    bootstrap/parity thread has published `_last_inventory_snapshot`. When that
    happens, build the same inventory shape synchronously from the already-built
    registry so callers still receive a stable diagnostics payload.
    """

    snapshot = get_workflow_registry_inventory_snapshot()
    if snapshot:
        return snapshot
    if registry is None:
        return snapshot
    if not allow_sync_build:
        return _build_pending_workflow_inventory_snapshot(
            registry=registry,
            discovered_workflow_ids=discovered_workflow_ids,
        )

    inferred_workflow_ids = (
        list(discovered_workflow_ids)
        if isinstance(discovered_workflow_ids, list)
        else _infer_vontology_workflow_ids_from_registry(registry)
    )
    authority_report = build_workflow_concept_authority_report(registry=registry)
    inventory_snapshot = _build_workflow_parity_inventory(
        registry=registry,
        discovered_workflow_ids=inferred_workflow_ids,
        authority_report=authority_report,
    )
    _apply_workflow_parity_policy(inventory_snapshot)
    with _inventory_lock:
        global _last_inventory_snapshot
        _last_inventory_snapshot = copy.deepcopy(inventory_snapshot)
    return inventory_snapshot


def _register_python_defined_workflows(registry: WorkflowRegistry) -> None:
    """Register the current Python-defined workflow surface into ``registry``."""

    # Production runtime registration is now Vontology-authoritative.
    # Python workflow definitions remain only as bootstrap/test support and are
    # not registered onto the production registry path.
    return None


def _build_workflow_purity_source_snapshot() -> WorkflowRegistry:
    """Build a deterministic registry snapshot for purity reporting.

    The workflow-purity report is a regression gate over code-authored runtime
    authority surfaces. It must stay usable in standalone operator runs even if
    live Vontology discovery or authoritative graph loading is slow. Keep this
    snapshot intentionally DB-free and limited to code-registered workflow
    sources only.
    """

    registry = WorkflowRegistry()
    _register_python_defined_workflows(registry)
    return registry


def build_workflow_purity_registry_snapshot() -> WorkflowRegistry:
    """Build the registry snapshot used by standalone workflow-purity checks.

    This intentionally excludes live workflow discovery, authoritative graph
    loading, and deferred registry diagnostics. Those paths are valuable for
    runtime parity checks, but they make the purity report unsuitable as a fast
    operator-visible regression gate.
    """

    return _build_workflow_purity_source_snapshot()


# ---------------------------------------------------------------------------
# Workflow Registry
# ---------------------------------------------------------------------------


def _build_workflow_registry(
    *,
    allow_bootstrap: bool,
    force_background_deferred_work: bool = False,
) -> WorkflowRegistry:
    """Build the unified workflow registry.

    Args:
        allow_bootstrap:
            Retained for backwards compatibility. Runtime registry construction
            is now read-only regardless of this flag so workflow authority must
            come from already-materialised Vontology artefacts.
    """
    registry = WorkflowRegistry(
        definition_loader=load_workflow_definition_from_vontology,
    )
    requested_bootstrap = bool(allow_bootstrap)

    _register_python_defined_workflows(registry)

    # 3. Vontology-discovered workflows — lazy registration
    #    (JVNAUTOSCI-1424 Phase 1: defer expensive per-workflow DB loads)
    discovered_workflow_ids: List[str] = []
    lazy_count = 0
    eager_override_count = 0
    try:
        discovered_workflow_ids = discover_workflow_ids()

        for wf_id in discovered_workflow_ids:
            try:
                existing_registration = registry.get_registration(wf_id)
                if existing_registration is not None:
                    existing_source = str(
                        getattr(existing_registration, "source", "") or ""
                    ).strip()
                    if existing_source.lower() == "vontology":
                        # Already Vontology-authoritative — skip.
                        continue
                    # Non-Vontology source — eagerly load the Vontology
                    # override (rare: typically <5 workflows).
                    registered, error_code = register_workflow_from_vontology(
                        registry=registry,
                        workflow_id=wf_id,
                        replace_existing=True,
                    )
                    if registered:
                        eager_override_count += 1
                        logger.info(
                            "Registered Vontology workflow override: %s (replaced source=%s)",
                            wf_id,
                            existing_source or "unknown",
                        )
                    elif error_code:
                        logger.debug(
                            "Skipping Vontology workflow override %s (%s)",
                            wf_id,
                            error_code,
                        )
                    continue

                # Not yet registered — register lazily.
                lazy_reg = LazyWorkflowRegistration(
                    workflow_id=wf_id,
                    source="vontology",
                )
                if registry.register_lazy(lazy_reg):
                    lazy_count += 1

            except Exception as e:
                logger.warning("Failed to register Vontology workflow %s: %s", wf_id, e)

    except Exception as e:
        logger.error("Failed to discover Vontology workflows: %s", e)

    logger.info(
        "Workflow registry built: %d eager, %d lazy (%d Vontology overrides). "
        "Discovered %d Vontology workflow IDs.",
        len(list(registry.eager_workflow_ids())),
        lazy_count,
        eager_override_count,
        len(discovered_workflow_ids),
    )

    # 4. Deferred work — bootstrap + parity inventory in a daemon thread.
    #    These operations trigger lazy-loading of definitions so they must
    #    NOT block the registry return path (JVNAUTOSCI-1424).
    _launch_deferred_registry_work(
        registry=registry,
        discovered_workflow_ids=discovered_workflow_ids,
        expected_authoritative_file_copy_workflow_ids=(
            _EXPECTED_AUTHORITATIVE_FILE_COPY_WORKFLOW_IDS
        ),
        expected_authoritative_reasoning_recovery_workflow_ids=(
            _EXPECTED_AUTHORITATIVE_REASONING_RECOVERY_WORKFLOW_IDS
        ),
        expected_authoritative_support_maintenance_workflow_ids=(
            _EXPECTED_AUTHORITATIVE_SUPPORT_MAINTENANCE_WORKFLOW_IDS
        ),
        requested_bootstrap=requested_bootstrap,
        force_background=force_background_deferred_work,
    )

    return registry


def _launch_deferred_registry_work(
    *,
    registry: WorkflowRegistry,
    discovered_workflow_ids: List[str],
    expected_authoritative_file_copy_workflow_ids: tuple[str, ...],
    expected_authoritative_reasoning_recovery_workflow_ids: tuple[str, ...],
    expected_authoritative_support_maintenance_workflow_ids: tuple[str, ...],
    requested_bootstrap: bool,
    force_background: bool = False,
) -> None:
    """Launch background thread for parity inventory and capability indexing.

    Runtime workflow bootstrap has been removed; the deferred work now only
    builds diagnostics and the capability index without mutating workflow
    authority during registry construction.
    """

    def _deferred_work() -> None:
        try:
            bootstrap_report: Dict[str, Any] = {
                "enabled": False,
                "requested": requested_bootstrap,
                "reason": "runtime_bootstrap_removed",
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

            expected_authoritative_file_copy_report = (
                _build_expected_authoritative_workflow_report(
                    workflow_ids=expected_authoritative_file_copy_workflow_ids,
                )
            )
            expected_authoritative_reasoning_recovery_report = (
                _build_expected_authoritative_workflow_report(
                    workflow_ids=(
                        expected_authoritative_reasoning_recovery_workflow_ids
                    ),
                )
            )
            expected_authoritative_support_maintenance_report = (
                _build_expected_authoritative_workflow_report(
                    workflow_ids=(
                        expected_authoritative_support_maintenance_workflow_ids
                    ),
                )
            )

            authority_report = build_workflow_concept_authority_report(
                registry=registry,
            )
            authority_report["bootstrap"] = bootstrap_report
            authority_report["expected_authoritative_file_copy_workflows"] = (
                expected_authoritative_file_copy_report
            )
            authority_report["expected_authoritative_reasoning_recovery_workflows"] = (
                expected_authoritative_reasoning_recovery_report
            )
            authority_report["expected_authoritative_support_maintenance_workflows"] = (
                expected_authoritative_support_maintenance_report
            )

            inventory_discovered_workflow_ids = sorted(
                set(discovered_workflow_ids).union(
                    _infer_vontology_workflow_ids_from_registry(registry)
                )
            )
            inventory_snapshot = _build_workflow_parity_inventory(
                registry=registry,
                discovered_workflow_ids=inventory_discovered_workflow_ids,
                authority_report=authority_report,
            )
            with _inventory_lock:
                global _last_inventory_snapshot
                _last_inventory_snapshot = inventory_snapshot

            _apply_workflow_parity_policy(inventory_snapshot)

            # JVNAUTOSCI-1424 Phase 2: Build the workflow capability index
            # for RAG-first routing.  This is deferred to avoid blocking
            # startup — the index is populated from the registry after
            # bootstrap and parity work completes.
            try:
                # Batch-fetch descriptions for lazy workflows so the
                # capability index has searchable text.
                lazy_ids = list(registry.lazy_workflow_ids())
                if lazy_ids:
                    purposes = batch_fetch_workflow_purposes(lazy_ids)
                    for wf_id, purpose_text in purposes.items():
                        lazy_reg = registry._lazy.get(wf_id)  # type: ignore[attr-defined]
                        if lazy_reg and not lazy_reg.purpose:
                            lazy_reg.purpose = purpose_text

                from ...services.workflow_capability_service import (
                    get_workflow_capability_index,
                )

                cap_index = get_workflow_capability_index()
                cap_count = cap_index.index_from_registry(registry)
                logger.info(
                    "[workflow_capability_index] Built with %d entries.",
                    cap_count,
                )
            except Exception as exc:
                logger.warning(
                    "workflow_capability_index_build_failed: %s",
                    exc,
                )

            logger.info(
                "Deferred workflow registry work completed: "
                "%d eager workflows after background loading.",
                len(list(registry.eager_workflow_ids())),
            )
        except Exception as exc:
            logger.error(
                "Deferred workflow registry work failed: %s",
                exc,
                exc_info=True,
            )

    parity_mode = os.getenv("VON_WORKFLOW_PARITY_ENFORCEMENT", "fail").strip().lower()
    if not force_background and parity_mode in {"fail", "strict", "error"}:
        _deferred_work()
        return

    thread = threading.Thread(
        target=_deferred_work,
        name="workflow-registry-deferred",
        daemon=True,
    )
    thread.start()


def build_workflow_registry() -> WorkflowRegistry:
    """Build the unified runtime WorkflowRegistry.

    Kept as the main entry point for runtime consumers. The build path is now
    read-only and expects workflow authority to already exist in Vontology.
    """
    return _build_workflow_registry(allow_bootstrap=True)


def build_workflow_registry_read_only(
    *,
    defer_parity_work: bool = False,
) -> WorkflowRegistry:
    """Build the unified WorkflowRegistry without side effects.

    ``defer_parity_work=True`` keeps operator-facing read surfaces responsive by
    forcing deferred inventory/parity work into a daemon thread even when
    parity enforcement is configured as strict. Callers that need an immediate
    fully-built parity snapshot should use the default synchronous behaviour.
    """
    return _build_workflow_registry(
        allow_bootstrap=False,
        force_background_deferred_work=defer_parity_work,
    )


# Keep the old name as an alias for backward compatibility.
build_durable_workflow_registry = build_workflow_registry
build_durable_workflow_registry_read_only = build_workflow_registry_read_only


# ---------------------------------------------------------------------------
# Action Registry
# ---------------------------------------------------------------------------


def _register_durable_action_modules(registry: ActionRegistry) -> None:
    """Lazily import durable action families only when action execution is needed.

    Read-only workflow inspection surfaces import this module too, so keep the
    heavy action/workflow module graph out of the import path unless an actual
    ActionRegistry is being built.
    """

    from ..skill_interop import register_skill_interop_actions
    from .control_flow_actions import register_control_flow_actions
    from .enrichment_workflow import register_enrichment_actions
    from .entity_identity_resolution_workflow import (
        register_entity_identity_resolution_actions,
    )
    from .file_copy_interpretation_workflow import (
        register_file_copy_interpretation_actions,
    )
    from .file_copy_typing_workflow import register_file_copy_typing_actions
    from .file_copy_upload_classification_workflow import (
        register_file_copy_upload_classification_actions,
    )
    from .file_copy_upload_handler_workflow import (
        register_file_copy_upload_handler_actions,
    )
    from .jira_task_full_reconciliation_workflow import (
        register_jira_task_full_reconciliation_actions,
    )
    from .jira_task_incremental_import_workflow import (
        register_jira_task_incremental_import_actions,
    )
    from .paper_representation_workflow import register_paper_representation_actions
    from .parent_specificity_concept_dossier_workflow import (
        register_parent_specificity_concept_dossier_actions,
    )
    from .parent_specificity_rumination_workflow import (
        register_parent_specificity_rumination_actions,
    )
    from .planning_workflow import register_planning_actions
    from .rag_sync_workflow import register_rag_sync_actions
    from .rumination_workflow import register_rumination_actions
    from .subworkflow_actions import register_subworkflow_actions
    from .talk_representation_workflow import register_talk_representation_actions
    from .testing_workflow_actions import register_testing_workflow_actions
    from .workflow_creation_workflow import register_workflow_creation_actions
    from .workflow_gap_recovery_workflow import (
        register_workflow_gap_recovery_actions,
    )
    from .workflow_introspection_maintenance_workflow import (
        register_workflow_introspection_maintenance_actions,
    )

    register_rag_sync_actions(registry)
    register_enrichment_actions(registry)
    register_rumination_actions(registry)
    register_planning_actions(registry)
    register_workflow_introspection_maintenance_actions(registry)
    register_file_copy_typing_actions(registry)
    register_file_copy_upload_classification_actions(registry)
    register_file_copy_upload_handler_actions(registry)
    register_file_copy_interpretation_actions(registry)
    register_entity_identity_resolution_actions(registry)
    register_jira_task_incremental_import_actions(registry)
    register_jira_task_full_reconciliation_actions(registry)
    register_parent_specificity_concept_dossier_actions(registry)
    register_parent_specificity_rumination_actions(registry)
    register_paper_representation_actions(registry)
    register_talk_representation_actions(registry)
    register_workflow_gap_recovery_actions(registry)
    register_control_flow_actions(registry, definition_loader=_resolve_subworkflow_definition)
    register_subworkflow_actions(registry, definition_loader=_resolve_subworkflow_definition)
    register_workflow_creation_actions(registry)
    register_testing_workflow_actions(registry)
    register_skill_interop_actions(registry)


def build_durable_action_registry() -> ActionRegistry:
    """Build an ActionRegistry containing durable workflow action handlers.

    This registers handlers for background/durable workflows only
    (rag_sync, enrichment, rumination, planning).  The
    orchestrator's conversation-turn
    handlers (narration, missing-tool-call, write-policy, todo-refresh)
    are added separately via ``ActionRegistry.merge()`` in the
    orchestrator constructor.
    """
    registry = ActionRegistry()
    _register_durable_action_modules(registry)
    # Keep durable action routing aligned with orchestrator routing: if an
    # action ID is not explicitly registered, treat it as an MCP tool name.
    registry.set_fallback_handler(_durable_mcp_fallback_action)
    return registry


@lru_cache(maxsize=128)
def _resolve_subworkflow_definition(workflow_id: str):
    """Resolve subworkflow definitions from registry first, then Vontology loader.

    This keeps built-in durable subworkflow composition runnable even when
    Vontology publication is unavailable or intentionally read-only in tests.
    """
    workflow_id_clean = str(workflow_id or "").strip()
    if not workflow_id_clean:
        return None

    try:
        registry = build_workflow_registry_read_only()
        definition = registry.get(workflow_id_clean)
        if definition is not None:
            return definition
    except Exception:
        definition = None

    return load_workflow_definition_from_vontology(workflow_id_clean)
