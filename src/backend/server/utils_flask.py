import copy
import datetime as _dt
import importlib
import logging
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from flask import Flask, g, jsonify, redirect, request, url_for

if TYPE_CHECKING:
    from .routes.von_routes import von_bp
    from .routes.vontology_routes import get_instance_counts, vontology_bp
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
    from .routes.task_routes import task_bp
    from .routes.message_routes import message_bp
    from ..db.connection_manager import ensure_monitor_started, get_db
    from ..services.annotation_extraction_service import (
        PROMPT_CONCEPT_ID,
        prompt_concept_health_status,
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


def _bind_imports(module_name: str, names: list[str]) -> None:
    module = importlib.import_module(module_name)
    globals().update({name: getattr(module, name) for name in names})


_bind_imports("src.backend.server.routes.von_routes", ["von_bp"])
_bind_imports(
    "src.backend.server.routes.vontology_routes",
    ["vontology_bp", "get_instance_counts"],
)
_bind_imports("src.backend.server.routes.concept_routes", ["concept_bp"])
_bind_imports("src.backend.server.routes.settings_routes", ["settings_bp"])
_bind_imports(
    "src.backend.server.routes.elicitation_routes",
    ["elicitation_bp"],
)
_bind_imports(
    "src.backend.server.routes.annotations_routes",
    ["annotations_bp"],
)
_bind_imports("src.backend.server.routes.admin_routes", ["admin_bp"])
_bind_imports("src.backend.server.routes.auth_routes", ["auth_bp"])
_bind_imports(
    "src.backend.server.routes.agent_gmail_oauth_routes",
    ["agent_gmail_oauth_bp"],
)
_bind_imports(
    "src.backend.server.routes.predicate_routes",
    ["predicate_bp"],
)
_bind_imports(
    "src.backend.server.routes.workflows_routes",
    ["workflows_bp"],
)
_bind_imports(
    "src.backend.server.routes.room_device_routes",
    ["room_device_bp"],
)
_bind_imports(
    "src.backend.server.routes.client_capabilities_routes",
    ["client_capabilities_bp"],
)
_bind_imports("src.backend.server.routes.speech_routes", ["speech_bp"])
_bind_imports("src.backend.server.routes.task_routes", ["task_bp"])
_bind_imports("src.backend.server.routes.message_routes", ["message_bp"])
_bind_imports(
    "src.backend.db.connection_manager",
    ["ensure_monitor_started", "get_db"],
)
_bind_imports(
    "src.backend.services.annotation_extraction_service",
    ["prompt_concept_health_status", "PROMPT_CONCEPT_ID"],
)
_bind_imports(
    "src.backend.services.runtime_code_version_service",
    [
        "DEFAULT_APP_VERSION",
        "get_runtime_code_version",
        "get_runtime_code_version_info",
    ],
)
_bind_imports(
    "src.backend.services.google_oauth_config",
    [
        "google_oauth_strict_startup_enabled",
        "validate_google_oauth_startup_or_raise",
    ],
)
_bind_imports(
    "src.backend.services.mongo_startup_config",
    [
        "mongo_startup_probe_enabled",
        "mongo_strict_startup_enabled",
        "run_mongo_startup_probe",
        "validate_mongo_startup_or_raise",
    ],
)
_bind_imports(
    "src.backend.services.settings_service",
    [
        "get_internal_mcp_max_tool_invocations",
        "get_internal_mcp_tool_batch_cap",
    ],
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
_durable_workflow_components_snapshot = None
_durable_workflow_startup_status_snapshot = None


def _copy_runtime_snapshot_payload(payload: Any) -> Any:
    try:
        return copy.deepcopy(payload)
    except Exception:
        return payload


def _set_durable_workflow_components_snapshot(
    app: Flask,
    components: dict[str, Any] | None,
) -> None:
    global _durable_workflow_components_snapshot
    _durable_workflow_components_snapshot = _copy_runtime_snapshot_payload(components)
    app.config["DURABLE_WORKFLOW_COMPONENTS"] = _copy_runtime_snapshot_payload(
        components
    )


def _set_durable_workflow_startup_status_snapshot(
    app: Flask,
    status: dict[str, Any] | None,
) -> None:
    global _durable_workflow_startup_status_snapshot
    _durable_workflow_startup_status_snapshot = _copy_runtime_snapshot_payload(status)
    app.config["DURABLE_WORKFLOW_STARTUP_STATUS"] = _copy_runtime_snapshot_payload(
        status
    )


def get_durable_workflow_components_snapshot() -> dict[str, Any] | None:
    snapshot = _copy_runtime_snapshot_payload(_durable_workflow_components_snapshot)
    return snapshot if isinstance(snapshot, dict) else None


def get_durable_workflow_startup_status_snapshot() -> dict[str, Any] | None:
    snapshot = _copy_runtime_snapshot_payload(_durable_workflow_startup_status_snapshot)
    return snapshot if isinstance(snapshot, dict) else None


def _build_durable_workflow_bootstrap_summary(
    components: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    if not isinstance(components, dict):
        return summary

    for key in (
        "entity_workflow_bootstrap",
        "entity_information_retrieval_workflow_bootstrap",
        "concept_search_instance_retrieval_workflow_bootstrap",
        "multilingual_concept_enrichment_workflow_bootstrap",
        "conversation_turn_workflow_bootstrap",
        "paper_workflow_bootstrap",
        "episode_evaluation_workflow_bootstrap",
        "paper_recommendation_workflow_bootstrap",
        "talk_workflow_bootstrap",
        "testing_workflow_bootstrap",
        "turn_pipeline_monitoring_workflow_bootstrap",
        "workflow_authority_bootstrap",
        "workflow_authoring_prompt_bootstrap",
    ):
        report = components.get(key)
        if not isinstance(report, dict):
            continue
        item: dict[str, Any] = {"success": bool(report.get("success", False))}
        publication = report.get("publication")
        if isinstance(publication, dict):
            materialisation_status = str(
                publication.get("materialisation_status") or ""
            ).strip()
            if materialisation_status:
                item["materialisation_status"] = materialisation_status
            if "drift_detected" in publication:
                item["drift_detected"] = bool(publication.get("drift_detected"))
            drift_workflow_ids = [
                str(workflow_id).strip()
                for workflow_id in (publication.get("drift_workflow_ids") or [])
                if str(workflow_id).strip()
            ]
            if drift_workflow_ids:
                item["drift_workflow_ids"] = drift_workflow_ids
            skip_reason = str(publication.get("skip_reason") or "").strip()
            if skip_reason:
                item["skip_reason"] = skip_reason
        summary[key] = item

    return summary


def _build_durable_workflow_registry():
    """Build the durable runtime registry without blocking on parity work."""
    from ..workflows.durable.registry_factory import (
        get_shared_workflow_registry_read_only,
    )

    # Durable startup should populate the same shared registry that verified
    # submission and discovery reuse later, otherwise launch-time verification
    # pays for a second full lazy-registry build in the same process.
    return get_shared_workflow_registry_read_only(
        defer_parity_work=True,
        start_deferred_registry_work=True,
    )


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
        after_report = build_workflow_concept_authority_report(
            registry=authority_registry
        )
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
        from ..services.conversation_turn_workflow_vontology_service import (
            bootstrap_canonical_conversation_turn_workflows,
        )
        from ..services.entity_representation_workflow_vontology_service import (
            bootstrap_canonical_entity_representation_workflows,
        )
        from ..services.entity_information_retrieval_workflow_vontology_service import (
            bootstrap_canonical_entity_information_retrieval_workflow,
        )
        from ..services.concept_search_instance_retrieval_workflow_vontology_service import (
            bootstrap_canonical_concept_search_instance_retrieval_workflow,
        )
        from ..services.multilingual_concept_enrichment_vontology_service import (
            bootstrap_canonical_multilingual_concept_enrichment_workflow,
        )
        from ..services.episode_evaluation_workflow_vontology_service import (
            bootstrap_canonical_episode_evaluation_workflow,
        )
        from ..services.entity_identity_resolution_workflow_vontology_service import (
            bootstrap_canonical_entity_identity_resolution_workflow,
        )
        from ..services.paper_recommendation_workflow_vontology_service import (
            bootstrap_canonical_paper_recommendation_workflow,
        )
        from ..services.testing_workflow_vontology_service import (
            bootstrap_canonical_testing_workflows,
        )
        from ..services.turn_pipeline_monitoring_workflow_vontology_service import (
            bootstrap_canonical_turn_pipeline_monitoring_workflows,
        )
        from ..services.talk_representation_workflow_vontology_service import (
            bootstrap_canonical_talk_representation_workflows,
        )
        from ..services.workflow_capability_service import (
            run_workflow_capability_index_startup_check,
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

        entity_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="entity workflow",
            bootstrap_fn=bootstrap_canonical_entity_representation_workflows,
        )
        entity_information_retrieval_workflow_bootstrap_report = (
            _run_workflow_family_bootstrap(
                label="entity-information retrieval workflow",
                bootstrap_fn=bootstrap_canonical_entity_information_retrieval_workflow,
            )
        )
        concept_search_instance_retrieval_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="concept-search instance retrieval workflow",
            bootstrap_fn=bootstrap_canonical_concept_search_instance_retrieval_workflow,
        )
        multilingual_concept_enrichment_workflow_bootstrap_report = (
            _run_workflow_family_bootstrap(
                label="multilingual concept-enrichment workflow",
                bootstrap_fn=bootstrap_canonical_multilingual_concept_enrichment_workflow,
            )
        )
        conversation_turn_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="conversation-turn workflow",
            bootstrap_fn=bootstrap_canonical_conversation_turn_workflows,
        )
        paper_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="paper workflow",
            bootstrap_fn=bootstrap_canonical_paper_representation_workflows,
        )
        episode_evaluation_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="episode evaluation workflow",
            bootstrap_fn=bootstrap_canonical_episode_evaluation_workflow,
        )
        entity_identity_resolution_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="entity identity-resolution workflow",
            bootstrap_fn=bootstrap_canonical_entity_identity_resolution_workflow,
        )
        paper_recommendation_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="paper recommendation workflow",
            bootstrap_fn=bootstrap_canonical_paper_recommendation_workflow,
        )
        talk_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="talk workflow",
            bootstrap_fn=bootstrap_canonical_talk_representation_workflows,
        )
        testing_workflow_bootstrap_report = _run_workflow_family_bootstrap(
            label="testing workflow",
            bootstrap_fn=bootstrap_canonical_testing_workflows,
        )
        turn_pipeline_monitoring_workflow_bootstrap_report = (
            _run_workflow_family_bootstrap(
                label="turn-pipeline monitoring workflow",
                bootstrap_fn=bootstrap_canonical_turn_pipeline_monitoring_workflows,
            )
        )

        workflow_authority_bootstrap_report = _bootstrap_workflow_authority_for_startup(
            app_logger
        )

        # Build registries if not already done
        if _durable_workflow_registry is None:
            _durable_workflow_registry = _build_durable_workflow_registry()
        if _durable_action_registry is None:
            _durable_action_registry = _build_durable_action_registry()

        workflow_capability_index_startup_report = (
            run_workflow_capability_index_startup_check(
                workflow_registry=_durable_workflow_registry
            )
        )

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
        result["entity_workflow_bootstrap"] = entity_workflow_bootstrap_report
        result["entity_information_retrieval_workflow_bootstrap"] = (
            entity_information_retrieval_workflow_bootstrap_report
        )
        result["concept_search_instance_retrieval_workflow_bootstrap"] = (
            concept_search_instance_retrieval_workflow_bootstrap_report
        )
        result["multilingual_concept_enrichment_workflow_bootstrap"] = (
            multilingual_concept_enrichment_workflow_bootstrap_report
        )
        result["conversation_turn_workflow_bootstrap"] = (
            conversation_turn_workflow_bootstrap_report
        )
        result["paper_workflow_bootstrap"] = paper_workflow_bootstrap_report
        result["episode_evaluation_workflow_bootstrap"] = (
            episode_evaluation_workflow_bootstrap_report
        )
        result["entity_identity_resolution_workflow_bootstrap"] = (
            entity_identity_resolution_workflow_bootstrap_report
        )
        result["paper_recommendation_workflow_bootstrap"] = (
            paper_recommendation_workflow_bootstrap_report
        )
        result["talk_workflow_bootstrap"] = talk_workflow_bootstrap_report
        result["testing_workflow_bootstrap"] = testing_workflow_bootstrap_report
        result["turn_pipeline_monitoring_workflow_bootstrap"] = (
            turn_pipeline_monitoring_workflow_bootstrap_report
        )
        result["workflow_authority_bootstrap"] = workflow_authority_bootstrap_report
        result["workflow_capability_index_startup_check"] = (
            workflow_capability_index_startup_report
        )
        if not bool(entity_workflow_bootstrap_report.get("success", False)):
            app_logger.warning(
                "[durable_workflows] entity workflow bootstrap failed: %s",
                entity_workflow_bootstrap_report,
            )
        if not bool(
            entity_information_retrieval_workflow_bootstrap_report.get("success", False)
        ):
            app_logger.warning(
                "[durable_workflows] entity-information retrieval workflow bootstrap failed: %s",
                entity_information_retrieval_workflow_bootstrap_report,
            )
        if not bool(
            concept_search_instance_retrieval_workflow_bootstrap_report.get(
                "success",
                False,
            )
        ):
            app_logger.warning(
                "[durable_workflows] concept-search instance retrieval workflow bootstrap failed: %s",
                concept_search_instance_retrieval_workflow_bootstrap_report,
            )
        if not bool(
            multilingual_concept_enrichment_workflow_bootstrap_report.get(
                "success",
                False,
            )
        ):
            app_logger.warning(
                "[durable_workflows] multilingual concept-enrichment workflow bootstrap failed: %s",
                multilingual_concept_enrichment_workflow_bootstrap_report,
            )
        if not bool(conversation_turn_workflow_bootstrap_report.get("success", False)):
            app_logger.warning(
                "[durable_workflows] conversation-turn workflow bootstrap failed: %s",
                conversation_turn_workflow_bootstrap_report,
            )
        if not bool(paper_workflow_bootstrap_report.get("success", False)):
            app_logger.warning(
                "[durable_workflows] paper workflow bootstrap failed: %s",
                paper_workflow_bootstrap_report,
            )
        elif bool(
            (paper_workflow_bootstrap_report.get("publication") or {}).get(
                "drift_detected"
            )
        ):
            app_logger.warning(
                "[durable_workflows] paper workflow repo-seed repair applied for Vontology drift: %s",
                (paper_workflow_bootstrap_report.get("publication") or {}).get(
                    "drift_workflow_ids"
                )
                or (paper_workflow_bootstrap_report.get("workflow_ids") or []),
            )
        if not bool(episode_evaluation_workflow_bootstrap_report.get("success", False)):
            app_logger.warning(
                "[durable_workflows] episode evaluation workflow bootstrap failed: %s",
                episode_evaluation_workflow_bootstrap_report,
            )
        if not bool(
            entity_identity_resolution_workflow_bootstrap_report.get("success", False)
        ):
            app_logger.warning(
                "[durable_workflows] entity identity-resolution workflow bootstrap failed: %s",
                entity_identity_resolution_workflow_bootstrap_report,
            )
        if not bool(
            paper_recommendation_workflow_bootstrap_report.get("success", False)
        ):
            app_logger.warning(
                "[durable_workflows] paper recommendation workflow bootstrap failed: %s",
                paper_recommendation_workflow_bootstrap_report,
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
        if not bool(
            turn_pipeline_monitoring_workflow_bootstrap_report.get("success", False)
        ):
            app_logger.warning(
                "[durable_workflows] turn-pipeline monitoring workflow bootstrap failed: %s",
                turn_pipeline_monitoring_workflow_bootstrap_report,
            )
        if not bool(workflow_authority_bootstrap_report.get("success", False)):
            app_logger.warning(
                "[durable_workflows] workflow authority bootstrap failed: %s",
                workflow_authority_bootstrap_report,
            )
        if not bool(workflow_capability_index_startup_report.get("ready", False)):
            app_logger.warning(
                "[durable_workflows] workflow capability index not ready after startup check: %s",
                workflow_capability_index_startup_report,
            )

        # Ensure long-running identity-resolution maintenance keeps running
        # without manual schedule setup.
        try:
            from ..services.identity_resolution_schedule_bootstrap_service import (
                ensure_identity_resolution_background_schedule,
                ensure_identity_resolution_event_bindings,
            )

            identity_schedule_report = ensure_identity_resolution_background_schedule()
            result["identity_resolution_schedule_bootstrap"] = identity_schedule_report
            if not bool(identity_schedule_report.get("success", False)):
                app_logger.warning(
                    "[durable_workflows] identity schedule bootstrap failed: %s",
                    identity_schedule_report,
                )

            identity_event_binding_report = ensure_identity_resolution_event_bindings()
            result["identity_resolution_event_binding_bootstrap"] = (
                identity_event_binding_report
            )
            if not bool(identity_event_binding_report.get("success", False)):
                app_logger.warning(
                    "[durable_workflows] identity event-binding bootstrap failed: %s",
                    identity_event_binding_report,
                )
        except Exception as schedule_exc:
            app_logger.warning(
                "[durable_workflows] identity schedule/event-binding bootstrap error: %s",
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
            from ..services.workflow_authoring_vontology_service import (
                ensure_workflow_authoring_prompt_support,
            )

            workflow_authoring_prompt_report = (
                ensure_workflow_authoring_prompt_support()
            )
            result["workflow_authoring_prompt_bootstrap"] = (
                workflow_authoring_prompt_report
            )
            if not bool(workflow_authoring_prompt_report.get("success", False)):
                app_logger.warning(
                    "[durable_workflows] workflow-authoring prompt bootstrap failed: %s",
                    workflow_authoring_prompt_report,
                )
        except Exception as prompt_exc:
            app_logger.warning(
                "[durable_workflows] workflow-authoring prompt bootstrap error: %s",
                prompt_exc,
            )

        try:
            from ..services.paper_recommendation_background_schedule_bootstrap_service import (
                ensure_paper_recommendation_background_schedule,
            )

            paper_recommendation_schedule_report = (
                ensure_paper_recommendation_background_schedule()
            )
            result["paper_recommendation_schedule_bootstrap"] = (
                paper_recommendation_schedule_report
            )
            if not bool(paper_recommendation_schedule_report.get("success", False)):
                app_logger.warning(
                    "[durable_workflows] paper recommendation schedule bootstrap failed: %s",
                    paper_recommendation_schedule_report,
                )
        except Exception as schedule_exc:
            app_logger.warning(
                "[durable_workflows] paper recommendation schedule bootstrap error: %s",
                schedule_exc,
            )

        try:
            from ..services.turn_pipeline_monitoring_schedule_bootstrap_service import (
                ensure_turn_pipeline_monitoring_schedules,
            )

            turn_pipeline_monitoring_schedule_report = (
                ensure_turn_pipeline_monitoring_schedules()
            )
            result["turn_pipeline_monitoring_schedule_bootstrap"] = (
                turn_pipeline_monitoring_schedule_report
            )
            if not bool(turn_pipeline_monitoring_schedule_report.get("success", False)):
                app_logger.warning(
                    "[durable_workflows] turn-pipeline monitoring schedule bootstrap failed: %s",
                    turn_pipeline_monitoring_schedule_report,
                )
        except Exception as schedule_exc:
            app_logger.warning(
                "[durable_workflows] turn-pipeline monitoring schedule bootstrap error: %s",
                schedule_exc,
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

        try:
            from ..services.multilingual_concept_enrichment_schedule_bootstrap_service import (
                ensure_multilingual_concept_enrichment_background_schedule,
            )

            multilingual_enrichment_schedule_report = (
                ensure_multilingual_concept_enrichment_background_schedule()
            )
            result["multilingual_concept_enrichment_schedule_bootstrap"] = (
                multilingual_enrichment_schedule_report
            )
            if not bool(multilingual_enrichment_schedule_report.get("success", False)):
                app_logger.warning(
                    "[durable_workflows] multilingual concept-enrichment schedule bootstrap failed: %s",
                    multilingual_enrichment_schedule_report,
                )
        except Exception as schedule_exc:
            app_logger.warning(
                "[durable_workflows] multilingual concept-enrichment schedule bootstrap error: %s",
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


def _start_async_process_shutdown(
    app_logger: logging.Logger,
    *,
    werkzeug_shutdown: Callable[..., object] | None,
    fallback_delay_seconds: float = 0.2,
) -> None:
    """Finish graceful shutdown work in the background.

    The caller can return an HTTP response immediately while the durable worker
    drains and the process exits shortly afterwards.
    """

    def _run_shutdown() -> None:
        try:
            _stop_durable_workflow_system()
        except Exception as exc:
            try:
                app_logger.warning("[shutdown] Durable workflow stop error: %s", exc)
            except Exception:
                pass

        # Give the response a moment to flush before terminating the server.
        time.sleep(max(float(fallback_delay_seconds), 0.0))

        if werkzeug_shutdown is not None:
            try:
                werkzeug_shutdown()
            except Exception as exc:
                try:
                    app_logger.warning("[shutdown] Werkzeug shutdown error: %s", exc)
                except Exception:
                    pass
            return

        try:
            os._exit(0)
        except Exception as exc:
            try:
                app_logger.warning("[shutdown] Fallback exit error: %s", exc)
            except Exception:
                pass

    threading.Thread(
        target=_run_shutdown,
        name="von-admin-shutdown",
        daemon=True,
    ).start()


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

    _install_request_timing_middleware(app)
    _configure_flask_app_core(app, list_models_func, generate_func)
    _register_default_blueprints(app)

    # --- Log App Version ---
    # Use app.logger if available, otherwise print
    _log_flask_app_initialisation(app)
    gateway_instance = _initialise_internal_mcp_gateway(app)
    _configure_internal_mcp_orchestrator_startup(app, gateway_instance)
    _ensure_db_monitor_started(app)
    _maybe_start_startup_rag_requeue(app)
    _configure_durable_workflow_startup(app)
    _log_prompt_concept_health(app)

    # (Prewarm logic moved below route registrations to avoid early first-request state.)

    # --- Root/Health Check Endpoint ---
    @app.route("/")
    def root_redirect():
        """Redirect root to Von interface."""
        return _build_root_redirect_response()

    # Capture process start time once for uptime reporting
    from datetime import datetime, timezone as _tz

    app.config["SERVER_START_TIME"] = app.config.get(
        "SERVER_START_TIME"
    ) or datetime.now(_tz.utc).isoformat().replace("+00:00", "Z")

    @app.route("/health")
    def health_check():
        """Health check endpoint with pid and start time for process manager UI."""
        return _build_health_check_response(app)

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

        return _handle_rag_runtime_request()

    @app.route("/admin/chat_history_backfill", methods=["POST"])
    def admin_chat_history_backfill():
        """Backfill legacy chat history to the current user@org namespace and RAG.

        Logged-in only. Uses the current Flask session to derive user/org/namespace.
        Optional JSON body:
          {"max_sessions": int, "max_messages": int, "dry_run": bool}
        """
        return _handle_admin_chat_history_backfill_request()

    @app.route("/admin/chat_history_agent_provenance_backfill", methods=["POST"])
    def admin_chat_history_agent_provenance_backfill():
        """Mark reliable historical test/agent-created chat sessions.

        Defaults to dry-run mode and only mutates deterministic browser-test
        fixtures and benchmark-harness sessions.
        """
        return _handle_admin_chat_history_agent_provenance_backfill_request()

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
        return _handle_admin_chat_history_reindex_request(app)

    @app.route("/admin/rag_integrity", methods=["POST"])
    def admin_rag_integrity():
        return _handle_admin_rag_integrity_request()

    @app.route("/admin/rag_sync", methods=["POST"])
    def admin_rag_sync():
        return _handle_admin_rag_sync_request()

    @app.route("/diag")
    def diagnostics():
        """Lightweight diagnostics endpoint exposing runtime/process/cache info."""
        return _build_diagnostics_response(app)

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
        return _build_db_status_response()

    # --- API Endpoint for Version ---
    @app.route("/api/version")
    def get_version():
        """API endpoint to get the version of the app."""
        return _build_runtime_version_response()

    # --- Graceful Shutdown Endpoint (admin) ---
    @app.route("/admin/shutdown", methods=["POST"])
    def admin_shutdown():
        """Gracefully shut down the Flask development server.

        Requires header X-Admin-Token matching env VON_ADMIN_TOKEN.
        If token missing or mismatch returns 401.
        Intended for controlled stop via run.ps1 script.
        """
        return _handle_admin_shutdown_request(app)

    # Backwards-compatible alias for tests and older clients that call the shorter '/api/vontology' path.
    @app.route("/api/vontology/instance_counts")
    def alias_instance_counts():
        # Delegate to the blueprint handler
        return _build_instance_counts_alias_response()

    # --- Workflow Model Policy Diagnostics (JVNAUTOSCI-998) ---
    @app.route("/admin/policy_comparison")
    def admin_policy_comparison():
        """Compare JSON-based and graph-based workflow model policy representations.

        Returns a diagnostic report showing differences between the two.
        Used to validate graph parity before deprecating JSON fallback.
        """
        return _handle_admin_policy_comparison_request()

    # Optional background prewarm (model list + tree) to reduce first-request latency.
    # Placed AFTER all routes to ensure decorators complete before any internal
    # test_client calls. Skipped when running under pytest (env PYTEST_CURRENT_TEST) or
    # when app.testing already true, or when disabled via env/config.
    _register_optional_prewarm(app)

    return app


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


def _install_request_timing_middleware(app: Flask) -> None:
    """Attach lightweight request timing middleware."""
    slow_request_threshold_ms = float(
        os.environ.get("VON_SLOW_REQUEST_THRESHOLD_MS", "1000")
    )

    @app.before_request
    def _start_request_timer():
        g._request_start_time = time.perf_counter()

    @app.after_request
    def _log_slow_requests(response):
        start_time = getattr(g, "_request_start_time", None)
        if start_time is not None:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            if elapsed_ms >= slow_request_threshold_ms:
                app.logger.warning(
                    "[slow_request] %s %s took %.1fms (threshold=%.0fms) status=%s",
                    request.method,
                    request.endpoint or request.path,
                    elapsed_ms,
                    slow_request_threshold_ms,
                    response.status_code,
                )
            elif elapsed_ms >= 200:
                app.logger.info(
                    "[request_timing] %s %s %.1fms status=%s",
                    request.method,
                    request.path,
                    elapsed_ms,
                    response.status_code,
                )
        return response


def _configure_flask_app_core(
    app: Flask,
    list_models_func: Callable[[], List[str]],
    generate_func: Callable[[str, Optional[List[Dict[str, str]]], Optional[str]], str],
) -> None:
    """Apply core Flask config and feature-flag context wiring."""
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", _DEV_SECRET_KEY)
    strict_oauth_startup = google_oauth_strict_startup_enabled()

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

    if mongo_strict_startup_enabled():
        validate_mongo_startup_or_raise()

    if mongo_startup_probe_enabled() and not _is_running_under_pytest():
        probe_result = run_mongo_startup_probe()
        app.logger.info(
            "Mongo startup probe succeeded (read=%s write=%s collection=%s).",
            probe_result.get("read_ok"),
            probe_result.get("write_ok"),
            probe_result.get("write_collection"),
        )

    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["GENERATE_FUNC"] = generate_func
    app.config["LIST_MODELS_FUNC"] = list_models_func
    app.config.setdefault("PEOPLE", [])
    app.config.setdefault("CONTEXT", [])

    from ..languagemodels.model_defaults import DEFAULT_OLLAMA_MODEL

    app.config.setdefault("MODEL", DEFAULT_OLLAMA_MODEL)

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


def _register_default_blueprints(app: Flask) -> None:
    """Register the standard route blueprints for the Von web server."""
    app.register_blueprint(von_bp, url_prefix="/von")
    app.register_blueprint(vontology_bp, url_prefix="/vontology/api/vontology")
    app.register_blueprint(concept_bp, url_prefix="/api/concepts")
    app.register_blueprint(settings_bp, url_prefix="/api/settings")
    app.register_blueprint(elicitation_bp, url_prefix="/api/elicitation")
    app.register_blueprint(annotations_bp, url_prefix="/api/annotations")
    app.register_blueprint(predicate_bp)
    app.register_blueprint(workflows_bp)
    app.register_blueprint(room_device_bp)
    app.register_blueprint(client_capabilities_bp)
    app.register_blueprint(speech_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(task_bp, url_prefix="/api/tasks")
    app.register_blueprint(message_bp, url_prefix="/api/messages")
    app.register_blueprint(auth_bp, url_prefix="/von")
    app.register_blueprint(agent_gmail_oauth_bp, url_prefix="/von")


def _log_flask_app_initialisation(app: Flask) -> None:
    runtime_code_version = get_runtime_code_version()
    try:
        app.logger.setLevel(logging.INFO)
        app.logger.info(
            "--- Flask App Initializing - Version: %s ---", runtime_code_version
        )
    except Exception:
        print(f"--- Flask App Initializing - Version: {runtime_code_version} ---")


def _initialise_internal_mcp_gateway(app: Flask):
    """Build the internal MCP gateway and persist it into app config."""
    gateway_instance = None
    try:
        from ..integrations.internal_mcp import (
            InternalMCPChatOrchestrator,
            InternalMCPGateway,
            InternalMCPTransport,
            build_default_catalogue,
        )

        del InternalMCPChatOrchestrator
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
    return gateway_instance


def _configure_internal_mcp_orchestrator_startup(app: Flask, gateway_instance) -> None:
    """Initialise or defer the internal MCP orchestrator."""
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    app.config["INTERNAL_MCP_ORCHESTRATOR_STATUS"] = {
        "state": "disabled" if gateway_instance is None else "pending",
        "ready": False,
        "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }

    if gateway_instance is None:
        return

    from ..integrations.internal_mcp import InternalMCPChatOrchestrator

    orchestrator_logger = (
        app.logger.getChild("mcp_orchestrator") if app.logger else None
    )
    blocking_orchestrator_start = os.getenv(
        "VON_INTERNAL_MCP_ORCHESTRATOR_BLOCKING_STARTUP", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}

    def _build_orchestrator() -> None:
        start_perf = time.perf_counter()
        app.config["INTERNAL_MCP_ORCHESTRATOR_STATUS"] = {
            "state": "initialising",
            "ready": False,
            "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        try:
            try:
                bootstrap_max_tool_invocations = get_internal_mcp_max_tool_invocations()
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
        return

    try:
        app.logger.info(
            "[mcp_orchestrator] Deferring initialisation to background thread "
            "(set VON_INTERNAL_MCP_ORCHESTRATOR_BLOCKING_STARTUP=1 to restore blocking startup)."
        )
    except Exception:
        pass

    try:
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


def _ensure_db_monitor_started(app: Flask) -> None:
    try:
        ensure_monitor_started()
    except Exception as exc:  # pragma: no cover
        try:
            app.logger.warning("Failed to start DB monitor: %s", exc)
        except Exception:
            pass


def _maybe_start_startup_rag_requeue(app: Flask) -> None:
    """Requeue eligible unindexed sessions on startup outside pytest."""
    if _is_running_under_pytest():
        return

    try:
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


def _configure_durable_workflow_startup(app: Flask) -> None:
    """Initialise or defer durable workflow runtime startup."""
    _set_durable_workflow_components_snapshot(app, None)
    _set_durable_workflow_startup_status_snapshot(
        app,
        {
            "state": "skipped_pytest" if _is_running_under_pytest() else "pending",
            "ready": False,
            "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        },
    )

    if _is_running_under_pytest():
        return

    blocking_durable_startup = os.getenv(
        "VON_DURABLE_WORKFLOWS_BLOCKING_STARTUP", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}

    def _bootstrap_durable_workflow_system() -> None:
        _set_durable_workflow_startup_status_snapshot(
            app,
            {
                "state": "initialising",
                "ready": False,
                "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            },
        )
        startup_perf = time.perf_counter()
        try:
            components = _start_durable_workflow_system(app.logger)
            duration_ms = int((time.perf_counter() - startup_perf) * 1000)
            if components is not None:
                _set_durable_workflow_components_snapshot(app, components)
                _set_durable_workflow_startup_status_snapshot(
                    app,
                    {
                        "state": "ready",
                        "ready": True,
                        "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                        "duration_ms": duration_ms,
                        "workflow_bootstrap_summary": _build_durable_workflow_bootstrap_summary(
                            components
                        ),
                    },
                )
                try:
                    app.logger.info(
                        "[durable_workflows] Initialised in %dms (blocking_startup=%s).",
                        duration_ms,
                        blocking_durable_startup,
                    )
                except Exception:
                    pass

                import atexit

                atexit.register(_stop_durable_workflow_system)
            else:
                _set_durable_workflow_startup_status_snapshot(
                    app,
                    {
                        "state": "not_started",
                        "ready": False,
                        "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                        "duration_ms": duration_ms,
                    },
                )
        except Exception as exc:  # pragma: no cover
            _set_durable_workflow_startup_status_snapshot(
                app,
                {
                    "state": "failed",
                    "ready": False,
                    "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "error": str(exc),
                },
            )
            try:
                app.logger.warning(
                    "[startup] Durable workflow system startup failed: %s", exc
                )
            except Exception:
                pass

    if blocking_durable_startup:
        _bootstrap_durable_workflow_system()
        return

    try:
        app.logger.info(
            "[durable_workflows] Deferring startup to background thread "
            "(set VON_DURABLE_WORKFLOWS_BLOCKING_STARTUP=1 to restore blocking startup)."
        )
    except Exception:
        pass

    try:
        threading.Thread(
            target=_bootstrap_durable_workflow_system,
            name="durable_workflow_startup",
            daemon=True,
        ).start()
    except Exception as exc:  # pragma: no cover
        _set_durable_workflow_startup_status_snapshot(
            app,
            {
                "state": "failed",
                "ready": False,
                "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "error": f"thread_start_failed:{exc}",
            },
        )
        try:
            app.logger.warning(
                "[durable_workflows] Failed to start async startup thread: %s",
                exc,
            )
        except Exception:
            pass


def _log_prompt_concept_health(app: Flask) -> None:
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
    except Exception as exc:  # pragma: no cover - defensive
        try:
            app.logger.warning("Prompt concept health check unexpected error: %s", exc)
        except Exception:
            pass


def _build_root_redirect_response():
    return redirect(url_for("von.serve_page"))


def _build_health_check_response(app: Flask):
    import socket
    import urllib.request

    local_ip = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
        sock.close()
    except Exception as exc:
        print(f"[health] Local IP detection (method 1) failed: {exc}")
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
            print(f"[health] Local IP from hostname: {local_ip}")
        except Exception as inner_exc:
            print(f"[health] Local IP detection (method 2) failed: {inner_exc}")
            local_ip = "127.0.0.1"

    public_ip = app.config.get("PUBLIC_IP_ADDRESS")
    if not public_ip:
        try:
            with urllib.request.urlopen(
                "https://api.ipify.org?format=text", timeout=3
            ) as response:
                public_ip = response.read().decode("utf-8").strip()
                app.config["PUBLIC_IP_ADDRESS"] = public_ip
                print(f"[health] Public IP fetched and cached: {public_ip}")
        except Exception as exc:
            print(f"[health] Public IP fetch failed: {exc}")
            public_ip = None

    version_info = get_runtime_code_version_info()
    agent_test_instance = (
        str(os.environ.get("VON_AGENT_TEST_INSTANCE") or "")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )
    return jsonify(
        status="healthy",
        version=version_info.get("version"),
        version_details=version_info,
        agent_test_instance=agent_test_instance,
        agent_test_environment_marker="VON_AGENT_TEST_INSTANCE",
        pid=os.getpid(),
        start_time=app.config["SERVER_START_TIME"],
        local_ip=local_ip,
        public_ip=public_ip,
        rag_pending_count=None,
    )


def _describe_runtime_component(obj):
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
                value = getattr(obj, attr, None)
                if isinstance(value, str) and value.strip():
                    info[attr] = value.strip()
            except Exception:
                pass
        for attr in ("provider", "host", "selection_source"):
            try:
                value = getattr(obj, attr, None)
                if isinstance(value, str) and value.strip():
                    info[attr] = value.strip()
            except Exception:
                pass
        return info
    except Exception:
        return None


def _handle_rag_runtime_request():
    import os

    try:
        from src.backend.services.rag_service import (
            RAGBackendUnavailable,
            peek_rag_service,
        )

        namespace = request.args.get("namespace")
        service = peek_rag_service()

        if service is None:
            effective_namespace = namespace or os.getenv("VON_DEFAULT_NAMESPACE")
            return jsonify(
                {
                    "success": True,
                    "service_initialised": False,
                    "requested_namespace": namespace,
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

        try:
            resolver = getattr(service, "_resolve_effective_namespace", None)
            if callable(resolver):
                effective_namespace = resolver(namespace)
            else:
                effective_namespace = namespace or os.getenv("VON_DEFAULT_NAMESPACE")
        except Exception:
            effective_namespace = namespace or os.getenv("VON_DEFAULT_NAMESPACE")

        index_persist_dir = None
        try:
            namespace_dir = getattr(service, "_namespace_persist_dir", None)
            if callable(namespace_dir) and isinstance(effective_namespace, str):
                index_persist_dir = namespace_dir(effective_namespace)
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
        runtime_configuration = None
        namespace_state = None
        try:
            get_embedder = getattr(service, "get_runtime_embed_model", None)
            if callable(get_embedder):
                embedder = _describe_runtime_component(get_embedder())

            get_llm = getattr(service, "get_runtime_llm", None)
            if callable(get_llm):
                llm = _describe_runtime_component(get_llm())

            get_runtime_configuration = getattr(
                service,
                "get_runtime_configuration_summary",
                None,
            )
            if callable(get_runtime_configuration):
                config = get_runtime_configuration()
                if isinstance(config, dict):
                    runtime_configuration = config

            get_namespace_runtime_state = getattr(
                service,
                "get_namespace_runtime_state",
                None,
            )
            if callable(get_namespace_runtime_state):
                state = get_namespace_runtime_state(effective_namespace)
                if isinstance(state, dict):
                    namespace_state = state

            if embedder is None or llm is None:
                service_context = getattr(service, "service_context", None)
                if service_context is not None:
                    if embedder is None:
                        embedder = _describe_runtime_component(
                            getattr(service_context, "embed_model", None)
                        )
                    if llm is None:
                        llm = _describe_runtime_component(
                            getattr(service_context, "llm", None)
                        )
        except Exception:
            pass

        try:
            last_query = getattr(service, "_last_query_info", None)
        except Exception:
            last_query = None

        return jsonify(
            {
                "success": True,
                "service_initialised": True,
                "requested_namespace": namespace,
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
                "runtime_configuration": runtime_configuration,
                "namespace_state": namespace_state,
                "openai_key_present": bool(os.getenv("OPENAI_API_KEY")),
                "last_query": last_query,
            }
        )
    except RAGBackendUnavailable as exc:
        return (
            jsonify(
                success=False,
                error="rag_backend_unavailable",
                message=str(exc),
            ),
            503,
        )
    except Exception as exc:
        return jsonify(success=False, error="unexpected", detail=str(exc)), 500


def _resolve_chat_history_admin_context(
    flask_session: object,
    *,
    requested_namespace: str | None = None,
    enforce_namespace_match: bool,
):
    from src.backend.services.namespace_service import derive_namespace

    get = getattr(flask_session, "get", None)
    if not callable(get):
        return None, (jsonify(error="Not authenticated"), 401)

    session_user_slug = _get_session_user_slug(flask_session)
    session_user_concept_id = get("user_concept_id")
    if not (session_user_slug or session_user_concept_id):
        return None, (jsonify(error="Not authenticated"), 401)

    session_org_raw = get("organisation_concept_id") or get("org_id")
    organisation_slug = _slug_from_maybe_concept_id(session_org_raw)
    role_in_org = get("role_in_org")

    session_namespace = get("namespace")
    if not session_namespace and session_user_slug:
        session_namespace = derive_namespace(session_user_slug, organisation_slug)

    target_namespace = requested_namespace or session_namespace
    if not isinstance(target_namespace, str) or not target_namespace:
        return None, (jsonify(error="Not authenticated"), 401)

    if enforce_namespace_match and session_namespace != target_namespace:
        return None, (
            jsonify(error="namespace_mismatch", session_namespace=session_namespace),
            403,
        )

    user_concept_id = (
        session_user_concept_id
        if isinstance(session_user_concept_id, str) and session_user_concept_id
        else (f"#V#{session_user_slug}" if session_user_slug else None)
    )
    if not user_concept_id:
        return None, (jsonify(error="Not authenticated"), 401)

    return (
        {
            "user_concept_id": user_concept_id,
            "target_namespace": target_namespace,
            "organisation_concept_id": organisation_slug,
            "role_in_org": role_in_org,
            "session_namespace": session_namespace,
        },
        None,
    )


def _handle_admin_chat_history_backfill_request():
    try:
        from flask import session as flask_session
        from src.backend.services import chat_history_service

        context, error = _resolve_chat_history_admin_context(
            flask_session,
            enforce_namespace_match=False,
        )
        if error is not None:
            return error
        assert context is not None

        body = request.get_json(silent=True) or {}
        result = chat_history_service.backfill_chat_history_for_user(
            user_concept_id=context["user_concept_id"],
            target_namespace=context["target_namespace"],
            organisation_concept_id=context["organisation_concept_id"],
            role_in_org=context["role_in_org"],
            max_sessions=int(body.get("max_sessions", 10)),
            max_messages=int(body.get("max_messages", 500)),
            dry_run=bool(body.get("dry_run", False)),
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify(error="unexpected", detail=str(exc)), 500


def _handle_admin_chat_history_agent_provenance_backfill_request():
    try:
        from flask import session as flask_session
        from src.backend.services import chat_history_service

        context, error = _resolve_chat_history_admin_context(
            flask_session,
            enforce_namespace_match=False,
        )
        if error is not None:
            return error
        assert context is not None

        body = request.get_json(silent=True) or {}
        result = chat_history_service.backfill_agent_created_chat_session_provenance(
            user_concept_id=context["user_concept_id"],
            namespace=context["target_namespace"],
            include_legacy=bool(body.get("include_legacy", False)),
            max_sessions=int(body.get("max_sessions", 500)),
            dry_run=bool(body.get("dry_run", True)),
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify(error="unexpected", detail=str(exc)), 500


def _handle_admin_chat_history_reindex_request(app: Flask):
    try:
        from flask import session as flask_session
        from src.backend.services import chat_history_service

        context, error = _resolve_chat_history_admin_context(
            flask_session,
            requested_namespace=request.args.get("namespace")
            or flask_session.get("namespace"),
            enforce_namespace_match=True,
        )
        if error is not None:
            return error
        assert context is not None

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

            session_id = session_ids[0]
            try:
                t0 = time.monotonic()
            except Exception:
                t0 = None

            try:
                app.logger.info(
                    "[chat_history_reindex] chunk start user=%s ns=%s session=%s chunk_start=%s chunk_size=%s dry_run=%s",
                    context["user_concept_id"],
                    context["target_namespace"],
                    session_id,
                    int(chunk_start or 0),
                    int(chunk_size or 25),
                    bool(dry_run),
                )
            except Exception:
                pass

            result = chat_history_service.reindex_chat_history_session_chunk(
                user_concept_id=context["user_concept_id"],
                target_namespace=context["target_namespace"],
                organisation_concept_id=context["organisation_concept_id"],
                role_in_org=context["role_in_org"],
                session_id=session_id,
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
                    context["user_concept_id"],
                    context["target_namespace"],
                    session_id,
                    result.get("messages_indexed_attempted"),
                    result.get("messages_indexed_success"),
                    result.get("messages_indexed_failed"),
                    result.get("next_chunk_start"),
                    result.get("done"),
                    elapsed_ms,
                    len(result.get("errors") or []),
                )
            except Exception:
                pass

            return jsonify(result)

        result = chat_history_service.reindex_chat_history_for_user_namespace(
            user_concept_id=context["user_concept_id"],
            target_namespace=context["target_namespace"],
            organisation_concept_id=context["organisation_concept_id"],
            role_in_org=context["role_in_org"],
            session_ids=session_ids,
            max_sessions=max_sessions,
            max_messages=max_messages,
            reset_counters=reset_counters,
            dry_run=dry_run,
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify(error="unexpected", detail=str(exc)), 500


def _resolve_namespace_scope_values(namespace: str | None) -> list[str] | None:
    if not namespace:
        return None

    session_namespaces = [namespace]
    try:
        from src.backend.services.namespace_service import parse_namespace

        parsed = parse_namespace(namespace)
        if parsed.get("user_id"):
            user_only_namespace = f"#V#{parsed['user_id']}"
            if user_only_namespace not in session_namespaces:
                session_namespaces.append(user_only_namespace)
    except Exception:
        pass

    return session_namespaces


def _handle_admin_rag_integrity_request():
    db = get_db()
    if db is None:
        return jsonify({"error": "db_unavailable"}), 503

    sessions_coll = db["interaction_sessions"]
    interactions_coll = (
        db["interactions"] if "interactions" in db.list_collection_names() else None
    )
    namespace = request.args.get("namespace")
    session_namespaces = _resolve_namespace_scope_values(namespace)
    session_filter = (
        {"namespace": {"$in": session_namespaces}} if session_namespaces else {}
    )

    result = {
        "sessions": sessions_coll.count_documents(session_filter or {}),
        "scoped_sessions": sessions_coll.count_documents(session_filter or {}),
        "interactions": (
            interactions_coll.count_documents({})
            if interactions_coll is not None
            else 0
        ),
        "indexed": sessions_coll.count_documents(
            {"indexing_status": "indexed", **session_filter}
        ),
        "pending": sessions_coll.count_documents(
            {"indexing_status": "pending", **session_filter}
        ),
        "failed": sessions_coll.count_documents(
            {"indexing_status": "failed", **session_filter}
        ),
        "skipped": sessions_coll.count_documents(
            {"indexing_status": "skipped", **session_filter}
        ),
        "eligible_sessions": sessions_coll.count_documents(
            {
                "$or": [
                    {"history": {"$exists": True, "$ne": []}},
                    {"summary": {"$exists": True, "$type": "string", "$ne": ""}},
                ],
                **session_filter,
            }
        ),
        "eligible_interactions": 0,
        "anomalies": [],
        "namespace": namespace,
        "session_namespace": (session_namespaces[0] if session_namespaces else None),
        "session_namespaces": session_namespaces,
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
        for item in sample_with_text:
            session_id = item.get("session_id")
            if session_id is None:
                continue
            session_doc = sessions_coll.find_one(
                {"_id": session_id}, {"indexing_status": 1}
            )
            if not session_doc or session_doc.get("indexing_status") not in (
                "pending",
                "indexed",
                "failed",
                "skipped",
            ):
                orphan_sessions.append(str(session_id))
        if orphan_sessions:
            result["anomalies"].append(
                {"type": "orphan_text_interactions", "session_ids": orphan_sessions}
            )

    return jsonify(result)


def _handle_admin_rag_sync_request():
    from ..services.rag_sync_service import sync_to_chat_store

    payload = request.get_json(silent=True) or {}
    try:
        result = sync_to_chat_store(
            namespace=payload.get("namespace"),
            user_concept_id=payload.get("user_concept_id"),
            organisation_concept_id=payload.get("organisation_concept_id"),
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


def _build_diagnostics_response(app: Flask):
    import threading

    rss_mb = None
    thread_count = None
    try:
        import psutil  # type: ignore

        process = psutil.Process()
        rss_mb = round(process.memory_info().rss / (1024 * 1024), 2)
        thread_count = process.num_threads()
    except Exception:
        try:
            import tracemalloc

            if tracemalloc.is_tracing():
                snap = tracemalloc.take_snapshot()
                rss_mb = round(
                    sum([item.size for item in snap.statistics("filename")])
                    / (1024 * 1024),
                    2,
                )
        except Exception:
            pass
        thread_count = len(threading.enumerate())

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

    tree_cache = {}
    try:
        from .routes.vontology_routes import _TREE_CACHE  # type: ignore

        now_ts = time.time()
        ttl_env = os.getenv("VONTOLOGY_TREE_TTL")
        if _TREE_CACHE:
            record = _TREE_CACHE.get("Thing")
            if record and isinstance(record, tuple) and len(record) >= 5:
                ts, payload, build_secs, alloc_kb, hits = record
                node_count = None
                try:
                    if isinstance(payload, dict) and isinstance(
                        payload.get("tree"), list
                    ):
                        stack = list(payload["tree"])
                        count = 0
                        while stack:
                            node = stack.pop()
                            count += 1
                            children = (
                                node.get("children") if isinstance(node, dict) else None
                            )
                            if isinstance(children, list):
                                stack.extend(children)
                        node_count = count
                except Exception:
                    pass
                tree_cache = {
                    "cached": True,
                    "age_sec": round(now_ts - ts, 1),
                    "build_time_sec": round(build_secs, 3),
                    "alloc_kb": round(alloc_kb, 1),
                    "ttl_sec": int(ttl_env) if ttl_env and ttl_env.isdigit() else None,
                    "node_count": node_count,
                    "hits": hits,
                }
            else:
                tree_cache = {"cached": True}
        else:
            tree_cache = {"cached": False}
    except Exception:
        tree_cache = {"cached": False, "error": "unavailable"}

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
        counts_cache = {
            "size": size,
            "ttl_sec": int(ttl_env) if ttl_env and ttl_env.isdigit() else None,
            "stats_cache": get_vontology_concept_stats_cache_summary(),
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
                        key: len(value) if isinstance(value, (list, set, tuple)) else 0
                        for key, value in raw_map.items()
                    },
                }
                break
        salient_cache = {
            "size": cache_size,
            "split_entries": split_entries,
            "stats": dict(_SALIENT_STATS) if isinstance(_SALIENT_STATS, dict) else None,
            "ttl_sec": 30,
            "sample_scope_payload": sample_scope,
        }
    except Exception:
        salient_cache = {"error": "unavailable"}

    try:
        from ..mcp_server.process_guard import get_mcp_helper_inventory

        mcp_helper_inventory = get_mcp_helper_inventory()
    except Exception:
        mcp_helper_inventory = {"success": False, "error": "unavailable"}

    entity_counts_stats = {}
    try:
        from .routes.vontology_routes import _ENTITY_COUNTS_STATS  # type: ignore

        ema = _ENTITY_COUNTS_STATS.get("ema_ms")
        entity_counts_stats = {
            "total_calls": int(_ENTITY_COUNTS_STATS.get("total_calls") or 0),
            "last_ts": _ENTITY_COUNTS_STATS.get("last_ts"),
            "ema_ms": None if ema is None else round(float(ema), 1),
        }
    except Exception:
        entity_counts_stats = {"error": "unavailable"}

    try:
        from ..db.mongo_client import get_effective_mongo_uri, is_using_fallback_uri  # type: ignore

        effective_uri = get_effective_mongo_uri()
        redacted_uri = effective_uri
        if "://" in redacted_uri and "@" in redacted_uri:
            scheme, rest = redacted_uri.split("://", 1)
            if "@" in rest:
                creds, hostpart = rest.split("@", 1)
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

    session_user = None
    effective_user = None
    header_user = None
    normalised_header_user = None
    header_user_validation = None
    header_user_raw_exact_exists = None
    header_user_normalised_exact_exists = None
    user_visibility_sample = None
    try:
        from flask import session as flask_session
        from ..security.access_control import (
            _normalise_concept_id,  # type: ignore
            _validate_person_concept,  # type: ignore
            get_effective_user_concept_id,
        )
        from ..services.concept_service import (
            _find_raw_concept_by_exact_concept_id,  # type: ignore
        )

        session_user = flask_session.get("user_concept_id")
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
        from ..integrations.internal_mcp import search_proxy_mcp as search_proxy_module  # type: ignore

        proxy_instance = getattr(search_proxy_module, "_proxy_instance", None)
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
        "mcp_helper_inventory": mcp_helper_inventory,
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
            get_namespace_isolation_diagnostics_snapshot(include_recent_events=False)
        )
    except Exception as exc:
        diag["namespace_isolation_diagnostics"] = {
            "available": False,
            "error": type(exc).__name__,
        }

    try:
        from ..workflows.durable.startup import get_system_status

        diag["durable_workflows"] = get_system_status()
    except Exception as exc:
        diag["durable_workflows"] = {"error": str(exc), "available": False}

    gateway = app.config.get("INTERNAL_MCP_GATEWAY")
    if gateway is None:
        diag["internal_mcp_gateway"] = {"configured": False}
    else:
        try:
            diag["internal_mcp_gateway"] = gateway.get_diagnostics()
        except Exception as exc:  # pragma: no cover - defensive
            diag["internal_mcp_gateway"] = {"configured": True, "error": str(exc)}

    diag["internal_mcp_orchestrator_startup"] = app.config.get(
        "INTERNAL_MCP_ORCHESTRATOR_STATUS"
    )
    diag["durable_workflow_startup"] = app.config.get("DURABLE_WORKFLOW_STARTUP_STATUS")

    try:
        from ..services.annotation_extraction_service import (
            fallback_nonjson_metric_stats,
            phrase_cache_stats,
        )

        diag["annotations"] = {
            "phrase_cache": phrase_cache_stats(),
            "fallback_nonjson": fallback_nonjson_metric_stats(),
        }
    except Exception:
        pass

    try:
        from .routes.settings_routes import _IMPORT_METRICS, _ORPHAN_SCAN_METRICS  # type: ignore

        if _IMPORT_METRICS:
            diag["import_metrics"] = dict(_IMPORT_METRICS)
        if _ORPHAN_SCAN_METRICS:
            diag["orphan_metrics"] = dict(_ORPHAN_SCAN_METRICS)
    except Exception:
        pass

    try:
        from ...vontology.utils_vontology import _ACCESSOR_STATS  # type: ignore

        diag["accessor_stats"] = {
            key: dict(value) for key, value in _ACCESSOR_STATS.items()
        }
    except Exception:
        pass

    return jsonify(diag)


def _build_db_status_response():
    from datetime import datetime, timezone as _tz

    using_fallback = None
    atlas_detected = None
    host_only = None
    try:
        from ..db.mongo_client import get_effective_mongo_uri, is_using_fallback_uri  # type: ignore

        effective_uri = get_effective_mongo_uri()
        using_fallback = is_using_fallback_uri()
        if effective_uri:
            try:
                after_scheme = (
                    effective_uri.split("://", 1)[1]
                    if "://" in effective_uri
                    else effective_uri
                )
                if "@" in after_scheme:
                    after_scheme = after_scheme.split("@", 1)[1]
                host_only = after_scheme.split("/", 1)[0]
            except Exception:
                host_only = None
            atlas_detected = "mongodb.net" in effective_uri.lower()
    except Exception:
        pass
    return jsonify(
        using_fallback=using_fallback,
        atlas_detected=atlas_detected,
        effective_host=host_only,
        timestamp=datetime.now(_tz.utc).isoformat().replace("+00:00", "Z"),
    )


def _build_runtime_version_response():
    return jsonify(get_runtime_code_version_info())


def _handle_admin_shutdown_request(app: Flask):
    expected = os.environ.get("VON_ADMIN_TOKEN")
    provided = request.headers.get("X-Admin-Token")
    if not expected:
        return jsonify(success=False, error="shutdown_disabled"), 403
    if provided != expected:
        return jsonify(success=False, error="unauthorized"), 401

    werkzeug_shutdown = request.environ.get("werkzeug.server.shutdown")
    _start_async_process_shutdown(
        app.logger,
        werkzeug_shutdown=werkzeug_shutdown if callable(werkzeug_shutdown) else None,
    )
    status = (
        "shutting_down" if callable(werkzeug_shutdown) else "shutting_down_fallback"
    )
    return jsonify(success=True, status=status), 202


def _build_instance_counts_alias_response():
    return get_instance_counts()


def _handle_admin_policy_comparison_request():
    try:
        from ..services.workflow_policy_graph_service import (
            compare_policy_json_vs_graph,
        )

        policy_id = request.args.get("policy_id", "#V#default_workflow_model_policy")
        report = compare_policy_json_vs_graph(policy_id)
        return jsonify(report)
    except Exception as exc:
        return jsonify(error=str(exc), status="error"), 500


def _start_prewarm(app: Flask) -> None:
    try:
        if os.getenv("VON_PREWARM_DISABLE") in {
            "1",
            "true",
            "TRUE",
            "True",
        } or app.config.get("PREWARM_DISABLE"):
            app.logger.info("Prewarm disabled by VON_PREWARM_DISABLE/ config flag.")
            return

        def _prewarm_worker():
            t0 = time.time()
            try:
                try:
                    list_models_func = app.config.get("LIST_MODELS_FUNC")
                    if callable(list_models_func):
                        models = list_models_func()
                        app.logger.info(
                            "[prewarm] Listed %d models.",
                            len(models) if isinstance(models, list) else -1,
                        )
                except Exception as exc:
                    app.logger.warning("[prewarm] Model list failed: %s", exc)
                try:
                    with app.test_client() as client:
                        response = client.get("/vontology/api/vontology/tree?refresh=1")
                        if response.status_code == 200:
                            app.logger.info(
                                "[prewarm] Tree build OK (len bytes=%s)",
                                len(response.data),
                            )
                        else:
                            app.logger.warning(
                                "[prewarm] Tree build non-200 status=%s",
                                response.status_code,
                            )
                except Exception as exc:
                    app.logger.warning("[prewarm] Tree build failed: %s", exc)
            finally:
                app.logger.info("[prewarm] Completed in %.2fs", time.time() - t0)

        threading.Thread(
            target=_prewarm_worker,
            name="prewarm-thread",
            daemon=True,
        ).start()
    except Exception as exc:
        try:
            app.logger.warning("[prewarm] Failed to start: %s", exc)
        except Exception:
            pass


def _register_optional_prewarm(app: Flask) -> None:
    if (
        not app.testing
        and "PYTEST_CURRENT_TEST" not in os.environ
        and not app.config.get("PREWARM_DISABLE")
    ):
        try:

            @app.before_first_request  # type: ignore[attr-defined]
            def _defer_prewarm():  # type: ignore
                _start_prewarm(app)

        except Exception:
            _start_prewarm(app)


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
