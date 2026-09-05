from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from ..db.transient_errors import is_transient_mongo_error
from ..security.access_control import (
    can_access_concept,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
)
from ..languagemodels.llm_interface import get_active_model_name, get_llm_client
from ..prompt.annotation_prompt import AnnotationPromptBuilder
from ..services.episode_critique_memory_service import (
    list_recent_workflow_improvement_suggestions,
)
from ..services.text_value_service import (
    delete_text_relation,
    get_texts_for_concept,
    upsert_text_for_concept,
    upsert_singleton_text_relation,
)
from ..services.workflow_authoring_vontology_service import (
    build_workflow_authoring_prompt_contract,
    get_workflow_authoring_prompt_health_status,
)
from ..services.workflow_description_vontology_service import (
    WORKFLOW_DESCRIPTION_PROMPT_CONCEPT_ID,
    build_deterministic_workflow_description,
    build_workflow_description_prompt_context,
    resolve_workflow_description_prompt_concept_id,
)
from ..services.workflow_discovery_service import (
    classify_workflow_concept_executability,
)
from ..services.workflow_episode_service import (
    count_workflow_use_episodes,
    get_workflow_episode_counts_for_workflows,
    get_workflow_usage_aggregates_for_workflows,
    list_workflow_use_episodes,
)
from ..services.namespace_service import resolve_canonical_namespace
from ..services.workflow_actor_scope_service import (
    WorkflowActorScope,
    WorkflowActorScopeError,
    resolve_authoritative_workflow_actor_scope,
)
from .durable.registry_factory import (
    get_or_build_workflow_registry_inventory_snapshot,
    get_supported_durable_workflow_action_ids,
    get_shared_workflow_registry_read_only,
    resolve_workflow_definition_from_authority,
)
from .durable.models import WorkflowInstanceStatus, WorkflowSchedule
from .durable.startup import get_instance_manager
from .required_effects_contracts import (
    normalise_workflow_required_effects_contract,
)
from .workflow_launch_input_contracts import (
    normalise_workflow_launch_input_contract,
)
from .trace_store import list_recent_workflow_execution_traces
from .vontology_loader import (
    build_workflow_process_graph,
    build_workflow_process_graph_from_definition,
    resolve_workflow_background_launch_policy,
    resolve_workflow_discovery_exemplars,
    resolve_workflow_launch_input_contract,
    resolve_workflow_narrative_text,
    resolve_workflow_publication_lifecycle,
    resolve_workflow_routing_profile,
)
from .workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
    serialise_workflow_definition_to_authoring_spec,
)
from .workflow_concept_authority_service import (
    publish_workflow_definition_from_definition,
    upsert_workflow_json_policy_text,
    upsert_workflow_publication_lifecycle,
    workflow_child_visibility_covers_parent,
    WORKFLOW_BACKGROUND_LAUNCH_POLICY_TEXT_PREDICATE,
    WORKFLOW_DISCOVERY_EXEMPLARS_TEXT_PREDICATE,
    WORKFLOW_LAUNCH_INPUT_CONTRACT_TEXT_PREDICATE,
    WORKFLOW_REQUIRED_EFFECTS_CONTRACT_TEXT_PREDICATE,
    WORKFLOW_ROUTING_PROFILE_TEXT_PREDICATE,
)
from .workflow_definition_identity_service import (
    build_workflow_definition_identity,
    build_workflow_definition_identity_from_graph,
    validate_workflow_definition_contract,
)
from .workflow_listing_service import (
    build_workflow_listing_entry,
    collect_workflow_introspection_projection_ids,
    filter_workflow_ids_for_current_actor,
    project_workflow_introspection_payload_for_current_actor,
)

logger = logging.getLogger(__name__)

_TRANSIENT_INSTANCE_ERROR_MARKERS = ("temporarily unavailable",)
_WORKFLOW_STUDIO_AI_PROPOSAL_MIN_LENGTH = 20
_WORKFLOW_CANDIDATE_VALIDATION_PROFILE_CONTRACT_ONLY = "contract_only"
_WORKFLOW_CANDIDATE_VALIDATION_PROFILE_GENERATION_SAFE = "generation_safe"
_WORKFLOW_AUTHORING_PROPOSAL_SCHEMA_VERSION = "workflow_authoring_proposal.v1"
_WORKFLOW_AUTHORING_PROPOSAL_TEXT_PREDICATE = "#V#hasWorkflowAuthoringProposalJson"
_WORKFLOW_ACTIVE_AUTHORING_PROPOSAL_ID_TEXT_PREDICATE = (
    "#V#hasActiveWorkflowAuthoringProposalId"
)
_WORKFLOW_AUTHORING_PROPOSAL_STATUS_PENDING_REVIEW = "pending_review"
_WORKFLOW_AUTHORING_PROPOSAL_STATUS_APPROVED = "approved"
_WORKFLOW_AUTHORING_PROPOSAL_STATUS_REJECTED = "rejected"
_WORKFLOW_AUTHORING_PROPOSAL_STATUS_ROLLED_BACK = "rolled_back"
_WORKFLOW_AUTHORING_PROPOSAL_STATUS_SUPERSEDED = "superseded"
_WORKFLOW_ROUTING_ROLE_VALUES = {"execution", "authoring", "maintenance"}


class WorkflowStudioConflictError(RuntimeError):
    """Raised when preview/apply targets a stale workflow definition."""


class WorkflowStudioAuthorityError(RuntimeError):
    """Raised when the current actor cannot load one complete workflow graph."""

    def __init__(self, error_code: str) -> None:
        self.error_code = _clean_text(error_code) or (
            "workflow_definition_not_loadable_for_actor"
        )
        super().__init__(self.error_code)


def _clean_text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list_of_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_values = [item.strip() for item in value.split(",")]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        raw_values = [str(item or "").strip() for item in value]
    else:
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        if not item or item in seen:
            continue
        seen.add(item)
        cleaned.append(item)
    return cleaned


def _json_roundtrip(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _coerce_bool(value: Any, *, default: bool | None = None) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"1", "true", "yes", "on"}:
            return True
        if cleaned in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _is_retryable_instance_error(exc: Exception) -> bool:
    if is_transient_mongo_error(exc):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_INSTANCE_ERROR_MARKERS)


def _workflow_definition_loader(workflow_id: str):
    workflow_id_clean = _clean_text(workflow_id)
    if not workflow_id_clean:
        return None
    try:
        definition, _source, _registry = _load_actor_scoped_runtime_definition(
            workflow_id_clean
        )
        return definition
    except Exception:
        logger.debug(
            "workflow studio could not load actor-scoped definition for %s",
            workflow_id_clean,
            exc_info=True,
        )
        return None


def _actor_visible_raw_concept(concept_id: str) -> dict[str, Any] | None:
    concept_id_clean = _clean_text(concept_id)
    if not concept_id_clean:
        return None
    try:
        from ..services.concept_service import _find_raw_concept_by_exact_concept_id

        # Existence and actor visibility are deliberately separate here.  The
        # ordinary exact concept reader returns the same not-found result for an
        # absent concept and one concealed from the current actor.  Treating both
        # as absent would let Studio authoring attempt to recreate a hidden ID.
        raw_concept = _find_raw_concept_by_exact_concept_id(concept_id_clean)
    except Exception as exc:
        logger.warning(
            "workflow studio concept existence authority unavailable for %s",
            concept_id_clean,
            exc_info=True,
        )
        raise WorkflowStudioAuthorityError(
            "workflow_concept_existence_authority_unavailable"
        ) from exc
    if raw_concept is None:
        return None
    try:
        if can_access_concept(concept_id_clean):
            return dict(raw_concept)
    except Exception as exc:
        logger.warning(
            "workflow studio concept visibility authority unavailable for %s",
            concept_id_clean,
            exc_info=True,
        )
        raise WorkflowStudioAuthorityError(
            "workflow_concept_existence_authority_unavailable"
        ) from exc
    raise WorkflowStudioAuthorityError(
        "workflow_definition_not_loadable_for_actor"
    )


def _actor_visible_concept_exists(concept_id: str) -> bool:
    return _actor_visible_raw_concept(concept_id) is not None


def _workflow_concept_exists(workflow_id: str) -> bool:
    return _actor_visible_concept_exists(workflow_id)


def _resolve_current_studio_actor_scope() -> WorkflowActorScope:
    """Resolve the ambient Studio actor and require one canonical namespace."""

    ambient_user_id = get_effective_user_concept_id()
    ambient_org_id = get_effective_organisation_concept_id()
    try:
        scope = resolve_authoritative_workflow_actor_scope(
            allow_unscoped_claims=False,
            ambient_user_id=ambient_user_id,
            ambient_org_id=ambient_org_id,
            ambient_context_supplied=bool(ambient_user_id or ambient_org_id),
        )
    except WorkflowActorScopeError as exc:
        raise WorkflowStudioAuthorityError(exc.reason) from exc
    if not scope.user_concept_id or not scope.namespace:
        raise WorkflowStudioAuthorityError("workflow_actor_authority_required")
    return scope


def require_workflow_studio_mutation_actor() -> WorkflowActorScope:
    """Require authenticated ambient authority before any Studio write.

    Authoring payload identity fields are collaboration metadata, not
    authentication authority.  Keeping this gate in the service means direct
    callers and HTTP routes share the same fail-closed rule.
    """

    return _resolve_current_studio_actor_scope()


def _resolve_studio_episode_actor_scope(
    claimed_namespace: str | None,
) -> WorkflowActorScope:
    """Resolve metrics scope and reject caller-selected foreign namespaces."""

    actor_scope = _resolve_current_studio_actor_scope()
    claimed = _clean_text(claimed_namespace)
    if claimed:
        canonical_claim = resolve_canonical_namespace(claimed)
        if canonical_claim != actor_scope.namespace:
            raise WorkflowStudioAuthorityError("workflow_actor_scope_mismatch")
    return actor_scope


def _load_actor_scoped_runtime_definition(
    workflow_id: str,
) -> tuple[Any, str, Any]:
    """Load a complete request-scoped graph without trusting shared cache data."""

    workflow_id_clean = _clean_text(workflow_id)
    registry = get_shared_workflow_registry_read_only(defer_parity_work=True)
    resolution = resolve_workflow_definition_from_authority(
        workflow_id_clean,
        registry=registry,
        use_current_shared_registry=False,
        register_authoritative_fallback=True,
        actor_user_id=get_effective_user_concept_id(),
        actor_org_id=get_effective_organisation_concept_id(),
    )
    if resolution.definition is None:
        raise WorkflowStudioAuthorityError(
            resolution.error_code or "workflow_definition_not_loadable_for_actor"
        )
    return (
        resolution.definition,
        resolution.registration_source or "vontology",
        resolution.registry or registry,
    )


def _load_authoring_runtime_definition(
    workflow_id: str,
) -> tuple[Any | None, str, Any]:
    """Load an existing target with actor authority, while allowing new IDs."""

    workflow_id_clean = _clean_text(workflow_id)
    registry = get_shared_workflow_registry_read_only(defer_parity_work=True)
    existing_registry_ids = set(registry.all_workflow_ids())
    if (
        workflow_id_clean not in existing_registry_ids
        and not _workflow_concept_exists(workflow_id_clean)
    ):
        return None, "new_workflow", registry
    return _load_actor_scoped_runtime_definition(workflow_id_clean)


def build_workflow_catalogue_payload(
    *,
    limit: int,
    namespace: str | None,
    session_id: str | None,
    turn_id: str | None,
    include_designs: bool = True,
) -> dict[str, Any]:
    actor_scope = _resolve_studio_episode_actor_scope(namespace)
    actor_namespace = actor_scope.namespace
    registry = get_shared_workflow_registry_read_only(defer_parity_work=True)
    inventory_snapshot = get_or_build_workflow_registry_inventory_snapshot(
        registry=registry,
        allow_sync_build=False,
    )
    if not isinstance(inventory_snapshot, dict):
        inventory_snapshot = {}

    all_workflow_ids = sorted(list(registry.all_workflow_ids()))
    introspection_workflow_ids = collect_workflow_introspection_projection_ids(
        all_workflow_ids,
        parity_inventory=inventory_snapshot,
    )
    workflow_ids = filter_workflow_ids_for_current_actor(all_workflow_ids)
    selected_ids = workflow_ids[:limit]
    usage_aggregate_map = get_workflow_usage_aggregates_for_workflows(
        selected_ids,
        namespace=actor_namespace,
        session_id=session_id,
        turn_id=turn_id,
        strict_namespace_scope=True,
    )
    episode_count_map = get_workflow_episode_counts_for_workflows(
        selected_ids,
        namespace=actor_namespace,
        session_id=session_id or None,
        turn_id=turn_id or None,
        strict_namespace_scope=True,
    )

    items: list[dict[str, Any]] = []
    for workflow_id in selected_ids:
        listing_entry = build_workflow_listing_entry(
            registry=registry,
            workflow_id=workflow_id,
            resolve_vontology_metadata=False,
        )
        usage = (
            usage_aggregate_map.get(workflow_id, {})
            if isinstance(usage_aggregate_map, dict)
            else {}
        )
        attempts = usage.get("attempts")
        completions = usage.get("completions")
        failures = usage.get("failures")
        in_progress = usage.get("in_progress")
        completion_rate = usage.get("completion_rate")
        terminal_success_rate = usage.get("terminal_success_rate")

        if not bool(listing_entry.get("definition_loaded")):
            is_executable = False
            executability_reason = "inspection_summary_pending"
            executability_detail = "lazy_definition_not_loaded"
        else:
            try:
                is_executable, executability_reason, executability_detail = (
                    classify_workflow_concept_executability(workflow_id)
                )
            except Exception as exc:
                logger.warning(
                    "workflow studio executability classification failed for %s: %s",
                    workflow_id,
                    exc,
                )
                is_executable = False
                executability_reason = "classification_error"
                executability_detail = f"classification_error:{type(exc).__name__}"

        item = {
            **listing_entry,
            "attempts": int(attempts) if isinstance(attempts, (int, float)) else 0,
            "completions": (
                int(completions) if isinstance(completions, (int, float)) else 0
            ),
            "failures": int(failures) if isinstance(failures, (int, float)) else 0,
            "in_progress": (
                int(in_progress) if isinstance(in_progress, (int, float)) else 0
            ),
            "completion_rate": (
                float(completion_rate)
                if isinstance(completion_rate, (int, float))
                else None
            ),
            "terminal_success_rate": (
                float(terminal_success_rate)
                if isinstance(terminal_success_rate, (int, float))
                else None
            ),
            "last_episode_at": (
                str(usage.get("last_episode_at"))
                if usage.get("last_episode_at") is not None
                else None
            ),
            "episodes_count": int(episode_count_map.get(workflow_id, 0)),
            "is_executable": bool(is_executable),
            "executability_reason": executability_reason,
            "executability_detail": executability_detail,
        }
        if include_designs or item["is_executable"]:
            items.append(item)

    payload = {
        "items": items,
        "count": len(items),
        "total": len(workflow_ids),
        "episodes_scope": {
            "namespace": actor_namespace,
            "session_id": session_id or None,
            "turn_id": turn_id or None,
        },
        "parity_inventory": inventory_snapshot,
    }
    return project_workflow_introspection_payload_for_current_actor(
        payload,
        workflow_ids=introspection_workflow_ids,
        total_workflow_ids=all_workflow_ids,
    )


def _build_decision_view(definition: Mapping[str, Any] | None) -> dict[str, Any]:
    graph = _as_mapping(definition)
    steps = _as_list_of_mappings(graph.get("steps"))
    decision_points: list[dict[str, Any]] = []
    terminal_steps: list[dict[str, Any]] = []

    for step in steps:
        step_id = _clean_text(step.get("step_id"))
        control_flow = _as_mapping(step.get("control_flow"))
        branch_targets = [
            {
                "reason": reason,
                "to_step": _clean_text(control_flow.get(reason_key)) or None,
            }
            for reason, reason_key in (
                ("next", "next"),
                ("on_true", "on_true"),
                ("on_false", "on_false"),
                ("on_failure", "on_failure"),
                ("on_unknown", "on_unknown"),
                ("on_approval_required", "on_approval_required"),
                ("on_break", "on_break"),
                ("on_continue", "on_continue"),
            )
            if _clean_text(control_flow.get(reason_key))
        ]
        declarative_conditions = _as_list_of_mappings(
            control_flow.get("conditions") or control_flow.get("declarative_conditions")
        )
        is_terminal = not branch_targets and not declarative_conditions
        if is_terminal:
            terminal_steps.append(
                {
                    "step_id": step_id,
                    "name": _clean_text(step.get("name")) or step_id,
                }
            )
            continue
        if len(branch_targets) > 1 or declarative_conditions or step.get("preconditions"):
            decision_points.append(
                {
                    "step_id": step_id,
                    "name": _clean_text(step.get("name")) or step_id,
                    "preconditions": list(step.get("preconditions") or []),
                    "branch_targets": branch_targets,
                    "conditions": declarative_conditions,
                }
            )

    return {
        "decision_points": decision_points,
        "terminal_steps": terminal_steps,
        "count": len(decision_points),
    }


def _build_dataflow_view(definition: Mapping[str, Any] | None) -> dict[str, Any]:
    graph = _as_mapping(definition)
    steps = _as_list_of_mappings(graph.get("steps"))
    produced_context_keys: list[str] = []
    consumed_context_symbols: list[str] = []
    subworkflows: list[str] = []
    actions: list[str] = []
    step_dataflows: list[dict[str, Any]] = []

    def _append_unique(target: list[str], value: Any) -> None:
        cleaned = _clean_text(value)
        if cleaned and cleaned not in target:
            target.append(cleaned)

    for step in steps:
        step_row = {
            "step_id": _clean_text(step.get("step_id")),
            "name": _clean_text(step.get("name")) or _clean_text(step.get("step_id")),
            "reads_variables": list(step.get("reads_variables") or []),
            "writes_variables": list(step.get("writes_variables") or []),
            "writes_context_keys": list(step.get("writes_context_keys") or []),
            "context_input_mappings": list(step.get("context_input_mappings") or []),
            "tool_output_context_mappings": list(
                step.get("tool_output_context_mappings") or []
            ),
        }
        step_dataflows.append(step_row)
        for key in step_row["writes_context_keys"]:
            _append_unique(produced_context_keys, key)
        for mapping_id in step_row["context_input_mappings"]:
            _append_unique(consumed_context_symbols, mapping_id)
        for mapping_id in step_row["tool_output_context_mappings"]:
            _append_unique(consumed_context_symbols, mapping_id)
        _append_unique(actions, step.get("invokes_action_target") or step.get("invokes_action"))
        _append_unique(subworkflows, step.get("invokes_workflow"))

    return {
        "variable_declarations": list(graph.get("variable_declarations") or []),
        "step_dataflows": step_dataflows,
        "produced_context_keys": produced_context_keys,
        "referenced_mapping_ids": consumed_context_symbols,
        "actions": actions,
        "subworkflows": subworkflows,
    }


def _build_topology_summary(definition: Mapping[str, Any] | None) -> dict[str, Any]:
    graph = _as_mapping(definition)
    steps = _as_list_of_mappings(graph.get("steps"))
    edges = _as_list_of_mappings(graph.get("edges"))
    branching_steps = 0
    for step in steps:
        step_id = _clean_text(step.get("step_id"))
        outgoing = [
            edge
            for edge in edges
            if _clean_text(edge.get("from")) == step_id and _clean_text(edge.get("to"))
        ]
        if len(outgoing) > 1:
            branching_steps += 1
    return {
        "step_count": len(steps),
        "edge_count": len(edges),
        "branching_step_count": branching_steps,
    }


def _build_operations_payload(workflow_id: str) -> dict[str, Any]:
    manager = get_instance_manager()
    actor_scope = _resolve_current_studio_actor_scope()

    schedule_items = manager.list_schedules(
        user_id=actor_scope.user_concept_id,
        workflow_id=workflow_id,
        limit=200,
    )
    schedules = []
    for schedule in schedule_items:
        schedule_namespace = resolve_canonical_namespace(
            getattr(schedule, "namespace", None),
            getattr(schedule, "user_id", None),
            getattr(schedule, "org_id", None),
        )
        if (
            _clean_text(getattr(schedule, "user_id", None))
            != actor_scope.user_concept_id
            or (_clean_text(getattr(schedule, "org_id", None)) or None)
            != actor_scope.organisation_concept_id
            or schedule_namespace != actor_scope.namespace
        ):
            continue
        schedules.append(schedule.to_status_dict())

    instances_payload: dict[str, Any]
    try:
        instance_items = manager.list_instance_status_dicts(
            user_id=actor_scope.user_concept_id,
            org_id=actor_scope.organisation_concept_id,
            namespace=actor_scope.namespace,
            workflow_id=workflow_id,
            limit=30,
        )
        instances_payload = {
            "items": instance_items,
            "count": len(instance_items),
            "active_count": len(
                [
                    item
                    for item in instance_items
                    if _clean_text(item.get("status"))
                    in WorkflowInstanceStatus.active_values()
                ]
            ),
            "degraded": False,
        }
    except Exception as exc:
        if _is_retryable_instance_error(exc):
            instances_payload = {
                "items": [],
                "count": 0,
                "active_count": 0,
                "degraded": True,
                "retryable": True,
                "error": "workflow_instances_temporarily_unavailable",
                "detail": str(exc)[:300],
                "retry_after_seconds": 2,
            }
        else:
            raise

    execution_items = list_recent_workflow_execution_traces(
        limit=12,
        namespace=actor_scope.namespace,
        workflow_id=workflow_id,
    )
    episode_items = list_workflow_use_episodes(
        workflow_id=workflow_id,
        namespace=actor_scope.namespace,
        strict_namespace_scope=True,
        limit=12,
    )

    return {
        "schedules": {
            "items": schedules,
            "count": len(schedules),
        },
        "bindings": {
            # Event bindings are global control-plane records, not actor-owned
            # workflow evidence.  They remain available through the explicit
            # trusted-operator MCP surface and are intentionally absent here.
            "items": [],
            "count": 0,
            "diagnostics": [],
            "available": False,
            "reason": "trusted_operator_surface_required",
        },
        "instances": instances_payload,
        "executions": {
            "items": execution_items,
            "count": len(execution_items),
        },
        "episodes": {
            "items": episode_items,
            "count": len(episode_items),
        },
    }


def _build_current_policy_payload(workflow_id: str) -> dict[str, Any]:
    publication_lifecycle, publication_lifecycle_source = (
        resolve_workflow_publication_lifecycle(workflow_id)
    )
    routing_profile, routing_profile_source = resolve_workflow_routing_profile(
        workflow_id
    )
    discovery_exemplars, discovery_exemplars_source = (
        resolve_workflow_discovery_exemplars(workflow_id)
    )
    background_launch_policy, background_launch_policy_source = (
        resolve_workflow_background_launch_policy(workflow_id)
    )
    launch_input_contract, launch_input_contract_source = (
        resolve_workflow_launch_input_contract(workflow_id)
    )
    return {
        "publication_lifecycle": _as_mapping(publication_lifecycle),
        "publication_lifecycle_source": publication_lifecycle_source,
        "routing_profile": _as_mapping(routing_profile),
        "routing_profile_source": routing_profile_source,
        "discovery_exemplars": _as_mapping(discovery_exemplars),
        "discovery_exemplars_source": discovery_exemplars_source,
        "background_launch_policy": _as_mapping(background_launch_policy),
        "background_launch_policy_source": background_launch_policy_source,
        "launch_input_contract": _as_mapping(launch_input_contract),
        "launch_input_contract_source": launch_input_contract_source,
    }


def _build_authoring_spec_with_policy_metadata(
    *,
    authoring_spec: Mapping[str, Any],
    policy_payload: Mapping[str, Any] | None,
    operations_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    spec = _json_roundtrip(dict(authoring_spec))
    if not isinstance(spec, dict):
        spec = dict(authoring_spec)
    workflow_metadata = _as_mapping(spec.get("workflow_metadata"))
    policy = _as_mapping(policy_payload)
    for key in (
        "routing_profile",
        "discovery_exemplars",
        "background_launch_policy",
        "launch_input_contract",
    ):
        value = policy.get(key)
        if isinstance(value, Mapping) and value:
            workflow_metadata[key] = _json_roundtrip(dict(value))
    operations = _as_mapping(operations_payload)
    bindings = _as_mapping(operations.get("bindings"))
    schedules = _as_mapping(operations.get("schedules"))
    if isinstance(bindings.get("items"), list) and bindings.get("items"):
        workflow_metadata.setdefault(
            "event_bindings",
            _json_roundtrip(bindings.get("items")),
        )
    if isinstance(schedules.get("items"), list) and schedules.get("items"):
        workflow_metadata.setdefault(
            "schedule_specs",
            _json_roundtrip(schedules.get("items")),
        )
    if workflow_metadata:
        spec["workflow_metadata"] = workflow_metadata
    return spec


def _load_workflow_authoring_proposal(workflow_id: str) -> dict[str, Any] | None:
    workflow_id_clean = _clean_text(workflow_id)
    if not workflow_id_clean:
        return None
    active_proposal_id = _load_active_workflow_authoring_proposal_id(workflow_id_clean)
    if active_proposal_id:
        proposal = _load_workflow_authoring_proposal_by_id(
            workflow_id_clean,
            active_proposal_id,
        )
        if proposal:
            return proposal

    rows = _list_workflow_authoring_proposal_rows(workflow_id_clean)
    if not rows:
        return None

    for row in rows:
        proposal = _as_mapping(row.get("proposal_payload"))
        if _clean_text(proposal.get("status")) == _WORKFLOW_AUTHORING_PROPOSAL_STATUS_PENDING_REVIEW:
            return proposal
    return _as_mapping(rows[0].get("proposal_payload")) or None


def _proposal_row_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    proposal = _as_mapping(row.get("proposal_payload"))
    return (
        _clean_text(proposal.get("updated_at_utc"))
        or _clean_text(row.get("relation_updated_at")),
        _clean_text(proposal.get("created_at_utc"))
        or _clean_text(row.get("relation_created_at")),
        _clean_text(row.get("relation_id")),
    )


def _parse_workflow_authoring_proposal_row(
    row: Mapping[str, Any],
    *,
    workflow_id: str,
) -> dict[str, Any] | None:
    text = _clean_text(row.get("text"))
    if not text:
        return None
    try:
        payload = json.loads(text)
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    proposal_payload = {
        str(key): value for key, value in payload.items() if str(key).strip()
    }
    payload_workflow_id = _clean_text(proposal_payload.get("workflow_id"))
    if payload_workflow_id and payload_workflow_id != workflow_id:
        return None
    proposal_id = (
        _clean_text(_as_mapping(row.get("context")).get("proposal_id"))
        or _clean_text(proposal_payload.get("proposal_id"))
    )
    if not proposal_id:
        return None
    proposal_payload["proposal_id"] = proposal_id
    if not payload_workflow_id:
        proposal_payload["workflow_id"] = workflow_id
    return {
        **dict(row),
        "proposal_id": proposal_id,
        "proposal_payload": proposal_payload,
    }


def _list_workflow_authoring_proposal_rows(workflow_id: str) -> list[dict[str, Any]]:
    rows = get_texts_for_concept(
        subject_concept_id=workflow_id,
        predicate=_WORKFLOW_AUTHORING_PROPOSAL_TEXT_PREDICATE,
        limit=200,
    )
    if not isinstance(rows, list) or not rows:
        return []
    parsed_rows: list[dict[str, Any]] = []
    for row in rows:
        parsed = _parse_workflow_authoring_proposal_row(
            _as_mapping(row),
            workflow_id=workflow_id,
        )
        if parsed:
            parsed_rows.append(parsed)
    parsed_rows.sort(key=_proposal_row_sort_key, reverse=True)
    return parsed_rows


def _load_active_workflow_authoring_proposal_id(workflow_id: str) -> str | None:
    rows = get_texts_for_concept(
        subject_concept_id=workflow_id,
        predicate=_WORKFLOW_ACTIVE_AUTHORING_PROPOSAL_ID_TEXT_PREDICATE,
        limit=5,
    )
    if not isinstance(rows, list) or not rows:
        return None
    rows_sorted = sorted(
        (_as_mapping(row) for row in rows),
        key=lambda row: (
            _clean_text(row.get("relation_updated_at"))
            or _clean_text(row.get("relation_created_at")),
            _clean_text(row.get("relation_id")),
        ),
        reverse=True,
    )
    for row in rows_sorted:
        proposal_id = _clean_text(row.get("text"))
        if proposal_id:
            return proposal_id
    return None


def _load_workflow_authoring_proposal_by_id(
    workflow_id: str,
    proposal_id: str,
) -> dict[str, Any] | None:
    workflow_id_clean = _clean_text(workflow_id)
    proposal_id_clean = _clean_text(proposal_id)
    if not workflow_id_clean or not proposal_id_clean:
        return None
    for row in _list_workflow_authoring_proposal_rows(workflow_id_clean):
        if _clean_text(row.get("proposal_id")) != proposal_id_clean:
            continue
        return _as_mapping(row.get("proposal_payload")) or None
    return None


def _store_active_workflow_authoring_proposal_id(
    workflow_id: str,
    proposal_id: str,
) -> None:
    workflow_id_clean = _clean_text(workflow_id)
    proposal_id_clean = _clean_text(proposal_id)
    if not workflow_id_clean or not proposal_id_clean:
        return None
    upsert_singleton_text_relation(
        subject_concept_id=workflow_id_clean,
        predicate=_WORKFLOW_ACTIVE_AUTHORING_PROPOSAL_ID_TEXT_PREDICATE,
        text=proposal_id_clean,
        lang="en-NZ",
        context={"source": "workflow_studio_service"},
        garbage_collect=True,
    )


def _store_workflow_authoring_proposal(
    workflow_id: str,
    proposal_payload: Mapping[str, Any],
    *,
    update_active_pointer: bool | None = None,
) -> dict[str, Any]:
    payload = _json_roundtrip(dict(proposal_payload))
    workflow_id_clean = _clean_text(workflow_id)
    proposal_id = _clean_text(payload.get("proposal_id"))
    if not workflow_id_clean:
        raise ValueError("workflow_id_required")
    if not proposal_id:
        raise ValueError("workflow_authoring_proposal_id_required")
    payload["workflow_id"] = _clean_text(payload.get("workflow_id")) or workflow_id_clean
    stored = upsert_text_for_concept(
        subject_concept_id=workflow_id_clean,
        predicate=_WORKFLOW_AUTHORING_PROPOSAL_TEXT_PREDICATE,
        text=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        lang="en-NZ",
        context={
            "source": "workflow_studio_service",
            "proposal_id": proposal_id,
        },
    )
    kept_relation_id = _clean_text(stored.get("relation_id"))

    for row in _list_workflow_authoring_proposal_rows(workflow_id_clean):
        relation_id = _clean_text(row.get("relation_id"))
        if (
            _clean_text(row.get("proposal_id")) != proposal_id
            or not relation_id
            or relation_id == kept_relation_id
        ):
            continue
        delete_text_relation(
            workflow_id_clean,
            relation_id,
            garbage_collect=True,
        )

    if update_active_pointer is None:
        current_active_proposal_id = _load_active_workflow_authoring_proposal_id(
            workflow_id_clean
        )
        update_active_pointer = (
            not current_active_proposal_id or current_active_proposal_id == proposal_id
        )
    if update_active_pointer:
        _store_active_workflow_authoring_proposal_id(workflow_id_clean, proposal_id)

    return payload if isinstance(payload, dict) else dict(proposal_payload)


def _proposal_summary(proposal_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    proposal = _as_mapping(proposal_payload)
    if not proposal:
        return {"available": False, "active": False}
    status = _clean_text(proposal.get("status")) or "unknown"
    candidate_validation = _as_mapping(proposal.get("candidate_validation"))
    preview_summary = _as_mapping(proposal.get("preview_summary"))
    proposal_context = _as_mapping(proposal.get("proposal_context"))
    promotion_evaluation = _as_mapping(proposal.get("promotion_evaluation"))
    return {
        "available": True,
        "active": status == _WORKFLOW_AUTHORING_PROPOSAL_STATUS_PENDING_REVIEW,
        "proposal_id": _clean_text(proposal.get("proposal_id")) or None,
        "status": status,
        "created_at_utc": proposal.get("created_at_utc"),
        "updated_at_utc": proposal.get("updated_at_utc"),
        "created_by": proposal.get("created_by"),
        "reviewed_at_utc": proposal.get("reviewed_at_utc"),
        "reviewed_by": proposal.get("reviewed_by"),
        "review_reason": _clean_text(proposal.get("review_reason")) or None,
        "candidate_validation": candidate_validation or None,
        "preview_summary": preview_summary or None,
        "authoring_spec": proposal.get("authoring_spec")
        if isinstance(proposal.get("authoring_spec"), Mapping)
        else None,
        "proposal_context": proposal_context or None,
        "promotion_evaluation": promotion_evaluation or None,
        "previous_authoring_spec_available": isinstance(
            proposal.get("previous_authoring_spec"),
            Mapping,
        ),
    }


def get_workflow_authoring_proposal(workflow_id: str) -> dict[str, Any] | None:
    """Return the full stored workflow-authoring proposal payload when present."""

    workflow_id_clean = _clean_text(workflow_id)
    if not workflow_id_clean:
        return None
    proposal = _load_workflow_authoring_proposal(workflow_id_clean)
    return _as_mapping(proposal) or None


def get_workflow_authoring_proposal_by_id(
    workflow_id: str,
    proposal_id: str,
) -> dict[str, Any] | None:
    """Return an exact stored workflow-authoring proposal payload when present."""

    workflow_id_clean = _clean_text(workflow_id)
    proposal_id_clean = _clean_text(proposal_id)
    if not workflow_id_clean or not proposal_id_clean:
        return None
    proposal = _load_workflow_authoring_proposal_by_id(
        workflow_id_clean,
        proposal_id_clean,
    )
    return _as_mapping(proposal) or None


def _normalise_routing_profile(value: Any) -> dict[str, Any] | None:
    raw = _as_mapping(value)
    if not raw:
        return None
    role = _clean_text(raw.get("role")).lower().replace("-", "_")
    if role not in _WORKFLOW_ROUTING_ROLE_VALUES:
        return None
    payload = {
        "schema_version": "workflow_routing_profile.v1",
        "role": role,
        "authoring_intent_required": bool(
            _coerce_bool(raw.get("authoring_intent_required"), default=role == "authoring")
        ),
        "explicit_workflow_context_required": bool(
            _coerce_bool(
                raw.get("explicit_workflow_context_required"),
                default=role == "maintenance",
            )
        ),
        "prefer_existing_capability": bool(
            _coerce_bool(raw.get("prefer_existing_capability"), default=role == "authoring")
        ),
    }
    routing_eligible = _coerce_bool(raw.get("routing_eligible"))
    if routing_eligible is not None:
        payload["routing_eligible"] = routing_eligible
    execution_mode = (
        _clean_text(
            raw.get("execution_mode")
            or raw.get("selected_execution_mode")
            or raw.get("dispatch_execution_mode")
        )
        .lower()
        .replace("-", "_")
    )
    if execution_mode in {"custom_workflow", "direct_response", "tool_pipeline"}:
        payload["execution_mode"] = execution_mode
    return payload


def _normalise_discovery_exemplars(value: Any) -> dict[str, Any] | None:
    raw = _as_mapping(value)
    if not raw:
        return None
    keywords = _clean_string_list(raw.get("keywords"))
    examples = _clean_string_list(raw.get("examples") or raw.get("exemplars"))
    routing_notes = _clean_string_list(raw.get("routing_notes"))
    required_query_cues = _clean_string_list(
        raw.get("required_query_cues")
        or raw.get("required_cues")
        or raw.get("source_required_cues")
    )
    negative_query_cues = _clean_string_list(
        raw.get("negative_query_cues") or raw.get("negative_keywords")
    )
    excluded_query_cues = _clean_string_list(
        raw.get("excluded_query_cues") or raw.get("exclude_query_cues")
    )
    if (
        not keywords
        and not examples
        and not routing_notes
        and not required_query_cues
        and not negative_query_cues
        and not excluded_query_cues
    ):
        return None
    payload = {
        "schema_version": "workflow_discovery_exemplars.v1",
        "keywords": keywords,
        "examples": examples,
    }
    if routing_notes:
        payload["routing_notes"] = routing_notes
    if required_query_cues:
        payload["required_query_cues"] = required_query_cues
    if negative_query_cues:
        payload["negative_query_cues"] = negative_query_cues
    if excluded_query_cues:
        payload["excluded_query_cues"] = excluded_query_cues
    return payload


def _normalise_background_launch_policy(value: Any) -> dict[str, Any] | None:
    raw = _as_mapping(value)
    if not raw:
        return None
    interval_seconds = _coerce_positive_int(raw.get("min_interval_seconds"))
    if interval_seconds is None:
        interval_minutes = _coerce_positive_int(raw.get("min_interval_minutes"))
        if interval_minutes is not None:
            interval_seconds = interval_minutes * 60
    enabled = bool(
        _coerce_bool(raw.get("enabled"), default=bool(interval_seconds))
    )
    return {
        "schema_version": "workflow_background_launch_policy.v1",
        "enabled": enabled and bool(interval_seconds),
        "min_interval_seconds": interval_seconds or 0,
        "scope": _clean_text(raw.get("scope")) or "global_per_server",
        "applies_to_sources": _clean_string_list(
            raw.get("applies_to_sources") or raw.get("applies_to")
        )
        or ["event"],
    }


def _normalise_event_binding_specs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    specs: list[dict[str, Any]] = []
    for item in value:
        raw = _as_mapping(item)
        event_type = _clean_text(raw.get("event_type"))
        if not event_type:
            continue
        input_mapping_raw = _as_mapping(raw.get("input_mapping"))
        input_mapping = {
            _clean_text(key): _clean_text(value)
            for key, value in input_mapping_raw.items()
            if _clean_text(key) and _clean_text(value)
        }
        specs.append(
            {
                "event_type": event_type,
                "input_mapping": input_mapping,
                "enabled": bool(_coerce_bool(raw.get("enabled"), default=True)),
            }
        )
    return specs


def _normalise_schedule_specs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    specs: list[dict[str, Any]] = []
    for item in value:
        raw = _as_mapping(item)
        schedule_type = _clean_text(raw.get("schedule_type")).lower()
        if schedule_type not in {"interval", "cron", "once"}:
            continue
        row: dict[str, Any] = {
            "schedule_type": schedule_type,
            "enabled": bool(_coerce_bool(raw.get("enabled"), default=True)),
            "description": _clean_text(raw.get("description")) or None,
            "default_inputs": _as_mapping(raw.get("default_inputs")),
        }
        interval_seconds = _coerce_positive_int(raw.get("interval_seconds"))
        if interval_seconds is not None:
            row["interval_seconds"] = interval_seconds
        cron_expression = _clean_text(raw.get("cron_expression"))
        if cron_expression:
            row["cron_expression"] = cron_expression
        run_at = _clean_text(raw.get("run_at"))
        if run_at:
            row["run_at"] = run_at
        specs.append(row)
    return specs


def _metadata_routing_eligible(
    *,
    publication_lifecycle: Mapping[str, Any] | None,
    routing_profile: Mapping[str, Any] | None,
) -> bool:
    lifecycle = _as_mapping(publication_lifecycle)
    if isinstance(lifecycle.get("routing_eligible"), bool):
        return bool(lifecycle.get("routing_eligible"))
    profile = _as_mapping(routing_profile)
    if isinstance(profile.get("routing_eligible"), bool):
        return bool(profile.get("routing_eligible"))
    return bool(lifecycle.get("published", True))


def _lifecycle_passthrough(lifecycle: Mapping[str, Any] | None) -> dict[str, Any]:
    current = _as_mapping(lifecycle)
    passthrough: dict[str, Any] = {}
    for key in (
        "validation_passed",
        "postconditions_verified",
        "optional_test_instance_id",
        "last_error",
        "review_state",
        "review_reason",
        "reviewed_at",
        "reviewed_by",
        "proposal_id",
        "proposal_source_session_id",
        "proposal_source_turn_id",
        "experiment_run_id",
        "supersedes_workflow_id",
        "superseded_by_workflow_id",
        "routing_eligible",
        "rollout_state",
        "approval_required",
        "promotion_decision",
        "event_binding_ids",
        "schedule_ids",
    ):
        if key in current:
            passthrough[key] = current.get(key)
    return passthrough


def _apply_workflow_policy_metadata(
    *,
    workflow_id: str,
    workflow_metadata: Mapping[str, Any] | None,
    user_id: str | None = None,
    org_id: str | None = None,
    namespace: str | None = None,
    policy_keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    metadata = _as_mapping(workflow_metadata)
    selected_policy_keys = (
        {_clean_text(item) for item in policy_keys if _clean_text(item)}
        if policy_keys is not None
        else None
    )

    def _selected(key: str) -> bool:
        return selected_policy_keys is None or key in selected_policy_keys

    applied: dict[str, Any] = {
        "routing_profile": None,
        "discovery_exemplars": None,
        "background_launch_policy": None,
        "launch_input_contract": None,
        "required_effects_contract": None,
        "event_binding_ids": [],
        "schedule_ids": [],
        "disabled_binding_ids": [],
        "disabled_schedule_ids": [],
    }

    routing_profile = (
        _normalise_routing_profile(metadata.get("routing_profile"))
        if _selected("routing_profile")
        else None
    )
    if routing_profile is not None:
        applied["routing_profile"] = upsert_workflow_json_policy_text(
            workflow_id=workflow_id,
            predicate=WORKFLOW_ROUTING_PROFILE_TEXT_PREDICATE,
            payload=routing_profile,
            context={"source": "workflow_studio_service"},
        )

    discovery_exemplars = (
        _normalise_discovery_exemplars(metadata.get("discovery_exemplars"))
        if _selected("discovery_exemplars")
        else None
    )
    if discovery_exemplars is not None:
        applied["discovery_exemplars"] = upsert_workflow_json_policy_text(
            workflow_id=workflow_id,
            predicate=WORKFLOW_DISCOVERY_EXEMPLARS_TEXT_PREDICATE,
            payload=discovery_exemplars,
            context={"source": "workflow_studio_service"},
        )

    background_launch_policy = (
        _normalise_background_launch_policy(metadata.get("background_launch_policy"))
        if _selected("background_launch_policy")
        else None
    )
    if background_launch_policy is not None:
        applied["background_launch_policy"] = upsert_workflow_json_policy_text(
            workflow_id=workflow_id,
            predicate=WORKFLOW_BACKGROUND_LAUNCH_POLICY_TEXT_PREDICATE,
            payload=background_launch_policy,
            context={"source": "workflow_studio_service"},
        )

    launch_input_contract, launch_input_contract_error = (
        normalise_workflow_launch_input_contract(metadata.get("launch_input_contract"))
        if _selected("launch_input_contract")
        else (None, None)
    )
    if (
        _selected("launch_input_contract")
        and metadata.get("launch_input_contract") is not None
        and launch_input_contract is None
    ):
        raise ValueError(
            "workflow_launch_input_contract_invalid:"
            f"{launch_input_contract_error or 'invalid_contract'}"
        )
    if launch_input_contract is not None:
        applied["launch_input_contract"] = upsert_workflow_json_policy_text(
            workflow_id=workflow_id,
            predicate=WORKFLOW_LAUNCH_INPUT_CONTRACT_TEXT_PREDICATE,
            payload=launch_input_contract,
            context={"source": "workflow_studio_service"},
        )

    required_effects_contract = (
        normalise_workflow_required_effects_contract(
            metadata.get("required_effects_contract")
        )
        if _selected("required_effects_contract")
        else None
    )
    if required_effects_contract is not None:
        applied["required_effects_contract"] = upsert_workflow_json_policy_text(
            workflow_id=workflow_id,
            predicate=WORKFLOW_REQUIRED_EFFECTS_CONTRACT_TEXT_PREDICATE,
            payload=required_effects_contract,
            context={"source": "workflow_studio_service"},
        )

    manager = (
        get_instance_manager()
        if (_selected("event_bindings") and "event_bindings" in metadata)
        or (_selected("schedule_specs") and "schedule_specs" in metadata)
        else None
    )

    if _selected("event_bindings") and "event_bindings" in metadata:
        assert manager is not None
        desired_bindings = _normalise_event_binding_specs(metadata.get("event_bindings"))
        existing_bindings = [
            binding
            for binding in manager.list_event_bindings(limit=500)
            if _clean_text(getattr(binding, "workflow_id", "")) == workflow_id
        ]
        desired_event_types = {item["event_type"] for item in desired_bindings}
        for binding in existing_bindings:
            binding_id = _clean_text(getattr(binding, "binding_id", ""))
            if (
                binding_id
                and _clean_text(getattr(binding, "event_type", "")) not in desired_event_types
            ):
                if manager.set_event_binding_enabled(binding_id, enabled=False):
                    applied["disabled_binding_ids"].append(binding_id)
        actor = _clean_text(user_id) or None
        for spec in desired_bindings:
            binding, _created, _updated = manager.upsert_event_binding(
                event_type=spec["event_type"],
                workflow_id=workflow_id,
                input_mapping=spec["input_mapping"],
                enabled=spec["enabled"],
                actor=actor,
                replace_existing=True,
            )
            binding_id = _clean_text(getattr(binding, "binding_id", ""))
            if binding_id:
                applied["event_binding_ids"].append(binding_id)

    if _selected("schedule_specs") and "schedule_specs" in metadata:
        assert manager is not None
        desired_schedules = _normalise_schedule_specs(metadata.get("schedule_specs"))
        existing_schedules = [
            schedule
            for schedule in manager.list_schedules(limit=200)
            if _clean_text(getattr(schedule, "workflow_id", "")) == workflow_id
            and getattr(schedule, "origin", "legacy_unmanaged") == "workflow_release"
        ]
        for schedule in existing_schedules:
            schedule_id = _clean_text(getattr(schedule, "schedule_id", ""))
            if schedule_id and manager.set_schedule_enabled(schedule_id, False):
                applied["disabled_schedule_ids"].append(schedule_id)
        actor_user = _clean_text(user_id) or "workflow_studio"
        actor_org = _clean_text(org_id) or "default"
        actor_namespace = _clean_text(namespace) or actor_user or "#V#workflow_studio"
        for spec in desired_schedules:
            schedule_type = spec["schedule_type"]
            default_inputs = _as_mapping(spec.get("default_inputs"))
            description = _clean_text(spec.get("description")) or None
            if schedule_type == "interval":
                interval_seconds = _coerce_positive_int(spec.get("interval_seconds"))
                if interval_seconds is None:
                    continue
                schedule = WorkflowSchedule.create_interval(
                    workflow_id,
                    interval_seconds,
                    user_id=actor_user,
                    org_id=actor_org,
                    namespace=actor_namespace,
                    default_inputs=default_inputs,
                    description=description,
                )
            elif schedule_type == "cron":
                cron_expression = _clean_text(spec.get("cron_expression"))
                if not cron_expression:
                    continue
                schedule = WorkflowSchedule.create_cron(
                    workflow_id,
                    cron_expression,
                    user_id=actor_user,
                    org_id=actor_org,
                    namespace=actor_namespace,
                    default_inputs=default_inputs,
                    description=description,
                )
            else:
                run_at = _clean_text(spec.get("run_at"))
                if not run_at:
                    continue
                try:
                    run_at_dt = datetime.fromisoformat(run_at)
                except Exception:
                    continue
                schedule = WorkflowSchedule.create_once(
                    workflow_id,
                    run_at_dt,
                    user_id=actor_user,
                    org_id=actor_org,
                    namespace=actor_namespace,
                    default_inputs=default_inputs,
                    description=description,
                )
            schedule.origin = "workflow_release"
            schedule.enabled = bool(spec.get("enabled", True))
            schedule_id = manager.create_schedule(schedule)
            applied["schedule_ids"].append(schedule_id)

    return applied


def _set_workflow_runtime_enablement(workflow_id: str, *, enabled: bool) -> dict[str, Any]:
    manager = get_instance_manager()
    binding_ids: list[str] = []
    schedule_ids: list[str] = []
    for binding in manager.list_event_bindings(limit=500):
        if _clean_text(getattr(binding, "workflow_id", "")) != workflow_id:
            continue
        binding_id = _clean_text(getattr(binding, "binding_id", ""))
        if binding_id and manager.set_event_binding_enabled(binding_id, enabled=enabled):
            binding_ids.append(binding_id)
    for schedule in manager.list_schedules(limit=200):
        if _clean_text(getattr(schedule, "workflow_id", "")) != workflow_id:
            continue
        schedule_id = _clean_text(getattr(schedule, "schedule_id", ""))
        if schedule_id and manager.set_schedule_enabled(schedule_id, enabled):
            schedule_ids.append(schedule_id)
    return {
        "binding_ids": binding_ids,
        "schedule_ids": schedule_ids,
        "enabled": enabled,
    }


def _build_authoring_payload(
    *,
    workflow_id: str,
    definition: Any | None,
    source: str,
    operations_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy_payload = _build_current_policy_payload(workflow_id)
    prompt_health = get_workflow_authoring_prompt_health_status()
    if definition is None:
        return {
            "available": False,
            "reason": "workflow_definition_not_loaded",
            "policy": policy_payload,
            "prompt_contract": build_workflow_authoring_prompt_contract(),
            "prompt_health": prompt_health,
        }

    authoring_spec = serialise_workflow_definition_to_authoring_spec(definition)
    authoring_spec = _build_authoring_spec_with_policy_metadata(
        authoring_spec=authoring_spec,
        policy_payload=policy_payload,
        operations_payload=operations_payload,
    )
    definition_identity = build_workflow_definition_identity(
        workflow_id=workflow_id,
        source=source,
        definition=definition,
        authoritative_definition=definition if source.lower() == "vontology" else None,
    )
    validation = validate_workflow_definition_contract(
        definition=definition,
        supported_action_ids=get_supported_durable_workflow_action_ids(),
        enforce_supported_actions=True,
        known_workflow_ids=get_shared_workflow_registry_read_only(
            defer_parity_work=True
        ).all_workflow_ids(),
        workflow_definition_loader=_workflow_definition_loader,
    )
    return {
        "available": True,
        "current_spec": authoring_spec,
        "base_definition_hash": definition_identity.get("definition_hash"),
        "validation": validation,
        "policy": policy_payload,
        "prompt_contract": build_workflow_authoring_prompt_contract(),
        "prompt_health": prompt_health,
    }


def _summarise_authoring_diff(
    *,
    current_spec: Mapping[str, Any],
    proposed_spec: Mapping[str, Any],
) -> dict[str, Any]:
    current_steps = {
        _clean_text(step.get("state_id") or step.get("state_key")): dict(step)
        for step in (current_spec.get("steps") or [])
        if isinstance(step, Mapping)
    }
    proposed_steps = {
        _clean_text(step.get("state_id") or step.get("state_key")): dict(step)
        for step in (proposed_spec.get("steps") or [])
        if isinstance(step, Mapping)
    }
    current_ids = set(current_steps.keys())
    proposed_ids = set(proposed_steps.keys())
    added_state_ids = sorted(item for item in proposed_ids - current_ids if item)
    removed_state_ids = sorted(item for item in current_ids - proposed_ids if item)
    updated_state_ids = sorted(
        state_id
        for state_id in proposed_ids & current_ids
        if proposed_steps.get(state_id) != current_steps.get(state_id)
    )
    return {
        "workflow_description_changed": (
            _clean_text(current_spec.get("description") or current_spec.get("workflow_description"))
            != _clean_text(proposed_spec.get("description") or proposed_spec.get("workflow_description"))
        ),
        "initial_state_changed": (
            _clean_text(current_spec.get("initial_state_key"))
            != _clean_text(proposed_spec.get("initial_state_key"))
        ),
        "added_state_ids": added_state_ids,
        "removed_state_ids": removed_state_ids,
        "updated_state_ids": updated_state_ids,
        "changed_state_count": len(added_state_ids)
        + len(removed_state_ids)
        + len(updated_state_ids),
    }


def _build_improvement_guidance_payload(
    workflow_id: str,
    *,
    namespace: str | None,
) -> dict[str, Any]:
    items = list_recent_workflow_improvement_suggestions(
        workflow_id,
        namespace=namespace,
        limit=8,
    )
    categories = _clean_string_list([item.get("category") for item in items])
    high_priority_count = sum(
        1 for item in items if _clean_text(item.get("priority")) == "high"
    )
    return {
        "workflow_id": workflow_id,
        "available": bool(items),
        "count": len(items),
        "high_priority_count": high_priority_count,
        "categories": categories,
        "source": "episode_critique_memory_projection",
        "items": items,
    }


def build_workflow_studio_detail_payload(
    workflow_id: str,
    *,
    namespace: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    workflow_id_clean = _clean_text(workflow_id)
    if not workflow_id_clean:
        raise ValueError("workflow_id is required")

    runtime_definition, source, registry = _load_actor_scoped_runtime_definition(
        workflow_id_clean
    )
    if runtime_definition is not None:
        definition_graph = build_workflow_process_graph_from_definition(runtime_definition)
        warnings = list((definition_graph or {}).get("warnings") or [])
    else:
        definition_graph, warnings = build_workflow_process_graph(workflow_id_clean)
    raw, raw_source = resolve_workflow_narrative_text(workflow_id_clean)

    actor_scope = _resolve_studio_episode_actor_scope(namespace)
    listing_entry = build_workflow_listing_entry(
        registry=registry,
        workflow_id=workflow_id_clean,
    )
    usage_aggregate_map = get_workflow_usage_aggregates_for_workflows(
        [workflow_id_clean],
        namespace=actor_scope.namespace,
        session_id=session_id,
        turn_id=turn_id,
        strict_namespace_scope=True,
    )
    usage = (
        usage_aggregate_map.get(workflow_id_clean, {})
        if isinstance(usage_aggregate_map, dict)
        else {}
    )
    try:
        is_executable, executability_reason, executability_detail = (
            classify_workflow_concept_executability(workflow_id_clean)
        )
    except Exception as exc:
        logger.warning(
            "workflow studio detail executability classification failed for %s: %s",
            workflow_id_clean,
            exc,
        )
        is_executable = False
        executability_reason = "classification_error"
        executability_detail = f"classification_error:{type(exc).__name__}"

    if definition_graph:
        definition_identity = build_workflow_definition_identity_from_graph(
            workflow_id=workflow_id_clean,
            graph=definition_graph if isinstance(definition_graph, dict) else None,
        )
    elif runtime_definition is not None:
        definition_identity = build_workflow_definition_identity(
            workflow_id=workflow_id_clean,
            source=source,
            definition=runtime_definition,
            authoritative_definition=(
                runtime_definition if source.lower() == "vontology" else None
            ),
        )
    else:
        definition_identity = None

    operations = _build_operations_payload(workflow_id_clean)
    episodes_count = count_workflow_use_episodes(
        workflow_id=workflow_id_clean,
        namespace=actor_scope.namespace,
        session_id=session_id or None,
        turn_id=turn_id or None,
        strict_namespace_scope=True,
    )

    completion_rate = usage.get("completion_rate")
    current_policy = _build_current_policy_payload(workflow_id_clean)
    current_proposal = _proposal_summary(
        _load_workflow_authoring_proposal(workflow_id_clean)
    )
    improvement_guidance = _build_improvement_guidance_payload(
        workflow_id_clean,
        namespace=actor_scope.namespace,
    )

    return {
        "workflow_id": workflow_id_clean,
        "summary": {
            **listing_entry,
            "attempts": int(usage.get("attempts") or 0),
            "completions": int(usage.get("completions") or 0),
            "failures": int(usage.get("failures") or 0),
            "in_progress": int(usage.get("in_progress") or 0),
            "completion_rate": (
                float(completion_rate)
                if isinstance(completion_rate, (int, float))
                else None
            ),
            "terminal_success_rate": (
                float(usage.get("terminal_success_rate"))
                if isinstance(usage.get("terminal_success_rate"), (int, float))
                else None
            ),
            "last_episode_at": usage.get("last_episode_at"),
            "episodes_count": int(episodes_count or 0),
            "is_executable": bool(is_executable),
            "executability_reason": executability_reason,
            "executability_detail": executability_detail,
            "definition_identity": definition_identity,
            "publication_lifecycle": current_policy.get("publication_lifecycle") or None,
            "routing_profile": current_policy.get("routing_profile") or None,
            "improvement_suggestion_count": improvement_guidance.get("count") or 0,
        },
        "authority": {
            "authoritative_store": "vontology",
            "derived_view_model": "workflow_studio.read_model.v1",
            "preview_required": True,
            "supported_edit_modes": [
                "authoring_spec",
                "workflow_description_proposal",
                "authoring_proposal_review",
            ],
            "runtime_source": source,
        },
        "views": {
            "topology": {
                "definition": definition_graph,
                "summary": _build_topology_summary(definition_graph),
            },
            "decision": _build_decision_view(definition_graph),
            "dataflow": _build_dataflow_view(definition_graph),
        },
        "operations": operations,
        "authoring": _build_authoring_payload(
            workflow_id=workflow_id_clean,
            definition=runtime_definition,
            source=source,
            operations_payload=operations,
        ),
        "proposal": current_proposal,
        "improvement_guidance": improvement_guidance,
        "raw": raw,
        "raw_source": raw_source,
        "warnings": list(warnings or []),
    }


def _ensure_current_hash_matches(
    *,
    workflow_id: str,
    runtime_definition: Any | None,
    runtime_source: str,
    expected_hash: str | None,
) -> None:
    expected_hash_clean = _clean_text(expected_hash)
    if not expected_hash_clean or runtime_definition is None:
        return
    current_identity = build_workflow_definition_identity(
        workflow_id=workflow_id,
        source=runtime_source or "unknown",
        definition=runtime_definition,
        authoritative_definition=(
            runtime_definition if runtime_source.lower() == "vontology" else None
        ),
    )
    current_hash = _clean_text(current_identity.get("definition_hash"))
    if current_hash != expected_hash_clean:
        raise WorkflowStudioConflictError("workflow_definition_hash_conflict")


def _normalise_candidate_validation_profile(value: Any) -> str:
    profile = _clean_text(value).lower()
    if profile == _WORKFLOW_CANDIDATE_VALIDATION_PROFILE_CONTRACT_ONLY:
        return profile
    return _WORKFLOW_CANDIDATE_VALIDATION_PROFILE_GENERATION_SAFE


def _build_generation_safe_validation(
    *,
    authoring_spec: Mapping[str, Any],
    contract_validation: Mapping[str, Any],
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    publication_spec = _as_mapping(authoring_spec.get("publication_spec"))
    steps = authoring_spec.get("steps")
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes, bytearray)):
        steps = publication_spec.get("steps")
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes, bytearray)):
        steps = []

    executable_step_count = 0
    llm_step_count = 0
    for raw_step in steps:
        if not isinstance(raw_step, Mapping):
            continue
        step = dict(raw_step)
        state_id = _clean_text(step.get("state_id")) or None
        action_id = _clean_text(step.get("action_id"))
        invoked_workflow_id = _clean_text(step.get("invoked_workflow_id"))
        execution_mode = _clean_text(step.get("execution_mode")).lower()
        conditional_transitions = step.get("conditional_transitions")
        has_conditional_transitions = isinstance(conditional_transitions, list) and bool(
            conditional_transitions
        )
        has_failure_path = bool(_clean_text(step.get("on_failure_state"))) or has_conditional_transitions
        is_executable = bool(action_id or invoked_workflow_id)
        if not is_executable:
            continue
        executable_step_count += 1

        if not has_failure_path:
            errors.append(
                {
                    "state_id": state_id,
                    "reason_code": "executable_step_missing_failure_path",
                    "severity": "error",
                }
            )

        if action_id == "llm.action" or execution_mode == "llm":
            llm_step_count += 1
            if not isinstance(step.get("validation_policy"), Mapping):
                errors.append(
                    {
                        "state_id": state_id,
                        "reason_code": "llm_step_missing_validation_policy",
                        "severity": "error",
                    }
                )
            writes_context_keys = step.get("writes_context_keys")
            tool_output_mapping_specs = step.get("tool_output_mapping_specs")
            if (
                isinstance(writes_context_keys, list)
                and writes_context_keys
                and (not isinstance(tool_output_mapping_specs, list) or not tool_output_mapping_specs)
            ):
                warnings.append(
                    {
                        "state_id": state_id,
                        "reason_code": "llm_step_writes_context_without_output_mapping",
                        "severity": "warning",
                    }
                )

    if executable_step_count == 0:
        errors.append(
            {
                "state_id": None,
                "reason_code": "workflow_has_no_executable_steps",
                "severity": "error",
            }
        )

    contract_errors = contract_validation.get("errors")
    if isinstance(contract_errors, list) and contract_errors:
        warnings.append(
            {
                "state_id": None,
                "reason_code": "contract_validation_errors_present",
                "severity": "warning",
            }
        )

    return {
        "profile": _WORKFLOW_CANDIDATE_VALIDATION_PROFILE_GENERATION_SAFE,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "executable_step_count": executable_step_count,
            "llm_step_count": llm_step_count,
        },
    }


def _build_candidate_repair_hints(
    *,
    contract_validation: Mapping[str, Any],
    generation_safe_validation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []

    contract_errors = contract_validation.get("errors")
    if isinstance(contract_errors, list):
        for reason_code in contract_errors:
            code = _clean_text(reason_code)
            if not code:
                continue
            hints.append(
                {
                    "reason_code": code,
                    "repair_hint": (
                        "Fix the candidate authoring spec so the workflow contract validates "
                        "cleanly before publication or bounded execution."
                    ),
                    "scope": "workflow_contract",
                }
            )

    generation_safe_errors = generation_safe_validation.get("errors")
    if isinstance(generation_safe_errors, list):
        for item in generation_safe_errors:
            issue = dict(item) if isinstance(item, Mapping) else {}
            code = _clean_text(issue.get("reason_code"))
            if not code:
                continue
            repair_hint = "Repair the candidate workflow so it is safe to execute on the generation path."
            if code == "llm_step_missing_validation_policy":
                repair_hint = "Add a validation policy to every LLM execution step."
            elif code == "executable_step_missing_failure_path":
                repair_hint = "Add an explicit failure path for every executable step."
            elif code == "workflow_has_no_executable_steps":
                repair_hint = "Add at least one executable workflow step before publication."
            hints.append(
                {
                    "reason_code": code,
                    "repair_hint": repair_hint,
                    "scope": "generation_safety",
                    "state_id": issue.get("state_id"),
                }
            )

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for hint in hints:
        key = (
            _clean_text(hint.get("scope")),
            _clean_text(hint.get("reason_code")),
            _clean_text(hint.get("state_id")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hint)
    return deduped


def validate_workflow_candidate(
    workflow_id: str,
    *,
    authoring_spec: Mapping[str, Any],
    base_definition_hash: str | None = None,
    validation_profile: str | None = None,
    include_preview: bool = False,
) -> dict[str, Any]:
    preview_payload = preview_workflow_authoring_spec(
        workflow_id,
        authoring_spec=authoring_spec,
        base_definition_hash=base_definition_hash,
    )
    preview = _as_mapping(preview_payload.get("preview"))
    contract_validation = _as_mapping(preview.get("contract_validation"))
    resolved_profile = _normalise_candidate_validation_profile(validation_profile)
    generation_safe_validation = _build_generation_safe_validation(
        authoring_spec=authoring_spec,
        contract_validation=contract_validation,
    )

    contract_valid = bool(contract_validation.get("valid"))
    generation_safe_valid = bool(generation_safe_validation.get("valid"))
    valid = contract_valid
    if resolved_profile == _WORKFLOW_CANDIDATE_VALIDATION_PROFILE_GENERATION_SAFE:
        valid = contract_valid and generation_safe_valid

    definition_identity = _as_mapping(preview.get("definition_identity"))
    diff_summary = _as_mapping(preview.get("diff_summary"))
    repair_hints = _build_candidate_repair_hints(
        contract_validation=contract_validation,
        generation_safe_validation=generation_safe_validation,
    )
    assertion_classes = ["workflow_candidate_validation"]
    if not contract_valid:
        assertion_classes.append("workflow_contract_validation_failure")
    if not generation_safe_valid:
        assertion_classes.append("workflow_generation_safety_failure")

    candidate_validation = {
        "workflow_id": _clean_text(workflow_id),
        "validation_profile": resolved_profile,
        "valid": valid,
        "contract_validation": contract_validation,
        "generation_safe_validation": generation_safe_validation,
        "definition_identity": definition_identity,
        "diff_summary": diff_summary,
        "repair_hints": repair_hints,
        "assertion_classes": assertion_classes,
        "quality_signals": {
            "contract_valid": contract_valid,
            "generation_safe_valid": generation_safe_valid,
            "repair_hint_count": len(repair_hints),
        },
    }
    result = {
        "success": True,
        "workflow_id": _clean_text(workflow_id),
        "candidate_validation": candidate_validation,
        "guardrails": {
            "preview_required": True,
            "validation_profile": resolved_profile,
            "publishes_to_vontology": False,
        },
    }
    if include_preview:
        result["preview"] = preview
    return result


def preview_workflow_authoring_spec(
    workflow_id: str,
    *,
    authoring_spec: Mapping[str, Any],
    base_definition_hash: str | None = None,
) -> dict[str, Any]:
    workflow_id_clean = _clean_text(workflow_id)
    runtime_definition, runtime_source, _registry = _load_authoring_runtime_definition(
        workflow_id_clean
    )
    _ensure_current_hash_matches(
        workflow_id=workflow_id_clean,
        runtime_definition=runtime_definition,
        runtime_source=runtime_source,
        expected_hash=base_definition_hash,
    )

    proposed_definition = build_workflow_definition_from_authoring_spec(authoring_spec)
    if _clean_text(getattr(proposed_definition, "workflow_id", "")) != workflow_id_clean:
        raise ValueError("workflow_id_mismatch")

    known_workflow_ids = get_shared_workflow_registry_read_only(
        defer_parity_work=True
    ).all_workflow_ids()
    contract_validation = validate_workflow_definition_contract(
        definition=proposed_definition,
        supported_action_ids=get_supported_durable_workflow_action_ids(),
        enforce_supported_actions=True,
        known_workflow_ids=known_workflow_ids,
        workflow_definition_loader=_workflow_definition_loader,
    )
    preview_graph = build_workflow_process_graph_from_definition(proposed_definition)
    preview_identity = build_workflow_definition_identity(
        workflow_id=workflow_id_clean,
        source="workflow_studio_preview",
        definition=proposed_definition,
        authoritative_definition=None,
    )
    current_spec = (
        serialise_workflow_definition_to_authoring_spec(runtime_definition)
        if runtime_definition is not None
        else {}
    )
    normalised_spec = serialise_workflow_definition_to_authoring_spec(
        proposed_definition
    )
    return {
        "workflow_id": workflow_id_clean,
        "preview": {
            "authoring_spec": normalised_spec,
            "graph": preview_graph,
            "definition_identity": preview_identity,
            "contract_validation": contract_validation,
            "diff_summary": _summarise_authoring_diff(
                current_spec=current_spec,
                proposed_spec=normalised_spec,
            ),
        },
        "guardrails": {
            "preview_required": True,
            "apply_requires_current_hash": True,
            "publishes_to_vontology": True,
        },
    }


def _collect_explicit_authored_concept_ids(
    authoring_spec: Mapping[str, Any],
) -> tuple[str, ...]:
    """Collect caller-selected concept IDs that publication will persist or link.

    Generated step and mapping IDs remain the publication service's concern.
    This preflight covers IDs an author can choose directly, including legacy
    metadata/input encodings accepted by the normaliser.
    """

    collected: list[str] = []

    def _append(value: Any, *, field: str, require_concept_id: bool = True) -> None:
        value_clean = _clean_text(value)
        if not value_clean:
            return
        if require_concept_id and not value_clean.startswith("#"):
            raise ValueError(f"workflow_authoring_concept_id_invalid:{field}")
        if value_clean.startswith("#") and value_clean not in collected:
            collected.append(value_clean)

    raw_steps = authoring_spec.get("steps")
    if not isinstance(raw_steps, list):
        return ()
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            continue
        field_prefix = f"steps[{index}]"
        _append(raw_step.get("concept_id"), field=f"{field_prefix}.concept_id")
        _append(
            raw_step.get("action_concept_id"),
            field=f"{field_prefix}.action_concept_id",
        )
        _append(
            raw_step.get("subworkflow_id"),
            field=f"{field_prefix}.subworkflow_id",
        )

        metadata = _as_mapping(raw_step.get("metadata"))
        _append(
            metadata.get("workflow_step_concept_id"),
            field=f"{field_prefix}.metadata.workflow_step_concept_id",
        )

        prompt_contract = _as_mapping(raw_step.get("prompt_contract"))
        _append(
            prompt_contract.get("resolved_prompt_concept_id"),
            field=f"{field_prefix}.prompt_contract.resolved_prompt_concept_id",
        )
        requested_prompt_ids = prompt_contract.get("requested_prompt_concept_ids")
        if isinstance(requested_prompt_ids, Sequence) and not isinstance(
            requested_prompt_ids,
            (str, bytes, bytearray),
        ):
            for prompt_index, prompt_id in enumerate(requested_prompt_ids):
                _append(
                    prompt_id,
                    field=(
                        f"{field_prefix}.prompt_contract."
                        f"requested_prompt_concept_ids[{prompt_index}]"
                    ),
                )

        raw_inputs = raw_step.get("inputs")
        if isinstance(raw_inputs, Mapping):
            for tool_param, raw_value in raw_inputs.items():
                if not isinstance(raw_value, Mapping):
                    continue
                _append(
                    raw_value.get("$mapping_concept_id")
                    or raw_value.get("mapping_concept_id"),
                    field=f"{field_prefix}.inputs.{tool_param}.mapping_concept_id",
                )

        context_mappings = raw_step.get("context_input_mappings")
        if isinstance(context_mappings, Sequence) and not isinstance(
            context_mappings,
            (str, bytes, bytearray),
        ):
            for mapping_index, raw_mapping in enumerate(context_mappings):
                if not isinstance(raw_mapping, Mapping):
                    continue
                _append(
                    raw_mapping.get("mapping_concept_id")
                    or raw_mapping.get("$mapping_concept_id"),
                    field=(
                        f"{field_prefix}.context_input_mappings[{mapping_index}]."
                        "mapping_concept_id"
                    ),
                )

        for mappings_field, mappings_value in (
            (
                "tool_output_context_mappings",
                raw_step.get("tool_output_context_mappings"),
            ),
            (
                "metadata.tool_output_context_mappings",
                metadata.get("tool_output_context_mappings"),
            ),
        ):
            if not isinstance(mappings_value, Sequence) or isinstance(
                mappings_value,
                (str, bytes, bytearray),
            ):
                continue
            for mapping_index, raw_mapping in enumerate(mappings_value):
                if not isinstance(raw_mapping, Mapping):
                    continue
                _append(
                    raw_mapping.get("mapping_concept_id"),
                    field=(
                        f"{field_prefix}.{mappings_field}[{mapping_index}]."
                        "mapping_concept_id"
                    ),
                )

        writes_context_keys = raw_step.get("writes_context_keys")
        if not isinstance(writes_context_keys, Sequence) or isinstance(
            writes_context_keys,
            (str, bytes, bytearray),
        ):
            writes_context_keys = metadata.get("writes_context_keys")
        if isinstance(writes_context_keys, Sequence) and not isinstance(
            writes_context_keys,
            (str, bytes, bytearray),
        ):
            for context_key in writes_context_keys:
                _append(
                    context_key,
                    field=f"{field_prefix}.writes_context_keys",
                    require_concept_id=False,
                )

    return tuple(collected)


def _preflight_explicit_authored_concept_ids(
    workflow_id: str,
    authoring_spec: Mapping[str, Any],
) -> None:
    """Fail before publication for hidden or audience-narrower authored children."""

    concept_ids = _collect_explicit_authored_concept_ids(authoring_spec)
    if not concept_ids:
        return
    workflow_doc = _actor_visible_raw_concept(workflow_id)
    for concept_id in concept_ids:
        # A provably absent child may be created by canonical publication.
        # Existing children must be visible to the ambient actor; the helper
        # raises a concealment-safe authority error when they are not.
        child_doc = _actor_visible_raw_concept(concept_id)
        if (
            workflow_doc is not None
            and child_doc is not None
            and not workflow_child_visibility_covers_parent(
                workflow_doc,
                child_doc,
            )
        ):
            raise WorkflowStudioAuthorityError(
                "workflow_child_visibility_narrower_than_parent"
            )


def apply_workflow_authoring_spec(
    workflow_id: str,
    *,
    authoring_spec: Mapping[str, Any],
    base_definition_hash: str | None = None,
) -> dict[str, Any]:
    require_workflow_studio_mutation_actor()
    preview = preview_workflow_authoring_spec(
        workflow_id,
        authoring_spec=authoring_spec,
        base_definition_hash=base_definition_hash,
    )
    contract_validation = _as_mapping(
        _as_mapping(preview.get("preview")).get("contract_validation")
    )
    if not bool(contract_validation.get("valid")):
        raise ValueError("workflow_authoring_preview_invalid")

    definition = build_workflow_definition_from_authoring_spec(authoring_spec)
    purpose = _clean_text(
        authoring_spec.get("description") or authoring_spec.get("workflow_description")
    )
    runtime_definition, _runtime_source, _registry = _load_authoring_runtime_definition(
        _clean_text(workflow_id)
    )
    create_missing_workflow = bool(
        runtime_definition is None and _runtime_source == "new_workflow"
    )
    _preflight_explicit_authored_concept_ids(workflow_id, authoring_spec)
    publication = publish_workflow_definition_from_definition(
        definition=definition,
        create_missing=create_missing_workflow,
        create_missing_child_concepts=True,
        purpose=purpose or None,
    )
    published_workflow_ids = publication.get("published_workflow_ids")
    if not (
        isinstance(published_workflow_ids, list)
        and _clean_text(workflow_id) in published_workflow_ids
    ):
        return {
            "workflow_id": _clean_text(workflow_id),
            "publication": publication,
            "preview": _as_mapping(preview.get("preview")),
            "metadata_sync": None,
        }
    metadata_sync = _apply_workflow_policy_metadata(
        workflow_id=_clean_text(workflow_id),
        workflow_metadata=_as_mapping(getattr(definition, "metadata", None)),
        policy_keys=("launch_input_contract", "required_effects_contract"),
    )
    return {
        "workflow_id": _clean_text(workflow_id),
        "publication": publication,
        "preview": _as_mapping(preview.get("preview")),
        "metadata_sync": metadata_sync,
    }


def submit_workflow_authoring_proposal(
    workflow_id: str,
    *,
    authoring_spec: Mapping[str, Any],
    base_definition_hash: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    proposed_by: str | None = None,
    proposal_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    require_workflow_studio_mutation_actor()
    workflow_id_clean = _clean_text(workflow_id)
    preview_payload = preview_workflow_authoring_spec(
        workflow_id_clean,
        authoring_spec=authoring_spec,
        base_definition_hash=base_definition_hash,
    )
    preview = _as_mapping(preview_payload.get("preview"))
    validation_payload = validate_workflow_candidate(
        workflow_id_clean,
        authoring_spec=authoring_spec,
        base_definition_hash=base_definition_hash,
        include_preview=False,
    )
    candidate_validation = _as_mapping(validation_payload.get("candidate_validation"))
    runtime_definition, runtime_source, _registry = _load_authoring_runtime_definition(
        workflow_id_clean
    )
    current_spec = (
        serialise_workflow_definition_to_authoring_spec(runtime_definition)
        if runtime_definition is not None
        else None
    )
    existing_proposal = _load_workflow_authoring_proposal(workflow_id_clean)
    existing_proposal_id = _clean_text((existing_proposal or {}).get("proposal_id"))
    proposal_id = str(uuid.uuid4())
    if (
        existing_proposal_id
        and _clean_text((existing_proposal or {}).get("status"))
        == _WORKFLOW_AUTHORING_PROPOSAL_STATUS_PENDING_REVIEW
    ):
        superseded_payload = _as_mapping(existing_proposal)
        superseded_payload["status"] = _WORKFLOW_AUTHORING_PROPOSAL_STATUS_SUPERSEDED
        superseded_payload["updated_at_utc"] = _utc_now_iso()
        _store_workflow_authoring_proposal(
            workflow_id_clean,
            superseded_payload,
            update_active_pointer=False,
        )
    proposal_payload = {
        "schema_version": _WORKFLOW_AUTHORING_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "workflow_id": workflow_id_clean,
        "status": _WORKFLOW_AUTHORING_PROPOSAL_STATUS_PENDING_REVIEW,
        "created_at_utc": (
            (existing_proposal or {}).get("created_at_utc") or _utc_now_iso()
        ),
        "updated_at_utc": _utc_now_iso(),
        "created_by": _clean_text(proposed_by) or None,
        "base_definition_hash": _clean_text(base_definition_hash) or None,
        "runtime_source": runtime_source,
        "authoring_spec": _json_roundtrip(
            serialise_workflow_definition_to_authoring_spec(
                build_workflow_definition_from_authoring_spec(authoring_spec)
            )
        ),
        "candidate_validation": _json_roundtrip(candidate_validation),
        "preview_summary": {
            "definition_identity": _json_roundtrip(
                _as_mapping(preview.get("definition_identity"))
            ),
            "diff_summary": _json_roundtrip(_as_mapping(preview.get("diff_summary"))),
            "contract_validation": _json_roundtrip(
                _as_mapping(preview.get("contract_validation"))
            ),
        },
        "proposal_source_session_id": _clean_text(session_id) or None,
        "proposal_source_turn_id": _clean_text(turn_id) or None,
        "prompt_contract": build_workflow_authoring_prompt_contract(),
        "prompt_health": get_workflow_authoring_prompt_health_status(),
    }
    if isinstance(proposal_context, Mapping) and proposal_context:
        proposal_payload["proposal_context"] = _json_roundtrip(proposal_context)
    if isinstance(existing_proposal, Mapping) and isinstance(
        existing_proposal.get("previous_authoring_spec"),
        Mapping,
    ):
        proposal_payload["previous_authoring_spec"] = _json_roundtrip(
            existing_proposal.get("previous_authoring_spec")
        )
    elif current_spec is not None:
        proposal_payload["previous_authoring_spec"] = _json_roundtrip(current_spec)

    stored_proposal = _store_workflow_authoring_proposal(
        workflow_id_clean,
        proposal_payload,
        update_active_pointer=True,
    )
    current_lifecycle, _current_lifecycle_source = resolve_workflow_publication_lifecycle(
        workflow_id_clean
    )
    lifecycle = _as_mapping(current_lifecycle)
    upsert_workflow_publication_lifecycle(
        workflow_id=workflow_id_clean,
        phase=_clean_text(lifecycle.get("phase")) or "published",
        published=bool(lifecycle.get("published", True)),
        validation_passed=lifecycle.get("validation_passed")
        if isinstance(lifecycle.get("validation_passed"), bool)
        else None,
        postconditions_verified=lifecycle.get("postconditions_verified")
        if isinstance(lifecycle.get("postconditions_verified"), bool)
        else None,
        optional_test_instance_id=_clean_text(lifecycle.get("optional_test_instance_id"))
        or None,
        last_error=None,
        review_state=_WORKFLOW_AUTHORING_PROPOSAL_STATUS_PENDING_REVIEW,
        review_reason=None,
        proposal_id=_clean_text(stored_proposal.get("proposal_id")) or None,
        proposal_source_session_id=_clean_text(session_id) or None,
        proposal_source_turn_id=_clean_text(turn_id) or None,
        approval_required=True,
        routing_eligible=bool(lifecycle.get("routing_eligible", lifecycle.get("published", True))),
        rollout_state=_clean_text(lifecycle.get("rollout_state")) or "proposal_pending_review",
        event_binding_ids=_clean_string_list(lifecycle.get("event_binding_ids")),
        schedule_ids=_clean_string_list(lifecycle.get("schedule_ids")),
    )
    return {
        "success": True,
        "workflow_id": workflow_id_clean,
        "proposal": _proposal_summary(stored_proposal),
        "candidate_validation": candidate_validation,
        "guardrails": {
            "preview_required": True,
            "approval_required": True,
            "publishes_to_vontology": False,
        },
        "next_step": "approval",
    }


def record_workflow_authoring_promotion_evaluation(
    workflow_id: str,
    *,
    promotion_evaluation: Mapping[str, Any],
    proposal_id: str | None = None,
) -> dict[str, Any]:
    require_workflow_studio_mutation_actor()
    workflow_id_clean = _clean_text(workflow_id)
    if not workflow_id_clean:
        raise ValueError("workflow_id_required")

    expected_proposal_id = _clean_text(proposal_id)
    proposal = (
        _load_workflow_authoring_proposal_by_id(workflow_id_clean, expected_proposal_id)
        if expected_proposal_id
        else _load_workflow_authoring_proposal(workflow_id_clean)
    )
    if not isinstance(proposal, Mapping):
        raise ValueError("workflow_authoring_proposal_missing")
    proposal_payload = _as_mapping(proposal)

    stored_proposal_id = _clean_text(proposal_payload.get("proposal_id"))
    if expected_proposal_id and stored_proposal_id and expected_proposal_id != stored_proposal_id:
        raise ValueError("workflow_authoring_proposal_id_mismatch")

    evaluation = _json_roundtrip(_as_mapping(promotion_evaluation))
    if not evaluation:
        raise ValueError("promotion_evaluation_required")

    proposal_payload["promotion_evaluation"] = evaluation
    proposal_payload["updated_at_utc"] = _utc_now_iso()
    stored_proposal = _store_workflow_authoring_proposal(
        workflow_id_clean,
        proposal_payload,
        update_active_pointer=None,
    )

    current_lifecycle, _current_lifecycle_source = resolve_workflow_publication_lifecycle(
        workflow_id_clean
    )
    lifecycle = _as_mapping(current_lifecycle)
    routing_profile = resolve_workflow_routing_profile(workflow_id_clean)[0]
    upsert_workflow_publication_lifecycle(
        workflow_id=workflow_id_clean,
        phase=_clean_text(lifecycle.get("phase")) or "published",
        published=bool(lifecycle.get("published", True)),
        validation_passed=lifecycle.get("validation_passed")
        if isinstance(lifecycle.get("validation_passed"), bool)
        else None,
        postconditions_verified=lifecycle.get("postconditions_verified")
        if isinstance(lifecycle.get("postconditions_verified"), bool)
        else None,
        optional_test_instance_id=_clean_text(lifecycle.get("optional_test_instance_id"))
        or None,
        last_error=_clean_text(lifecycle.get("last_error")) or None,
        review_state=_clean_text(lifecycle.get("review_state")) or None,
        review_reason=_clean_text(lifecycle.get("review_reason")) or None,
        reviewed_at=_clean_text(lifecycle.get("reviewed_at")) or None,
        reviewed_by=_clean_text(lifecycle.get("reviewed_by")) or None,
        proposal_id=_clean_text(stored_proposal.get("proposal_id"))
        or stored_proposal_id
        or expected_proposal_id,
        proposal_source_session_id=_clean_text(
            stored_proposal.get("proposal_source_session_id")
        )
        or None,
        proposal_source_turn_id=_clean_text(
            stored_proposal.get("proposal_source_turn_id")
        )
        or None,
        experiment_run_id=_clean_text(evaluation.get("experiment_run_id")) or None,
        supersedes_workflow_id=_clean_text(lifecycle.get("supersedes_workflow_id")) or None,
        superseded_by_workflow_id=_clean_text(lifecycle.get("superseded_by_workflow_id"))
        or None,
        routing_eligible=_metadata_routing_eligible(
            publication_lifecycle=lifecycle,
            routing_profile=_as_mapping(routing_profile),
        ),
        rollout_state=_clean_text(lifecycle.get("rollout_state")) or None,
        approval_required=lifecycle.get("approval_required")
        if isinstance(lifecycle.get("approval_required"), bool)
        else True,
        promotion_decision=_clean_text(evaluation.get("promotion_recommendation")) or None,
        event_binding_ids=_clean_string_list(lifecycle.get("event_binding_ids")),
        schedule_ids=_clean_string_list(lifecycle.get("schedule_ids")),
    )
    return {
        "success": True,
        "workflow_id": workflow_id_clean,
        "proposal": _proposal_summary(stored_proposal),
        "promotion_evaluation": evaluation,
    }


def review_workflow_authoring_proposal(
    workflow_id: str,
    *,
    action: str,
    review_reason: str | None = None,
    reviewed_by: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    require_workflow_studio_mutation_actor()
    workflow_id_clean = _clean_text(workflow_id)
    review_action = _clean_text(action).lower()
    if review_action not in {"approve", "reject"}:
        raise ValueError("review_action_invalid")
    proposal = _load_workflow_authoring_proposal(workflow_id_clean)
    if not isinstance(proposal, Mapping):
        raise ValueError("workflow_authoring_proposal_missing")
    proposal_payload = _as_mapping(proposal)
    authoring_spec_raw = proposal_payload.get("authoring_spec")
    authoring_spec = (
        dict(authoring_spec_raw)
        if isinstance(authoring_spec_raw, Mapping)
        else None
    )
    if review_action == "approve" and authoring_spec is None:
        raise ValueError("workflow_authoring_proposal_authoring_spec_missing")

    current_lifecycle, _current_lifecycle_source = resolve_workflow_publication_lifecycle(
        workflow_id_clean
    )
    lifecycle = _as_mapping(current_lifecycle)

    if review_action == "reject":
        proposal_payload["status"] = _WORKFLOW_AUTHORING_PROPOSAL_STATUS_REJECTED
        proposal_payload["reviewed_at_utc"] = _utc_now_iso()
        proposal_payload["reviewed_by"] = _clean_text(reviewed_by) or None
        proposal_payload["review_reason"] = _clean_text(review_reason) or None
        proposal_payload["updated_at_utc"] = _utc_now_iso()
        stored_proposal = _store_workflow_authoring_proposal(
            workflow_id_clean,
            proposal_payload,
        )
        upsert_workflow_publication_lifecycle(
            workflow_id=workflow_id_clean,
            phase=_clean_text(lifecycle.get("phase")) or "published",
            published=bool(lifecycle.get("published", True)),
            validation_passed=lifecycle.get("validation_passed")
            if isinstance(lifecycle.get("validation_passed"), bool)
            else None,
            postconditions_verified=lifecycle.get("postconditions_verified")
            if isinstance(lifecycle.get("postconditions_verified"), bool)
            else None,
            optional_test_instance_id=_clean_text(
                lifecycle.get("optional_test_instance_id")
            )
            or None,
            last_error=None,
            review_state=_WORKFLOW_AUTHORING_PROPOSAL_STATUS_REJECTED,
            review_reason=_clean_text(review_reason) or None,
            reviewed_at=_clean_text(stored_proposal.get("reviewed_at_utc")) or None,
            reviewed_by=_clean_text(reviewed_by) or None,
            proposal_id=_clean_text(stored_proposal.get("proposal_id")) or None,
            approval_required=False,
            routing_eligible=bool(
                lifecycle.get("routing_eligible", lifecycle.get("published", True))
            ),
            rollout_state=_clean_text(lifecycle.get("rollout_state")) or "published",
            event_binding_ids=_clean_string_list(lifecycle.get("event_binding_ids")),
            schedule_ids=_clean_string_list(lifecycle.get("schedule_ids")),
        )
        return {
            "success": True,
            "workflow_id": workflow_id_clean,
            "proposal": _proposal_summary(stored_proposal),
            "review_action": review_action,
        }

    runtime_definition, runtime_source, _registry = _load_authoring_runtime_definition(
        workflow_id_clean
    )
    current_spec = (
        serialise_workflow_definition_to_authoring_spec(runtime_definition)
        if runtime_definition is not None
        else None
    )
    if authoring_spec is None:
        raise ValueError("workflow_authoring_proposal_authoring_spec_missing")
    apply_result = apply_workflow_authoring_spec(
        workflow_id_clean,
        authoring_spec=authoring_spec,
        base_definition_hash=_clean_text(proposal_payload.get("base_definition_hash"))
        or None,
    )
    definition = build_workflow_definition_from_authoring_spec(authoring_spec)
    metadata_sync = _as_mapping(apply_result.get("metadata_sync"))
    additional_metadata_sync = _apply_workflow_policy_metadata(
        workflow_id=workflow_id_clean,
        workflow_metadata=_as_mapping(getattr(definition, "metadata", None)),
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        policy_keys=(
            "routing_profile",
            "discovery_exemplars",
            "background_launch_policy",
            "event_bindings",
            "schedule_specs",
        ),
    )
    metadata_sync.update(
        {
            key: additional_metadata_sync.get(key)
            for key in (
                "routing_profile",
                "discovery_exemplars",
                "background_launch_policy",
                "event_binding_ids",
                "schedule_ids",
                "disabled_binding_ids",
                "disabled_schedule_ids",
            )
        }
    )
    proposal_payload["status"] = _WORKFLOW_AUTHORING_PROPOSAL_STATUS_APPROVED
    proposal_payload["reviewed_at_utc"] = _utc_now_iso()
    proposal_payload["reviewed_by"] = _clean_text(reviewed_by) or None
    proposal_payload["review_reason"] = _clean_text(review_reason) or None
    proposal_payload["updated_at_utc"] = _utc_now_iso()
    if current_spec is not None:
        proposal_payload["previous_authoring_spec"] = _json_roundtrip(current_spec)
    stored_proposal = _store_workflow_authoring_proposal(
        workflow_id_clean,
        proposal_payload,
    )
    routing_profile = metadata_sync.get("routing_profile")
    upsert_workflow_publication_lifecycle(
        workflow_id=workflow_id_clean,
        phase="published",
        published=True,
        validation_passed=True,
        postconditions_verified=True,
        optional_test_instance_id=_clean_text(
            lifecycle.get("optional_test_instance_id")
        )
        or None,
        last_error=None,
        review_state=_WORKFLOW_AUTHORING_PROPOSAL_STATUS_APPROVED,
        review_reason=_clean_text(review_reason) or None,
        reviewed_at=_clean_text(stored_proposal.get("reviewed_at_utc")) or None,
        reviewed_by=_clean_text(reviewed_by) or None,
        proposal_id=_clean_text(stored_proposal.get("proposal_id")) or None,
        proposal_source_session_id=_clean_text(
            stored_proposal.get("proposal_source_session_id")
        )
        or None,
        proposal_source_turn_id=_clean_text(
            stored_proposal.get("proposal_source_turn_id")
        )
        or None,
        approval_required=False,
        routing_eligible=_metadata_routing_eligible(
            publication_lifecycle=lifecycle,
            routing_profile=_as_mapping(routing_profile),
        ),
        rollout_state="published",
        event_binding_ids=_clean_string_list(metadata_sync.get("event_binding_ids")),
        schedule_ids=_clean_string_list(metadata_sync.get("schedule_ids")),
    )
    return {
        "success": True,
        "workflow_id": workflow_id_clean,
        "review_action": review_action,
        "publication": apply_result.get("publication"),
        "preview": apply_result.get("preview"),
        "proposal": _proposal_summary(stored_proposal),
        "metadata_sync": metadata_sync,
        "runtime_source_before_review": runtime_source,
    }


def rollback_workflow_authoring_promotion(
    workflow_id: str,
    *,
    review_reason: str | None = None,
    reviewed_by: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    require_workflow_studio_mutation_actor()
    workflow_id_clean = _clean_text(workflow_id)
    proposal = _load_workflow_authoring_proposal(workflow_id_clean)
    proposal_payload = _as_mapping(proposal)
    previous_authoring_spec = proposal_payload.get("previous_authoring_spec")
    if not isinstance(previous_authoring_spec, Mapping):
        raise ValueError("workflow_authoring_previous_spec_missing")
    runtime_definition, runtime_source, _registry = _load_authoring_runtime_definition(
        workflow_id_clean
    )
    current_identity = (
        build_workflow_definition_identity(
            workflow_id=workflow_id_clean,
            source=runtime_source or "unknown",
            definition=runtime_definition,
            authoritative_definition=(
                runtime_definition if runtime_source.lower() == "vontology" else None
            ),
        )
        if runtime_definition is not None
        else {}
    )
    apply_result = apply_workflow_authoring_spec(
        workflow_id_clean,
        authoring_spec=previous_authoring_spec,
        base_definition_hash=_clean_text(current_identity.get("definition_hash")) or None,
    )
    definition = build_workflow_definition_from_authoring_spec(
        previous_authoring_spec
    )
    metadata_sync = _as_mapping(apply_result.get("metadata_sync"))
    additional_metadata_sync = _apply_workflow_policy_metadata(
        workflow_id=workflow_id_clean,
        workflow_metadata=_as_mapping(getattr(definition, "metadata", None)),
        user_id=user_id,
        org_id=org_id,
        namespace=namespace,
        policy_keys=(
            "routing_profile",
            "discovery_exemplars",
            "background_launch_policy",
            "event_bindings",
            "schedule_specs",
        ),
    )
    metadata_sync.update(
        {
            key: additional_metadata_sync.get(key)
            for key in (
                "routing_profile",
                "discovery_exemplars",
                "background_launch_policy",
                "event_binding_ids",
                "schedule_ids",
                "disabled_binding_ids",
                "disabled_schedule_ids",
            )
        }
    )
    proposal_payload["status"] = _WORKFLOW_AUTHORING_PROPOSAL_STATUS_ROLLED_BACK
    proposal_payload["reviewed_at_utc"] = _utc_now_iso()
    proposal_payload["reviewed_by"] = _clean_text(reviewed_by) or None
    proposal_payload["review_reason"] = _clean_text(review_reason) or None
    proposal_payload["updated_at_utc"] = _utc_now_iso()
    stored_proposal = _store_workflow_authoring_proposal(
        workflow_id_clean,
        proposal_payload,
    )
    upsert_workflow_publication_lifecycle(
        workflow_id=workflow_id_clean,
        phase="published",
        published=True,
        validation_passed=True,
        postconditions_verified=True,
        last_error=None,
        review_state=_WORKFLOW_AUTHORING_PROPOSAL_STATUS_APPROVED,
        review_reason=_clean_text(review_reason) or "rollback_applied",
        reviewed_at=_clean_text(stored_proposal.get("reviewed_at_utc")) or None,
        reviewed_by=_clean_text(reviewed_by) or None,
        proposal_id=_clean_text(stored_proposal.get("proposal_id")) or None,
        approval_required=False,
        routing_eligible=_metadata_routing_eligible(
            publication_lifecycle={},
            routing_profile=_as_mapping(metadata_sync.get("routing_profile")),
        ),
        rollout_state="rolled_back",
        event_binding_ids=_clean_string_list(metadata_sync.get("event_binding_ids")),
        schedule_ids=_clean_string_list(metadata_sync.get("schedule_ids")),
    )
    return {
        "success": True,
        "workflow_id": workflow_id_clean,
        "publication": apply_result.get("publication"),
        "proposal": _proposal_summary(stored_proposal),
        "metadata_sync": metadata_sync,
    }


def demote_workflow_routing(
    workflow_id: str,
    *,
    review_reason: str | None = None,
    reviewed_by: str | None = None,
) -> dict[str, Any]:
    require_workflow_studio_mutation_actor()
    workflow_id_clean = _clean_text(workflow_id)
    _load_actor_scoped_runtime_definition(workflow_id_clean)
    lifecycle, _source = resolve_workflow_publication_lifecycle(workflow_id_clean)
    current = _as_mapping(lifecycle)
    runtime_enablement = _set_workflow_runtime_enablement(workflow_id_clean, enabled=False)
    stored = upsert_workflow_publication_lifecycle(
        workflow_id=workflow_id_clean,
        phase="demoted",
        published=False,
        validation_passed=current.get("validation_passed")
        if isinstance(current.get("validation_passed"), bool)
        else None,
        postconditions_verified=current.get("postconditions_verified")
        if isinstance(current.get("postconditions_verified"), bool)
        else None,
        optional_test_instance_id=_clean_text(current.get("optional_test_instance_id"))
        or None,
        last_error=None,
        review_state="demoted",
        review_reason=_clean_text(review_reason) or None,
        reviewed_at=_utc_now_iso(),
        reviewed_by=_clean_text(reviewed_by) or None,
        proposal_id=_clean_text(current.get("proposal_id")) or None,
        approval_required=False,
        routing_eligible=False,
        rollout_state="demoted",
        event_binding_ids=[],
        schedule_ids=[],
    )
    return {
        "success": True,
        "workflow_id": workflow_id_clean,
        "publication_lifecycle": stored,
        "runtime_enablement": runtime_enablement,
    }


def supersede_workflow_publication(
    workflow_id: str,
    *,
    replacement_workflow_id: str,
    review_reason: str | None = None,
    reviewed_by: str | None = None,
) -> dict[str, Any]:
    require_workflow_studio_mutation_actor()
    workflow_id_clean = _clean_text(workflow_id)
    replacement_workflow_id_clean = _clean_text(replacement_workflow_id)
    if not replacement_workflow_id_clean:
        raise ValueError("replacement_workflow_id_required")
    # Resolve both complete graphs before the first mutation. A visible source
    # must not be disabled merely because its replacement is hidden or partial.
    _load_actor_scoped_runtime_definition(workflow_id_clean)
    _load_actor_scoped_runtime_definition(replacement_workflow_id_clean)
    current_runtime_enablement = _set_workflow_runtime_enablement(
        workflow_id_clean,
        enabled=False,
    )
    current_lifecycle = upsert_workflow_publication_lifecycle(
        workflow_id=workflow_id_clean,
        phase="superseded",
        published=False,
        last_error=None,
        review_state="superseded",
        review_reason=_clean_text(review_reason) or None,
        reviewed_at=_utc_now_iso(),
        reviewed_by=_clean_text(reviewed_by) or None,
        superseded_by_workflow_id=replacement_workflow_id_clean,
        routing_eligible=False,
        rollout_state="superseded",
        event_binding_ids=[],
        schedule_ids=[],
    )
    replacement_existing, _source = resolve_workflow_publication_lifecycle(
        replacement_workflow_id_clean
    )
    replacement_lifecycle = _as_mapping(replacement_existing)
    replacement_stored = upsert_workflow_publication_lifecycle(
        workflow_id=replacement_workflow_id_clean,
        phase="published",
        published=True,
        validation_passed=replacement_lifecycle.get("validation_passed")
        if isinstance(replacement_lifecycle.get("validation_passed"), bool)
        else None,
        postconditions_verified=replacement_lifecycle.get("postconditions_verified")
        if isinstance(replacement_lifecycle.get("postconditions_verified"), bool)
        else None,
        optional_test_instance_id=_clean_text(
            replacement_lifecycle.get("optional_test_instance_id")
        )
        or None,
        last_error=None,
        review_state=_WORKFLOW_AUTHORING_PROPOSAL_STATUS_APPROVED,
        review_reason=_clean_text(review_reason) or None,
        reviewed_at=_utc_now_iso(),
        reviewed_by=_clean_text(reviewed_by) or None,
        supersedes_workflow_id=workflow_id_clean,
        routing_eligible=True,
        rollout_state="published",
        event_binding_ids=_clean_string_list(replacement_lifecycle.get("event_binding_ids")),
        schedule_ids=_clean_string_list(replacement_lifecycle.get("schedule_ids")),
    )
    return {
        "success": True,
        "workflow_id": workflow_id_clean,
        "replacement_workflow_id": replacement_workflow_id_clean,
        "superseded_lifecycle": current_lifecycle,
        "replacement_lifecycle": replacement_stored,
        "runtime_enablement": current_runtime_enablement,
    }


def build_workflow_description_proposal(
    workflow_id: str,
    *,
    mode: str = "auto",
    user_concept_id: str | None = None,
    org_concept_id: str | None = None,
) -> dict[str, Any]:
    workflow_id_clean = _clean_text(workflow_id)
    detail = build_workflow_studio_detail_payload(workflow_id_clean)
    summary = _as_mapping(detail.get("summary"))
    current_description = _clean_text(summary.get("description"))
    prompt_context = build_workflow_description_prompt_context(workflow_id_clean)
    deterministic = build_deterministic_workflow_description(
        concept_id=workflow_id_clean,
        concept_name=_clean_text(summary.get("workflow_id")) or workflow_id_clean,
        existing_description=current_description or None,
        workflow_context=prompt_context,
    )

    requested_mode = _clean_text(mode).lower() or "auto"
    prompt_concept_id = resolve_workflow_description_prompt_concept_id(
        workflow_id=workflow_id_clean
    ) or WORKFLOW_DESCRIPTION_PROMPT_CONCEPT_ID
    proposal_text = deterministic or current_description
    proposal_source = "deterministic"
    llm_error: str | None = None

    if requested_mode in {"auto", "ai", "llm"}:
        try:
            prompt_template = AnnotationPromptBuilder(prompt_concept_id).get_instruction()
            prompt_lines = [
                prompt_template,
                "",
                f"Workflow ID: {workflow_id_clean}",
                f"Current description: {current_description or 'None'}",
            ]
            for key, value in prompt_context.items():
                prompt_lines.append(f"{key}: {value}")
            prompt_lines.append("")
            prompt_lines.append(
                "Return only the revised workflow description in New Zealand English."
            )
            selected_model = get_active_model_name(
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
            )
            raw_response = get_llm_client(
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
            ).generate(
                prompt="\n".join(prompt_lines),
                model=selected_model,
            )
            llm_text = (
                raw_response.strip()
                if isinstance(raw_response, str)
                else str(raw_response).strip()
            )
            if llm_text.lower().startswith("description:"):
                llm_text = llm_text.split(":", 1)[1].strip()
            if len(llm_text) >= _WORKFLOW_STUDIO_AI_PROPOSAL_MIN_LENGTH:
                proposal_text = llm_text
                proposal_source = "llm"
        except Exception as exc:
            llm_error = str(exc)
            logger.warning(
                "workflow studio description proposal fell back to deterministic path for %s: %s",
                workflow_id_clean,
                exc,
            )

    return {
        "workflow_id": workflow_id_clean,
        "current_description": current_description or None,
        "proposal": {
            "text": proposal_text or None,
            "source": proposal_source,
            "prompt_concept_id": prompt_concept_id,
            "llm_error": llm_error,
            "mode_requested": requested_mode,
            "context": prompt_context,
        },
        "guardrails": {
            "proposal_only": True,
            "apply_path": "authoring_spec",
            "publish_target": "vontology",
        },
    }


__all__ = [
    "WorkflowStudioAuthorityError",
    "WorkflowStudioConflictError",
    "apply_workflow_authoring_spec",
    "build_workflow_catalogue_payload",
    "build_workflow_description_proposal",
    "build_workflow_studio_detail_payload",
    "demote_workflow_routing",
    "get_workflow_authoring_proposal",
    "get_workflow_authoring_proposal_by_id",
    "preview_workflow_authoring_spec",
    "require_workflow_studio_mutation_actor",
    "record_workflow_authoring_promotion_evaluation",
    "review_workflow_authoring_proposal",
    "rollback_workflow_authoring_promotion",
    "submit_workflow_authoring_proposal",
    "supersede_workflow_publication",
    "validate_workflow_candidate",
]
