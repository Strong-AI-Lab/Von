from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..db.transient_errors import is_transient_mongo_error
from ..languagemodels.llm_interface import get_active_model_name, get_llm_client
from ..prompt.annotation_prompt import AnnotationPromptBuilder
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
from ..services.workflow_event_integration_service import (
    build_event_workflow_binding_diagnostics,
)
from .durable.registry_factory import (
    build_durable_workflow_registry_read_only,
    get_or_build_workflow_registry_inventory_snapshot,
    get_shared_durable_action_registry,
)
from .durable.models import WorkflowInstanceStatus
from .durable.startup import get_instance_manager
from .trace_store import list_recent_workflow_execution_traces
from .vontology_loader import (
    build_workflow_process_graph,
    build_workflow_process_graph_from_definition,
    load_workflow_definition_from_vontology,
    resolve_workflow_narrative_text,
)
from .workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
    serialise_workflow_definition_to_authoring_spec,
)
from .workflow_concept_authority_service import (
    publish_workflow_definition_from_definition,
)
from .workflow_definition_identity_service import (
    build_workflow_definition_identity,
    build_workflow_definition_identity_from_graph,
    validate_workflow_definition_contract,
)
from .workflow_listing_service import build_workflow_listing_entry

logger = logging.getLogger(__name__)

_TRANSIENT_INSTANCE_ERROR_MARKERS = ("temporarily unavailable",)
_WORKFLOW_STUDIO_AI_PROPOSAL_MIN_LENGTH = 20
_WORKFLOW_CANDIDATE_VALIDATION_PROFILE_CONTRACT_ONLY = "contract_only"
_WORKFLOW_CANDIDATE_VALIDATION_PROFILE_GENERATION_SAFE = "generation_safe"


class WorkflowStudioConflictError(RuntimeError):
    """Raised when preview/apply targets a stale workflow definition."""


def _clean_text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list_of_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


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
        registry = build_durable_workflow_registry_read_only(defer_parity_work=True)
        definition = registry.get(workflow_id_clean)
        if definition is not None:
            return definition
    except Exception:
        logger.debug(
            "workflow studio could not load registry definition for %s",
            workflow_id_clean,
            exc_info=True,
        )
    try:
        return load_workflow_definition_from_vontology(workflow_id_clean)
    except Exception:
        logger.debug(
            "workflow studio could not load Vontology definition for %s",
            workflow_id_clean,
            exc_info=True,
        )
        return None


def _load_runtime_definition(
    workflow_id: str,
) -> tuple[Any | None, str, Any]:
    workflow_id_clean = _clean_text(workflow_id)
    registry = build_durable_workflow_registry_read_only(defer_parity_work=True)
    definition = None
    source = "unknown"
    try:
        definition = registry.get(workflow_id_clean)
        source = (
            registry.get_registration_source(workflow_id_clean, resolve_lazy=True)
            or "unknown"
        )
    except Exception:
        logger.debug(
            "workflow studio registry.get failed for %s",
            workflow_id_clean,
            exc_info=True,
        )
    if definition is None:
        try:
            definition = load_workflow_definition_from_vontology(workflow_id_clean)
            if definition is not None:
                source = "vontology"
        except Exception:
            logger.debug(
                "workflow studio load_workflow_definition_from_vontology failed for %s",
                workflow_id_clean,
                exc_info=True,
            )
    return definition, source, registry


def build_workflow_catalogue_payload(
    *,
    limit: int,
    namespace: str | None,
    session_id: str | None,
    turn_id: str | None,
    include_designs: bool = True,
) -> dict[str, Any]:
    registry = build_durable_workflow_registry_read_only(defer_parity_work=True)
    inventory_snapshot = get_or_build_workflow_registry_inventory_snapshot(
        registry=registry,
        allow_sync_build=False,
    )
    if not isinstance(inventory_snapshot, dict):
        inventory_snapshot = {}

    workflow_ids = sorted(list(registry.all_workflow_ids()))
    selected_ids = workflow_ids[:limit]
    usage_aggregate_map = get_workflow_usage_aggregates_for_workflows(selected_ids)
    episode_count_map = get_workflow_episode_counts_for_workflows(
        selected_ids,
        namespace=namespace or None,
        session_id=session_id or None,
        turn_id=turn_id or None,
    )

    items: list[dict[str, Any]] = []
    for workflow_id in selected_ids:
        listing_entry = build_workflow_listing_entry(
            registry=registry,
            workflow_id=workflow_id,
        )
        usage = (
            usage_aggregate_map.get(workflow_id, {})
            if isinstance(usage_aggregate_map, dict)
            else {}
        )
        attempts = usage.get("attempts")
        completions = usage.get("completions")
        completion_rate = usage.get("completion_rate")

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
            "completion_rate": (
                float(completion_rate)
                if isinstance(completion_rate, (int, float))
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

    return {
        "items": items,
        "count": len(items),
        "total": len(workflow_ids),
        "episodes_scope": {
            "namespace": namespace or None,
            "session_id": session_id or None,
            "turn_id": turn_id or None,
        },
        "parity_inventory": inventory_snapshot,
    }


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

    schedules = [
        schedule.to_status_dict()
        for schedule in manager.list_schedules(limit=200)
        if _clean_text(getattr(schedule, "workflow_id", "")) == workflow_id
    ]

    all_bindings = [
        binding.to_status_dict()
        for binding in manager.list_event_bindings(limit=500)
    ]
    workflow_bindings = [
        binding
        for binding in all_bindings
        if _clean_text(binding.get("workflow_id")) == workflow_id
    ]
    binding_diagnostics = [
        row
        for row in build_event_workflow_binding_diagnostics(all_bindings)
        if workflow_id in (row.get("workflow_ids") or [])
    ]

    instances_payload: dict[str, Any]
    try:
        instance_items = manager.list_instance_status_dicts(
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
        workflow_id=workflow_id,
    )
    episode_items = list_workflow_use_episodes(workflow_id=workflow_id, limit=12)

    return {
        "schedules": {
            "items": schedules,
            "count": len(schedules),
        },
        "bindings": {
            "items": workflow_bindings,
            "count": len(workflow_bindings),
            "diagnostics": binding_diagnostics,
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


def _build_authoring_payload(
    *,
    workflow_id: str,
    definition: Any | None,
    source: str,
) -> dict[str, Any]:
    if definition is None:
        return {
            "available": False,
            "reason": "workflow_definition_not_loaded",
        }

    authoring_spec = serialise_workflow_definition_to_authoring_spec(definition)
    definition_identity = build_workflow_definition_identity(
        workflow_id=workflow_id,
        source=source,
        definition=definition,
        authoritative_definition=definition if source.lower() == "vontology" else None,
    )
    action_registry = get_shared_durable_action_registry()
    validation = validate_workflow_definition_contract(
        definition=definition,
        supported_action_ids=action_registry.all_action_ids(),
        enforce_supported_actions=True,
        known_workflow_ids=build_durable_workflow_registry_read_only(
            defer_parity_work=True
        ).all_workflow_ids(),
        workflow_definition_loader=_workflow_definition_loader,
    )
    return {
        "available": True,
        "current_spec": authoring_spec,
        "base_definition_hash": definition_identity.get("definition_hash"),
        "validation": validation,
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

    definition_graph, warnings = build_workflow_process_graph(workflow_id_clean)
    raw, raw_source = resolve_workflow_narrative_text(workflow_id_clean)
    runtime_definition, source, registry = _load_runtime_definition(workflow_id_clean)

    listing_entry = build_workflow_listing_entry(
        registry=registry,
        workflow_id=workflow_id_clean,
    )
    usage_aggregate_map = get_workflow_usage_aggregates_for_workflows([workflow_id_clean])
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
        namespace=namespace or None,
        session_id=session_id or None,
        turn_id=turn_id or None,
    )

    completion_rate = usage.get("completion_rate")

    return {
        "workflow_id": workflow_id_clean,
        "summary": {
            **listing_entry,
            "attempts": int(usage.get("attempts") or 0),
            "completions": int(usage.get("completions") or 0),
            "completion_rate": (
                float(completion_rate)
                if isinstance(completion_rate, (int, float))
                else None
            ),
            "last_episode_at": usage.get("last_episode_at"),
            "episodes_count": int(episodes_count or 0),
            "is_executable": bool(is_executable),
            "executability_reason": executability_reason,
            "executability_detail": executability_detail,
            "definition_identity": definition_identity,
        },
        "authority": {
            "authoritative_store": "vontology",
            "derived_view_model": "workflow_studio.read_model.v1",
            "preview_required": True,
            "supported_edit_modes": ["authoring_spec", "workflow_description_proposal"],
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
        ),
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
    runtime_definition, runtime_source, _registry = _load_runtime_definition(
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

    action_registry = get_shared_durable_action_registry()
    known_workflow_ids = build_durable_workflow_registry_read_only(
        defer_parity_work=True
    ).all_workflow_ids()
    contract_validation = validate_workflow_definition_contract(
        definition=proposed_definition,
        supported_action_ids=action_registry.all_action_ids(),
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


def apply_workflow_authoring_spec(
    workflow_id: str,
    *,
    authoring_spec: Mapping[str, Any],
    base_definition_hash: str | None = None,
) -> dict[str, Any]:
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
    publication = publish_workflow_definition_from_definition(
        definition=definition,
        create_missing=True,
        purpose=purpose or None,
    )
    return {
        "workflow_id": _clean_text(workflow_id),
        "publication": publication,
        "preview": _as_mapping(preview.get("preview")),
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
    "WorkflowStudioConflictError",
    "apply_workflow_authoring_spec",
    "build_workflow_catalogue_payload",
    "build_workflow_description_proposal",
    "build_workflow_studio_detail_payload",
    "preview_workflow_authoring_spec",
    "validate_workflow_candidate",
]
