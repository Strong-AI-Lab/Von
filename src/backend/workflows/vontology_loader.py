from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.text_value_service import (
    get_preferred_text_for_concept,
    get_texts_for_concept,
)
from .prompt_metadata_resolution import (
    WORKFLOW_STEP_PROMPT_LINK_PREDICATE_ALIASES,
    build_workflow_prompt_contract,
    normalise_prompt_metadata_defaults,
    normalise_prompt_validation_policy,
    resolve_workflow_prompt_metadata,
)
from .plan_state_runtime import (
    normalise_workflow_completion_gate_spec,
    normalise_workflow_plan_state_policy_spec,
    normalise_workflow_step_checkpoint_policy_spec,
)
from .subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    build_subworkflow_contract,
)
from .workflow_action_contracts import resolve_workflow_action_target
from .engine import (
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowActionInvocation,
    WORKFLOW_STEP_EXECUTION_MODE_CONTROL,
    WORKFLOW_STEP_EXECUTION_MODE_DETERMINISTIC,
    WORKFLOW_STEP_EXECUTION_MODE_LLM,
    WORKFLOW_STEP_EXECUTION_MODE_SUBWORKFLOW,
    WorkflowTransitionSpec,
    _normalise_approval_gate_spec,
    _normalise_idempotency_policy_spec,
    _normalise_retry_policy_spec,
    build_transition_condition,
    normalise_workflow_step_execution_mode,
)

logger = logging.getLogger(__name__)

WORKFLOW_STEP_CONTROL_FLOW_CONCEPT_DATA_KEY = "workflow_step_control_flow"

# Canonical workflow graph predicates with legacy-compatible aliases.
# The first entry in each tuple is the preferred canonical concept predicate.
WORKFLOW_GRAPH_PREDICATE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "hasInitialStep": (
        "#V#hasInitialStep",
        "hasInitialStep",
        "#V#has_initial_step",
        "has_initial_step",
    ),
    "hasStep": ("#V#hasStep", "hasStep", "#V#has_step", "has_step"),
    "invokesAction": (
        "#V#invokesAction",
        "invokesAction",
        "#V#invokes_action",
        "invokes_action",
    ),
    "invokesWorkflow": (
        "#V#invokesWorkflow",
        "invokesWorkflow",
        "#V#invokes_workflow",
        "invokes_workflow",
        "#V#workflow_step_invokes_workflow",
        "workflow_step_invokes_workflow",
        "#V#workflowStepInvokesWorkflow",
        "workflowStepInvokesWorkflow",
    ),
    "workflowStepInvokesTool": (
        "#V#workflow_step_invokes_tool",
        "workflow_step_invokes_tool",
        "#V#workflowStepInvokesTool",
        "workflowStepInvokesTool",
    ),
    "workflowStepUsesLlmPrompt": WORKFLOW_STEP_PROMPT_LINK_PREDICATE_ALIASES,
    "nextStep": ("#V#nextStep", "nextStep", "#V#next_step", "next_step"),
    "onTrueNextStep": (
        "#V#onTrueNextStep",
        "onTrueNextStep",
        "#V#on_true_next_step",
        "on_true_next_step",
    ),
    "onFalseNextStep": (
        "#V#onFalseNextStep",
        "onFalseNextStep",
        "#V#on_false_next_step",
        "on_false_next_step",
    ),
    "onFailureNextStep": (
        "#V#onFailureNextStep",
        "onFailureNextStep",
        "#V#on_failure_next_step",
        "on_failure_next_step",
    ),
    "onUnknownNextStep": (
        "#V#onUnknownNextStep",
        "onUnknownNextStep",
        "#V#on_unknown_next_step",
        "on_unknown_next_step",
    ),
    "onApprovalRequiredNextStep": (
        "#V#onApprovalRequiredNextStep",
        "onApprovalRequiredNextStep",
        "#V#on_approval_required_next_step",
        "on_approval_required_next_step",
    ),
    "onBreakNextStep": (
        "#V#onBreakNextStep",
        "onBreakNextStep",
        "#V#on_break_next_step",
        "on_break_next_step",
    ),
    "onContinueNextStep": (
        "#V#onContinueNextStep",
        "onContinueNextStep",
        "#V#on_continue_next_step",
        "on_continue_next_step",
    ),
    "hasPrecondition": (
        "#V#hasPrecondition",
        "hasPrecondition",
        "#V#has_precondition",
        "has_precondition",
    ),
    "hasEffect": ("#V#hasEffect", "hasEffect", "#V#has_effect", "has_effect"),
    "readsVariable": (
        "#V#readsVariable",
        "readsVariable",
        "#V#reads_variable",
        "reads_variable",
    ),
    "writesVariable": (
        "#V#writesVariable",
        "writesVariable",
        "#V#writes_variable",
        "writes_variable",
    ),
    "hasInputMap": (
        "#V#hasInputMap",
        "hasInputMap",
        "#V#has_input_map",
        "has_input_map",
    ),
    "workflowStepMapsContextKeyToToolParam": (
        "#V#workflow_step_maps_context_key_to_tool_param",
        "workflow_step_maps_context_key_to_tool_param",
        "#V#workflowStepMapsContextKeyToToolParam",
        "workflowStepMapsContextKeyToToolParam",
    ),
    "workflowStepWritesContextKey": (
        "#V#workflow_step_writes_context_key",
        "workflow_step_writes_context_key",
        "#V#workflowStepWritesContextKey",
        "workflowStepWritesContextKey",
    ),
    "workflowStepMapsToolOutputFieldToContextKey": (
        "#V#workflow_step_maps_tool_output_field_to_context_key",
        "workflow_step_maps_tool_output_field_to_context_key",
        "#V#workflowStepMapsToolOutputFieldToContextKey",
        "workflowStepMapsToolOutputFieldToContextKey",
    ),
    "hasWorkflowVariable": (
        "#V#hasWorkflowVariable",
        "hasWorkflowVariable",
        "#V#has_workflow_variable",
        "has_workflow_variable",
    ),
}

# Preferred roots for workflow type discovery.
WORKFLOW_DISCOVERY_BASE_TYPE_IDS: Tuple[str, ...] = (
    "#V#ai_workflow",
    "#V#durable_workflow",
    "#V#workflow",
    "#V#llm_workflow",
)

WORKFLOW_STEP_VACUITY_REASON_CODE = "workflow_step_vacuous"

# Canonical narrative precedence for workflow introspection surfaces.
# Keep ordering stable so monitor output is deterministic across code paths.
WORKFLOW_DESCRIPTION_TEXT_PREDICATE_PRECEDENCE: Tuple[Tuple[str, ...], ...] = (
    ("hasDefinition", "#V#hasDefinition"),
    ("hasContent", "#V#hasContent"),
    ("hasDescription", "#V#hasDescription"),
)
# Retrieval/routing should prefer explicit descriptions over longer-form
# narrative/content text so capability matching consumes the most targeted
# workflow summary available in Vontology.
WORKFLOW_ROUTING_DESCRIPTION_TEXT_PREDICATE_PRECEDENCE: Tuple[
    Tuple[str, ...], ...
] = (
    ("#V#hasDescription", "hasDescription"),
    ("#V#hasContent", "hasContent"),
    ("#V#hasDefinition", "hasDefinition"),
)
WORKFLOW_DESCRIPTION_SOURCE_NONE = "none"
WORKFLOW_DESCRIPTION_SOURCE_REGISTRATION = "registration.purpose"
WORKFLOW_DESCRIPTION_SOURCE_DEFINITION = "definition.purpose"
WORKFLOW_BACKGROUND_LAUNCH_POLICY_SOURCE_NONE = "none"
WORKFLOW_BACKGROUND_LAUNCH_POLICY_SCHEMA_VERSION = (
    "workflow_background_launch_policy.v1"
)
WORKFLOW_PUBLICATION_LIFECYCLE_SOURCE_NONE = "none"
WORKFLOW_PUBLICATION_LIFECYCLE_SCHEMA_VERSION = (
    "workflow_publication_lifecycle.v1"
)
WORKFLOW_PUBLICATION_LIFECYCLE_CONCEPT_DATA_KEY = "workflow_publication_lifecycle"
WORKFLOW_PUBLICATION_LIFECYCLE_TEXT_PREDICATE_PRECEDENCE: Tuple[
    Tuple[str, ...], ...
] = (
    (
        "#V#hasWorkflowLifecycleJson",
        "hasWorkflowLifecycleJson",
        "#V#has_workflow_lifecycle_json",
        "has_workflow_lifecycle_json",
    ),
)
WORKFLOW_STEP_RETRY_POLICY_TEXT_PREDICATE_PRECEDENCE: Tuple[Tuple[str, ...], ...] = (
    (
        "#V#hasWorkflowStepRetryPolicyJson",
        "hasWorkflowStepRetryPolicyJson",
        "#V#has_workflow_step_retry_policy_json",
        "has_workflow_step_retry_policy_json",
    ),
)
WORKFLOW_STEP_APPROVAL_GATE_TEXT_PREDICATE_PRECEDENCE: Tuple[Tuple[str, ...], ...] = (
    (
        "#V#hasWorkflowStepApprovalGateJson",
        "hasWorkflowStepApprovalGateJson",
        "#V#has_workflow_step_approval_gate_json",
        "has_workflow_step_approval_gate_json",
    ),
)
WORKFLOW_STEP_IDEMPOTENCY_POLICY_TEXT_PREDICATE_PRECEDENCE: Tuple[
    Tuple[str, ...], ...
] = (
    (
        "#V#hasWorkflowStepIdempotencyPolicyJson",
        "hasWorkflowStepIdempotencyPolicyJson",
        "#V#has_workflow_step_idempotency_policy_json",
        "has_workflow_step_idempotency_policy_json",
    ),
)
WORKFLOW_STEP_CHECKPOINT_POLICY_TEXT_PREDICATE_PRECEDENCE: Tuple[
    Tuple[str, ...], ...
] = (
    (
        "#V#hasWorkflowStepCheckpointPolicyJson",
        "hasWorkflowStepCheckpointPolicyJson",
        "#V#has_workflow_step_checkpoint_policy_json",
        "has_workflow_step_checkpoint_policy_json",
    ),
)
WORKFLOW_BACKGROUND_LAUNCH_POLICY_TEXT_PREDICATE_PRECEDENCE: Tuple[
    Tuple[str, ...], ...
] = (
    (
        "#V#hasBackgroundLaunchPolicyJson",
        "hasBackgroundLaunchPolicyJson",
        "#V#has_background_launch_policy_json",
        "has_background_launch_policy_json",
    ),
    (
        "#V#hasWorkflowLaunchPolicyJson",
        "hasWorkflowLaunchPolicyJson",
        "#V#has_workflow_launch_policy_json",
        "has_workflow_launch_policy_json",
    ),
    (
        "#V#hasBackgroundRunPolicyJson",
        "hasBackgroundRunPolicyJson",
        "#V#has_background_run_policy_json",
        "has_background_run_policy_json",
    ),
)
WORKFLOW_PLAN_STATE_POLICY_TEXT_PREDICATE_PRECEDENCE: Tuple[Tuple[str, ...], ...] = (
    (
        "#V#hasWorkflowPlanStatePolicyJson",
        "hasWorkflowPlanStatePolicyJson",
        "#V#has_workflow_plan_state_policy_json",
        "has_workflow_plan_state_policy_json",
    ),
)
WORKFLOW_COMPLETION_GATE_TEXT_PREDICATE_PRECEDENCE: Tuple[
    Tuple[str, ...], ...
] = (
    (
        "#V#hasWorkflowCompletionGateJson",
        "hasWorkflowCompletionGateJson",
        "#V#has_workflow_completion_gate_json",
        "has_workflow_completion_gate_json",
    ),
)
WORKFLOW_BACKGROUND_LAUNCH_INTERVAL_SECONDS_TEXT_PREDICATE_PRECEDENCE: Tuple[
    Tuple[str, ...], ...
] = (
    (
        "#V#hasMinimumBackgroundLaunchIntervalSeconds",
        "hasMinimumBackgroundLaunchIntervalSeconds",
        "#V#has_minimum_background_launch_interval_seconds",
        "has_minimum_background_launch_interval_seconds",
    ),
    (
        "#V#hasMinimumLaunchIntervalSeconds",
        "hasMinimumLaunchIntervalSeconds",
        "#V#has_minimum_launch_interval_seconds",
        "has_minimum_launch_interval_seconds",
    ),
)
WORKFLOW_BACKGROUND_LAUNCH_INTERVAL_MINUTES_TEXT_PREDICATE_PRECEDENCE: Tuple[
    Tuple[str, ...], ...
] = (
    (
        "#V#hasMinimumBackgroundLaunchIntervalMinutes",
        "hasMinimumBackgroundLaunchIntervalMinutes",
        "#V#has_minimum_background_launch_interval_minutes",
        "has_minimum_background_launch_interval_minutes",
    ),
    (
        "#V#hasMinimumLaunchIntervalMinutes",
        "hasMinimumLaunchIntervalMinutes",
        "#V#has_minimum_launch_interval_minutes",
        "has_minimum_launch_interval_minutes",
    ),
)

_WORKFLOW_MAPPING_ID_RE = re.compile(
    r"^#V#workflow_mapping_([a-z0-9_]+)_to_([a-z0-9_]+?)(?:_param(?:eter)?)?$",
    re.IGNORECASE,
)
_WORKFLOW_MAPPING_DESCRIPTION_RE = re.compile(
    r"context key\s+['\"]([^'\"]+)['\"].*?['\"]([^'\"]+)['\"]\s+parameter",
    re.IGNORECASE,
)
_WORKFLOW_PROMPT_DEFAULTS_INPUT_KEYS: tuple[str, ...] = (
    "__prompt_defaults",
    "prompt_defaults",
    "__prompt_defaults_json",
    "prompt_defaults_json",
)
_WORKFLOW_PROMPT_VALIDATION_POLICY_INPUT_KEYS: tuple[str, ...] = (
    "__prompt_validation_policy",
    "prompt_validation_policy",
)
_WORKFLOW_OUTPUT_MAPPING_ID_RE = re.compile(
    r"^#V#workflow_mapping_tool_field_([a-z0-9_]+)_to_([a-z0-9_]+)$",
    re.IGNORECASE,
)
_WORKFLOW_OUTPUT_MAPPING_DESCRIPTION_RE = re.compile(
    r"tool output field\s+['\"]([^'\"]+)['\"].*?context key\s+['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

_WORKFLOW_MAPPING_SCHEMA_VERSION = 1
_WORKFLOW_MAPPING_SPEC_CANDIDATE_KEYS: Tuple[str, ...] = (
    "workflow_mapping_spec",
    "workflow_mapping_spec_json",
    "workflow_mapping_json",
    "mapping_spec",
    "mapping_spec_json",
    "mapping_json",
)
_WORKFLOW_MAPPING_SPEC_ALLOWED_KEYS: Tuple[str, ...] = (
    "schema_version",
    "mapping_type",
    "workflow_step_id",
    "tool_id",
    "context_key_concept_id",
    "tool_param_name",
    "tool_output_field_name",
    "target_context_key_concept_id",
)
_WORKFLOW_INPUT_MAPPING_TYPES: Tuple[str, ...] = (
    "context_key_to_tool_param",
    "workflow_step_maps_context_key_to_tool_param",
)
_WORKFLOW_OUTPUT_MAPPING_TYPES: Tuple[str, ...] = (
    "tool_output_field_to_context_key",
    "workflow_step_maps_tool_output_field_to_context_key",
)


def _normalise_mapping_type(value: Any) -> str:
    return str(value or "").strip().lower()


def _coerce_mapping_spec_object(value: Any) -> Dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_structured_mapping_spec(
    mapping_doc: Dict[str, Any] | None,
) -> tuple[Dict[str, Any] | None, str | None]:
    """Extract a structured mapping spec from a mapping concept document.

    Preferred path for deterministic runtime behaviour:
    - read explicit schema fields from concept_data
    - fall back to concept-id and relation-backed description parsing only
      when structured schema is absent
    """

    if not isinstance(mapping_doc, dict):
        return None, None

    concept_data = mapping_doc.get("concept_data")

    candidate_values: List[Any] = []
    for key in _WORKFLOW_MAPPING_SPEC_CANDIDATE_KEYS:
        candidate_values.extend(
            (
                mapping_doc.get(key),
                concept_data.get(key) if isinstance(concept_data, dict) else None,
            )
        )

    saw_candidate = False
    for raw_value in candidate_values:
        if raw_value is None:
            continue
        saw_candidate = True
        spec_object = _coerce_mapping_spec_object(raw_value)
        if isinstance(spec_object, dict):
            return spec_object, None

    if saw_candidate:
        return None, "schema_invalid_format"
    return None, None


def _validate_mapping_spec_common(
    *,
    spec: Dict[str, Any],
    step_id: str | None,
    action_id: str | None,
) -> str | None:
    unknown_keys = [key for key in spec.keys() if key not in _WORKFLOW_MAPPING_SPEC_ALLOWED_KEYS]
    if unknown_keys:
        return "schema_unknown_fields"

    schema_version = spec.get("schema_version")
    if schema_version is not None:
        version_text = str(schema_version).strip()
        if version_text != str(_WORKFLOW_MAPPING_SCHEMA_VERSION):
            return "schema_version_unsupported"

    spec_step_id = str(spec.get("workflow_step_id") or "").strip()
    if step_id and spec_step_id and spec_step_id != step_id:
        return "schema_step_mismatch"

    spec_tool_id = _normalise_invoked_action_target(str(spec.get("tool_id") or ""))
    expected_action_id = _normalise_invoked_action_target(action_id or "")
    if expected_action_id and spec_tool_id and spec_tool_id != expected_action_id:
        return "schema_tool_mismatch"

    return None


def _normalise_relationship_targets(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def detect_vacuous_workflow_steps(
    *,
    workflow_id: str | None,
    graph: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Find workflow steps that have no executable contract.

    A step is considered vacuous when it contains no executable instruction:
      - no ``invokesAction`` target
      - no preconditions/effects
      - no variable read/write declarations

    This intentionally surfaces steps that appear structurally wired but do not
    declare what they should do, enabling fast diagnosis from previous step
    context before execution is attempted.
    """
    if not isinstance(graph, dict):
        return []

    steps = graph.get("steps")
    edges = graph.get("edges")
    if not isinstance(steps, list) or not steps:
        return []

    initial_step = graph.get("initial_step")
    if not isinstance(initial_step, str) or not initial_step.strip():
        initial_step = None

    step_by_id: Dict[str, Dict[str, Any]] = {}
    step_order: Dict[str, int] = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("step_id", "") or "").strip()
        if step_id:
            step_by_id[step_id] = step
            step_order[step_id] = index

    incoming: Dict[str, List[Dict[str, Any]]] = {key: [] for key in step_by_id}
    outgoing_count: Dict[str, int] = {key: 0 for key in step_by_id}
    if isinstance(edges, list):
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            from_step_id = str(edge.get("from", "") or "").strip()
            to_step_id = str(edge.get("to", "") or "").strip()
            predicate = edge.get("predicate")
            if from_step_id and to_step_id:
                outgoing_count[from_step_id] = outgoing_count.get(from_step_id, 0) + 1
                incoming.setdefault(to_step_id, []).append(
                    {
                        "from_step_id": from_step_id,
                        "predicate": predicate,
                    }
                )

    issues: List[Dict[str, Any]] = []

    for step in steps:
        if not isinstance(step, dict):
            continue

        step_id = str(step.get("step_id", "") or "").strip()
        if not step_id:
            continue

        invokes_action = str(step.get("invokes_action", "") or "").strip()
        invokes_workflow = str(step.get("invokes_workflow", "") or "").strip()
        preconditions = step.get("preconditions") or []
        effects = step.get("effects") or []
        reads_variables = step.get("reads_variables") or []
        writes_variables = step.get("writes_variables") or []
        context_input_mappings = step.get("context_input_mappings") or []
        tool_output_context_mappings = step.get("tool_output_context_mappings") or []
        writes_context_keys = step.get("writes_context_keys") or []

        has_contract = bool(
            invokes_action
            or invokes_workflow
            or preconditions
            or effects
            or reads_variables
            or writes_variables
            or context_input_mappings
            or tool_output_context_mappings
            or writes_context_keys
        )
        if has_contract:
            continue
        if step_id != initial_step and outgoing_count.get(step_id, 0) <= 0:
            # Terminal marker states are intentionally actionless. Treat the
            # leaf-node shape as authoritative rather than flagging them as
            # vacuous, or executable workflows get misclassified as design
            # artefacts during discovery/routing.
            continue

        previous_steps = incoming.get(step_id, [])
        previous_step_context: List[Dict[str, Any]] = []
        if previous_steps:
            for context in previous_steps:
                prev_step_id = str(context.get("from_step_id", "") or "").strip()
                prev_step = step_by_id.get(prev_step_id, {})
                previous_step_context.append(
                    {
                        "step_id": prev_step_id,
                        "step_name": prev_step.get("name"),
                        "invokes_action": str(prev_step.get("invokes_action", "") or "").strip()
                        or None,
                        "invokes_workflow": str(
                            prev_step.get("invokes_workflow", "") or ""
                        ).strip()
                        or None,
                        "link_predicate": context.get("predicate"),
                    }
                )
        elif step_id == initial_step:
            previous_step_context = [
                {
                    "step_id": "__workflow_input__",
                    "step_name": "Workflow input",
                    "invokes_action": None,
                    "invokes_workflow": None,
                    "link_predicate": None,
                }
            ]

        issues.append(
            {
                "workflow_id": str(workflow_id or "").strip() or None,
                "step_id": step_id,
                "step_name": str(step.get("name", "") or "").strip() or None,
                "step_index": step_order.get(step_id, -1),
                "reason_code": WORKFLOW_STEP_VACUITY_REASON_CODE,
                "cause": (
                    "No executable contract found for step. "
                    "Expected invokesAction/invokesWorkflow, preconditions/effects, "
                    "variable read/write declarations, "
                    "or context mapping declarations."
                ),
                "previous_steps": previous_step_context,
                "previous_step_count": len(previous_step_context),
            }
        )

    return issues


def _first_relationship_target(
    relationships: Mapping[str, Any], candidate_predicates: Iterable[str]
) -> Optional[str]:
    target, _ = _first_relationship_target_with_predicate(
        relationships, candidate_predicates
    )
    return target


def _all_relationship_targets(
    relationships: Mapping[str, Any], candidate_predicates: Iterable[str]
) -> List[str]:
    targets, _ = _all_relationship_targets_with_predicates(
        relationships, candidate_predicates
    )
    return targets


def _first_relationship_target_with_predicate(
    relationships: Mapping[str, Any], candidate_predicates: Iterable[str]
) -> Tuple[Optional[str], Optional[str]]:
    for predicate in candidate_predicates:
        targets = _normalise_relationship_targets(relationships.get(predicate))
        if targets:
            return targets[0], predicate
    return None, None


def _all_relationship_targets_with_predicates(
    relationships: Mapping[str, Any], candidate_predicates: Iterable[str]
) -> Tuple[List[str], List[str]]:
    results: List[str] = []
    matched_predicates: List[str] = []
    for predicate in candidate_predicates:
        targets = _normalise_relationship_targets(relationships.get(predicate))
        if targets:
            matched_predicates.append(predicate)
            results.extend(targets)
    # Preserve order but remove duplicates.
    seen: set[str] = set()
    unique: List[str] = []
    for item in results:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique, matched_predicates


def _record_legacy_alias_use(
    *,
    legacy_aliases: set[str],
    canonical_predicate: str,
    matched_predicates: Iterable[Optional[str]],
) -> None:
    for matched in matched_predicates:
        if isinstance(matched, str) and matched and matched != canonical_predicate:
            legacy_aliases.add(matched)


def _normalise_context_key_symbol(raw_symbol: str) -> str:
    symbol = str(raw_symbol or "").strip()
    if symbol.startswith("#V#workflow_context_key_"):
        return symbol[len("#V#workflow_context_key_") :]
    if symbol.startswith("workflow_context_key_"):
        return symbol[len("workflow_context_key_") :]
    if symbol.startswith("#V#"):
        return symbol[3:]
    return symbol


def _mapping_pair_from_concept_id(mapping_concept_id: str) -> tuple[str | None, str | None]:
    if not isinstance(mapping_concept_id, str):
        return None, None
    match = _WORKFLOW_MAPPING_ID_RE.match(mapping_concept_id.strip())
    if not match:
        return None, None
    context_key = _normalise_context_key_symbol(match.group(1))
    tool_param = match.group(2).strip()
    if context_key and tool_param:
        return context_key, tool_param
    return None, None


def _mapping_pair_from_description(description: str) -> tuple[str | None, str | None]:
    if not isinstance(description, str):
        return None, None
    match = _WORKFLOW_MAPPING_DESCRIPTION_RE.search(description)
    if not match:
        return None, None
    context_key = _normalise_context_key_symbol(match.group(1))
    tool_param = match.group(2).strip()
    if context_key and tool_param:
        return context_key, tool_param
    return None, None


def _extract_mapping_pair(
    *,
    mapping_concept_id: str,
    mapping_doc: Dict[str, Any] | None,
    step_id: str | None,
    action_id: str | None,
) -> tuple[str | None, str | None, str | None]:
    structured_spec, structured_error = _extract_structured_mapping_spec(mapping_doc)
    if structured_spec:
        mapping_type = _normalise_mapping_type(structured_spec.get("mapping_type"))
        if mapping_type not in _WORKFLOW_INPUT_MAPPING_TYPES:
            return None, None, "schema_mapping_type_mismatch"
        validation_error = _validate_mapping_spec_common(
            spec=structured_spec,
            step_id=step_id,
            action_id=action_id,
        )
        if validation_error:
            context_key, tool_param = _mapping_pair_from_concept_id(mapping_concept_id)
            if context_key and tool_param:
                return context_key, tool_param, None
            description = _mapping_description_text(mapping_concept_id, mapping_doc)
            context_key, tool_param = _mapping_pair_from_description(
                str(description or "")
            )
            if context_key and tool_param:
                return context_key, tool_param, None
            return None, None, validation_error
        context_key = _normalise_context_key_symbol(
            str(structured_spec.get("context_key_concept_id") or "")
        )
        tool_param = str(structured_spec.get("tool_param_name") or "").strip()
        if not context_key or not tool_param:
            return None, None, "schema_required_fields_missing"
        return context_key, tool_param, None
    if structured_error:
        return None, None, structured_error

    context_key, tool_param = _mapping_pair_from_concept_id(mapping_concept_id)
    if context_key and tool_param:
        return context_key, tool_param, None

    description = _mapping_description_text(mapping_concept_id, mapping_doc)
    context_key, tool_param = _mapping_pair_from_description(str(description or ""))
    if context_key and tool_param:
        return context_key, tool_param, None

    return None, None, "mapping_pattern_not_detected"


def _tool_output_mapping_pair_from_concept_id(
    mapping_concept_id: str,
) -> tuple[str | None, str | None]:
    if not isinstance(mapping_concept_id, str):
        return None, None
    match = _WORKFLOW_OUTPUT_MAPPING_ID_RE.match(mapping_concept_id.strip())
    if not match:
        return None, None
    tool_output_field = match.group(1).strip()
    context_key = _normalise_context_key_symbol(match.group(2))
    if tool_output_field and context_key:
        return tool_output_field, context_key
    return None, None


def _tool_output_mapping_pair_from_description(
    description: str,
) -> tuple[str | None, str | None]:
    if not isinstance(description, str):
        return None, None
    match = _WORKFLOW_OUTPUT_MAPPING_DESCRIPTION_RE.search(description)
    if not match:
        return None, None
    tool_output_field = match.group(1).strip()
    context_key = _normalise_context_key_symbol(match.group(2))
    if tool_output_field and context_key:
        return tool_output_field, context_key
    return None, None


def _mapping_description_text(
    mapping_concept_id: str,
    mapping_doc: Dict[str, Any] | None,
) -> str:
    """Resolve mapping narrative text from canonical text relations."""

    candidate_concept_id = (
        mapping_doc.get("concept_id") if isinstance(mapping_doc, dict) else None
    )
    if not isinstance(candidate_concept_id, str) or not candidate_concept_id.strip():
        candidate_concept_id = mapping_concept_id

    if isinstance(candidate_concept_id, str) and candidate_concept_id.strip():
        best = get_preferred_text_for_concept(
            candidate_concept_id.strip(),
            predicate_precedence=(
                ("hasDescription", "#V#hasDescription"),
                ("hasContent", "#V#hasContent"),
            ),
            preferred_languages=("en-NZ", "en"),
            limit=20,
        )
        text_value = best.get("text") if isinstance(best, dict) else None
        if isinstance(text_value, str) and text_value.strip():
            return text_value.strip()

    if isinstance(mapping_doc, dict):
        for raw_value in (
            mapping_doc.get("description"),
            mapping_doc.get("comment"),
            mapping_doc.get("attributes", {}).get("description")
            if isinstance(mapping_doc.get("attributes"), dict)
            else None,
        ):
            if isinstance(raw_value, str) and raw_value.strip():
                return raw_value.strip()

    return ""


def _parse_step_input_map(step_relationships: Mapping[str, Any]) -> Dict[str, Any]:
    input_map: Dict[str, Any] = {}
    if not isinstance(step_relationships, Mapping):
        return input_map

    raw_inputs = _all_relationship_targets(
        dict(step_relationships),
        WORKFLOW_GRAPH_PREDICATE_ALIASES["hasInputMap"],
    )
    for entry in raw_inputs:
        if "=" in entry:
            key, _, value = entry.partition("=")
        elif ":" in entry:
            key, _, value = entry.partition(":")
        else:
            continue
        key = key.strip()
        value = value.strip()
        if key:
            input_map[key] = value
    return input_map


def _extract_step_prompt_resolution_config(
    input_map: Dict[str, Any],
) -> tuple[dict[str, Any], str]:
    defaults_raw: Any = None
    for key in _WORKFLOW_PROMPT_DEFAULTS_INPUT_KEYS:
        if key not in input_map:
            continue
        defaults_raw = input_map.pop(key)
        break

    policy_raw: Any = None
    for key in _WORKFLOW_PROMPT_VALIDATION_POLICY_INPUT_KEYS:
        if key not in input_map:
            continue
        policy_raw = input_map.pop(key)
        break

    return (
        normalise_prompt_metadata_defaults(defaults_raw),
        normalise_prompt_validation_policy(policy_raw),
    )


_WORKFLOW_STEP_EXECUTION_MODE_INPUT_KEYS: tuple[str, ...] = (
    "execution_mode",
    "__execution_mode",
    "workflow_step_execution_mode",
    "step_execution_mode",
)
_WORKFLOW_STEP_LLM_POLICY_INPUT_KEYS: tuple[str, ...] = (
    "llm_policy",
    "__llm_policy",
    "workflow_step_llm_policy",
)
_WORKFLOW_STEP_VALIDATION_POLICY_INPUT_KEYS: tuple[str, ...] = (
    "validation_policy",
    "__validation_policy",
    "workflow_step_validation_policy",
)


def _extract_step_execution_mode(input_map: Dict[str, Any]) -> str | None:
    for key in _WORKFLOW_STEP_EXECUTION_MODE_INPUT_KEYS:
        if key not in input_map:
            continue
        return normalise_workflow_step_execution_mode(input_map.pop(key))
    return None


def _extract_step_llm_policy(
    *,
    input_map: Dict[str, Any],
    prompt_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    policy: dict[str, Any] = {}
    raw_policy: Any = None
    for key in _WORKFLOW_STEP_LLM_POLICY_INPUT_KEYS:
        if key not in input_map:
            continue
        raw_policy = input_map.pop(key)
        break

    if isinstance(raw_policy, Mapping):
        policy.update(dict(raw_policy))
    elif isinstance(raw_policy, str) and raw_policy.strip():
        try:
            parsed = json.loads(raw_policy)
        except Exception:
            parsed = None
        if isinstance(parsed, Mapping):
            policy.update(dict(parsed))

    prompt_contract_map = (
        prompt_contract if isinstance(prompt_contract, Mapping) else {}
    )
    prompt_metadata = prompt_contract_map.get("metadata")
    prompt_metadata_map = (
        prompt_metadata if isinstance(prompt_metadata, Mapping) else {}
    )

    requested_prompt_ids = prompt_contract_map.get("requested_prompt_concept_ids")
    if isinstance(requested_prompt_ids, list) and requested_prompt_ids:
        policy.setdefault("prompt_candidates", list(requested_prompt_ids))
    resolved_prompt_id = str(
        prompt_contract_map.get("resolved_prompt_concept_id") or ""
    ).strip()
    if resolved_prompt_id:
        policy.setdefault("selected_prompt_id", resolved_prompt_id)
    prompt_text = str(prompt_contract_map.get("prompt_text") or "").strip()
    if prompt_text:
        policy.setdefault("prompt_text", prompt_text)

    allowed_tools = prompt_metadata_map.get("allowed_tools")
    if isinstance(allowed_tools, list) and allowed_tools:
        policy.setdefault("allowed_tools", list(allowed_tools))
    tool_priority = prompt_metadata_map.get("tool_resolution_priority")
    if isinstance(tool_priority, list) and tool_priority:
        policy.setdefault("tool_resolution_priority", list(tool_priority))
    model_preference = prompt_metadata_map.get("model_preference")
    if isinstance(model_preference, str) and model_preference.strip():
        policy.setdefault("model_preference", model_preference.strip())
    agent_profiles = prompt_metadata_map.get("agent_profile_ids")
    if isinstance(agent_profiles, list) and agent_profiles:
        policy.setdefault("agent_profile_ids", list(agent_profiles))

    selection_policy = str(policy.get("selection_policy") or "").strip().lower()
    if selection_policy not in {"fixed", "adaptive", "bandit"}:
        policy["selection_policy"] = "adaptive"

    return policy


def _extract_step_validation_policy(
    input_map: Dict[str, Any],
) -> dict[str, Any] | None:
    raw_policy: Any = None
    for key in _WORKFLOW_STEP_VALIDATION_POLICY_INPUT_KEYS:
        if key not in input_map:
            continue
        raw_policy = input_map.pop(key)
        break

    policy: dict[str, Any] = {}
    if isinstance(raw_policy, Mapping):
        policy.update(dict(raw_policy))
    elif isinstance(raw_policy, str) and raw_policy.strip():
        try:
            parsed = json.loads(raw_policy)
        except Exception:
            parsed = None
        if isinstance(parsed, Mapping):
            policy.update(dict(parsed))

    return policy or None


def _extract_tool_output_mapping(
    *,
    mapping_concept_id: str,
    mapping_doc: Dict[str, Any] | None,
    step_id: str | None,
    action_id: str | None,
) -> tuple[str | None, str | None, str | None]:
    structured_spec, structured_error = _extract_structured_mapping_spec(mapping_doc)
    if structured_spec:
        mapping_type = _normalise_mapping_type(structured_spec.get("mapping_type"))
        if mapping_type not in _WORKFLOW_OUTPUT_MAPPING_TYPES:
            return None, None, "schema_mapping_type_mismatch"
        validation_error = _validate_mapping_spec_common(
            spec=structured_spec,
            step_id=step_id,
            action_id=action_id,
        )
        if validation_error:
            tool_output_field, context_key = _tool_output_mapping_pair_from_concept_id(
                mapping_concept_id
            )
            if tool_output_field and context_key:
                return tool_output_field, context_key, None
            description = _mapping_description_text(mapping_concept_id, mapping_doc)
            tool_output_field, context_key = _tool_output_mapping_pair_from_description(
                str(description or "")
            )
            if tool_output_field and context_key:
                return tool_output_field, context_key, None
            return None, None, validation_error
        tool_output_field = str(
            structured_spec.get("tool_output_field_name") or ""
        ).strip()
        context_key = _normalise_context_key_symbol(
            str(structured_spec.get("target_context_key_concept_id") or "")
        )
        if not tool_output_field or not context_key:
            return None, None, "schema_required_fields_missing"
        return tool_output_field, context_key, None
    if structured_error:
        return None, None, structured_error

    tool_output_field, context_key = _tool_output_mapping_pair_from_concept_id(
        mapping_concept_id
    )
    if tool_output_field and context_key:
        return tool_output_field, context_key, None

    description = _mapping_description_text(mapping_concept_id, mapping_doc)
    tool_output_field, context_key = _tool_output_mapping_pair_from_description(
        str(description or "")
    )
    if tool_output_field and context_key:
        return tool_output_field, context_key, None

    return None, None, "mapping_pattern_not_detected"


def _normalise_invoked_action_target(raw_target: str) -> str | None:
    """Normalise workflow action/tool relation targets into executable action IDs.

    Vontology may encode step actions directly as MCP method names
    (e.g. ``fetch_concept``) or as tool concept IDs
    (e.g. ``#V#fetch_concept_tool``).  The runtime action registry expects
    action IDs/method names, so concept IDs are reduced to their method form.
    """

    if not isinstance(raw_target, str):
        return None
    resolved_action_id, _contract_payload, _contract_source = (
        resolve_workflow_action_target(raw_target)
    )
    return resolved_action_id


def _normalise_invoked_workflow_target(raw_target: str) -> str | None:
    """Normalise workflow-subworkflow targets while preserving concept IDs."""

    if not isinstance(raw_target, str):
        return None
    target = raw_target.strip()
    if not target:
        return None
    return target


def _evaluate_transition_result_truth(context: Dict[str, Any]) -> bool:
    """Evaluate branching truth from explicit test outcomes when present.

    Read-back test steps can return payloads like ``{"result": False}`` while
    invocation status remains successful. Branch routing should therefore honour
    explicit test outcomes before falling back to generic success flags.
    """

    if "result" in context:
        result_value = context.get("result")
        if isinstance(result_value, dict) and "result" in result_value:
            return bool(result_value.get("result"))
        return bool(result_value)
    return bool(context.get("last_step_ok"))


def _normalise_non_empty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _extract_structured_workflow_purpose(text: str) -> str | None:
    """Extract a human-readable purpose from structured workflow JSON text."""

    raw_text = _normalise_non_empty_text(text)
    if raw_text is None or not raw_text.startswith("{"):
        return None

    try:
        payload = json.loads(raw_text)
    except Exception:
        return None

    if not isinstance(payload, Mapping):
        return None

    return _normalise_non_empty_text(payload.get("purpose"))


def _resolve_workflow_relation_text(
    workflow_id: str,
    *,
    predicate_precedence: Sequence[str | Sequence[str]],
) -> tuple[str | None, str]:
    """Resolve workflow text from canonical text relations with source telemetry."""

    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return None, WORKFLOW_DESCRIPTION_SOURCE_NONE

    preferred_text = get_preferred_text_for_concept(
        workflow_id.strip(),
        predicate_precedence=predicate_precedence,
        preferred_languages=("en-NZ", "en"),
        limit=200,
    )
    if not isinstance(preferred_text, Mapping):
        return None, WORKFLOW_DESCRIPTION_SOURCE_NONE

    predicate = str(preferred_text.get("predicate") or "").strip()
    text = _normalise_non_empty_text(preferred_text.get("text"))
    if not predicate or not text:
        return None, WORKFLOW_DESCRIPTION_SOURCE_NONE

    structured_purpose = _extract_structured_workflow_purpose(text)
    if structured_purpose:
        return structured_purpose, f"text_relation:{predicate}:purpose"
    return text, f"text_relation:{predicate}"


def resolve_workflow_narrative_text(workflow_id: str) -> tuple[str | None, str]:
    """Resolve workflow narrative text from canonical text relations.

    Precedence:
      1. hasDefinition
      2. hasContent
      3. hasDescription (including #V#hasDescription)
    """
    return _resolve_workflow_relation_text(
        workflow_id,
        predicate_precedence=WORKFLOW_DESCRIPTION_TEXT_PREDICATE_PRECEDENCE,
    )


def resolve_workflow_initial_step(
    workflow_id: str,
    workflow_doc: Mapping[str, Any] | None = None,
) -> str | None:
    """Resolve a workflow's initial step ID without loading the full definition."""

    workflow_id_text = _normalise_non_empty_text(workflow_id)
    if workflow_id_text is None:
        return None

    doc = workflow_doc
    if not isinstance(doc, Mapping):
        doc = ConceptsRepository.find_one(
            {"concept_id": workflow_id_text},
            {"concept_id": 1, "relationships": 1},
        )
    if not isinstance(doc, Mapping):
        return None

    relationships = doc.get("relationships") or {}
    if not isinstance(relationships, Mapping):
        return None

    initial_step = _first_relationship_target(
        relationships,
        WORKFLOW_GRAPH_PREDICATE_ALIASES["hasInitialStep"],
    )
    return _normalise_non_empty_text(initial_step)


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    return None


def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if value <= 0:
            return None
        return int(value)
    if isinstance(value, str):
        token = value.strip()
        if not token:
            return None
        try:
            parsed = float(token)
        except ValueError:
            return None
        if parsed <= 0:
            return None
        return int(parsed)
    return None


def _normalise_background_launch_scope(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"global", "global_per_server", "server", "per_server"}:
        return "global_per_server"
    return "global_per_server"


def _normalise_background_launch_sources(value: Any) -> list[str]:
    raw_items: list[str] = []
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [item for item in value if isinstance(item, str)]

    if not raw_items:
        return ["event"]

    normalised: list[str] = []
    for raw in raw_items:
        token = raw.strip().lower()
        if not token:
            continue
        if token in {"all", "*", "any"}:
            return ["all"]
        if token in {"event", "events", "event_trigger", "event_triggers"}:
            normalised.append("event")
            continue
        if token in {"schedule", "schedules"}:
            normalised.append("schedule")
            continue
        if token in {"manual", "manual_trigger", "manual_create"}:
            normalised.append("manual")
            continue

    deduped = sorted(set(normalised))
    return deduped or ["event"]


def _normalise_background_launch_policy(
    raw_policy: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(raw_policy, Mapping):
        return None

    interval_seconds: int | None = None
    for key in (
        "min_interval_seconds",
        "min_launch_interval_seconds",
        "minimum_interval_seconds",
        "minimum_launch_interval_seconds",
        "minimum_background_launch_interval_seconds",
        "cadence_seconds",
        "cooldown_seconds",
    ):
        interval_seconds = _coerce_positive_int(raw_policy.get(key))
        if interval_seconds is not None:
            break

    if interval_seconds is None:
        for key in (
            "min_interval_minutes",
            "min_launch_interval_minutes",
            "minimum_interval_minutes",
            "cadence_minutes",
            "cooldown_minutes",
        ):
            interval_minutes = _coerce_positive_int(raw_policy.get(key))
            if interval_minutes is not None:
                interval_seconds = int(interval_minutes) * 60
                break

    enabled = _coerce_bool(raw_policy.get("enabled"))
    if enabled is None and interval_seconds is not None:
        enabled = interval_seconds > 0
    if enabled is None:
        enabled = False

    if interval_seconds is None:
        if enabled:
            return None
        interval_seconds = 0

    if interval_seconds <= 0:
        enabled = False

    return {
        "schema_version": WORKFLOW_BACKGROUND_LAUNCH_POLICY_SCHEMA_VERSION,
        "enabled": bool(enabled),
        "min_interval_seconds": int(interval_seconds),
        "scope": _normalise_background_launch_scope(raw_policy.get("scope")),
        "applies_to_sources": _normalise_background_launch_sources(
            raw_policy.get("applies_to_sources")
            if "applies_to_sources" in raw_policy
            else raw_policy.get("applies_to")
        ),
    }


def _parse_background_launch_policy_text_value(text_value: Any) -> dict[str, Any] | None:
    text = _normalise_non_empty_text(text_value)
    if not text:
        return None

    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None

    if isinstance(parsed, Mapping):
        normalised = _normalise_background_launch_policy(parsed)
        if normalised is not None:
            return normalised

    seconds_value = _coerce_positive_int(text)
    if seconds_value is not None:
        return _normalise_background_launch_policy(
            {"min_interval_seconds": seconds_value}
        )
    return None


def resolve_workflow_background_launch_policy(
    workflow_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve background launch cadence policy from workflow representation.

    Policy is read from workflow text relations so cadence behaviour remains
    Vontology-defined rather than hard-coded in specialised orchestration.
    """

    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return None, WORKFLOW_BACKGROUND_LAUNCH_POLICY_SOURCE_NONE

    texts: list[dict[str, Any]] = []
    try:
        raw_texts = get_texts_for_concept(workflow_id)
    except Exception:
        raw_texts = []
    if isinstance(raw_texts, list):
        texts = [item for item in raw_texts if isinstance(item, dict)]

    invalid_sources: list[str] = []

    for predicate_aliases in WORKFLOW_BACKGROUND_LAUNCH_POLICY_TEXT_PREDICATE_PRECEDENCE:
        for item in texts:
            predicate = str(item.get("predicate") or "").strip()
            if predicate not in predicate_aliases:
                continue
            policy = _parse_background_launch_policy_text_value(item.get("text"))
            if policy is not None:
                return policy, f"text_relation:{predicate}"
            invalid_sources.append(f"text_relation_invalid:{predicate}")

    for (
        predicate_aliases
    ) in WORKFLOW_BACKGROUND_LAUNCH_INTERVAL_SECONDS_TEXT_PREDICATE_PRECEDENCE:
        for item in texts:
            predicate = str(item.get("predicate") or "").strip()
            if predicate not in predicate_aliases:
                continue
            interval_seconds = _coerce_positive_int(item.get("text"))
            if interval_seconds is not None:
                return (
                    {
                        "schema_version": WORKFLOW_BACKGROUND_LAUNCH_POLICY_SCHEMA_VERSION,
                        "enabled": True,
                        "min_interval_seconds": int(interval_seconds),
                        "scope": "global_per_server",
                        "applies_to_sources": ["event"],
                    },
                    f"text_relation:{predicate}",
                )
            invalid_sources.append(f"text_relation_invalid:{predicate}")

    for (
        predicate_aliases
    ) in WORKFLOW_BACKGROUND_LAUNCH_INTERVAL_MINUTES_TEXT_PREDICATE_PRECEDENCE:
        for item in texts:
            predicate = str(item.get("predicate") or "").strip()
            if predicate not in predicate_aliases:
                continue
            interval_minutes = _coerce_positive_int(item.get("text"))
            if interval_minutes is not None:
                return (
                    {
                        "schema_version": WORKFLOW_BACKGROUND_LAUNCH_POLICY_SCHEMA_VERSION,
                        "enabled": True,
                        "min_interval_seconds": int(interval_minutes) * 60,
                        "scope": "global_per_server",
                        "applies_to_sources": ["event"],
                    },
                    f"text_relation:{predicate}",
                )
            invalid_sources.append(f"text_relation_invalid:{predicate}")

    if invalid_sources:
        return None, invalid_sources[0]
    return None, WORKFLOW_BACKGROUND_LAUNCH_POLICY_SOURCE_NONE


def _parse_json_object_text_value(text_value: Any) -> dict[str, Any] | None:
    text = _normalise_non_empty_text(text_value)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    if isinstance(parsed, Mapping):
        return dict(parsed)
    return None


def _normalise_workflow_publication_lifecycle(
    value: Any,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None

    phase = _normalise_non_empty_text(value.get("phase")) or "published"
    published_raw = value.get("published")
    if isinstance(published_raw, bool):
        published = published_raw
    else:
        published = phase not in {
            "draft",
            "validated",
            "validation_failed",
            "draft_failed_completion_gate",
        }

    payload: dict[str, Any] = {
        "schema_version": (
            _normalise_non_empty_text(value.get("schema_version"))
            or WORKFLOW_PUBLICATION_LIFECYCLE_SCHEMA_VERSION
        ),
        "phase": phase,
        "published": bool(published),
    }
    for key in ("validation_passed", "postconditions_verified"):
        raw_flag = value.get(key)
        if isinstance(raw_flag, bool):
            payload[key] = raw_flag
    optional_test_instance_id = _normalise_non_empty_text(
        value.get("optional_test_instance_id")
    )
    if optional_test_instance_id:
        payload["optional_test_instance_id"] = optional_test_instance_id
    last_error = _normalise_non_empty_text(value.get("last_error"))
    if last_error:
        payload["last_error"] = last_error
    return payload


def resolve_workflow_publication_lifecycle(
    workflow_id: str,
    workflow_doc: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve explicit workflow publication lifecycle metadata.

    Draft workflows must remain loadable so the workflow-creation authoring flow
    can validate them before publication, but discovery/routing should ignore
    workflows whose lifecycle explicitly says ``published=false``.
    """

    workflow_id_text = _normalise_non_empty_text(workflow_id)
    if not workflow_id_text:
        return None, WORKFLOW_PUBLICATION_LIFECYCLE_SOURCE_NONE

    concept_doc = workflow_doc if isinstance(workflow_doc, Mapping) else None
    if concept_doc is None:
        try:
            raw_doc = ConceptsRepository.find_one(
                {"concept_id": workflow_id_text},
                {"concept_id": 1, "concept_data": 1},
            )
        except Exception:
            raw_doc = None
        if isinstance(raw_doc, Mapping):
            concept_doc = raw_doc

    concept_data = (
        concept_doc.get("concept_data") if isinstance(concept_doc, Mapping) else None
    )
    if isinstance(concept_data, Mapping):
        raw_lifecycle = concept_data.get(
            WORKFLOW_PUBLICATION_LIFECYCLE_CONCEPT_DATA_KEY
        )
        if isinstance(raw_lifecycle, Mapping):
            normalised = _normalise_workflow_publication_lifecycle(raw_lifecycle)
            if normalised is not None:
                return normalised, "concept_data"

    lifecycle, lifecycle_source = _resolve_policy_from_text_relations(
        concept_id=workflow_id_text,
        predicate_precedence=WORKFLOW_PUBLICATION_LIFECYCLE_TEXT_PREDICATE_PRECEDENCE,
        normaliser=_normalise_workflow_publication_lifecycle,
    )
    if lifecycle is not None:
        return lifecycle, lifecycle_source or WORKFLOW_PUBLICATION_LIFECYCLE_SOURCE_NONE
    if isinstance(lifecycle_source, str) and lifecycle_source:
        return None, lifecycle_source
    return None, WORKFLOW_PUBLICATION_LIFECYCLE_SOURCE_NONE


def _resolve_policy_from_text_relations(
    *,
    concept_id: str,
    predicate_precedence: Tuple[Tuple[str, ...], ...],
    normaliser: Callable[[Any], dict[str, Any] | None],
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw_texts = get_texts_for_concept(concept_id)
    except Exception:
        raw_texts = []
    texts = [item for item in raw_texts if isinstance(item, Mapping)]
    invalid_sources: list[str] = []

    for predicate_aliases in predicate_precedence:
        for item in texts:
            predicate = str(item.get("predicate") or "").strip()
            if predicate not in predicate_aliases:
                continue
            parsed = _parse_json_object_text_value(item.get("text"))
            if parsed is None:
                invalid_sources.append(f"text_relation_invalid:{predicate}:json_invalid")
                continue
            try:
                normalised = normaliser(parsed)
            except ValueError as exc:
                invalid_sources.append(f"text_relation_invalid:{predicate}:{exc}")
                continue
            if normalised is not None:
                return normalised, f"text_relation:{predicate}"

    if invalid_sources:
        return None, invalid_sources[0]
    return None, None


def resolve_workflow_step_runtime_policies(
    step_id: str,
) -> tuple[dict[str, Any], list[str]]:
    policies: dict[str, Any] = {}
    warnings: list[str] = []
    if not isinstance(step_id, str) or not step_id.strip():
        return policies, warnings

    retry_policy, retry_source = _resolve_policy_from_text_relations(
        concept_id=step_id,
        predicate_precedence=WORKFLOW_STEP_RETRY_POLICY_TEXT_PREDICATE_PRECEDENCE,
        normaliser=_normalise_retry_policy_spec,
    )
    if retry_policy is not None:
        policies["retry_policy"] = retry_policy
    elif isinstance(retry_source, str) and retry_source:
        warnings.append(f"workflow_step_retry_policy_invalid:{step_id}:{retry_source}")

    approval_gate, approval_source = _resolve_policy_from_text_relations(
        concept_id=step_id,
        predicate_precedence=WORKFLOW_STEP_APPROVAL_GATE_TEXT_PREDICATE_PRECEDENCE,
        normaliser=_normalise_approval_gate_spec,
    )
    if approval_gate is not None:
        policies["approval_gate"] = approval_gate
    elif isinstance(approval_source, str) and approval_source:
        warnings.append(
            f"workflow_step_approval_gate_invalid:{step_id}:{approval_source}"
        )

    idempotency_policy, idempotency_source = _resolve_policy_from_text_relations(
        concept_id=step_id,
        predicate_precedence=WORKFLOW_STEP_IDEMPOTENCY_POLICY_TEXT_PREDICATE_PRECEDENCE,
        normaliser=_normalise_idempotency_policy_spec,
    )
    if idempotency_policy is not None:
        policies["idempotency_policy"] = idempotency_policy
    elif isinstance(idempotency_source, str) and idempotency_source:
        warnings.append(
            "workflow_step_idempotency_policy_invalid:"
            f"{step_id}:{idempotency_source}"
        )

    checkpoint_policy, checkpoint_source = _resolve_policy_from_text_relations(
        concept_id=step_id,
        predicate_precedence=WORKFLOW_STEP_CHECKPOINT_POLICY_TEXT_PREDICATE_PRECEDENCE,
        normaliser=normalise_workflow_step_checkpoint_policy_spec,
    )
    if checkpoint_policy is not None:
        policies["checkpoint_policy"] = checkpoint_policy
    elif isinstance(checkpoint_source, str) and checkpoint_source:
        warnings.append(
            "workflow_step_checkpoint_policy_invalid:"
            f"{step_id}:{checkpoint_source}"
        )

    return policies, warnings


def resolve_workflow_long_horizon_policies(
    workflow_id: str,
) -> tuple[dict[str, Any], list[str]]:
    policies: dict[str, Any] = {}
    warnings: list[str] = []
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return policies, warnings

    plan_state_policy, plan_state_source = _resolve_policy_from_text_relations(
        concept_id=workflow_id,
        predicate_precedence=WORKFLOW_PLAN_STATE_POLICY_TEXT_PREDICATE_PRECEDENCE,
        normaliser=normalise_workflow_plan_state_policy_spec,
    )
    if plan_state_policy is not None:
        policies["plan_state_policy"] = plan_state_policy
    elif isinstance(plan_state_source, str) and plan_state_source:
        warnings.append(
            f"workflow_plan_state_policy_invalid:{workflow_id}:{plan_state_source}"
        )

    completion_gate, completion_gate_source = _resolve_policy_from_text_relations(
        concept_id=workflow_id,
        predicate_precedence=WORKFLOW_COMPLETION_GATE_TEXT_PREDICATE_PRECEDENCE,
        normaliser=normalise_workflow_completion_gate_spec,
    )
    if completion_gate is not None:
        policies["completion_gate"] = completion_gate
    elif isinstance(completion_gate_source, str) and completion_gate_source:
        warnings.append(
            "workflow_completion_gate_invalid:"
            f"{workflow_id}:{completion_gate_source}"
        )

    return policies, warnings


def resolve_workflow_description(
    workflow_id: str,
    *,
    workflow_source: str | None = None,
    registration_purpose: Any = None,
    definition_purpose: Any = None,
) -> tuple[str, str]:
    """Resolve the best workflow description for routing/listing surfaces.

    Routing and discoverability should prefer explicit ``hasDescription`` text
    over longer-form narrative/content relations, with registration/definition
    purpose as the non-Vontology fallback.
    """

    registration_text = _normalise_non_empty_text(registration_purpose)
    definition_text = _normalise_non_empty_text(definition_purpose)
    source_token = str(workflow_source or "").strip().lower()

    if source_token == "vontology":
        relation_text, relation_source = _resolve_workflow_relation_text(
            workflow_id,
            predicate_precedence=WORKFLOW_ROUTING_DESCRIPTION_TEXT_PREDICATE_PRECEDENCE,
        )
        if relation_text:
            return relation_text, relation_source
        if registration_text:
            return registration_text, WORKFLOW_DESCRIPTION_SOURCE_REGISTRATION
        if definition_text:
            return definition_text, WORKFLOW_DESCRIPTION_SOURCE_DEFINITION
        return "", relation_source

    if registration_text:
        return registration_text, WORKFLOW_DESCRIPTION_SOURCE_REGISTRATION
    if definition_text:
        return definition_text, WORKFLOW_DESCRIPTION_SOURCE_DEFINITION

    relation_text, relation_source = _resolve_workflow_relation_text(
        workflow_id,
        predicate_precedence=WORKFLOW_ROUTING_DESCRIPTION_TEXT_PREDICATE_PRECEDENCE,
    )
    if relation_text:
        return relation_text, relation_source
    return "", relation_source


def best_effort_workflow_narrative_text(workflow_id: str) -> Optional[str]:
    """Fetch a stored workflow definition *narrative* text from Vontology.

    Workflows should be represented structurally via explicit relationships
    (steps + control flow). This text is optional and is not machine-parsed.
    """
    narrative_text, _ = resolve_workflow_narrative_text(workflow_id)
    return narrative_text


def _fetch_concepts_by_id(concept_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not concept_ids:
        return {}

    cursor = ConceptsRepository.find(
        {"concept_id": {"$in": concept_ids}},
        {
            "concept_id": 1,
            "name": 1,
            "relationships": 1,
            "concept_data": 1,
            "attributes": 1,
            "description": 1,
            "comment": 1,
        },
        limit=len(concept_ids),
    )
    docs = list(cursor)
    mapping: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        cid = doc.get("concept_id")
        if isinstance(cid, str) and cid.strip():
            mapping[cid] = doc
    return mapping


def _resolve_step_control_flow_metadata(
    step_doc: Mapping[str, Any],
) -> dict[str, Any]:
    concept_data = step_doc.get("concept_data")
    if not isinstance(concept_data, Mapping):
        return {}

    raw_control_flow = concept_data.get(WORKFLOW_STEP_CONTROL_FLOW_CONCEPT_DATA_KEY)
    if not isinstance(raw_control_flow, Mapping):
        return {}

    explicit_branches = raw_control_flow.get("conditions")
    if explicit_branches is None:
        explicit_branches = raw_control_flow.get("declarative_conditions")
    if explicit_branches is None:
        return {}

    return {
        "conditions": explicit_branches,
    }


def build_workflow_process_graph(
    workflow_id: str,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Build a workflow definition from explicit Vontology relationships.

    Canonical graph predicates are defined in
    ``WORKFLOW_GRAPH_PREDICATE_ALIASES``. Legacy aliases are still accepted
    for compatibility and are surfaced as warnings.
    """

    warnings: List[str] = []

    workflow_doc = ConceptsRepository.find_one(
        {"concept_id": workflow_id},
        {"concept_id": 1, "name": 1, "relationships": 1, "concept_data": 1},
    )
    if not workflow_doc:
        return None, ["workflow_concept_not_found"]

    relationships = workflow_doc.get("relationships") or {}
    if not isinstance(relationships, dict):
        relationships = {}

    workflow_runtime_policies, workflow_policy_warnings = (
        resolve_workflow_long_horizon_policies(workflow_id)
    )
    warnings.extend(workflow_policy_warnings)
    publication_lifecycle, publication_lifecycle_source = (
        resolve_workflow_publication_lifecycle(workflow_id, workflow_doc)
    )
    if publication_lifecycle is not None:
        workflow_runtime_policies["publication_lifecycle"] = publication_lifecycle
    if (
        isinstance(publication_lifecycle_source, str)
        and publication_lifecycle_source
        and publication_lifecycle_source != WORKFLOW_PUBLICATION_LIFECYCLE_SOURCE_NONE
    ):
        if publication_lifecycle is not None:
            workflow_runtime_policies["publication_lifecycle_source"] = (
                publication_lifecycle_source
            )
        else:
            warnings.append(
                "workflow_publication_lifecycle_invalid:"
                f"{workflow_id}:{publication_lifecycle_source}"
            )

    legacy_aliases: set[str] = set()

    initial_step_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["hasInitialStep"]
    initial_step, initial_step_predicate = _first_relationship_target_with_predicate(
        relationships,
        initial_step_candidates,
    )
    _record_legacy_alias_use(
        legacy_aliases=legacy_aliases,
        canonical_predicate=initial_step_candidates[0],
        matched_predicates=(initial_step_predicate,),
    )

    has_step_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["hasStep"]
    step_ids, step_predicates = _all_relationship_targets_with_predicates(
        relationships,
        has_step_candidates,
    )
    _record_legacy_alias_use(
        legacy_aliases=legacy_aliases,
        canonical_predicate=has_step_candidates[0],
        matched_predicates=step_predicates,
    )

    # Read workflow-level variable declarations.
    variable_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["hasWorkflowVariable"]
    variable_ids, variable_predicates = _all_relationship_targets_with_predicates(
        relationships,
        variable_candidates,
    )
    _record_legacy_alias_use(
        legacy_aliases=legacy_aliases,
        canonical_predicate=variable_candidates[0],
        matched_predicates=variable_predicates,
    )
    variable_declarations: List[Dict[str, Any]] = []
    if variable_ids:
        var_docs = _fetch_concepts_by_id(variable_ids)
        for var_id in variable_ids:
            var_doc = var_docs.get(var_id)
            if not isinstance(var_doc, dict):
                warnings.append(f"workflow_variable_concept_missing:{var_id}")
                continue
            var_data = var_doc.get("concept_data") or {}
            if not isinstance(var_data, dict):
                var_data = {}
            var_name = str(
                var_data.get("variable_name")
                or var_data.get("key")
                or var_doc.get("name")
                or var_id
            ).strip()
            var_default = var_data.get("default_value")
            variable_declarations.append(
                {
                    "concept_id": var_id,
                    "name": var_name,
                    "default_value": var_default,
                }
            )

    if initial_step and initial_step not in step_ids:
        step_ids.insert(0, initial_step)
    if not initial_step and step_ids:
        initial_step = step_ids[0]
        warnings.append("missing_hasInitialStep_used_first_hasStep")

    if not step_ids:
        return None, ["workflow_has_no_steps"]

    step_docs = _fetch_concepts_by_id(step_ids)
    missing_steps = [sid for sid in step_ids if sid not in step_docs]
    if missing_steps:
        warnings.append(f"missing_step_concepts:{','.join(missing_steps[:25])}")

    step_items: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    def _edge(source_id: str, predicate: str, target_id: Optional[str]) -> None:
        if not target_id:
            return
        edges.append({"from": source_id, "predicate": predicate, "to": target_id})

    for step_id in step_ids:
        doc = step_docs.get(step_id, {})
        step_rels = doc.get("relationships") or {}
        if not isinstance(step_rels, dict):
            step_rels = {}
        step_input_map = _parse_step_input_map(step_rels)

        invokes_action_candidates = (
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["invokesAction"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["workflowStepInvokesTool"],
        )
        invokes_action_raw, invokes_action_predicate = _first_relationship_target_with_predicate(
            step_rels,
            invokes_action_candidates,
        )
        invokes_action_target = _normalise_non_empty_text(invokes_action_raw)
        invokes_action, action_contract_payload, action_contract_source = (
            resolve_workflow_action_target(str(invokes_action_raw or ""))
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=WORKFLOW_GRAPH_PREDICATE_ALIASES["invokesAction"][0],
            matched_predicates=(invokes_action_predicate,),
        )

        prompt_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["workflowStepUsesLlmPrompt"]
        prompt_concept_ids, prompt_predicates = _all_relationship_targets_with_predicates(
            step_rels,
            prompt_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=prompt_candidates[0],
            matched_predicates=prompt_predicates,
        )
        prompt_defaults, prompt_validation_policy = (
            _extract_step_prompt_resolution_config(dict(step_input_map))
        )
        prompt_contract: dict[str, Any] | None = None
        prompt_resolution_diagnostics: dict[str, Any] | None = None
        if prompt_concept_ids:
            prompt_resolution = resolve_workflow_prompt_metadata(
                prompt_concept_ids=prompt_concept_ids,
                defaults=prompt_defaults,
                validation_policy=prompt_validation_policy,
            )
            prompt_contract = build_workflow_prompt_contract(
                resolution=prompt_resolution,
                validation_policy=prompt_validation_policy,
            )
            prompt_resolution_diagnostics = dict(prompt_resolution.diagnostics)
            prompt_errors = prompt_resolution.diagnostics.get("errors") or []
            if prompt_errors:
                raise ValueError(
                    "workflow_prompt_contract_invalid:"
                    f"{workflow_id}:{step_id}:{'|'.join(str(item) for item in prompt_errors)}"
                )
            for warning in prompt_resolution.diagnostics.get("warnings") or []:
                warnings.append(
                    f"workflow_prompt_contract_warning:{workflow_id}:{step_id}:{warning}"
                )

        invokes_workflow_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES[
            "invokesWorkflow"
        ]
        invokes_workflow_raw, invokes_workflow_predicate = (
            _first_relationship_target_with_predicate(
                step_rels,
                invokes_workflow_candidates,
            )
        )
        invokes_workflow = _normalise_invoked_workflow_target(
            invokes_workflow_raw or ""
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=invokes_workflow_candidates[0],
            matched_predicates=(invokes_workflow_predicate,),
        )

        if invokes_action and invokes_workflow:
            warnings.append(
                "workflow_step_multiple_invocation_targets:"
                f"{step_id}:invokesAction+invokesWorkflow"
            )

        next_step_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["nextStep"]
        next_step, next_step_predicate = _first_relationship_target_with_predicate(
            step_rels,
            next_step_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=next_step_candidates[0],
            matched_predicates=(next_step_predicate,),
        )

        on_true_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["onTrueNextStep"]
        on_true, on_true_predicate = _first_relationship_target_with_predicate(
            step_rels,
            on_true_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=on_true_candidates[0],
            matched_predicates=(on_true_predicate,),
        )

        on_false_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["onFalseNextStep"]
        on_false, on_false_predicate = _first_relationship_target_with_predicate(
            step_rels,
            on_false_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=on_false_candidates[0],
            matched_predicates=(on_false_predicate,),
        )

        on_failure_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["onFailureNextStep"]
        on_failure, on_failure_predicate = _first_relationship_target_with_predicate(
            step_rels,
            on_failure_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=on_failure_candidates[0],
            matched_predicates=(on_failure_predicate,),
        )

        on_unknown_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["onUnknownNextStep"]
        on_unknown, on_unknown_predicate = _first_relationship_target_with_predicate(
            step_rels,
            on_unknown_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=on_unknown_candidates[0],
            matched_predicates=(on_unknown_predicate,),
        )

        on_approval_required_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES[
            "onApprovalRequiredNextStep"
        ]
        on_approval_required, on_approval_required_predicate = (
            _first_relationship_target_with_predicate(
                step_rels,
                on_approval_required_candidates,
            )
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=on_approval_required_candidates[0],
            matched_predicates=(on_approval_required_predicate,),
        )

        on_break_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["onBreakNextStep"]
        on_break, on_break_predicate = _first_relationship_target_with_predicate(
            step_rels,
            on_break_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=on_break_candidates[0],
            matched_predicates=(on_break_predicate,),
        )

        on_continue_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["onContinueNextStep"]
        on_continue, on_continue_predicate = _first_relationship_target_with_predicate(
            step_rels,
            on_continue_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=on_continue_candidates[0],
            matched_predicates=(on_continue_predicate,),
        )

        precondition_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["hasPrecondition"]
        preconditions, precondition_predicates = _all_relationship_targets_with_predicates(
            step_rels,
            precondition_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=precondition_candidates[0],
            matched_predicates=precondition_predicates,
        )

        effect_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["hasEffect"]
        effects, effect_predicates = _all_relationship_targets_with_predicates(
            step_rels,
            effect_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=effect_candidates[0],
            matched_predicates=effect_predicates,
        )

        reads_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["readsVariable"]
        reads_vars, reads_predicates = _all_relationship_targets_with_predicates(
            step_rels,
            reads_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=reads_candidates[0],
            matched_predicates=reads_predicates,
        )

        writes_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["writesVariable"]
        writes_vars, writes_predicates = _all_relationship_targets_with_predicates(
            step_rels,
            writes_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=writes_candidates[0],
            matched_predicates=writes_predicates,
        )

        input_mapping_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES[
            "workflowStepMapsContextKeyToToolParam"
        ]
        context_input_mappings, input_mapping_predicates = (
            _all_relationship_targets_with_predicates(
                step_rels,
                input_mapping_candidates,
            )
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=input_mapping_candidates[0],
            matched_predicates=input_mapping_predicates,
        )

        writes_context_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES[
            "workflowStepWritesContextKey"
        ]
        writes_context_keys, writes_context_predicates = (
            _all_relationship_targets_with_predicates(
                step_rels,
                writes_context_candidates,
            )
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=writes_context_candidates[0],
            matched_predicates=writes_context_predicates,
        )

        output_mapping_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES[
            "workflowStepMapsToolOutputFieldToContextKey"
        ]
        tool_output_context_mappings, output_mapping_predicates = (
            _all_relationship_targets_with_predicates(
                step_rels,
                output_mapping_candidates,
            )
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=output_mapping_candidates[0],
            matched_predicates=output_mapping_predicates,
        )

        runtime_policies, policy_warnings = resolve_workflow_step_runtime_policies(
            step_id
        )
        warnings.extend(policy_warnings)

        control_flow = {
            "next": next_step,
            "on_true": on_true,
            "on_false": on_false,
            "on_failure": on_failure,
            "on_unknown": on_unknown,
            "on_approval_required": on_approval_required,
            "on_break": on_break,
            "on_continue": on_continue,
        }
        control_flow.update(_resolve_step_control_flow_metadata(doc))

        step_items.append(
            {
                "step_id": step_id,
                "name": doc.get("name"),
                "invokes_action": invokes_action,
                "invokes_action_target": invokes_action_target,
                "invokes_workflow": invokes_workflow,
                "preconditions": preconditions,
                "effects": effects,
                "reads_variables": reads_vars,
                "writes_variables": writes_vars,
                "context_input_mappings": context_input_mappings,
                "tool_output_context_mappings": tool_output_context_mappings,
                "writes_context_keys": writes_context_keys,
                "prompt_contract": prompt_contract,
                "prompt_resolution_diagnostics": prompt_resolution_diagnostics,
                **runtime_policies,
                "control_flow": control_flow,
            }
        )
        if action_contract_payload:
            step_items[-1]["action_contract"] = action_contract_payload
        if action_contract_source:
            step_items[-1]["action_contract_source"] = action_contract_source

        _edge(step_id, "nextStep", next_step)
        _edge(step_id, "onTrueNextStep", on_true)
        _edge(step_id, "onFalseNextStep", on_false)
        _edge(step_id, "onFailureNextStep", on_failure)
        _edge(step_id, "onUnknownNextStep", on_unknown)
        _edge(step_id, "onApprovalRequiredNextStep", on_approval_required)
        _edge(step_id, "onBreakNextStep", on_break)
        _edge(step_id, "onContinueNextStep", on_continue)

    if legacy_aliases:
        warnings.append(
            f"legacy_workflow_predicates_used:{','.join(sorted(legacy_aliases))}"
        )

    definition = {
        "representation": "vontology_process_graph_v1",
        "workflow_id": workflow_id,
        "initial_step": initial_step,
        "workflow_metadata": workflow_runtime_policies,
        "variable_declarations": variable_declarations,
        "steps": step_items,
        "edges": edges,
        "warnings": warnings,
    }
    return definition, warnings


def load_workflow_definition_from_vontology(
    workflow_id: str,
) -> Optional[WorkflowDefinition]:
    """Load an executable WorkflowDefinition from Vontology.

    JVNAUTOSCI-922 Phase 3.2: This function converts the raw process graph
    produced by ``build_workflow_process_graph()`` into an executable
    ``WorkflowDefinition`` for the workflow engine.

    Fixes applied (Phase 3.2):
    - Correct key: reads ``initial_step`` (not ``initial_state``) from graph.
    - Declarative transition conditions: control-flow branches are compiled
      into validated declarative condition specs + deterministic evaluators.
    - ``on_failure`` transitions: mapped to a condition checking the
      ``last_action_failed`` context flag (set by the action registry on error).
    - ``on_unknown`` transitions: mapped to a condition checking the
      ``last_action_unknown`` context flag so ambiguous outcomes are routed
      explicitly.
    - Input mapping: reads ``hasInputMap`` / ``has_input_map`` relationships
      from step concepts to populate ``WorkflowActionInvocation.inputs``.
    - Context mapping: reads ``workflow_step_maps_context_key_to_tool_param``
      mapping concepts and encodes dynamic input bindings resolved at runtime.
    - Output mapping: reads
      ``workflow_step_maps_tool_output_field_to_context_key`` mapping concepts
      and carries structured tool-output→context write contracts.
    - Subworkflow invocation: reads ``invokesWorkflow`` predicates and encodes
      explicit subworkflow invocation contracts (inputs/outputs/failure mode)
      in per-state metadata.
    - Metadata: preconditions, effects, and variable read/write lists are
      carried through as ``WorkflowStateSpec.metadata`` for introspection, along
      with context read/write contract hints.
    """
    graph, warnings = build_workflow_process_graph(workflow_id)
    if not graph:
        return None
    fatal_warning_prefixes = (
        "workflow_step_retry_policy_invalid:",
        "workflow_step_approval_gate_invalid:",
        "workflow_step_idempotency_policy_invalid:",
        "workflow_step_checkpoint_policy_invalid:",
        "workflow_plan_state_policy_invalid:",
        "workflow_completion_gate_invalid:",
    )
    for warning in warnings:
        if isinstance(warning, str) and warning.startswith(fatal_warning_prefixes):
            raise ValueError(warning)

    # Correct key from build_workflow_process_graph output.
    initial_state_id = graph.get("initial_step")
    if not initial_state_id:
        steps = graph.get("steps")
        if steps and len(steps) > 0:
            initial_state_id = steps[0]["step_id"]

    if not initial_state_id:
        return None

    # Pre-fetch step docs for input-map reading.
    step_ids = [s["step_id"] for s in graph.get("steps", []) if s.get("step_id")]
    step_docs = _fetch_concepts_by_id(step_ids) if step_ids else {}
    mapping_ids: list[str] = []
    for step in graph.get("steps", []):
        for mapping_key in (
            "context_input_mappings",
            "tool_output_context_mappings",
        ):
            raw_mapping_ids = step.get(mapping_key)
            if not isinstance(raw_mapping_ids, list):
                continue
            for item in raw_mapping_ids:
                if isinstance(item, str) and item.strip():
                    mapping_ids.append(item.strip())
    mapping_docs = _fetch_concepts_by_id(mapping_ids) if mapping_ids else {}

    states: Dict[str, WorkflowStateSpec] = {}

    for step in graph.get("steps", []):
        step_id = step.get("step_id")
        invokes_action = step.get("invokes_action")
        invokes_workflow = step.get("invokes_workflow")
        has_static_workflow_invocation = isinstance(invokes_workflow, str) and bool(
            invokes_workflow.strip()
        )
        is_subworkflow_action = (
            (str(invokes_action or "").strip() == WORKFLOW_SUBWORKFLOW_ACTION_ID)
            or has_static_workflow_invocation
        )
        invocation_action_id = (
            WORKFLOW_SUBWORKFLOW_ACTION_ID if is_subworkflow_action else invokes_action
        )
        mapping_target_id = (
            invokes_workflow if has_static_workflow_invocation else invocation_action_id
        )
        control_flow = step.get("control_flow", {})

        actions: list[WorkflowActionInvocation] = []
        reads_context_keys: list[str] = []
        unresolved_input_mappings: list[str] = []
        invalid_input_mapping_specs: list[Dict[str, str]] = []
        tool_output_context_mappings: list[Dict[str, str]] = []
        unresolved_output_mappings: list[str] = []
        invalid_output_mapping_specs: list[Dict[str, str]] = []
        subworkflow_input_mappings: list[Dict[str, str]] = []
        subworkflow_output_mappings: list[Dict[str, str]] = []
        subworkflow_static_input_keys: list[str] = []
        subworkflow_failure_mode = ""
        action_contract: dict[str, Any] | None = None
        action_contract_source = ""
        step_has_prompt_contract = isinstance(step.get("prompt_contract"), Mapping)
        if invocation_action_id or step_has_prompt_contract:
            # Read input mapping from Vontology (hasInputMap relationships).
            doc = step_docs.get(step_id, {})
            step_rels = doc.get("relationships") or {}
            if not isinstance(step_rels, dict):
                step_rels = {}
            input_map = _parse_step_input_map(step_rels)
            _extract_step_prompt_resolution_config(input_map)
            configured_execution_mode = _extract_step_execution_mode(input_map)
            prompt_contract = (
                dict(step.get("prompt_contract"))
                if isinstance(step.get("prompt_contract"), Mapping)
                else None
            )
            action_contract = (
                dict(step.get("action_contract"))
                if isinstance(step.get("action_contract"), Mapping)
                else None
            )
            prompt_resolution_diagnostics = (
                dict(step.get("prompt_resolution_diagnostics"))
                if isinstance(step.get("prompt_resolution_diagnostics"), Mapping)
                else None
            )

            # Read semantic context mappings from dedicated mapping concepts.
            semantic_mapping_ids = step.get("context_input_mappings") or []
            if isinstance(semantic_mapping_ids, list):
                for mapping_concept_id in semantic_mapping_ids:
                    if not isinstance(mapping_concept_id, str) or not mapping_concept_id:
                        continue
                    context_key, tool_param, parse_error = _extract_mapping_pair(
                        mapping_concept_id=mapping_concept_id,
                        mapping_doc=mapping_docs.get(mapping_concept_id),
                        step_id=step_id,
                        action_id=mapping_target_id,
                    )
                    if not context_key or not tool_param:
                        unresolved_input_mappings.append(mapping_concept_id)
                        if parse_error:
                            invalid_input_mapping_specs.append(
                                {
                                    "mapping_concept_id": mapping_concept_id,
                                    "reason_code": parse_error,
                                }
                            )
                        continue
                    input_map[tool_param] = {
                        "$context_key": context_key,
                        "$mapping_concept_id": mapping_concept_id,
                    }
                    reads_context_keys.append(context_key)
                    if is_subworkflow_action:
                        subworkflow_input_mappings.append(
                            {
                                "child_input_key": tool_param,
                                "parent_context_key": context_key,
                                "mapping_concept_id": mapping_concept_id,
                            }
                        )
            if is_subworkflow_action:
                subworkflow_failure_mode = str(
                    input_map.get("failure_mode") or input_map.get("__failure_mode") or ""
                ).strip()
                subworkflow_static_input_keys = list(
                    dict.fromkeys(
                        str(key).strip()
                        for key, raw_value in input_map.items()
                        if isinstance(key, str)
                        and str(key).strip()
                        and isinstance(raw_value, str)
                        and str(raw_value).strip()
                        and str(key).strip() not in {"workflow_id", "failure_mode"}
                        and not str(key).strip().startswith("__")
                        and not str(key).strip().startswith("workflow_step_")
                    )
                )

            if has_static_workflow_invocation:
                input_map["workflow_id"] = str(invokes_workflow).strip()
                input_map["__parent_workflow_id"] = str(workflow_id or "").strip()
                input_map["__parent_state_id"] = str(step_id or "").strip()
            if prompt_resolution_diagnostics:
                input_map["__prompt_resolution_diagnostics"] = (
                    prompt_resolution_diagnostics
                )

            execution_mode = configured_execution_mode
            if not execution_mode:
                if has_static_workflow_invocation:
                    execution_mode = WORKFLOW_STEP_EXECUTION_MODE_SUBWORKFLOW
                elif prompt_contract:
                    if str(invocation_action_id or "").strip().startswith(
                        "workflow_control."
                    ):
                        execution_mode = WORKFLOW_STEP_EXECUTION_MODE_CONTROL
                    else:
                        execution_mode = WORKFLOW_STEP_EXECUTION_MODE_LLM
                elif str(invocation_action_id or "").strip().startswith(
                    "workflow_control."
                ):
                    execution_mode = WORKFLOW_STEP_EXECUTION_MODE_CONTROL
                else:
                    execution_mode = WORKFLOW_STEP_EXECUTION_MODE_DETERMINISTIC

            llm_policy = (
                _extract_step_llm_policy(
                    input_map=input_map,
                    prompt_contract=prompt_contract,
                )
                if execution_mode == WORKFLOW_STEP_EXECUTION_MODE_LLM
                else None
            )
            validation_policy = _extract_step_validation_policy(input_map)
            if (
                validation_policy is None
                and isinstance(prompt_contract, Mapping)
                and prompt_contract.get("validation_policy")
            ):
                validation_policy = {
                    "prompt_validation_policy": str(
                        prompt_contract.get("validation_policy") or ""
                    ).strip()
                }

            actions.append(
                WorkflowActionInvocation(
                    action_id=invocation_action_id,
                    inputs=input_map if input_map else {},
                    contract_concept_id=(
                        str(action_contract.get("concept_id") or "").strip()
                        if isinstance(action_contract, Mapping)
                        else None
                    )
                    or None,
                    execution_mode=execution_mode,
                    prompt_contract=prompt_contract,
                    llm_policy=llm_policy,
                    validation_policy=validation_policy,
                    subworkflow_id=(
                        str(invokes_workflow or "").strip() or None
                        if execution_mode == WORKFLOW_STEP_EXECUTION_MODE_SUBWORKFLOW
                        else None
                    ),
                )
            )

            semantic_output_mapping_ids = step.get("tool_output_context_mappings") or []
            if isinstance(semantic_output_mapping_ids, list):
                for mapping_concept_id in semantic_output_mapping_ids:
                    if not isinstance(mapping_concept_id, str) or not mapping_concept_id:
                        continue
                    tool_output_field, context_key, parse_error = _extract_tool_output_mapping(
                        mapping_concept_id=mapping_concept_id,
                        mapping_doc=mapping_docs.get(mapping_concept_id),
                        step_id=step_id,
                        action_id=mapping_target_id,
                    )
                    if not tool_output_field or not context_key:
                        unresolved_output_mappings.append(mapping_concept_id)
                        if parse_error:
                            invalid_output_mapping_specs.append(
                                {
                                    "mapping_concept_id": mapping_concept_id,
                                    "reason_code": parse_error,
                                }
                            )
                        continue
                    tool_output_context_mappings.append(
                        {
                            "tool_output_field": tool_output_field,
                            "context_key": context_key,
                            "mapping_concept_id": mapping_concept_id,
                        }
                    )
                    if is_subworkflow_action and tool_output_field.startswith("result."):
                        child_output_field = tool_output_field[len("result.") :].strip()
                        if not child_output_field:
                            continue
                        subworkflow_output_mappings.append(
                            {
                                "child_output_field": child_output_field,
                                "parent_context_key": context_key,
                                "mapping_concept_id": mapping_concept_id,
                            }
                        )

        transitions: list[WorkflowTransitionSpec] = []
        transition_condition_specs: list[Dict[str, Any]] = []

        on_failure_target = control_flow.get("on_failure")
        on_unknown_target = control_flow.get("on_unknown")
        on_approval_required_target = control_flow.get("on_approval_required")
        on_break_target = control_flow.get("on_break")
        on_continue_target = control_flow.get("on_continue")
        on_true_target = control_flow.get("on_true")
        on_false_target = control_flow.get("on_false")
        next_target = control_flow.get("next")

        def _append_transition_from_spec(
            *,
            to_state: str,
            reason: str,
            condition_spec: Mapping[str, Any],
        ) -> None:
            if not isinstance(to_state, str) or not to_state.strip():
                raise ValueError(
                    "workflow_transition_condition_invalid:"
                    f"missing_target:{workflow_id}:{step_id}:{reason}"
                )
            try:
                normalised_spec, compiled_condition = build_transition_condition(
                    condition_spec
                )
            except ValueError as exc:
                raise ValueError(
                    "workflow_transition_condition_invalid:"
                    f"{workflow_id}:{step_id}:{reason}:{exc}"
                ) from exc
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=to_state,
                    condition=compiled_condition,
                    condition_spec=normalised_spec,
                    reason=reason,
                )
            )
            transition_condition_specs.append(
                {
                    "to_state": to_state,
                    "reason": reason,
                    "condition_spec": normalised_spec,
                }
            )

        explicit_condition_branches = control_flow.get("conditions")
        if explicit_condition_branches is None:
            explicit_condition_branches = control_flow.get("declarative_conditions")
        if explicit_condition_branches is not None:
            if not isinstance(explicit_condition_branches, list):
                raise ValueError(
                    "workflow_transition_condition_invalid:"
                    f"{workflow_id}:{step_id}:conditions_not_list"
                )
            for index, branch in enumerate(explicit_condition_branches):
                if not isinstance(branch, Mapping):
                    raise ValueError(
                        "workflow_transition_condition_invalid:"
                        f"{workflow_id}:{step_id}:branch_not_mapping:{index}"
                    )
                to_state = (
                    str(
                        branch.get("to")
                        or branch.get("to_state")
                        or branch.get("next")
                        or ""
                    ).strip()
                )
                reason = str(branch.get("reason") or f"condition_{index + 1}").strip()
                raw_condition_spec = branch.get("condition")
                if not isinstance(raw_condition_spec, Mapping):
                    raise ValueError(
                        "workflow_transition_condition_invalid:"
                        f"{workflow_id}:{step_id}:{reason}:condition_missing"
                    )
                _append_transition_from_spec(
                    to_state=to_state,
                    reason=reason,
                    condition_spec=raw_condition_spec,
                )

        # Priority: on_failure → on_unknown → on_approval_required → on_break/on_continue →
        # on_true/on_false → next.
        # Canonical Vontology workflows rely on this deterministic ordering.
        if on_failure_target:
            _append_transition_from_spec(
                to_state=on_failure_target,
                reason="on_failure",
                condition_spec={
                    "kind": "context_flag",
                    "key": "last_action_failed",
                    "expected": True,
                },
            )

        if on_unknown_target:
            _append_transition_from_spec(
                to_state=on_unknown_target,
                reason="on_unknown",
                condition_spec={
                    "kind": "context_flag",
                    "key": "last_action_unknown",
                    "expected": True,
                },
            )

        if on_approval_required_target:
            _append_transition_from_spec(
                to_state=on_approval_required_target,
                reason="on_approval_required",
                condition_spec={
                    "kind": "context_flag",
                    "key": "approval_required",
                    "expected": True,
                },
            )

        if on_break_target:
            _append_transition_from_spec(
                to_state=on_break_target,
                reason="on_break",
                condition_spec={
                    "kind": "control_signal",
                    "signal": "break",
                },
            )

        if on_continue_target:
            _append_transition_from_spec(
                to_state=on_continue_target,
                reason="on_continue",
                condition_spec={
                    "kind": "control_signal",
                    "signal": "continue",
                },
            )

        if on_true_target:
            _append_transition_from_spec(
                to_state=on_true_target,
                reason="on_true",
                condition_spec={
                    "kind": "transition_result_truth",
                    "expected": True,
                },
            )

        if on_false_target:
            _append_transition_from_spec(
                to_state=on_false_target,
                reason="on_false",
                condition_spec={
                    "kind": "transition_result_truth",
                    "expected": False,
                },
            )

        if next_target:
            _append_transition_from_spec(
                to_state=next_target,
                reason="next_step",
                condition_spec={"kind": "always"},
            )

        is_terminal = not transitions

        # Carry Vontology metadata through for introspection.
        step_metadata: Dict[str, Any] = {}
        preconditions = step.get("preconditions")
        effects = step.get("effects")
        reads_vars = step.get("reads_variables")
        writes_vars = step.get("writes_variables")
        writes_context_keys = step.get("writes_context_keys")
        retry_policy = step.get("retry_policy")
        approval_gate = step.get("approval_gate")
        idempotency_policy = step.get("idempotency_policy")
        checkpoint_policy = step.get("checkpoint_policy")
        prompt_contract = step.get("prompt_contract")
        if preconditions:
            step_metadata["preconditions"] = preconditions
        if effects:
            step_metadata["effects"] = effects
        if reads_vars:
            step_metadata["reads_variables"] = reads_vars
        if reads_context_keys:
            step_metadata["reads_context_keys"] = reads_context_keys
        if writes_vars:
            step_metadata["writes_variables"] = writes_vars
        if writes_context_keys:
            step_metadata["writes_context_keys"] = writes_context_keys
        if retry_policy:
            step_metadata["retry_policy"] = retry_policy
        if approval_gate:
            step_metadata["approval_gate"] = approval_gate
        if idempotency_policy:
            step_metadata["idempotency_policy"] = idempotency_policy
        if checkpoint_policy:
            step_metadata["checkpoint_policy"] = checkpoint_policy
        if prompt_contract:
            step_metadata["prompt_contract"] = prompt_contract
        action_execution_modes = [
            str(getattr(action, "execution_mode", "") or "").strip()
            for action in actions
            if str(getattr(action, "execution_mode", "") or "").strip()
        ]
        if action_execution_modes:
            step_metadata["execution_modes"] = action_execution_modes
            if len(action_execution_modes) == 1:
                step_metadata["execution_mode"] = action_execution_modes[0]
        llm_policies = []
        for action in actions:
            raw_llm_policy = getattr(action, "llm_policy", None)
            if not isinstance(raw_llm_policy, Mapping):
                continue
            llm_policies.append(
                {
                    str(key): value
                    for key, value in raw_llm_policy.items()
                    if isinstance(key, str)
                }
            )
        if llm_policies:
            step_metadata["llm_policies"] = llm_policies
            if len(llm_policies) == 1:
                step_metadata["llm_policy"] = llm_policies[0]
        if action_contract:
            step_metadata["action_contract"] = action_contract
        action_contract_source = step.get("action_contract_source")
        if isinstance(action_contract_source, str) and action_contract_source.strip():
            step_metadata["action_contract_source"] = action_contract_source.strip()
        if is_subworkflow_action:
            workflow_target = str(invokes_workflow or "").strip()
            workflow_id_context_key = ""
            workflow_id_mapping_concept_id = ""
            workflow_id_input = input_map.get("workflow_id")
            if isinstance(workflow_id_input, Mapping):
                workflow_id_context_key = str(
                    workflow_id_input.get("$context_key") or ""
                ).strip()
                workflow_id_mapping_concept_id = str(
                    workflow_id_input.get("$mapping_concept_id") or ""
                ).strip()
            if workflow_target:
                step_metadata["invokes_workflow"] = workflow_target
            if workflow_target or workflow_id_context_key:
                step_metadata["subworkflow_contract"] = build_subworkflow_contract(
                    workflow_id=workflow_target or None,
                    workflow_id_context_key=workflow_id_context_key or None,
                    workflow_id_mapping_concept_id=(
                        workflow_id_mapping_concept_id or None
                    ),
                    input_mappings=subworkflow_input_mappings,
                    output_mappings=subworkflow_output_mappings,
                    static_input_keys=subworkflow_static_input_keys,
                    failure_mode=subworkflow_failure_mode,
                )
        if tool_output_context_mappings:
            step_metadata["tool_output_context_mappings"] = tool_output_context_mappings
        if unresolved_input_mappings:
            step_metadata["unresolved_input_mappings"] = unresolved_input_mappings
        if invalid_input_mapping_specs:
            step_metadata["invalid_input_mapping_specs"] = invalid_input_mapping_specs
        if unresolved_output_mappings:
            step_metadata["unresolved_output_mappings"] = unresolved_output_mappings
        if invalid_output_mapping_specs:
            step_metadata["invalid_output_mapping_specs"] = invalid_output_mapping_specs
        if transition_condition_specs:
            step_metadata["transition_condition_specs"] = transition_condition_specs

        states[step_id] = WorkflowStateSpec(
            state_id=step_id,
            actions=actions,
            transitions=transitions,
            terminal=is_terminal,
            metadata=step_metadata,
        )

    purpose = best_effort_workflow_narrative_text(workflow_id)
    background_launch_policy, background_launch_policy_source = (
        resolve_workflow_background_launch_policy(workflow_id)
    )
    workflow_metadata: Dict[str, Any] = {}
    graph_workflow_metadata = graph.get("workflow_metadata")
    if isinstance(graph_workflow_metadata, Mapping):
        workflow_metadata.update(dict(graph_workflow_metadata))
    if background_launch_policy is not None:
        workflow_metadata["background_launch_policy"] = background_launch_policy
    if (
        isinstance(background_launch_policy_source, str)
        and background_launch_policy_source
        and background_launch_policy_source != WORKFLOW_BACKGROUND_LAUNCH_POLICY_SOURCE_NONE
    ):
        workflow_metadata["background_launch_policy_source"] = (
            background_launch_policy_source
        )
    graph_variable_declarations = graph.get("variable_declarations")
    if isinstance(graph_variable_declarations, list) and graph_variable_declarations:
        workflow_metadata["variable_declarations"] = graph_variable_declarations

    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state=initial_state_id,
        states=states,
        purpose=purpose,
        metadata=workflow_metadata,
    )


def _get_recursive_subtypes(root_type_id: str, max_depth: int = 3) -> set[str]:
    """Find all concept IDs that are subtypes of the given root type (transitively)."""
    found = set()
    queue = [root_type_id]
    depth = 0

    while queue and depth < max_depth:
        # Find immediate subtypes: concepts where (is_a_type_of == current OR has_subtype contains current?? No.
        # Subtype relationship: Child -> is_a_type_of -> Parent
        # OR Parent -> has_subtype -> Child.
        # We should check both, but efficiently.

        # Query: relationships.is_a_type_of IN queue
        # AND relationships.is_a_type_of is a list of strings

        parents = list(queue)
        queue = []

        # Find children via is_a_type_of (most common)
        cursor = ConceptsRepository.find(
            {"relationships.is_a_type_of": {"$in": parents}}, {"concept_id": 1}
        )
        for doc in cursor:
            cid = doc.get("concept_id")
            if cid and cid not in found and cid != root_type_id:
                found.add(cid)
                queue.append(cid)

        # Find children via has_subtype (less common but possible)
        # We need to query concepts that have has_subtype pointing to... no...
        # We need to query the PARENT documents to see their has_subtype.

        parent_docs = ConceptsRepository.find(
            {"concept_id": {"$in": parents}},
            {"relationships.has_subtype": 1, "relationships.#V#has_subtype": 1},
        )
        for pdoc in parent_docs:
            rels = pdoc.get("relationships") or {}
            targets = _all_relationship_targets(rels, ("has_subtype", "#V#has_subtype"))
            for t in targets:
                if t and t not in found and t != root_type_id:
                    # Verify target exists and is a concept? optional
                    found.add(t)
                    queue.append(t)

        depth += 1

    return found


def _discover_workflow_type_family_ids() -> set[str]:
    """Return workflow base types plus discovered subtypes."""

    type_ids: set[str] = set()
    for base_type_id in WORKFLOW_DISCOVERY_BASE_TYPE_IDS:
        if isinstance(base_type_id, str) and base_type_id:
            type_ids.add(base_type_id)
            type_ids.update(_get_recursive_subtypes(base_type_id))
    return type_ids


def discover_workflow_ids() -> List[str]:
    """Find all potential Vontology-defined workflows.

    Heuristic:
    1. Concepts that define a workflow structure (have hasInitialStep aliases).
    2. Concepts typed under known workflow roots (instance or subtype paths)
       that define an initial step.
    """
    candidates = set()

    # 1. Direct property search (best effort, includes canonical and legacy aliases)
    try:
        direct_initial_step_query = [
            {f"relationships.{predicate}": {"$exists": True}}
            for predicate in WORKFLOW_GRAPH_PREDICATE_ALIASES["hasInitialStep"]
        ]
        cursor = ConceptsRepository.find(
            {"$or": direct_initial_step_query},
            {"concept_id": 1},
        )
        for doc in cursor:
            concept_id = doc.get("concept_id")
            if isinstance(concept_id, str) and concept_id.strip():
                candidates.add(concept_id.strip())
    except Exception as e:
        logger.warning(f"Error querying workflows by property: {e}")

    # 2. Type-based discovery over workflow type families. This catches
    # instance-typed workflow concepts that use canonical #V# graph predicates.
    workflow_type_ids = sorted(_discover_workflow_type_family_ids())
    if workflow_type_ids:
        try:
            typed_cursor = ConceptsRepository.find(
                {
                    "$or": [
                        {"relationships.is_an_instance_of": {"$in": workflow_type_ids}},
                        {"relationships.is_a_type_of": {"$in": workflow_type_ids}},
                    ]
                },
                {"concept_id": 1, "relationships": 1},
            )
            for doc in typed_cursor:
                concept_id = doc.get("concept_id")
                if not isinstance(concept_id, str) or not concept_id.strip():
                    continue
                rels = doc.get("relationships") or {}
                if not isinstance(rels, dict):
                    continue
                initial_step = _first_relationship_target(
                    rels,
                    WORKFLOW_GRAPH_PREDICATE_ALIASES["hasInitialStep"],
                )
                if initial_step:
                    candidates.add(concept_id.strip())
        except Exception as e:
            logger.warning(f"Error querying workflows by type: {e}")

    if not candidates:
        return []

    lifecycle_docs: dict[str, dict[str, Any]] = {}
    try:
        lifecycle_cursor = ConceptsRepository.find(
            {"concept_id": {"$in": sorted(candidates)}},
            {"concept_id": 1, "concept_data": 1},
        )
        for doc in lifecycle_cursor:
            concept_id = doc.get("concept_id")
            if isinstance(concept_id, str) and concept_id.strip():
                lifecycle_docs[concept_id.strip()] = dict(doc)
    except Exception as e:
        logger.warning(f"Error querying workflow publication lifecycle: {e}")

    discoverable_candidates: set[str] = set()
    for concept_id in candidates:
        lifecycle, _source = resolve_workflow_publication_lifecycle(
            concept_id,
            lifecycle_docs.get(concept_id),
        )
        if isinstance(lifecycle, Mapping) and lifecycle.get("published") is False:
            continue
        discoverable_candidates.add(concept_id)

    return sorted(discoverable_candidates)


def batch_fetch_workflow_purposes(
    workflow_ids: Iterable[str],
) -> Dict[str, str]:
    """Batch-fetch routing descriptions for workflow concept IDs.

    Returns {workflow_id: purpose_text} for IDs where a description is
    available. Uses ``resolve_workflow_description`` so lazy registrations and
    capability indexing consume the same workflow-description authority as
    routing/listing surfaces, then caps the result to the first 300 characters
    to keep the registry lightweight.

    JVNAUTOSCI-1424 Phase 2: Provides purpose metadata for lazy
    registrations so the workflow capability index has searchable text
    for Vontology-authored workflows without resolving full definitions.
    """
    purposes: Dict[str, str] = {}
    for wf_id in workflow_ids:
        if not isinstance(wf_id, str) or not wf_id.strip():
            continue
        try:
            text, _source = resolve_workflow_description(
                wf_id.strip(),
                workflow_source="vontology",
            )
            if text:
                purposes[wf_id.strip()] = text[:300]
        except Exception:
            continue
    return purposes
